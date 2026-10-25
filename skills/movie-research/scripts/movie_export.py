#!/usr/bin/env python3
"""movie_export: read the folders written by lua/legacy/movie_recorder.lua.

Standard library only. Subcommands print short summaries; full results can
go to a JSON file with --json, so big data never has to be pasted anywhere.

  summary  <export>                  totals, lag runs, snapshots, write count
  frames   <export> --from N --to M  the per-frame rows (frame, lag, input)
  writes   <export> [--address A]    watched writes (frame, address, old, new, pc);
                                     --bits shows which bits flipped
  flags    <export> [--once]         per-bit history of watched bytes; --once lists
                                     bits set once and never cleared (pickups, flags);
                                     --snapshots WRAM reads periodic snapshots instead,
                                     for flags whose address is still unknown
  locate   <export> --frame F --domain CartRAM --inside WRAM
                                     where one domain's bytes sit in another (e.g. the
                                     live copy of the save file)
  timeline <export> --address A      segments by a watched variable, e.g. the current
                                     room, with a snapshot frame for each
  execs    <export> [--group HL,DE]  exec-watch hits: registers each time a watched
                                     address ran, raw or grouped by distinct values;
                                     --writes: registers at each write to a watch
                                     line with 'registers'
  events   <export> --source room 0xC3EF 3 [--source ...] [--fades 0.5]
                                     transitions: changes of named variables grouped,
                                     with writer PCs, lag burst, screenshots before and
                                     after, and a guessed method (load, fade, instant)
  catalog  <export> --source ...     rooms.json, doors.json, items.json from the
                                     labeling station's labels.json
  shots    <export> [--dark 0.5]     screenshots with mean brightness; --dark lists
                                     dips (fades, black transition screens)
  compare  <a> <b>                   first frame where two recordings of the same
                                     movie differ: input, lag, or memory snapshot

Frame numbers are BizHawk's frame counter after the frame ran (the number the
on-screen counter shows), so the input on row N is the movie's input log line N-1.

Run `python movie_export.py <subcommand> -h` for options.
"""

import argparse
import bisect
import csv
import hashlib
import json
import os
import re
import sys

# The PNG reader sits next to this script (a copy of tile-graphics' png.py, so
# this skill works on its own). Put this folder first on the path: `python -I`
# leaves it off, and an installed package named "png" must not win.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import png  # noqa: E402


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
    # execs.tsv (recorder 9+, exec_watch) and write_hits.tsv (recorder 10+,
    # watch lines with registers) share one format: a hit with registers.
    def hits(name):
        return [{"frame": int(r["frame"]), "address": r["address"], "hit": int(r["hit"]),
                 "registers": parse_registers(r["registers"]), "stack": r.get("stack") or "",
                 "capture": (r.get("capture") or "").split()}
                for r in read_tsv(os.path.join(folder, name))]
    return {"folder": folder, "meta": meta, "frames": frames, "snapshots": snaps, "writes": writes,
            "execs": hits("execs.tsv"), "write_hits": hits("write_hits.tsv")}


def parse_registers(text):
    """Parse the recorder's register column ("A=0x12 PC=0x5FF6").

    Returns: dict name -> int; empty for "?" or malformed parts.
    Requires: nothing.
    """
    regs = {}
    for part in (text or "").split():
        name, _, value = part.partition("=")
        try:
            regs[name] = int(value, 0)
        except ValueError:
            pass
    return regs


def exec_field(hit, name, big_endian=False):
    """Read one named field of an exec hit, for grouping.

    Names: a register ("A", "PC"); a pair of one-letter registers read high
    byte first ("HL" = H * 256 + L, as on Z80-family CPUs); "stackN" for the
    Nth 2-byte word at SP (little-endian unless big_endian); "capN" for the
    Nth captured range as hex text.
    Returns: an int or str, or None when the hit doesn't have that field.
    Requires: nothing.
    """
    regs = hit["registers"]
    if name in regs:
        return regs[name]
    if len(name) == 2 and name[0] in regs and name[1] in regs:
        return regs[name[0]] * 256 + regs[name[1]]
    if name.startswith("stack") and name[5:].isdigit():
        i = int(name[5:])
        word = hit["stack"][4 * i:4 * i + 4]
        if len(word) < 4:
            return None
        lo, hi = int(word[0:2], 16), int(word[2:4], 16)
        return lo * 256 + hi if big_endian else hi * 256 + lo
    if name.startswith("cap") and name[3:].isdigit():
        i = int(name[3:])
        return hit["capture"][i] if i < len(hit["capture"]) else None
    return None


def group_execs(execs, fields, big_endian=False):
    """Group exec hits by the values of some fields.

    Returns: list of {"values": {field: value}, "count", "first_frame",
    "last_frame"}, in order of first appearance.
    Requires: nothing.
    """
    groups = {}
    for h in execs:
        key = tuple(exec_field(h, f, big_endian) for f in fields)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"values": dict(zip(fields, key)), "count": 0,
                               "first_frame": h["frame"], "last_frame": h["frame"]}
        g["count"] += 1
        g["last_frame"] = h["frame"]
    return sorted(groups.values(), key=lambda g: g["first_frame"])


