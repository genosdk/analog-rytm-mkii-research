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
- **Filter 2** belongs in the digital sample playback path before the sample DAC.
  The firmware package's section ID 2 loads at `0x07020000` and is the leading
  candidate for that DSP path. This must be proven by disassembly and ultimately
  by hardware behavior.

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

1. **Complete:** decompress every compressed ELE3 section with exact size/hash
   validation. Section ID 2 expands to a 42,302-byte ColdFire image; its four-byte
   prefix is followed by an image addressed in the `0x80000000` region.
2. **Complete through descriptor-driven runtime conversion:** trace BR logical
   parameter 20 from word 30 (`+0x3C`) of the internal `sound_t` subview. That
   subview begins `0x12` bytes into each packed record, so BR is packed-record
   word 39 (`+0x4E`). The 12-track loop at `0x4011C722` invokes converter
   `0x40119672` for two four-entry descriptor banks at `0x8000EA3C` and
   `0x8000EA4C`, driving eight control-frame words per track. Direct execution
   of initializer `0x4011AE52` proves the exact mapping
   `frame_word = 26 + 42 * track + sound_destination`; project ingestion is no
   longer required to resolve the descriptor banks.
3. **First BR consumer proven; sample quantizer still open:** the 13-record
   EMAC loop at `0x4011C69E` smooths 21 longwords/42 words per record. Its
   iteration 19 reads `0x8000F7F4..0x8000F7F7`, the track-0 SRR/BR pair, at
   `0x4011C6C0`. This is a control-rate smoother, not the audio quantizer.
   Afterward, the descriptor pass applies modulation and the render dispatch
   begins. eDMA channel 54 is
   acknowledged by ISR `0x40118AF2`; callback `0x4011B3AE` observes DMA state,
   and `0x4011CACC` dispatches the renderer and two voice-control passes. The
   generic 16-byte stream-descriptor family at `0x41310D30` supports indices
   `0x00..0x82` and defaults to 48 kHz. Its high-index users `0x81` and `0x82`
   are proven buffer streams, but their playback/record direction is not. They
   are therefore not accepted as Filter 2 insertion points.
4. Locate sample fetch/interpolation, existing BRR and DAC handoff routines.
5. Establish sample rate, numeric representation, saturation and available cycles.
6. Insert an exact bypass hook and prove bit-identical output.
7. Insert a single 2-pole low-pass instance on one voice.
8. Expand to eight simultaneous physical voices and sweep worst-case resonance.
9. Add modes, modulation, drive and optional 4-pole cascade.

## Current offline assets

- `research/ar172_extract.py` independently decodes OS SysEx transport, validates
  frame structure, parses ELE3, extracts all four sections, verifies each stream,
  and decompresses UCL NRV2B. It is read-only and cannot emit firmware.
- `research/br_bridge_trace.py` verifies the stock MAIN hash and fixed machine-code
  signatures for the render/bridge call order, the `0x54`-byte record stride,
  physical-voice mapping `[0,4,1,5,8,6,10,2]`, and the 12-track BR conversion loop.
- `research/AR172_BR_BRIDGE_TRACE.json` is the machine-readable passing trace.
- `research/control_frame_trace.py` proves the packed control frame is exactly
  `572 = 26 + 13 * 42` words, reconciles the record and `sound_t` views, and
  verifies the four-entry/two-bank BR descriptor machinery. Its passing output
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
- `research/br_consumer_trace.py` proves the 13-by-42-word EMAC smoother and
  its track-0 SRR/BR read, then smoke-tests `0x40108944` and `0x40105188` to
  return under emulation. Passing output is
  `research/AR172_BR_CONSUMER_TRACE.json`.
- `recovered_library/minicoldfire.py` now queues PIT0 when the modeled timer
  fires and implements the ColdFire EMAC transfers/multiply-accumulate subset,
  `SATS`, classic word multiply and the register-encoding precedence needed by
  the stock control and voice paths.
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

The audio-interface transition model is now implemented. All three poll sites
(`0x40117F16`, `0x40118396`, `0x40118518`) observe bit `0x80` in status word
`+0x1E` of the objects referenced by `0x80005820`/`0x80005824`, and
`0x40117F00` returns in a 17,109-instruction smoke call. The render-side poll at
`0x40109FFE` observes and clears mask `0x10` in TCD30 CSR word `0xFC0453DE`;
the exact peripheral meaning of that bit remains hardware-unproven.

Next, execute the terminal BR read at `0x4011870E` through the stock 32-sample
render loop and capture input/output sample provenance. That point—not the
control smoother, mixer, or unclassified `0x81`/`0x82` streams—is the preferred
Filter 2 insertion boundary. In parallel, LFO2 should reuse the
proven destination equation and update machinery via shadow state; the stock
42-word record remains frozen until persistence and SysEx compatibility are
mapped.

## External format cross-checks

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
