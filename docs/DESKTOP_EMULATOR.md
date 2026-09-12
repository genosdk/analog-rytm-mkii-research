# Analog Rytm MKII Desktop Emulator

## Current target

The desktop emulator runs the unmodified Analog Rytm MKII OS 1.72 MAIN image in a custom QEMU ColdFire/MCF5441x machine model. Elektron firmware is not bundled or redistributed.

## First launch

1. Open `AR MKII Emulator.app`.
2. Choose an official Analog Rytm MKII firmware `.syx` file when prompted.
3. The app extracts/decompresses MAIN locally and boots it in the embedded custom QEMU backend.
4. The emulated firmware currently presents a finite chain of four warnings because persistent storage/calibration hardware is not yet modeled. Press **NO** four times to continue:
   - improved tuning/calibration prompt
   - `+DRIVE ERROR 10`
   - missing synth calibration / factory samples
   - analog calibration missing
5. The normal parameter UI is then usable.

## Proven controls

The desktop bridge currently exposes only panel mappings verified directly against OS 1.72:

- Trig 1–16
- Encoders A–H plus the separate Level/Data encoder I
- TRIG
- SYN
- SMP
- FLTR
- AMP
- LFO
- YES
- NO

The page keys have been validated by causal changes in the firmware's presented OLED framebuffer.

The computer keyboard maps `QWERTYUI` to Trigs 1–8 and `ASDFGHJK` to
Trigs 9–16. Click and drag one of the eight A–H function knobs vertically, or
use the mouse wheel, to send native relative encoder movement. The displayed
values are refreshed from native firmware storage on all six pages. SYN, SMP,
FLTR, AMP, and LFO use the live Q8 track bank; TRIG uses its packed kit-record
fields. Encoder I is displayed separately as Level and follows the firmware's
32-bit selected-track index into the exported 13-track native Q8 level bank.
Trig presses 1–12 select native track indices 0–11, so mouse and QWERTY
triggering also move the Level readback to the corresponding track.

Audio and the guarded emulator-generated `QEMU TEST` provider are enabled by
default, including when the macOS app is opened by double-clicking. After the
normal UI appears, select a track and click **LOAD TEST**. The desktop opens SMP
and sends the proven four
`Encoder D +8` frames; the untouched stock setter assigns Sample Slot 1. Hold
the matching QWERTY trigger to hear the generated sample through the host audio
tap. Renderer service continues while the native pad bitmap remains asserted;
key-up completes a bounded eight-block release tail. Launch with `--no-audio` to
disable the host tap, bounded trigger service, and test-sample provider.

## Display

The firmware stores its presented 1 KiB OLED framebuffer as 64x128 row-major MSB data. The desktop frontend rotates that buffer 90 degrees into the physical 128x64 display orientation.

## Architecture

The standalone application contains:

- frozen Python/Tk desktop frontend
- custom `qemu-system-m68k` containing the `elektron-ar-mk2` machine
- QEMU runtime libraries bundled inside the macOS application

The application starts QEMU paused, connects the emulated front-panel UART first, and only then releases the guest CPU. This prevents the initial panel identity query from being lost during host startup.

## Firmware flow already validated

`desktop control -> UART8 -> eDMA -> INTC -> firmware ISR -> parser -> live UI queue -> UI dispatcher -> firmware renderer -> presented OLED framebuffer`

## Limitations

This is an experimental research emulator, not an Elektron product. Persistent drive/calibration/factory-sample state, audio hardware, LEDs and a number of MCF5441x peripherals remain incomplete or unmodeled. Do not treat emulator behavior as validation that a modified `.syx` image is safe to flash to hardware.

## Experimental audio-service trace

The default desktop mode enables a passive host-paced stereo tap at the proven
stock renderer boundary. It follows the firmware's four-block selector at
`0x42F78044`, waits
until the selected 2 KiB block is stable, sums the eight physical-voice lanes,
converts the signed renderer words to little-endian 16-bit PCM, and hands the
result to QEMU at 48 kHz. The same mono mix is currently sent to left and right;
the hardware pan/return mapping is not yet proven.

The tap itself remains passive. The default desktop mode also enables a guarded
service gate and exposes the explicit **LOAD TEST** action. A rising Trig/pad
edge schedules eight stock vector-191 renderer passes. If the native pad bitmap
is still asserted when that budget drains, the emulator replenishes one block
at a time from the SSI-derived timer. The final native release replaces any
reserve with exactly eight blocks, after accounting for an in-flight service.
Keyboard auto-repeat does not manufacture another rising edge.

