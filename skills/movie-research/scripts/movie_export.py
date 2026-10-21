#!/usr/bin/env python3
"""movie_export: read the folders written by lua/legacy/movie_recorder.lua.

Standard library only. Subcommands print short summaries; full results can
go to a JSON file with --json, so big data never has to be pasted anywhere.

  summary  <export>                  totals, lag runs, snapshots, write count
  frames   <export> --from N --to M  the per-frame rows (frame, lag, input)
  writes   <export> [--address A]    watched writes (frame, address, old, new, pc)
  timeline <export> --address A      segments by a watched variable, e.g. the current
                                     room, with a snapshot frame for each
  compare  <a> <b>                   first frame where two recordings of the same
                                     movie differ: input, lag, or memory snapshot

Frame numbers are BizHawk's frame counter after the frame ran (the number the
on-screen counter shows), so the input on row N is the movie's input log line N-1.

Run `python movie_export.py <subcommand> -h` for options.
"""

import argparse
import csv
import hashlib
import json
import os
import sys


# ── Reading an export folder ──────────────────────────────────────────────

def read_tsv(path):
    """Read a tab-separated file with a header row.

    Returns: list of dicts (empty if the file is missing).
    Requires: read access.
    """
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def load(folder):
    """Load every table of one export.

    Returns: dict with "meta" (key -> value), "frames", "snapshots", "writes".
    Raises SystemExit when the folder has no frames.tsv.
    Requires: read access to the folder.
    """
    if not os.path.exists(os.path.join(folder, "frames.tsv")):
        sys.exit(f"{folder}: no frames.tsv (is this a movie_recorder export folder?)")
    meta = {r["key"]: r["value"] for r in read_tsv(os.path.join(folder, "run.tsv"))}
    frames = [{"frame": int(r["frame"]), "lag": r["lag"] == "1", "input": r["input"]}
              for r in read_tsv(os.path.join(folder, "frames.tsv"))]
    # "reason" exists from recorder version 2 on (listed / every / change:0xADDR).
    snaps = [{"frame": int(r["frame"]), "domain": r["domain"], "file": r["file"], "bytes": int(r["bytes"]),
              "reason": r.get("reason") or "?"}
             for r in read_tsv(os.path.join(folder, "snapshots.tsv"))]
    writes = [{"frame": int(r["frame"]), "address": r["address"], "old": int(r["old"]), "new": int(r["new"]),
               "pc": r["pc"]} for r in read_tsv(os.path.join(folder, "writes.tsv"))]
    return {"folder": folder, "meta": meta, "frames": frames, "snapshots": snaps, "writes": writes}


def lag_runs(frames, top=5):
    """Find the longest runs of consecutive lag frames.

    Returns: list of (first_frame, length), longest first, at most `top`.
    Requires: nothing.
    """
    runs, start, length = [], None, 0
    for r in frames:
        if r["lag"]:
            if start is None:
                start, length = r["frame"], 0
            length += 1
        elif start is not None:
            runs.append((start, length))
            start = None
    if start is not None:
        runs.append((start, length))
    return sorted(runs, key=lambda x: (-x[1], x[0]))[:top]


