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
| Safe cave candidate | `0x402B4200..0x402B5000` |

## Sample/SRR trace

| Landmark | Meaning |
|---|---|
| `0x401ABC48` | Bit Reduction descriptor |
| physical parameter `0x15` | Stock BR parameter |
| packed record word 39 | Proven SRR/BR consumer input |
| `0x4011C69E` | 13 × 42-word control-rate EMAC smoother |
| `0x40117F00` | Pre-render/audio-interface routine; returns under modeled READY transitions |
| `0x4011870E` | Terminal BR render read |
| `0x4011877A..0x401187A0` | Stock 32-sample render loop |

The control smoother is not the audio quantizer. The audio-interface blocker is now
resolved: all three READY polls execute, and the TCD30 CSR `0x10` poll exits after a
modeled transient observation. The next task is to execute the terminal BR read and
32-sample render loop with sample-word provenance. This remains the preferred boundary
for a digital Filter 2 before the DAC path.

## Audio scheduling

- eDMA interrupt channel: 54.
- Render sequence: `0x40117F00` → `0x4010A2E0` → `0x40108944` → `0x40105188`.
- PIT0 vector 205 is now deliverable in MiniColdFire.
- Descriptor initializer `0x4011AE52` completes in 154 synthetic instructions.
- Destination indices 39..46 map from input destinations 13..20.
- READY poll sites `0x40117F16`, `0x40118396`, and `0x40118518` execute in a
  17,109-instruction pre-render smoke call.
- `0x40109FFE` polls TCD30 CSR word `0xFC0453DE`, mask `0x10`. The bit's exact
  hardware semantics are deliberately left unclassified pending hardware evidence.

## Feature tracks

### Slice16

The transactional Slice16 image is the preferred candidate. Its SHA-256 is
`9233da51a2467a7dd0a7b8897af41058e54fa4768e86479c226778a26c7a709f`.
The zero-persistence variant remains a fallback. Neither may skip the staged hardware
acceptance and recovery protocol.

### SRR

The functional SRR research build is statically valid and hardware-unverified. The upper
BR bands correspond to 2, 4, 8, 16, and 32-sample hold periods. Its recorded SHA-256 is
`ac077fe3d2262494265a12f1b8264e53e637091323c2306cf90573560b06a82e`.

### LFO2

LFO2 should reuse the proven destination equation and update machinery via shadow state.
The stock 42-word record stays frozen until persistence and SysEx compatibility are mapped.

### Filter 2

Filter 2 belongs in the digital sample path before the DAC, after the exact quantizer/render
boundary is proven. The reference model is a topology-preserving state-variable filter.

## Safety boundary

This repository contains no original or modified Elektron firmware image. Research scripts
are read-only unless explicitly documented otherwise. Hardware testing must follow
`docs/AR172_FIRST_HARDWARE_TEST_PROTOCOL.md` in order.
