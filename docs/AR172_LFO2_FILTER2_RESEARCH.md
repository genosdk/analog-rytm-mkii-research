# Analog Rytm MKII OS 1.72 — LFO2 and Filter 2 research branch

## Status

Both requested features are now active reverse-engineering targets. This branch
does **not** alter the validated Slice16 artifacts or the required first-hardware
test order.

The stock package is now fully and reproducibly unpacked. Elektron's three
compressed sections use UCL NRV2B with the 8-bit bit buffer—not aPLib. The
decompressed MAIN image is 2,903,032 bytes with SHA-256
`5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772`.

## Architecture decision

The two features belong on opposite sides of the existing control/audio boundary:

- **LFO2** begins in the ColdFire MAIN image: parameter state, trigger modes,
  phase/update scheduling, modulation destination dispatch, UI, parameter locks,
  MIDI and persistence.
- **Filter 2** belongs in a writable digital sample path before the sample DAC.
  Section ID 2 is now rejected as that path: its embedded upgrade/service text
  proves it is the temporary ColdFire bootstrap updater. Section ID 1 is a
  16-bit FPGA configuration stream, not CPU code. MAIN's BR path is now proven
  to terminate in a hardware control packet. MAIN routine `0x40117F00` is now
  proven to convert external audio arriving through eDMA channels 31/32 into an
  eight-lane, 32-word output plane at `0x800067F8`. That post-conversion plane is
  the first strong Filter 2 hook candidate. Programmable logic remains a
  separate, substantially harder fallback.

The physical analog voice topology remains unchanged:

`sample playback -> Filter 2 (digital) -> DAC/mix with analog generator -> analog overdrive -> existing analog filter`

Filter 2 therefore targets the sample layer. It cannot become a second physical
analog filter through firmware.

## Compatibility rule

Unmodified kits, sounds, projects and SysEx objects must load exactly as stock.
New state must use a versioned extension or verified-unused storage; existing
42-parameter track frames will not be enlarged speculatively.

## LFO2 target behavior

- One additional independent LFO per drum track and the FX track.
- Stock waveform family: triangle, sine, square, saw, ramp, exponential, random.
- Stock-style FREE, TRIG, HOLD, ONE and HALF behavior.
- Independent speed, multiplier, fade, phase, mode, destination and depth.
- LFO2-to-LFO1 routing is deferred until the basic destination dispatcher is proven.
- UI candidate: repeated press of `LFO` selects LFO1/LFO2. No UI patch is allowed
  until the existing page descriptor and encoder-binding tables are mapped.

### LFO2 reverse-engineering gates

1. Locate all code and tables that reference the current LFO parameter IDs.
2. Identify the control-rate scheduler and per-track phase/state allocation.
3. Map modulation destination dispatch and scaling/saturation rules.
4. Find verified-unused RAM for 13 shadow LFO states.
5. Prove a disabled LFO2 path is bit-identical to stock output/control frames.
6. Add trigger/reset behavior, then one destination, then all destinations.
7. Add storage/UI/p-lock support only after runtime behavior is stable.

## Filter 2 target behavior

- Sample-side multimode filter: LP, HP, BP and notch.
- Resonance, cutoff, drive and OFF bypass.
- Initial 2-pole topology; 4-pole cascade only after cycle-margin measurement.
- Proposed order selector: `Filter 2 -> SRR/BRR` or `SRR/BRR -> Filter 2`.
- Existing analog filter remains downstream and unchanged.

### Filter 2 reverse-engineering gates

1. **Complete, with role correction:** decompress every compressed ELE3 section
   with exact size/hash validation. Section ID 2 expands to a 42,302-byte
   ColdFire bootstrap/updater with literal `BOOTSTRAP UPGRADE`, CRC, version and
   service-test messages. It is not the runtime sample DSP. Section ID 1 expands
   to a 149,516-byte 16-bit FPGA configuration stream with a 32-byte `0xFF`
   preamble and `AA99 30A1` configuration framing; it contains no ColdFire RTS
   words and must not be disassembled as CPU code.
2. **Complete through descriptor-driven runtime conversion:** direct execution
   of initializer `0x4011AE52` proves
   `frame_word = 26 + 42 * track + sound_destination`. The external sound model
   assigns sample BR modulation destination ID 11, so track 0 BR is frame word
   37 / packed-record word 11 at `0x8000F7BE`. A synthetic descriptor passed to
   converter `0x40119672` dynamically reads that exact address. The earlier
   identification of record word 39 / `0x8000F7F6` as BR was wrong; it is a
   separate field used by the 12-track modulation pass. The two four-entry
   banks at `0x8000EA3C` and `0x8000EA4C` remain proven modulation-descriptor
   machinery, not BR-specific banks.
3. **BR smoothing coverage proven; sample quantizer still open:** the 13-record
   EMAC loop at `0x4011C69E` smooths 21 longwords/42 words per record. Its inner
   iteration 5 reads `0x8000F7BC..0x8000F7BF`, which contains track-0 BR at
   `0x8000F7BE`. This is a control-rate smoother, not the audio quantizer.
   Afterward, the descriptor pass applies modulation and the render dispatch
   begins. eDMA channel 54 is
   acknowledged by ISR `0x40118AF2`; callback `0x4011B3AE` observes DMA state,
   and `0x4011CACC` dispatches the renderer and two voice-control passes. The
   generic 16-byte stream-descriptor family at `0x41310D30` supports indices
   `0x00..0x82` and defaults to 48 kHz. Its high-index users `0x81` and `0x82`
   are proven buffer streams, but their playback/record direction is not. They
   are therefore not accepted as Filter 2 insertion points. The ready waits are
   now identified as eDMA TCD completion state: channels 31/32 expose DONE bit
   `0x80` at CSR offset `+0x1E`, while channel 30 exposes ESG bit `0x10` at
   `0xFC0453DE`. MiniColdFire now executes software-started major loops and
   scatter/gather reloads, eliminating those callback stalls.
4. **Complete main mix-stage probe:** calling the stock machine-table initializer at
   `0x4009BB0A`, then supplying the board audio-ready state and the proven
   channel-31/32 TCD pointers, lets callback `0x4011B3AE` reach the end of its
   main mix sequence at `0x4011CB00` in exactly 40,512 modeled instructions.
   All render/control landmarks execute. This address is not the interrupt
   return; the final `RTE` is at `0x4011CF12`.
   A watchpoint sees track-0 BR only once, in the control smoother. The frame
   builder maps source-target longword 18 at `0x8000E5C4` to frame longword 18
   at `0x8000F7BC`, whose low word is BR at `0x8000F7BE`.
5. **DMA roles narrowed; prior section-ID-2 hypothesis rejected:** stock audio
   initializer `0x401178FA` now runs in the emulator and fills the real transfer
   geometry. Channel 30 transfers `17 × 16 = 272` bytes from a 4 MiB modulo
   hardware window beginning at `0x4B7FFFF0`; MAIN does not read its SRAM landing
   buffer, even across repeated callbacks. Channels 31 and 32 each perform eight
   `9 × 16 = 144`-byte external-to-SRAM transfers per callback from
   `0x4F9372E0`. These are ingress/state blocks, not a proven outbound PCM or
   quantizer boundary. A BR-only differential changes the control frame and two
   smoother-state words but no external hardware window in the storage-free,
   inactive-voice model.