def timeline(export, address, width=1, big_endian=False, min_frames=1):
    """Split the movie into segments by the value of a watched variable.

    Built from writes.tsv: "init" rows give the starting value, later rows
    the changes. A multi-byte value is read little-endian unless big_endian.
    Segments shorter than min_frames (e.g. a value passing through a
    half-written state between its two bytes) are dropped; their frames go
    to the preceding segment (the value only counts as changed once fully
    written), and neighbours with equal values are merged.
    Returns: list of {"value", "start", "end", "frames", "snapshot"}, where
    snapshot is the best snapshot frame inside the segment (one taken because
    the value changed, else the first) or None.
    Raises ValueError when the address was never watched.
    Requires: an export loaded with load().
    """
    wanted = {address + i for i in range(width)}
    rows = [w for w in export["writes"] if int(w["address"], 16) in wanted]
    if not rows:
        raise ValueError(f"0x{address:04X} isn't in writes.tsv: add 'watch' or 'snapshot_on_change' for it")
    last_frame = export["frames"][-1]["frame"] if export["frames"] else max(w["frame"] for w in rows)
    byte = {}
    for w in rows:  # starting values: init rows, else the first write's old value
        a = int(w["address"], 16)
        if a not in byte:
            byte[a] = w["new"] if w["pc"] == "init" else w["old"]

    def value():
        order = range(width) if big_endian else range(width - 1, -1, -1)
        v = 0
        for i in order:
            v = (v << 8) | byte.get(address + i, 0)
        return v

    by_frame = {}
    for w in rows:
        if w["pc"] != "init":
            by_frame.setdefault(w["frame"], []).append(w)
    first = min(w["frame"] for w in rows)
    segments, current, start = [], value(), first
    for frame in sorted(by_frame):
        for w in by_frame[frame]:
            byte[int(w["address"], 16)] = w["new"]
        v = value()
        if v != current:
            segments.append({"value": current, "start": start, "end": frame - 1})
            current, start = v, frame
    segments.append({"value": current, "start": start, "end": last_frame})

    merged = []
    for s in segments:
        if s["end"] - s["start"] + 1 < min_frames and len(segments) > 1:
            continue
        if merged and merged[-1]["value"] == s["value"]:
            merged[-1]["end"] = s["end"]
        else:
            merged.append(dict(s))
    for i in range(len(merged) - 1):  # close gaps left by dropped segments
        merged[i]["end"] = merged[i + 1]["start"] - 1
    shots = sorted({(s["frame"], s["reason"]) for s in export["snapshots"]})
    for s in merged:
        s["frames"] = s["end"] - s["start"] + 1
        inside = [(f, r) for f, r in shots if s["start"] <= f <= s["end"]]
        changed = [f for f, r in inside if r.startswith("change:")]
        s["snapshot"] = changed[0] if changed else (inside[0][0] if inside else None)
    return merged


def snapshot_hash(folder, snap):
    """SHA-1 of one snapshot file. Returns: hex string, or None if missing."""
    path = os.path.join(folder, snap["file"])
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()


def first_byte_diff(path_a, path_b, limit=8):
    """Offsets where two snapshot files differ.

    Returns: (total differing bytes, list of up to `limit` (offset, a, b)).
    Requires: read access to both files.
    """
    with open(path_a, "rb") as f:
        a = f.read()
    with open(path_b, "rb") as f:
        b = f.read()
    diffs = [(i, a[i], b[i]) for i in range(min(len(a), len(b))) if a[i] != b[i]]
    return len(diffs) + abs(len(a) - len(b)), diffs[:limit]


def compare(ea, eb):
    """Compare two recordings of the same movie.

    Inputs differing means they aren't the same movie (or one is from a
    different frame offset); lag differing means the emulators diverged in
    timing; snapshots differing means memory diverged.
    Returns: dict with first_input_diff, first_lag_diff, lag counts, and
    snapshot results (first differing frame per domain, with byte offsets).
    Requires: both exports loaded with load().
    """
    fa = {r["frame"]: r for r in ea["frames"]}
    fb = {r["frame"]: r for r in eb["frames"]}
    common = sorted(set(fa) & set(fb))
    first_input = next((f for f in common if fa[f]["input"] != fb[f]["input"]), None)
    first_lag = next((f for f in common if fa[f]["lag"] != fb[f]["lag"]), None)
    lag_a = sum(1 for f in common if fa[f]["lag"])
    lag_b = sum(1 for f in common if fb[f]["lag"])

    sa = {(s["frame"], s["domain"]): s for s in ea["snapshots"]}
    sb = {(s["frame"], s["domain"]): s for s in eb["snapshots"]}
    domains = {}
    for key in sorted(set(sa) & set(sb)):
        frame, domain = key
        d = domains.setdefault(domain, {"compared": 0, "first_diff_frame": None})
        d["compared"] += 1
        if d["first_diff_frame"] is None and snapshot_hash(ea["folder"], sa[key]) != snapshot_hash(eb["folder"], sb[key]):
            total, offsets = first_byte_diff(os.path.join(ea["folder"], sa[key]["file"]),
                                             os.path.join(eb["folder"], sb[key]["file"]))
            d.update(first_diff_frame=frame, differing_bytes=total,
                     first_offsets=[{"offset": f"0x{o:04X}", "a": x, "b": y} for o, x, y in offsets])
    return {"common_frames": len(common), "first_input_diff": first_input, "first_lag_diff": first_lag,
            "lag_frames_a": lag_a, "lag_frames_b": lag_b, "snapshots": domains}


