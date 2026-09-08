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
| BR UI/storage domain | `0..127`, nominal runtime encoding `BR << 8` |
| packed record word 39 | Proven SRR/BR consumer input in the descriptor/control trace; terminal provenance reconciliation remains open |
| `0x4011C69E` | 13 × 42-word control-rate EMAC smoother |
| `0x40117F00` | Pre-render/audio-interface routine; returns under modeled READY transitions |
| `0x401186B2..0x40118700` | Previous/current sample-level state shaping and persistence |
| `0x4011870E` | Terminal BR render read |
| `0x40118744..0x40118750` | BR exponential-table lookup producing D4/D3 |
| `0x4011875A..0x4011876C` | D4 sample-level compensation and 32-sample ramp setup |
| `0x4011877A..0x401187A0` | Stock 32-sample render loop |
| `0x4011878E`, `0x40118792` | Paired signed-fractional D3 quantizer MACs |
| `0x40118782`, `0x40118788` | Post-quantizer sample-level ramp MACs through ACC2/ACC3 |
| `0x800067F8..0x80006BF7` | Eight post-BR voice blocks, 32 longwords each |
| `0x4010A2E0` | Consumes all 256 voice words and emits strided frame slots |
| `0x40109F04` | Shared handoff/staging pipeline |
| eDMA 30 | Input-side: `0x4B7FFFF0` → SRAM `0x8000DDD0` |
| eDMA 42 | Outbound: 256-byte SRAM block → `0x4B400000` |

The control smoother is not the audio quantizer. The terminal BR setup and loop now
execute from `0x4011870E` through `0x401187A6`. Across eight raw BR words, all 128
loop iterations and 256 sample operations match the independently reconstructed
quantizer equation:

`Q(x) = (((signed32(x) * signed32(D3)) >> 31) << D2) mod 2^32`

The surrounding block is now reconstructed as well. If `Lprev` and `Lnext` are
the previous/current sample-level ramp states, stock computes:

```text
r0   = truncQ31(Lprev, D4)
r1   = truncQ31(Lnext, D4)
dr   = wrap32(r1-r0) >> 5
y[i] = truncQ31(Q(sample[i]), wrap32(r0 + i*dr)), i=0..31
```

D4 is therefore BR-dependent amplitude compensation applied to the sample-level
smoothing ramp, not another amplitude-resolution quantizer. The instruction-order
pipeline and this closed-form expression match for 10,000 randomized blocks;
10,006 independent Q31 alignment cases also pass. Normal exit clears ACC0..ACC3.
See `research/br_full_path_reconstruct.py` and
`research/AR172_BR_FULL_PATH_RECONSTRUCTION.md`.

Runtime provenance also proves the post-BR voice slab, renderer address permutation,
shared 0x200-byte staging area, and outbound eDMA-42 direction. The post-BR slab is
therefore the preferred semantic Filter 2 insertion point. Cycle margin, exact
control-frame-to-terminal-BR provenance, and physical hardware behavior remain open.
An in-memory-only candidate replaces the renderer call at `0x4011CAE2` with a call
to unused space at `0x402B4800`; the cave tail-jumps to the stock renderer. It
preserves the tagged post-BR slab, renderer return state, nonzero renderer frame,
fixed stage, and nonzero outbound DMA block exactly. The final-mix inputs are a
documented synthetic fixture because the compact model has no project loader. The
detour adds one semantic instruction per 32-frame block; it does not establish real
cycle margin.

## Audio scheduling

- eDMA interrupt channel: 54.
- Render sequence: `0x40117F00` → `0x4010A2E0` → `0x40108944` → `0x40105188`.
- PIT0 vector 205 is now deliverable in MiniColdFire.
- Descriptor initializer `0x4011AE52` completes in 154 synthetic instructions.
- Destination indices 39..46 map from input destinations 13..20.
- READY poll sites `0x40117F16`, `0x40118396`, and `0x40118518` execute in a
  17,109-instruction pre-render smoke call.
- `0x40109FFE` polls TCD30 CSR word `0xFC0453DE`, mask `0x10`. TCD30's descriptor
  establishes an external-to-SRAM input path; the precise meaning of mask `0x10`
  remains hardware-unverified.
- The stream default and block geometry imply 1,500 32-frame blocks per second,
  or a 666.667-microsecond block deadline at 48 kHz. The traced stock components
  execute 40,971 semantic instructions per block, a 61.4565-MIPS lower bound if
  each counted instruction took one cycle. Scheduler glue, cache/SDRAM stalls,
  and untraced callback work are excluded.

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

Filter 2 belongs at the proven voice-separated post-BR boundary, before renderer
`0x4010A2E0`. The reference model is a topology-preserving state-variable filter;
the disabled bypass is exact through a signal-bearing outbound DMA block under a
documented synthetic runtime fixture. Full callback timing, board clock
confirmation, and project-loaded runtime validation remain required before
enabling it.

## Safety boundary

This repository contains no original or modified Elektron firmware image. Research scripts
are read-only unless explicitly documented otherwise. Hardware testing must follow
`docs/AR172_FIRST_HARDWARE_TEST_PROTOCOL.md` in order.
