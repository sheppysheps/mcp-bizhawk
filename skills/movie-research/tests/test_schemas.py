"""Tests for schemas/findings-1.schema.json and schemas/catalog-1.schema.json.

Needs the jsonschema package (dev only; the scripts themselves stay standard
library only). Every test skips when it isn't installed. The real-catalog
test also needs $CLAUDEWORK_ASSETS and skips without it.

Run from the repo root:  python -m unittest discover -s skills/movie-research/tests -v
"""

import glob
import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMAS = os.path.join(HERE, "..", "..", "..", "schemas")

try:
    import jsonschema
    from referencing import Registry, Resource
except ImportError:
    jsonschema = None

SHA = "0123456789abcdef0123456789abcdef01234567"


def validators():
    """Build validators for both schemas, with the cross-file $ref resolved.

    Returns: (findings_validator, catalog_validator).
    Depends on: jsonschema + referencing, the two schema files in schemas/.
    """
    schemas = {}
    for name in ("findings-1.schema.json", "catalog-1.schema.json"):
        with open(os.path.join(SCHEMAS, name), encoding="utf-8") as f:
            schemas[name] = json.load(f)
    registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas.values())
    return tuple(jsonschema.Draft202012Validator(schemas[n], registry=registry)
                 for n in ("findings-1.schema.json", "catalog-1.schema.json"))


def finding(*evidence):
    """A minimal valid findings/1 line carrying the given evidence entries.

    Returns: a dict; nothing in it refers to a real game.
    """
    return {"format": "findings/1", "id": "test.flag", "kind": "flag", "address": "0x0010", "domain": "WRAM",
            "bank": None, "width": 1, "bits": "0x04", "rom_sha1": SHA, "confidence": "likely",
            "evidence": list(evidence)}


CAPTURE = {"capture": "pass1", "frames": [10, 20], "writers": [{"pc": "0x4000", "bank": None}],
           "method": "write watch", "note": "n"}
STATIC = {"source": "rom", "method": "static: disassembly of the caller", "at": "04:4BFF"}


@unittest.skipIf(jsonschema is None, "jsonschema not installed")
class EvidenceShapes(unittest.TestCase):
    """findings/1 evidence: exactly one of a capture entry or a static entry (ADR 0003, 2026-10-26)."""

    @classmethod
    def setUpClass(cls):
        cls.findings, cls.catalog = validators()

    def valid(self, doc):
        return self.findings.is_valid(doc)

    def test_capture_entry_passes(self):
        self.assertTrue(self.valid(finding(CAPTURE)))

    def test_static_entry_passes_with_and_without_optional_keys(self):
        self.assertTrue(self.valid(finding(STATIC)))
        self.assertTrue(self.valid(finding({"source": "rom", "method": "static: table read"})))
        self.assertTrue(self.valid(finding(dict(STATIC, rom_sha1=SHA, note="other ROM"))))

    def test_both_shapes_in_one_finding_pass(self):
        self.assertTrue(self.valid(finding(CAPTURE, STATIC)))

    def test_mixed_entry_fails(self):
        # The old placeholder: a capture entry pretending to be a ROM read.
        self.assertFalse(self.valid(finding(dict(STATIC, capture="rom", frames=[0, 0]))))
        self.assertFalse(self.valid(finding(dict(CAPTURE, source="rom"))))

    def test_misspelled_or_unknown_keys_fail(self):
        self.assertFalse(self.valid(finding(dict(CAPTURE, writer=[]))))
        self.assertFalse(self.valid(finding({"source": "rom", "method": "static: x", "nots": "typo"})))

    def test_static_entry_rules(self):
        self.assertFalse(self.valid(finding(dict(STATIC, method="disassembly"))), "method must start 'static: '")
        self.assertFalse(self.valid(finding(dict(STATIC, source="ram"))))
        self.assertFalse(self.valid(finding(dict(STATIC, at="4BFF"))), "at is BB:AAAA")
        self.assertFalse(self.valid(finding({"method": "static: x"})), "source is required")

    def test_catalog_transition_accepts_static_evidence(self):
        doors = {"format": "catalog/1", "capture": "c", "transitions": [
            {"id": "t1", "from": "a", "to": "b", "kind": "door", "exit": "walk", "entry": "stand",
             "exit_door": "", "entry_door": "", "requires": [], "requires_back": None, "leave_requires": None,
             "frames": [5], "evidence": [dict(STATIC, rom_sha1=SHA)]}]}
        errors = [e.message for e in self.catalog.iter_errors(doors)]
        self.assertEqual(errors, [])


@unittest.skipIf(jsonschema is None, "jsonschema not installed")
class RealCatalogs(unittest.TestCase):
    """Catalogs already written in the asset store must stay valid (the format string stays catalog/1)."""

    def test_existing_catalogs_still_validate(self):
        assets = os.environ.get("CLAUDEWORK_ASSETS")
        if not assets:
            self.skipTest("CLAUDEWORK_ASSETS not set")
        paths = [p for name in ("rooms.json", "doors.json", "items.json")
                 for p in glob.glob(os.path.join(assets, "movies", "exports", "*", name))]
        if not paths:
            self.skipTest("no catalogs in the asset store")
        _, catalog = validators()
        for path in paths:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            if doc.get("format") != "catalog/1":
                continue
            with self.subTest(path=os.path.relpath(path, assets)):
                self.assertEqual([e.message for e in catalog.iter_errors(doc)][:3], [])


if __name__ == "__main__":
    unittest.main()
