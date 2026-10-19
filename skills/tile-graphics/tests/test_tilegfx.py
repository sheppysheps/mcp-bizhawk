"""Unit tests for the tile-graphics skill. Standard library only.

Run from the repo root:  python -m unittest discover -s skills/tile-graphics/tests -v
"""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

from formats import get_format, flip  # noqa: E402
from gameboy import GBVideo, infer_palettes, parse_io, dmg_palette, DMG_SHADES  # noqa: E402
from png import read_png, write_png  # noqa: E402

# Pan Docs "VRAM Tile Data" example tile; its first row decodes to 0 2 3 3 3 3 2 0.
PANDOCS_TILE = bytes([0x3C, 0x7E, 0x42, 0x42, 0x42, 0x42, 0x42, 0x42, 0x7E, 0x5E, 0x7E, 0x0A, 0x7C, 0x56, 0x38, 0x7C])


def solid_tile(ci):
    """2bpp tile whose every pixel has color index `ci`. Returns 16 bytes."""
    lo = 0xFF if ci & 1 else 0x00
    hi = 0xFF if ci & 2 else 0x00
    return bytes([lo, hi] * 8)


def io_bytes(lcdc=0x91, scy=0, scx=0, bgp=0xE4, obp0=0xE4, obp1=0xE4, wy=0, wx=0):
    """12 bytes as dumped from $FF40-$FF4B."""
    return bytes([lcdc, 0, scy, scx, 0, 0, 0, bgp, obp0, obp1, wy, wx])


class Formats(unittest.TestCase):
    def test_pandocs_example_row(self):
        px = get_format("gb2bpp").decode(PANDOCS_TILE, 0)
        self.assertEqual(px[0:8], [0, 2, 3, 3, 3, 3, 2, 0])

    def test_nes_planar(self):
        data = bytes([0x80] + [0] * 7 + [0x80] + [0] * 7)  # leftmost pixel of row 0 has both planes set
        px = get_format("nes2bpp").decode(data, 0)
        self.assertEqual(px[0], 3)
        self.assertEqual(sum(px), 3)

    def test_snes_4bpp_planes(self):
        data = bytearray(32)
        data[0], data[1], data[16], data[17] = 0x80, 0x00, 0x80, 0x80  # planes 0, 2, 3 set at top-left
        self.assertEqual(get_format("snes4bpp").decode(bytes(data), 0)[0], 0b1101)

    def test_gba_and_genesis_nibble_order(self):
        data = bytes([0x21] + [0] * 31)
        self.assertEqual(get_format("gba4bpp").decode(data, 0)[:2], [1, 2], "GBA: low nibble is the left pixel")
        self.assertEqual(get_format("genesis4bpp").decode(data, 0)[:2], [2, 1], "Genesis: high nibble is the left pixel")

    def test_unknown_format(self):
        with self.assertRaises(KeyError):
            get_format("n64")

    def test_flip(self):
        tile = list(range(64))
        self.assertEqual(flip(tile, True, False)[0], 7)
        self.assertEqual(flip(tile, False, True)[0], 56)
        self.assertEqual(flip(tile, True, True)[0], 63)


class Png(unittest.TestCase):
    def test_roundtrip(self):
        pixels = [(x * 40 % 256, y * 60 % 256, 7, 255 if x else 0) for y in range(3) for x in range(5)]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.png")
            write_png(path, 5, 3, pixels)
            self.assertEqual(read_png(path), (5, 3, pixels))


