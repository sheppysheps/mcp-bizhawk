"""Unit tests for findings_export.py, on made-up findings. Standard library only.

Run from the repo root:  python -m unittest discover -s skills/movie-research/tests -v
"""

import ast
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import findings_export as fx  # noqa: E402

ROM_A, ROM_B = "a" * 40, "b" * 40


def finding(fid, address, domain="System Bus", bank=None, confidence="confirmed", **extra):
    """A minimal findings/1 record for the tests."""
    rec = {"format": "findings/1", "id": fid, "kind": extra.pop("kind", "code"), "address": address,
           "domain": domain, "bank": bank, "width": extra.pop("width", 1), "rom_sha1": ROM_A,
           "confidence": confidence, "evidence": [{"source": "rom", "method": "static: test"}]}
    if confidence == "confirmed":
        rec["confirmed_by"] = "test"
    rec.update(extra)
    return rec


def rom(n):
    return {"kind": "rom", "n": n}


SAMPLE = [
    finding("code.main", "0x0150"),
    finding("code.banked.routine", "0x4BFF", bank=rom(4), name="A banked routine", width=45),
    finding("vars.counter", "0xC0A0", domain="WRAM", confidence="likely", kind="variable"),
    finding("flags.one", "0xC0A0", domain="WRAM", kind="flag", bits="0x04"),
    finding("vars.banked_wram", "0xD010", domain="WRAM", bank={"kind": "wram", "n": 2}, kind="variable"),
    finding("vars.hram", "0xFFB3", domain="HRAM", kind="variable"),
    finding("save.block", "0x0000", domain="CartRAM", bank={"kind": "sram", "n": 0}, kind="table"),
    finding("code.lead", "0x4000", bank=rom(1), confidence="lead"),
    finding("code.old", "0x4100", bank=rom(1), superseded_by="code.new"),
    finding("code.no_bank", "0x5000"),
]


class FindingsExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, records, name="f.jsonl"):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
        return path

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = fx.main(list(argv))
        return code, out.getvalue()

    def test_only_confirmed_and_likely_current_findings_are_kept(self):
        kept, skipped = fx.select(SAMPLE)
        self.assertNotIn("code.lead", [f["id"] for f in kept])
        self.assertNotIn("code.old", [f["id"] for f in kept])
        self.assertEqual(skipped, {"confidence lead": ["code.lead"], "superseded": ["code.old"]})

    def test_game_boy_places_cpu_addresses_and_domain_offsets(self):
        gb = fx.GameBoy
        self.assertEqual(gb.place("System Bus", 0x4BFF, rom(4)), (("rom", 4, 0x4BFF), None))
        self.assertEqual(gb.place("ROM", 0x13BFF, None), (("rom", 4, 0x7BFF), None))
        self.assertEqual(gb.place("WRAM", 0xC0A0, None), (("wram", 0, 0xC0A0), None))
        self.assertEqual(gb.place("WRAM", 0x20A0, None), (("wram", 2, 0xD0A0), None))
        self.assertEqual(gb.place("HRAM", 0x33, None), (("hram", 0, 0xFFB3), None))
        self.assertEqual(gb.place("CartRAM", 0x2010, None), (("sram", 1, 0xA010), None))
        spot, why = gb.place("System Bus", 0x5000, None)
        self.assertIsNone(spot)
        self.assertIn("no ROM bank", why)

    def test_sym_file_lists_every_label_sorted_by_bank_and_address(self):
        path = self.write(SAMPLE)
        out = os.path.join(self.dir, "out.sym")
        code, log = self.run_cli("sym", path, "-o", out)
        self.assertEqual(code, 0)
        with open(out, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh if not ln.startswith(";")]
        self.assertEqual(lines, ["00:0150 code_main", "00:A000 save_block", "00:C0A0 flags_one",
                                 "00:C0A0 vars_counter", "00:FFB3 vars_hram", "02:D010 vars_banked_wram",
                                 "04:4BFF code_banked_routine"])
        self.assertIn("code.no_bank: 0x5000 is switchable ROM", log)
        self.assertIn("skipped (confidence lead): 1", log)

    def test_ghidra_script_uses_the_loaders_block_names(self):
        path = self.write(SAMPLE)
        out = os.path.join(self.dir, "labels.py")
        code, _ = self.run_cli("ghidra", path, "-o", out)
        self.assertEqual(code, 0)
        with open(out, encoding="utf-8") as fh:
            text = fh.read()
        compile(text, out, "exec")  # the script must at least be valid Python
        literal = text.split("LABELS = ", 1)[1].split("\n\ndef main", 1)[0]
        labels = {where: (names, comment) for where, names, comment in ast.literal_eval(literal)}
        self.assertEqual(labels["rom4::4bff"][0], ["code_banked_routine"])
        self.assertIn("A banked routine (confirmed, 45 bytes)", labels["rom4::4bff"][1])
        self.assertEqual(labels["c0a0"][0], ["flags_one", "vars_counter"])
        self.assertIn("bits 0x04", labels["c0a0"][1])
        self.assertIn("wram2::d010", labels)
        self.assertIn("0150", labels)

    def test_mixed_roms_need_a_choice(self):
        other = dict(finding("code.other", "0x0200"), rom_sha1=ROM_B)
        path = self.write(SAMPLE[:2] + [other])
        code, log = self.run_cli("sym", path, "-o", os.path.join(self.dir, "x.sym"))
        self.assertEqual(code, 2)
        self.assertIn("2 ROMs", log)
        code, log = self.run_cli("sym", path, "-o", os.path.join(self.dir, "x.sym"), "--rom-sha1", ROM_B)
        self.assertEqual(code, 0)
        self.assertIn("1 labels written", log)

    def test_bad_lines_are_reported_not_fatal(self):
        path = self.write([SAMPLE[0], "not json", {"format": "other"}])
        code, log = self.run_cli("sym", path, "-o", os.path.join(self.dir, "x.sym"))
        self.assertEqual(code, 0)
        self.assertIn("f.jsonl:2: not JSON", log)
        self.assertIn("f.jsonl:3: not a findings/1 record", log)

    def test_a_folder_reads_every_jsonl_file(self):
        os.makedirs(os.path.join(self.dir, "d"))
        self.write(SAMPLE[:1], os.path.join("d", "a.jsonl"))
        self.write(SAMPLE[1:2], os.path.join("d", "b.jsonl"))
        findings, problems = fx.read_findings([os.path.join(self.dir, "d")])
        self.assertEqual([f["id"] for f in findings], ["code.main", "code.banked.routine"])
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
