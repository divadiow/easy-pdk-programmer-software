# Copyright (C) 2026 divadiow
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from contextlib import redirect_stderr
import hashlib
import importlib.util
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ny8_easypdk", ROOT / "ny8-easypdk.py")
assert SPEC is not None and SPEC.loader is not None
ny8 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ny8)


def encode_program_words(words: list[int]) -> bytes:
    encoded = bytearray()
    for word in words:
        inverted = (~word) & 0x3FFF
        encoded.extend(((inverted >> 7) & 0x7F, inverted & 0x7F))
    return bytes(encoded)


def encode_info_words(words: list[int]) -> bytes:
    encoded = bytearray(b"\x00\x60\x00\x00")
    for word in words:
        raw16 = ((word & 0x3F80) << 2) | ((word & 0x007F) << 1)
        encoded.extend(raw16.to_bytes(2, "big"))
    return bytes(encoded)


def safe_status(**overrides: int) -> dict[str, int]:
    status = {
        "result": 16,
        "hw_variant": 2,
        "target_vdd_mv": 3300,
        "active_vdd_mv": 3300,
        "active_vpp_mv": 0,
        "off_vdd_mv": 0,
        "off_vpp_mv": 0,
        "capture_count": ny8.NY8_DUMP_RX_BYTES,
    }
    status.update(overrides)
    return status


class DecodeTests(unittest.TestCase):
    def test_program_round_trip_and_range_checksum(self) -> None:
        words = [((index * 37) + 0x123) & 0x3FFF for index in range(2048)]
        self.assertEqual(ny8.decode_program_words(encode_program_words(words)), words)
        self.assertEqual(ny8.qwriter_program_range_checksum(words), 0x007FF738)

    def test_program_decode_rejects_bad_length_and_high_bits(self) -> None:
        with self.assertRaises(ny8.ProtocolError):
            ny8.decode_program_words(b"\x00" * (ny8.NY8_DUMP_DATA_BYTES - 1))
        invalid = bytearray(ny8.NY8_DUMP_DATA_BYTES)
        invalid[17] = 0x80
        with self.assertRaises(ny8.ProtocolError):
            ny8.decode_program_words(bytes(invalid))

    def test_information_decode_and_fields(self) -> None:
        words = [0] * ny8.NY8_INFO_WORDS

        def put(address: int, value: int) -> None:
            words[address - ny8.NY8_INFO_START_ADDRESS] = value

        put(0x05, 0x0070)
        put(0x08, 0x002A)
        put(0x09, 0x0094)
        put(0x0D, 0x2AD5)
        put(0x0E, 0x00A5)
        put(0x0F, 0x08AB)
        for address in (0x10, 0x12, 0x14):
            put(address, 0x0172)
        for address in (0x11, 0x13, 0x15):
            put(address, 0x00F5)

        command, raw_words, decoded = ny8.decode_info_pass(encode_info_words(words))
        self.assertEqual(command, b"\x00\x60\x00\x00")
        self.assertEqual(len(raw_words), ny8.NY8_INFO_WORDS)
        self.assertEqual(decoded, words)
        fields = ny8.decode_qwriter_information(decoded)
        expected = {
            "status_byte": 0x94,
            "checksum_present": False,
            "enforce_program": False,
            "rolling_code": True,
            "release_mode": True,
            "qwriter_version_raw": 1,
            "lvr_trim": 7,
            "lvd_ldo_trim": 0x2A,
            "ihrc_trim": 0x55,
            "ihrc_trim_reserve": 0x15,
            "hardware_trim": 2,
            "ilrc_trim": 0xA5,
            "read_protect_raw": 1,
            "over_write_raw": 1,
            "cp_pass_raw": 1,
            "virtual_body": 0xA,
            "cp_sram_raw": 1,
            "identity_block_plausible": True,
            "id_redundancy_matches": True,
        }
        for key, value in expected.items():
            self.assertEqual(fields[key], value, key)
        self.assertEqual(ny8.qwriter_chip_id(words[0x10 - 0x05]), 0x0B12)
        self.assertEqual(ny8.qwriter_chip_id(words[0x11 - 0x05]), 0x0715)

        words[0x14 - 0x05] ^= 1
        self.assertFalse(ny8.decode_qwriter_information(words)["id_redundancy_matches"])

    def test_information_decode_rejects_bad_length(self) -> None:
        with self.assertRaises(ny8.ProtocolError):
            ny8.decode_info_pass(b"\x00" * (ny8.NY8_INFO_PASS_RX_BYTES - 1))

    def test_stored_checksum_decode(self) -> None:
        words = [0] * ny8.NY8_DUMP_WORDS
        words[-2:] = [0x1234, 0x2345]
        self.assertEqual(ny8.qwriter_stored_code_checksum(words), 0x8D2345)