6. **MAIN combiner geometry proven:** routine `0x4010A2E0` performs exactly 256
   writes arranged as 32 frames by eight 32-bit lanes, with a 64-byte frame
   stride, into `0x80000800..0x80000FDC`. It consumes three 256-longword source
   planes rooted at `0x80006BF8`, `0x80007040` and `0x800067FC`. The preceding
   external-ingress routine `0x40117F00` populates the `0x800067F8` plane; the
   other two planes persist across this inactive callback and are the current
   upstream sample/render targets.
7. **Stock BR encoder proven in machine renderer 0:** the per-voice dispatcher
   selects renderer `0x4010CBA8` from the 53-entry table at `0x40277FE8`.
   Renderer instruction `0x4010CC58` reads track-0 BR at `0x8000F7BE` and
   caches it. Its bounded five-case control table maps cases 0..4 to
   `0x4010CFD2`, `0x4010D028`, `0x4010D102`, `0x4010D164`, and
   `0x4010D204`; case 3 reads the cached BR at `0x4010D16E`, performs fixed-point
   conversion through `0x4011A0B6`, and packs a BR-dependent word at
   `0x80006544`. Zero/high vectors produce `0x0000`/`0x7E18` BR and distinct
   packed words `0xB31407FF`/`0xB31800B1`. This proves a downstream control
   encoder, not yet the PCM quantization instruction. The following case 1 does
   read that word at `0x4010D08A`, but canonicalizes both synthetic vectors to
   `0xF0100000`; all three source planes and combined output remain identical.
8. **Authentic trigger and full-interrupt progression proven:** startup at
   `0x400A02F8` initializes the real queue object `0x4192A9D0` with a 1,024-entry
   ring at `0x419531F8` through stock helper `0x40001350`; `0x400A0BCC` then
   publishes it through `0x4011CF58`. The zero-start fixture had fallen back to
   sentinel `0x42F4D850`, whose null ring pointer caused the earlier fault.
   Track-0 slot-0's real 56-byte record at `0x42AC4038` now produces event value
   1, flag word `0x80`, and queued command `0x1F` with track mask 1. Continuing
   past the old mix-stage stop reaches the one-shot clear at `0x4011CC64` and
   final `RTE` at `0x4011CF12`. Twelve complete interrupts naturally traverse
   renderer cases `0,1,2,2,2,2,2,2,2,2,3,4`, without forcing event state.
   The associated 84-byte track parameter record begins at `0x412FAC4B`; stock
   copier `0x40119316` maps its longword `+0x14` to live source `0x8000E5C4`.
   A high test value produces nonzero BR on the trigger callback. The earlier
   loss of BR during the ten-callback ramp was an emulator defect, not stock
   behavior: normal EMAC loads were incorrectly masked by the MASK register
   even when the instruction's MAM bit was clear. With ColdFire MAM semantics
   corrected, BR remains live through case 3.
   Further static tracing identifies `0x412FAC37` as a 131-entry sample-slot
   bank with `0x98` bytes per entry; the 84-byte record is embedded at slot
   offset `+0x14`. Trigger-record field `+0x18` selects the slot, metadata near
   slot offset `+0x68` is validated, and stock routines `0x4011AE0C` and
   `0x4011AE52` publish its parameter/modulation state. Loader `0x400341DC`
   copies complete `0x98`-byte records into reserved slots `0x81`/`0x82`.
9. **Control-frame interpolation gate recovered:** trigger flag bit 5 (`0x20`)
   gates publication of interpolation state. With the authentic voice-reset
   bit combined as `0xA0`, instructions `0x4011C616` and `0x4011C61E` consume
   trigger fields `+0x0C` and `+0x10` from live cells `0x8000E4A0` and
   `0x8000E46C`; flag `0x80` alone never reads them. The stock path preserves
   the tested `+0x10` value at `0x80005E10`, derives a rate term at
   `0x80005E44`, advances phase `0x80005E78` by `0x7080` per interrupt, emits
   the stepped coefficient sequence `0`, `0x74822`, `0xE9044` at
   `0x8000E438`, and
   decrements trigger field `+0x20` at `0x8000FC24` by `0x3840`. Twelve complete
   interrupts remain stable, but still produce no nonzero CPU source-plane
   write in the storage-free fixture. This rejects the earlier assumption that
   a complete `0x98`-byte slot alone was the missing activation primitive. A
   dedicated loop at `0x4011C69E` consumes the 13 coefficients at
   `0x8000E438..0x8000E468` while building 13 records of 21 longwords at
   `0x8000F7A8`. The value at `0x8000E438` is therefore a per-track
   control-frame interpolation coefficient—not a sample cursor or PCM pointer.
10. **Physical BR control sink proven:** natural zero/high BR vectors reach case
   3 as `0x0000`/`0x7D2D` and produce packed controls
   `0x001407FF`/`0x0017F942` at `0x80006544`. Stock packetizer `0x40077D14`
   serializes the high vector as tagged words `0x8001B017` and `0x8001F942` in
   the selected ping-pong packet at `0x80005010`. Stock eDMA channel 15 then
   transfers 510 four-byte units (2,040 bytes) from `0x8000501C` to peripheral
   FIFO `0xFC03C034`. The CPU source planes and combined output remain
   bit-identical between vectors. BR is therefore a physical hardware control
   field, not a CPU-side PCM quantizer.
11. **Hardware control link classified as DSPI1:** MCF5441x address
   `0xFC03C000` is DSPI1; the observed sink `0xFC03C034` is its PUSHR transmit
   FIFO and eDMA request 15 is DSPI1 TFFF. Stock setup at `0x4011D7F6` programs
   CTAR0 to 16-bit, CPOL=1, CPHA=1, MSB-first, at internal bus clock divided by
   eight. The 510-longword DMA image is a framed queue: `0x8000AAAA` sync, 492
   continuous CTAR0/PCS0 payload commands (the first clears the transfer
   counter), `0x08005555` end-of-queue, and 16 zero padding commands. MAIN also
   routes DSPI1 through the SDHC alternate-function pins: PCS0 PF2/B13, SOUT
   PG7/B12, SIN PG6/C11 and SCK PG5/A10. Firmware and the processor reference
   manual identify the bus and chip-select exactly, but not the board-level
   device attached to PCS0.
