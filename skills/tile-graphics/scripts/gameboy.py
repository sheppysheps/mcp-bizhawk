"""Game Boy / Game Boy Color video decoding from memory dumps.

Inputs are what mcp-bizhawk can dump (bizhawk_dump_memory):
  VRAM  8 KiB (DMG) or 16 KiB (CGB: bank 0 then bank 1)
  OAM   160 bytes (40 objects x 4 bytes)
  IO    12 bytes from System Bus $FF40-$FF4B (LCDC, STAT, SCY, SCX, LY, LYC,
        DMA, BGP, OBP0, OBP1, WY, WX)
plus, for CGB colors, palettes (palettes JSON) that the caller supplies or
infers from a screenshot (infer_palettes): BizHawk's Lua can't read CGB
palette RAM directly.

All hardware rules follow Pan Docs (https://gbdev.io/pandocs/, CC0):
"VRAM Tile Data", "VRAM Tile Maps" (BG map attributes), "OAM", "LCDC",
"Object Priority". BizHawk's MIT-licensed GB GPU Viewer
(src/BizHawk.Client.EmuHawk/tools/GB/GBGPUView.cs) was used as a cross-check.

Known limits: the dumps describe ONE moment, usually the end of a frame, so
mid-frame effects (scroll changes per line, palette swaps, a status bar made
by changing registers partway down the screen) aren't reproduced.
"""

from formats import get_format, flip

GB2BPP = get_format("gb2bpp")
DMG_SHADES = [(255, 255, 255), (170, 170, 170), (85, 85, 85), (0, 0, 0)]
TRANSPARENT = (0, 0, 0, 0)

IO_NAMES = ["LCDC", "STAT", "SCY", "SCX", "LY", "LYC", "DMA", "BGP", "OBP0", "OBP1", "WY", "WX"]


def parse_io(io_bytes):
    """Decode the 12 bytes dumped from $FF40-$FF4B.

    Returns: dict register name -> value. Raises ValueError for short input.
    Requires: nothing.
    """
    if len(io_bytes) < 12:
        raise ValueError(f"IO dump must be the 12 bytes at $FF40-$FF4B (got {len(io_bytes)})")
    return {name: io_bytes[i] for i, name in enumerate(IO_NAMES)}


def parse_color(value):
    """Accept a color as "#RRGGBB" or [r, g, b].

    Returns: (r, g, b) tuple. Raises ValueError for anything else.
    Requires: nothing.
    """
    if isinstance(value, str):
        s = value.lstrip("#")
        if len(s) != 6:
            raise ValueError(f"color must look like #RRGGBB, got {value!r}")
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    if len(value) != 3:
        raise ValueError(f"color must be [r, g, b], got {value!r}")
    return tuple(int(v) for v in value)


def hex_color(rgb):
    """Format (r, g, b) as "#RRGGBB". Returns: str. Requires: nothing."""
    return "#{:02X}{:02X}{:02X}".format(*rgb[:3])


def dmg_palette(reg):
    """Expand a DMG palette register (BGP/OBP0/OBP1) to 4 RGB shades.

    Returns: list of 4 (r, g, b), color index 0..3 -> shade. Requires: nothing.
    """
    return [DMG_SHADES[(reg >> (2 * i)) & 3] for i in range(4)]