def bit_changes(old, new):
    """Which bits flipped between two byte values.

    Returns: list of (bit, "set" or "cleared"), lowest bit first.
    Requires: nothing.
    """
    diff = (old ^ new) & 0xFF
    return [(b, "set" if new >> b & 1 else "cleared") for b in range(8) if diff >> b & 1]


def bits_text(old, new):
    """Short form of bit_changes: "+3 -0" (bit 3 set, bit 0 cleared). Returns: str."""
    return " ".join(("+" if kind == "set" else "-") + str(b) for b, kind in bit_changes(old, new))


def flag_bits(writes, addresses=None):
    """Per-bit history of watched bytes, from writes.tsv rows.

    "init" rows give each byte's starting value. Returns: list of dicts
    {"address", "bit", "initial", "final", "sets", "clears", "first_set",
    "set_frames"}, one per bit that changed, ordered by first change.
    Requires: nothing.
    """
    state, out = {}, {}
    for w in writes:
        a = int(w["address"], 16)
        if addresses is not None and a not in addresses:
            continue
        if w["pc"] == "init":
            state[a] = w["new"]
            continue
        old = state.get(a, w["old"])
        for b, kind in bit_changes(old, w["new"]):
            rec = out.get((a, b))
            if rec is None:
                rec = out[(a, b)] = {"address": f"0x{a:04X}", "bit": b, "initial": old >> b & 1, "sets": 0,
                                     "clears": 0, "first_set": None, "set_frames": [], "first_change": w["frame"]}
            if kind == "set":
                rec["sets"] += 1
                rec["set_frames"].append(w["frame"])
                if rec["first_set"] is None:
                    rec["first_set"] = w["frame"]
            else:
                rec["clears"] += 1
        state[a] = w["new"]
    for (a, b), rec in out.items():
        rec["final"] = state.get(a, 0) >> b & 1
    return sorted(out.values(), key=lambda r: (r["first_change"], int(r["address"], 16), r["bit"]))


def snapshot_bits(folder, snaps, lo=0, length=None):
    """Per-bit history of a memory domain, from its snapshots instead of writes.

    For flags whose location is unknown: snapshot a whole domain periodically,
    then look for bits that change rarely. Each change is only known to have
    happened after the previous snapshot, so every record also gives that
    frame ("first_set_after"). Same record shape as flag_bits, with "address"
    an offset into the domain plus "domain".
    Returns: list of dicts, ordered by first change; empty with < 2 snapshots.
    Requires: the snapshot files of one domain, all the same size.
    """
    snaps = sorted(snaps, key=lambda s: s["frame"])
    out, prev, prev_frame = {}, None, None
    for s in snaps:
        with open(os.path.join(folder, s["file"]), "rb") as f:
            data = f.read()
        cur = data[lo:lo + length] if length is not None else data[lo:]
        if prev is not None and cur != prev and len(cur) == len(prev):
            # XOR as big integers, then find the differing bytes with a regex:
            # both run in C, so a 32 KB domain over thousands of snapshots stays fast.
            x = (int.from_bytes(prev, "big") ^ int.from_bytes(cur, "big")).to_bytes(len(cur), "big")
            for m in re.finditer(rb"[^\x00]", x):
                i = m.start()
                old, new = prev[i], cur[i]
                for b, kind in bit_changes(old, new):
                    rec = out.get((i, b))
                    if rec is None:
                        rec = out[(i, b)] = {"domain": s["domain"], "address": f"0x{lo + i:04X}", "bit": b,
                                             "initial": old >> b & 1, "sets": 0, "clears": 0, "first_set": None,
                                             "first_set_after": None, "set_frames": [], "first_change": s["frame"]}
                    if kind == "set":
                        rec["sets"] += 1
                        rec["set_frames"].append(s["frame"])
                        if rec["first_set"] is None:
                            rec["first_set"], rec["first_set_after"] = s["frame"], prev_frame
                    else:
                        rec["clears"] += 1
        prev, prev_frame = cur, s["frame"]
    for (i, b), rec in out.items():
        rec["final"] = prev[i] >> b & 1
    return sorted(out.values(), key=lambda r: (r["first_change"], int(r["address"], 16), r["bit"]))