12. **External-audio ingress and correction proven:** routine `0x40117F00` does
   make 520/512/512 reads from `0x8000BC00`, `0x8000C000`, and `0x8000C400`, but
   those regions are not signal planes. Full stock startup copies each one
   byte-exactly from fixed MAIN regions `0x402C0C00`, `0x402C1000`, and
   `0x402C1400` via 256 longword writes at `0x4000081C`; the routine never writes
   them. They are immutable DSP tables. The actual signal dependency enters
   from external window `0x4F9372E0`: eDMA channels 31 and 32 each deliver eight
   144-byte blocks, after which the routine writes 256 longwords at
   `0x800067F8`. With the authentic tables installed, zero external input yields
   zero output; a repeated nonzero external word produces 248 nonzero outputs,
   with only indices `0,32,...224` zero. This proves an eight-by-32
   post-conversion boundary and corrects the earlier three-source-plane model.
   Synthetic amplitude tests are not a simple signed-linear scale, so exact
   fixed-point format and saturation remain deliberately unassigned.
13. **Inert post-conversion detour and next function executed:** the stock call
   at `0x4011CACC` is redirected to eight zero cave bytes at `0x402B4340`. The
   stub executes the untouched `JSR 0x40117F00` and `RTS`; it contains no data
   access or arithmetic. Stock MAIN contains no aligned literal, absolute
   JMP/JSR or relative branch into the used cave range. Under both zero and
   active external-input vectors, the candidate reaches combiner `0x4010A2E0`
   with identical registers, ingress-output hash and complete combined-input
   hash. The combiner then executes all 3,227 modeled instructions, performs
   256 lane-major writes and produces a bit-identical output hash. The lab MAIN
   changes ten byte positions and has SHA-256
   `59cfeddb3364f26332ff093a2c6904797c6a94ed18fbb3a5edfa50ee3ea18a6b`.
   It is intentionally not packaged as ELE3 or SysEx.
14. **Numeric/export contract established under emulation:** the stock loop at
   `0x40118778` performs 128 paired `FROM_MAC` extractions and writes 256
   sequential longwords at `0x800067F8..0x80006BF4`, arranged as eight lanes
   of 32 words. Every extraction uses fractional EMAC mode (`MACSR` values
   `0x28`/`0x29` in the active vector) without the `0x80` saturation-enable
   bit. Controlled values passed through the actual stock extraction/store
   instructions map `+0x80000000` to `0x80000000` and `-0x80000001` to
   `0x7FFFFFFF`: overflow wraps rather than clamps. The plane is therefore a
   signed 32-bit fractional/Q1.31-domain export from 48-bit accumulators. The
   inert detour adds exactly two MiniColdFire semantic instructions before the
   mixer for both zero and active vectors; real processor-cycle margin remains
   a hardware-timer gate, not an emulator claim.
15. **Writable state reservation and first dispatcher executed:** the stub sits
   inside a 26,176-byte zero run at `0x402B41E0..0x402BA81F` in the SDRAM-loaded
   MAIN image. A 496-byte version-1 `F2L2` state ABI is reserved at
   `0x402B4400..0x402B45EF`: 32-byte header, eight 32-byte Filter 2 slots and
   thirteen 16-byte LFO2 slots. Stock contains no detected aligned literal,
   absolute transfer or relative branch into the span and makes no access over
   12 million modeled boot instructions. The retained candidate has flags and
   masks zero. Its dispatcher calls stock ingress, tests the Filter 2 flag and
   takes the disabled fast return; a temporary armed image additionally tests
   lane-mask bit 0 and reaches a dedicated NOP placeholder. Both paths remain
   bit-identical to stock through the next combiner for zero and active input.
   The retained disabled dispatcher MAIN has SHA-256
   `0f8351bcb4821f603265bafdfb1ba071b8c61c148a1ba84d1f11481b3378ba5a`.
16. **Two-stage unity/saturation kernel executed:** an 82-byte target routine
   processes all 32 lane-0 Q1.31-domain words through `s1=sat32(x+0)` and
   `s2=sat32(s1+0)`, writing both state words and the lane output. The armed
   temporary image executes 32 loop iterations, 64 `SATS` operations and 64
   state writes while remaining bit-identical to stock through the mixer for
   zero and active input. Direct overflow cases clamp to `0x7FFFFFFF` and
   `0x80000000`. Working registers and the pre-dispatch status register are
   saved and restored. The retained disabled MAIN has SHA-256
   `ffb560ab0e3b1ababfa175e185976b3fc816d6cacaf2ee250aa5571c60ea5abb`.
17. **First non-unity two-pole response executed:** a 94-byte target routine
   implements two cascaded signed-saturating half-step stages,
   `state=sat32(state + (sat32(input-state) >> 1))`. Target output matches the
   independent fixed-point oracle for impulse, positive DC, negative DC,
   alternating signed limits, zero callback and active callback vectors. The
   armed active path performs 32 iterations, 128 `SATS` operations, 64 state
   writes and 32 lane writes; it changes mixer input/output as intended while
   preserving registers, status and normal combiner execution. Only the
   disabled candidate is retained, with SHA-256
   `45ba0ca490e7dfa030e20d529c8ca356ea9990ce11818b477150fc4b25c1725c`.
18. **State-loaded programmable coefficient and general Q1.31 multiply
   executed:** the 114-byte target routine reads a nonnegative Q1.31
   coefficient from lane-0 state word `0x402B4428`. Its helper performs signed
   32x32-to-64 multiplication followed by an arithmetic 31-bit shift, twice per
   sample. Coefficients `0`, `1/8`, `1/4`, `1/2` and `0x7FFFFFFF`, plus an
   alternating signed-limit vector, match the independent oracle with 64
   multiply calls per 32-sample block. State also remains exact across three
   shared-RAM callback entries (active, zero, active) through the mixer
   boundary. The retained disabled candidate has SHA-256
   `c0a2f13c0e8a082a3cf453079d2e383ca56aeb0b68c698bb22910e961905ec17`.
19. **Shadow control mapping and sample-rate coefficient slew executed:** a
   monotonic 7-bit control maps across `0x00000000..0x7FFFFFFF` using
   `round(control * 0x7FFFFFFF / 127)`. Each target change is divided across all
   32 samples in the callback, with signed division truncated toward zero and
   only the discarded remainder committed after the block. Full-range up/down,
   intermediate up/down and stationary ramps all match the oracle, as do five
   consecutive shared-state callback entries targeting controls
   `16,112,40,96,0`. Thus the full coefficient jump never reaches the first
   audio sample. The retained disabled candidate has SHA-256
   `80e58123c641921e993c044e7eb03488ac3420919a8d739dc8b34c9864190bb4`.
20. **Eight-lane replication and isolation executed:** the original cave now
   holds only a six-byte absolute jump, while a 362-byte extension at
   `0x402B4600..0x402B476A` dispatches the shared slewed two-pole routine across
   the eight 32-word audio lanes and their eight existing 32-byte state slots.
   Every one-hot lane mask performs exactly 32 audio writes, 64 multiplies and
   65 state writes, while all seven unselected audio lanes and complete state
   slots remain unchanged. The full `0x00FF` mask matches eight independent
   fixed-point oracles with 256 audio writes, 512 multiplies and 520 state
   writes. Stock takes 35,362 semantic emulator instructions from callback
   entry to mixer; the full Filter 2 path takes 43,967, a modeled delta of
   8,605 instructions. This is not a hardware-cycle or interrupt-margin claim.
   The retained disabled candidate has SHA-256
   `577e1c1d75cfe76c556b663143786cf200b2488f0c709716deafd7b011f47bb6`.
