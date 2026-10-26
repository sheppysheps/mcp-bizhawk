"""Unit tests for save_file.py. Standard library only.

Run from the repo root:  python -m unittest discover -s skills/movie-research/tests -v
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import save_file  # noqa: E402


def make_save(data, formula="sum16-not-le", size=32):
    """A fake cart RAM: data, its checksum right after, FF padding. Returns: bytes."""
    cs = save_file.checksum_bytes(formula, data)
    return (data + cs).ljust(size, b"\xFF")


class SaveFile(unittest.TestCase):
    def test_identify_finds_the_formula(self):
        save = make_save(bytes([1, 0, 2, 0x10, 0x25, 0x7F, 0x80, 0xC3]))
        self.assertIn("sum16-not-le", save_file.identify(save, 0, 8, 8))
        self.assertNotIn("sum16-be", save_file.identify(save, 0, 8, 8))

    def test_patch_edits_and_recomputes(self):
        save = make_save(bytes(8))
        out = save_file.patch(save, 0, 8, 8, "sum16-not-le", sets=[(1, 0x22)], set_bits=[(3, 7)],
                              clear_bits=[(1, 1)])
        self.assertEqual(out[:8], bytes([0, 0x20, 0, 0x80, 0, 0, 0, 0]))
        # The 16-bit formula comes first; its low byte also matches the 8-bit one.
        self.assertEqual(save_file.identify(out, 0, 8, 8)[0], "sum16-not-le")
        self.assertEqual(out[10:], save[10:])  # padding untouched
        with self.assertRaises(ValueError):
            save_file.patch(save, 0, 8, 8, "sum16-not-le", sets=[(9, 1)])

    def test_cli_refuses_to_overwrite_the_input(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "a.sav")
            with open(src, "wb") as f:
                f.write(make_save(bytes(8)))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = save_file.main(["patch", src, src, "--data", "0", "8", "--at", "8",
                                       "--checksum", "sum16-not-le", "--set", "0=1"])
            self.assertEqual(code, 1)
            self.assertIn("refusing", out.getvalue())
            dst = os.path.join(d, "b.sav")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(save_file.main(["patch", src, dst, "--data", "0", "8", "--at", "8",
                                                 "--checksum", "sum16-not-le", "--set-bit", "0x2.3"]), 0)
            with open(dst, "rb") as f:
                self.assertIn("sum16-not-le", save_file.identify(f.read(), 0, 8, 8))


if __name__ == "__main__":
    unittest.main()