The held-key smoke gate used a 4,096-frame generated sample so its first 100
measured services all contained active sample data. It completed those services
in 0.694 seconds (144.183 services/s), observed native key-up at service 106,
stopped at service 114 after the eight-block tail, produced nonzero host PCM,
and retained responsive SMP-page rendering. Real-time 48 kHz needs 1,500
32-frame services/s, so this is a control-semantics result, not a real-time
playback claim; ColdFire TCG is still about 10.4 times short in this run.

A separate quiet performance mode now matches the packaged desktop's
`guest_errors` logging level instead of enabling per-transfer `unimp` traces.
With a 48,000-frame generated sample held for 10 seconds, the unmodified
ColdFire translation path completed 1,711 services (171.09 services/s), still
8.77 times short of the 1,500-service/s real-time requirement. Short one-second
runs varied from 147.72 to 166.83 services/s on the shared runner, so the
10-second result is the characterization value rather than a hard platform
benchmark.

The quiet gate can also save the first stable nonzero 2 KiB renderer block to a
temporary local file. That capture verifies the native path remains nonzero,
but its hash is not a universal output oracle: two timing-dependent first-block
states were observed while the sample envelope was starting. No PCM capture is
stored in this repository.

Inlining the fractional multiply helper and two forms of instruction-level MAC
fusion were tested locally against this gate. None produced a material gain;
one fusion changed the captured output and was rejected, while the
output-preserving versions remained within measurement noise. The target-side
experiments were reverted. A useful real-time accelerator therefore needs a
larger, differentially validated kernel or ISR boundary, not another individual
MAC helper rewrite.

That next boundary is now captured by `qemu/plugins/ar_audio_contract.c`. One
stock invocation of `0x401184C4..0x401187FF` exposed 29 registers at each
boundary and 1,009 ordered data accesses (791 loads and 218 stores across 97
instruction PCs), returning through `0x40117FC2`. The companion comparator has
strict value and address-topology modes and refuses incomplete captures.

Two independent boots reproduced the counts but not identical entry state or
address topology. QEMU record/replay attempts also timed out before native
sample assignment. Therefore these counts characterize the boundary but do not
authorize replacement. The immediate acceleration gate is an identical entry
snapshot or same-process shadow execution followed by strict native/candidate
comparison. Runtime traces remain local and were deleted after aggregation.

The identical-state mechanism is now implemented and proven against an
original, non-proprietary ColdFire control fixture. `ar_audio_shadow.c` uses one
call for footprint discovery, snapshots the next call at entry, executes it,
restores the state in-process, and repeats the same kernel on the same vCPU. The
control matched all 29 registers, three ordered accesses, and all eight touched
bytes at exit. It then restored the native exit state before resuming normal
guest execution.

This proves the shadow harness, not the stock audio kernel: the latter remains
`READY_PENDING_LOCAL_MAIN_RUN`. A stock run must pass the same fail-closed
checks before this boundary can validate an accelerator.

`--mock-audio-service` enables a default-off research shim for the external
audio-service clock. After the stock firmware installs INTC1 source 63 at
vector 191, the shim models SSI1 transmit-FIFO demand every 666.667
microseconds, derived from 32 renderer frames at 48 kHz. eDMA channel 54 drains
one 2,048-byte block to SSI1; its untouched completion ISR acknowledges channel
54 and software-forces source 63. The source-63 ISR then reaches the stock audio
routine at `0x40117A28`. The shim waits for its acknowledgement
and for CPU IPL to return below 5 before issuing another request, preventing
interrupt coalescing from masquerading as sustained renderer progress.

For a short trigger, a pad edge is delayed by 10 Type-8 ticks so the native
trigger state is visible before source 63 is raised. A held pad uses the
SSI-derived timer after its initial budget; its release tail returns to the
Type-8 scheduler for UI headroom. The shim observes
the native IFR63 clear on entry and CPU interrupt level returning below 5 on
completion. The verified release budget is eight 32-frame blocks.
QEMU now re-arms eDMA channel 15 when the modeled external audio interface
consumes the DSPI1 transmit FIFO; without that request, firmware waited
indefinitely for DSPI1 SR.EOQF at `0x40077D90` after the first transfer.

The cadence is now derived downstream from the stock SSI1 configuration:
CCR `0x00056F00` requires a 98.304 MHz SSI clock for its 24.576 MHz bit clock,
16 32-clock I2S slots, and 48 kHz frame rate. The native SSI1/eDMA54 chain sustained 8,732 completed
services, delivered a
nonzero triggered renderer block to the host tap, and retained responsive UI.
The upstream bootloader-established CDRH/PLL handoff is still missing. Without
the option, emulator behavior is unchanged.
