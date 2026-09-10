# Analog Rytm MKII OS 1.72 Research

Public reverse-engineering workspace for the Analog Rytm MKII OS 1.72 research project.

## Current milestone

The firmware transport/container path is understood well enough to decode, modify,
recompress, checksum, and re-encode OS 1.72. The stock sample Bit Reduction control
path is now traced from the packed track parameter through the machine renderer and
out to the external audio hardware transport.

A critical correction supersedes earlier notes: the arithmetic block around
`0x4011870E..0x401187A4` is **not** the sample BR quantizer. The true sample BR field
is track destination/record word 11 (`0x8000F7BE` for track 0). MAIN encodes that
parameter into a per-voice control word and serializes it through DSPI/eDMA; the final
hardware-side amplitude quantization law remains to be measured on a physical Rytm.

A first functional Sample Rate Reduction research image still exists, but its original
BR-selector rationale was based on the superseded `0x4011870E` interpretation and it
must not be treated as a validated BR implementation.

## Safety gate

Do **not** jump directly to a functional custom image on hardware. The staged hardware
sequence is:

1. Verify normal boot and disposable project baseline.
2. Verify startup-menu recovery with original Elektron OS 1.72 over physical MIDI.
3. Characterize stock BR on stock OS 1.72.
4. Verify the byte-identical stock round-trip image.
5. Verify a checksum-correct modified MAIN image is accepted.
6. Verify the inert code-cave detour boots and behaves normally.
7. Only then test functional feature candidates on one disposable sample/track.

See `docs/AR172_FIRST_HARDWARE_TEST_PROTOCOL.md`.

## Repository policy

This repository intentionally excludes:

- Elektron's original `.syx` firmware.
- Modified/custom `.syx` firmware images.
- Raw extracted proprietary firmware sections.

Those files belong in local `private/` or `build/` directories, which are ignored by Git.
The repo stores only original research notes, patch locations, hashes, validation reports,
and reproducible tooling.

## Key results

- Sample Slot is parameter `0x29` in Q8 format. Index 0 is `OFF`; the picker
  domain is `OFF + slots 1..127`, with blank slots rendered as `---` from the
  name-pointer table at `0x41928DCC` and packed metadata at `0x419289CC`.
- OS package: ELE3 over Elektron SysEx transport.
- Device ID: `0x0C`.
- MAIN load address: `0x40000400`.
- MAIN decompressed size: `2,903,032` bytes.
- Stock MAIN SHA-256: `5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772`.
- Stock OS 1.72 SysEx SHA-256: `1ea60357abe8b876d8b9c52e6dcd988d833478a49d09e3cb22d42782ef822b2f`.
- Corrected cave candidate: `0x402B4200`.
- Sample BR destination/record word: `11`.
- Track-0 sample BR address: `0x8000F7BE`.
- Machine-0 renderer: `0x4010CBA8`.
- Direct BR frame read: `0x4010CC58`.
- BR control encode path: case 3 at `0x4010D164`, cached read at `0x4010D16E`.
- Per-voice BR control word: `0x80006544`.
- Reconstructed CPU command law:
  `0xB31407FF + ceil(BR * 0x40000 / 127)` for BR `0..127`.
- BR 0 endpoint: `0xB31407FF`.
- BR 127 endpoint: `0xB31807FF`.
- The command is packetized by the `0x40077Dxx` path and submitted through eDMA
  channel 15 to DSPI1 PUSHR; MAIN does not show a proven BR-dependent PCM mask/shift.
- DSPI1 sends 16-bit payloads as `0x8001xxxx` PUSHR entries: `CONT=1`, PCS mask
  `0x01`; the BR/control link is therefore PCS0, SCK, SOUT and SIN.
- Direct XC3S200A/VQ100 IOB-bit extraction classifies all 68 BOND57 user pins.
  A subsequent IOI/INT first-hop decode rejects the earlier P28-P31 locality
  hypothesis: none has a selected fabric consumer and P29 `MUX_O` is `NONE`.
  The DSPI package pins remain unresolved pending dedicated-clock and continuing-net
  tracing.
- Section ID 2 is the temporary ColdFire bootstrap/updater, not the runtime sample DSP.
- Section ID 1 is an FPGA configuration stream, not ColdFire code.
- Renderer combiner `0x4010A2E0` writes 32 frames × 8 lanes and consumes three
  256-longword source planes.
- The emulator-generated slot-1 sample now follows the stock setter, trigger,
  registry, resampler and renderer path: 32 nonzero voice-slab longwords become
  18 nonzero words in the live renderer ring at `0x80001800`.
- The live external-audio channel-30 chain contains 18 ESG-linked TCDs. The
  QEMU model now decodes ELINK counts and follows DLASTSG descriptors, allowing
  the stock audio ISR to return from `0x40109F04` instead of spinning.
- The opt-in desktop audio tap follows selector `0x42F78044`, waits for a stable
  completed renderer block, mixes its eight signed lanes, and supplies 48 kHz
  stereo PCM through QEMU's paced host-audio backend.
- Desktop pad/QWERTY rising edges now schedule eight bounded vector-191 renderer
  services. Re-arming DSPI1's channel-15 transmit request at each external
  audio event prevents the firmware's EOQ wait from stalling repeated blocks.
