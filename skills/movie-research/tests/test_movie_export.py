"""Unit tests for movie_export.py. Standard library only.

Run from the repo root:  python -m unittest discover -s skills/movie-research/tests -v
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import movie_export  # noqa: E402


def write_export(folder, lag_frames=(), inputs=None, snapshots=None, writes=(), frames=10, meta=None, execs=None,
                 write_hits=None):
    """Create a fake movie_recorder export folder.

    lag_frames: frames flagged as lag. inputs: {frame: "A|Start"}.
    snapshots: {(frame, domain): bytes}. writes: (frame, addr, old, new, pc) tuples.
    execs: (frame, addr, hit, registers text, stack hex, capture text) tuples;
    None writes no execs.tsv (as when no exec_watch is set). write_hits: the
    same tuples for write_hits.tsv.
    Returns: the folder path.
    """
    for name, rows in (("execs.tsv", execs), ("write_hits.tsv", write_hits)):
        if rows is not None:
            with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
                f.write("frame\taddress\thit\tregisters\tstack\tcapture\n")
                for x in rows:
                    f.write("%d\t0x%04X\t%d\t%s\t%s\t%s\n" % x)
    inputs = inputs or {}
    with open(os.path.join(folder, "frames.tsv"), "w", encoding="utf-8") as f:
        f.write("frame\tlag\tinput\n")
        for n in range(1, frames + 1):
            f.write(f"{n}\t{1 if n in lag_frames else 0}\t{inputs.get(n, '.')}\n")
    with open(os.path.join(folder, "snapshots.tsv"), "w", encoding="utf-8") as f:
        f.write("frame\tdomain\tfile\tbytes\treason\n")
        for (frame, domain), data in (snapshots or {}).items():
            reason = "every"
            if isinstance(data, tuple):  # (bytes, reason)
                data, reason = data
            name = f"snap_f{frame:07d}_{domain}.bin"
            with open(os.path.join(folder, name), "wb") as b:
                b.write(data)
            f.write(f"{frame}\t{domain}\t{name}\t{len(data)}\t{reason}\n")
    with open(os.path.join(folder, "writes.tsv"), "w", encoding="utf-8") as f:
        f.write("frame\taddress\told\tnew\tpc\n")
        for w in writes:
            f.write("%d\t0x%04X\t%d\t%d\t%s\n" % w)
    with open(os.path.join(folder, "run.tsv"), "w", encoding="utf-8") as f:
        f.write("key\tvalue\n")
        for k, v in (meta or {"rom_name": "Test", "started_at_frame": "0", "end_reason": "movie ended"}).items():
            f.write(f"{k}\t{v}\n")
    return folder


def run(argv):
    """Run main() with stdout captured. Returns: (exit code, text)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = movie_export.main(argv)
    return code, out.getvalue()


