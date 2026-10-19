#!/usr/bin/env python3
"""tile-graphics: turn emulator memory dumps into tile sheets, maps and sprites.

Standard library only. Typical inputs come from mcp-bizhawk's
bizhawk_dump_memory (see ../SKILL.md). Subcommands:

  tiles             any system: raw tile sheet in a chosen format
  gb-map            Game Boy: a whole 32x32 BG/window tile map + cell JSON
  gb-sprites        Game Boy: every OAM object as a sheet + JSON
  gb-screen         Game Boy: the composed 160x144 screen
  gb-infer-palettes Game Boy Color: palettes recovered from a screenshot
  gb-verify         Game Boy: compare a rendered screen with a screenshot

Run `python tilegfx.py <subcommand> -h` for options.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from formats import FORMATS, get_format  # noqa: E402
from gameboy import GBVideo, infer_palettes, parse_io  # noqa: E402
from png import read_png, write_png  # noqa: E402


def _read(path):
    """Read a binary file. Returns: bytes. Requires: read access."""
    with open(path, "rb") as f:
        return f.read()


def _write_json(path, obj):
    """Write pretty JSON. Returns: None. Requires: write access."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")


def _gray(levels):
    """Evenly spaced grayscale palette, index 0 = white.

    Returns: list of `levels` (r, g, b) tuples. Requires: nothing.
    """
    return [(round(255 - i * 255 / (levels - 1)),) * 3 for i in range(levels)]


def _video(args, need_oam=False):
    """Build a GBVideo from --vram/--oam/--io options.

    Returns: GBVideo. Raises SystemExit with a message for missing inputs.
    Requires: dump files on disk.
    """
    io = parse_io(_read(args.io)) if args.io else None
    if need_oam and not args.oam:
        sys.exit("this command needs --oam (160-byte OAM dump)")
    cgb = True if args.cgb else (False if args.dmg else None)
    video = GBVideo(_read(args.vram), _read(args.oam) if args.oam else b"", io, cgb)
    # Registers dumped at frame end may differ from what was used while the
    # screen was drawn (games change them mid-frame). Let the caller override.
    for name in ("lcdc", "scx", "scy", "wx", "wy"):
        value = getattr(args, name, None)
        if value is not None:
            video.io[name.upper()] = value
    return video


def _palettes(args, video):
    """Resolve palettes from --palettes JSON (if any). Returns (bg, obj, source)."""
    given = None
    if getattr(args, "palettes", None):
        with open(args.palettes, encoding="utf-8") as f:
            given = json.load(f)
    return video.palettes(given)