21. **Foreground control-publication boundary identified and executed:** stock
   routine `0x4011AF4C` is a five-instruction indexed 16-bit setter for the
   572-word target array at `0x8000E57C`; `0x4011AF60` is its four-instruction
   getter. `0x400B5936` is the setter's sole absolute callsite. Eight synthetic
   indices spanning `0..571` each produce exactly one correctly addressed
   two-byte write and round-trip through the getter. In contrast, an authentic
   audio callback reaches the mixer after reading this array 299 times at only
   three frame-builder instructions, performs zero target-array writes, and
   never visits the setter, getter or foreground callsite. Therefore
   `0x400B5936` is the selected interception boundary: a future shim can
   tail-call the untouched stock setter for ordinary indices and publish only
   separately proved Filter 2 commands as aligned Q1.31 shadow longwords,
   leaving mapping work outside the audio callback.
22. **Default-pass-through Filter 2 publication shim executed:** the unique
   foreground callsite `0x400B5936` now targets an 88-byte flag-gated shim at
   `0x402B4780`. When disabled, or when an ordinary `0..571` stock index is
   supplied, the shim tail-calls untouched setter `0x4011AF4C`; tested stock
   writes and disabled return state remain exact. When armed, isolated virtual
   indices `0x7FF8..0x7FFF` select lanes 0..7. A 128-entry table at
   `0x402B4800` maps mouse/QWERTY-friendly controls `0..127` to the proven
   nonnegative Q1.31 coefficient curve. Each command publishes its lane target
   with one aligned 32-bit store. An integrated eight-command run followed by
   an authentic callback performs 512 multiplies and matches all eight filter
   oracles. The retained default-disabled decompressed MAIN has SHA-256
   `25dd3dbfc0276f4da630e2804851d1e542f48aacc9a29cc91242a23ecaf69549`.
23. **Initial desktop control ingress selected and executed:** for emulation,
   the least invasive ingress is a host command bridge that calls the proven
   virtual setter ABI directly. It adds no firmware instructions and keeps both
   UI event processing and Q1.31 mapping outside the audio callback. Mouse
   events publish absolute lane values clamped to `0..127`; `Digit1..Digit8`
   select a lane, arrows apply one-step changes, Page Up/Down apply eight-step
   changes, and Home/End select endpoints. Eleven mixed events produce exact
   final vector `[1,16,32,40,64,80,96,127]`, every event one aligned target
   longword store. A 12-million-instruction storage-free boot leaves UART8
   eDMA channels 34/35 disabled, their TCDs zero and vectors 154/155 at the
   default handler, so UART8 is rejected only as the initial emulator ingress;
   it is not ruled out on hardware.
24. **Local eight-knob controller built and executed:** a dependency-free local
   service now holds one long-lived emulator instance, arms only emulator RAM
   and publishes all eight Filter 2 lanes through virtual indices
   `0x7FF8..0x7FFF`. The rendered control surface supports vertical mouse drag,
   wheel adjustment, arrow/Page Up/Page Down/Home/End keyboard control and
   double-click reset across the full `0..127` domain. Its QWERTY piano sends
   note-on/off events through a separate `/api/note` path; those events do not
   mutate Filter 2 state. Build validation executes eight real shim
   publications with one aligned target store each, exact final vector
   `[0,16,32,48,64,80,96,127]`, complete static assets and no flashable output.
25. **Authentic QWERTY key-down trigger bound under emulation:** stock trigger
   field `0x42AC4040` (`+0x08`) accepts a MIDI-style note as `note << 16`.
   Instructions reported at `0x4011BDA8`, `0x4011BDB6` and `0x4011BDCC`
   read, publish and reread that exact word; live track-0 state
   `0x80006388` preserves vectors 48, 60 and 72 as `0x00300000`,
   `0x003C0000` and `0x00480000`. Each vector enters renderer state 0,
   clears the one-shot and enqueues stock command `0x1F` with track mask 1.
   The controller now routes QWERTY key-down through this exact path in the
   same long-lived emulator used by the Filter 2 knobs.
26. **Authentic QWERTY key-up/release bound under emulation:** stock routine
   `0x401188C6` constructs both event types. Type 1 claims a source bit in the
   per-track bitmap at `0x42AAF558 + track*0x1A0C`; type 2 requires and clears
   that bit, then writes state 2 to the 56-byte trigger record. The callback
   advances the native release through renderer cases `1`, `2`, `3`, `4` and
   idle. The controller now invokes this path on key-up.
27. **Foreground command handler recovered:** generic dequeue routine
   `0x40001444` removes command `0x1F`; the worker loop at `0x400A1120`
   dispatches it to `0x400A19D4`, which obtains object `0x41AA7260` through
   `0x40173AFC` and calls `0x400B5A36(object, track_mask)`. For track mask 1,
   the setter writes object fields `+0x20 = 3` and `+0x50 = 0`. Executing this
   path alone does not activate note-dependent rendering.
28. **Sound Chromatic Mode and pitch consumer proven through DSPI1:** stock
   callback code copies track source byte `0x412FACA1` (record offset `+0x6A`)
   to live mode `0x8000EA18` at `0x4011AEC4`. The full mode matrix matches the
   independently decoded sound format: Off=0, Synth=1, Sample=2 and
   Synth+Sample=3. Off/Sample give the synth renderer fixed note 60;
   Synth/Synth+Sample select bit 0, read live pitch `0x80006388` at
   `0x4011CA7E`, and pass exact `note<<16` as renderer argument 3. Renderer
   `0x4010CBA8` writes pitch-dependent halfwords `0x8000641A/1C` at
   `0x4010CF2C/36`; packetizer `0x40077D14` reads them at `0x40077D64/66`
   into DSPI1 words 46/47. The local QWERTY bridge now selects stock Synth
   chromatic mode and validates the renderer read on every key-down.
29. Separately trace physical MIDI/USB ingress on hardware before binding the
   virtual command ABI to a device transport.
30. Add modes, LFO2 modulation, drive and optional 4-pole cascade.

## Current offline assets

- `research/ar172_extract.py` independently decodes OS SysEx transport, validates
  frame structure, parses ELE3, extracts all four sections, verifies each stream,
  and decompresses UCL NRV2B. It is read-only and cannot emit firmware.
- `research/br_bridge_trace.py` verifies the stock MAIN hash and fixed machine-code
  signatures for the render/bridge call order, the `0x54`-byte record stride,
  physical-voice mapping `[0,4,1,5,8,6,10,2]`, and the 12-track modulation loop.
- `research/AR172_BR_BRIDGE_TRACE.json` is the machine-readable passing trace.
- `research/control_frame_trace.py` proves the packed control frame is exactly
  `572 = 26 + 13 * 42` words, pins BR to destination/record word 11, and
  verifies the four-entry/two-bank modulation descriptor machinery. Its passing output
  is `research/AR172_CONTROL_FRAME_TRACE.json`.
