# EasyPDK Lite NY8 read-only support

## Status

This is experimental, read-only firmware and a matching host utility for the
EasyPDK Programmer Lite R1. It is not an extension of the normal Padauk
programming support, a general Nyquest programmer, or an official Nyquest tool.

Two loose SOP16 targets have been live-tested: the original `Y6` from the
investigated sensor board and one suspected NY8 removed from a TH03Pro Forever
Young device. Both have protocol-visible Q-Writer IDs `0x0F02` and `0x0014`,
which match Q-Writer's NY8A054E revision C profile. That is strong identification
evidence, but not cryptographic proof of the die manufacturer.

Q-Writer classifies this profile as OTP, with a 4096-byte/2048-word program
space, and does not mark it as MTP or EEPROM-equipped.

| Item | Status |
| --- | --- |
| EasyPDK Programmer Lite R1 | Supported and live-tested |
| Original loose SOP16 `Y6`, pin 8 isolated as described below | Supported and live-tested |
| One loose SOP16 target removed from a TH03Pro Forever Young device, pin 8 isolated | Supported and live-tested; not a claim about every TH03Pro |
| Other chips reporting an ID in the host database | Identification text only; not validated for reads |
| NY8A054E-family parts in other packages | Not validated |
| In-circuit targets or externally powered targets | Not supported |
| EasyPDK Mini, Blue Pill conversions, and other programmer variants | Refused by firmware |
| Write, erase, execute, fuse, trim, or protection changes | Not implemented |

## Supported operations

The experimental firmware exposes only two target transactions:

- `F`: one fixed 2048-word program-ROM read using the proven opcode-`0x20`
  sequence. There is no automatic retry.
- `N`: two fixed reads of information words `0x05..0x15` in one powered session
  using the proven opcode-`0x60` sequence.

`G` only retrieves a completed capture from STM32 RAM after the target has been
powered off. The host retrieves every capture twice and requires the two copies
to be byte-identical before saving it.

`dump2048` has an additional profile allowlist: after the target is off, the host
hashes the first 36 raw bytes and saves the capture only if they match one of the
two independently reproduced program-prefix fingerprints. Different firmware
in an otherwise valid NY8A054E will deliberately be rejected and no output files
will be published. This is a post-read save guardrail, not chip authentication
or pre-read electrical protection; another device sharing an allowed program
prefix can pass it. The fingerprint gate does not apply to `readid`.

The host utility can:

- decode the Q-Writer chip ID and match the extracted 054D/054E/054E1 entries;
- repeat the fixed information read up to ten times and report instability;
- decode Q-Writer family flags and NY8A054E factory trim fields already present
  in the `0x05..0x15` capture;
- report protection and CP-related fields as raw values without inventing active
  polarity or resolving Q-Writer's inconsistent higher-level label mapping;
- identify the final two program words as stored-checksum metadata;
- calculate Q-Writer's independent program-range checksum offline from a saved
  full dump; and
- keep rolling-code bytes uninterpreted when the target's rolling-code flag is
  disabled.

The ID table is descriptive only. Recognising another listed ID does not prove
that the Lite R1 wiring or these transactions are safe for that part.

## Explicitly unsupported

There is no target write, erase, execute, calibration, trim adjustment,
blank-check, verification, fuse/config write, protection change, arbitrary
address read, arbitrary opcode, or nonzero-VPP operation. The ordinary EasyPDK
target-operation handlers are compiled out of this image.

The firmware does not expose a unique silicon serial number. None was found in
the supplied Q-Writer material.

The stored 24-bit code-checksum field is not an independently verified integrity
check because its generating algorithm has not been established. Q-Writer's
separate range-checksum algorithm is known and is calculated offline as
`sum(byte XOR absolute_byte_address)`.

The complete Q-Writer option-range checksum cannot be calculated from EXP7.1:
it needs information words `0x00..0x0F`, while the deliberately unchanged read
window starts at `0x05`. Reading `0x00..0x04` is deferred to a separately reviewed
experiment.

## Safety boundary

