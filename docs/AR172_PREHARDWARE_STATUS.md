# Analog Rytm MKII OS 1.72 — Pre-hardware research status

**Input:** `Analog-Rytm_MKII_OS1.72.syx`

## Independently verified

- SysEx size: 1,706,144 bytes
- Data packets: 13,329
- Packet checksums: 13,329 / 13,329 valid
- Device ID: `0x0C` (Analog Rytm MKII)
- Container: `ELE3`
- Stored container checksum: `0x2111B1DE`
- Recomputed container checksum: `0x2111B1DE`
- MAIN load address: `0x40000400`
- MAIN decompressed size: 2,903,032 bytes
- MAIN SHA-256: `5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772`

## Transport round-trip

The stock decoded container was re-encoded locally using the recovered Elektron transport rules.

- Rebuilt file is byte-for-byte identical to stock.
- Stock/rebuilt SHA-256: `1ea60357abe8b876d8b9c52e6dcd988d833478a49d09e3cb22d42782ef822b2f`

## MAIN recompression

A local aPLib-compatible greedy encoder was implemented and validated.

- Original MAIN: 2,903,032 bytes
- Recompressed MAIN: 1,372,531 bytes
- Re-decompressed output: byte-for-byte identical to original MAIN
- Section stream length and byte-sum fields validate.

## Important cave correction

The previous proposed cave at `0x402C3434` is **unsafe**.

Startup code shows two initialized-data copy regions:

- `0x402B5000..0x402BD000` -> RAM around `0x80000000`
- `0x402BD000..0x402C4FF8` -> RAM around `0x80008000`

Therefore the zero-filled tail is initialized RAM data, not free executable space.

### New cave candidate

`0x402B41E0..0x402B5000` is a 3,616-byte zero pad immediately before the first initialized-data source region.

Immediately before it is a counted function-pointer table:

- count at `0x402B3FB0`: `0x0000008B` (139)
- 139 code pointers occupy `0x402B3FB4..0x402B41E0`
- zero padding begins exactly at the end of the counted table

For patches, use `0x402B4200` as the first cave address, leaving 32 bytes of guard space after the table.

Static checks for `0x402B4200..0x402B4800`:

- no plausible even-address 32-bit literal references into the region
- no absolute `JMP` / `JSR` targets into the region
- no decoded Bcc/BRA/BSR relative targets into the region

Hardware execution remains the final proof that this range is safe/executable.

## Verified control-frame pack window

The late runtime parameter overlay occurs before the control-frame conversion.

The bulk pack/conversion begins at:

- `0x4011C550`: `MOVE.L #0x03D70000,D7`
- `0x4011C566`: loads loop count `0x023C` = 572 words
- `0x4011C594`: loop branch back into the pack body
- `0x4011C598`: first instruction after the pack loop

572 words matches:

`26-word prefix + 13 tracks * 42 parameters = 572 words`

This remains the preferred transient slice-transform window.

## Safe-cave inert detour build

An inert control-flow test build has been generated using the corrected cave.

Patch point:

- `0x4011C312` stock: `20 39 80 00 67 B8`
- replacement: `4E F9 40 2B 42 00` (`JMP 0x402B4200`)

Cave at `0x402B4200`:

- executes the displaced six bytes
- jumps back to `0x4011C318`

This intentionally changes **control flow only**, not intended behavior.

Validation:

- packet checksums: 14,278 / 14,278 valid
- rebuilt ELE3 content checksum validates
- DSP and FPGA decompress identically to stock
- modified MAIN decompresses exactly to the intended patched image
- 17 MAIN bytes differ from stock

**This build is a research artifact and should not be flashed until the hardware recovery/acceptance sequence is ready.**

## Functional slice prototype direction

The public FW1.70 pattern format stores START p-locks as values `0..120`, while the FW1.70+ sound structure stores START/END as 16-bit fields whose low byte is used for fine resolution. The OS 1.72 runtime table likewise uses 16-bit START/END values.

A lab selector range of START `105..120` therefore maps naturally to raw high-byte values `0x6900..0x7800` if the normal p-lock scaling path is retained.

Before packaging the functional build, the remaining static design issue is how to make START/END substitution transient without relying on an unsafe scratch location. The preferred solution is to transform only the control-frame pack representation, or otherwise use a strictly bounded prototype that can restore the live table deterministically.

## Hardware gate

The following still require the physical AR MKII:

1. bootloader acceptance of a checksum-correct modified MAIN
2. execution permission at the new cave address
3. real-time timing/glitch behavior
4. exact audible START/END endpoint semantics
5. recovery behavior after a failed custom image
