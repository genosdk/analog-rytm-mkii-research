# Analog Rytm MKII Desktop Emulator

## Current target

The desktop emulator runs the unmodified Analog Rytm MKII OS 1.72 MAIN image in a custom QEMU ColdFire/MCF5441x machine model. Elektron firmware is not bundled or redistributed.

## First launch

1. Open `AR MKII Emulator.app`.
2. Choose an official Analog Rytm MKII firmware `.syx` file when prompted.
3. The app extracts/decompresses MAIN locally and boots it in the embedded custom QEMU backend.
4. The emulated firmware presents one dismissible startup modal. Press **NO**
   once to continue to the normal parameter UI.

The desktop profile supplies synthetic calibration plus empty, volatile
factory-storage state. Stock OS 1.72 validates the modeled eMMC identity and
block count, `ekFS`, the empty `MaGj` manifest, and the `SM` version-2 record.
It contains no factory PCM or project sample assignment.

## Proven controls

The desktop bridge currently exposes only panel mappings verified directly against OS 1.72:

- Trig 1–16
- Encoders A–I
- TRIG
- SYN
- SMP
- FLTR
- AMP
- LFO
- YES
- NO

The page keys have been validated by causal changes in the firmware's presented OLED framebuffer.

The main window uses a pixel-registered photographic Photon OS faceplate. Its
OLED opening is replaced at runtime by the firmware's live framebuffer, while
invisible hit regions preserve the validated nine encoders, eight page/action
keys, and sixteen Trigs. Press, focus, and LED state are rendered as independent
overlays rather than being baked into the neutral skin.

The activation sheet is coordinate-identical to the neutral faceplate. Only a
held control's photographic crop is raised, allowing simultaneous Trigs and
their corresponding performance pads to glow independently. Encoder values use
small physical-style cap markers; the selected encoder receives a restrained
orange focus ring.

The nine encoders are mouse-draggable 0–127 knobs; the wheel and keyboard also
change their values. QWERTYUI/ASDFGHJK provide press/release control for Trigs
1–16. A narrow host status strip beneath the hardware keeps the Filter 2
extension visibly separate from the recovered physical panel.

The **FILTER 2** button opens an eight-knob runtime drawer. Each knob controls
one audio lane using drag, wheel, arrows, Page Up/Down, Home/End, and
double-click reset. The drawer is explicitly labeled as an emulator extension,
not a recovered physical-panel page. This mode requires the verified OS 1.72
MAIN; `--no-filter2` boots the selected MAIN untouched and disables the drawer.

## Display

The firmware stores its presented 1 KiB OLED framebuffer as 64x128 row-major MSB data. The desktop frontend rotates that buffer 90 degrees into the physical 128x64 display orientation.

## Architecture

The standalone application contains:

- frozen Python/Tk desktop frontend
- custom `qemu-system-m68k` containing the `elektron-ar-mk2` machine
- QEMU runtime libraries bundled inside the macOS application

The application starts QEMU paused, connects the emulated front-panel UART first, and only then releases the guest CPU. This prevents the initial panel identity query from being lost during host startup.

Each packaged architecture must also pass the frontend's native Tk runtime
self-test. It loads both photographic rasters, constructs all 36 active crops,
composes every control state simultaneously, renders a patterned firmware OLED
surface, clears the overlays, and rejects incomplete or out-of-bounds geometry.
This runs from the signed `.app`, so it validates the same frozen resources and
Tk image path used at launch rather than only inspecting PNG headers. It then
replays pointer presses at all eight button and sixteen Trig hit regions plus a
drag on each encoder A-I, checks the corresponding illumination, and compares
all 57 emitted JSONL records with the production bridge contract.

By default, the launcher derives a temporary, non-flashable Filter 2 + LFO2
runtime candidate from the caller-supplied, hash-verified OS 1.72 MAIN. One
versioned 108-byte snapshot atomically publishes eight lanes of Filter 2,
LFO2 rate/depth, waveform/mode, enable/retrigger masks, and phase-reset
generations to their proven shadow-state cells. The candidate and control
snapshot live only in the temporary runtime directory and are removed when the
app exits.

## Firmware flow already validated

`desktop control -> UART8 -> eDMA -> INTC -> firmware ISR -> parser -> live UI queue -> UI dispatcher -> firmware renderer -> presented OLED framebuffer`

## Limitations

This is an experimental research emulator, not an Elektron product. The modeled
drive is sparse, volatile, and empty; changes disappear when the app exits.
Factory samples, project persistence, the physical analog-control receiver,
and a number of MCF5441x peripherals remain incomplete or unmodeled. Do not
treat emulator behavior as validation that a modified `.syx` image is safe to
flash to hardware.

## Experimental audio-service trace

`--audio` enables a passive host-paced stereo tap at the proven stock renderer
boundary. It follows the firmware's four-block selector at `0x42F78044`, waits
until the selected 2 KiB block is stable, sums the eight physical-voice lanes,
converts the signed renderer words to little-endian 16-bit PCM, and hands the
result to QEMU at 48 kHz. The same mono mix is currently sent to left and right;
the hardware pan/return mapping is not yet proven.

The tap itself remains passive. In desktop mode, `--audio` also enables a
bounded service gate: each rising Trig/pad edge schedules eight stock
vector-191 renderer passes. This proves repeated QWERTY-to-host-PCM operation
without enabling the unbounded research clock. Longer realtime playback is
still blocked on ColdFire TCG throughput.

The Filter 2 + LFO2 drawer exposes the same bounded path as **AUDITION · 8
BLOCKS** when `--audio` is active. It is disabled otherwise, making the audio
backend requirement visible rather than silently changing launch behavior.

