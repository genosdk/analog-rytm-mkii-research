# AR MKII OS 1.72 — QEMU emulation status

## Milestone

The real decompressed MAIN image now executes far enough under QEMU to initialize the
scheduler, complete a front-panel identity handshake, pass the first DSPI-dependent
startup path, receive a timer wakeup, and write a non-zero 128×64 software framebuffer.

This is **emulator research only**. None of the compatibility changes below are applied
to the validated flashable `.syx` artifacts.

## Proven in the current harness

- MAIN native load address: `0x40000400`.
- Observed MAIN entry: `0x40000870`.
- Bootstrap stack used by the harness: `0x47FFFFE0`.
- QEMU temporary machine: `mcf5208evb` with `-cpu any` and 128 MiB RAM.
- `-cpu any` executes the reachable ColdFire `FF1.L` instructions directly; the older
  guest-side FF1 trap workaround is no longer required.
- Rytm PIT0 scheduler interrupt translated from INTC2 source 13/vector 205 to the
  MCF5208EVB INTC0 source 4/vector 68.
- UART8 (`0xEC070000`) is aliased to QEMU UART0 (`0xFC060000`) for panel-protocol research.
- Boot-equivalent UART RX/TX enable state is supplied before MAIN entry.
- Observed firmware UART transmit sequence before panel identity response:
  `62 00 F1 62 00 70 00`.
- The five-byte panel identity response accepted by the live validator is:
  `70 07 05 05 00`.
- At the validator breakpoint the bytes are present as D6=`0x70`, D5=`0x07`,
  D4=`0x05`, D2=`0x05`; both `0x05` values match the firmware's initialized globals.
- DSPI0 (`0xFC05C000`) is temporarily redirected to RAM-backed register scratch at
  `0x47F00000`, with status initialized ready. This is deliberately a discovery stub,
  not a DSPI device model.
- One DTIM1 wakeup path is translated onto QEMU PIT1 so the waiting startup task runs.
- Known framebuffer pointer global: `0x4026F478`.
- Observed framebuffer pointer: `0x41906644`.
- Frame size: 1024 bytes = 128×64×1 bpp.
- First framebuffer write watchpoint: PC `0x4006D3AC`.
- First completed non-zero frame: 95 non-zero bytes, 367 lit pixels.
- First completed frame SHA-256:
  `9fc4f1bc105a8aaa5f2769c905bfdcd8fa68a200f2b8b48a168c558df79fa542`.

## Emulator-only translations

The reproducible builder is `qemu/build_mcf5208_emulator_shim.py`. It takes a local,
decompressed MAIN binary and emits a raw QEMU `-kernel` image. It does not create or
modify SysEx firmware.

The shim currently provides:

1. Initial SP + jump to the observed MAIN entry.
2. Scheduler PIT interrupt translation.
3. UART8 → QEMU UART0 register aliases.
4. Boot-equivalent UART RX/TX enable state.
5. RAM-backed DSPI0 discovery registers.
6. DTIM1 one-shot wakeup → QEMU PIT1 translation.

## Panel stub

`qemu/panel_handshake_stub.py` connects to a QEMU Unix serial socket and replies to the
observed `70 00` identity request with `70 07 05 05 00`.

Example launch shape:

```bash
qemu-system-m68k \
  -M mcf5208evb \
  -cpu any \
  -m 128M \
  -kernel build/ar_mk2_mcf5208_shim.bin \
  -nographic \
  -serial unix:/tmp/ar_panel.sock,server=on,wait=off \
  -monitor none
```

In another terminal:

```bash
python qemu/panel_handshake_stub.py /tmp/ar_panel.sock
```

## Current boundary

The first real software framebuffer is proven, but the full normal Rytm UI is not yet
running interactively. After the first render, initialization continues through buffer
stream setup and scheduler primitives, then active work drains back to idle.

Immediate targets:

1. Identify which post-render task/event is required to advance from the initial frame
   into the normal UI.
2. Replace the RAM-backed DSPI discovery stub with a minimal stateful DSPI model based on
   observed transfer semantics.
3. Model the remaining DTIM/interrupt behavior instead of translating one wakeup.
4. Connect repeated framebuffer export to `ar_panel_gui.py`.
5. Feed panel button/encoder events back into the firmware once the normal UI task is
   alive.

## Safety boundary

These QEMU translations are intentionally incompatible with a hardware firmware update.
They must never be folded into the staged hardware-test `.syx` files. Hardware testing
still follows the recovery → stock round-trip → inert detour → functional-candidate order.
