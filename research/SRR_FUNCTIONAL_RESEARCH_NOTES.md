# Analog Rytm MKII OS 1.72 — SRR functional research build

**Status: STATICALLY VALIDATED RESEARCH BUILD — DO NOT FLASH YET**

This image is the first build containing an actual Sample Rate Reduction experiment rather than an inert detour.

## Corrected BR identity

The OS 1.72 master descriptor at `0x401ABC48` points directly to `Bit Reduction` / `BR`.

- Physical parameter ID: `0x15`
- Internal maximum: `0x7800`
- Stock terminal BR read: `0x4011870E`

Any earlier interim note identifying BR as `0x14` is superseded by this direct descriptor-table verification.

## Laboratory behavior

Below internal BR `0x7400`, the firmware follows the stock render path.

At the top five internal BR bands, the first hook makes the stock bit-reduction stage see BR=0, while the original BR value remains in parameter state and selects SRR:

| Internal BR | Experimental behavior |
|---|---|
| `0x7400..0x74FF` | hold 2 samples |
| `0x7500..0x75FF` | hold 4 samples |
| `0x7600..0x76FF` | hold 8 samples |
| `0x7700..0x77FF` | hold 16 samples |
| `0x7800+` | hold 32 samples |

This is a lab control scheme only. A production implementation should give SRR its own UI/storage parameter.

## Render-loop proof

The stock loop:

- initializes `D0=16` at `0x40118778`
- starts at `0x4011877A`
- emits two `MOVE.L A0,(A6)+` writes per iteration at `0x40118798` and `0x4011879C`
- decrements at `0x4011879E`
- branches back at `0x401187A0`

That yields 32 rendered 32-bit samples = `0x80` bytes.

The SRR post-pass subtracts `0x80` from A6, performs a conventional sample-and-hold across those 32 longs, and advances A6 naturally back to its original end pointer.

## Patch layout

- `0x4011870E` → `JMP 0x402B4200`
- `0x402B4200` → BR/SRR selector pre-hook
- `0x401187A6` → `JMP 0x402B4300` + NOP
- `0x402B4300` → 32-sample SRR post-pass
- return → `0x401187AE`

The stock cave area was zero-filled and static scans found no literal references, absolute JMP/JSR targets, or relative branch targets into the used range.

## Integrity validation

- SysEx packet checksums: **14,137 / 14,137 valid**
- ELE3 content checksum: **`0x9930D853`**, recomputes exactly
- MAIN decompressed size: **2,903,032 bytes**
- MAIN changes vs stock: **244 bytes**
- MAIN recompress → redecompress: **byte-for-byte exact**
- META: unchanged
- bootstrap/recovery section: unchanged
- FPGA section: unchanged
- authentication trailer: none

Output SHA-256:

`ac077fe3d2262494265a12f1b8264e53e637091323c2306cf90573560b06a82e`

## Hardware gate

Do not flash this image before the staged hardware protocol has first proven:

1. stock recovery over physical MIDI,
2. the byte-identical stock round-trip image,
3. bootloader acceptance of a checksum-correct modified MAIN,
4. the inert safe-cave detour.

Only then should this functional SRR image be considered for a disposable one-track test.
