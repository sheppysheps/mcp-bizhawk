#!/usr/bin/env python3
"""tile-graphics: turn emulator memory dumps into tile sheets, maps and sprites.

Standard library only. Typical inputs come from mcp-bizhawk's
bizhawk_dump_memory (see ../SKILL.md). Subcommands:

  tiles             any system: raw tile sheet in a chosen format (gray or colorized)
  gb-tiles          Game Boy: VRAM tile banks, each tile in the palette the game draws it with
  gb-map            Game Boy: a whole 32x32 BG/window tile map + grid JSON
  gb-sprites        Game Boy: every OAM object as a sheet + JSON
  gb-screen         Game Boy: the composed 160x144 screen
  gb-infer-palettes Game Boy Color: palettes recovered from a screenshot
  gb-verify         Game Boy: compare a rendered screen with a screenshot
  montage           any images: side by side, for before/after comparisons

Every image command also writes a zoomed copy (<name>_4x.png by default,
--scale 1 turns it off): 1x files are exact for machine checks, zoomed ones
are for reading. JSON is kept compact: grids are one row per line.

Run `python tilegfx.py <subcommand> -h` for options.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from formats import FORMATS, get_format  # noqa: E402
from gameboy import (GBVideo, infer_palettes, parse_color, parse_io,  # noqa: E402
                     screenshot_warnings, tile_usage)
from png import read_png, scale_pixels, write_png  # noqa: E402

# Exit codes for gb-verify.
EXIT_OK, EXIT_MISMATCH, EXIT_INCONCLUSIVE = 0, 1, 2


# ── Helpers ───────────────────────────────────────────────────────────────

def _read(path):
    """Read a binary file. Returns: bytes. Requires: read access."""
    with open(path, "rb") as f:
        return f.read()


def _dumps(obj, indent=0):
    """JSON text where lists of plain values stay on one line.

    Keeps grids (32 rows of 32 numbers) and palettes readable and small,
    instead of json.dump's one-number-per-line output.
    Returns: str. Requires: json.
    """
    pad, inner = "  " * indent, "  " * (indent + 1)
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        items = [f"{inner}{json.dumps(k)}: {_dumps(v, indent + 1)}" for k, v in obj.items()]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(obj, list):
        def flat(v):
            return not isinstance(v, (dict, list))
        if not obj or all(flat(v) for v in obj):
            return json.dumps(obj)                                   # [1, 2, 3] on one line
        if all((isinstance(v, list) and all(flat(x) for x in v)) or
               (isinstance(v, dict) and all(flat(x) for x in v.values())) for v in obj):
            return "[\n" + ",\n".join(inner + json.dumps(v) for v in obj) + "\n" + pad + "]"  # one row per line
        return "[\n" + ",\n".join(inner + _dumps(v, indent + 1) for v in obj) + "\n" + pad + "]"
    return json.dumps(obj)


def _write_json(path, obj):
    """Write compact, readable JSON (see _dumps). Returns: None. Requires: write access."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_dumps(obj) + "\n")


def _save_png(path, width, height, pixels, scale):
    """Write a 1x PNG and, for scale > 1, a nearest-neighbour zoomed copy.

    The zoomed copy is named <stem>_<scale>x.png next to the original.
    Returns: list of paths written. Requires: write access.
    """
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    write_png(path, width, height, pixels)
    written = [path]
    if scale > 1:
        stem, ext = os.path.splitext(path)
        zoomed = f"{stem}_{scale}x{ext or '.png'}"
        write_png(zoomed, *scale_pixels(width, height, pixels, scale))
        written.append(zoomed)
    return written


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


def _load_palettes_json(path):
    """Read a palettes JSON file (as written by gb-infer-palettes).

    Returns: dict, or None when path is empty. Requires: read access.
    """
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _palettes(args, video):
    """Resolve palettes from --palettes JSON (if any). Returns (bg, obj, source)."""
    return video.palettes(_load_palettes_json(getattr(args, "palettes", None)))


