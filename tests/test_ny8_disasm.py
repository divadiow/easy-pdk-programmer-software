# Copyright (C) 2026 divadiow
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ny8_disasm", ROOT / "tools" / "ny8_disasm.py"
)
assert SPEC is not None and SPEC.loader is not None
disasm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(disasm)


class DecodeTests(unittest.TestCase):
    def test_representative_instruction_families(self) -> None:
        cases = (
            (0x000, 0x0000, "NOP"),
            (0x000, 0x0085, "MOVAR PORTA"),
            (0x000, 0x0105, "MOVR PORTA,ToAcc"),
            (0x000, 0x0385, "ADDAR PORTA,ToReg"),
            (0x000, 0x0986, "BTRSS PORTB,bit 3"),
            (0x000, 0x21A5, "MOVIA 0xA5"),
            (0x2FF, 0x315A, "CALL 0x35A"),
            (0x3FF, 0x3355, "GOTO 0x555"),
            (0x000, 0x2ABC, "LCALL 0x2BC"),
            (0x000, 0x3ABC, "LGOTO 0x2BC"),
            (0x000, 0x4000, ".word (outside 14-bit range)"),
        )
        for address, word, expected in cases:
            with self.subTest(word=word):
                self.assertEqual(disasm.decode(address, word), expected)

    def test_stored_checksum(self) -> None:
        self.assertEqual(disasm.stored_code_checksum((0x1234, 0x2345)), 0x8D2345)


class CommandLineTests(unittest.TestCase):
    def run_main(self, arguments: list[str]) -> str:
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["ny8_disasm.py", *arguments]):
            with redirect_stdout(output):
                disasm.main()
        return output.getvalue()

    def test_full_image_marks_checksum_words_as_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rom = Path(directory) / "synthetic.bin"
            words = [0] * disasm.NY8A054E_ROM_WORDS
            words[-2:] = [0x1234, 0x2345]
            rom.write_bytes(struct.pack(f">{len(words)}H", *words))
            text = self.run_main(
                [str(rom), "--start", "0x7fd", "--end", "0x800"]
            )
        self.assertIn("7FD: 0000  NOP", text)
        self.assertIn("7FE: 1234  .metadata Q-Writer stored-checksum source 1/2", text)
        self.assertIn("7FF: 2345  .metadata Q-Writer stored-checksum source 2/2", text)
        self.assertIn("packed24=0x8D2345", text)

    def test_partial_image_treats_final_words_as_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rom = Path(directory) / "partial.bin"
            rom.write_bytes(struct.pack(">2H", 0x0000, 0x0001))
            text = self.run_main([str(rom)])
        self.assertEqual(text, "000: 0000  NOP\n001: 0001  SLEEP\n")

    def test_odd_input_and_invalid_range_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rom = Path(directory) / "odd.bin"
            rom.write_bytes(b"\x00")
            with self.assertRaisesRegex(SystemExit, "even number"):
                self.run_main([str(rom)])
            rom.write_bytes(b"\x00\x00")
            with self.assertRaisesRegex(SystemExit, "invalid start/end"):
                self.run_main([str(rom), "--start", "2"])


if __name__ == "__main__":
    unittest.main()