- The default-disabled Filter 2 lab detour now has an eight-lane Q1.31 kernel,
  per-sample coefficient slew, one-hot lane isolation, and exact stock bypass.
  Virtual indices `0x7FF8..0x7FFF` publish mouse-friendly `0..127` controls to
  the eight lane targets outside the audio callback.
- The local controller exposes eight mouse/wheel/keyboard knobs and QWERTY
  notes `A W S E D F T G Y H U J K` (notes 48..60). Note-on and note-off both
  execute through the recovered stock note-event constructor in the emulator.
- Renderer-scoped execution across all 34 public machines and five forced
  states covers 170 stock contexts. It identifies 117 of the packetizer's 492
  payload halfwords as renderer-owned, with no universal field; the other 375
  still require whole-callback writer/consumer rejection before use.
- Whole-callback tracing identifies 233 non-renderer-written fields and 327
  written fields in total. The stock packetizer reads all 492 payload fields;
  165 read-but-not-callback-written fields remain for initialization and
  non-note-event ownership tests.
- The modeled machine/audio initialization, control-DMA and queue setup, and
  authentic note-on/note-off constructors write none of those 165 fields. This
  negative result preserves them as unobserved—not as proven-spare—candidates.
- Static MAIN cross-references find 255 exact address literals for 77 of the 165
  candidates, grouped into 24 neighborhoods that now prioritize dynamic
  parameter/event tracing; 88 may still use base-relative/computed addressing.
- Track-0 canaries survive into all 170 stock packets, but the ranked 197/198
  pair is rejected by logical track 1. A full eight-lane sweep now covers 1,360
  machine/state/voice contexts, expands callback ownership to 368 fields, and
  leaves 124 eight-lane writer-unobserved candidates. Fifty-eight adjacent
  pairs are also read-isolated; the new ranked winner is words 67/68. Across
  1,360 stock and 1,360 seeded executions, those are the only packet words that
  change in every eight-lane machine/state comparison. Static structure then
  rejects them: 48 of the 124 fields have 123 explicit absolute `MOVE.W`
  writers. The 76 fields left by that opcode screen are all contained in two
  additional stock-managed structures: 37 intersect a 56-by-8-byte array with
  a computed indexed byte writer, and the other 39 are companion halfwords in
  an explicitly initialized 80-by-4-byte slot array. Under the conservative
  structure rule, no spare-field candidate—and no adjacent pair—survives.
- The opt-in continuous research clock now applies interrupt backpressure and
  has sustained 2,303 completed native services while accepting later UI input.
  Its provisional 10 ms period is not yet a claim of physical-device cadence.
- A BR-low/high test with deterministic nonzero CPU render planes produces identical
  CPU PCM/combined output while the hardware control word diverges.

## Stock BR hardware characterization

`research/br_hardware_characterize.py` is the active measurement harness.

It can:

- generate deterministic 48-kHz BR test WAVs,
- automate BR `0..127` over MIDI CC 26,
- trigger one diagnostic sample per setting into a continuous recording,
- analyze the capture against uniform quantizer models,
- infer candidate bit-depth plateaus and rounding/truncation behavior,
- correlate each measured setting with the reconstructed CPU/DSPI command word.

Run:

```bash
python research/br_hardware_characterize.py self-test
python research/br_hardware_characterize.py stimuli build/br_hw/stimuli
```

The software self-test currently passes and exactly recovers a synthetic 6-bit
truncate-to-zero quantizer. The real AR MKII hardware-side law remains unproven until
physical capture data is supplied.

See `docs/AR172_BR_HARDWARE_CHARACTERIZATION.md`.

## Other feature tracks

### Slice16

The transactional Slice16 candidate remains the preferred first functional feature test
after stock recovery and inert-detour gates. It is independent of the corrected BR
architecture.

### SRR

The existing functional SRR research image remains hardware-unverified. Its BR-overload
selector concept must be redesigned before it can be considered production architecture,
because the old selector was attached to the misidentified `0x4011870E` path.

### LFO2 / Filter 2

The emulator-side Filter 2 path is implemented and mechanically proved from its
foreground control-publication shim through all eight renderer lanes. Disabled
operation remains stock-equivalent. Physical-device cycle margin and a safe hardware
activation sequence remain unproved, so no flashable image is produced.

The CPU-side LFO2 path now shares that publication/state ABI across all eight
lanes and implements triangle, sine, square, saw, ramp, exponential and
deterministic-random waveforms. Loop, one-shot, half-shot and hold modes are
executable; explicit reset and authentic trigger-mode note-on restart phase and
the random sequence. The disabled callback remains bit-identical to stock.

The detailed evidence and memory map are in
`docs/AR172_LFO2_FILTER2_RESEARCH.md`.

## Local Filter 2 controller

Place the stock decompressed MAIN at the ignored path
`research/extracted_stock_nrv/section_3_id_3.decompressed.bin`, then run:

```bash
python controller/validate_controller.py
python controller/filter2_controller_service.py
```

Open `http://127.0.0.1:8765`. The service creates only a temporary runtime
candidate and never writes an ELE3 container, SysEx package, or flashable image.

## Railway dashboard

The included `app.py` is a zero-dependency read-only status dashboard suitable for a
private Railway service. It serves `/health` and does not serve firmware files.

```bash
python app.py
```

Railway can detect the included Dockerfile automatically. Do not enable public networking
unless you intentionally want the research dashboard exposed.
