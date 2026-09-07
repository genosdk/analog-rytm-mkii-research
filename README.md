# Analog Rytm MKII OS 1.72 Research

Private reverse-engineering workspace for the Analog Rytm MKII OS 1.72 research project.

## Current milestone

The firmware transport/container path is understood well enough to decode, modify,
recompress, checksum, and re-encode OS 1.72. Static analysis has located the sample
renderer's Bit Reduction path and produced a first functional **Sample Rate Reduction
(SRR)** research patch. The SRR build is statically valid but remains **hardware-unverified**.

## Safety gate

Do **not** jump directly to the functional SRR build on hardware. The staged hardware
sequence is:

1. Verify normal boot and disposable project baseline.
2. Verify startup-menu recovery with original Elektron OS 1.72 over physical MIDI.
3. Verify the byte-identical stock round-trip image.
4. Verify a checksum-correct modified MAIN image is accepted.
5. Verify the inert code-cave detour boots and behaves normally.
6. Only then test the functional SRR image on one disposable sample/track.

## Repository policy

This repository intentionally excludes:

- Elektron's original `.syx` firmware.
- Modified/custom `.syx` firmware images.
- Raw extracted proprietary firmware sections.

Those files belong in local `private/` or `build/` directories, which are ignored by Git.
The repo stores only original research notes, patch locations, hashes, validation reports,
and reproducible tooling.

## Key results

- OS package: ELE3 over Elektron SysEx transport.
- Device ID: `0x0C` (Analog Rytm MKII).
- MAIN load address: `0x40000400`.
- MAIN decompressed size: `2,903,032` bytes.
- Stock OS 1.72 SHA-256: `1ea60357abe8b876d8b9c52e6dcd988d833478a49d09e3cb22d42782ef822b2f`.
- Corrected cave candidate: `0x402B4200`.
- Bit Reduction descriptor: `0x401ABC48`.
- BR physical parameter: `0x15`.
- BR terminal render read: `0x4011870E`.
- Stock 32-sample render loop: `0x4011877A..0x401187A0`.
- Stock BR coefficient setup and all 32 quantizer MACs execute under MiniColdFire.
- Proven quantizer equation: `Q(x) = (((signed32(x) * signed32(D3)) >> 31) << D2) mod 2^32`.
- Runtime matrix: 8 BR words, 128 loop iterations, 256 / 256 sample matches.
- All three audio-interface READY polls execute and return under MiniColdFire.
- The TCD30 CSR `0x10` poll executes through a modeled one-observation transition.
- Functional SRR research image SHA-256: `ac077fe3d2262494265a12f1b8264e53e637091323c2306cf90573560b06a82e`.

See `docs/REVERSE_ENGINEERING_MAP.md`, `docs/AR172_LFO2_FILTER2_RESEARCH.md`,
and `research/SRR_FUNCTIONAL_RESEARCH_NOTES.md`.

## Railway dashboard

The included `app.py` is a zero-dependency read-only status dashboard suitable for a
private Railway service. It serves `/health` and does not serve firmware files.

```bash
python app.py
```

Railway can detect the included Dockerfile automatically. Do not enable public networking
unless you intentionally want the research dashboard exposed.

## Active reverse-engineering target

The terminal BR path is now instruction-executed from `0x4011870E` through
`0x401187A6`. Eight BR words independently reproduce the runtime D2/D3/D4
coefficients, and every one of 256 quantizer evaluations matches the signed Q31
equation above. The active target is now the post-quantizer sample-format/DAC
handoff so Filter 2 can be placed at a cycle-safe digital boundary. Front-panel
BR mapping and physical hardware behavior remain unverified.