- Firmware is restricted to detected Lite hardware.
- Target VDD is compile-time fixed at 3300 mV.
- VPP is compile-time hard-disabled; nonzero VPP requests are rejected.
- SCK and SDI use STM32 weak pulls, and SDO remains input-only.
- Every completed target transaction is followed by target power-off and fresh
  off-rail ADC checks.
- The host requires explicit confirmation flags before either target read.
- Physical target pin 8 isolated from the adapter's `A5`/VPP contact is required
  by the host workflow and is the only setup validated for either target.

## Hardware setup and pin mapping

Both validated targets were loose and completely removed from their original
PCBs.
Unplug the Lite R1 before inserting, removing, or changing the target. Do not
connect another power supply, the original product PCB, or a debug probe to the
target while the Lite R1 is connected.

Orient the SOP16 package normally: its pin-1 dot/notch end goes at the breakout
end marked `B4`/pin 1. The stock PFS173/PFS154 SO16 breakout labels describe the
Padauk part it was designed for, so they do **not** match the NY8 port names:

| NY8 SOP16 pin | NY8 function | SO16 breakout label | Lite R1 connection |
| ---: | --- | --- | --- |
| 5 | VDD | `VDD` | controlled target VDD |
| 8 | `PA5/RSTb/Vpp` | `A5` | `PA5_ICVPP`; **must be left open** |
| 9 | `PA4/SCK` | `A3` | STM32 PB3 |
| 10 | `PA3/SDO` | `A4` | STM32 PB4, input only |
| 11 | `PA2/SDI` | `A0` | STM32 PB6 |
| 12 | VSS | `GND` | ground |

Use an interposer or socket arrangement that leaves only target contact 8 open;
a bent package leg was the setup actually tested. Check continuity before
powering the programmer. Do not omit target pin 12, which is the real VSS pin.
The data sheet also labels SOP16 pin 16 with an alternate SDO function, but this
implementation uses only the validated programming SDO on physical pin 10.

Pin 8 is not isolated because the NY8 data sheet says to leave it disconnected.
It is isolated because the stock breakout sends that contact to the Lite R1 VPP
amplifier. The no-VPP firmware deliberately commands that output to 0 V and
neither drives nor samples pin 8 as part of the read protocol. All successful
reads of both specimens used an open contact 8; operation with it connected has
not been qualified under the final firmware. This is the validated setup and
safety boundary, not a claim that the chip inherently requires its reset/VPP pin
to be isolated.

## EXP7.1 interpretation of the tested Y6

From information word `0x09` low byte `0xF1`:

- stored code-checksum field: present;
- enforce-program: disabled;
- rolling code: disabled; and
- release mode: enabled.

Raw factory fields:

- LVR trim `0x3`;
- LVD/LDO trim `0x1E`;
- IHRC trim `0x17`;
- IHRC reserve `0x1F`;
- hardware trim `0x1`;
- ILRC trim `0xCF`;
- Q-Writer-version bit `1`;
- READ_PROTECT bit `0` and OVER_WRITE bit `0`, using the raw XML labels only;
- CP-pass bit `0`, virtual-body field `0xF`, and CP-SRAM bit `1`.

The retained program dump has final words `0x3EF2 0x04DA`, producing the stored
24-bit field `0xBC84DA`. Its offline Q-Writer program-range checksum is
`0x008004E6`.

## TH03Pro Forever Young specimen validation

One second loose SOP16 target was validated on 2026-09-03. Six independent
information-read cycles agreed on all 17 words and on both copies within every
cycle. Its IDs are the same `0x0F02`/`0x0014` NY8A054E revision C match as Y6,
and all reported family, protection, CP, and mode fields agree with Y6.

Only three captured information words differ, corresponding to plausible
per-die factory trims:

| Factory field | Original Y6 | TH03Pro specimen |
| --- | ---: | ---: |
| LVD/LDO trim | `0x1E` | `0x1C` |
| IHRC trim | `0x17` | `0x0E` |
| ILRC trim | `0xCF` | `0xD0` |