# ── Commands ──────────────────────────────────────────────────────────────

def _emit(args, data, lines):
    """Print summary lines, and write `data` to --json if given. Returns: None."""
    for line in lines:
        print(line)
    if getattr(args, "json", None):
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        print(f"wrote {args.json}")


def cmd_summary(args):
    """Totals for one export. Returns: None."""
    e = load(args.export)
    fr = e["frames"]
    lag = sum(1 for r in fr if r["lag"])
    runs = lag_runs(fr)
    first, last = (fr[0]["frame"], fr[-1]["frame"]) if fr else (None, None)
    data = {"meta": e["meta"], "frames": len(fr), "first_frame": first, "last_frame": last, "lag_frames": lag,
            "longest_lag_runs": [{"frame": f, "length": n} for f, n in runs],
            "snapshots": len(e["snapshots"]), "snapshot_frames": len({s["frame"] for s in e["snapshots"]}),
            "writes": len(e["writes"])}
    m = e["meta"]
    lines = [f"{args.export}",
             f"  ROM {m.get('rom_name', '?')}  hash {m.get('rom_hash', '?')}",
             f"  frames {first}..{last} ({len(fr)} rows; movie length {m.get('movie_length', '?')}); "
             f"end: {m.get('end_reason', '?')}",
             f"  lag frames {lag} ({100.0 * lag / max(1, len(fr)):.2f}%); longest runs: "
             + (", ".join(f"{n} at {f}" for f, n in runs) or "none"),
             f"  snapshots {len(e['snapshots'])} files at {data['snapshot_frames']} frames; watched writes {len(e['writes'])}"]
    if m.get("started_at_frame") not in (None, "0"):
        lines.append(f"  WARNING: recording started at frame {m['started_at_frame']}, not 0")
    _emit(args, data, lines)


def cmd_frames(args):
    """Rows of frames.tsv in a range. Returns: None."""
    e = load(args.export)
    rows = [r for r in e["frames"] if args.start <= r["frame"] <= args.end]
    shown = rows[:args.limit]
    lines = [f"{r['frame']:>8}  {'LAG' if r['lag'] else '   '}  {r['input']}" for r in shown]
    if len(rows) > len(shown):
        lines.append(f"... {len(rows) - len(shown)} more rows (raise --limit or use --json)")
    _emit(args, rows, lines)


def cmd_writes(args):
    """Watched writes, optionally for one address or frame range. Returns: None."""
    e = load(args.export)
    want = None if args.address is None else int(args.address, 0)
    rows = [w for w in e["writes"] if (want is None or int(w["address"], 16) == want)
            and args.start <= w["frame"] <= args.end]
    shown = rows[:args.limit]
    lines = [f"{w['frame']:>8}  {w['address']}  {w['old']:>3} -> {w['new']:<3}  pc {w['pc']}" for w in shown]
    if len(rows) > len(shown):
        lines.append(f"... {len(rows) - len(shown)} more writes (raise --limit or use --json)")
    if not rows:
        lines.append("no matching writes")
    _emit(args, rows, lines)


