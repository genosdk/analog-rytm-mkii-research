# AR MKII OS 1.72 display / screenshot path

## Status: live framebuffer pointer recovered

The firmware contains a native MIDI RPC screenshot implementation. Static tracing now establishes not only the 128×64 format but the **exact global used to obtain the current live framebuffer**.

## Screenshot RPC anchors

- `MidiRpcScreenshotRequest` type name: `0x40221BCB`
- `MidiRpcScreenshotResponse` type name: `0x40221BF1`
- request/response reflection records: around `0x401B2110..0x401B2128`
- screenshot request handler: around `0x40087560`
- async screenshot worker: `0x400863E4`

The handler allocates a 1024-byte response payload with explicit dimensions:

- width `0x80` = **128**
- height `0x40` = **64**
- storage `0x400` = **1024 bytes**

Therefore the display image is **128×64, 1 bit/pixel**.

## Exact live framebuffer path

The async worker at `0x400863E4` performs the equivalent of:

```c
src = get_live_framebuffer();
memcpy(response_pixels, src, 0x400);
```

The accessor is fully resolved:

```asm
0x40092B90:
    MOVE.L  0x4026F478,D0
    RTS
```

Therefore:

> **`0x4026F478` contains the guest pointer to the current 1024-byte framebuffer.**

In the stock initialized MAIN image:

```text
0x4026F474 = 0x41906A44
0x4026F478 = 0x41906644
```

Those two buffers differ by exactly `0x400` bytes, strongly indicating a 1024-byte **double-buffer pair**:

```text
buffer A: 0x41906644 .. 0x41906A43
buffer B: 0x41906A44 .. 0x41906E43
```

Code around `0x40092Axx` reads and writes both pointer globals, consistent with front/back-buffer swapping. The desktop bridge should therefore **read the pointer dynamically from `0x4026F478` every frame**, rather than hard-code one buffer address.

## Bitmap RTTI / renderer anchors

- `Bitmap` RTTI name `6Bitmap`: `0x40229ECE`
- probable typeinfo: `0x401C14AC`
- object vptr: `0x401C14BC`
- constructor-like routine: `0x4006D304`
- common Bitmap/UI draw routine: `0x4006EE24`

The factory `UI TEST (DISPLAY)` screen uses this same software rendering layer.

## Emulator consequence

The first useful GUI requires no OLED controller model:

1. execute MAIN in SDRAM;
2. read big-endian pointer at guest `0x4026F478`;
3. copy 1024 bytes from that guest address;
4. expose them as `framebuffer.bin`;
5. render in `ar_panel_gui.py`.

This gives us the **actual Elektron firmware-rendered pixels**.