def _tile_colors(args, levels):
    """Colors for the `tiles` command: --colors, --palettes/--palette, or gray.

    Returns: (list of `levels` (r, g, b), description string).
    Raises SystemExit for a malformed or too-short color list.
    Requires: a palettes JSON file when --palettes is used.
    """
    if args.colors:
        colors = [parse_color(c.strip()) for c in args.colors.split(",")]
    elif args.palettes:
        layer, _, number = (args.palette or "bg:0").partition(":")
        pals = _load_palettes_json(args.palettes).get(layer)
        if pals is None or not number.isdigit() or int(number) >= len(pals):
            sys.exit(f"--palette must be bg:N or obj:N with N in the palettes file (got {args.palette!r})")
        entry = pals[int(number)] or []
        gray = _gray(levels)
        colors = [parse_color(c) if c else gray[i] for i, c in enumerate(entry)]
        return colors + gray[len(colors):], f"palette {layer}:{number} from {os.path.basename(args.palettes)}"
    else:
        return _gray(levels), "grayscale by color index (0 = white)"
    if len(colors) < levels:
        sys.exit(f"--colors needs {levels} colors for this format, got {len(colors)}")
    return colors, "colors from --colors"


# ── Commands ──────────────────────────────────────────────────────────────

def cmd_tiles(args):
    """Render a raw tile sheet in any registered format.

    Returns: None; writes args.out (PNG) and a zoomed copy. Requires: a dump file.
    """
    fmt = get_format(args.format)
    data = _read(args.input)
    start = args.offset
    count = args.count or (len(data) - start) // fmt.bytes_per_tile
    cols = args.columns
    rows = (count + cols - 1) // cols
    levels = 1 << fmt.bits_per_pixel
    palette, how = _tile_colors(args, levels)
    pixels = [(0, 0, 0, 0)] * (cols * 8 * rows * 8)
    for t in range(count):
        off = start + t * fmt.bytes_per_tile
        if off + fmt.bytes_per_tile > len(data):
            break
        px = fmt.decode(data, off)
        tx, ty = (t % cols) * 8, (t // cols) * 8
        for y in range(8):
            for x in range(8):
                r, g, b = palette[px[y * 8 + x]][:3]
                pixels[(ty + y) * cols * 8 + tx + x] = (r, g, b, 255)
    files = _save_png(args.out, cols * 8, rows * 8, pixels, args.scale)
    print(f"{count} {fmt.name} tiles from offset 0x{start:X}, {cols * 8}x{rows * 8} px, {how}")
    print("wrote " + ", ".join(files))


def cmd_gb_tiles(args):
    """Draw each VRAM tile bank, coloring every tile with the palette it is drawn with.

    Tiles the maps or OAM use get that palette; unused tiles stay grayscale.
    Returns: None; writes <out>_bank0.png (and _bank1.png on CGB), zoomed
    copies, and an optional --json usage summary.
    Requires: VRAM dump (OAM and IO recommended), palettes JSON for CGB colors.
    """
    video = _video(args)
    bg, obj, source = _palettes(args, video)
    use = tile_usage(video)
    gray = _gray(4)
    stem = os.path.splitext(args.out)[0]
    files, summary = [], {}
    for bank in (0, 1) if video.cgb else (0,):
        pixels = [(0, 0, 0, 0)] * (128 * 192)
        counts = {"bg": 0, "obj": 0, "unused": 0}
        for t in range(384):
            layer, pal = use.get((bank, t), (None, 0))
            counts[layer or "unused"] += 1
            colors = gray if layer is None else (bg if layer == "bg" else obj)[pal]
            px = video.tile(bank, t * 16)
            tx, ty = (t % 16) * 8, (t // 16) * 8
            for y in range(8):
                for x in range(8):
                    r, g, b = colors[px[y * 8 + x]][:3]
                    pixels[(ty + y) * 128 + tx + x] = (r, g, b, 255)
        files += _save_png(f"{stem}_bank{bank}.png", 128, 192, pixels, args.scale)
        summary[f"bank{bank}"] = {"tiles_on_background_maps": counts["bg"], "tiles_only_in_sprites": counts["obj"],
                                  "unused_tiles_gray": counts["unused"]}
    if args.json:
        _write_json(args.json, {
            "description": "VRAM tile banks drawn 16 tiles wide (tile n at column n % 16, row n // 16, address "
                           "$8000 + 16n). Each tile is colored with the first palette a tile map or sprite uses it "
                           "with; unused tiles are gray.",
            "palette_source": source, "banks": summary})
        files.append(args.json)
    for name, s in summary.items():
        print(f"{name}: {s['tiles_on_background_maps']} map tiles, {s['tiles_only_in_sprites']} sprite-only tiles, "
              f"{s['unused_tiles_gray']} unused (gray); palettes: {source}")
    print("wrote " + ", ".join(files))


def cmd_gb_map(args):
    """Render a whole Game Boy tile map and describe it as compact grids.

    Returns: None; writes args.out (PNG), a zoomed copy and, with --json, the grids.
    Requires: VRAM dump; IO dump for LCDC/addressing (defaults otherwise).
    """
    video = _video(args)
    bg, _, source = _palettes(args, video)
    which = {"bg": video.bg_map_offset("bg"), "win": video.bg_map_offset("win"),
             "9800": 0x1800, "9c00": 0x1C00}[args.map.lower()]
    pixels, cells = video.render_map(which, bg)
    files = _save_png(args.out, 256, 256, pixels, args.scale)
    tiles = [[c["tile"] for c in cells[r * 32:(r + 1) * 32]] for r in range(32)]
    attrs = [[c["attr"] for c in cells[r * 32:(r + 1) * 32]] for r in range(32)]
    summary = {"distinct_tiles": len({c["tile"] for c in cells}),
               "palettes_used": sorted({c["palette"] for c in cells}) if video.cgb else [0],
               "cells_drawn_over_sprites": sum(1 for c in cells if c["priority"]),
               "flipped_cells": sum(1 for c in cells if c["attr"] & 0x60)}
    if args.json:
        doc = {"description": f"Tile map at ${0x8000 + which:04X}: 32x32 cells, 8x8 pixels each (256x256 px; the "
                              "screen shows a 160x144 window of it). tile_numbers[row][col] is the tile index.",
               "map_address": f"${0x8000 + which:04X}", "mode": "CGB" if video.cgb else "DMG",
               "lcdc": f"0x{video.io['LCDC']:02X}", "palette_source": source, "summary": summary,
               "tile_numbers": tiles}
        if video.cgb:
            doc["cgb_attribute_bits"] = ("bits 0-2 palette, bit 3 VRAM bank, bit 5 flip x, bit 6 flip y, "
                                         "bit 7 drawn over sprites")
            doc["cgb_attributes"] = attrs
        _write_json(args.json, doc)
        files.append(args.json)
    print(f"map ${0x8000 + which:04X}: {summary['distinct_tiles']} distinct tiles, palettes {summary['palettes_used']}, "
          f"palettes from: {source}")
    print("wrote " + ", ".join(files))


def describe_object(o):
    """Readable JSON record for one OAM object. Returns: dict. Requires: nothing."""
    return {"oam_slot": o["index"], "screen_x": o["x"], "screen_y": o["y"], "tile_number": o["tile"],
            "size": f"8x{o['height']}", "palette": o["palette"], "vram_bank": o["bank"],
            "flip_x": o["x_flip"], "flip_y": o["y_flip"], "behind_background": o["behind_bg"]}


def cmd_gb_sprites(args):
    """Draw every OAM object on a sheet and list the on-screen ones.

    Returns: None; writes args.out (PNG, 10 columns of 16x24 cells), a zoomed
    copy and --json. Requires: VRAM, OAM and (for 8x16 mode/palettes) IO dumps.
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
                    r, g, b = colors[ci][:3]
                    pixels[(oy + y) * cols * cell_w + ox + x] = (r, g, b, 255)
    files = _save_png(args.out, cols * cell_w, rows * cell_h, pixels, args.scale)
    shown = [o for o in objs if o["visible"]]
    sprites_on = bool(video.io["LCDC"] & 0x02)
    if args.json:
        _write_json(args.json, {
            "description": "OAM objects (hardware sprites). The sheet shows slot n at column n % 10, row n // 10. "
                           "screen_x/screen_y are the top-left corner on the 160x144 screen.",
            "mode": "CGB" if video.cgb else "DMG", "lcdc": f"0x{video.io['LCDC']:02X}",
            "sprites_enabled_in_lcdc": sprites_on, "palette_source": source,
            "on_screen": [describe_object(o) for o in shown],
            "off_screen_slots": [o["index"] for o in objs if not o["visible"]]})
        files.append(args.json)
    print(f"{len(objs)} objects, {len(shown)} on screen, palettes: {source}")
    if not sprites_on and shown:
        print("note: LCDC bit 1 is clear (sprites off) in this dump, yet objects sit on screen: the game may turn "
              "sprites off only briefly (e.g. at vblank). Compare with the screenshot; try --lcdc with bit 1 set.")
    print("wrote " + ", ".join(files))


def cmd_gb_screen(args):
    """Compose the 160x144 screen from the dumps.

    Returns: None; writes args.out (PNG) and a zoomed copy. Requires: VRAM + IO (OAM optional).
    """
    video = _video(args)
    bg, obj, source = _palettes(args, video)
    files = _save_png(args.out, 160, 144, video.render_screen(bg, obj), args.scale)
    print(f"screen (palettes: {source})")
    print("wrote " + ", ".join(files))


def cmd_gb_infer(args):
    """Recover CGB palettes from a screenshot taken at the dumped moment.

    Returns: None; writes args.out (palettes JSON) and prints stats and warnings.
    Requires: VRAM, OAM, IO dumps and a 160x144 screenshot.
    """
    video = _video(args)
    result = infer_palettes(video, read_png(args.screenshot))
    # Record the registers used: palettes inferred with sprites off have no
    # OBJ colors, and gb-verify warns when it runs with different overrides.
    result["lcdc_used"] = f"0x{video.io['LCDC']:02X}"
    visible = [o for o in video.objects() if o["visible"]]
    if not video.io["LCDC"] & 0x02 and visible and result["stats"]["obj"]["colors_found"] == 0:
        result["warnings"].append(f"LCDC bit 1 is clear (sprites off) but {len(visible)} objects are on screen, so no "
                                  "sprite colors were recovered. If sprites show in the screenshot, rerun with "
                                  f"--lcdc 0x{video.io['LCDC'] | 0x02:02X} here and in every later command.")
    _write_json(args.out, result)
    s = result["stats"]
    print(f"BG {s['bg']['colors_found']}/32 colors, {s['bg']['conflicting_pixels']} conflicting pixels; "
          f"OBJ {s['obj']['colors_found']}/32 colors, {s['obj']['conflicting_pixels']} conflicting pixels")
    for w in result["warnings"]:
        print("WARNING: " + w)
    print("wrote " + args.out)


def _control_video(video):
    """Copy of `video` with all tile pixel data blanked (maps and OAM kept).

    Used as a negative control: a decode that draws nothing should NOT match
    a real screenshot. Returns: GBVideo. Requires: nothing.
    """
    vram = bytearray(video.vram)
    for bank_start in (0, 0x2000) if len(vram) == 0x4000 else (0,):
        vram[bank_start:bank_start + 0x1800] = bytes(0x1800)
    control = GBVideo(bytes(vram), video.oam, dict(video.io), video.cgb)
    return control


def cmd_gb_verify(args):
    """Render the screen and compare it pixel by pixel with a screenshot.

    Also runs a negative control (tile data blanked). If the control matches
    as well, the comparison can't tell a right decode from a wrong one.
    Returns: EXIT_OK (match >= --min-match and conclusive), EXIT_MISMATCH,
    or EXIT_INCONCLUSIVE (matches, but the screenshot or control says the
    match proves little). Writes --diff (zoomed copy too) and --report JSON.
    Requires: VRAM + IO dumps (OAM optional), screenshot, palettes for CGB.
    """
    video = _video(args)
    bg, obj, source = _palettes(args, video)
    rendered = video.render_screen(bg, obj)
    w, h, shot = read_png(args.screenshot)
    if (w, h) != (160, 144):
        sys.exit(f"screenshot must be 160x144 (got {w}x{h})")

    def match(pixels):
        return sum(1 for a, b in zip(pixels, shot) if a[:3] == b[:3])

    same = match(rendered)
    pct = 100.0 * same / len(rendered)
    control_pct = 100.0 * match(_control_video(video).render_screen(bg, obj)) / len(rendered)
    warnings = screenshot_warnings(shot)
    control_matches = control_pct >= args.min_match
    if control_matches:
        warnings.append(f"negative control (all tiles blank) also matches {control_pct:.2f}%: this comparison "
                        "can't distinguish a correct decode from an empty one")
    conclusive = not warnings          # only plain-screenshot and control warnings so far
    given = _load_palettes_json(args.palettes) or {}
    lcdc_now = f"0x{video.io['LCDC']:02X}"
    if given.get("lcdc_used") and given["lcdc_used"] != lcdc_now:
        warnings.append(f"the palettes were inferred with LCDC {given['lcdc_used']} but this run uses {lcdc_now}; "
                        "re-infer them with the same overrides (missing sprite colors draw as gray)")
    if "screenshot" in source:
        warnings.append("palettes were recovered from this same screenshot, so the match checks tile/map/sprite "
                        "structure, not the true colors")
    files = []
    if args.diff:
        diff = [(a[0] // 3, a[1] // 3, a[2] // 3, 255) if a[:3] == b[:3] else (255, 0, 255, 255)
                for a, b in zip(rendered, shot)]
        files += _save_png(args.diff, 160, 144, diff, args.scale)
    code = EXIT_MISMATCH if pct < args.min_match else (EXIT_OK if conclusive else EXIT_INCONCLUSIVE)
    if args.report:
        _write_json(args.report, {
            "match_percent": round(pct, 3), "matching_pixels": same, "total_pixels": len(rendered),
            "negative_control_match_percent": round(control_pct, 3), "palette_source": source,
            "lcdc_used": f"0x{video.io['LCDC']:02X}",
            "verdict": ["match", "mismatch", "inconclusive"][code], "warnings": warnings})
        files.append(args.report)
    print(f"{same}/{len(rendered)} pixels match ({pct:.2f}%); negative control {control_pct:.2f}%; "
          f"palettes: {source}; verdict: {['match', 'mismatch', 'inconclusive'][code]}")
    for x in warnings:
        print("WARNING: " + x)
    if files:
        print("wrote " + ", ".join(files))
    return code


def cmd_montage(args):
    """Place PNGs side by side (left to right) for easy comparison.

    Images are aligned to the top and separated by a dark gray gap of --gap
    pixels, then the whole strip is zoomed by --scale.
    Returns: None; writes args.out only (already zoomed). Requires: read access to inputs.
    """
    images = [read_png(p) for p in args.inputs]
    gap = args.gap
    width = sum(w for w, _, _ in images) + gap * (len(images) - 1)
    height = max(h for _, h, _ in images)
    pixels = [(40, 40, 40, 255)] * (width * height)
    x0 = 0
    for w, h, px in images:
        for y in range(h):
            pixels[y * width + x0:y * width + x0 + w] = px[y * w:(y + 1) * w]
        x0 += w + gap
    folder = os.path.dirname(args.out)
    if folder:
        os.makedirs(folder, exist_ok=True)
    write_png(args.out, *scale_pixels(width, height, pixels, max(1, args.scale)))
    print(f"{len(images)} images side by side, {width}x{height} px zoomed {max(1, args.scale)}x")
    print("wrote " + args.out)


# ── CLI ───────────────────────────────────────────────────────────────────

def main(argv=None):
    """Parse arguments and run a subcommand. Returns: process exit code."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def scale_opt(sp):
        sp.add_argument("--scale", type=int, default=4,
                        help="also write a nearest-neighbour copy zoomed this many times as <name>_Nx.png "
                             "(default 4; 1 = no zoomed copy)")

    t = sub.add_parser("tiles", help="raw tile sheet, any system")
    t.add_argument("input", help="dump file (e.g. VRAM or a ROM region)")
    t.add_argument("--format", required=True, choices=sorted(FORMATS), help="tile layout")
    t.add_argument("--offset", type=lambda v: int(v, 0), default=0, help="first byte (e.g. 0x2000)")
    t.add_argument("--count", type=int, default=0, help="tiles to draw (default: to end of file)")
    t.add_argument("--columns", type=int, default=16)
    t.add_argument("--colors", help="comma-separated #RRGGBB per color index, e.g. '#FFFFFF,#AAAAAA,#555555,#000000'")
    t.add_argument("--palettes", help="palettes JSON (from gb-infer-palettes) to color with")
    t.add_argument("--palette", help="which palette in --palettes: bg:N or obj:N (default bg:0)")
    t.add_argument("--out", required=True)
    scale_opt(t)
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

    g = sub.add_parser("gb-tiles", help="VRAM tile banks colored by the palette each tile is drawn with")
    gb_inputs(g)
    g.add_argument("--palettes", help="palettes JSON (CGB)")
    g.add_argument("--out", required=True, help="file name stem: writes <stem>_bank0.png (and _bank1.png on CGB)")
    g.add_argument("--json", help="write a short usage summary here")
    scale_opt(g)
    g.set_defaults(fn=cmd_gb_tiles)

    m = sub.add_parser("gb-map", help="whole BG/window tile map")
    gb_inputs(m)
    m.add_argument("--map", default="bg", help="bg | win | 9800 | 9c00 (default: the BG map LCDC selects)")
    m.add_argument("--palettes", help="palettes JSON (CGB)")
    m.add_argument("--out", required=True)
    m.add_argument("--json", help="write tile-number and attribute grids here")
    scale_opt(m)
    m.set_defaults(fn=cmd_gb_map)

    s = sub.add_parser("gb-sprites", help="all OAM objects")
    gb_inputs(s, oam_required=True)
    s.add_argument("--palettes", help="palettes JSON (CGB)")
    s.add_argument("--out", required=True)
    s.add_argument("--json", help="write the on-screen object list here")
    scale_opt(s)
    s.set_defaults(fn=cmd_gb_sprites)

    c = sub.add_parser("gb-screen", help="compose the 160x144 screen")
    gb_inputs(c)
    c.add_argument("--palettes", help="palettes JSON (CGB)")
    c.add_argument("--out", required=True)
    scale_opt(c)
    c.set_defaults(fn=cmd_gb_screen)

    i = sub.add_parser("gb-infer-palettes", help="recover CGB palettes from a screenshot")
    gb_inputs(i)
    i.add_argument("--screenshot", required=True, help="160x144 PNG from bizhawk_screenshot at the dumped moment")
    i.add_argument("--out", required=True, help="palettes JSON to write")
    i.set_defaults(fn=cmd_gb_infer)

    v = sub.add_parser("gb-verify", help="compare a rendered screen with a screenshot (exit 0 match, 1 mismatch, "
                                         "2 inconclusive)")
    gb_inputs(v)
    v.add_argument("--screenshot", required=True)
    v.add_argument("--palettes", help="palettes JSON (CGB)")
    v.add_argument("--diff", help="write a diff PNG here (mismatches magenta)")
    v.add_argument("--report", help="write a small JSON verdict here")
    v.add_argument("--min-match", type=float, default=99.0, help="percent needed for a match")
    scale_opt(v)
    v.set_defaults(fn=cmd_gb_verify)

    mo = sub.add_parser("montage", help="put PNGs side by side (e.g. screenshot | rebuilt | diff)")
    mo.add_argument("inputs", nargs="+", help="PNG files, left to right (use the 1x versions)")
    mo.add_argument("--out", required=True)
    mo.add_argument("--gap", type=int, default=4, help="pixels between images (before zoom)")
    mo.add_argument("--scale", type=int, default=3, help="zoom for the whole strip (default 3)")
    mo.set_defaults(fn=cmd_montage)

    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