class SafetyGateTests(unittest.TestCase):
    def test_validated_program_prefix_gate(self) -> None:
        self.assertEqual(
            ny8.VALIDATED_PROGRAM_PREFIXES_SHA256,
            {
                "69C2F39AB27A6AC8CDBE072E78CCF9786E0495707A7EBC939E6104A047034AA9":
                    "first-tested TH02Pro program-prefix profile",
                "671D90D7B482ED49F1E73996E67D2EB538F778652DA8B0F96002604ADB3B0512":
                    "TH03Pro Forever Young program-prefix profile",
                "6311F61E835FF2508A29F46139F1D7DB85A95ACEAFA62A79C8335B6D5C7A0608":
                    "S09 temperature/humidity device program-prefix profile",
            },
        )
        synthetic_capture = bytes(range(ny8.VALIDATED_PROGRAM_PREFIX_BYTES))
        synthetic_digest = hashlib.sha256(synthetic_capture).hexdigest().upper()
        with mock.patch.dict(
            ny8.VALIDATED_PROGRAM_PREFIXES_SHA256,
            {synthetic_digest: "synthetic specimen"},
            clear=True,
        ):
            digest, specimen = ny8.require_validated_program_prefix(
                synthetic_capture + b"first suffix"
            )
            self.assertEqual(digest, synthetic_digest)
            self.assertEqual(specimen, "synthetic specimen")
            repeated_digest, _ = ny8.require_validated_program_prefix(
                synthetic_capture + b"different suffix"
            )
            self.assertEqual(repeated_digest, synthetic_digest)
            with self.assertRaises(ny8.ProtocolError):
                ny8.require_validated_program_prefix(
                    b"x" * ny8.VALIDATED_PROGRAM_PREFIX_BYTES
                )
        with self.assertRaises(ny8.ProtocolError):
            ny8.require_validated_program_prefix(b"short")

    def test_status_parser_and_safe_boundaries(self) -> None:
        packed = struct.pack("<8I", 16, 2, 3300, 2800, 250, 500, 500, 4100)
        status = ny8.parse_status(packed)
        with redirect_stderr(io.StringIO()):
            self.assertTrue(ny8.dump_status_is_safe(status))
            self.assertFalse(
                ny8.dump_status_is_safe(safe_status(active_vpp_mv=251))
            )
            self.assertFalse(ny8.dump_status_is_safe(safe_status(hw_variant=1)))
        with self.assertRaises(ny8.ProtocolError):
            ny8.parse_status(packed[:-1])

    def test_information_status_accepts_only_complete_safe_capture(self) -> None:
        with redirect_stderr(io.StringIO()):
            self.assertTrue(
                ny8.info_status_has_safe_capture(
                    safe_status(result=17, capture_count=ny8.NY8_INFO_RX_BYTES)
                )
            )
            self.assertTrue(
                ny8.info_status_has_safe_capture(
                    safe_status(result=18, capture_count=ny8.NY8_INFO_RX_BYTES)
                )
            )
            self.assertFalse(
                ny8.info_status_has_safe_capture(
                    safe_status(result=17, capture_count=75)
                )
            )

    def test_firmware_marker_and_lite_gate(self) -> None:
        ny8.require_experimental_lite("NY8EXP7.1 HWVAR:LITE")
        with self.assertRaises(ny8.ProtocolError):
            ny8.require_experimental_lite("NY8EXP7.1 HWVAR:MINI_PILL")
        with self.assertRaises(ny8.ProtocolError):
            ny8.require_experimental_lite("FREE-PDK HWVAR:LITE")

    def test_required_command_line_confirmations(self) -> None:
        parser = ny8.build_parser()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["dump2048", "--output-prefix", "capture"])
        args = parser.parse_args(
            [
                "dump2048",
                "--confirm-pin8-isolated",
                "--confirm-one-full-read",
                "--output-prefix",
                "capture",
            ]
        )
        self.assertEqual(args.action, "dump2048")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "readid",
                    "--confirm-pin8-isolated",
                    "--confirm-id-read",
                    "--repeats",
                    "11",
                    "--output-prefix",
                    "capture",
                ]
            )

    def test_capture_retrieval_chunk_boundaries(self) -> None:
        calls = []

        def fake_command(_port: object, command: str, payload: bytes = b"") -> bytes:
            self.assertEqual(command, "G")
            offset, length = struct.unpack("<HH", payload)
            calls.append((offset, length))
            return bytes([offset & 0xFF]) * length

        with mock.patch.object(ny8, "command", side_effect=fake_command):
            result = ny8.retrieve_capture_pass(object(), 4100, 1)
        self.assertEqual(len(result), 4100)
        self.assertEqual(calls, [(offset, min(60, 4100 - offset)) for offset in range(0, 4100, 60)])


class OutputTests(unittest.TestCase):
    def test_output_suffixes_and_preexisting_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "capture"
            raw_path, rom_path = ny8.dump_output_paths(str(prefix))
            self.assertEqual(raw_path.name, "capture.ny8-rx4100.bin")
            self.assertEqual(rom_path.name, "capture.ny8-rom14be.bin")
            raw_path.write_bytes(b"existing")
            with self.assertRaises(ny8.ProtocolError):
                ny8.dump_output_paths(str(prefix))

    def test_publish_is_no_clobber_and_rolls_back_first_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_path = root / "raw.bin"
            rom_path = root / "rom.bin"
            ny8.write_dump_files(raw_path, b"raw", rom_path, b"rom")
            self.assertEqual(raw_path.read_bytes(), b"raw")
            self.assertEqual(rom_path.read_bytes(), b"rom")

            raw_path.unlink()
            rom_path.write_bytes(b"keep")
            with self.assertRaises(ny8.ProtocolError):
                ny8.write_dump_files(raw_path, b"new raw", rom_path, b"new rom")
            self.assertFalse(raw_path.exists())
            self.assertEqual(rom_path.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