Five independent full target transactions completed with result 16, VPP at
0 mV, safe VDD, target power-off, and identical duplicate RAM retrievals. The
program-prefix fingerprint reproduced every time. Three separately saved full
captures were byte-identical. Every stored word was 14-bit clean, and the
offline disassembler reported no unknown opcode encodings. The programmed image
is distinct from Y6: 1797 of its 2048 stored words differ.

This validates the protocol and save profile only for the tested loose specimen;
it does not establish that every TH03Pro revision uses this MCU or wiring.

## Build and flash

Required build tools are GNU Make, an Arm GNU `arm-none-eabi` toolchain, and
`dfu-suffix`. On Windows, run Make from an environment such as MSYS2 so the
Makefile's Unix commands are available. A separate build directory keeps the
experimental output distinct from the upstream binary:

```sh
make -C Firmware/source \
  BUILD_DIR=build-ny8-readonly \
  GCC_PATH=/path/to/arm-none-eabi/bin \
  DFU_SUFFIX=/path/to/dfu-suffix \
  all
```

Confirm that `dfu-suffix --check` reports VID `0483`, PID `df11`, and a valid
suffix for `Firmware/source/build-ny8-readonly/EASYPDKPROG.dfu`. Before any
release, rename that generated file to an unmistakable name such as
`EASYPDKPROG-Lite-NY8EXP7.1-readonly.dfu`; never replace or relabel the tracked
upstream `Firmware/EASYPDKPROG.dfu`.

To enter DFU mode, unplug the Lite R1, hold its button while reconnecting USB,
then release the button. Flash only STM32 internal-flash alternate 0 at
`0x08000000`:

```sh
dfu-util -d 0483:df11 -a 0 \
  --dfuse-address 0x08000000 \
  -D Firmware/source/build-ny8-readonly/EASYPDKPROG.dfu
```

Unplug and reconnect after a successful download. The early boot log is not
needed: once the CDC port appears, `python ny8-easypdk.py info` requests the
firmware marker and rail status directly. If recovery is needed, re-enter DFU
mode and flash a known-good upstream Lite R1 image. The unchanged tracked
`Firmware/EASYPDKPROG.dfu` is the upstream image in this tree.

## Host setup and tests

The host utility needs Python 3.10 or newer and pyserial:

```powershell
python -m pip install -r .\requirements-ny8.txt
python -m unittest discover -s .\tests -v
```

## Host examples

Harmless firmware and rail check:

```powershell
python .\ny8-easypdk.py info
```

Six independent fixed information reads:

```powershell
python .\ny8-easypdk.py readid `
  --confirm-pin8-isolated `
  --confirm-id-read `
  --repeats 6 `
  --output-prefix .\target-info
```

One fixed full-program read:

```powershell
python .\ny8-easypdk.py dump2048 `
  --confirm-pin8-isolated `
  --confirm-one-full-read `
  --output-prefix .\target-rom
```

The output prefix must be new; the utility refuses to overwrite captures.

Offline disassembly accepts the host's 4096-byte `*.ny8-rom14be.bin` output:

```powershell
python .\tools\ny8_disasm.py .\target-rom.ny8-rom14be.bin `
  --output .\target-rom.ny8-disasm.txt
```

## Evidence, provenance, and publication boundary

The chip-family identification is supported by stable repeated reads, the two
protocol-visible ID words and their redundancy checks, a Q-Writer database
match, the documented 2048-word geometry, and plausible decoded instructions.
It remains an evidence-based identification, not a unique silicon identity.

The fixed read transaction was independently reimplemented with reference to
the MIT-licensed `PixMob_IR` NY8 dumper. The disassembler also references the
MIT-licensed `Ghidra_NY8A054E` opcode description. Full notices and source links
are in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). Q-Writer
materials were examined for interoperability facts; no vendor executable,
library, embedded firmware, configuration file, or copied implementation is in
this repository.

Do not commit or release target captures, raw ROM, full disassembly, vendor
packages, reverse-engineering workspaces, local paths/logs, or intermediate
build products. A release should contain source plus the uniquely named final
DFU/BIN files and a checksum manifest. The pre-Git experimental chronology and
its source-recovery limits are recorded in
[`docs/EXPERIMENT_HISTORY.md`](docs/EXPERIMENT_HISTORY.md).
