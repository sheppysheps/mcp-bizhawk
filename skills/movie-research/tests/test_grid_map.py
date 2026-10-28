"""Unit tests for grid_map.py, on small maps drawn here. Standard library only.

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

import grid_map  # noqa: E402
from png import write_png  # noqa: E402

WHITE, ROOM, BLACK, RED, MAGENTA = (255, 255, 255), (255, 204, 153), (0, 0, 0), (255, 0, 0), (255, 0, 255)
OX, OY, C = 10, 10, 20  # origin and cell size of the drawn maps


class Canvas:
    """A tiny map painter: room cells, edge styles and bars on a white image.

    Edges are drawn on the shared grid line between two cells, 3 px thick
    unless `thick` says otherwise. Returns nothing; save() writes the PNG.
    Requires: png.write_png.
    """

    def __init__(self, cols, rows):
        self.w, self.h = OX * 2 + cols * C, OY * 2 + rows * C
        self.px = [WHITE] * (self.w * self.h)

    def put(self, x, y, rgb):
        self.px[y * self.w + x] = rgb

    def room(self, c, r):
        for y in range(OY + r * C, OY + (r + 1) * C):
            for x in range(OX + c * C, OX + (c + 1) * C):
                self.put(x, y, ROOM)

    def edge(self, a, b, style, thick=3):
        """Draw the line between cells a and b: dotted, dash, red, wall."""
        (c, r), (c2, _) = a, b
        vertical = c2 > c
        span = range(C)
        for t in span:
            on = {"dotted": t % 2 == 0, "dash": t % 7 < 4, "red": t % 7 < 4, "wall": True}[style]
            if not on:
                continue
            rgb = RED if style == "red" else BLACK
            # dotted grid lines are single dots on white, like a drawn grid
            width = 1 if style == "dotted" else thick
            for d in range(-(width // 2), width - width // 2):
                if vertical:
                    self.put(OX + (c + 1) * C + d, OY + r * C + t, rgb)
                else:
                    self.put(OX + c * C + t, OY + (r + 1) * C + d, rgb)
            if style == "dotted":  # the gaps of a drawn grid line are white
                for t2 in (t + 1,):
                    if t2 < C:
                        if vertical:
                            self.put(OX + (c + 1) * C, OY + r * C + t2, WHITE)
                        else:
                            self.put(OX + c * C + t2, OY + (r + 1) * C, WHITE)

    def bar(self, c_from, c_to, r):
        """A horizontal bar between the boundaries on either side of the gap cells."""
        x0, x1 = OX + (c_from + 1) * C - 3, OX + c_to * C + 3
        y = OY + r * C + C // 2
        for x in range(x0, x1 + 1):
            for d in (-2, -1, 0, 1, 2):
                self.put(x, y + d, MAGENTA)

    def save(self, path):
        write_png(path, self.w, self.h, [p + (255,) for p in self.px])


def config(folder, image, extra=None):
    """Write a config for a drawn map and return its path."""
    cfg = {
        "image": image,
        "grid": {"origin": [OX, OY], "cell": [C, C], "cols": 4, "rows": 2, "refine": False},
        "colors": {"room": list(ROOM), "background": list(WHITE), "key": list(RED)},
        "rules": [{"class": "open", "color": "background", "min": 0.3},
                  {"class": "key", "color": "key", "min": 0.3},
                  {"class": "wall", "color": "line", "min": 0.9},
                  {"class": "door", "color": "line", "min": 0.35}],
        "room_min": 0.5,
        "bridge": {"color": list(MAGENTA)},
        "places": {"prefix": "p-"},
        "catalogs": [{"doors": os.path.join(folder, "doors.json")}],
        "corrections": "corrections.json",
    }
    cfg.update(extra or {})
    path = os.path.join(folder, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return path


def doors(folder, moves):
    """Write a catalog/1 doors.json from (frame, from, to, exit_door, requires) tuples."""
    trans = [{"id": f"m{i}", "from": a, "to": b, "kind": "door", "exit": "other", "entry": "other",
              "exit_door": side, "entry_door": "", "requires": req, "requires_back": None,
              "leave_requires": None, "frames": [f], "evidence": []}
             for i, (f, a, b, side, req) in enumerate(moves)]
    with open(os.path.join(folder, "doors.json"), "w", encoding="utf-8") as f:
        json.dump({"format": "catalog/1", "capture": "test", "transitions": trans}, f)


def draw_map(path, thick=3):
    """The test map: row a is four rooms' worth of cells, row b has a gap at b2.

    a1|a2 dotted (same room), a2|a3 dash (door), a3|a4 red (key door inside a room),
    a1|b1 dash (door), a3|b3 and a4|b4 walls, b3|b4 dotted, and a bar b1~b3 over b2.
    Rooms: {a1, a2}, {a3, a4}, {b1}, {b3, b4}.
    """
    cv = Canvas(4, 2)
    for cell in ((0, 0), (1, 0), (2, 0), (3, 0), (0, 1), (2, 1), (3, 1)):
        cv.room(*cell)
    cv.edge((0, 0), (1, 0), "dotted")
    cv.edge((1, 0), (2, 0), "dash", thick)
    cv.edge((2, 0), (3, 0), "red", thick)
    cv.edge((0, 0), (0, 1), "dash", thick)
    cv.edge((2, 0), (2, 1), "wall")
    cv.edge((3, 0), (3, 1), "wall")
    cv.edge((2, 1), (3, 1), "dotted")
    cv.bar(0, 2, 1)
    cv.save(path)


MOVES = [(10, "p-lobby", "p-east", "east", ["key"]),
         (20, "p-east", "p-lobby", "west", []),
         (30, "p-lobby", "p-south", "bottom", []),
         (40, "p-south", "p-far", "east", [])]


class GridMapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, moves=MOVES, thick=3, corrections=None):
        image = os.path.join(self.dir, "map.png")
        draw_map(image, thick)
        doors(self.dir, moves)
        with open(os.path.join(self.dir, "corrections.json"), "w", encoding="utf-8") as f:
            json.dump(corrections or {"anchors": {"p-lobby": "a1"}}, f)
        return config(self.dir, image)

    def run_align(self, path):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(grid_map.main(["align", path]), 0)
        with open(os.path.join(self.dir, "result.json"), encoding="utf-8") as f:
            result = json.load(f)
        with open(os.path.join(self.dir, "mismatches.md"), encoding="utf-8") as f:
            return result, f.read()

    def test_edges_and_rooms_are_read(self):
        cfg = grid_map.load_config(self.build())
        grid = grid_map.Grid(cfg)
        cls = {grid_map.edge_name(cfg, a, b): e["class"] for (a, b), e in grid.edges.items()}
        self.assertEqual(cls, {"a1|a2": "open", "a2|a3": "door", "a3|a4": "key", "a1|b1": "door",
                               "a3|b3": "wall", "a4|b4": "wall", "b3|b4": "open"})
        self.assertEqual([b["name"] for b in grid.bridges], ["b1~b3"])
        classes, unknown = grid_map.apply_corrections(grid, grid_map.load_corrections(cfg))
        rooms, _ = grid_map.build_rooms(grid, classes)
        self.assertEqual([grid_map.room_label(cfg, r) for r in rooms], ["a1 a2", "a3 a4", "b1", "b3 b4"])
        self.assertEqual(unknown, [])

    def test_one_pixel_dashes_read_as_doors_and_dots_as_open(self):
        cfg = grid_map.load_config(self.build(thick=1))
        grid = grid_map.Grid(cfg)
        cls = {grid_map.edge_name(cfg, a, b): e["class"] for (a, b), e in grid.edges.items()}
        self.assertEqual((cls["a2|a3"], cls["a1|b1"], cls["a1|a2"]), ("door", "door", "open"))

    def test_labeled_moves_align_uniquely(self):
        result, md = self.run_align(self.build())
        self.assertEqual((result["solutions"], result["unexplained_moves"]), (1, 0))
        self.assertEqual({p: " ".join(v["cells"]) for p, v in result["places"].items()},
                         {"p-lobby": "a1 a2", "p-east": "a3 a4", "p-south": "b1", "p-far": "b3 b4"})
        self.assertEqual([m["doors"] for m in result["moves"]], [["a2|a3"], ["a2|a3"], ["a1|b1"], ["b1|b3"]])
        self.assertIn("- [ ] b1~b3", md)

    def test_a_key_move_needs_a_key_door_nearby(self):
        # p-south -> p-far needs a key, but neither b1 nor b3 b4 holds a key door.
        moves = MOVES[:3] + [(40, "p-south", "p-far", "east", ["key"])]
        result, md = self.run_align(self.build(moves))
        self.assertEqual(result["unexplained_moves"], 1)
        self.assertIn("frame 40: `p-south` -> `p-far`", md)

    def test_more_places_than_rooms_are_reported_as_shared(self):
        moves = MOVES + [(50, "p-far", "p-extra", "west", [])]
        result, md = self.run_align(self.build(moves))
        self.assertGreaterEqual(result["unexplained_moves"], 1)
        self.assertIn("## Places sharing one map room", md)
        self.assertRegex(md, r"- \[ \] .*`p-extra`")

    def test_corrections_override_edges_and_report_typos(self):
        corr = {"anchors": {"p-lobby": "a1"}, "edges": {"a1|a2": {"class": "door"}, "a9|a10": {"class": "open"}}}
        cfg = grid_map.load_config(self.build(corrections=corr))
        grid = grid_map.Grid(cfg)
        classes, unknown = grid_map.apply_corrections(grid, grid_map.load_corrections(cfg))
        rooms, _ = grid_map.build_rooms(grid, classes)
        self.assertEqual(len(rooms), 5)
        self.assertEqual(unknown, ["a9|a10"])

    def test_bad_anchor_is_an_error(self):
        path = self.build(corrections={"anchors": {"p-lobby": "b2"}})
        with self.assertRaises(SystemExit):
            self.run_align(path)

    def test_refine_finds_the_grid_from_a_rough_guess(self):
        image = os.path.join(self.dir, "map.png")
        draw_map(image)
        counts = [0] * 200
        for i in range(5):
            counts[OX + i * C] = 30
        origin, pitch = grid_map.refine_axis(counts, OX + 3, C + 0.6, 4)
        self.assertAlmostEqual(origin, OX, places=3)
        self.assertAlmostEqual(pitch, C, places=3)


if __name__ == "__main__":
    unittest.main()