def cmd_timeline(args):
    """Segments of the movie by a watched variable's value. Returns: None."""
    e = load(args.export)
    try:
        segs = timeline(e, int(args.address, 0), args.width, args.big_endian, args.min_frames)
    except ValueError as err:
        sys.exit(str(err))
    digits = 2 * args.width
    if args.unique:
        by_value = {}
        for s in segs:
            u = by_value.setdefault(s["value"], {"value": s["value"], "first_frame": s["start"], "visits": 0,
                                                 "frames": 0, "snapshot": s["snapshot"]})
            u["visits"] += 1
            u["frames"] += s["frames"]
            if u["snapshot"] is None:
                u["snapshot"] = s["snapshot"]
        rows = sorted(by_value.values(), key=lambda u: u["first_frame"])
        shown = rows[:args.limit]
        lines = [f"{len(rows)} distinct values of {args.address} in {len(segs)} segments (first visit order)"]
        lines += [f"  0x{u['value']:0{digits}X}  first {u['first_frame']:>7}  visits {u['visits']:>3}  "
                  f"frames {u['frames']:>7}  shot {u['snapshot'] if u['snapshot'] is not None else '-'}"
                  for u in shown]
        data = rows
    else:
        shown = segs[:args.limit]
        lines = [f"{len(segs)} segments, {len({s['value'] for s in segs})} distinct values of {args.address}"]
        lines += [f"  0x{s['value']:0{digits}X}  frames {s['start']:>7}-{s['end']:<7} ({s['frames']:>6})  "
                  f"shot {s['snapshot'] if s['snapshot'] is not None else '-'}" for s in shown]
        data, rows = segs, segs
    if len(rows) > len(shown):
        lines.append(f"  ... {len(rows) - len(shown)} more (raise --limit or use --json)")
    _emit(args, data, lines)


def cmd_compare(args):
    """Compare two exports of the same movie. Returns: exit code 0 if identical, 1 otherwise."""
    result = compare(load(args.a), load(args.b))
    lines = [f"compared {result['common_frames']} common frames",
             f"  first input difference: {result['first_input_diff'] or 'none'}"
             + ("  (different movies or offsets: fix that first)" if result["first_input_diff"] else ""),
             f"  first lag difference:   {result['first_lag_diff'] or 'none'}"
             f"  (lag frames: a {result['lag_frames_a']}, b {result['lag_frames_b']})"]
    for domain, d in result["snapshots"].items():
        if d["first_diff_frame"] is None:
            lines.append(f"  {domain}: identical in {d['compared']} snapshots")
        else:
            offs = ", ".join(f"{o['offset']} ({o['a']} vs {o['b']})" for o in d["first_offsets"][:4])
            lines.append(f"  {domain}: first differs at frame {d['first_diff_frame']}, "
                         f"{d['differing_bytes']} bytes, e.g. {offs}")
    _emit(args, result, lines)
    same = (result["first_input_diff"] is None and result["first_lag_diff"] is None
            and all(d["first_diff_frame"] is None for d in result["snapshots"].values()))
    return 0 if same else 1


def main(argv=None):
    """Parse arguments and run a subcommand. Returns: process exit code."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--json", help="also write the full result to this JSON file")

    s = sub.add_parser("summary", help="totals for one export")
    s.add_argument("export")
    common(s)
    s.set_defaults(fn=cmd_summary)

    f = sub.add_parser("frames", help="per-frame rows in a range")
    f.add_argument("export")
    f.add_argument("--from", dest="start", type=int, default=0)
    f.add_argument("--to", dest="end", type=int, default=10 ** 9)
    f.add_argument("--limit", type=int, default=40)
    common(f)
    f.set_defaults(fn=cmd_frames)

    w = sub.add_parser("writes", help="watched writes")
    w.add_argument("export")
    w.add_argument("--address", help="one System Bus address, e.g. 0xC0A0")
    w.add_argument("--from", dest="start", type=int, default=0)
    w.add_argument("--to", dest="end", type=int, default=10 ** 9)
    w.add_argument("--limit", type=int, default=40)
    common(w)
    w.set_defaults(fn=cmd_writes)

    t = sub.add_parser("timeline", help="segments of the movie by a watched variable (e.g. the current room)")
    t.add_argument("export")
    t.add_argument("--address", required=True, help="first byte of the variable, e.g. 0xC9F9")
    t.add_argument("--width", type=int, default=1, help="bytes in the variable (default 1)")
    t.add_argument("--big-endian", action="store_true", help="high byte first (default: little-endian)")
    t.add_argument("--min-frames", type=int, default=1, help="drop segments shorter than this (half-written values)")
    t.add_argument("--unique", action="store_true", help="one row per distinct value instead of per segment")
    t.add_argument("--limit", type=int, default=40)
    common(t)
    t.set_defaults(fn=cmd_timeline)

    c = sub.add_parser("compare", help="first divergence between two recordings of one movie")
    c.add_argument("a")
    c.add_argument("b")
    common(c)
    c.set_defaults(fn=cmd_compare)

    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
