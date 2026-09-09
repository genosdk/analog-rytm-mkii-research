# Analog Rytm MKII OS 1.72 — historical SRR research build

**STATUS: SUPERSEDED — DO NOT FLASH**

The historical artifact with SHA-256
`ac077fe3d2262494265a12f1b8264e53e637091323c2306cf90573560b06a82e`
was a checksum-valid research image, but its BR-selector premise has since been
disproven. It must not be used as the basis for Photon OS SRR/BR work or flashed
to hardware.

## Why it is superseded

The image detoured `0x4011870E` under the assumption that this instruction read
sample Bit Reduction. Later control-frame and renderer tracing proved the actual
stock sample-BR field is destination/record word 11, track-0 address
`0x8000F7BE`, and machine renderer 0 reads it at `0x4010CC58`.

The real CPU-side BR path is now:

```text
0x8000F7BE                 stock sample-BR word
    -> 0x4010CC58          renderer read/cache
    -> 0x4010D16E          case-3 BR hardware-control conversion
    -> 0x80006544          packed per-voice hardware command
    -> 0x40077D14          DSPI packetizer
    -> eDMA channel 15
    -> DSPI1 PUSHR 0xFC03C034
    -> external FPGA/audio hardware
```

`0x4011870E` belongs to a different per-voice coefficient/render path. The
D2/D3/D4 arithmetic reconstructed around that address remains valid firmware
arithmetic, but it is **not** the stock sample-BR quantizer.

## What remains valid from the historical artifact

The following historical facts remain useful only as engineering/provenance data:

- the package could be decoded, modified, recompressed and checksummed correctly;
- the candidate cave beginning at `0x402B4200` was statically unreferenced in the
  stock image;
- the post-pass itself implemented a conventional 32-longword sample-and-hold;
- the modified package had valid SysEx/ELE3 integrity fields;
- original output SHA-256 was
  `ac077fe3d2262494265a12f1b8264e53e637091323c2306cf90573560b06a82e`.

Those facts do **not** establish that the build implemented SRR on the intended
sample-BR path.

## Historical patch layout — reference only

- `0x4011870E` -> `JMP 0x402B4200`
- `0x402B4200` -> historical selector pre-hook
- `0x401187A6` -> `JMP 0x402B4300` + NOP
- `0x402B4300` -> 32-longword sample-and-hold post-pass

Do not recreate or flash this layout.

## Replacement direction

Any new SRR implementation must start from one of two proven architectures:

1. **Hardware-control route:** preserve the stock BR command encoder and add SRR
   as a separate parameter/control command only after the FPGA/audio-side command
   semantics are understood; or
2. **MAIN sample route:** insert SRR only at a genuinely proven active sample PCM
   boundary, with OFF/bypass shown bit-identical and cycle headroom measured.

The stock BR hardware command itself is now reconstructed separately in
`research/br_runtime_command_reconstruct.py`; physical BR audio behavior is
characterized by `research/br_hardware_characterize.py` once hardware is present.

## Safety boundary

This historical image is permanently classified **DO NOT FLASH**. Hardware tests
must follow `docs/AR172_FIRST_HARDWARE_TEST_PROTOCOL.md`; no SRR build should be
introduced until a replacement prototype is based on the corrected architecture.