def locate(src, dst, min_run=4, top=5):
    """Where a block of one memory domain appears in another, e.g. the live copy
    of a save file (cart RAM) inside work RAM.

    Trailing padding (a run of one repeated byte value) is dropped from `src`
    first. Every offset of `dst` is scored by how many bytes match.
    Returns: dict with "length" (bytes compared), "exact" (offsets where all
    match) and "best" (up to `top` (offset, matching bytes), best first;
    offsets with fewer than `min_run` matching bytes are left out).
    Requires: nothing.
    """
    end = len(src)
    while end > 1 and src[end - 1] == src[-1]:
        end -= 1
    block = src[:end]
    if not block:
        return {"length": 0, "exact": [], "best": []}
    exact, start = [], dst.find(block)
    while start != -1:
        exact.append(start)
        start = dst.find(block, start + 1)
    n = len(block)
    scores = []
    for o in range(len(dst) - n + 1):
        window = dst[o:o + n]
        score = n if window == block else sum(1 for a, b in zip(window, block) if a == b)
        if score >= min_run:
            scores.append((score, o))
    scores.sort(key=lambda t: (-t[0], t[1]))
    return {"length": n, "exact": exact, "best": [(o, s) for s, o in scores[:top]]}


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


def events(export, sources, cluster=60, lag_min=5, fades=None, only_pcs=None, skip_pcs=None):
    """Transitions in a capture: changes of named watched variables, grouped.

    Each source is (name, address, width), e.g. ("room", 0xC3EF, 3).
    `only_pcs` maps a source name to the writer PCs that count for it (e.g. a
    byte that is a room ID when the door code writes it and scratch state
    otherwise); its other changes are dropped, unless they ride along with a
    kept change of another source within `cluster` frames (entering an area
    sets its first room from the area loader, not from the door code).
    `skip_pcs` is the opposite, and usually the sturdier choice when every
    level has its own door code: a source's changes made only by these
    writers (known noise, e.g. a menu routine) are dropped, with the same
    ride-along exception. Changes
    of all sources within `cluster` frames of the previous change join one
    event. Each event gets the nearest lag burst (a run of at least `lag_min`
    lag frames starting up to 10 frames before and 60 after the first change),
    the screenshots just before and just after, and a guessed method: "load"
    (with a lag burst), "fade" (no burst, but a dark screenshot in the event)
    or "instant". `fades` is an optional list of dark screenshot frames
    (from shots --dark); dark frames that no change explains become events of
    their own ("unexplained fade"), the lead to transitions without a known
    variable.
    Returns: list of {"frame", "end", "changes": [{"source", "frame", "old",
    "new", "width", "pcs"}], "lag", "guess", "shot_before", "shot_after"}.
    Raises ValueError when a source was never watched (see timeline).
    Requires: an export loaded with load().
    """
    by_frame = {}
    for w in export["writes"]:
        if w["pc"] != "init":
            by_frame.setdefault(w["frame"], []).append(w)
    changes = []
    for name, address, width in sources:
        segs = timeline(export, address, width, min_frames=2)
        wanted = {address + i for i in range(width)}
        for prev, seg in zip(segs, segs[1:]):
            # A value written in two halves lands over two frames: look at both.
            near = by_frame.get(seg["start"] - 1, []) + by_frame.get(seg["start"], [])
            pcs = sorted({w["pc"] for w in near if int(w["address"], 16) in wanted})
            allowed = (only_pcs or {}).get(name)
            skipped = (skip_pcs or {}).get(name, set())
            kept = (allowed is None or any(int(pc, 16) in allowed for pc in pcs)) and not (
                pcs and all(int(pc, 16) in skipped for pc in pcs))
            changes.append({"source": name, "frame": seg["start"], "old": prev["value"], "new": seg["value"],
                            "width": width, "pcs": pcs, "kept": kept})
    anchors = [c for c in changes if c["kept"]]
    changes = [c for c in changes if c["kept"] or any(
        a["source"] != c["source"] and abs(a["frame"] - c["frame"]) <= cluster for a in anchors)]
    for c in changes:
        del c["kept"]
    changes.sort(key=lambda c: c["frame"])
    fade_frames = sorted(fades or [])
    groups = []
    for c in changes:
        if groups and c["frame"] - groups[-1]["end"] <= cluster:
            groups[-1]["changes"].append(c)
            groups[-1]["end"] = c["frame"]
        else:
            groups.append({"frame": c["frame"], "end": c["frame"], "changes": [c]})
    for f in fade_frames:
        if not any(g["frame"] - cluster <= f <= g["end"] + cluster for g in groups):
            groups.append({"frame": f, "end": f, "changes": []})
    groups.sort(key=lambda g: g["frame"])

    runs = sorted(r for r in lag_runs(export["frames"], top=10 ** 9) if r[1] >= lag_min)
    shots = list_shots(export["folder"])
    shot_frames = [s["frame"] for s in shots]
    for g in groups:
        near = [r for r in runs if g["frame"] - 10 <= r[0] <= g["end"] + 60]
        g["lag"] = max((r[1] for r in near), default=0)
        dark = any(g["frame"] - cluster <= f <= g["end"] + cluster for f in fade_frames)
        if not g["changes"]:
            g["guess"] = "unexplained fade"
        elif g["lag"]:
            g["guess"] = "load"
        else:
            g["guess"] = "fade" if dark else "instant"
        i = bisect.bisect_left(shot_frames, g["frame"])
        g["shot_before"] = shots[i - 1]["file"] if i > 0 else None
        after = [s for s in shots[i:] if s["frame"] <= g["end"] + 200]
        changed = [s for s in after if s["reason"].startswith("change")]
        g["shot_after"] = (changed or after or [None])[0]
        if g["shot_after"]:
            g["shot_after"] = g["shot_after"]["file"]
    return groups


