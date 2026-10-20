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


def write_export(folder, lag_frames=(), inputs=None, snapshots=None, writes=(), frames=10, meta=None):
    """Create a fake movie_recorder export folder.

    lag_frames: frames flagged as lag. inputs: {frame: "A|Start"}.
    snapshots: {(frame, domain): bytes}. writes: (frame, addr, old, new, pc) tuples.
    Returns: the folder path.
    """
    inputs = inputs or {}
    with open(os.path.join(folder, "frames.tsv"), "w", encoding="utf-8") as f:
        f.write("frame\tlag\tinput\n")
        for n in range(1, frames + 1):
            f.write(f"{n}\t{1 if n in lag_frames else 0}\t{inputs.get(n, '.')}\n")
    with open(os.path.join(folder, "snapshots.tsv"), "w", encoding="utf-8") as f:
        f.write("frame\tdomain\tfile\tbytes\n")
        for (frame, domain), data in (snapshots or {}).items():
            name = f"snap_f{frame:07d}_{domain}.bin"
            with open(os.path.join(folder, name), "wb") as b:
                b.write(data)
            f.write(f"{frame}\t{domain}\t{name}\t{len(data)}\n")
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

    def test_not_an_export(self):
        with self.assertRaises(SystemExit):
            movie_export.load(self.folder("empty"))


if __name__ == "__main__":
    unittest.main()