- `research/audio_stream_trace.py` verifies the eDMA-54 audio interrupt,
  callback/render sequence, the 131-record generic buffer-stream table and its
  direct setup/reset callers. Its report deliberately keeps stream direction
  unresolved; passing output is `research/AR172_AUDIO_STREAM_TRACE.json`.
- `research/runtime_descriptor_probe.py` boots MAIN in the MiniColdFire model,
  delivers the installed PIT0 vector 205, and breaks at descriptor initializer
  `0x4011AE52`. The current run executes 1,324,352 scheduled instructions and
  returns to the stock idle task cleanly. A synthetic call to `0x4011AE52`
  completes in 154 instructions and proves destination indices 39..46 from
  input destinations 13..20. The absence of project/storage data is now an
  optional limitation only, recorded in
  `research/AR172_RUNTIME_DESCRIPTOR_PROBE.json`.
- `research/br_consumer_trace.py` proves the 13-by-42-word EMAC smoother covers
  track-0 BR destination 11, then smoke-tests `0x40108944` and `0x40105188` to
  return under emulation. Passing output is
  `research/AR172_BR_CONSUMER_TRACE.json`.
- `research/audio_callback_probe.py` executes a complete stock callback with
  explicit board preconditions, executes stock audio-DMA initializer
  `0x401178FA`, records every landmark and real eDMA transfer geometry, watches
  the BR slot, and differentially proves its frame-builder source.
  Passing output is `research/AR172_AUDIO_CALLBACK_PROBE.json`.
- `research/firmware_section_roles.py` rejects section ID 2 as a runtime DSP from
  its embedded updater/service UI and classifies section ID 1 as a 16-bit FPGA
  configuration stream from exact framing signatures. Passing output is
  `research/AR172_FIRMWARE_SECTION_ROLES.json`.
- `research/render_mixer_probe.py` executes `0x4010A2E0` in callback context and
  proves its three source planes plus 32-frame/eight-lane output geometry.
  Passing output is `research/AR172_RENDER_MIXER_PROBE.json`.
- `research/sample_br_renderer_probe.py` installs stock machine renderer 0 for
  physical voice 0, selects its bounded BR case, proves both the direct frame
  read and BR-dependent packed control word, and completes the whole callback.
  Passing output is `research/AR172_SAMPLE_BR_RENDERER_PROBE.json`.
- `research/trigger_queue_probe.py` reconstructs the stock runtime queue,
  submits the authentic 56-byte trigger record, runs the interrupt through its
  final `RTE`, and proves natural five-case renderer progression plus one-shot
  event cleanup. Passing output is
  `research/AR172_TRIGGER_QUEUE_PROBE.json`.
- `research/sample_state_probe.py` (legacy filename) proves trigger flag bit 5
  gates control-frame interpolation, maps the four trigger fields into live
  SRAM, and measures its natural phase/countdown progression over twelve
  complete interrupts. Passing output is
  `research/AR172_SAMPLE_STATE_PROBE.json`.
- `research/br_hardware_sink_probe.py` follows natural case-3 BR through the
  stock packetizer and eDMA channel 15 into peripheral FIFO `0xFC03C034`.
  Passing output is `research/AR172_BR_HARDWARE_SINK_PROBE.json`.
- `research/dspi1_control_link_probe.py` verifies the stock DSPI1 setup bytes,
  decodes CTAR0 timing and the SDHC-pin route, and proves the exact sync,
  asserted-PCS0 payload, end marker and padding geometry. Passing output is
  `research/AR172_DSPI1_CONTROL_LINK_PROBE.json`.
- `research/post_voice_ingress_probe.py` (legacy filename retained) proves the
  three apparent input planes are stock-initialized immutable DSP tables, then
  differentially traces the real channel-31/32 external-audio dependency and
  eight-by-32 output geometry at `0x40117F00`. Passing output is
  `research/AR172_POST_VOICE_INGRESS_PROBE.json`.
- `research/filter2_bypass_canary_probe.py` builds the inert cave stub, audits
  the stock cave for references, executes both detoured ingress and the next
  stock combiner, and requires register/input/output identity for zero and
  active vectors. Its passing output is
  `research/AR172_FILTER2_BYPASS_CANARY_PROBE.json`; the decompressed execution
  image is `AR172_FILTER2_BYPASS_CANARY_MAIN_DO_NOT_FLASH.bin`.
- `research/filter2_numeric_contract_probe.py` fixes the output-loop signature,
  extraction cadence, fractional mode, wrap behavior and two-instruction inert
  detour overhead. Its passing output is
  `research/AR172_FILTER2_NUMERIC_CONTRACT_PROBE.json`.
- `research/filter2_lfo2_state_canary_probe.py` audits and reserves the shared
  496-byte state ABI in writable MAIN SDRAM. Its passing output and retained
  lab image are `research/AR172_FILTER2_LFO2_STATE_CANARY_PROBE.json` and
  `AR172_FILTER2_LFO2_STATE_CANARY_MAIN_DO_NOT_FLASH.bin`.
- `research/filter2_dispatcher_probe.py` executes both the default-disabled
  flag fast path and a temporary armed lane-0 placeholder path, requiring stock
  equivalence through the combiner. Its passing output and retained lab image
  are `research/AR172_FILTER2_DISPATCHER_PROBE.json` and
  `AR172_FILTER2_DISPATCHER_CANARY_MAIN_DO_NOT_FLASH.bin`.
- `research/filter2_unity_kernel_probe.py` executes a two-stage saturating unity
  kernel, verifies signed clamp boundaries and requires complete stock identity.
  Its passing report and retained disabled image are
  `research/AR172_FILTER2_UNITY_KERNEL_PROBE.json` and
  `AR172_FILTER2_UNITY_KERNEL_CANARY_MAIN_DO_NOT_FLASH.bin`.
- `research/filter2_half_kernel_probe.py` executes the first non-unity two-pole
  response and compares target code against fixed-point impulse/DC/limit and
  callback oracles. Its passing report and retained disabled image are
  `research/AR172_FILTER2_HALF_KERNEL_PROBE.json` and
  `AR172_FILTER2_HALF_KERNEL_CANARY_MAIN_DO_NOT_FLASH.bin`.
- `research/filter2_q31_coefficient_probe.py` replaces the half-step shortcut
  with a state-loaded general Q1.31 multiplier and verifies coefficient sweeps
  plus shared-state callback continuity. Its passing report and retained
  disabled image are `research/AR172_FILTER2_Q31_COEFFICIENT_PROBE.json` and
  `AR172_FILTER2_Q31_COEFFICIENT_CANARY_MAIN_DO_NOT_FLASH.bin`.
- `research/filter2_coefficient_slew_probe.py` defines the 7-bit shadow-control
  mapping and executes monotonic 32-sample coefficient ramps, including
  consecutive callback state. Its passing report and retained disabled image
  are `research/AR172_FILTER2_COEFFICIENT_SLEW_PROBE.json` and
  `AR172_FILTER2_COEFFICIENT_SLEW_CANARY_MAIN_DO_NOT_FLASH.bin`.