NOT_TRANSITIONS = {"not a transition", "menu", ""}


def region_of(place):
    """The region of a place name: the prefix of a numbered room ("DF9: Lobby"
    -> "DF", "L2: Cave" -> "L"), the part before a colon otherwise, else the
    name itself. Returns: str. Requires: nothing.
    """
    m = re.match(r"\s*([A-Za-z]+)\d+\s*:", place)
    if m:
        return m.group(1)
    return place.split(":")[0].strip() if ":" in place else place


def catalog(rows, labels, capture=""):
    """Rooms, doors and items catalogs from labeled events (labeling station).

    The person's "Arrived in" names are the ground truth for places; the
    watched values seen on arrival are evidence. A value that arrived in more
    than one place is listed as a conflict (it isn't a place ID on its own).
    Doors are directed: from the previous labeled place to this one, merged
    when kind, doors and requirements agree, with every frame they were used.
    Format "draft-1" (the shared findings format is still being agreed).
    Returns: dict {"rooms": {...}, "doors": {...}, "items": {...}}.
    Requires: `rows` from events(); `labels` as saved by label_server.
    """
    by_frame = {r["frame"]: r for r in rows}
    head = {"format": "draft-1", "capture": capture}
    places, value_names, doors, prev = {}, {}, {}, None
    for key in sorted(labels.get("events", {}), key=lambda k: int(k) if k.isdigit() else -1):
        if not key.isdigit():
            continue
        frame, lab = int(key), labels["events"][key]
        kind = (lab.get("kind") or "").strip()
        place = (lab.get("arrival") or "").strip()
        if kind in NOT_TRANSITIONS or not place:
            continue
        ev = by_frame.get(frame, {"changes": [], "lag": 0, "guess": "manual"})
        values = {c["source"]: f"{c['new']:0{2 * c['width']}X}" for c in ev["changes"]}
        p = places.setdefault(place, {"name": place, "region": region_of(place), "visits": [], "values": {}})
        p["visits"].append(frame)
        for source, value in values.items():
            p["values"].setdefault(f"{source}:{value}", []).append(frame)
            value_names.setdefault(f"{source}:{value}", set()).add(place)
        if prev is not None and prev != place:
            fields = ("exit_door", "entry_door", "exit", "entry", "requires", "requires_back", "leave_requires")
            info = {f: (lab.get(f) or "").strip() for f in fields}
            dkey = (prev, place, kind) + tuple(info[f] for f in fields)
            d = doors.setdefault(dkey, dict({"from": prev, "to": place, "kind": kind, "frames": [], "evidence": []},
                                            **info))
            d["frames"].append(frame)
            d["evidence"].append({"frame": frame, "guess": ev["guess"], "lag": ev["lag"],
                                  "changes": [{"source": c["source"], "new": values.get(c["source"]),
                                               "pcs": c["pcs"]} for c in ev["changes"]],
                                  "notes": (lab.get("notes") or "").strip()})
        prev = place
    conflicts = {v: sorted(n) for v, n in value_names.items() if len(n) > 1}
    for p in places.values():
        p["conflicting_values"] = sorted(v for v in p["values"] if v in conflicts)
    return {"rooms": dict(head, places=sorted(places.values(), key=lambda p: p["visits"][0]), conflicts=conflicts),
            "doors": dict(head, doors=sorted(doors.values(), key=lambda d: d["frames"][0])),
            "items": dict(head, items=labels.get("items") or [],
                          note="items are labeled separately; none labeled yet" if not labels.get("items") else "")}