class GBVideo:
    """Decoder for one moment of Game Boy video state."""

    def __init__(self, vram, oam=b"", io=None, cgb=None):
        """Wrap dumps.

        vram: 8 KiB or 16 KiB; oam: 160 bytes (optional); io: parse_io() dict.
        cgb: True/False, or None to decide from the VRAM size (16 KiB = CGB).
        Raises ValueError for an unexpected VRAM size. Requires: nothing.
        """
        if len(vram) not in (0x2000, 0x4000):
            raise ValueError(f"VRAM dump must be 8192 (DMG) or 16384 (CGB) bytes, got {len(vram)}")
        self.vram = bytes(vram)
        self.oam = bytes(oam)
        self.io = io or {"LCDC": 0x91, "SCY": 0, "SCX": 0, "WY": 0, "WX": 0, "BGP": 0xE4, "OBP0": 0xE4, "OBP1": 0xE4}
        self.cgb = (len(vram) == 0x4000) if cgb is None else cgb
        self._tiles = {}

    # ── Tiles ─────────────────────────────────────────────────────────────

    def tile(self, bank, offset):
        """Decode the tile at a VRAM offset ($8000 = offset 0) in a bank.

        Returns: 64 color indices (cached). Requires: bank 1 only on CGB dumps.
        """
        key = (bank, offset)
        if key not in self._tiles:
            self._tiles[key] = GB2BPP.decode(self.vram, bank * 0x2000 + offset)
        return self._tiles[key]

    def bg_tile_offset(self, index):
        """VRAM offset of a BG/window tile index under LCDC bit 4.

        Returns: offset into the bank ($8000 method if bit 4 set, else signed $8800 method).
        Requires: nothing.
        """
        if self.io["LCDC"] & 0x10:
            return index * 16
        signed = index - 256 if index >= 128 else index
        return 0x1000 + signed * 16

    def map_entry(self, map_offset, col, row):
        """Tile index and CGB attribute byte for one tile-map cell.

        map_offset: 0x1800 ($9800) or 0x1C00 ($9C00).
        Returns: (index, attr); attr is 0 on DMG. Requires: nothing.
        """
        cell = map_offset + (row & 31) * 32 + (col & 31)
        attr = self.vram[0x2000 + cell] if self.cgb else 0
        return self.vram[cell], attr

    def map_tile_pixels(self, map_offset, col, row):
        """Decode the tile shown at a map cell, with CGB bank and flips applied.

        Returns: (64 color indices, palette number, bg_priority flag). Requires: nothing.
        """
        index, attr = self.map_entry(map_offset, col, row)
        bank = 1 if (self.cgb and attr & 0x08) else 0
        pixels = self.tile(bank, self.bg_tile_offset(index))
        if attr & 0x60:
            pixels = flip(pixels, bool(attr & 0x20), bool(attr & 0x40))
        return pixels, attr & 0x07, bool(attr & 0x80)

    # ── Objects (sprites) ─────────────────────────────────────────────────

    def objects(self):
        """Parse OAM into 40 object records.

        Returns: list of dicts (index, x, y screen coords, tile, flags, palette,
        bank, x_flip, y_flip, behind_bg, height, visible). Requires: 160 OAM bytes.
        """
        tall = bool(self.io["LCDC"] & 0x04)
        out = []
        for i in range(min(40, len(self.oam) // 4)):
            y, x, tile, flags = self.oam[i * 4:i * 4 + 4]
            height = 16 if tall else 8
            out.append({
                "index": i, "x": x - 8, "y": y - 16, "tile": tile, "flags": flags,
                "palette": (flags & 0x07) if self.cgb else (1 if flags & 0x10 else 0),
                "bank": 1 if (self.cgb and flags & 0x08) else 0,
                "x_flip": bool(flags & 0x20), "y_flip": bool(flags & 0x40),
                "behind_bg": bool(flags & 0x80), "height": height,
                "visible": 0 < x < 168 and (16 - height) < y < 160,
            })
        return out

    def object_pixels(self, obj):
        """Color indices of a whole object (8x8 or 8x16), flips applied.

        Returns: list of height*8 color indices. Requires: nothing.
        """
        tile = obj["tile"] & 0xFE if obj["height"] == 16 else obj["tile"]
        rows = []
        for t in range(obj["height"] // 8):
            px = self.tile(obj["bank"], (tile + t) * 16)
            rows += [px[r * 8:(r + 1) * 8] for r in range(8)]
        if obj["y_flip"]:
            rows.reverse()
        if obj["x_flip"]:
            rows = [list(reversed(r)) for r in rows]
        return [c for r in rows for c in r]

    # ── Palettes ──────────────────────────────────────────────────────────

    def palettes(self, given=None):
        """Resolve palettes to RGB.

        given: palettes JSON dict {"bg": [8 x [4 x [r,g,b] or null]], "obj": ...}.
        Returns: (bg, obj, source): lists of palettes, each 4 (r, g, b) tuples.
        DMG uses BGP/OBP0/OBP1; CGB without `given` falls back to grayscale.
        Requires: nothing.
        """
        if not self.cgb:
            return [dmg_palette(self.io["BGP"])], [dmg_palette(self.io["OBP0"]), dmg_palette(self.io["OBP1"])], "dmg-registers"
        gray = list(DMG_SHADES)
        if not given:
            return [gray] * 8, [gray] * 8, "grayscale (no CGB palettes given)"

        def fill(pals):
            out = []
            for p in range(8):
                src = pals[p] if pals and p < len(pals) and pals[p] else [None] * 4
                out.append([parse_color(src[c]) if c < len(src) and src[c] is not None else gray[c] for c in range(4)])
            return out
        return fill(given.get("bg")), fill(given.get("obj")), given.get("source", "given")

    # ── Rendering ─────────────────────────────────────────────────────────

    def bg_map_offset(self, which="bg"):
        """Tile map in use for the background or window. Returns 0x1800 or 0x1C00."""
        bit = 0x08 if which == "bg" else 0x40
        return 0x1C00 if self.io["LCDC"] & bit else 0x1800

    def render_map(self, map_offset, bg_pals):
        """Render a whole 32x32 tile map (256x256 px).

        Returns: (pixels RGBA list, cells list of dicts per map cell). Requires: nothing.
        """
        pixels = [TRANSPARENT] * (256 * 256)
        cells = []
        for row in range(32):
            for col in range(32):
                px, pal, prio = self.map_tile_pixels(map_offset, col, row)
                index, attr = self.map_entry(map_offset, col, row)
                cells.append({"col": col, "row": row, "tile": index, "attr": attr, "palette": pal, "priority": prio})
                colors = bg_pals[pal if self.cgb else 0]
                for y in range(8):
                    for x in range(8):
                        r, g, b = colors[px[y * 8 + x]]
                        pixels[(row * 8 + y) * 256 + col * 8 + x] = (r, g, b, 255)
        return pixels, cells

    def layers(self):
        """Per-screen-pixel BG/window info at the dumped moment (160x144).

        Returns: list of (color_index, palette, bg_priority) per pixel, or None
        where the background is disabled (DMG with LCDC bit 0 clear). Requires: nothing.
        """
        io = self.io
        lcdc = io["LCDC"]
        bg_on = bool(lcdc & 0x01) or self.cgb   # on CGB bit 0 is priority, not enable
        win_on = bool(lcdc & 0x20) and bg_on and io["WX"] <= 166 and io["WY"] <= 143
        bg_map, win_map = self.bg_map_offset("bg"), self.bg_map_offset("win")
        out = []
        for sy in range(144):
            for sx in range(160):
                if not bg_on:
                    out.append(None)
                    continue
                if win_on and sy >= io["WY"] and sx >= io["WX"] - 7:
                    wx, wy = sx - (io["WX"] - 7), sy - io["WY"]
                    px, pal, prio = self.map_tile_pixels(win_map, wx // 8, wy // 8)
                    ci = px[(wy % 8) * 8 + wx % 8]
                else:
                    bx, by = (sx + io["SCX"]) & 255, (sy + io["SCY"]) & 255
                    px, pal, prio = self.map_tile_pixels(bg_map, bx // 8, by // 8)
                    ci = px[(by % 8) * 8 + bx % 8]
                out.append((ci, pal, prio))
        return out

    def object_layer(self):
        """Which object pixel wins at each screen pixel, before BG priority.

        Applies the 10-objects-per-line limit and object-vs-object priority
        (CGB: OAM order; DMG: smaller X, then OAM order).
        Returns: list per pixel of None or (color_index, palette, behind_bg, obj_index).
        Requires: OAM dump; LCDC bit 1 (objects enabled).
        """
        out = [None] * (160 * 144)
        if not (self.io["LCDC"] & 0x02) or not self.oam:
            return out
        objs = self.objects()
        decoded = {o["index"]: self.object_pixels(o) for o in objs}
        for sy in range(144):
            line = [o for o in objs if o["y"] <= sy < o["y"] + o["height"]][:10]  # first 10 in OAM order
            if not self.cgb:
                line.sort(key=lambda o: (o["x"], o["index"]))
            for sx in range(160):
                for o in line:
                    dx = sx - o["x"]
                    if 0 <= dx < 8:
                        ci = decoded[o["index"]][(sy - o["y"]) * 8 + dx]
                        if ci != 0:
                            out[sy * 160 + sx] = (ci, o["palette"], o["behind_bg"], o["index"])
                            break
        return out

    def render_screen(self, bg_pals, obj_pals):
        """Compose the 160x144 screen: BG/window plus objects with priority rules.

        Returns: list of RGBA pixels. Requires: nothing (OAM optional).
        """
        layers, objs = self.layers(), self.object_layer()
        lcdc0 = bool(self.io["LCDC"] & 0x01)
        out = []
        for i in range(160 * 144):
            bg = layers[i]
            ob = objs[i]
            if bg is None:
                color = DMG_SHADES[0]
                bg_ci, bg_prio = 0, False
            else:
                bg_ci, pal, bg_prio = bg
                color = bg_pals[pal if self.cgb else 0][bg_ci]
            if ob is not None:
                ci, opal, behind, _ = ob
                if self.cgb:
                    obj_wins = bg_ci == 0 or not lcdc0 or (not behind and not bg_prio)
                else:
                    obj_wins = bg_ci == 0 or not behind
                if obj_wins:
                    color = obj_pals[opal][ci]
            out.append((color[0], color[1], color[2], 255))
        return out


def infer_palettes(video, screenshot):
    """Recover CGB palettes from a screenshot of the same moment.

    Every screen pixel tells us "palette P, color index C shows as RGB X".
    BG votes come from pixels where the background is visible; object votes
    from pixels where an object pixel wins. Majority wins per (palette, index).
    screenshot: (width, height, pixels) from png.read_png, 160x144.

    Returns: dict {"description", "bg", "obj": 8 x 4 "#RRGGBB" or null,
    "source", "stats", "warnings"}. stats.*.conflicting_pixels counts votes
    that disagreed with the winner: near zero means the decoding is consistent
    with the picture; many mean wrong registers/dumps or mid-frame effects.
    warnings is non-empty when the screenshot is too plain to trust.
    Raises ValueError if the screenshot isn't 160x144. Requires: nothing.
    """
    w, h, shot = screenshot
    if (w, h) != (160, 144):
        raise ValueError(f"screenshot must be 160x144 (got {w}x{h}); use BizHawk's raw screenshot")
    layers, objs = video.layers(), video.object_layer()
    lcdc0 = bool(video.io["LCDC"] & 0x01)
    votes = {"bg": {}, "obj": {}}
    for i in range(160 * 144):
        rgb = shot[i][:3]
        bg, ob = layers[i], objs[i]
        bg_ci, bg_prio = (bg[0], bg[2]) if bg else (0, False)
        obj_wins = False
        if ob is not None:
            ci, opal, behind, _ = ob
            obj_wins = bg_ci == 0 or not lcdc0 or (not behind and not bg_prio)
            if obj_wins:
                votes["obj"].setdefault((opal, ci), {}).setdefault(rgb, 0)
                votes["obj"][(opal, ci)][rgb] += 1
        if bg is not None and not obj_wins:
            key = (bg[1], bg[0])
            votes["bg"].setdefault(key, {}).setdefault(rgb, 0)
            votes["bg"][key][rgb] += 1
    result = {
        "description": "CGB palettes recovered from a screenshot: bg[p][c] / obj[p][c] is the color shown for "
                       "palette p, color index c, as '#RRGGBB'; null = not visible on screen, so unknown. Colors are "
                       "in the screenshot's color space, not raw RGB555 hardware values.",
        "source": "inferred from screenshot", "stats": {}, "warnings": []}
    found_colors = set()
    for layer in ("bg", "obj"):
        pals = [[None] * 4 for _ in range(8)]
        agree = conflicts = 0
        for (p, c), tally in votes[layer].items():
            best, n = max(tally.items(), key=lambda kv: kv[1])
            pals[p][c] = hex_color(best)
            found_colors.add(best)
            agree += n
            conflicts += sum(tally.values()) - n
        result[layer] = pals
        result["stats"][layer] = {"colors_found": sum(1 for p in pals for c in p if c is not None),
                                  "agreeing_pixels": agree, "conflicting_pixels": conflicts}
    result["warnings"] = screenshot_warnings(shot)
    if len(found_colors) == 1 and not result["warnings"]:
        result["warnings"].append(f"every recovered color is {hex_color(next(iter(found_colors)))}: these aren't "
                                  "the game's real palettes")
    return result


def screenshot_warnings(shot_pixels):
    """Flag screenshots too plain to prove anything (fades, blank frames).

    A frame with one or two colors gives every palette entry the same color,
    so palette recovery and pixel-match checks pass trivially.
    Returns: list of warning strings (empty when the frame looks usable).
    Requires: nothing.
    """
    colors = {p[:3] for p in shot_pixels}
    if len(colors) == 1:
        return [f"the screenshot is one solid color ({hex_color(next(iter(colors)))}), probably a fade or blank "
                "frame: recovered palettes and match percentages mean nothing. Capture a frame with visible content."]
    if len(colors) <= 2:
        return [f"the screenshot has only {len(colors)} colors: most palette entries can't be recovered and a "
                "match proves little. Capture a busier frame."]
    return []


def tile_usage(video):
    """Find the palette each VRAM tile is drawn with, from both tile maps and OAM.

    Used to colorize whole tile banks. A tile used with several palettes keeps
    the first one found (BG maps first, then objects in OAM order).
    Returns: dict (bank, tile_number 0..383) -> ("bg" | "obj", palette number).
    Requires: nothing (OAM optional).
    """
    use = {}
    for map_offset in (0x1800, 0x1C00):
        for row in range(32):
            for col in range(32):
                index, attr = video.map_entry(map_offset, col, row)
                bank = 1 if (video.cgb and attr & 0x08) else 0
                key = (bank, video.bg_tile_offset(index) // 16)
                use.setdefault(key, ("bg", attr & 0x07 if video.cgb else 0))
    for o in video.objects():
        first = o["tile"] & 0xFE if o["height"] == 16 else o["tile"]
        for t in range(o["height"] // 8):
            use.setdefault((o["bank"], first + t), ("obj", o["palette"]))
    return use
