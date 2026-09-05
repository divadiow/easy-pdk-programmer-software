#!/usr/bin/env python3
# Copyright (C) 2026 divadiow
# SPDX-License-Identifier: GPL-3.0-or-later

"""Safe Windows host utility for EasyPDK NY8EXP7/EXP7.1.

Launching without a subcommand does nothing. Target operations are limited to a
fixed 2048-word program read and a fixed two-pass configuration-information read.
Both require explicit physical/scope confirmations and never request nonzero VPP.
Completed target-off RAM is retrieved twice before files are accepted.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import struct
import sys
import tempfile
import time

import serial
from serial.tools import list_ports


EASYPDK_VID = 0x0483
EASYPDK_PID = 0x5740
NY8_MARKER = "NY8EXP7"
NY8_DUMP_WORDS = 2048
NY8_DUMP_COMMAND_RX_BYTES = 4
NY8_DUMP_DATA_BYTES = 2 * NY8_DUMP_WORDS
NY8_DUMP_RX_BYTES = NY8_DUMP_COMMAND_RX_BYTES + NY8_DUMP_DATA_BYTES
NY8_DUMP_USB_CHUNK_MAX = 60
NY8_INFO_START_ADDRESS = 0x0005
NY8_INFO_END_ADDRESS = 0x0015
NY8_INFO_WORDS = NY8_INFO_END_ADDRESS - NY8_INFO_START_ADDRESS + 1
NY8_INFO_PASSES = 2
NY8_INFO_COMMAND_RX_BYTES = 4
NY8_INFO_DATA_BYTES = 2 * NY8_INFO_WORDS
NY8_INFO_PASS_RX_BYTES = NY8_INFO_COMMAND_RX_BYTES + NY8_INFO_DATA_BYTES
NY8_INFO_RX_BYTES = NY8_INFO_PASSES * NY8_INFO_PASS_RX_BYTES
NY8_INFO_DEFAULT_REPEATS = 6
NY8_INFO_REQUIRED_ADDRESSES = (0x05, 0x0F, 0x10, 0x11, 0x15)
OFF_RAIL_MAX_MV = 500
IDLE_SETTLE_SECONDS = 0.25
VALIDATED_PROGRAM_PREFIX_BYTES = 36
# SHA-256 of the raw command response plus the first 16 program words.
VALIDATED_PROGRAM_PREFIXES_SHA256 = {
    "69C2F39AB27A6AC8CDBE072E78CCF9786E0495707A7EBC939E6104A047034AA9":
        "first-tested TH02Pro program-prefix profile",
    "671D90D7B482ED49F1E73996E67D2EB538F778652DA8B0F96002604ADB3B0512":
        "TH03Pro Forever Young program-prefix profile",
    "6311F61E835FF2508A29F46139F1D7DB85A95ACEAFA62A79C8335B6D5C7A0608":
        "S09 temperature/humidity device program-prefix profile",
    "ED93A3FF6E47B468409C5AE5F6F2E8FBDD1202B90AC809248FE3B1B4EE3ABD28":
        "P01 Forever Young SOP8 program-prefix profile",
}

QWRITER_ID_DATABASE = {
    (0x0F00, 0x000C): "NY8A054D revision A",
    (0x0F00, 0x0014): "NY8A054E revision A",
    (0x0F01, 0x0014): "NY8A054E revision B",
    (0x0F02, 0x0014): "NY8A054E revision C",
    (0x0F00, 0x001E): "NY8A054E1 revision A",
    (0x0F01, 0x001E): "NY8A054E1 revision B",
    (0x0F03, 0x001E): "NY8A054E1 revision C",
}

RESULT_NAMES = {
    0: "exact 53 AD observation: SDO high",
    1: "exact 53 AD observation: SDO low",
    2: "refused: programmer is not Lite",
    3: "refused: VPP was not off",
    4: "VDD outside compiled safe range",
    5: "firmware buffer too small",
    6: "aborted/disconnected",
    7: "rails did not power off",
    8: "internal error",
    9: "ADC measurement timeout",
    16: "fixed 2048-word read completed and target powered off",
    17: "two-pass configuration-information read completed and target powered off",
    18: "Q-Writer identity-check words disagreed and target powered off",
}

HW_VARIANTS = {
    0: "UNKNOWN",
    1: "MINI_PILL",
    2: "LITE",
}


class ProtocolError(RuntimeError):
    pass


def exact_read(port: serial.Serial, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = port.read(size - len(data))
        if not chunk:
            raise ProtocolError(
                f"serial timeout after {len(data)} of {size} expected bytes"
            )
        data.extend(chunk)
    return bytes(data)


def command(port: serial.Serial, cmd: str, payload: bytes = b"") -> bytes:
    if len(cmd) != 1 or len(payload) > 0xFF:
        raise ValueError("invalid EasyPDK command frame")

    frame = cmd.encode("ascii") + bytes((len(payload),)) + payload
    if port.write(frame) != len(frame):
        raise ProtocolError(f"short serial write for command {cmd!r}")
    port.flush()

    while True:
        header = exact_read(port, 3)
        response_type = chr(header[0])
        response_len = header[1] | (header[2] << 8)
        response = exact_read(port, response_len)

        if response_type == "D":
            print(f"DEBUG: {response!r}", file=sys.stderr)
            continue
        if response_type == "E":
            raise ProtocolError(f"firmware rejected command {cmd!r}")
        if response_type != "A":
            raise ProtocolError(f"unexpected response type {response_type!r}")
        return response


def read_version(port: serial.Serial) -> str:
    return command(port, "I").decode("ascii", errors="replace").strip()


def set_outputs_zero(port: serial.Serial) -> None:
    command(port, "O", struct.pack("<II", 0, 0))


def read_voltages(port: serial.Serial) -> tuple[int, int, int]:
    response = command(port, "U")
    if len(response) != 12:
        raise ProtocolError(f"GETVOLTAGES returned {len(response)} bytes, expected 12")
    return struct.unpack("<III", response)


def print_voltages(label: str, values: tuple[int, int, int]) -> None:
    vref, vdd, vpp = values
    print(f"{label}: Vref={vref} mV, VDD={vdd} mV, VPP={vpp} mV")


def require_outputs_off(values: tuple[int, int, int], label: str) -> None:
    _, vdd, vpp = values
    if vdd > OFF_RAIL_MAX_MV or vpp > OFF_RAIL_MAX_MV:
        raise ProtocolError(
            f"{label}: refusing because VDD={vdd} mV or VPP={vpp} mV exceeds "
            f"the {OFF_RAIL_MAX_MV} mV idle limit"
        )


def parse_status(response: bytes) -> dict[str, int]:
    if len(response) != 32:
        raise ProtocolError(f"NY8 status returned {len(response)} bytes, expected 32")
    fields = struct.unpack("<8I", response)
    keys = (
        "result",
        "hw_variant",
        "target_vdd_mv",
        "active_vdd_mv",
        "active_vpp_mv",
        "off_vdd_mv",
        "off_vpp_mv",
        "capture_count",
    )
    return dict(zip(keys, fields))


def decode_program_words(raw_bytes: bytes) -> list[int]:
    if len(raw_bytes) != NY8_DUMP_DATA_BYTES:
        raise ProtocolError(
            f"ROM stream has {len(raw_bytes)} bytes, expected {NY8_DUMP_DATA_BYTES}"
        )
    if any(value & 0x80 for value in raw_bytes):
        raise ProtocolError("ROM stream contains bytes outside the expected 7-bit range")

    words = []
    for offset in range(0, len(raw_bytes), 2):
        b1, b2 = raw_bytes[offset : offset + 2]
        encoded = (b1 << 7) | b2
        words.append((~encoded) & 0x3FFF)
    return words


def decode_info_pass(raw_pass: bytes) -> tuple[bytes, list[int], list[int]]:
    if len(raw_pass) != NY8_INFO_PASS_RX_BYTES:
        raise ProtocolError(
            f"information pass has {len(raw_pass)} bytes, "
            f"expected {NY8_INFO_PASS_RX_BYTES}"
        )

    command_rx = raw_pass[:NY8_INFO_COMMAND_RX_BYTES]
    raw_words = []
    writer_words = []
    data = raw_pass[NY8_INFO_COMMAND_RX_BYTES:]
    for offset in range(0, len(data), 2):
        raw16 = int.from_bytes(data[offset : offset + 2], "big")
        raw_words.append(raw16)
        writer_words.append(((raw16 >> 2) & 0x3F80) | ((raw16 >> 1) & 0x007F))
    return command_rx, raw_words, writer_words


def print_status(status: dict[str, int]) -> None:
    result = status["result"]
    print(f"NY8 result: {result} - {RESULT_NAMES.get(result, 'unknown')}")
    print(
        "Hardware variant: "
        f"{status['hw_variant']} - "
        f"{HW_VARIANTS.get(status['hw_variant'], 'unknown')}"
    )
    print(
        f"Rails active: target VDD={status['target_vdd_mv']} mV, "
        f"measured VDD={status['active_vdd_mv']} mV, "
        f"measured VPP={status['active_vpp_mv']} mV"
    )
    print(
        f"Rails after cleanup: VDD={status['off_vdd_mv']} mV, "
        f"VPP={status['off_vpp_mv']} mV"
    )
    print(f"Raw transfer bytes captured: {status['capture_count']}")


def require_experimental_lite(version: str) -> None:
    if NY8_MARKER not in version:
        raise ProtocolError(
            f"refusing target operation: GETVERINFO lacks {NY8_MARKER!r}"
        )
    if "HWVAR:LITE" not in version:
        raise ProtocolError("refusing target operation: firmware did not detect Lite hardware")


def open_port(name: str) -> tuple[serial.Serial, str]:
    port = serial.Serial(
        port=name,
        baudrate=115200,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=5.0,
        write_timeout=5.0,
    )
    try:
        port.dtr = True
        time.sleep(0.20)
        port.reset_input_buffer()
        version = read_version(port)
        return port, version
    except Exception:
        port.close()
        raise


def find_and_open(requested_port: str | None) -> tuple[serial.Serial, str]:
    if requested_port:
        port, version = open_port(requested_port)
        print(f"Port: {requested_port}")
        return port, version

    candidates = [
        item
        for item in list_ports.comports()
        if item.vid == EASYPDK_VID and item.pid == EASYPDK_PID
    ]
    if not candidates:
        raise ProtocolError("no 0483:5740 EasyPDK CDC port found")

    errors = []
    for item in candidates:
        try:
            port, version = open_port(item.device)
            if "FREE-PDK EASY PROG" not in version:
                port.close()
                errors.append(f"{item.device}: unexpected GETVERINFO {version!r}")
                continue
            print(f"Port: {item.device} ({item.description})")
            return port, version
        except Exception as exc:
            errors.append(f"{item.device}: {exc}")

    raise ProtocolError("no responding EasyPDK port: " + "; ".join(errors))


def do_info(port: serial.Serial) -> None:
    button = command(port, "B")
    if len(button) != 1:
        raise ProtocolError(f"GETBUTTON returned {len(button)} bytes, expected 1")
    command(port, "L", b"\x00")
    print(f"GETBUTTON: {'pressed' if button[0] else 'released'}")
    print("SETLED off: ACK")


def common_status_errors(status: dict[str, int]) -> list[str]:
    errors = []
    if status["hw_variant"] != 2:
        errors.append("hardware variant is not LITE")
    if status["target_vdd_mv"] != 3300:
        errors.append("compiled target VDD is not 3300 mV")
    if not 2800 <= status["active_vdd_mv"] <= 3800:
        errors.append("active VDD is outside 2800..3800 mV")
    if status["active_vpp_mv"] > 250:
        errors.append("active VPP exceeds 250 mV")
    if status["off_vdd_mv"] > OFF_RAIL_MAX_MV:
        errors.append("cleanup VDD exceeds the idle limit")
    if status["off_vpp_mv"] > OFF_RAIL_MAX_MV:
        errors.append("cleanup VPP exceeds the idle limit")
    return errors


def dump_status_is_safe(status: dict[str, int]) -> bool:
    errors = common_status_errors(status)
    if status["result"] != 16:
        errors.append("completion result is not 16")
    if status["capture_count"] != NY8_DUMP_RX_BYTES:
        errors.append(
            f"capture count is {status['capture_count']}, expected {NY8_DUMP_RX_BYTES}"
        )
    for error in errors:
        print(f"Dump rejected: {error}.", file=sys.stderr)
    return not errors


def info_status_has_safe_capture(status: dict[str, int]) -> bool:
    errors = common_status_errors(status)
    if status["result"] not in (17, 18):
        errors.append("result is neither a complete nor a mismatched information capture")
    if status["capture_count"] != NY8_INFO_RX_BYTES:
        errors.append(
            f"capture count is {status['capture_count']}, expected {NY8_INFO_RX_BYTES}"
        )
    for error in errors:
        print(f"Information capture rejected: {error}.", file=sys.stderr)
    return not errors


def retrieve_capture_pass(
    port: serial.Serial, capture_bytes: int, pass_number: int
) -> bytes:
    raw_rx = bytearray()
    for offset in range(0, capture_bytes, NY8_DUMP_USB_CHUNK_MAX):
        length = min(NY8_DUMP_USB_CHUNK_MAX, capture_bytes - offset)
        chunk = command(port, "G", struct.pack("<HH", offset, length))
        if len(chunk) != length:
            raise ProtocolError(
                f"RAM retrieval pass {pass_number} offset {offset} returned "
                f"{len(chunk)} bytes, expected {length}"
            )
        raw_rx.extend(chunk)
    if len(raw_rx) != capture_bytes:
        raise ProtocolError(
            f"RAM retrieval pass {pass_number} assembled {len(raw_rx)} bytes, "
            f"expected {capture_bytes}"
        )
    print(f"Target-off RAM retrieval pass {pass_number}: {len(raw_rx)} bytes")
    return bytes(raw_rx)


def dump_output_paths(prefix: str) -> tuple[Path, Path]:
    base = Path(prefix).expanduser().resolve()
    raw_path = base.with_name(base.name + ".ny8-rx4100.bin")
    rom_path = base.with_name(base.name + ".ny8-rom14be.bin")
    if not raw_path.parent.is_dir():
        raise ProtocolError(f"output directory does not exist: {raw_path.parent}")
    for path in (raw_path, rom_path):
        if path.exists():
            raise ProtocolError(f"refusing to overwrite existing output: {path}")
    return raw_path, rom_path


def info_output_paths(prefix: str) -> tuple[Path, Path]:
    base = Path(prefix).expanduser().resolve()
    raw_path = base.with_name(base.name + ".ny8-info-rx76.bin")
    decoded_path = base.with_name(base.name + ".ny8-info14be.bin")
    if not raw_path.parent.is_dir():
        raise ProtocolError(f"output directory does not exist: {raw_path.parent}")
    for path in (raw_path, decoded_path):
        if path.exists():
            raise ProtocolError(f"refusing to overwrite existing output: {path}")
    return raw_path, decoded_path


def stage_temp_file(path: Path, data: bytes) -> Path:
    handle, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return temporary_path
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def write_dump_files(
    raw_path: Path, raw_rx: bytes, rom_path: Path, rom_data: bytes
) -> None:
    raw_temp = stage_temp_file(raw_path, raw_rx)
    rom_temp = None
    published_paths = []
    try:
        rom_temp = stage_temp_file(rom_path, rom_data)
        for temporary_path, final_path in (
            (raw_temp, raw_path),
            (rom_temp, rom_path),
        ):
            try:
                os.link(temporary_path, final_path)
            except FileExistsError as exc:
                raise ProtocolError(
                    f"refusing to overwrite existing output: {final_path}"
                ) from exc
            published_paths.append(final_path)
    except Exception:
        for path in reversed(published_paths):
            path.unlink(missing_ok=True)
        raise
    finally:
        raw_temp.unlink(missing_ok=True)
        if rom_temp is not None:
            rom_temp.unlink(missing_ok=True)


def do_dump2048(port: serial.Serial, raw_path: Path, rom_path: Path) -> bool:
    print("Starting the one permitted fixed target read (no automatic retry).")
    status = parse_status(command(port, "F"))
    print_status(status)
    if not dump_status_is_safe(status):
        print(
            "RAM retrieval suppressed because the target transaction was not safe and complete.",
            file=sys.stderr,
        )
        return False

    idle_after_capture = read_voltages(port)
    print_voltages("Idle before RAM retrieval", idle_after_capture)
    require_outputs_off(idle_after_capture, "Post-capture rail check")

    first = retrieve_capture_pass(port, NY8_DUMP_RX_BYTES, 1)
    second = retrieve_capture_pass(port, NY8_DUMP_RX_BYTES, 2)
    if first != second:
        raise ProtocolError("the two target-off RAM retrieval passes differ")
    print("Target-off RAM retrieval comparison: PASS")

    _, profile = require_validated_program_prefix(first)
    print(f"Validated program-prefix fingerprint: PASS ({profile})")

    words = decode_program_words(first[NY8_DUMP_COMMAND_RX_BYTES:])
    if len(words) != NY8_DUMP_WORDS:
        raise ProtocolError(f"decoded {len(words)} words, expected {NY8_DUMP_WORDS}")
    rom_data = b"".join(struct.pack(">H", word) for word in words)

    write_dump_files(raw_path, first, rom_path, rom_data)
    print(f"Raw RX: {raw_path}")
    print(f"  size={len(first)} SHA-256={hashlib.sha256(first).hexdigest().upper()}")
    print(f"Decoded 14-bit ROM, big-endian: {rom_path}")
    print(
        f"  size={len(rom_data)} SHA-256={hashlib.sha256(rom_data).hexdigest().upper()}"
    )
    print("First 16 decoded words: " + " ".join(f"{word:04X}" for word in words[:16]))
    stored_checksum = qwriter_stored_code_checksum(words)
    range_checksum = qwriter_program_range_checksum(words)
    print("Q-Writer-derived ROM metadata:")
    print(f"  Source words 0x7FE/0x7FF: {words[-2]:04X} {words[-1]:04X}")
    print(f"  Stored 24-bit code checksum: 0x{stored_checksum:06X}")
    print("  This is a stored field, not an independently verified ROM checksum.")
    print(f"  Offline program-range checksum: 0x{range_checksum:08X}")
    print("  Algorithm: sum(byte XOR absolute byte address), range 0x0000..0x0FFF.")
    return True


def require_validated_program_prefix(raw_capture: bytes) -> tuple[str, str]:
    if len(raw_capture) < VALIDATED_PROGRAM_PREFIX_BYTES:
        raise ProtocolError(
            "full-read capture is shorter than the validated fingerprint window"
        )
    digest = hashlib.sha256(
        raw_capture[:VALIDATED_PROGRAM_PREFIX_BYTES]
    ).hexdigest().upper()
    profile = VALIDATED_PROGRAM_PREFIXES_SHA256.get(digest)
    if profile is None:
        known_fingerprints = "\n".join(
            f"  {name}: {known_digest}"
            for known_digest, name in VALIDATED_PROGRAM_PREFIXES_SHA256.items()
        )
        raise ProtocolError(
            "full-read prefix did not match a validated program-prefix profile\n"
            f"known SHA-256 fingerprints:\n{known_fingerprints}\n"
            f"actual SHA-256: {digest}"
        )
    return digest, profile


def qwriter_chip_id(info_word: int) -> int:
    return (info_word & 0x001F) | (((info_word >> 5) & 0x001F) << 8)


def qwriter_stored_code_checksum(words: list[int]) -> int:
    if len(words) != NY8_DUMP_WORDS:
        raise ProtocolError(
            f"stored-checksum decode requires {NY8_DUMP_WORDS} program words"
        )
    return (((words[-2] & 0x3FFF) << 14) | (words[-1] & 0x3FFF)) & 0xFFFFFF


def qwriter_program_range_checksum(words: list[int]) -> int:
    if len(words) != NY8_DUMP_WORDS:
        raise ProtocolError(
            f"program-range checksum requires {NY8_DUMP_WORDS} program words"
        )

    qwriter_bytes = bytearray()
    for word in words:
        if word & ~0x3FFF:
            raise ProtocolError("program-range checksum received a non-14-bit word")
        qwriter_bytes.extend((word & 0xFF, ((word >> 8) & 0x3F) | 0xC0))
    return sum(value ^ address for address, value in enumerate(qwriter_bytes)) & 0xFFFFFFFF


def decode_qwriter_information(words: list[int]) -> dict[str, object]:
    if len(words) != NY8_INFO_WORDS:
        raise ProtocolError(
            f"Q-Writer information decode requires {NY8_INFO_WORDS} words"
        )

    def word(address: int) -> int:
        return words[address - NY8_INFO_START_ADDRESS]

    status_byte = word(0x09) & 0x00FF
    identity_block = (word(0x0F), word(0x10), word(0x11))
    id0_sources = (word(0x10), word(0x12), word(0x14))
    id1_sources = (word(0x11), word(0x13), word(0x15))
    return {
        "status_byte": status_byte,
        "checksum_present": not bool(status_byte & 0x04),
        "enforce_program": not bool(status_byte & 0x10),
        "rolling_code": not bool(status_byte & 0x20),
        "release_mode": bool(status_byte & 0x80),
        "qwriter_version_raw": (word(0x09) >> 7) & 0x01,
        "lvr_trim": (word(0x05) >> 4) & 0x07,
        "lvd_ldo_trim": word(0x08) & 0x3F,
        "ihrc_trim": word(0x0D) & 0x7F,
        "ihrc_trim_reserve": (word(0x0D) >> 7) & 0x1F,
        "hardware_trim": (word(0x0D) >> 12) & 0x03,
        "ilrc_trim": word(0x0E) & 0xFF,
        "read_protect_raw": word(0x0F) & 0x01,
        "over_write_raw": (word(0x0F) >> 1) & 0x01,
        "cp_pass_raw": (word(0x0F) >> 3) & 0x01,
        "virtual_body": (word(0x0F) >> 4) & 0x0F,
        "cp_sram_raw": (word(0x0F) >> 11) & 0x01,
        "identity_block_plausible": not (
            all(value == 0x0000 for value in identity_block)
            or all(value == 0x3FFF for value in identity_block)
        ),
        "id0_sources": id0_sources,
        "id1_sources": id1_sources,
        "id_redundancy_matches": len(set(id0_sources)) == 1
        and len(set(id1_sources)) == 1,
    }


def print_qwriter_information_report(words: list[int]) -> None:
    fields = decode_qwriter_information(words)
    print("Q-Writer-derived target report:")
    print(
        "  Family status byte: "
        f"0x{fields['status_byte']:02X} "
        "(configuration byte 0x12 / information word 0x09 low byte)"
    )
    print(
        "  Stored code-checksum field: "
        f"{'present' if fields['checksum_present'] else 'not enabled'}"
    )
    print(
        "  Enforce-program: "
        f"{'enabled' if fields['enforce_program'] else 'disabled'}"
    )
    print(
        "  Rolling code: "
        f"{'enabled' if fields['rolling_code'] else 'disabled'}"
    )
    print(
        "  Release mode: "
        f"{'enabled' if fields['release_mode'] else 'disabled'}"
    )
    print("  Device-specific factory fields (raw codes; no engineering units inferred):")
    print(f"    LVR trim: 0x{fields['lvr_trim']:X}")
    print(f"    LVD/LDO trim: 0x{fields['lvd_ldo_trim']:02X}")
    print(f"    IHRC trim: 0x{fields['ihrc_trim']:02X}")
    print(f"    IHRC trim reserve: 0x{fields['ihrc_trim_reserve']:02X}")
    print(f"    Hardware trim: 0x{fields['hardware_trim']:X}")
    print(f"    ILRC trim: 0x{fields['ilrc_trim']:02X}")
    print(f"    QWITER_VERSION raw bit: {fields['qwriter_version_raw']}")
    print("  Protection fields (raw XML labels; polarity/host mapping is not inferred):")
    print(f"    READ_PROTECT bit: {fields['read_protect_raw']}")
    print(f"    OVER_WRITE bit: {fields['over_write_raw']}")
    print("  CP fields (raw; active polarity is not inferred):")
    print(f"    CP_PASS bit: {fields['cp_pass_raw']}")
    print(f"    VIRTUAL_BODY: 0x{fields['virtual_body']:X}")
    print(f"    CP_SRAM bit: {fields['cp_sram_raw']}")
    print(
        "  Information words 0x0F..0x11 nonblank plausibility: "
        f"{'PASS' if fields['identity_block_plausible'] else 'FAIL'}"
    )
    print(
        "  Repeated ID-source words 0x10/12/14 and 0x11/13/15: "
        f"{'MATCH' if fields['id_redundancy_matches'] else 'DIFFER'}"
    )
    if not fields["rolling_code"]:
        print("  Rolling-code ROM bytes are not interpreted because its enable flag is off.")


def do_readid(
    port: serial.Serial, raw_path: Path, decoded_path: Path, repeats: int
) -> bool:
    accepted_capture = None
    accepted_words = None
    command_responses = []
    unstable_addresses = set()

    for attempt in range(1, repeats + 1):
        print(f"Starting independent information read {attempt} of {repeats}.")
        status = parse_status(command(port, "N"))
        print_status(status)
        if not info_status_has_safe_capture(status):
            print(
                "RAM retrieval suppressed because no safely completed information "
                "capture is available.",
                file=sys.stderr,
            )
            return False

        idle_after_capture = read_voltages(port)
        print_voltages("Idle before RAM retrieval", idle_after_capture)
        require_outputs_off(idle_after_capture, "Post-capture rail check")

        first = retrieve_capture_pass(port, NY8_INFO_RX_BYTES, 1)
        second = retrieve_capture_pass(port, NY8_INFO_RX_BYTES, 2)
        if first != second:
            raise ProtocolError("the two target-off information RAM retrievals differ")

        pass_a = decode_info_pass(first[:NY8_INFO_PASS_RX_BYTES])
        pass_b = decode_info_pass(first[NY8_INFO_PASS_RX_BYTES:])
        command_responses.append((pass_a[0], pass_b[0]))
        pass_unstable = {
            NY8_INFO_START_ADDRESS + index
            for index, (first_value, second_value) in enumerate(zip(pass_a[2], pass_b[2]))
            if first_value != second_value
        }
        required_unstable = pass_unstable.intersection(NY8_INFO_REQUIRED_ADDRESSES)
        if required_unstable:
            addresses = ", ".join(f"0x{address:02X}" for address in sorted(required_unstable))
            raise ProtocolError(f"Q-Writer identity-check words differ at {addresses}")
        unstable_addresses.update(pass_unstable)
        if status["result"] != 17:
            print("Information capture retained, but firmware reported a pass mismatch.")
            return False

        if accepted_words is None:
            accepted_capture = first
            accepted_words = pass_a[2]
        else:
            repeat_unstable = {
                NY8_INFO_START_ADDRESS + index
                for index, (first_value, repeat_value) in enumerate(
                    zip(accepted_words, pass_a[2])
                )
                if first_value != repeat_value
            }
            required_unstable = repeat_unstable.intersection(
                NY8_INFO_REQUIRED_ADDRESSES
            )
            if required_unstable:
                addresses = ", ".join(
                    f"0x{address:02X}" for address in sorted(required_unstable)
                )
                raise ProtocolError(
                    f"target information read {attempt} changed identity-check "
                    f"words at {addresses}"
                )
            unstable_addresses.update(repeat_unstable)

        digest = hashlib.sha256(
            b"".join(struct.pack(">H", value) for value in pass_a[1])
        ).hexdigest().upper()
        print(
            f"Information read {attempt}: Q-Writer identity-word comparison PASS, "
            f"raw-word SHA-256={digest}"
        )

    if accepted_capture is None or accepted_words is None:
        raise ProtocolError("no information capture was accepted")
    print(f"All {repeats} independent reads agree on Q-Writer's identity-check words.")
    if unstable_addresses:
        addresses = ", ".join(
            f"0x{address:02X}" for address in sorted(unstable_addresses)
        )
        print(f"Non-identity diagnostic words varied at: {addresses}")
    else:
        print("All 17 captured information words were stable across every pass and read.")
    print("Captured command-return bytes (pass A / pass B):")
    for attempt, (first_command, second_command) in enumerate(command_responses, 1):
        print(
            f"  read {attempt}: {first_command.hex(' ').upper()} / "
            f"{second_command.hex(' ').upper()}"
        )

    first_pass = decode_info_pass(accepted_capture[:NY8_INFO_PASS_RX_BYTES])
    raw_words = first_pass[1]
    print("Q-Writer-style configuration-information decode:")
    print("  address  raw16  decoded14  inverted-diagnostic")
    for index, (raw16, value) in enumerate(zip(raw_words, accepted_words)):
        address = NY8_INFO_START_ADDRESS + index
        print(f"  0x{address:02X}     {raw16:04X}   {value:04X}       {(~value)&0x3FFF:04X}")

    word10 = accepted_words[0x10 - NY8_INFO_START_ADDRESS]
    word11 = accepted_words[0x11 - NY8_INFO_START_ADDRESS]
    chip_id0 = qwriter_chip_id(word10)
    chip_id1 = qwriter_chip_id(word11)
    database_name = QWRITER_ID_DATABASE.get((chip_id0, chip_id1))
    print(f"Q-Writer chipId0: 0x{chip_id0:04X} (from information word 0x10)")
    print(f"Q-Writer chipId1: 0x{chip_id1:04X} (from information word 0x11)")
    if database_name:
        print(f"Q-Writer database match: {database_name}")
    else:
        print("Q-Writer database match: none among the extracted 054D/054E/054E1 entries")
    print_qwriter_information_report(accepted_words)

    decoded_data = b"".join(struct.pack(">H", word) for word in accepted_words)
    write_dump_files(raw_path, accepted_capture, decoded_path, decoded_data)
    print(f"Raw two-pass information capture: {raw_path}")
    print(
        f"  size={len(accepted_capture)} "
        f"SHA-256={hashlib.sha256(accepted_capture).hexdigest().upper()}"
    )
    print(f"Decoded first pass, big-endian: {decoded_path}")
    print(
        f"  size={len(decoded_data)} "
        f"SHA-256={hashlib.sha256(decoded_data).hexdigest().upper()}"
    )
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="COM port override, for example COM7")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("info", help="GETVERINFO and harmless ordinary comms checks")
    dump_parser = subparsers.add_parser(
        "dump2048", help="perform the one fixed 2048-word read and save verified files"
    )
    dump_parser.add_argument(
        "--confirm-rst-vpp-isolated",
        "--confirm-pin8-isolated",
        dest="confirm_rst_vpp_isolated",
        action="store_true",
        required=True,
        help="confirm target RSTb/VPP (SOP16 pin 8 or SOP8 pin 4) is isolated from adapter A5/VPP",
    )
    dump_parser.add_argument(
        "--confirm-one-full-read",
        action="store_true",
        required=True,
        help="confirm one fixed 2048-word target transaction is authorized",
    )
    dump_parser.add_argument(
        "--output-prefix",
        required=True,
        help="new output prefix; .ny8-rx4100.bin and .ny8-rom14be.bin are appended",
    )
    info_parser = subparsers.add_parser(
        "readid",
        help="read the fixed 0x05..0x15 information window and decode chip IDs",
    )
    info_parser.add_argument(
        "--confirm-rst-vpp-isolated",
        "--confirm-pin8-isolated",
        dest="confirm_rst_vpp_isolated",
        action="store_true",
        required=True,
        help="confirm target RSTb/VPP (SOP16 pin 8 or SOP8 pin 4) is isolated from adapter A5/VPP",
    )
    info_parser.add_argument(
        "--confirm-id-read",
        action="store_true",
        required=True,
        help="confirm the requested repeated fixed read-only 0x60 information reads",
    )
    info_parser.add_argument(
        "--repeats",
        type=int,
        choices=range(1, 11),
        default=NY8_INFO_DEFAULT_REPEATS,
        help=f"independent target reads (default: {NY8_INFO_DEFAULT_REPEATS}; maximum: 10)",
    )
    info_parser.add_argument(
        "--output-prefix",
        required=True,
        help="new output prefix; information raw/decoded suffixes are appended",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    port = None
    exit_code = 1
    raw_path = None
    decoded_path = None
    try:
        if args.action == "dump2048":
            raw_path, decoded_path = dump_output_paths(args.output_prefix)
        elif args.action == "readid":
            raw_path, decoded_path = info_output_paths(args.output_prefix)

        port, version = find_and_open(args.port)
        print(f"GETVERINFO: {version}")

        set_outputs_zero(port)
        time.sleep(IDLE_SETTLE_SECONDS)
        idle_before = read_voltages(port)
        print_voltages("Idle before", idle_before)

        if args.action == "info":
            do_info(port)
            ok = True
        elif args.action == "dump2048":
            require_outputs_off(idle_before, "Pre-operation rail check")
            require_experimental_lite(version)
            ok = do_dump2048(port, raw_path, decoded_path)
        elif args.action == "readid":
            require_outputs_off(idle_before, "Pre-operation rail check")
            require_experimental_lite(version)
            ok = do_readid(port, raw_path, decoded_path, args.repeats)
        else:
            raise ProtocolError(f"unsupported action {args.action!r}")
        exit_code = 0 if ok else 2
    except (OSError, serial.SerialException, ProtocolError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        if port is not None and port.is_open:
            try:
                set_outputs_zero(port)
                time.sleep(IDLE_SETTLE_SECONDS)
                idle_after = read_voltages(port)
                print_voltages("Idle after", idle_after)
                require_outputs_off(idle_after, "Final rail check")
            except Exception as exc:
                print(f"WARNING: final safe-off check failed: {exc}", file=sys.stderr)
                if exit_code == 0:
                    exit_code = 3
            try:
                port.dtr = False
            except Exception:
                pass
            port.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
