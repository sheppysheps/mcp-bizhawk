#!/usr/bin/env python3
"""save_file: find a battery save's checksum and write edited saves.

  identify <save.bin> --data START END --at OFFSET    which checksum formula
                                                       matches a known-good save
  patch <in.bin> <out.bin> --data START END --at OFFSET --checksum NAME
        [--set OFFSET=VALUE ...] [--set-bit OFFSET.BIT ...] [--clear-bit OFFSET.BIT ...]
                                                       edit bytes or bits and fix
                                                       the checksum

A save here is the raw cart RAM (what BizHawk writes as SaveRAM for Game Boy
games, or what a memory dump of the CartRAM domain contains). Offsets are
into that file. Game-agnostic: the layout comes from the arguments.

Standard library only. System access: reads the input file, writes only the
output file named on the command line (never the input).
"""

import argparse
import sys

FORMULAS = {
    # name: (description, function(bytes) -> int, width in bytes, byte order)
    "sum8": ("8-bit sum of the bytes", lambda b: sum(b) & 0xFF, 1, "little"),
    "sum8-not": ("complement of the 8-bit sum", lambda b: ~sum(b) & 0xFF, 1, "little"),
    "sum8-neg": ("negated 8-bit sum (bytes + checksum = 0)", lambda b: -sum(b) & 0xFF, 1, "little"),
    "xor8": ("XOR of the bytes", lambda b: _xor(b), 1, "little"),
    "sum16-le": ("16-bit sum of the bytes, little-endian", lambda b: sum(b) & 0xFFFF, 2, "little"),
    "sum16-be": ("16-bit sum of the bytes, big-endian", lambda b: sum(b) & 0xFFFF, 2, "big"),
    "sum16-not-le": ("complement of the 16-bit byte sum, little-endian", lambda b: ~sum(b) & 0xFFFF, 2, "little"),
    "sum16-not-be": ("complement of the 16-bit byte sum, big-endian", lambda b: ~sum(b) & 0xFFFF, 2, "big"),
    "sum16-neg-le": ("negated 16-bit byte sum, little-endian", lambda b: -sum(b) & 0xFFFF, 2, "little"),
    "sum16-neg-be": ("negated 16-bit byte sum, big-endian", lambda b: -sum(b) & 0xFFFF, 2, "big"),
}


def _xor(data):
    """XOR of all bytes. Returns: int 0-255. Requires: nothing."""
    x = 0
    for b in data:
        x ^= b
    return x


def checksum_bytes(name, data):
    """The checksum of `data` under formula `name`, as stored bytes.
    Returns: bytes. Raises KeyError for an unknown formula. Requires: nothing."""
    _, fn, width, order = FORMULAS[name]
    return fn(data).to_bytes(width, order)


def identify(save, start, end, at):
    """Which formulas reproduce the checksum stored at `at` for save[start:end].

    One sample can match more than one formula by chance (more likely for
    1-byte ones, and a 1-byte formula also matches the low byte of a 2-byte
    one); confirm with a second save when possible.
    Returns: list of formula names that match, widest checksum first.
    Requires: nothing.
    """
    data = save[start:end]
    hits = []
    for name, (_, _, width, _) in FORMULAS.items():
        if save[at:at + width] == checksum_bytes(name, data):
            hits.append(name)
    return sorted(hits, key=lambda n: -FORMULAS[n][2])


def patch(save, start, end, at, formula, sets=(), set_bits=(), clear_bits=()):
    """Edit a save and recompute its checksum.

    sets: (offset, value) pairs; set_bits / clear_bits: (offset, bit) pairs.
    Edits outside [start, end) are refused, since the checksum wouldn't cover
    them. Returns: the new save as bytes. Raises ValueError on a bad edit.
    Requires: nothing.
    """
    out = bytearray(save)
    for off, value in sets:
        _check(off, start, end)
        if not 0 <= value <= 0xFF:
            raise ValueError(f"value {value} at 0x{off:X} isn't a byte")
        out[off] = value
    for off, bit in set_bits:
        _check(off, start, end)
        out[off] |= 1 << bit
    for off, bit in clear_bits:
        _check(off, start, end)
        out[off] &= ~(1 << bit) & 0xFF
    cs = checksum_bytes(formula, bytes(out[start:end]))
    out[at:at + len(cs)] = cs
    return bytes(out)


def _check(off, start, end):
    """Refuse an edit outside the checksummed data. Returns: None. Raises ValueError."""
    if not start <= off < end:
        raise ValueError(f"offset 0x{off:X} is outside the checksummed data 0x{start:X}-0x{end - 1:X}")


def _offset_value(text):
    """Parse "OFFSET=VALUE". Returns: (int, int). Raises ValueError."""
    off, _, value = text.partition("=")
    return int(off, 0), int(value, 0)


def _offset_bit(text):
    """Parse "OFFSET.BIT". Returns: (int, int). Raises ValueError."""
    off, _, bit = text.rpartition(".")
    b = int(bit)
    if not 0 <= b <= 7:
        raise ValueError(f"bit {b} isn't 0-7")
    return int(off, 0), b


def main(argv=None):
    """Parse arguments and run a command. Returns: exit code."""
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("identify", "patch"):
        s = sub.add_parser(name)
        s.add_argument("save")
        if name == "patch":
            s.add_argument("out")
            s.add_argument("--checksum", required=True, choices=sorted(FORMULAS))
            s.add_argument("--set", action="append", default=[], type=_offset_value, metavar="OFFSET=VALUE")
            s.add_argument("--set-bit", action="append", default=[], type=_offset_bit, metavar="OFFSET.BIT")
            s.add_argument("--clear-bit", action="append", default=[], type=_offset_bit, metavar="OFFSET.BIT")
        s.add_argument("--data", nargs=2, required=True, metavar=("START", "END"),
                       help="the checksummed bytes, END exclusive, e.g. 0 0xCC")
        s.add_argument("--at", required=True, help="offset of the stored checksum, e.g. 0xCC")
    args = p.parse_args(argv)
    with open(args.save, "rb") as f:
        save = f.read()
    start, end, at = int(args.data[0], 0), int(args.data[1], 0), int(args.at, 0)
    if args.cmd == "identify":
        hits = identify(save, start, end, at)
        for h in hits:
            print(f"{h}: {FORMULAS[h][0]}")
        if len(hits) > 1:
            print("(the first, widest match is the most specific; confirm with a second save)")
        if not hits:
            print("no known formula matches (try other --data bounds, or the checksum is a CRC or keyed)")
        return 0 if hits else 1
    try:
        out = patch(save, start, end, at, args.checksum, args.set, args.set_bit, args.clear_bit)
    except ValueError as err:
        print(err)
        return 1
    if args.out == args.save:
        print("refusing to overwrite the input; write to a new file")
        return 1
    with open(args.out, "wb") as f:
        f.write(out)
    changed = sum(1 for a, b in zip(save, out) if a != b)
    print(f"wrote {args.out}: {changed} bytes changed, checksum {checksum_bytes(args.checksum, out[start:end]).hex()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
