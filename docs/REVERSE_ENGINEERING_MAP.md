# Reverse-engineering map

## Proven package and CPU facts

| Item | Result |
|---|---|
| Firmware | Analog Rytm MKII OS 1.72 |
| Container | ELE3 over Elektron SysEx transport |
| Device ID | `0x0C` |
| MAIN load | `0x40000400` |
| MAIN size | `2,903,032` bytes |
| CPU model | NXP/Freescale MCF5441x ColdFire family; QEMU `cfv4e` baseline |
| Stock MAIN SHA-256 | `5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772` |
| Stock SysEx SHA-256 | `1ea60357abe8b876d8b9c52e6dcd988d833478a49d09e3cb22d42782ef822b2f` |
| Safe cave candidate | `0x402B4200..0x402B5000` |

## Sample Bit Reduction — corrected path

Earlier research that labeled `0x4011870E..0x401187A4` as sample Bit Reduction is **superseded**. That arithmetic block is real firmware DSP/control code but its `A1` provenance lands on another track destination, not sample BR.

The current proven BR path is:

| Landmark | Meaning |
|---|---|
| BR UI/storage domain | `0..127` |
| MIDI control | CC `26`; NRPN `1:10` |
| modulation destination | `11` |
| track-0 packed BR | `0x8000F7BE` / frame word `37` |
| control smoother | `0x4011C69E`; covers the BR-containing longword at `0x8000F7BC` |
| machine renderer table | `0x40277FE8` |
| machine-0 renderer | `0x4010CBA8` |
| direct BR read | `0x4010CC58` |
| BR-update state case | case 3 at `0x4010D164` |
| cached BR read | `0x4010D16E` |
| fixed-point helper | `0x4011A0B6` |
| per-voice BR command | `0x80006544` |
| packetizer family | `0x40077Dxx`; source slab `0x800063C0..0x80006797` |
| DSPI submit | eDMA channel `15` -> DSPI1 PUSHR `0xFC03C034` |

### CPU command law

The renderer's 128-point BR command sweep reconstructs to:

`command(BR) = 0xB31407FF + ceil(BR * 0x40000 / 127)`

with endpoints:

- BR 0 -> `0xB31407FF`
- BR 127 -> `0xB31807FF`

The associated four-halfword per-voice record is serialized as:

`0x4000, 0x0080, command_hi, command_lo`

and expanded into DSPI PUSHR entries by the packetizer. MAIN does not numerically apply this command to PCM in the traced path.

### CPU-side PCM differential

A BR-low/high experiment with deterministic **nonzero** data pre-seeded into the three CPU renderer source planes produced:

- identical traced renderer writes,
- identical combined CPU output,
- different packed BR hardware commands.

This strongly rejects the old hypothesis that stock BR is a ColdFire-side PCM `AND`/shift/truncation operation.

The exact **hardware-side** quantizer equation remains unproven. `research/br_hardware_characterize.py` is the active measurement harness for resolving it on a physical Rytm.

## Render and audio geometry

| Landmark | Meaning |
|---|---|
| `0x4010A2E0` | MAIN combiner: 32 frames × 8 lanes |
| source plane A | `0x80006BF8..0x80006FF4` |
| source plane B | `0x80007040..0x8000743C` |
| source plane C | `0x800067FC..0x80006BF8` |
| combined output | `0x80000800..0x80000FDC`, `0x40`-byte frame stride |
| eDMA 30 | `17 × 16 = 272` byte external modulo-window ingress beginning at `0x4B7FFFF0` |
| eDMA 31/32 | repeated `9 × 16 = 144` byte external-to-SRAM ingress/state blocks |
| eDMA 15 | outbound BR/control packet transport to DSPI1 |

The previous classification of a post-`0x401187xx` slab as a proven post-BR PCM boundary is retired with the BR correction.

## Firmware section roles

- **Section ID 2**: temporary ColdFire bootstrap/updater/service UI; not the runtime sample DSP.
- **Section ID 1**: 149,516-byte 16-bit FPGA configuration stream; not ColdFire code.
- **Section ID 3**: MAIN runtime image containing the control/render path under active analysis.

## Event/control state

The renderer uses an eight-entry, `0x20`-byte per-physical-voice event-state array around `0x8000FEF8`.

A synthetic BR-update event naturally transitions:

`case 3 -> case 4 with 32-unit countdown -> 16 -> 0 -> idle (-1)`

This event machine handles parameter/control timing. It is distinct from the missing project/sample-resource activation state needed for fully genuine sample playback emulation.

## Feature tracks

### Slice16

The transactional Slice16 candidate remains the preferred first functional feature test after recovery and inert-detour gates. Its control-frame transformation is independent of the corrected BR architecture.

### SRR

The existing functional SRR research image is hardware-unverified. Its original selector overloaded the path previously mislabeled as BR, so that selector architecture is **superseded** and must be redesigned before production use.

### LFO2

LFO2 remains a MAIN control/UI/state problem. It should reuse the proven destination/update machinery through shadow state while preserving the stock 42-word record until persistence and SysEx compatibility are mapped.

### Filter 2

A Filter 2 insertion point is **not yet proven**. The next acceptable boundary is either:

1. the first genuine active-sample PCM/fetch/interpolation write inside MAIN, or
2. a hardware-side point proven from physical characterization/FPGA analysis.

Do not use the retired `0x401187xx` BR interpretation as the insertion boundary.

## Active next gate

Run the stock BR hardware characterization suite:

- `docs/AR172_BR_HARDWARE_CHARACTERIZATION.md`
- `research/br_hardware_characterize.py`

Measure all BR values against a deterministic ramp, correlate the captured quantization behavior with the reconstructed DSPI command law, and establish the true hardware-side bit-depth/rounding function.

## Safety boundary

This repository contains no original or modified Elektron firmware image. Research scripts are read-only unless explicitly documented otherwise. Hardware testing must follow `docs/AR172_FIRST_HARDWARE_TEST_PROTOCOL.md` in order.
