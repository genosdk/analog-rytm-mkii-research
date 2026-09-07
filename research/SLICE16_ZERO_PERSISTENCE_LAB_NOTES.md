# AR MKII OS 1.72 — Slice16 Zero-Persistence Lab Build

**DO NOT FLASH until the stock round-trip and inert-detour hardware tests pass.**

## Prototype behavior

- START 0–104: stock behavior.
- START 105–120: lab selectors for equal slices 1–16.
- The hook never writes the live START/END parameter table. It substitutes only the current `D0` input pair while the 572-word control image is converted.
- Slice width: `0x0780` over the internal `0x0000..0x7800` boundary domain.

## Patch architecture

1. `0x4011C550` → pre-hook `0x402B4200`: save D5/D6, initialize decrement-at-entry pair phase=24, replay displaced instruction.
2. `0x4011C56E` → pair-hook `0x402B4240`: identify START/END pairs, transform only D0, replay the exact two displaced 4-byte ColdFire MAC-family instructions.
3. `0x4011C598` → post-hook `0x402B4320`: restore D6/D5, replay displaced post-loop instructions.

## Offline validation

- Hook sizes: pre 18 B, pair 110 B, post 16 B.
- MAIN changed bytes: 136.
- Semantic cases: 1573 (13 tracks × 121 START values), all passed.
- SysEx packets/checksums: 14200/14200 passed.
- Container checksum: `0xF511DD21`.
- MAIN recompression/decompression: bit-identical to intended patched MAIN.
- Metadata/DSP/FPGA: decompressed bytes identical to stock.

## Hardware gates still open

1. Modified firmware acceptance by the physical bootloader.
2. Execution/safety of the candidate cave at `0x402B4200`.
3. Real-time timing margin of the per-pair hook.
4. Actual ColdFire→DSP boundary semantics.
5. Audible endpoint/click/loop behavior.

The first custom code hardware test remains the **inert detour build**, not this Slice16 build.