def cmd_tiles(args):
    """Render a raw tile sheet in any registered format.

    Returns: None; writes args.out (PNG). Requires: a dump file.
    """
    fmt = get_format(args.format)
    data = _read(args.input)
    start = args.offset
    count = args.count or (len(data) - start) // fmt.bytes_per_tile
    cols = args.columns
    rows = (count + cols - 1) // cols
    palette = _gray(1 << fmt.bits_per_pixel) if fmt.bits_per_pixel <= 4 else _gray(256)
    pixels = [(0, 0, 0, 0)] * (cols * 8 * rows * 8)
    for t in range(count):
        off = start + t * fmt.bytes_per_tile
        if off + fmt.bytes_per_tile > len(data):
            break
        px = fmt.decode(data, off)
        tx, ty = (t % cols) * 8, (t // cols) * 8
        for y in range(8):
            for x in range(8):
                r, g, b = palette[px[y * 8 + x]]
                pixels[(ty + y) * cols * 8 + tx + x] = (r, g, b, 255)
    write_png(args.out, cols * 8, rows * 8, pixels)
    print(f"{count} {fmt.name} tiles from offset 0x{start:X} -> {args.out} ({cols * 8}x{rows * 8}, grayscale by color index)")


def cmd_gb_map(args):
    """Render a whole Game Boy tile map and describe every cell.

    Returns: None; writes args.out (PNG) and, with --json, the cell list.
    Requires: VRAM dump; IO dump for LCDC/addressing (defaults otherwise).
    """
    video = _video(args)
    bg, _, source = _palettes(args, video)
    which = {"bg": video.bg_map_offset("bg"), "win": video.bg_map_offset("win"),
             "9800": 0x1800, "9c00": 0x1C00}[args.map.lower()]
    pixels, cells = video.render_map(which, bg)
    write_png(args.out, 256, 256, pixels)
    if args.json:
        _write_json(args.json, {"map": f"${0x8000 + which:04X}", "cgb": video.cgb, "lcdc": video.io["LCDC"],
                                "palettes": source, "cells": cells})
    print(f"map ${0x8000 + which:04X} -> {args.out} (256x256, palettes: {source})")


def cmd_gb_sprites(args):
    """Draw every OAM object on a sheet and list their attributes.

    Returns: None; writes args.out (PNG, 10 columns of 16x24 cells) and --json.
    Requires: VRAM, OAM and (for 8x16 mode/palettes) IO dumps.
    """
    video = _video(args, need_oam=True)
    _, obj_pals, source = _palettes(args, video)
    objs = video.objects()
    cell_w, cell_h, cols = 16, 24, 10
    rows = (len(objs) + cols - 1) // cols
    pixels = [(0, 0, 0, 0)] * (cols * cell_w * rows * cell_h)
    for o in objs:
        px = video.object_pixels(o)
        ox, oy = (o["index"] % cols) * cell_w + 4, (o["index"] // cols) * cell_h + 4
        colors = obj_pals[o["palette"]]
        for y in range(o["height"]):
            for x in range(8):
                ci = px[y * 8 + x]
                if ci:
                    r, g, b = colors[ci]
                    pixels[(oy + y) * cols * cell_w + ox + x] = (r, g, b, 255)
    write_png(args.out, cols * cell_w, rows * cell_h, pixels)
    if args.json:
        _write_json(args.json, {"cgb": video.cgb, "lcdc": video.io["LCDC"], "palettes": source, "objects": objs})
    shown = sum(1 for o in objs if o["visible"])
    print(f"{len(objs)} objects ({shown} on screen) -> {args.out} (palettes: {source})")


def cmd_gb_screen(args):
    """Compose the 160x144 screen from the dumps.

    Returns: None; writes args.out (PNG). Requires: VRAM + IO (OAM optional).
    """
    video = _video(args)
    bg, obj, source = _palettes(args, video)
    write_png(args.out, 160, 144, video.render_screen(bg, obj))
    print(f"screen -> {args.out} (palettes: {source})")


def cmd_gb_infer(args):
    """Recover CGB palettes from a screenshot taken at the dumped moment.

    Returns: None; writes args.out (palettes JSON) and prints stats.
    Requires: VRAM, OAM, IO dumps and a 160x144 screenshot.
    """
    video = _video(args)
    result = infer_palettes(video, read_png(args.screenshot))
    _write_json(args.out, result)
    s = result["stats"]
    print(f"palettes -> {args.out}: BG {s['bg']['entries_found']}/32 colors, {s['bg']['conflicts']} conflicting pixels; "
          f"OBJ {s['obj']['entries_found']}/32 colors, {s['obj']['conflicts']} conflicting pixels")


def cmd_gb_verify(args):
    """Render the screen and compare it pixel by pixel with a screenshot.

    Returns: exit code 0 if the match is >= --min-match percent, else 1.
    Writes --diff (PNG: matches dimmed, mismatches magenta) when given.
    Requires: VRAM + IO dumps (OAM optional), screenshot, palettes for CGB.
    """
    video = _video(args)
    bg, obj, source = _palettes(args, video)
    rendered = video.render_screen(bg, obj)
    w, h, shot = read_png(args.screenshot)
    if (w, h) != (160, 144):
        sys.exit(f"screenshot must be 160x144 (got {w}x{h})")
    same = sum(1 for a, b in zip(rendered, shot) if a[:3] == b[:3])
    pct = 100.0 * same / len(rendered)
    if args.diff:
        diff = [(a[0] // 3, a[1] // 3, a[2] // 3, 255) if a[:3] == b[:3] else (255, 0, 255, 255) for a, b in zip(rendered, shot)]
        write_png(args.diff, 160, 144, diff)
    print(f"{same}/{len(rendered)} pixels match ({pct:.2f}%), palettes: {source}")
    return 0 if pct >= args.min_match else 1


def main(argv=None):
    """Parse arguments and run a subcommand. Returns: process exit code."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tiles", help="raw tile sheet, any system")
    t.add_argument("input", help="dump file (e.g. VRAM or a ROM region)")
    t.add_argument("--format", required=True, choices=sorted(FORMATS), help="tile layout")
    t.add_argument("--offset", type=lambda v: int(v, 0), default=0, help="first byte (e.g. 0x2000)")
    t.add_argument("--count", type=int, default=0, help="tiles to draw (default: to end of file)")
    t.add_argument("--columns", type=int, default=16)
    t.add_argument("--out", required=True)
    t.set_defaults(fn=cmd_tiles)

    def gb_inputs(sp, oam_required=False):
        sp.add_argument("--vram", required=True, help="VRAM dump (8 KiB DMG or 16 KiB CGB)")
        sp.add_argument("--oam", required=oam_required, help="OAM dump (160 bytes)")
        sp.add_argument("--io", help="System Bus $FF40-$FF4B dump (12 bytes): LCDC, SCY, SCX, BGP, OBP0/1, WY, WX ...")
        for reg in ("lcdc", "scx", "scy", "wx", "wy"):
            sp.add_argument(f"--{reg}", type=lambda v: int(v, 0),
                            help=f"override {reg.upper()} from the IO dump (games may change it mid-frame), e.g. 0xE7")
        mode = sp.add_mutually_exclusive_group()
        mode.add_argument("--cgb", action="store_true", help="force CGB mode")
        mode.add_argument("--dmg", action="store_true", help="force DMG mode")

    m = sub.add_parser("gb-map", help="whole BG/window tile map")
    gb_inputs(m)
    m.add_argument("--map", default="bg", help="bg | win | 9800 | 9c00 (default: the BG map LCDC selects)")
    m.add_argument("--palettes", help="palettes JSON (CGB)")
    m.add_argument("--out", required=True)
    m.add_argument("--json", help="write per-cell tile/attribute JSON here")
    m.set_defaults(fn=cmd_gb_map)

    s = sub.add_parser("gb-sprites", help="all OAM objects")
    gb_inputs(s, oam_required=True)
    s.add_argument("--palettes", help="palettes JSON (CGB)")
    s.add_argument("--out", required=True)
    s.add_argument("--json", help="write object list JSON here")
    s.set_defaults(fn=cmd_gb_sprites)

    c = sub.add_parser("gb-screen", help="compose the 160x144 screen")
    gb_inputs(c)
    c.add_argument("--palettes", help="palettes JSON (CGB)")
    c.add_argument("--out", required=True)
    c.set_defaults(fn=cmd_gb_screen)

    i = sub.add_parser("gb-infer-palettes", help="recover CGB palettes from a screenshot")
    gb_inputs(i)
    i.add_argument("--screenshot", required=True, help="160x144 PNG from bizhawk_screenshot at the dumped moment")
    i.add_argument("--out", required=True, help="palettes JSON to write")
    i.set_defaults(fn=cmd_gb_infer)

    v = sub.add_parser("gb-verify", help="compare a rendered screen with a screenshot")
    gb_inputs(v)
    v.add_argument("--screenshot", required=True)
    v.add_argument("--palettes", help="palettes JSON (CGB)")
    v.add_argument("--diff", help="write a diff PNG here")
    v.add_argument("--min-match", type=float, default=99.0, help="percent needed for exit code 0")
    v.set_defaults(fn=cmd_gb_verify)

    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