`--mock-audio-service` enables a default-off research shim for the external
audio-service clock. After the stock firmware installs INTC1 source 63 at
vector 191, the shim unmasks it and raises its self-clearing force bit on the
existing 10 ms Type-8 cadence. The untouched ISR clears that bit on entry and
reaches the stock audio routine at `0x40117A28`. The shim waits for that clear
and for CPU IPL to return below 5 before issuing another request, preventing
interrupt coalescing from masquerading as sustained renderer progress.

For the bounded trigger gate, a pad edge is delayed by 10 Type-8 ticks so the
native trigger state is visible before source 63 is raised. The shim observes
the native IFR63 clear on entry and CPU interrupt level returning below 5 on
completion. The verified desktop budget is eight 32-frame blocks per pad edge.
QEMU now re-arms eDMA channel 15 when the modeled external audio interface
consumes the DSPI1 transmit FIFO; without that request, firmware waited
indefinitely for DSPI1 SR.EOQF at `0x40077D90` after the first transfer.

The active-retrigger runtime gate uses that delay to prove the native note-on
branch rather than merely traversing it. With lane 0 enabled in trigger mode,
the harness pauses QEMU, seeds nonzero phase, last-modulation and random-index
words through the local debug stub, resumes, and sends the ordinary desktop
Trig 1 event. One atomic monitor snapshot observes all three words cleared
before the first of eight bounded renderer services. The later SMP-page event
and nonzero generated-sample WAV still pass. This instrumentation changes no
candidate bytes and is recorded in
`research/AR172_QEMU_LFO2_ACTIVE_RETRIGGER_GATE.json`.

The subsequent selectivity matrix repeats the same causal test for Trigs 1–8.
Each edge clears exactly its corresponding lane's three words while all 21
words belonging to the other seven lanes retain distinct sentinels. Across the
eight edges, all 192 expected values match, 64 bounded renderer services
complete, generated-sample PCM remains nonzero, and the later SMP page remains
responsive. The matrix is recorded in
`research/AR172_QEMU_LFO2_RETRIGGER_MATRIX_GATE.json`.

The negative-control matrix marks each lane's stock note record as well as all
24 LFO2 state words. Trigs 1–8 in free mode each publish an accepted note-on
without changing any state word. After enabling the held lane's retrigger bit,
each corresponding note-off publishes type 2 and again preserves all 24
words. All 384 preservation comparisons pass, followed by 64 bounded services,
nonzero generated PCM and the SMP page. Evidence is in
`research/AR172_QEMU_LFO2_RETRIGGER_NEGATIVE_GATE.json`.

The explicit desktop-reset matrix drives the drawer's real `lane:reset` JSONL
event and versioned reset-generation byte for every lane. Each first-generation
edge clears exactly the selected phase, last-modulation and random-index triplet
while preserving the other 21 seeded words. Republishing an unrelated lane-8
depth change with all generations unchanged preserves all 24 words, and a
second lane-1 generation clears its triplet again. This yields 240 exact state
comparisons with no false reset; cleanup generations remove the diagnostic
sentinels and the later SMP page remains responsive. A separate fresh-runtime
control on the same candidate completes 64 renderer services with nonzero
generated PCM. Evidence is in
`research/AR172_QEMU_LFO2_EXPLICIT_RESET_GATE.json`.

The packaged QWERTY input lifecycle is regression-gated at the production
`PanelApp` methods. Duplicate key-downs emit no duplicate press, the 12 ms
release delay absorbs the release/press pair generated by host key repeat, and
a true release is delivered once. Mouse and keyboard can own the same pad
concurrently; focus loss cancels pending callbacks and releases every active
pad exactly once. Replaying two cycles through the production bridge yields
exact UART8 frames `23 01`, `23 00`, `23 01`, `23 00`. Evidence is in
`research/AR172_DESKTOP_QWERTY_LIFECYCLE_GATE.json`.

The two-key overlap gate composes that frontend behavior with live QEMU.
Holding Q, adding W, releasing Q and finally releasing W publishes group-3
masks `01 → 03 → 02 → 00`. Atomic snapshots of both native stock note records
move from seeded sentinels through `on/sentinel → on/on → off/on → off/off`,
with no cross-lane clobber. The subsequent SMP page remains responsive.
Evidence is in `research/AR172_QEMU_QWERTY_CHORD_GATE.json`.

The complete packaged keyboard matrix is also replayed through the production
JSONL bridge. QWERTYUI maps exactly to Trigs 1–8 as group-3 masks `01` through
`80`, and ASDFGHJK maps to Trigs 9–16 as group-2 masks `01` through `80`; every
key's release returns only its owning group to `00`. A simultaneous Q+A test
then proves that releasing either key leaves the other UART group untouched,
with exact frames `23 01`, `22 01`, `23 00`, `22 00`. Evidence is in
`research/AR172_DESKTOP_QWERTY_FULL_MATRIX_GATE.json`.

This option remains a research clock rather than a physical realtime claim.
The backpressured 10 ms model has sustained 2,303 completed services while the
UI remained responsive, but the physical device cadence has not yet been
measured. Without the option, emulator behavior is unchanged.

## Local controller audition render

The browser controller's **Render selected note** action is separate from
QEMU's realtime `--audio` backend. It executes at most 32 callbacks, drives the
chosen lane with a generated sine at the most recent QWERTY pitch, processes it
through Filter2/LFO2 and stock mixer `0x4010A2E0`, and returns the selected
mixer-output lane as a repeated 0.75-second 48-kHz stereo WAV. All 256 mixer
writes are checked on every callback, and the renderer's proven six guard bits
are removed before signed 16-bit conversion. Other stock source planes remain
fixture-dependent and the UI labels the generated-source boundary directly.