def cmd_catalog(args):
    """Write rooms.json, doors.json and items.json from labels.json. Returns: exit code."""
    e = load(args.export)
    path = os.path.join(args.export, "labels.json")
    if not os.path.exists(path):
        print(f"{path} not found: label the capture first (label_server.py)")
        return 1
    with open(path, encoding="utf-8") as f:
        labels = json.load(f)
    sources = [(name, int(addr, 0), int(width, 0)) for name, addr, width in args.source]
    rows = events(e, sources, only_pcs=parse_only_pcs(args.only_pc), skip_pcs=parse_only_pcs(args.skip_pc))
    out = catalog(rows, labels, capture=os.path.basename(os.path.normpath(args.export)))
    target = args.out or args.export
    os.makedirs(target, exist_ok=True)
    for name, data in out.items():
        with open(os.path.join(target, f"{name}.json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
    print(f"{len(out['rooms']['places'])} places ({len(out['rooms']['conflicts'])} conflicting values), "
          f"{len(out['doors']['doors'])} doors, {len(out['items']['items'])} items -> {target}")
    return 0


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
    for prefix, table, label in (("exec.", "execs", "exec hits"), ("write_hits.", "write_hits", "write hits")):
        keys = sorted(k[len(prefix):-len(".hits")] for k in m if k.startswith(prefix) and k.endswith(".hits"))
        if keys or e[table]:
            data[table] = len(e[table])
            parts = [f"{a} {m.get(f'{prefix}{a}.hits', '?')} hits ({m.get(f'{prefix}{a}.logged', '?')} logged"
                     + (f", {m[f'{prefix}{a}.errors']} errors" if m.get(f"{prefix}{a}.errors", "0") != "0" else "")
                     + ")" for a in keys]
            lines.append(f"  {label} logged {len(e[table])}" + (": " + "; ".join(parts) if parts else ""))
    if m.get("start_state"):
        lines.append(f"  started from checkpoint {m['start_state']} at frame {m.get('started_at_frame', '?')}")
    elif m.get("started_at_frame") not in (None, "0"):
        lines.append(f"  WARNING: recording started at frame {m['started_at_frame']}, not 0")
    if m.get("states_saved"):
        names = [s.strip() for s in m["states_saved"].split(",") if s.strip()]
        shown = ", ".join(names) if len(names) <= 4 else f"{names[0]} ... {names[-1]}"
        lines.append(f"  checkpoints saved: {len(names)} ({shown})")
    _emit(args, data, lines)


def _fmt(value):
    """Show a grouped field value: ints as hex, others as text. Returns: str."""
    if value is None:
        return "-"
    return f"0x{value:04X}" if isinstance(value, int) else str(value)


def cmd_execs(args):
    """Exec-watch hits (or, with --writes, register-logging watch hits), raw or
    grouped by register/stack/capture values. Returns: None."""
    e = load(args.export)
    table, source = ("write_hits", "write_hits.tsv: add 'registers' to a watch line") if args.writes \
        else ("execs", "execs.tsv: set exec_watch")
    want = None if args.address is None else int(args.address, 0)
    hits = [h for h in e[table] if (want is None or int(h["address"], 16) == want)
            and args.start <= h["frame"] <= args.end]
    if not hits:
        _emit(args, [], ["no matching hits" + ("" if e[table] else f" (no {source})")])
        return
    if args.group:
        fields = [f.strip() for f in args.group.split(",") if f.strip()]
        groups = group_execs(hits, fields, args.big_endian)
        shown = groups[:args.limit]
        lines = [f"{len(hits)} hits, {len(groups)} distinct ({', '.join(fields)})"]
        lines += ["  " + "  ".join(f"{k}={_fmt(v)}" for k, v in g["values"].items())
                  + f"  x{g['count']}  frames {g['first_frame']}-{g['last_frame']}" for g in shown]
        data, rows = groups, groups
    else:
        shown = hits[:args.limit]
        lines = [f"{h['frame']:>8}  {h['address']}  #{h['hit']:<5} "
                 + " ".join(f"{k}={_fmt(v)}" for k, v in sorted(h["registers"].items()))
                 + (f"  stack {h['stack']}" if h["stack"] else "") + (f"  cap {' '.join(h['capture'])}" if h["capture"] else "")
                 for h in shown]
        data, rows = hits, hits
    if len(rows) > len(shown):
        lines.append(f"  ... {len(rows) - len(shown)} more (raise --limit or use --json)")
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
    if args.bits:
        for w in rows:
            w["bits"] = bits_text(w["old"], w["new"]) if w["pc"] != "init" else ""
    shown = rows[:args.limit]
    lines = [f"{w['frame']:>8}  {w['address']}  {w['old']:>3} -> {w['new']:<3}  pc {w['pc']}"
             + (f"  bits {w['bits']}" if args.bits and w["bits"] else "") for w in shown]
    if len(rows) > len(shown):
        lines.append(f"... {len(rows) - len(shown)} more writes (raise --limit or use --json)")
    if not rows:
        lines.append("no matching writes")
    _emit(args, rows, lines)


def cmd_flags(args):
    """Per-bit history of watched bytes; --once keeps bits set exactly once and
    never cleared (collected items, opened doors, story flags). Returns: None."""
    e = load(args.export)
    lo, length = 0, None
    if args.address:
        lo, length = int(args.address, 0), 1
    elif args.range:
        lo, length = int(args.range[0], 0), int(args.range[1], 0)
    if args.snapshots:
        # Snapshots: --from/--to pick the snapshots, so a bit's history starts
        # at --from (e.g. after the game has initialised its RAM).
        snaps = [s for s in e["snapshots"] if s["domain"] == args.snapshots and args.start <= s["frame"] <= args.end]
        rows = snapshot_bits(e["folder"], snaps, lo, length)
    else:
        addresses = set(range(lo, lo + length)) if length is not None else None
        rows = flag_bits([w for w in e["writes"] if w["frame"] <= args.end], addresses)
        rows = [r for r in rows if r["first_change"] >= args.start]
    if args.once:
        rows = [r for r in rows if r["sets"] == 1 and r["clears"] == 0 and r["initial"] == 0]
    shown = rows[:args.limit]
    lines = [f"{len(rows)} bits" + (" set once and never cleared" if args.once else " changed")
             + (f" ({args.snapshots} offsets, from {len(snaps)} snapshots)" if args.snapshots else "")]
    for r in shown:
        when = f"{r['first_set'] if r['first_set'] is not None else '-':>7}"
        if args.snapshots:
            span = f"{r['first_set_after']}-{r['first_set']}" if r["first_set"] is not None else "-"
            when = f"{span:>15}"
        lines.append(f"  {r['address']} bit {r['bit']}  first set {when}  "
                     f"sets {r['sets']:>3}  clears {r['clears']:>3}  {r['initial']}->{r['final']}")
    if len(rows) > len(shown):
        lines.append(f"  ... {len(rows) - len(shown)} more (raise --limit or use --json)")
    if args.snapshots and len(snaps) < 2:
        lines.append(f"  (fewer than 2 {args.snapshots} snapshots in range: set snapshot_every in the recorder config)")
    elif not args.snapshots and not e["writes"]:
        lines.append("  (no writes.tsv rows: watch the flag range in the recorder config, or use --snapshots)")
    _emit(args, rows, lines)


def cmd_locate(args):
    """Find where a block of one domain sits in another at the same frame
    (e.g. the live copy of the save file in work RAM). Returns: exit code 0 if
    found, 1 if neither snapshot exists at that frame."""
    e = load(args.export)

    def snap(domain):
        for s in e["snapshots"]:
            if s["domain"] == domain and s["frame"] == args.frame:
                with open(os.path.join(e["folder"], s["file"]), "rb") as f:
                    return f.read()
        return None

    src, dst = snap(args.domain), snap(args.inside)
    if src is None or dst is None:
        print(f"no {args.domain if src is None else args.inside} snapshot at frame {args.frame}")
        return 1
    if args.range:
        lo = int(args.range[0], 0)
        src = src[lo:lo + int(args.range[1], 0)]
    result = locate(src, dst, top=args.top)
    lines = [f"{args.domain} block of {result['length']} bytes (trailing padding dropped) in {args.inside}, "
             f"frame {args.frame}",
             f"  exact copies at: {', '.join(f'0x{o:04X}' for o in result['exact']) or 'none'}"]
    lines += [f"  0x{o:04X}: {s}/{result['length']} bytes match" for o, s in result["best"]]
    _emit(args, result, lines)
    return 0


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


SHOT_RE = re.compile(r"^shot_f(\d+)(?:_(.+))?\.png$")


def list_shots(folder):
    """Find the recorder's screenshots in an export folder.

    Returns: list of {"frame", "reason", "file"} sorted by frame; the reason is
    "?" for screenshots from recorders before version 8 (no reason in the name).
    Requires: read access to the folder.
    """
    shots = []
    for name in os.listdir(folder):
        m = SHOT_RE.match(name)
        if m:
            shots.append({"frame": int(m.group(1)), "reason": m.group(2) or "?", "file": name})
    return sorted(shots, key=lambda s: s["frame"])


def brightness(path):
    """Mean luminance of a screenshot (0 black .. 255 white, Rec. 601 weights).

    Returns: float. Raises ValueError for PNG formats png.read_png can't read.
    Requires: png.read_png (standard library only).
    """
    width, height, pixels = png.read_png(path)
    total = sum(0.299 * r + 0.587 * g + 0.114 * b for r, g, b, _ in pixels)
    return total / max(1, width * height)


BRIGHTNESS_CACHE = "brightness.tsv"


def measure_shots(folder, shots):
    """Add "brightness" (rounded to 0.1) to each screenshot record.

    Decoding PNGs in pure Python is slow (minutes for ten thousand shots), so
    results are kept in brightness.tsv in the export folder and reused while
    a screenshot's size and modification time are unchanged. That file is the
    only thing movie_export writes into a capture.
    Returns: the median brightness, or 0.0 for an empty list.
    Requires: brightness(); the screenshot files in `folder`; write access to
    `folder` for the cache (skipped silently when it can't be written).
    """
    cache_path = os.path.join(folder, BRIGHTNESS_CACHE)
    cache = {}
    if os.path.exists(cache_path):
        for r in read_tsv(cache_path):
            cache[r["file"]] = (r["stamp"], float(r["brightness"]))
    changed = False
    for s in shots:
        st = os.stat(os.path.join(folder, s["file"]))
        stamp = f"{st.st_size}:{int(st.st_mtime)}"
        hit = cache.get(s["file"])
        if hit and hit[0] == stamp:
            s["brightness"] = hit[1]
        else:
            s["brightness"] = round(brightness(os.path.join(folder, s["file"])), 1)
            cache[s["file"]] = (stamp, s["brightness"])
            changed = True
    if changed:
        try:
            with open(cache_path, "w", encoding="utf-8", newline="\n") as f:
                f.write("file\tstamp\tbrightness\n")
                for name, (stamp, value) in sorted(cache.items()):
                    f.write(f"{name}\t{stamp}\t{value}\n")
        except OSError:
            pass
    values = sorted(s["brightness"] for s in shots)
    return values[len(values) // 2] if values else 0.0


def dips(shots, fraction=0.5, window=4):
    """Screenshots much darker than their neighbours: fades, not dark rooms.

    A shot is a dip when its brightness is at most `fraction` times the
    median of the `window` shots on each side of it. A level that is dark
    throughout has no dips; a fade between two rooms does.
    Returns: list of the dipping shots' frames, in order.
    Requires: shots with "brightness" (measure_shots), sorted by frame.
    """
    out = []
    for i, s in enumerate(shots):
        around = [t["brightness"] for t in shots[max(0, i - window):i] + shots[i + 1:i + 1 + window]]
        if not around:
            continue
        around.sort()
        if s["brightness"] <= fraction * around[len(around) // 2]:
            out.append(s["frame"])
    return out


def cmd_shots(args):
    """Screenshots with their mean brightness; --dark lists only dips (fades,
    black transition screens) below a fraction of the median. Returns: None."""
    shots = [s for s in list_shots(args.export) if args.start <= s["frame"] <= args.end]
    if not shots:
        _emit(args, [], ["no screenshots in that range"])
        return
    median = measure_shots(args.export, shots)
    limit = median * args.dark if args.dark is not None else None
    rows = [s for s in shots if limit is None or s["brightness"] <= limit]
    shown = rows[:args.limit]
    head = f"{len(shots)} screenshots, median brightness {median:.1f}"
    if limit is not None:
        head += f"; {len(rows)} at or below {args.dark:g} x median ({limit:.1f})"
    lines = [head] + [f"  f{s['frame']:>7}  {s['brightness']:>6.1f}  {s['reason']}" for s in shown]
    if len(rows) > len(shown):
        lines.append(f"  ... {len(rows) - len(shown)} more (raise --limit or use --json)")
    _emit(args, rows, lines)


def parse_only_pcs(pairs):
    """Turn --only-pc / --skip-pc NAME PCS pairs into {name: {pc, ...}}.

    Returns: dict, or None when no pairs were given. Raises ValueError on a
    PC that isn't a number.
    Requires: nothing.
    """
    if not pairs:
        return None
    out = {}
    for name, pcs in pairs:
        out.setdefault(name, set()).update(int(p, 0) for p in pcs.split(",") if p.strip())
    return out


def cmd_events(args):
    """Transitions grouped from named watched variables, with lag, screenshots
    and a guessed method. Returns: exit code 0, or 1 when a source wasn't watched."""
    e = load(args.export)
    sources = [(name, int(addr, 0), int(width, 0)) for name, addr, width in args.source]
    fades = None
    if args.fades is not None:
        shots = list_shots(args.export)
        measure_shots(args.export, shots)
        fades = dips(shots, args.fades)
    try:
        rows = events(e, sources, cluster=args.cluster, lag_min=args.lag_min, fades=fades,
                      only_pcs=parse_only_pcs(args.only_pc), skip_pcs=parse_only_pcs(args.skip_pc))
    except ValueError as err:
        print(err)
        return 1
    rows = [r for r in rows if args.start <= r["frame"] <= args.end]
    counts = {}
    for r in rows:
        counts[r["guess"]] = counts.get(r["guess"], 0) + 1
    lines = [f"{len(rows)} events: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))]
    for r in rows[:args.limit]:
        what = "; ".join(f"{c['source']} {c['old']:0{2 * c['width']}X}->{c['new']:0{2 * c['width']}X}"
                         + (f" pc {','.join(c['pcs'])}" if c["pcs"] else "") for c in r["changes"]) or "-"
        lines.append(f"  f{r['frame']:>7}  {r['guess']:<16} lag {r['lag']:>3}  {what}")
    if len(rows) > args.limit:
        lines.append(f"  ... {len(rows) - args.limit} more (raise --limit or use --json)")
    _emit(args, rows, lines)
    return 0


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
    w.add_argument("--bits", action="store_true", help="also show which bits flipped (+3 set, -0 cleared)")
    common(w)
    w.set_defaults(fn=cmd_writes)

    fl = sub.add_parser("flags", help="per-bit history of watched bytes (items, keys, door and story flags)")
    fl.add_argument("export")
    fl.add_argument("--address", help="one watched byte, e.g. 0xD000")
    fl.add_argument("--range", nargs=2, metavar=("START", "LENGTH"), help="a watched range, e.g. 0xD000 0x40")
    fl.add_argument("--once", action="store_true",
                    help="only bits that start clear, are set exactly once and never cleared")
    fl.add_argument("--snapshots", metavar="DOMAIN",
                    help="read this domain's snapshots instead of watched writes, e.g. WRAM (addresses are then "
                         "domain offsets): finds flags without knowing where they are")
    fl.add_argument("--from", dest="start", type=int, default=0)
    fl.add_argument("--to", dest="end", type=int, default=10 ** 9)
    fl.add_argument("--limit", type=int, default=60)
    common(fl)
    fl.set_defaults(fn=cmd_flags)

    lo = sub.add_parser("locate", help="where one domain's bytes sit in another (e.g. the save inside work RAM)")
    lo.add_argument("export")
    lo.add_argument("--frame", type=int, required=True, help="a frame with snapshots of both domains")
    lo.add_argument("--domain", required=True, help="the block to look for, e.g. CartRAM")
    lo.add_argument("--inside", required=True, help="the domain to search, e.g. WRAM")
    lo.add_argument("--range", nargs=2, metavar=("START", "LENGTH"), help="only this part of --domain")
    lo.add_argument("--top", type=int, default=5)
    common(lo)
    lo.set_defaults(fn=cmd_locate)

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

    x = sub.add_parser("execs", help="exec-watch hits (registers when an address ran), raw or grouped")
    x.add_argument("export")
    x.add_argument("--writes", action="store_true",
                   help="read write_hits.tsv instead: registers at each write to a 'watch ... registers' range")
    x.add_argument("--address", help="one watched address, e.g. 0x6BF6")
    x.add_argument("--group", help="comma list of fields to group by: registers (A, PC), pairs (HL, DE), "
                                   "stackN (Nth word at SP), capN (Nth captured range)")
    x.add_argument("--big-endian", action="store_true", help="read stack words high byte first")
    x.add_argument("--from", dest="start", type=int, default=0)
    x.add_argument("--to", dest="end", type=int, default=10 ** 9)
    x.add_argument("--limit", type=int, default=40)
    common(x)
    x.set_defaults(fn=cmd_execs)

    ev = sub.add_parser("events", help="transitions (doors, room loads, fades) from named watched variables")
    ev.add_argument("export")
    ev.add_argument("--source", nargs=3, action="append", required=True, metavar=("NAME", "ADDRESS", "WIDTH"),
                    help="a watched variable that changes at transitions, e.g. room 0xC3EF 3 (repeatable)")
    ev.add_argument("--fades", type=float, metavar="FRACTION",
                    help="also use screenshots at or below this fraction of their neighbours' median brightness "
                         "(e.g. 0.5): dips, so a dark level isn't one long fade; dips no change explains are "
                         "listed as unexplained fades")
    ev.add_argument("--only-pc", nargs=2, action="append", metavar=("NAME", "PCS"),
                    help="count a source's changes only when one of these writer PCs made them, e.g. "
                         "room 0x5E3F,0x5CC0 (a byte that is scratch state for other code)")
    ev.add_argument("--skip-pc", nargs=2, action="append", metavar=("NAME", "PCS"),
                    help="drop a source's changes made only by these writer PCs (known noise such as a menu "
                         "routine), e.g. room 0x4E0B; sturdier than --only-pc when each level has its own door code")
    ev.add_argument("--cluster", type=int, default=60, help="changes this many frames apart join one event")
    ev.add_argument("--lag-min", type=int, default=5, help="shortest lag run that counts as a load")
    ev.add_argument("--from", dest="start", type=int, default=0)
    ev.add_argument("--to", dest="end", type=int, default=10 ** 9)
    ev.add_argument("--limit", type=int, default=40)
    common(ev)
    ev.set_defaults(fn=cmd_events)

    ca = sub.add_parser("catalog", help="rooms.json, doors.json and items.json from the labeling station's labels")
    ca.add_argument("export")
    ca.add_argument("--source", nargs=3, action="append", required=True, metavar=("NAME", "ADDRESS", "WIDTH"),
                    help="the same sources the labels were made with")
    ca.add_argument("--only-pc", nargs=2, action="append", metavar=("NAME", "PCS"))
    ca.add_argument("--skip-pc", nargs=2, action="append", metavar=("NAME", "PCS"))
    ca.add_argument("--out", help="folder for the JSON files (default: the export folder)")
    ca.set_defaults(fn=cmd_catalog)

    sh = sub.add_parser("shots", help="screenshots with mean brightness; --dark finds fades")
    sh.add_argument("export")
    sh.add_argument("--dark", type=float, help="only list shots at or below this fraction of the median "
                                               "brightness, e.g. 0.5 for fades and black screens")
    sh.add_argument("--from", dest="start", type=int, default=0)
    sh.add_argument("--to", dest="end", type=int, default=10 ** 9)
    sh.add_argument("--limit", type=int, default=40)
    common(sh)
    sh.set_defaults(fn=cmd_shots)

    c = sub.add_parser("compare", help="first divergence between two recordings of one movie")
    c.add_argument("a")
    c.add_argument("b")
    common(c)
    c.set_defaults(fn=cmd_compare)

    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
