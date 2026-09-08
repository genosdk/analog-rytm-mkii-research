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