- `research/filter2_eight_lane_probe.py` moves the executable kernel beyond the
  reserved state block, dispatches the shared routine across all eight lanes,
  proves every one-hot mask is isolated, and compares the full mask against
  eight independent fixed-point oracles. Its passing report and retained
  disabled image are `research/AR172_FILTER2_EIGHT_LANE_PROBE.json` and
  `AR172_FILTER2_EIGHT_LANE_CANARY_MAIN_DO_NOT_FLASH.bin`.
- `research/filter2_control_publication_probe.py` identifies the unique stock
  indexed target-word setter callsite, executes setter/getter transactions
  across the full target-array index range, and proves the audio callback is a
  reader rather than a writer of that array. Its passing report is
  `research/AR172_FILTER2_CONTROL_PUBLICATION_PROBE.json`.
- `research/filter2_publication_shim_probe.py` builds the foreground
  default-pass-through shim, verifies ordinary stock setter behavior, publishes
  all eight virtual controls through single aligned Q1.31 stores, and executes
  their consumption by the full eight-lane filter. Its passing report and
  retained disabled image are `research/AR172_FILTER2_PUBLICATION_SHIM_PROBE.json`
  and `AR172_FILTER2_PUBLICATION_SHIM_CANARY_MAIN_DO_NOT_FLASH.bin`.
- `research/filter2_host_control_ingress_probe.py` defines and executes the
  first desktop-controller ingress contract against the existing virtual
  setter ABI. Its passing output is
  `research/AR172_FILTER2_HOST_CONTROL_INGRESS_PROBE.json`; it creates no new
  firmware image.
- `research/filter2_lfo2_cutoff_binding_probe.py` binds eight block-rate LFO2
  lanes to separate Filter 2 base/effective cutoff targets while preserving
  mask-off identity. Its report is
  `research/AR172_FILTER2_LFO2_CUTOFF_BINDING_PROBE.json`.
- `research/lfo2_control_publication_probe.py` publishes per-lane enable,
  trigger/free, reset, logarithmic rate, depth and Filter 2 cutoff through the
  proven foreground setter ABI. Its report is
  `research/AR172_LFO2_CONTROL_PUBLICATION_PROBE.json`.
- `research/lfo2_note_trigger_reset_probe.py` attaches phase/output/random-state
  reset to the authentic stock note-on constructor for trigger-mode lanes and
  proves free-mode and note-off preservation. Its report is
  `research/AR172_LFO2_NOTE_TRIGGER_RESET_PROBE.json`.
- `research/lfo2_waveform_mode_probe.py` proves the table-free triangle,
  square, saw and ramp paths plus loop, one-shot, half-shot and hold modes.
  `research/lfo2_extended_waveform_probe.py` completes sine, exponential and
  deterministic random using two locked 256-entry tables. Their reports are
  `research/AR172_LFO2_WAVEFORM_MODE_PROBE.json` and
  `research/AR172_LFO2_EXTENDED_WAVEFORM_PROBE.json`.
- `research/lfo2_controller_sequence_probe.py` drives the same virtual-index
  ABI through the desktop controller across 22 consecutive callbacks. It
  proves dynamic waveform/depth/mode changes, hold/resume behavior, exact
  one-shot and half-shot terminal phases, stable terminal modulation/targets,
  active random-lane disable/resume/reset transitions, and per-block Filter 2
  oracle agreement. Its report is
  `research/AR172_LFO2_CONTROLLER_SEQUENCE_PROBE.json`.
- `controller/filter2_controller_service.py` serves the local control surface
  and owns the long-lived emulator bridge. `controller/static/` contains the
  eight-knob and QWERTY interface, while `controller/validate_controller.py`
  and `controller/test_controller_service.py` provide machine-readable and
  HTTP/API validation. The passing report is
  `controller/AR172_FILTER2_CONTROLLER_BUILD.json`.
- `research/note_pitch_publication_probe.py` executes note vectors 48, 60 and
  72 through the untouched stock trigger path, requiring exact live-pitch
  publication, one-shot clearing, renderer state 0 and the authentic queued
  track-0 command. Its passing output is
  `research/AR172_NOTE_PITCH_PUBLICATION_PROBE.json`.
- `research/note_event_constructor_probe.py` executes stock routine
  `0x401188C6` with its recovered foreground event record. Type 1 encodes
  `note<<16`, claims the per-track source bit and enters renderer case 0; type 2
  requires and clears that ownership bit, emits trigger state 2 and advances
  the native release through cases 1, 2, 3 and into its longer case-4 tail. Its passing output is
  `research/AR172_NOTE_EVENT_CONSTRUCTOR_PROBE.json`.
- `research/note_pitch_consumer_boundary_probe.py` compares authentic notes 48,
  60 and 72 with the four stock Sound Chromatic Mode values. It proves the
  source-to-live mode copy, exact live-pitch read PC and renderer argument,
  renderer control-halfword writes, and packetizer reads into DSPI1 words
  46/47 in
  `research/AR172_NOTE_PITCH_CONSUMER_BOUNDARY_PROBE.json`.
- `research/synth_pitch_encoding_probe.py` executes all 128 MIDI notes through
  one authentic stock trigger callback and traces renderer 0's helper chain.
  Notes 0--29 hit the stock `0xF0000000` low-input floor; notes 30--127 are
  monotonic and obey the integer octave law (`output[n+12]` is `2*x` or
  `2*x+1`). Anchors 48/60/72 yield helper outputs `0x1965F`, `0x32CBF`, and
  `0x6597F`, then control-halfword pairs `19/12`, `38/25`, and `77/51`. The
  two post-exp2 paths are now exact: each channel is
  `clamp_0_7fff(((exp2_output + 1) * scale) >> 31)` with scales `0x62000` and
  `0x4168F`. All 128 observed pairs match. The full result is
  `research/AR172_SYNTH_PITCH_ENCODING_PROBE.json`.
- `research/renderer_control_ownership_probe.py` traces renderer-scoped writes
  across all 34 public machines and forced states 0..4. Its complete 170-context
  matrix is `research/AR172_RENDERER_CONTROL_OWNERSHIP_PROBE.json`.
- `research/whole_callback_control_ownership_probe.py` extends that trace over
  the complete audio callback and verifies that packetizer `0x40077D14` reads
  all 492 payload fields. Its full writer/consumer inventory is
  `research/AR172_WHOLE_CALLBACK_CONTROL_OWNERSHIP_PROBE.json`.
- `research/control_setup_ownership_probe.py` traces modeled machine/audio and
  control-DMA initialization, queue setup, and authentic note-on/note-off
  constructors. Its negative ownership result is captured in
  `research/AR172_CONTROL_SETUP_OWNERSHIP_PROBE.json`.
- `research/control_candidate_absolute_reference_probe.py` indexes exact MAIN
  address literals for the remaining candidates. Its field and cluster map is
  `research/AR172_CONTROL_CANDIDATE_ABSOLUTE_REFERENCE_PROBE.json`.
