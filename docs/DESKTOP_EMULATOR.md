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

This option remains a research clock rather than a physical realtime claim.
The backpressured 10 ms model has sustained 2,303 completed services while the
UI remained responsive, but the physical device cadence has not yet been
measured. Without the option, emulator behavior is unchanged.

## Local controller audition render

The browser controller's **Render selected note** action is separate from
QEMU's realtime `--audio` backend. It executes at most 32 callbacks, drives the
chosen lane with a generated sine at the most recent QWERTY pitch, monitors the
proven post-Filter2 Q1.31 output, and returns a repeated 0.75-second 48-kHz
stereo WAV. The stock mixer still executes and its 256 writes are checked, but
the compact storage-free fixture has not initialized its source-gain state and
therefore produces zero there. The UI labels this pre-mixer boundary directly.
