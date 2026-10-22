"""Minimal PNG writer and reader, standard library only (zlib + struct).

Kept dependency-free so the skill runs anywhere Python 3.9+ does and every
line is easy to audit. Supports what this skill needs: writing 8-bit RGBA, and
reading 8-bit non-interlaced RGB, RGBA, greyscale and palette PNGs (such as
BizHawk screenshots).
"""

import struct
import zlib

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind, data):
    """Build one PNG chunk.

    Returns: bytes of length + type + data + CRC.
    Requires: zlib (crc32), struct.
    """
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def write_png(path, width, height, pixels):
    """Write an RGBA image.

    pixels: flat list of (r, g, b, a) tuples, row-major, width*height long.
    Returns: None. Raises ValueError when the pixel count doesn't match.
    Requires: zlib, struct; write access to `path`.
    """
    if len(pixels) != width * height:
        raise ValueError(f"expected {width * height} pixels, got {len(pixels)}")
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (None) for every row
        for r, g, b, a in pixels[y * width:(y + 1) * width]:
            raw += bytes((r, g, b, a))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    with open(path, "wb") as f:
        f.write(_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b""))


def scale_pixels(width, height, pixels, factor):
    """Nearest-neighbour upscale, so small tiles stay crisp when zoomed.

    Returns: (width * factor, height * factor, pixels). factor 1 returns the input.
    Requires: nothing.
    """
    if factor <= 1:
        return width, height, pixels
    out = []
    for y in range(height):
        row = []
        for p in pixels[y * width:(y + 1) * width]:
            row += [p] * factor
        out += row * factor
    return width * factor, height * factor, out


def _paeth(a, b, c):
    """PNG Paeth predictor. Returns the predicted byte. Requires: nothing."""
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def read_png(path):
    """Read an 8-bit, non-interlaced PNG.

    Returns: (width, height, pixels) with pixels a flat list of (r, g, b, a).
    Raises ValueError for unsupported formats (16-bit, interlaced, packed bits).
    Requires: zlib, struct; read access to `path`.
    """
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(_SIGNATURE):
        raise ValueError(f"{path} is not a PNG file")
    pos, idat, palette, alpha = 8, bytearray(), None, None
    width = height = depth = ctype = interlace = None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = [tuple(body[i:i + 3]) for i in range(0, len(body), 3)]
        elif kind == b"tRNS":
            alpha = body
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if depth != 8 or interlace != 0 or ctype not in (0, 2, 3, 4, 6):
        raise ValueError(f"unsupported PNG (bit depth {depth}, color type {ctype}, interlace {interlace})")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    stride = width * channels
    raw = zlib.decompress(bytes(idat))
    rows, prev, i = [], bytearray(stride), 0
    for _ in range(height):
        ftype, line = raw[i], bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            left = line[x - channels] if x >= channels else 0
            up = prev[x]
            upleft = prev[x - channels] if x >= channels else 0
            if ftype == 1:
                line[x] = (line[x] + left) & 0xFF
            elif ftype == 2:
                line[x] = (line[x] + up) & 0xFF
            elif ftype == 3:
                line[x] = (line[x] + ((left + up) >> 1)) & 0xFF
            elif ftype == 4:
                line[x] = (line[x] + _paeth(left, up, upleft)) & 0xFF
        rows.append(line)
        prev = line
    pixels = []
    for line in rows:
        for x in range(width):
            px = line[x * channels:(x + 1) * channels]
            if ctype == 6:
                pixels.append(tuple(px))
            elif ctype == 2:
                pixels.append((px[0], px[1], px[2], 255))
            elif ctype == 0:
                pixels.append((px[0], px[0], px[0], 255))
            elif ctype == 4:
                pixels.append((px[0], px[0], px[0], px[1]))
            else:  # palette
                idx = px[0]
                r, g, b = palette[idx]
                a = alpha[idx] if alpha is not None and idx < len(alpha) else 255
                pixels.append((r, g, b, a))
    return width, height, pixels