- `research/control_frame_global_ownership_probe.py`,
  `control_frame_canary_persistence_probe.py`,
  `control_frame_candidate_pair_probe.py` and
  `control_frame_two_word_locality_probe.py` form the track-0 ownership,
  persistence, ranking and exact packet-locality chain. Their corresponding
  `AR172_CONTROL_FRAME_*_PROBE.json` reports preserve every context.
- `research/control_frame_track_lane_probe.py` rejects words 197/198 after a
  logical-track-1 write, and `control_frame_eight_lane_ownership_probe.py`
  extends ownership across the complete physical-voice mapping. Their reports
  are `AR172_CONTROL_FRAME_TRACK_LANE_PROBE.json` and
  `AR172_CONTROL_FRAME_EIGHT_LANE_OWNERSHIP_PROBE.json`.
- `research/control_frame_eight_lane_two_word_locality_probe.py` compares 1,360
  independent stock baselines with 1,360 words-67/68 seeded callbacks. Its
  compact per-lane digest report is
  `research/AR172_CONTROL_FRAME_EIGHT_LANE_TWO_WORD_LOCALITY_PROBE.json`.
- `research/control_frame_static_move_writer_probe.py` rejects callback-silent
  fields that are explicit destinations of stock absolute `MOVE.W` operations.
  Its instruction inventory is
  `research/AR172_CONTROL_FRAME_STATIC_MOVE_WRITER_PROBE.json`.
- `research/control_frame_computed_record_writer_probe.py` closes the remaining
  structural gap. Its report,
  `research/AR172_CONTROL_FRAME_COMPUTED_RECORD_WRITER_PROBE.json`, derives the
  computed 56-by-8-byte record-member writer and the initialized
  80-by-4-byte paired-slot array directly from locked stock instructions.
- `recovered_library/minicoldfire_audio.py` now queues PIT0 when the modeled timer
  fires and implements the ColdFire EMAC transfers/multiply-accumulate subset,
  `SATS`, classic word multiply, correct fractional-product scaling, and the
  register-encoding precedence needed by the stock control and voice paths,
  including `BYTEREV`. Fractional products are signed and implicitly doubled,
  and extension-word bit 8 correctly distinguishes MAC from MSAC; this repairs
  renderer 0's exp2 interpolation. EMAC address masking now follows the instruction MAM
  modifier: normal loads ignore MASK, while masked postincrement uses the old
  address for the load and masks the updated address. It
  also models software-started eDMA transfers and scatter/gather TCD reloads.
- `research/lfo2_filter2_reference.py` defines deterministic host reference models
  and test vectors for all proposed LFO modes and a topology-preserving state
  variable Filter 2.
- Existing Slice16 transactional SHA-256 remains
  `9233da51a2467a7dd0a7b8897af41058e54fa4768e86479c226778a26c7a709f`.

## Safety boundary

No LFO2/Filter 2 `.syx` exists yet. Nothing from this research branch should be
flashed. The required hardware sequence remains:

1. Original stock recovery.
2. Byte-identical stock round-trip.
3. Inert detour.
4. Slice16 transactional candidate.

Only after those gates pass should an isolated LFO2 bypass canary be packaged.

## Immediate next target

The DSPI1 spare-field route is closed: every candidate lies in a stock-managed
structure. LFO2 therefore remains CPU-resident in the versioned shadow-state
extension and modulates the proven CPU-side Filter 2 effective-cutoff path.
All seven waveform shapes and four run modes now execute under emulation. The
nonlinear multi-callback reset/retrigger matrix passes, and the desktop
controller exposes waveform, mode, rate, depth, enable, retrigger and phase
reset without disturbing its QWERTY note path or 0..127 mouse controls. A
22-callback controller-driven sequence also passes exact audio oracles and
proves one-shot clamps at `0xFFFFFFFF`, half-shot clamps at `0x80000000`, and
hold resumes without phase drift. This sequence exposed and corrected an
earlier half-shot immediate encoded as `0x00008000`. Active random-lane testing
also proves three disabled callbacks freeze phase, re-enable resumes it, and
reset clears phase, last modulation, and random index before the next callback.
The desktop state endpoint now reports actual per-lane emulator phase,
increment, depth, modulation, effective target, random index and enable/trigger
masks. The offline `POST /api/step` diagnostic now advances 1..32 authentic
callbacks to the proven pre-mixer boundary and returns callback number,
instruction/multiply counts, phase-before/after arrays and input/output hashes.
The browser's deliberate **Step callback** control executes one callback and
immediately refreshes selected-lane telemetry and the persistent callback
counter. Real-fixture service tests and the machine-readable controller build
validation pass. The next gate is an opt-in bounded continuous-run control with
explicit start/stop semantics; host audio playback remains a later gate.

For Filter 2, state-loaded Q1.31 coefficients, per-sample control slew, all
eight audio/state lanes, the foreground publication boundary, its
default-pass-through virtual command shim, the host/emulator ingress contract
and a working local eight-knob control surface are complete. QWERTY key-down
and key-up now enter the authentic stock track-0 constructor,
pitch-publication and release paths under emulation, with stock Sound Chromatic
Mode set to Synth. The post-exp2 calibration equations producing renderer pitch
halfwords `0x8000641A/1C` are now exact. A complete scan of the 53-entry renderer
table plus stock execution of representative renderer families proves that the
`0x62000 / 0x4168F` pair is not universal: only machine IDs 0 and 1 contain and
reproduce that exact dual-channel calibration. `0x4168F` recurs in machine IDs
0, 1, 13, 21, 22, 26, 30, 35, 36 and 41, sometimes duplicated or placed on
only one channel, while other renderer families emit symmetric or differently
scaled note-dependent values into the same DSPI1 words 46/47. These words are
therefore shared physical-voice control slots with machine-dependent pitch and
channel topology, not two fixed-function globally calibrated rails. The next
offline target is inventorying renderer-owned per-voice fields across every
public machine and renderer state, then ruling control fields in or out for
Filter 2. The public Sound-format machine byte maps directly to renderer-table
indices 0..33: the exact calibration pair belongs to BD Hard and BD Classic.
Entries 34..52 have no public Sound-format names and remain explicitly labeled
internal/reserved rather than being assigned speculative identities. All 34
public machines now execute through three note anchors; HH Lab initially exposed
a ColdFire `EXT.B` decoder collision with the broader `LEA` mask, now fixed and
covered by a register sign-extension regression.

The renderer-control ownership sweep now executes all 34 public renderers under
forced state selectors 0..4: 170 stock contexts total. The packetizer consumes
492 consecutive halfwords from `0x800063C0..0x80006796`, with exact mapping
`word = 1 + (address - 0x800063C0) / 2`; this reproduces pitch words 46/47 and
packed-control words 195/196. Renderers write 117 distinct outbound halfwords
and no halfword is written in every context. Those 117 fields are rejected as
universal Filter 2 transport. The remaining 375 are only *renderer-unobserved*,
not proven spare: they form packet-word ranges 1..45, 50..57, 60..65, 67..70,
73..79, 85..180, 197..221, 281..308, 333..422 and 427..492. The next gate is a
whole-callback writer/consumer inventory over those ranges before any canary is
published into them.
DSPI1 SCK-to-FPGA-CCLK and SOUT-to-FPGA-DIN are established,
while PCS0 and the live FPGA application's interpretation remain unresolved;
physical pin hunting is intentionally out of scope. Physical MIDI/USB ingress
remains a separate hardware trace. Hardware timer measurement is still required
before claiming real-time cycle margin.

