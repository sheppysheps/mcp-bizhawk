"""Export findings/1 records to disassembler and debugger labels.

Dashboard ADR 0003: findings live in the game repos as JSON Lines, one fact
per address; the fork owns the exporters. Only `confirmed` and `likely`
findings are exported (leads stay out of the tools people trust), and a
finding with `superseded_by` is skipped.

Commands:
  sym     FINDINGS... -o out.sym   `BB:AAAA label` symbol file (BGB, Emulicious,
                                   mgbdis and other Game Boy tools read it)
  ghidra  FINDINGS... -o out.py    a Ghidra script that adds the labels and an
                                   end-of-line comment per address; run it from
                                   the Script Manager (Jython or PyGhidra)

FINDINGS are .jsonl files or folders of them. All exported findings must be
about one ROM (`rom_sha1`); pass --rom-sha1 to pick one when they're mixed.
Addresses are placed with a per-platform memory map (--platform, default gb);
the map knows how each BizHawk domain sits in the CPU's address space and
how the Ghidra loader names banks. Standard library only.
"""

import argparse
import json
import os
import sys

EXPORTED = ("confirmed", "likely")

# ──────────────────────────────────────────────── reading findings


def read_findings(paths):
    """Read findings/1 records from .jsonl files and folders of them.

    Returns: (findings, problems): findings is a list of dicts in file order,
    problems a list of text for lines that aren't JSON or aren't findings/1.
    Requires: read access to the paths.
    """
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += sorted(os.path.join(p, f) for f in os.listdir(p) if f.endswith(".jsonl"))
        else:
            files.append(p)
    findings, problems = [], []
    for path in files:
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                where = f"{os.path.basename(path)}:{n}"
                try:
                    rec = json.loads(line)
                except ValueError as e:
                    problems.append(f"{where}: not JSON ({e})")
                    continue
                if rec.get("format") != "findings/1":
                    problems.append(f"{where}: not a findings/1 record")
                    continue
                missing = [k for k in ("id", "kind", "address", "domain", "bank", "rom_sha1", "confidence")
                           if k not in rec]
                if missing:
                    problems.append(f"{where}: missing {', '.join(missing)}")
                    continue
                rec["_where"] = where
                findings.append(rec)
    return findings, problems


def select(findings, rom_sha1=None):
    """Keep the findings an exporter may write.

    Returns: (kept, skipped): kept is the list to export; skipped maps a
    reason to the ids left out (leads, superseded findings, other ROMs).
    Raises ValueError when the kept findings are about more than one ROM and
    no rom_sha1 was given, since one label file can only describe one ROM.
    """
    skipped = {}
    kept = []
    for f in findings:
        if f["confidence"] not in EXPORTED:
            skipped.setdefault(f"confidence {f['confidence']}", []).append(f["id"])
        elif f.get("superseded_by"):
            skipped.setdefault("superseded", []).append(f["id"])
        elif rom_sha1 and f["rom_sha1"].lower() != rom_sha1.lower():
            skipped.setdefault("another ROM", []).append(f["id"])
        else:
            kept.append(f)
    roms = sorted({f["rom_sha1"].lower() for f in kept})
    if len(roms) > 1:
        raise ValueError(f"findings are about {len(roms)} ROMs ({', '.join(roms)}); pick one with --rom-sha1")
    return kept, skipped


def label_name(finding_id):
    """Turn a dotted finding id into a label every tool accepts.

    Returns: str, the id with dots as underscores (RGBDS reads a dot as a
    local-label separator, and some tools reject it), e.g. code_flags_set.
    """
    return finding_id.replace(".", "_")


# ──────────────────────────────────────────────── platform memory maps


