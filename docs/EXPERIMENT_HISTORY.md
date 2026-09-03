# NY8 read-only experiment history

The EXP labels below were bench milestones recorded before this work had a Git
history. They are not releases, tags, or original commits. The public Git
history was later organised into reviewable commits by concern: ignore rules,
firmware transport, guarded host utility, offline tooling, and documentation.
Inventing source commits for stages whose exact source was not retained would
misrepresent the evidence, so this table records the recovery boundary instead.

| Milestone | Date | Result | Exact source retained? |
| --- | --- | --- | --- |
| EXP1 | 2026-08-31 | Initial Lite-only, no-VPP safety scaffold and control observations | Yes, as a source patch |
| EXP2 | 2026-09-01 | No-handshake control experiment | No; executable artifacts and Python bytecode only |
| EXP3 | 2026-09-01 | Read-sequence discriminator | Host source yes; firmware executable/objects/assembly only |
| EXP4 | 2026-09-01 | Fixed eight-slot preview | Host source yes; firmware executable/objects/assembly only |
| EXP5 | 2026-09-01 | Fixed 16-slot/36-byte framing prefix | Host source yes; firmware executable/objects/assembly only |
| EXP6 | 2026-09-01 | Fixed 2048-word read; six independent full reads were byte-identical | Host source yes; firmware executable/objects/assembly only |
| EXP7 | 2026-09-02 | Fixed information read at `0x05..0x15` and initial ID interpretation | Host source and firmware executable yes; exact firmware source no |
| EXP7.1 | 2026-09-03 | Repeated information reads, Q-Writer field/checksum interpretation, and offline disassembly | Yes; this is the publication baseline |

All known flashable experimental binaries and supporting bench artifacts remain
in the private investigation workspace. They are deliberately excluded from the
public branch because they include target-derived data, intermediate builds,
logs, and third-party vendor materials. An exact EXP7.1 checkpoint was retained
privately before publication. It is not a public branch, and the public history
contains only reviewed, publication-safe material rather than descending from
that private checkpoint.

Consequently, EXP1 through EXP7 should remain historical evidence rather than be
recreated as artificial source commits. Only EXP7.1 has both an exact retained
source state and the hardware validation needed to serve as a reproducible
baseline.
