# AR MKII custom-firmware first hardware test protocol

## Purpose

Separate the remaining hardware-only questions in order:

1. Does stock recovery work normally?
2. What is the exact stock hardware-side Bit Reduction transfer function?
3. Does our byte-identical stock round-trip behave normally?
4. Does the bootloader accept a modified, checksum-correct MAIN image?
5. Does the corrected cave at `0x402B4200` execute safely via an inert detour?
6. Does the preferred Slice16 transactional build produce the expected sample boundaries?

Do not skip stages.

## Required before custom firmware

- Original Elektron `Analog-Rytm_MKII_OS1.72.syx`
- Elektron Transfer installed
- Physical MIDI interface with MIDI OUT -> AR MKII MIDI IN for startup-menu recovery
- USB cable for normal Transfer operation
- Disposable project/pattern and disposable sample
- Main outputs/phones turned down before TEST/recovery screens

Startup-menu recovery requires physical MIDI; do not depend on USB MIDI for that recovery path.

## Artifact hashes

| Artifact | SHA-256 |
|---|---|
| Original Elektron 1.72 | `1ea60357abe8b876d8b9c52e6dcd988d833478a49d09e3cb22d42782ef822b2f` |
| Byte-identical stock round-trip | `1ea60357abe8b876d8b9c52e6dcd988d833478a49d09e3cb22d42782ef822b2f` |
| Inert detour | `be90357de1e2857769a08d2a2542b0a2fd8c31c446fc029a32126c180eb9009e` |
| Preferred Slice16 transactional | `9233da51a2467a7dd0a7b8897af41058e54fa4768e86479c226778a26c7a709f` |
| Fallback Slice16 zero-persistence | `0d0a508c409ea1d19da76b9f9d2d1813704bc808bd44a00cb19ea90a8d533fba` |

## Stage A — stock baseline

1. Power on normally.
2. Record displayed OS version.
3. Load one disposable project/pattern and one known sample on one track.
4. Confirm START, END, LOOP, p-locks, playback, pads, sequencer, MIDI/USB and audio outputs.
5. Power-cycle and repeat a minimal playback check.

## Stage B — prove recovery before custom firmware

1. Connect physical MIDI interface OUT -> AR MKII MIDI IN.
2. Hold FUNC while powering on to enter STARTUP MENU.
3. Select the on-screen **OS UPGRADE** item. Follow the displayed item rather than relying on a hard-coded trig number.
4. Send the **original Elektron 1.72** file through the startup-menu/SysEx OS-upgrade path.
5. Confirm successful transfer, reboot and normal operation.

If stock recovery cannot be completed, stop. Do not flash a modified image.

## Stage B2 — characterize stock Bit Reduction hardware

This stage uses **stock OS 1.72 only** and does not require a custom firmware image.

The current reverse engineering proves that MAIN reads sample BR at `0x8000F7BE`, encodes it into a per-voice control command and serializes that command through DSPI/eDMA to external audio hardware. The final hardware-side amplitude quantization law is not yet proven.

Use:

- `docs/AR172_BR_HARDWARE_CHARACTERIZATION.md`
- `research/br_hardware_characterize.py`

Minimum sequence:

1. Run `python research/br_hardware_characterize.py self-test` and require `PASS`.
2. Generate the deterministic stimuli.
3. Load `br_ramp_full.wav` into one disposable sample track.
4. Prefer an Overbridge/digital individual-track capture with fixed gain and no processing.
5. Use MIDI CC 26 to sweep BR. The harness automates all 128 values.
6. Analyze the continuous WAV capture.
7. Repeat at least one shortened sweep and verify the inferred mapping repeats.
8. Do not call truncation/rounding bit-exact from an analog-only capture.

This stage should be completed before designing any replacement BR algorithm. It does not block the safety proof for an inert code-cave detour, but it is the stock reference for all later SRR/BR work.

## Stage C — byte-identical stock round-trip

File: `AR172_stock_roundtrip.syx`

1. Verify SHA-256 equals the original Elektron file.
2. Transfer normally.
3. Confirm reboot and baseline operation.

Expected: behavior is exactly stock because the files are byte-identical.

## Stage D — inert detour

File: `AR172_INERT_DETOUR_SAFE_CAVE_DO_NOT_FLASH.syx`

This changes control flow only:

- `0x4011C312` -> `0x402B4200`
- cave executes the exact displaced stock instruction
- cave returns to `0x4011C318`

### Test

1. Keep the physical-MIDI recovery setup immediately available.
2. Transfer the inert build once.
3. If the device refuses it before flashing, record the exact message and stop.
4. If accepted, confirm boot, display/buttons, pads, sample playback, START/END, sequencer, p-locks and audio.
5. Power-cycle once and repeat the short baseline.

Do not proceed to Slice16 unless Stage D is completely clean.

## Stage E — preferred Slice16 transactional build

File: `AR172_SLICE16_TRANSACTIONAL_LAB_DO_NOT_FLASH.syx`

Prototype behavior:

- START `0–104`: stock behavior
- START `105`: Slice 1
- START `106`: Slice 2
- ...
- START `120`: Slice 16

Internally, selectors 105–120 are replaced only during the control-frame pack with equal boundaries of width `0x0780`; the original START/END values are restored immediately afterward.

### E1 — minimal manual test

Use one long, easily recognizable sample on one track only. LOOP off. END 120. No LFO, retrig, slide, sound lock or other START/END modulation.

1. Set START 104 and trigger the pad. Confirm normal near-end START behavior.
2. Set START 105 and trigger. Expected: playback jumps to the **first 1/16** of the sample, not the near-end position.
3. Set START 106. Expected: second 1/16.
4. Set START 120. Expected: final 1/16.
5. Return START below 105 and confirm stock START behavior resumes.

If these mappings are wrong, stop and record exactly what is heard/displayed. Do not add more variables.

### E2 — first p-lock test

1. Create four trigs on one track.
2. Lock START to `105`, `106`, `107`, `108` respectively.
3. Expected: slices 1, 2, 3, 4 play sequentially.
4. Extend to a 16-step pattern with START locks 105–120 only after the four-step test is clean.
5. Confirm stopping playback and returning START below 105 restores ordinary behavior.

### E3 — basic regression after Slice16 works

Check:

- un-locked START below 105
- END editing
- LOOP off/on
- sample slot changes
- BRR
- ordinary non-sample p-locks
- two tracks playing simultaneously

Do not stress-test all 13 tracks until single/two-track operation is stable.

## Stage F — fallback only if needed

File: `AR172_SLICE16_ZERO_PERSISTENCE_LAB_DO_NOT_FLASH.syx`

This candidate never writes live START/END RAM, but it inserts a hook inside the 286-iteration MAC/EMAC conversion loop. Use it only if the transactional build shows a repeatable artifact consistent with transient START/END visibility while the inert build remains clean.

Do not use the fallback merely because the transactional build has an audible endpoint/click issue; both builds use the same synthetic slice boundaries, so that would more likely be an endpoint-semantics issue.

## Stop conditions

Immediately stop custom-firmware testing for any of the following:

- boot loop or repeated watchdog/reset
- startup menu unavailable
- corrupted display/UI
- persistent audio noise or severe glitches
- project/pattern corruption
- pads/buttons becoming unresponsive
- device becoming unusually hot
- firmware accepts transfer but cannot complete normal reboot

If normal boot fails but STARTUP MENU remains available, restore the original Elektron 1.72 through physical MIDI.