class GameBoy:
    """Where BizHawk's Game Boy domains sit in the CPU's address space.

    Findings may give an address as a CPU address (WRAM 0xCB3A) or as an
    offset into the domain (CartRAM 0x0000); both are accepted. Banks come
    from the finding's `bank`, or from the offset for flat domains.
    Ghidra names follow the GhidraBoy loader: rom0 and wram0 plain, other
    banks as overlays (rom12::4000, wram2::d000, vram1::8000), xram plain.
    Requires: nothing.
    """

    name = "gb"

    @staticmethod
    def place(domain, address, bank):
        """Find a finding's (kind, bank, CPU address).

        Returns: (kind, n, cpu) with kind one of rom, wram, vram, sram, hram,
        oam, io, or None and a reason when the address can't be placed (an
        unknown domain, a switchable ROM address with no bank).
        """
        n = bank.get("n") if isinstance(bank, dict) else None
        d = domain.lower().replace(" ", "")
        if d in ("systembus", "cpu"):
            return GameBoy._cpu(address, bank)
        if d == "rom":
            b = address // 0x4000
            return ("rom", b, address if b == 0 else 0x4000 + address % 0x4000), None
        if d == "wram":
            if address >= 0xC000:
                return GameBoy._cpu(address, bank)
            if address < 0x1000:
                return ("wram", 0, 0xC000 + address), None
            return ("wram", address // 0x1000, 0xD000 + address % 0x1000), None
        if d == "hram":
            return ("hram", 0, address if address >= 0xFF80 else 0xFF80 + address), None
        if d in ("cartram", "sram"):
            if address >= 0xA000:
                return ("sram", n or 0, address), None
            return ("sram", n if n is not None else address // 0x2000, 0xA000 + address % 0x2000), None
        if d == "vram":
            if address >= 0x8000:
                return ("vram", n or 0, address), None
            return ("vram", address // 0x2000, 0x8000 + address % 0x2000), None
        if d == "oam":
            return ("oam", 0, address if address >= 0xFE00 else 0xFE00 + address), None
        return None, f"domain {domain} has no place in the Game Boy map"

    @staticmethod
    def _cpu(cpu, bank):
        """Place a CPU address, taking the bank from the finding.

        Returns: ((kind, n, cpu), None), or (None, reason) for a switchable
        ROM address with no ROM bank (it could be any bank).
        """
        n = bank.get("n") if isinstance(bank, dict) else None
        if cpu < 0x4000:
            return ("rom", 0, cpu), None
        if cpu < 0x8000:
            if n is None or bank.get("kind") != "rom":
                return None, f"0x{cpu:04X} is switchable ROM but the finding gives no ROM bank"
            return ("rom", n, cpu), None
        if cpu < 0xA000:
            return ("vram", n or 0, cpu), None
        if cpu < 0xC000:
            return ("sram", n or 0, cpu), None
        if cpu < 0xD000:
            return ("wram", 0, cpu), None
        if cpu < 0xE000:
            return ("wram", n if n else 1, cpu), None
        if 0xFE00 <= cpu < 0xFEA0:
            return ("oam", 0, cpu), None
        if 0xFF00 <= cpu < 0xFF80:
            return ("io", 0, cpu), None
        if cpu >= 0xFF80:
            return ("hram", 0, cpu), None
        return None, f"0x{cpu:04X} is echo or unusable memory"

    @staticmethod
    def sym_address(kind, n, cpu):
        """The `BB:AAAA` form of a placed address.

        Returns: str, e.g. 04:4BFF; the bank part is the ROM, WRAM, VRAM or
        SRAM bank, and 00 for unbanked memory.
        """
        return f"{n:02X}:{cpu:04X}"

    @staticmethod
    def ghidra_address(kind, n, cpu):
        """The GhidraBoy address string of a placed address.

        Returns: str such as 4bff, rom4::4bff, wram1::d010 or vram1::8000, or
        None when the loader has no block for it (SRAM banks above 0: xram is
        one plain block).
        """
        if kind == "rom" and cpu >= 0x4000:
            return f"rom{n}::{cpu:04x}"
        if kind == "wram" and cpu >= 0xD000:
            return f"wram{n}::{cpu:04x}"
        if kind == "vram":
            return f"vram{n}::{cpu:04x}"
        if kind == "sram" and n:
            return None
        return f"{cpu:04x}"


PLATFORMS = {"gb": GameBoy}


def place_all(findings, platform):
    """Place every finding in the platform's memory map.

    Returns: (placed, unplaced): placed is a list of (finding, kind, n, cpu),
    unplaced a list of text for findings with no place (and why).
    """
    placed, unplaced = [], []
    for f in findings:
        addr = int(f["address"], 16)
        spot, why = platform.place(f["domain"], addr, f["bank"])
        if spot is None:
            unplaced.append(f"{f['id']}: {why}")
        else:
            placed.append((f,) + spot)
    return placed, unplaced


# ──────────────────────────────────────────────── writers


def comment_line(f):
    """One line describing a finding for a comment.

    Returns: str like "code.flags.set: Set a flag (confirmed, bits 0x04)".
    """
    extra = [f["confidence"]]
    if f.get("bits"):
        extra.append(f"bits {f['bits']}")
    if f.get("width", 1) > 1:
        extra.append(f"{f['width']} bytes")
    name = f": {f['name']}" if f.get("name") else ""
    return f"{f['id']}{name} ({', '.join(extra)})"


def write_sym(placed, platform, out, source):
    """Write a `BB:AAAA label` symbol file, sorted by bank and address.

    Returns: number of labels written. Several findings at one address each
    get their own line (symbol files allow it). Requires: write access.
    """
    rows = sorted(((n, cpu, label_name(f["id"])) for f, kind, n, cpu in placed))
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"; findings/1 labels from {source}, made by findings_export.py; "
                 f"confirmed and likely findings only\n")
        for n, cpu, name in rows:
            fh.write(f"{platform.sym_address(None, n, cpu)} {name}\n")
    return len(rows)


GHIDRA_TEMPLATE = '''# findings/1 labels from {source}, made by the mcp-bizhawk fork's findings_export.py.
# Confirmed and likely findings only. Run it from Ghidra's Script Manager on the
# program for ROM SHA-1 {rom}. Running it again adds nothing twice.
# @category findings
from ghidra.program.model.listing import CodeUnit
from ghidra.program.model.symbol import SourceType

LABELS = {labels}

def main():
    af = currentProgram.getAddressFactory()
    symbols = currentProgram.getSymbolTable()
    listing = currentProgram.getListing()
    added = missing = 0
    for where, names, comment in LABELS:
        addr = af.getAddress(where)
        if addr is None or currentProgram.getMemory().getBlock(addr) is None:
            print("no memory at %s, skipped %s" % (where, ", ".join(names)))
            missing += 1
            continue
        have = set(s.getName() for s in symbols.getSymbols(addr))
        for name in names:
            if name not in have:
                symbols.createLabel(addr, name, SourceType.USER_DEFINED)
                added += 1
        old = listing.getComment(CodeUnit.EOL_COMMENT, addr) or ""
        if comment not in old:
            listing.setComment(addr, CodeUnit.EOL_COMMENT, (old + "\\n" + comment) if old else comment)
    print("findings: %d labels added, %d addresses with no memory" % (added, missing))

main()
'''


def write_ghidra(placed, platform, out, source, rom):
    """Write a Ghidra script that adds one label per finding and a comment per address.

    Returns: (labels, unplaceable): the number of labels in the script, and
    text for findings the loader has no block for. The script is plain
    Python 2/3 for Jython or PyGhidra and only uses Ghidra's public API.
    Requires: write access to `out`.
    """
    by_addr, unplaceable = {}, []
    for f, kind, n, cpu in sorted(placed, key=lambda t: (t[2], t[3], t[0]["id"])):
        where = platform.ghidra_address(kind, n, cpu)
        if where is None:
            unplaceable.append(f"{f['id']}: no Ghidra block for {kind} bank {n}")
            continue
        names, lines = by_addr.setdefault(where, ([], []))
        names.append(label_name(f["id"]))
        lines.append(comment_line(f))
    entries = [(w, names, "; ".join(lines)) for w, (names, lines) in by_addr.items()]
    body = "[\n" + "".join(f"    ({w!r}, {names!r}, {c!r}),\n" for w, names, c in entries) + "]"
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(GHIDRA_TEMPLATE.format(source=source, rom=rom, labels=body))
    return sum(len(n) for _, n, _ in entries), unplaceable


# ──────────────────────────────────────────────── command line


def main(argv=None):
    """Command-line entry point. Returns: process exit code (0 on success, 2 on bad input)."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("sym", "ghidra"):
        p = sub.add_parser(name)
        p.add_argument("findings", nargs="+", help=".jsonl files or folders of them")
        p.add_argument("-o", "--out", required=True)
        p.add_argument("--platform", choices=sorted(PLATFORMS), default="gb")
        p.add_argument("--rom-sha1", help="export only the findings about this ROM")
    a = ap.parse_args(argv)
    platform = PLATFORMS[a.platform]
    findings, problems = read_findings(a.findings)
    try:
        kept, skipped = select(findings, a.rom_sha1)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    placed, unplaced = place_all(kept, platform)
    source = ", ".join(os.path.basename(os.path.normpath(p)) for p in a.findings)
    if a.cmd == "sym":
        n = write_sym(placed, platform, a.out, source)
    else:
        rom = kept[0]["rom_sha1"] if kept else "(none)"
        n, more = write_ghidra(placed, platform, a.out, source, rom)
        unplaced += more
    print(f"{len(findings)} findings read, {n} labels written -> {a.out}")
    for reason, ids in sorted(skipped.items()):
        print(f"  skipped ({reason}): {len(ids)}")
    for line in problems + unplaced:
        print(f"  not exported: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
