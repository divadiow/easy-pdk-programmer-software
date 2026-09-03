#!/usr/bin/env python3
# Copyright (C) 2026 divadiow
# SPDX-License-Identifier: GPL-3.0-or-later

"""Small, deterministic NY8A054E word disassembler.

Input is a sequence of 14-bit program words stored as 16-bit big-endian values.
The opcode masks follow the NY8A054E data sheet and James Wang's MIT-licensed
Ghidra_NY8A054E SLEIGH definition at commit
b057376fc34250ee9aa988aba3f420be61e47fc0. See THIRD_PARTY_NOTICES.md.
That SLEIGH definition omits ADCAR/SBCAR; their 0x34xx/0x35xx encodings are
included here. For a complete 2048-word NY8A054E image, words 0x7FE..0x7FF are
labelled as Q-Writer stored-checksum metadata instead of being presented as
executable instructions.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import struct


R_NAMES = {
    0x00: "INDF",
    0x01: "TMR0",
    0x02: "PCL",
    0x03: "STATUS",
    0x04: "FSR",
    0x05: "PORTA",
    0x06: "PORTB",
    0x08: "PCON",
    0x09: "BWUCON",
    0x0A: "PCHBUF",
    0x0B: "ABPLCON",
    0x0C: "BPHCON",
    0x0E: "INTE",
    0x0F: "INTF",
    0x15: "AWUCON",
    0x18: "INTEDG",
    0x19: "TMRH",
    0x1B: "RFC",
    0x1C: "TM34RH",
    0x1F: "INTE2",
}

F_NAMES = {
    0x05: "IOSTA",
    0x06: "IOSTB",
    0x09: "APHCON",
    0x0A: "PS0CV",
    0x0C: "BODCON",
    0x0E: "CMPCR",
    0x0F: "PCON1",
}

S_NAMES = {
    0x00: "TMR1",
    0x01: "T1CR1",
    0x02: "T1CR2",
    0x03: "PWM1DUTY",
    0x04: "PS1CV",
    0x05: "BZ1CR",
    0x06: "IRCR",
    0x07: "TBHP",
    0x08: "TBHD",
    0x0A: "P2CR1",
    0x0C: "PWM2DUTY",
    0x0F: "OSCCR",
    0x10: "TMR3",
    0x11: "T3CR1",
    0x12: "T3CR2",
    0x13: "PWM3DUTY",
    0x14: "PS3CV",
    0x16: "P4CR1",
    0x18: "PWM4DUTY",
    0x1B: "P5CR1",
    0x1D: "PWM5DUTY",
    0x1F: "PWM5RH",
}

EXACT = {
    0x0000: "NOP",
    0x0001: "SLEEP",
    0x0002: "CLRWDT",
    0x0003: "T0MD",
    0x0004: "ENI",
    0x0010: "RET",
    0x0011: "RETIE",
    0x0012: "DAA",
    0x0013: "DISI",
    0x0014: "T0MDR",
    0x0200: "CLRA",
    0x0210: "INT",
    0x0211: "TABLEA",
    0x0212: "CALLA",
    0x0213: "GOTOA",
}

REG_ALU = {
    0x03: "ADDAR",
    0x04: "SUBAR",
    0x05: "INCR",
    0x06: "DECR",
    0x07: "COMR",
    0x10: "ANDAR",
    0x11: "IORAR",
    0x12: "XORAR",
    0x13: "RRR",
    0x14: "RLR",
    0x15: "SWAPR",
    0x16: "INCRSZ",
    0x17: "DECRSZ",
    0x34: "ADCAR",
    0x35: "SBCAR",
}

IMM = {
    0x20: "RETIA",
    0x21: "MOVIA",
    0x22: "ANDIA",
    0x23: "IORIA",
    0x24: "XORIA",
    0x25: "ADDIA",
    0x26: "ADCIA",
    0x27: "SUBIA",
    0x30: "SBCIA",
}

NY8A054E_ROM_WORDS = 2048
NY8A054E_CHECKSUM_WORD = NY8A054E_ROM_WORDS - 2


def r_name(index: int) -> str:
    return R_NAMES.get(index, f"R{index:02X}")


def decode(address: int, word: int) -> str:
    if word & 0xC000:
        return ".word (outside 14-bit range)"
    if word in EXACT:
        return EXACT[word]

    rr = word & 0x7F
    dest = "ToReg" if word & 0x80 else "ToAcc"
    bit = (word >> 7) & 7
    op3 = (word >> 11) & 0x07
    op4 = (word >> 10) & 0x0F
    op5 = (word >> 9) & 0x1F
    op6 = (word >> 8) & 0x3F
    op7 = (word >> 7) & 0x7F
    op9 = (word >> 5) & 0x1FF
    op10 = (word >> 4) & 0x3FF

    if op10 == 0 and (word & 0x0F) >= 5:
        index = word & 0x0F
        return f"IOST {F_NAMES.get(index, f'F{index:X}')}"
    if op10 == 1 and (word & 0x0F) >= 5:
        index = word & 0x0F
        return f"IOSTR {F_NAMES.get(index, f'F{index:X}')}"
    if op9 == 2:
        index = word & 0x1F
        return f"SFUN {S_NAMES.get(index, f'S{index:02X}')}"
    if op9 == 3:
        index = word & 0x1F
        return f"SFUNR {S_NAMES.get(index, f'S{index:02X}')}"
    if op7 == 1:
        return f"MOVAR {r_name(rr)}"
    if op7 == 5:
        return f"CLRR {r_name(rr)}"
    if op7 == 109:
        return f"CMPAR {r_name(rr)}"
    if op6 == 1:
        return f"MOVR {r_name(rr)},{dest}"
    if op6 in REG_ALU:
        return f"{REG_ALU[op6]} {r_name(rr)},{dest}"
    if op4 in (2, 3, 6, 7):
        mnemonic = {2: "BTRSS", 3: "BTRSC", 6: "BSR", 7: "BCR"}[op4]
        return f"{mnemonic} {r_name(rr)},bit {bit}"
    if op6 in IMM:
        return f"{IMM[op6]} 0x{word & 0xFF:02X}"
    if op6 == 49:
        target = ((address + 1) & 0x700) | (word & 0xFF)
        return f"CALL 0x{target:03X}"
    if op5 == 25:
        target = ((address + 1) & 0x600) | (word & 0x1FF)
        return f"GOTO 0x{target:03X}"
    if op3 == 5:
        return f"LCALL 0x{word & 0x7FF:03X}"
    if op3 == 7:
        return f"LGOTO 0x{word & 0x7FF:03X}"
    return ".word (undefined opcode)"


def stored_code_checksum(words: tuple[int, ...]) -> int:
    return (((words[-2] & 0x3FFF) << 14) | (words[-1] & 0x3FFF)) & 0xFFFFFF


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("rom", type=Path)
    parser.add_argument("--start", type=lambda value: int(value, 0), default=0)
    parser.add_argument("--end", type=lambda value: int(value, 0))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    raw = args.rom.read_bytes()
    if len(raw) % 2:
        raise SystemExit("input length is not an even number of bytes")
    words = struct.unpack(f">{len(raw) // 2}H", raw)
    end = len(words) if args.end is None else min(args.end, len(words))
    if not (0 <= args.start <= end):
        raise SystemExit("invalid start/end range")
    lines = []
    checksum = (
        stored_code_checksum(words) if len(words) == NY8A054E_ROM_WORDS else None
    )
    for address in range(args.start, end):
        word = words[address]
        if checksum is not None and address >= NY8A054E_CHECKSUM_WORD:
            part = address - NY8A054E_CHECKSUM_WORD + 1
            suffix = f"; packed24=0x{checksum:06X}" if part == 2 else ""
            decoded = f".metadata Q-Writer stored-checksum source {part}/2{suffix}"
        else:
            decoded = decode(address, word)
        lines.append(f"{address:03X}: {word:04X}  {decoded}")
    text = "\n".join(lines) + ("\n" if lines else "")
    if args.output is None:
        print(text, end="")
    else:
        args.output.write_text(text, encoding="ascii", newline="\n")


if __name__ == "__main__":
    main()
