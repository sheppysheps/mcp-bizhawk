"""Grid map register: read a map image drawn on a grid of squares, turn it into
cells, rooms and doors, and line it up with a capture's labeled transitions.

Plan item A10. Many games (metroidvanias especially) draw their world on a
grid. A map made by a person names every door, including the ones a movie
never uses, but it can be wrong. So this tool treats the map as evidence, not
truth: it reads it, checks it against the labels, and writes a list of
disagreements for a person to settle in a corrections file.

Commands:
  read   config.json [--out DIR]   grid.json + overlay.png (what was read)
  align  config.json [--out DIR]   also result.json + mismatches.md

Everything game-specific lives in the config (image, grid, colours, edge rules,
which labeled places belong to the map, anchors) and the corrections file.
Standard library only; see SKILL.md for the config format.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from png import read_png, write_png  # noqa: E402

# ──────────────────────────────────────────────── config and corrections

DEFAULT_WORDS = {"west": "W", "left": "W", "east": "E", "right": "E", "top": "N", "bottom": "S"}
DEFAULT_RULES = [
    {"class": "key", "color": "key", "min": 0.3},
    {"class": "oneway", "color": "oneway", "min": 0.3},
    {"class": "wall", "color": "line", "min": 0.9},
    {"class": "door", "color": "line", "min": 0.35},
]
OPPOSITE = {"E": "W", "W": "E", "N": "S", "S": "N"}


def load_config(path):
    """Read a map config and resolve its relative paths against the config's folder.

    Returns: dict with defaults filled in (rules, same_room, door_words, tolerance).
    Raises KeyError when a required key (image, grid, colors) is missing.
    Requires: read access to `path`.
    """
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    base = os.path.dirname(os.path.abspath(path))
    for key in ("image", "corrections"):
        if cfg.get(key) and not os.path.isabs(os.path.expandvars(cfg[key])):
            cfg[key] = os.path.join(base, cfg[key])
        elif cfg.get(key):
            cfg[key] = os.path.expandvars(cfg[key])
    for c in cfg.get("catalogs", []):
        c["doors"] = os.path.expandvars(c["doors"])
    missing = [k for k in ("image", "grid", "colors") if k not in cfg]
    if missing:
        raise KeyError(f"{path}: config needs {', '.join(missing)}")
    cfg.setdefault("rules", DEFAULT_RULES)
    cfg.setdefault("same_room", ["open", "key", "oneway"])
    cfg.setdefault("door_words", DEFAULT_WORDS)
    cfg.setdefault("tolerance", 90)
    cfg.setdefault("room_min", 0.3)
    cfg.setdefault("markers", {})
    return cfg


def load_corrections(cfg):
    """Read the corrections file named in the config, if any.

    Returns: dict with "edges" {"a1|a2": {"class": ..., "note": ...}}, "merge"
    {old place id: new place id}, "anchors" {place id: cell}, "moves"
    {frame: {"ignore": true, "note": ...}} (labeled moves that aren't doors,
    such as glitches, left out of the alignment) and "notes" (free text).
    An absent file gives empty corrections.
    Requires: read access to the corrections file.
    """
    out = {"edges": {}, "merge": {}, "anchors": {}, "moves": {}, "notes": []}
    path = cfg.get("corrections")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            out.update(json.load(f))
    return out


# ──────────────────────────────────────────────── reading the image

def cell_name(cfg, c, r):
    """Name a cell the way the map does: row letter + column number, e.g. a3.

    Returns: str. Requires: cfg["grid"] with "row_names" (default a, b, c, ...).
    """
    rows = cfg["grid"].get("row_names", "abcdefghijklmnopqrstuvwxyz")
    return f"{rows[r]}{c + cfg['grid'].get('first_col', 1)}"


def edge_name(cfg, a, b):
    """Name the edge between two neighbouring cells, e.g. a3|a4 (top-left cell first).

    Returns: str. Requires: nothing beyond cell_name.
    """
    a, b = sorted((a, b), key=lambda t: (t[1], t[0]))
    return cell_name(cfg, *a) + "|" + cell_name(cfg, *b)


def classify_pixel(rgb, colors, tolerance):
    """Name a pixel by the nearest configured colour (sum of channel differences).

    colors: {name: [r, g, b]} or {name: {"rgb": [r, g, b], "tolerance": n}};
    a per-colour tolerance overrides the shared one (anti-aliased maps blend
    black lines into greys, so lines usually need a wider one).
    Returns: the colour's name, or "other" when none is within its tolerance.
    Requires: nothing.
    """
    best, score = "other", None
    for name, ref in colors.items():
        rgb_ref, tol = (ref["rgb"], ref.get("tolerance", tolerance)) if isinstance(ref, dict) else (ref, tolerance)
        d = abs(rgb[0] - rgb_ref[0]) + abs(rgb[1] - rgb_ref[1]) + abs(rgb[2] - rgb_ref[2])
        if d <= tol and (score is None or d / (tol + 1) < score):
            best, score = name, d / (tol + 1)
    return best


def rule_share(rule, stats):
    """The share of an edge's pixels a rule's colour(s) cover.

    Returns: float 0..1. Requires: nothing.
    """
    names = rule["color"] if isinstance(rule["color"], list) else [rule["color"]]
    return sum(stats.get(n, 0) for n in names)


def refine_axis(counts, origin, pitch, n):
    """Fit grid lines to the dark-pixel peaks near where the config says they are.

    Each of the n+1 expected lines snaps to the darkest position within a third
    of a cell; a least-squares line through the snapped positions gives the
    refined origin and pitch, so maps with fractional cell sizes stay aligned.
    Returns: (origin, pitch) as floats; the inputs unchanged when too few lines snap.
    Requires: nothing.
    """
    pts = []
    for i in range(n + 1):
        guess = origin + i * pitch
        lo, hi = int(guess - pitch / 3), int(guess + pitch / 3) + 1
        window = [(counts[x], x) for x in range(max(lo, 0), min(hi, len(counts)))]
        if window and max(window)[0] > 0:
            pts.append((i, max(window)[1]))
    if len(pts) < 3:
        return float(origin), float(pitch)
    k = len(pts)
    mi = sum(i for i, _ in pts) / k
    mx = sum(x for _, x in pts) / k
    var = sum((i - mi) ** 2 for i, _ in pts)
    slope = sum((i - mi) * (x - mx) for i, x in pts) / var
    return mx - slope * mi, slope


class Grid:
    """A map image read onto its grid: pixel classes, room cells, edges, markers.

    Built from a config (see load_config). All coordinates are (column, row),
    0-based; names (a3, a3|a4) are only for people.
    Requires: png.read_png; the config's image file.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.w, self.h, px = read_png(cfg["image"])
        colors, tol = cfg["colors"], cfg["tolerance"]
        self.kind = [classify_pixel(p, colors, tol) for p in px]
        self.px = px
        g = cfg["grid"]
        self.cols, self.rows = g["cols"], g["rows"]
        (ox, oy), (cw, ch) = g["origin"], g["cell"]
        if g.get("refine", True):
            dark_max = cfg.get("dark_max", 300)
            dark = [sum(p[:3]) < dark_max for p in px]
            colc = [sum(dark[y * self.w + x] for y in range(self.h)) for x in range(self.w)]
            rowc = [sum(dark[y * self.w + x] for x in range(self.w)) for y in range(self.h)]
            ox, cw = refine_axis(colc, ox, cw, self.cols)
            oy, ch = refine_axis(rowc, oy, ch, self.rows)
        self.ox, self.oy, self.cw, self.ch = ox, oy, cw, ch
        # A cell is a room cell when room colour wins against the background
        # colour; markers and text inside the cell (keys, spikes, letters) are
        # neither, so they don't hide a room cell.
        self.room = {}
        for r in range(self.rows):
            for c in range(self.cols):
                f = self.fraction(self.cell_pixels(c, r))
                room, bg = f.get("room", 0), f.get("background", 0)
                share = room / (room + bg) if room + bg else 0
                if share >= cfg["room_min"] and room >= 0.03:
                    self.room[(c, r)] = round(share, 2)
        self.edges = {}
        for (c, r) in self.room:
            for nb, vertical in (((c + 1, r), True), ((c, r + 1), False)):
                if nb in self.room:
                    stats = self.edge_stats(c, r, vertical)
                    self.edges[((c, r), nb)] = {"class": self.edge_class(stats), "pixels": stats}
        self.markers = self.find_markers()
        self.bridges = self.find_bridges()

    def find_bridges(self):
        """Find bars of the config's bridge colour that join two room cells
        which aren't neighbours (a passage drawn across a gap in the grid).

        A bar is a connected patch of the colour at least 0.9 cells long and
        at most half a cell thick; its two ends name the cells it joins. Dotted
        arrows in the same colour break into small patches and are skipped.
        Returns: list of {"a": cell, "b": cell, "name": "a~b"} for bars whose
        two ends are different room cells. Requires: cfg["bridge"] {"color": [r, g, b],
        "tolerance": n}; none found when it's absent.
        """
        spec = self.cfg.get("bridge")
        if not spec:
            return []
        ref, tol = spec["color"], spec.get("tolerance", 60)
        hit = [abs(p[0] - ref[0]) + abs(p[1] - ref[1]) + abs(p[2] - ref[2]) <= tol for p in self.px]
        seen, out = [False] * len(hit), []
        for start in range(len(hit)):
            if not hit[start] or seen[start]:
                continue
            stack, xs, ys = [start], [], []
            seen[start] = True
            while stack:
                k = stack.pop()
                x, y = k % self.w, k // self.w
                xs.append(x)
                ys.append(y)
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < self.w and 0 <= ny < self.h:
                        j = ny * self.w + nx
                        if hit[j] and not seen[j]:
                            seen[j] = True
                            stack.append(j)
            # A bar's end dots sit on cell boundaries, so the joined cells lie a
            # little beyond each end: look a quarter cell outward.
            wide, tall = max(xs) - min(xs), max(ys) - min(ys)
            if wide >= 0.9 * self.cw and tall <= 0.5 * self.ch:
                ym, reach = (min(ys) + max(ys)) / 2, self.cw / 4
                ends = [self.cell_at(min(xs) - reach, ym), self.cell_at(max(xs) + reach, ym)]
            elif tall >= 0.9 * self.ch and wide <= 0.5 * self.cw:
                xm, reach = (min(xs) + max(xs)) / 2, self.ch / 4
                ends = [self.cell_at(xm, min(ys) - reach), self.cell_at(xm, max(ys) + reach)]
            else:
                continue
            a, b = ends
            if a != b and a in self.room and b in self.room:
                out.append({"a": a, "b": b, "name": cell_name(self.cfg, *a) + "~" + cell_name(self.cfg, *b)})
        return out

    def cell_at(self, px_x, px_y):
        """The (column, row) a pixel position falls in. Returns: tuple of ints."""
        return int((px_x - self.ox) // self.cw), int((px_y - self.oy) // self.ch)

    def x(self, i):
        """Pixel column of vertical grid line i. Returns: int."""
        return int(round(self.ox + i * self.cw))

    def y(self, j):
        """Pixel row of horizontal grid line j. Returns: int."""
        return int(round(self.oy + j * self.ch))

    def cell_pixels(self, c, r, inset=0.2):
        """Pixel classes inside a cell, minus a margin so lines don't count.

        Returns: list of class names. Requires: the decoded image.
        """
        x0, x1, y0, y1 = self.x(c), self.x(c + 1), self.y(r), self.y(r + 1)
        mx, my = int((x1 - x0) * inset), int((y1 - y0) * inset)
        return [self.kind[y * self.w + x] for y in range(y0 + my, y1 - my) for x in range(x0 + mx, x1 - mx)]

    def edge_lanes(self, c, r, vertical):
        """Pixel classes along the edge between a cell and its right (vertical
        edge) or lower (horizontal edge) neighbour, as three parallel lanes one
        pixel apart (maps draw lines 1 to 3 pixels thick), skipping the corners
        where other lines cross.

        Returns: list of three lists of (class name, pixel) pairs.
        Requires: the decoded image.
        """
        lanes = [[], [], []]
        if vertical:
            x, y0, y1 = self.x(c + 1), self.y(r), self.y(r + 1)
            skip = max(2, (y1 - y0) // 8)
            for y in range(y0 + skip, y1 - skip):
                for i, d in enumerate((-1, 0, 1)):
                    k = y * self.w + x + d
                    lanes[i].append((self.kind[k], self.px[k]))
        else:
            y, x0, x1 = self.y(r + 1), self.x(c), self.x(c + 1)
            skip = max(2, (x1 - x0) // 8)
            for x in range(x0 + skip, x1 - skip):
                for i, d in enumerate((-1, 0, 1)):
                    k = (y + d) * self.w + x
                    lanes[i].append((self.kind[k], self.px[k]))
        return lanes

    def edge_stats(self, c, r, vertical):
        """Share of each class along an edge, ignoring dotted grid lines.

        "line" is darkness, not a palette colour: pixels with an RGB sum below
        cfg["dark_max"] (default 300), so anti-aliased black lines count too.
        Only runs of at least cfg["min_run"] dark pixels count (default 3):
        grid dots are 1-2 pixels long, dashes and walls longer. Palette classes
        (key, one-way, background, ...) count runs of 2 or more. Each class
        takes its best lane, so 1-pixel and 3-pixel lines read alike.
        Returns: {class: share 0..1}. Requires: the decoded image.
        """
        dark_max, need = self.cfg.get("dark_max", 300), self.cfg.get("min_run", 3)
        best = {}

        def runs(seq, keep):
            """Pixels per value in runs of at least `keep`. Returns: {value: count}."""
            counts, i = {}, 0
            while i < len(seq):
                j = i
                while j < len(seq) and seq[j] == seq[i]:
                    j += 1
                if j - i >= keep:
                    counts[seq[i]] = counts.get(seq[i], 0) + j - i
                i = j
            return counts

        for lane in self.edge_lanes(c, r, vertical):
            n = len(lane) or 1
            for k, v in runs([k for k, _ in lane], 2).items():
                if k != "line":
                    best[k] = max(best.get(k, 0), round(v / n, 2))
            dark = runs([sum(p[:3]) < dark_max for _, p in lane], need).get(True, 0)
            best["line"] = max(best.get("line", 0), round(dark / n, 2))
        return dict(sorted(best.items()))

    @staticmethod
    def fraction(kinds):
        """Share of each class in a list of pixel classes.

        Returns: {class: fraction rounded to 2 places}; empty for an empty list.
        """
        out = {}
        for k in kinds:
            out[k] = out.get(k, 0) + 1
        n = len(kinds) or 1
        return {k: round(v / n, 2) for k, v in sorted(out.items())}

    def edge_class(self, stats):
        """Apply the config's edge rules in order; the first one that matches wins.

        A rule's "color" is one colour name or a list of names (their shares add up).
        Returns: the rule's class, or "open" when none matches.
        """
        for rule in self.cfg["rules"]:
            if rule_share(rule, stats) >= rule["min"]:
                return rule["class"]
        return "open"

    def find_markers(self):
        """Count each configured marker colour inside every cell.

        Returns: {(c, r): {marker: pixel count}} for cells with at least the
        marker's min_pixels. Requires: cfg["markers"] {name: {"color": [r, g, b],
        "min_pixels": n, "tolerance": t}}.
        """
        out = {}
        for name, m in self.cfg["markers"].items():
            ref, tol, need = m["color"], m.get("tolerance", 40), m.get("min_pixels", 8)
            for r in range(self.rows):
                for c in range(self.cols):
                    n = 0
                    for y in range(self.y(r), self.y(r + 1)):
                        for x in range(self.x(c), self.x(c + 1)):
                            p = self.px[y * self.w + x]
                            if abs(p[0] - ref[0]) + abs(p[1] - ref[1]) + abs(p[2] - ref[2]) <= tol:
                                n += 1
                    if n >= need:
                        out.setdefault((c, r), {})[name] = n
        return out

    def to_json(self):
        """The grid as plain data for grid.json.

        Returns: dict with the fitted grid, room cells, edges (class + pixel
        shares) and markers, all keyed by cell and edge names.
        """
        name = lambda cr: cell_name(self.cfg, *cr)
        return {
            "format": "grid-map/1",
            "image": os.path.basename(self.cfg["image"]),
            "grid": {"origin": [round(self.ox, 2), round(self.oy, 2)], "cell": [round(self.cw, 3), round(self.ch, 3)],
                     "cols": self.cols, "rows": self.rows},
            "cells": {name(k): v for k, v in sorted(self.room.items(), key=lambda t: (t[0][1], t[0][0]))},
            "edges": {edge_name(self.cfg, a, b): e for (a, b), e in sorted(self.edges.items())},
            "markers": {name(k): v for k, v in sorted(self.markers.items())},
            "bridges": [b["name"] for b in self.bridges],
        }


EDGE_COLOURS = {"open": None, "door": (0, 160, 0), "key": (220, 0, 0), "oneway": (0, 140, 255), "wall": (90, 90, 90)}


def write_overlay(grid, rooms, path):
    """Draw what was read on top of the map: each edge in its class colour.

    Doors are green, key doors red, one-ways blue, walls grey; open edges are
    left as drawn. Lets a person check the reading at a glance.
    Returns: None. Requires: png.write_png; write access to `path`.
    """
    px = list(grid.px)
    for (a, b), e in grid.edges.items():
        col = EDGE_COLOURS.get(e["class"])
        if col is None:
            continue
        c, r = a
        if b[0] > a[0]:
            x = grid.x(c + 1)
            pts = [(x + d, y) for y in range(grid.y(r) + 3, grid.y(r + 1) - 2) for d in (-1, 0, 1)]
        else:
            y = grid.y(r + 1)
            pts = [(x, y + d) for x in range(grid.x(c) + 3, grid.x(c + 1) - 2) for d in (-1, 0, 1)]
        for x, y in pts:
            px[y * grid.w + x] = col + (255,)
    write_png(path, grid.w, grid.h, [tuple(p[:3]) + (255,) for p in px])


# ──────────────────────────────────────────────── rooms

def apply_corrections(grid, corr):
    """Override edge classes with the corrections file's "edges" entries.

    Returns: {edge key: class} for every room-to-room edge, plus the list of
    correction names that matched no edge (typos are reported, not ignored).
    """
    by_name = {edge_name(grid.cfg, a, b): (a, b) for (a, b) in grid.edges}
    classes = {k: e["class"] for k, e in grid.edges.items()}
    # Bridges join cells that aren't neighbours; they're named a~b and can be
    # corrected like any edge (for example to "wall" to drop one).
    for br in grid.bridges:
        by_name[br["name"]] = (br["a"], br["b"])
        classes[(br["a"], br["b"])] = grid.cfg.get("bridge", {}).get("class", "bridge")
    unknown = []
    for name, fix in corr["edges"].items():
        # people write either cell first ("h10|g10" or "g10|h10")
        sep = "~" if "~" in name else "|"
        key = name if name in by_name else sep.join(reversed(name.split(sep)))
        if key in by_name:
            classes[by_name[key]] = fix["class"]
        else:
            unknown.append(name)
    return classes, unknown


def build_rooms(grid, classes):
    """Join cells into rooms across every edge whose class is in same_room.

    Returns: (rooms, room_of): rooms is a list of cell lists, ordered by their
    top-left cell; room_of maps each cell to its room's index.
    """
    parent = {c: c for c in grid.room}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b), k in classes.items():
        if k in grid.cfg["same_room"]:
            parent[find(a)] = find(b)
    groups = {}
    for c in grid.room:
        groups.setdefault(find(c), []).append(c)
    rooms = sorted((sorted(v, key=lambda t: (t[1], t[0])) for v in groups.values()), key=lambda v: (v[0][1], v[0][0]))
    room_of = {c: i for i, v in enumerate(rooms) for c in v}
    return rooms, room_of


def room_label(cfg, cells):
    """Short text for a room: its cells' names, e.g. 'a4 b4 c4 d4'. Returns: str."""
    return " ".join(cell_name(cfg, *c) for c in cells)


# ──────────────────────────────────────────────── alignment with labeled moves

def load_moves(cfg, corr):
    """The labeled transitions that stay inside this map, in frame order.

    Reads each catalog/1 doors.json in cfg["catalogs"], keeps transitions whose
    both ends pass the place filter (cfg["places"]: {"prefix": ..., "ids": [...],
    "exclude": [...]}), and renames merged places (corrections "merge").
    Returns: list of transition dicts. Requires: read access to the catalogs.
    """
    flt = cfg.get("places", {})
    prefix, ids, exclude = flt.get("prefix"), set(flt.get("ids", [])), set(flt.get("exclude", []))
    merge = corr.get("merge", {})
    ignored = {int(f) for f, m in corr.get("moves", {}).items() if m.get("ignore")}

    def keep(p):
        return p not in exclude and ((prefix and p.startswith(prefix)) or p in ids)

    moves = []
    for c in cfg.get("catalogs", []):
        with open(c["doors"], encoding="utf-8") as f:
            for t in json.load(f)["transitions"]:
                t = dict(t, **{k: merge.get(t[k], t[k]) for k in ("from", "to")})
                if keep(t["from"]) and keep(t["to"]) and t["from"] != t["to"] and t["frames"][0] not in ignored:
                    moves.append(t)
    return sorted(moves, key=lambda t: t["frames"][0])


def sides(words, label):
    """Directions named in a door label ("bottom-right" -> {S, E}).

    Returns: set of N/E/S/W; empty when the label names none.
    """
    label = (label or "").lower()
    return {s for w, s in words.items() if w in label}


def position(rooms, r, cell):
    """Where a cell sits in its room's bounding box: the thirds it falls in.

    Returns: set of N/E/S/W (a one-row room is both N and S).
    """
    cs = rooms[r]
    c0, c1 = min(c for c, _ in cs), max(c for c, _ in cs)
    r0, r1 = min(y for _, y in cs), max(y for _, y in cs)
    out = set()
    if cell[1] - r0 <= (r1 - r0) / 3:
        out.add("N")
    if r1 - cell[1] <= (r1 - r0) / 3:
        out.add("S")
    if cell[0] - c0 <= (c1 - c0) / 3:
        out.add("W")
    if c1 - cell[0] <= (c1 - c0) / 3:
        out.add("E")
    return out


class Aligner:
    """Find which map room each labeled place is, so every labeled move crosses
    a door of the right kind on the right side.

    Rules, all from the config: a move crosses an edge of class "door" (the
    transition classes); a move whose requires list names the key word must
    touch a room holding a key edge; each direction word in a door label must
    match the edge's wall side or the door's position in the room, any one
    word being enough (labels are approximate). Moves the map can't explain
    are allowed up to a budget and reported.
    Requires: a Grid, its corrected edge classes and rooms.
    """

    def __init__(self, grid, classes, rooms, room_of, corr):
        cfg = grid.cfg
        self.cfg, self.grid, self.rooms, self.room_of = cfg, grid, rooms, room_of
        self.words = cfg["door_words"]
        self.transition = set(cfg.get("transition_classes", ["door", "bridge"]))
        self.key_word = cfg.get("key_requirement", "key")
        self.links = {}
        self.key_rooms = set()
        for (a, b), k in classes.items():
            ra, rb = room_of[a], room_of[b]
            if ra == rb:
                if k == "key":
                    self.key_rooms.add(ra)
                continue
            if k in self.transition:
                s = "E" if b[0] > a[0] else ("W" if b[0] < a[0] else ("S" if b[1] > a[1] else "N"))
                self.links.setdefault((ra, rb), []).append((s, a, b))
                self.links.setdefault((rb, ra), []).append((OPPOSITE[s], b, a))
        names = {cell_name(cfg, *c): c for c in grid.room}
        self.anchors = {}
        for place, cell in corr.get("anchors", {}).items():
            if cell not in names:
                raise SystemExit(f"anchor {place}: no room cell named {cell}")
            self.anchors[place] = room_of[names[cell]]

    def doors(self, t, A, B):
        """The map doors between rooms A and B that fit move t.

        Returns: list of edge names (empty when none fits).
        """
        if self.key_word in (t.get("requires") or []) and not ({A, B} & self.key_rooms):
            return []
        want_x, want_n = sides(self.words, t.get("exit_door")), sides(self.words, t.get("entry_door"))
        out = []
        for side, ca, cb in self.links.get((A, B), []):
            if want_x and not want_x & ({side} | position(self.rooms, A, ca)):
                continue
            if want_n and not want_n & ({OPPOSITE[side]} | position(self.rooms, B, cb)):
                continue
            out.append(edge_name(self.cfg, ca, cb))
        return out

    def solve(self, moves, budget=None, cap=50, node_limit=3_000_000):
        """Search assignments, allowing up to `budget` disagreements with the map.

        Two kinds of disagreement each cost 1: a labeled move with no fitting
        door (the map misses a door, or reads one wrong), and a place put in a
        map room another place already holds (the map misses a room boundary).
        Tries 0 first and stops at the first level with any solution, so it
        reports the fewest disagreements the map allows. `budget` defaults to
        the number of places beyond the map's room count, plus 2.
        Returns: (disagreements used or None, solutions, hit_node_limit), where each
        solution is (assignment {place: room}, per-move list of (move, edges or None)).
        """
        places = {p for t in moves for p in (t["from"], t["to"])}
        if budget is None:
            budget = max(0, len(places) - len(self.rooms)) + 2
        self.nodes = 0
        for allowed in range(budget + 1):
            sols = []

            def place(asg, p, room):
                """Cost of putting place p in room: 1 when another place holds it."""
                return 0 if p in asg else int(room in asg.values())

            def rec(i, asg, used, trace):
                self.nodes += 1
                if len(sols) >= cap or self.nodes > node_limit:
                    return
                if i == len(moves):
                    sols.append((dict(asg), list(trace)))
                    return
                t = moves[i]
                for A in ([asg[t["from"]]] if t["from"] in asg else range(len(self.rooms))):
                    ca = place(asg, t["from"], A)
                    if used + ca > allowed:
                        continue
                    new_a = t["from"] not in asg
                    asg[t["from"]] = A
                    # rooms a fitting door reaches first: cheap solutions are found early
                    tos = [asg[t["to"]]] if t["to"] in asg else sorted(
                        range(len(self.rooms)), key=lambda B: not self.doors(t, A, B))
                    for B in tos:
                        cb = place(asg, t["to"], B)
                        d = self.doors(t, A, B) if B != A else []
                        cost = used + ca + cb + (0 if d else 1)
                        if cost > allowed:
                            continue
                        new_b = t["to"] not in asg
                        asg[t["to"]] = B
                        rec(i + 1, asg, cost, trace + [(t, d or None)])
                        if new_b:
                            del asg[t["to"]]
                    if new_a:
                        del asg[t["from"]]

            rec(0, dict(self.anchors), 0, [])
            if sols or self.nodes > node_limit:
                return (allowed if sols else None), sols, self.nodes > node_limit
        return None, [], False


# ──────────────────────────────────────────────── reports

def mismatches(cfg, grid, classes, rooms, unknown_fixes, moves, used, sols, corr):
    """Write the review list a person works through, as Markdown.

    Sections: summary; labeled moves the map can't explain; places with more
    than one possible room; map rooms no labeled place reached (rooms the
    capture skipped, which need names); edges whose reading is borderline;
    cells with markers the config names as needing review; corrections that
    matched no edge.
    Returns: str (the Markdown). Requires: an alignment result.
    """
    L = [f"# Grid map review: {cfg.get('title', os.path.basename(cfg['image']))}", ""]
    L.append(f"Map `{os.path.basename(cfg['image'])}`: {len(grid.room)} room cells, {len(rooms)} rooms after "
             f"corrections, {len(moves)} labeled moves. Tick a box when it's settled, and write the answer "
             "into the corrections file (or tell Claude).")
    L.append("")
    if not sols:
        L += ["**No assignment fits**, even allowing unexplained moves. Check the anchors and the place filter.", ""]
    else:
        L.append(f"{len(sols)} assignment(s) found{' (search capped)' if len(sols) >= 50 else ''}, "
                 f"with {used} disagreement(s) with the map (the fewest possible). The first assignment is "
                 "shown; alternatives are listed per place.")
        L.append("")
        best = sols[0][0]
        first = {}
        for t in moves:
            first.setdefault(t["to"], t["frames"][0])
            first.setdefault(t["from"], t["frames"][0])
        L += ["## Labeled places on the map", "", "| Place | First labeled move (frame) | Map cells |", "|---|---|---|"]
        for p, r in sorted(best.items(), key=lambda t: t[1]):
            options = {s[0].get(p) for s in sols}
            alt = "" if len(options) == 1 else f" (or: {'; '.join(room_label(cfg, rooms[o]) for o in options if o != r)})"
            L.append(f"| `{p}` | {first.get(p, '')} | {room_label(cfg, rooms[r])}{alt} |")
        L.append("")
        shared = {}
        for p, r in best.items():
            shared.setdefault(r, []).append(p)
        L += ["## Places sharing one map room", "",
              "The labels see a fade the map doesn't draw: a missing room boundary on the map, or two "
              "labels for one room."]
        L += [f"- [ ] {room_label(cfg, rooms[r])}: {', '.join(f'`{p}`' for p in sorted(ps))}. Where's the boundary?"
              for r, ps in sorted(shared.items()) if len(ps) > 1] or ["None."]
        L.append("")
        bad = [t for t, d in sols[0][1] if d is None]
        L += ["## Labeled moves the map can't explain", ""]
        L += [f"- [ ] frame {t['frames'][0]}: `{t['from']}` -> `{t['to']}` ({t.get('exit_door') or '-'} / "
              f"{t.get('entry_door') or '-'}): the map has no fitting door. A missing door on the map, an edge "
              "read as the wrong class, or a label to fix?" for t in bad] or ["None."]
        L.append("")
        amb = sorted(p for p in best if len({s[0].get(p) for s in sols}) > 1)
        L += ["## Places with more than one possible room", ""]
        L += [f"- [ ] `{p}`: which of the rooms above?" for p in amb] or ["None."]
        L.append("")
        used_rooms = set().union(*(set(s[0].values()) for s in sols))
        L += ["## Map rooms no labeled place reached", "",
              "Rooms the capture never entered, or rooms the map splits that the game doesn't (or the reverse). "
              "A new room needs a name to become a place."]
        L += [f"- [ ] {room_label(cfg, rooms[i])}" for i in range(len(rooms)) if i not in used_rooms] or ["None."]
        L.append("")
    L += ["## Edges to confirm", "",
          "Edge readings close to a rule's threshold, and every edge of a class the config lists in "
          "`review_classes` (for example one-ways, which can also be fades)."]
    review = set(cfg.get("review_classes", ["oneway"]))
    fixed = {n for name in corr["edges"] for n in (name, "|".join(reversed(name.split("|"))))}
    rows = []
    for (a, b), e in sorted(grid.edges.items()):
        n = edge_name(cfg, a, b)
        if n in fixed:
            continue
        close = any(abs(rule_share(rule, e["pixels"]) - rule["min"]) < 0.06 for rule in cfg["rules"])
        if e["class"] in review or close:
            rows.append(f"- [ ] {n}: read as **{e['class']}** ({', '.join(f'{k} {v}' for k, v in e['pixels'].items())})")
    L += rows or ["None."]
    L.append("")
    L += ["## Bridges to explain", "",
          "Bars that join two room cells across a gap. The aligner treats them as passages between rooms; "
          "say what each really is (a passage, a one-way, a magnet path...) or mark it \"wall\" to drop it."]
    L += [f"- [ ] {b['name']}" + (" (corrected)" if b["name"] in corr["edges"] else "")
          for b in grid.bridges] or ["None."]
    L.append("")
    flag = cfg.get("review_markers", [])
    marks = [(cell_name(cfg, *c), m) for c, ms in sorted(grid.markers.items(), key=lambda t: (t[0][1], t[0][0]))
             for m in ms if m in flag]
    L += ["## Markers to explain", ""]
    L += [f"- [ ] {c}: `{m}` (not in the map's legend; what does it mean?)" for c, m in marks] or ["None."]
    L.append("")
    if unknown_fixes:
        L += ["## Corrections that matched no edge", ""] + [f"- {n}" for n in unknown_fixes] + [""]
    skipped = [(f, m) for f, m in sorted(corr.get("moves", {}).items(), key=lambda t: int(t[0])) if m.get("ignore")]
    if skipped:
        L += ["## Moves left out (corrections)", ""] + [f"- frame {f}: {m.get('note', '')}" for f, m in skipped] + [""]
    if corr.get("notes"):
        L += ["## Notes already settled", ""] + [f"- {n}" for n in corr["notes"]] + [""]
    return "\n".join(L)


def result_json(cfg, rooms, used, sols):
    """The alignment as data for result.json.

    Returns: dict with rooms (index -> cells), the first assignment, every
    place's possible rooms, and each move's doors (null when unexplained).
    """
    if not sols:
        return {"format": "grid-map-align/1", "rooms": [], "solutions": 0}
    best, trace = sols[0]
    return {
        "format": "grid-map-align/1",
        "unexplained_moves": used,
        "solutions": len(sols),
        "rooms": [[cell_name(cfg, *c) for c in cells] for cells in rooms],
        "places": {p: {"room": r, "cells": [cell_name(cfg, *c) for c in rooms[r]],
                       "alternatives": sorted({s[0].get(p) for s in sols} - {r})} for p, r in best.items()},
        "moves": [{"frame": t["frames"][0], "from": t["from"], "to": t["to"], "doors": d} for t, d in trace],
    }


# ──────────────────────────────────────────────── command line

def main(argv=None):
    """Command-line entry point. Returns: process exit code (0 on success)."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("read", "align"):
        p = sub.add_parser(name)
        p.add_argument("config")
        p.add_argument("--out", help="output folder (default: the config's folder)")
        if name == "align":
            p.add_argument("--budget", type=int, default=None,
                           help="most disagreements with the map to allow (default: extra places + 2)")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    out = a.out or os.path.dirname(os.path.abspath(a.config))
    os.makedirs(out, exist_ok=True)
    grid = Grid(cfg)
    corr = load_corrections(cfg)
    classes, unknown = apply_corrections(grid, corr)
    rooms, room_of = build_rooms(grid, classes)
    with open(os.path.join(out, "grid.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(grid.to_json(), f, indent=1)
        f.write("\n")
    try:
        write_overlay(grid, rooms, os.path.join(out, "overlay.png"))
    except OSError as e:  # e.g. the old overlay is open in an image viewer on Windows
        print(f"warning: overlay.png not written ({e}); close it in other programs and run again")
    print(f"read {len(grid.room)} room cells, {len(grid.edges)} edges, {len(rooms)} rooms -> {out}")
    if a.cmd == "align":
        moves = load_moves(cfg, corr)
        al = Aligner(grid, classes, rooms, room_of, corr)
        used, sols, limited = al.solve(moves, budget=a.budget)
        if limited:
            print(f"search stopped at its node limit after {al.nodes} steps; add anchors to narrow it")
        with open(os.path.join(out, "result.json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump(result_json(cfg, rooms, used, sols), f, indent=1)
            f.write("\n")
        with open(os.path.join(out, "mismatches.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write(mismatches(cfg, grid, classes, rooms, unknown, moves, used, sols, corr))
        print(f"{len(moves)} labeled moves: {len(sols)} assignment(s), {used} unexplained -> mismatches.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
