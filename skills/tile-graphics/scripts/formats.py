"""Tile formats: turn raw tile bytes into 8x8 grids of color indices.

Core-agnostic: each entry describes one well-documented bitplane layout, so a
tile sheet can be made for any system whose tiles use it. Add a format by
writing a decoder and registering it in FORMATS.

Sources: Pan Docs (gbdev.io/pandocs, CC0) for the Game Boy 2bpp layout; the
other layouts are the standard, publicly documented ones for each console.
"""

from dataclasses import dataclass
from typing import Callable, List


@dataclass(frozen=True)
class TileFormat:
    name: str
    bytes_per_tile: int
    bits_per_pixel: int
    systems: str
    decode: Callable[[bytes, int], List[int]]


def _interleaved_2bpp(data, off):
    """GB/GBC/SNES 2bpp: per row, byte 0 = low bit plane, byte 1 = high; bit 7 = leftmost.

    Returns: 64 color indices (0-3), row-major. Requires: 16 bytes at `off`.
    """
    out = []
    for row in range(8):
        lo, hi = data[off + 2 * row], data[off + 2 * row + 1]
        for bit in range(7, -1, -1):
            out.append(((lo >> bit) & 1) | (((hi >> bit) & 1) << 1))
    return out


def _nes_2bpp(data, off):
    """NES 2bpp: bytes 0-7 = low plane rows, bytes 8-15 = high plane rows; bit 7 = leftmost.

    Returns: 64 color indices (0-3). Requires: 16 bytes at `off`.
    """
    out = []
    for row in range(8):
        lo, hi = data[off + row], data[off + 8 + row]
        for bit in range(7, -1, -1):
            out.append(((lo >> bit) & 1) | (((hi >> bit) & 1) << 1))
    return out


def _snes_4bpp(data, off):
    """SNES/PC Engine 4bpp: planes 0+1 interleaved in bytes 0-15, planes 2+3 in bytes 16-31.

    Returns: 64 color indices (0-15). Requires: 32 bytes at `off`.
    """
    out = []
    for row in range(8):
        p0, p1 = data[off + 2 * row], data[off + 2 * row + 1]
        p2, p3 = data[off + 16 + 2 * row], data[off + 16 + 2 * row + 1]
        for bit in range(7, -1, -1):
            out.append(((p0 >> bit) & 1) | (((p1 >> bit) & 1) << 1) | (((p2 >> bit) & 1) << 2) | (((p3 >> bit) & 1) << 3))
    return out


def _gba_4bpp(data, off):
    """GBA 4bpp (linear): 4 bytes per row; each byte holds two pixels, LOW nibble = left pixel.

    Returns: 64 color indices (0-15). Requires: 32 bytes at `off`.
    """
    out = []
    for i in range(32):
        b = data[off + i]
        out += [b & 0x0F, b >> 4]
    return out


def _gba_8bpp(data, off):
    """GBA 8bpp (linear): one byte per pixel. Returns: 64 color indices. Requires: 64 bytes."""
    return list(data[off:off + 64])


def _genesis_4bpp(data, off):
    """Genesis/Mega Drive 4bpp (packed): 4 bytes per row; HIGH nibble = left pixel.

    Returns: 64 color indices (0-15). Requires: 32 bytes at `off`.
    """
    out = []
    for i in range(32):
        b = data[off + i]
        out += [b >> 4, b & 0x0F]
    return out


FORMATS = {
    f.name: f for f in [
        TileFormat("gb2bpp", 16, 2, "Game Boy, Game Boy Color, SNES 2bpp", _interleaved_2bpp),
        TileFormat("nes2bpp", 16, 2, "NES / Famicom", _nes_2bpp),
        TileFormat("snes4bpp", 32, 4, "SNES 4bpp, PC Engine BG", _snes_4bpp),
        TileFormat("gba4bpp", 32, 4, "Game Boy Advance 4bpp, Nintendo DS 4bpp", _gba_4bpp),
        TileFormat("gba8bpp", 64, 8, "Game Boy Advance 8bpp, Nintendo DS 8bpp", _gba_8bpp),
        TileFormat("genesis4bpp", 32, 4, "Sega Genesis / Mega Drive, Master System (packed)", _genesis_4bpp),
    ]
}


def get_format(name):
    """Look up a tile format by name.

    Returns: the TileFormat. Raises KeyError listing valid names otherwise.
    Requires: nothing.
    """
    if name not in FORMATS:
        raise KeyError(f"unknown tile format '{name}'; choose one of: {', '.join(sorted(FORMATS))}")
    return FORMATS[name]


def flip(tile, x_flip, y_flip):
    """Mirror an 8x8 grid of color indices.

    Returns: a new 64-entry list. Requires: nothing.
    """
    out = [0] * 64
    for y in range(8):
        for x in range(8):
            sx = 7 - x if x_flip else x
            sy = 7 - y if y_flip else y
            out[y * 8 + x] = tile[sy * 8 + sx]
    return out