The whole-callback ownership sweep extends the same 170 contexts from renderer
entry to the final audio-callback return. It finds 233 fields written outside
the selected renderer and 327 fields written by the callback in total (23 are
written in both scopes). The stock packetizer at `0x40077D14` reads every one of
the 492 payload halfwords at PCs `0x40077D62`, `0x40077D64`, `0x40077D66` and
`0x40077D68`. This rejects another 210 renderer-unobserved fields, leaving 165
payload fields that are read for transmission but not written by any callback
in the tested matrix. They are not yet spare: persistent initialization,
non-note events and operating modes outside this matrix may own them. The next
offline gate is tracing those paths before any inert canary publication.

The currently modeled pre-callback lifecycle does not narrow the 165 fields:
machine/audio preparation, control-DMA initialization, queue initialization and
installation, and the authentic note-on and note-off constructors write zero
packet-source halfwords. This is useful negative evidence, but the SRAM fixture
begins zero-filled and omits earlier board startup. Consequently, zero values in
those 165 fields are not evidence that they are spare. Parameter-change and
other non-note event paths, plus modes outside the 170-context matrix, remain
the next offline ownership targets.

A static exact-literal pass provides the next execution priorities. Seventy-seven
of the 165 fields occur as 255 absolute address literals in stock MAIN, grouped
into 24 neighborhoods. The densest are `0x401040B2..0x401049A0`,
`0x40105B58..0x401063D8` and `0x4011A404..0x4011AA5C`. These may be instruction
operands or data, so each still needs dynamic classification. The 88 fields
without exact literals remain reachable through base-register-relative or
computed addressing and are not cleared by this negative static result.

The first controlled canary chain proved that all 165 track-0 writer-free
halfwords persist from callback entry through packetizer read and final DSPI1
serialization in all 170 machine/state contexts. Pair ranking selected words
197/198, and a separately executed stock-versus-seeded comparison changed
exactly those two packet indices in every context. The subsequent structural
test correctly rejected the pair: logical track 1 dispatches physical voice 2
and writes the repeated 8-byte record beginning at word 197. This established
that track-0 persistence is insufficient for a universal transport claim.

Ownership now covers all eight physical voices and their stock logical-track
mapping `[0,4,1,5,8,6,10,2]`. The expanded sweep executes 34 public machines ×
five forced states × eight mappings, or 1,360 authentic callbacks. Renderer
ownership grows from 117 to 164 fields; combined renderer/non-renderer callback
ownership grows from 327 to 368. Forty-one track-0 false positives are rejected,
including words 197/198, leaving 124 fields with no observed writer across the
eight-lane matrix. These remain software candidates only. The next offline gate
reranks adjacent pairs within this 124-field intersection. Fifty-three fields
have no observed non-packet read, and all 58 adjacent pairs are fully
read-isolated. The deterministic winner is words 67/68 at
`0x80006444/0x80006446`, 20 words from the nearest known control field. The next
gate is bounded eight-lane persistence and exact packet-locality testing of only
that pair. This gate now passes: across 1,360 stock baselines and 1,360 seeded
callbacks, the complete 510-word DSPI1 packet differs at exactly words 67/68 in
every comparison. Values `0xF243/0x0DBC` survive at
`0x80006444/0x80006446` for all 34 public machines, forced states 0..4 and all
eight voice mappings. This proves eight-lane software persistence and exact
serialization locality, not spare FPGA semantics. The next gate is resolving
the surrounding words 67..70 structure and all non-callback initialization or
parameter-event writers before selecting the pair for an inert firmware canary.

That structural gate rejects words 67/68. Each of words 67..70 and 73..79 is
the destination of four explicit `MOVE.W Dn,(absolute-long)` instructions in
stock MAIN, even though none executes in the 1,360 callback contexts. Extending
the same exact-opcode scan over all 124 eight-lane writer-unobserved fields finds
48 fields targeted by 123 concrete writer instructions. Those fields are removed
from consideration. Seventy-six fields survive this specific static screen,
with 13 adjacent pairs; the deterministic next pair is words 281/282 at
`0x800065F0/0x800065F2`. This is not yet a spare-field claim because immediate,
byte/long, base-relative and computed writer forms still require classification.

The computed-address gate rejects that next pair without another 2,720-callback
locality sweep. Stock MAIN first zeroes the complete 492-halfword packet source,
then its loop at `0x4011CD20..0x4011CD40` clears member `+4` in 56 eight-byte
records using
`0x800063C0 + 8 * (21 + counter) + 4`. Words 281/282 are in record 70;
the entire apparent 281..308 hole is seven records from this same array. Across
all remaining fields, 37 intersect fifteen of these records. The final 39
isolated fields are each the companion halfword of an 80-entry four-byte slot
array rooted at `0x80006658`; the constructor at
`0x4011A886..0x4011AA60` explicitly initializes the other halfword of every
slot. Conservatively quarantining stock-managed structures leaves zero spare
field candidates and zero adjacent pairs. This closes the spare-pair search:
future work must use named stock destinations or an explicit versioned
transport extension, not words 281/282.

## External format cross-checks

- NXP's MCF5271 reference manual documents the ColdFire EMAC's four 48-bit
  accumulators, signed fractional data mode, `MACSR` overflow/saturation control
  and fractional result extraction. This is the architectural cross-check for
  the stock loop's Q1.31-domain classification; the firmware trace, not the
  manual, establishes that this particular loop leaves saturation disabled.
  <https://www.nxp.com/docs/en/reference-manual/MCF5271RM.pdf>
- NXP's ColdFire Family Programmer's Reference Manual provides the actual
  EMAC instruction encodings: extension-word bit 8 distinguishes MAC from
  MSAC, and the scale field is ignored for fractional operands.
  <https://www.nxp.com/docs/en/reference-manual/CFPRM.pdf>

- `mischa85/elektron-firmware-tool` independently parses ELE3 containers and
  supports the same UCL decompression family; it is a corroborating tool, not an
  Elektron specification.

- `bsp2/libanalogrytm` commit
  `5638457daeb8359870d3d05b23edee480db89f45` identifies sample BR and exposes
  modulation destinations as per-sound fields.
- `alisomay/rytm-rs` commit
  `a0a19f69fcfd44968f05ab7f0af8195633cb83f3` independently corroborates the
  Analog Rytm SysEx parameter model.
- Public `.arprj` examples from `sm-ll/ER` commit
  `8f2eb0644b906973a17c86488700506035d0a50b` confirm that Transfer project files
  are ZIP containers with a manifest plus an opaque device payload. They remain
  useful for future full project-loader work, but are not needed for the current
  descriptor proof.