class GameBoy(unittest.TestCase):
    def make_vram(self, cgb=True):
        """VRAM with tiles 0..3 solid in color 0..3 (bank 0), tile 1 in bank 1 = color 2."""
        vram = bytearray(0x4000 if cgb else 0x2000)
        for t in range(4):
            vram[t * 16:(t + 1) * 16] = solid_tile(t)
        if cgb:
            vram[0x2000 + 16:0x2000 + 32] = solid_tile(2)
        return vram

    def test_io_and_dmg_palette(self):
        io = parse_io(io_bytes(bgp=0x1B))
        self.assertEqual(io["BGP"], 0x1B)
        self.assertEqual(dmg_palette(0x1B), [DMG_SHADES[3], DMG_SHADES[2], DMG_SHADES[1], DMG_SHADES[0]])

    def test_signed_tile_addressing(self):
        v = GBVideo(self.make_vram(), io=parse_io(io_bytes(lcdc=0x81)))  # LCDC.4 = 0 -> $8800 method
        self.assertEqual(v.bg_tile_offset(0), 0x1000)
        self.assertEqual(v.bg_tile_offset(0xFF), 0x0FF0)
        v.io["LCDC"] = 0x91
        self.assertEqual(v.bg_tile_offset(0xFF), 0x0FF0)
        self.assertEqual(v.bg_tile_offset(0x01), 0x0010)

    def test_cgb_attributes_bank_palette_flip(self):
        vram = self.make_vram()
        vram[0x1800] = 1                 # map cell (0,0) uses tile 1
        vram[0x2000 + 0x1800] = 0x08 | 5  # attribute: bank 1, palette 5
        v = GBVideo(vram, io=parse_io(io_bytes()))
        px, pal, prio = v.map_tile_pixels(0x1800, 0, 0)
        self.assertEqual((px[0], pal, prio), (2, 5, False), "tile 1 from bank 1 is solid color 2")

    def test_objects_8x16_and_flags(self):
        oam = bytearray(160)
        oam[0:4] = bytes([16, 8, 0x03, 0x20 | 0x08 | 0x02])  # top-left, tile 3 (-> 2/3 in 8x16), x-flip, bank 1, pal 2
        v = GBVideo(self.make_vram(), oam, parse_io(io_bytes(lcdc=0x97)))  # LCDC.2 = 1 -> 8x16
        o = v.objects()[0]
        self.assertEqual((o["x"], o["y"], o["height"], o["palette"], o["bank"], o["x_flip"], o["visible"]),
                         (0, 0, 16, 2, 1, True, True))
        self.assertEqual(len(v.object_pixels(o)), 128)

    def test_screen_priority_and_palette_inference(self):
        vram = self.make_vram()
        for cell in range(32 * 32):
            vram[0x1800 + cell] = 2                      # whole BG: tile 2 = color index 2
        vram[0x2000 + 0x1800] = 0x80 | 1                 # cell (0,0): BG-over-OBJ, palette 1
        oam = bytearray(160)
        oam[0:4] = bytes([16, 8, 3, 0x00])               # object 0 at (0,0): tile 3 (color 3), OBJ palette 0
        oam[4:8] = bytes([16, 8 + 16, 3, 0x03])          # object 1 at (16,0): OBJ palette 3
        v = GBVideo(vram, oam, parse_io(io_bytes(lcdc=0x93)))  # BG on, OBJ on
        bg = [[(10 * p, 20, 30 + c, ) for c in range(4)] for p in range(8)]
        obj = [[(200, 10 * p, c) for c in range(4)] for p in range(8)]
        screen = v.render_screen(bg, obj)
        self.assertEqual(screen[0][:3], bg[1][2], "BG priority bit wins over the object")
        self.assertEqual(screen[16][:3], obj[3][3], "object drawn over plain BG")
        self.assertEqual(screen[8][:3], bg[0][2])
        # Infer palettes back from the composed screen: should recover what was used.
        result = infer_palettes(v, (160, 144, screen))
        self.assertEqual(result["bg"][0][2], list(bg[0][2]))
        self.assertEqual(result["bg"][1][2], list(bg[1][2]))
        self.assertEqual(result["obj"][3][3], list(obj[3][3]))
        self.assertEqual(result["stats"]["bg"]["conflicts"], 0)

    def test_ten_objects_per_line(self):
        vram = self.make_vram()
        oam = bytearray(160)
        for i in range(11):                               # 11 objects on line 0; the 11th must not show
            oam[i * 4:i * 4 + 4] = bytes([16, 8 + i * 8, 3, 0])
        v = GBVideo(vram, oam, parse_io(io_bytes(lcdc=0x93)))
        layer = v.object_layer()
        self.assertIsNotNone(layer[9 * 8])
        self.assertIsNone(layer[10 * 8], "the 11th object on a line isn't drawn")

    def test_window_and_scroll(self):
        vram = self.make_vram()
        vram[0x1800 + 1] = 1                              # BG map cell (1,0) = tile 1 (color 1)
        for cell in range(32 * 32):
            vram[0x1C00 + cell] = 3                       # window map = tile 3 (color 3)
        io = parse_io(io_bytes(lcdc=0x91 | 0x20 | 0x40, scx=8, wy=100, wx=7))  # window at x=0, y>=100
        v = GBVideo(vram, io=io)
        layers = v.layers()
        self.assertEqual(layers[0][0], 1, "SCX=8 shows map cell (1,0) at screen x=0")
        self.assertEqual(layers[100 * 160][0], 3, "window covers from WY=100")

    def test_bad_vram_size(self):
        with self.assertRaises(ValueError):
            GBVideo(b"\0" * 100)


if __name__ == "__main__":
    unittest.main()