class Export(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def folder(self, name):
        path = os.path.join(self.tmp.name, name)
        os.makedirs(path)
        return path

    def test_lag_runs(self):
        frames = [{"frame": n, "lag": n in (2, 3, 4, 8, 10), "input": "."} for n in range(1, 11)]
        self.assertEqual(movie_export.lag_runs(frames), [(2, 3), (8, 1), (10, 1)])

    def test_summary(self):
        f = write_export(self.folder("a"), lag_frames=(2, 3), inputs={5: "Start"},
                         snapshots={(5, "WRAM"): b"\0" * 4}, writes=[(6, 0xC000, 0, 1, "0x0150")])
        code, text = run(["summary", f])
        self.assertEqual(code, 0)
        self.assertIn("lag frames 2 (20.00%)", text)
        self.assertIn("2 at 2", text)
        self.assertIn("watched writes 1", text)

    def test_summary_warns_when_not_from_frame_zero(self):
        f = write_export(self.folder("a"), meta={"started_at_frame": "120"})
        _, text = run(["summary", f])
        self.assertIn("started at frame 120", text)

    def test_summary_names_checkpoints(self):
        f = write_export(self.folder("a"), meta={"started_at_frame": "195000", "start_state": "C:/x/state_f0195000.State",
                                                 "states_saved": "state_f0212000.State"})
        _, text = run(["summary", f])
        self.assertIn("started from checkpoint C:/x/state_f0195000.State at frame 195000", text)
        self.assertIn("checkpoints saved: 1 (state_f0212000.State)", text)
        many = write_export(self.folder("b"), meta={"started_at_frame": "0", "states_saved": ", ".join(
            f"state_f{n:07d}.State" for n in range(1000, 9000, 1000))})
        _, text = run(["summary", many])
        self.assertIn("checkpoints saved: 8 (state_f0001000.State ... state_f0008000.State)", text)
        self.assertNotIn("WARNING", text)

    def test_bit_changes(self):
        self.assertEqual(movie_export.bit_changes(0x04, 0x0C), [(3, "set")])
        self.assertEqual(movie_export.bit_changes(0x81, 0x02), [(0, "cleared"), (1, "set"), (7, "cleared")])
        self.assertEqual(movie_export.bits_text(0x81, 0x02), "-0 +1 -7")
        self.assertEqual(movie_export.bit_changes(5, 5), [])

    def test_writes_with_bits(self):
        f = write_export(self.folder("a"), writes=[(1, 0xD000, 0, 0, "init"), (5, 0xD000, 0, 8, "0x4000")])
        _, text = run(["writes", f, "--bits"])
        self.assertIn("0 -> 8    pc 0x4000  bits +3", text)

    def test_flags_once_finds_pickups(self):
        ws = [(0, 0xD000, 0x01, 0x01, "init"), (0, 0xD001, 0, 0, "init"),
              (10, 0xD000, 0x01, 0x05, "0x4000"),   # bit 2 set once: a pickup
              (20, 0xD001, 0x00, 0x80, "0x4100"),   # bit 7 set ...
              (30, 0xD001, 0x80, 0x00, "0x4100"),   # ... and cleared: a toggle, not a pickup
              (40, 0xD000, 0x05, 0x04, "0x4200")]   # bit 0 was set at start, cleared now
        f = write_export(self.folder("a"), frames=50, writes=ws)
        rows = movie_export.flag_bits(movie_export.load(f)["writes"])
        self.assertEqual([(r["address"], r["bit"]) for r in rows], [("0xD000", 2), ("0xD001", 7), ("0xD000", 0)])
        _, text = run(["flags", f, "--once"])
        self.assertIn("1 bits set once and never cleared", text)
        self.assertIn("0xD000 bit 2  first set      10  sets   1  clears   0  0->1", text)
        self.assertNotIn("bit 7", text)
        _, ranged = run(["flags", f, "--range", "0xD001", "1"])
        self.assertIn("0xD001 bit 7", ranged)
        self.assertNotIn("0xD000", ranged)

    def test_writes_filter_and_limit(self):
        ws = [(n, 0xC000 + (n % 2), n, n + 1, "0x0150") for n in range(1, 9)]
        f = write_export(self.folder("a"), writes=ws)
        _, text = run(["writes", f, "--address", "0xC001", "--limit", "2"])
        self.assertIn("0xC001", text)
        self.assertNotIn("0xC000", text)
        self.assertIn("2 more writes", text)

    def test_frames_range_and_json(self):
        f = write_export(self.folder("a"), inputs={3: "A|Start"})
        out = os.path.join(self.tmp.name, "rows.json")
        _, text = run(["frames", f, "--from", "3", "--to", "4", "--json", out])
        self.assertIn("A|Start", text)
        with open(out, encoding="utf-8") as fh:
            self.assertEqual([r["frame"] for r in json.load(fh)], [3, 4])

    def test_compare_identical(self):
        snaps = {(5, "WRAM"): b"\1\2\3"}
        a = write_export(self.folder("a"), lag_frames=(2,), snapshots=snaps)
        b = write_export(self.folder("b"), lag_frames=(2,), snapshots=snaps)
        code, text = run(["compare", a, b])
        self.assertEqual(code, 0, text)
        self.assertIn("first lag difference:   none", text)
        self.assertIn("WRAM: identical in 1 snapshots", text)

    def test_compare_finds_first_divergence(self):
        a = write_export(self.folder("a"), lag_frames=(2, 7), snapshots={(4, "WRAM"): bytes([1, 2, 3]), (8, "WRAM"): b"\0"})
        b = write_export(self.folder("b"), lag_frames=(2, 6), snapshots={(4, "WRAM"): bytes([1, 9, 3]), (8, "WRAM"): b"\0"})
        code, text = run(["compare", a, b])
        self.assertEqual(code, 1)
        self.assertIn("first lag difference:   6", text)
        self.assertIn("WRAM: first differs at frame 4, 1 bytes, e.g. 0x0001 (2 vs 9)", text)

    def test_compare_flags_different_movies(self):
        a = write_export(self.folder("a"), inputs={3: "A"})
        b = write_export(self.folder("b"), inputs={3: "B"})
        _, text = run(["compare", a, b])
        self.assertIn("first input difference: 3", text)
        self.assertIn("different movies or offsets", text)

    def test_timeline_two_byte_room_variable(self):
        # Room word at 0xC9F9 (little-endian): starts at 0x0003, becomes 0x0107 at frame 4
        # (low byte written first, so frame 4 briefly shows 0x0007), then 0x0003 again at 8.
        ws = [(0, 0xC9F9, 3, 3, "init"), (0, 0xC9FA, 0, 0, "init"),
              (4, 0xC9F9, 3, 7, "0x4A21"), (5, 0xC9FA, 0, 1, "0x4A23"),
              (8, 0xC9F9, 7, 3, "0x4A21"), (8, 0xC9FA, 1, 0, "0x4A23")]
        f = write_export(self.folder("a"), frames=12, writes=ws,
                         snapshots={(6, "WRAM"): (b"\0", "change:0xC9FA"), (5, "WRAM"): (b"\0", "every")})
        segs = movie_export.timeline(movie_export.load(f), 0xC9F9, width=2)
        self.assertEqual([(s["value"], s["start"], s["end"]) for s in segs],
                         [(0x0003, 0, 3), (0x0007, 4, 4), (0x0107, 5, 7), (0x0003, 8, 12)])
        self.assertEqual(segs[2]["snapshot"], 6, "a change-triggered snapshot is preferred")
        merged = movie_export.timeline(movie_export.load(f), 0xC9F9, width=2, min_frames=2)
        # The dropped half-written frame 4 stays with the old value: the room
        # only counts as changed once its whole value is written.
        self.assertEqual([(s["value"], s["start"], s["end"]) for s in merged],
                         [(0x0003, 0, 4), (0x0107, 5, 7), (0x0003, 8, 12)])

    def test_timeline_cli_unique_and_errors(self):
        ws = [(0, 0xC0A0, 1, 1, "init"), (3, 0xC0A0, 1, 2, "0x0150"), (6, 0xC0A0, 2, 1, "0x0150")]
        f = write_export(self.folder("a"), frames=9, writes=ws)
        _, text = run(["timeline", f, "--address", "0xC0A0", "--unique"])
        self.assertIn("2 distinct values", text)
        self.assertIn("visits   2", text)
        with self.assertRaises(SystemExit):
            run(["timeline", f, "--address", "0xD000"])

    def test_old_exports_without_reason_column_still_load(self):
        f = self.folder("old")
        write_export(f)
        with open(os.path.join(f, "snapshots.tsv"), "w", encoding="utf-8") as fh:
            fh.write("frame\tdomain\tfile\tbytes\n5\tWRAM\tsnap.bin\t1\n")
        self.assertEqual(movie_export.load(f)["snapshots"][0]["reason"], "?")

    def test_parse_registers(self):
        self.assertEqual(movie_export.parse_registers("A=0x12 PC=0x5FF6 bad SP=zz"), {"A": 0x12, "PC": 0x5FF6})
        self.assertEqual(movie_export.parse_registers("?"), {})

    def test_exec_field_pairs_stack_and_capture(self):
        hit = {"registers": {"H": 0x62, "L": 0x42, "PC": 0x6BF6}, "stack": "EF5F0343", "capture": ["0C", "FF00"]}
        self.assertEqual(movie_export.exec_field(hit, "HL"), 0x6242)
        self.assertEqual(movie_export.exec_field(hit, "PC"), 0x6BF6)
        self.assertEqual(movie_export.exec_field(hit, "stack0"), 0x5FEF)
        self.assertEqual(movie_export.exec_field(hit, "stack1"), 0x4303)
        self.assertEqual(movie_export.exec_field(hit, "stack0", big_endian=True), 0xEF5F)
        self.assertIsNone(movie_export.exec_field(hit, "stack2"))
        self.assertEqual(movie_export.exec_field(hit, "cap1"), "FF00")
        self.assertIsNone(movie_export.exec_field(hit, "DE"))

    def test_execs_grouped_by_source_pointer(self):
        ex = [(3, 0x6BF6, 1, "H=0x62 L=0x42 PC=0x6BF6", "EF5F", "0C"),
              (5, 0x6BF6, 2, "H=0x56 L=0x32 PC=0x6BF6", "EF5F", "14"),
              (8, 0x6BF6, 3, "H=0x62 L=0x42 PC=0x6BF6", "EF5F", "0C"),
              (9, 0x0150, 1, "H=0x00 L=0x00 PC=0x0150", "", "")]
        f = write_export(self.folder("a"), execs=ex,
                         meta={"started_at_frame": "0", "exec.0x6BF6.hits": "3", "exec.0x6BF6.logged": "3",
                               "exec.0x6BF6.errors": "0"})
        _, text = run(["execs", f, "--address", "0x6BF6", "--group", "HL,cap0,stack0"])
        self.assertIn("3 hits, 2 distinct (HL, cap0, stack0)", text)
        self.assertIn("HL=0x6242  cap0=0C  stack0=0x5FEF  x2  frames 3-8", text)
        _, raw = run(["execs", f, "--limit", "1"])
        self.assertIn("#1", raw)
        self.assertIn("3 more", raw)
        _, summary = run(["summary", f])
        self.assertIn("exec hits logged 4: 0x6BF6 3 hits (3 logged)", summary)

    def test_write_hits_find_the_copy_source(self):
        # A copy loop writing a 2-byte record: HL walks the source, DE the destination.
        wh = [(7, 0xCC30, 1, "D=0xCC E=0x30 H=0x62 L=0x42 PC=0x6BF6", "", ""),
              (7, 0xCC31, 2, "D=0xCC E=0x31 H=0x62 L=0x43 PC=0x6BF6", "", ""),
              (9, 0xCC30, 3, "D=0xCC E=0x30 H=0x56 L=0x32 PC=0x6BF6", "", "")]
        f = write_export(self.folder("a"), write_hits=wh,
                         meta={"started_at_frame": "0", "write_hits.0xCC30.hits": "3",
                               "write_hits.0xCC30.logged": "3", "write_hits.0xCC30.errors": "0"})
        _, text = run(["execs", f, "--writes", "--address", "0xCC30", "--group", "HL,DE"])
        self.assertIn("2 hits, 2 distinct (HL, DE)", text)
        self.assertIn("HL=0x6242  DE=0xCC30  x1  frames 7-7", text)
        _, summary = run(["summary", f])
        self.assertIn("write hits logged 3: 0xCC30 3 hits (3 logged)", summary)
        _, none = run(["execs", f])
        self.assertIn("no execs.tsv", none)

    def test_shots_brightness_finds_a_fade(self):
        f = write_export(self.folder("a"))
        grey = lambda v: [(v, v, v, 255)] * 16  # noqa: E731
        for frame, level, reason in ((100, 200, "range"), (110, 10, "range"), (120, 190, "range"), (130, 205, "every")):
            movie_export.png.write_png(os.path.join(f, f"shot_f{frame:07d}_{reason}.png"), 4, 4, grey(level))
        movie_export.png.write_png(os.path.join(f, "shot_f0000140.png"), 4, 4, grey(195))  # pre-v8 name
        shots = movie_export.list_shots(f)
        self.assertEqual([s["frame"] for s in shots], [100, 110, 120, 130, 140])
        self.assertEqual(shots[-1]["reason"], "?")
        self.assertAlmostEqual(movie_export.brightness(os.path.join(f, shots[1]["file"])), 10, places=3)
        _, text = run(["shots", f, "--dark", "0.5"])
        self.assertIn("5 screenshots, median brightness 195.0; 1 at or below 0.5 x median", text)
        self.assertIn("f    110    10.0  range", text)
        self.assertNotIn("f    100", text)

    def test_execs_without_execs_file(self):
        f = write_export(self.folder("a"))
        _, text = run(["execs", f])
        self.assertIn("no execs.tsv", text)
        self.assertEqual(movie_export.load(f)["execs"], [])

    def test_not_an_export(self):
        with self.assertRaises(SystemExit):
            movie_export.load(self.folder("empty"))


if __name__ == "__main__":
    unittest.main()
