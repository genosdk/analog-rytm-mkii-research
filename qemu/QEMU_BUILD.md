# Building the experimental AR MKII QEMU machine

This is a source scaffold for a custom QEMU system machine. It has **not yet been compiled in this workspace**, so expect small API adjustments depending on the QEMU revision used.

## Recommended QEMU baseline

Use current QEMU `master` or a recent release whose m68k target includes the `cfv4e` CPU model.

QEMU's existing `hw/m68k/mcf5208.c` is the template used here for CPU creation, SDRAM mapping and firmware loading.

## Add the machine

From a QEMU source checkout:

```bash
cp elektron_ar_mk2.c /path/to/qemu/hw/m68k/
cd /path/to/qemu
# Restore the ColdFire EMAC MASK hardware reset value.
patch -p1 < /path/to/0001-m68k-reset-coldfire-emac-mask.patch
# Keep load-form MAC instructions single-accumulator operations.
patch -p1 < /path/to/0002-m68k-fix-coldfire-emac-dual-detection.patch
# Decode load-form operands and fractional products from the correct fields.
patch -p1 < /path/to/0003-m68k-fix-coldfire-emac-load-operands.patch
# Apply or manually reproduce meson.build.patch
patch -p1 < /path/to/meson.build.patch
```

The EMAC patch is required for this machine. The MCF5441x MASK register resets
to `0xFFFF_FFFF`; QEMU otherwise zero-initializes it, causing EMAC-with-load
instructions to mask valid SRAM operands down to address zero. The pinned build
workflows apply all required EMAC patches automatically. The second patch
corrects QEMU's reversed load/no-load test for EMAC_B dual-accumulation
opcodes; without it, ordinary MAC-with-load instructions can perform an
unintended second accumulation. The third patch selects the load-form Rx and
add/subtract operation from the extension word, then restores signed Q1.31
product alignment; without it, packed parameter lanes are halved or sourced
from the wrong register.

The board eDMA model also implements ELINK count decoding, per-element
SOFF/DOFF updates, software START requests, and ESG scatter/gather TCD loads.
These behaviors are required by the stock channel-30 external-audio chain;
treating DLASTSG as an ordinary destination adjustment leaves the live audio
ISR spinning at `0x40109FFE`.

## Configure

Example development build:

```bash
mkdir build-ar
cd build-ar
../configure --target-list=m68k-softmmu --enable-debug --disable-werror
ninja
```

Confirm the machine appears:

```bash
./qemu-system-m68k -machine help | grep elektron
```

## First headless run

Use the **decompressed MAIN `.bin`**, not the `.syx` container:

```bash
./qemu-system-m68k \
  -M elektron-ar-mk2 \
  -m 128M \
  -bios AR172_SLICE16_TRANSACTIONAL_LAB_DO_NOT_FLASH.main.bin \
  -nographic \
  -d unimp,guest_errors,in_asm \
  -D ar_mk2_qemu.log
```

The first useful result is not a complete boot. It is a deterministic log showing where execution stops or spins and which MCF5441x registers were touched immediately beforehand.

## Iteration strategy

1. Run until the first stable blocker/spin.
2. Inspect the final MMIO accesses in `ar_mk2_qemu.log`.
3. Promote only the required module/registers from catch-all zero stubs into stateful stubs.
4. Repeat.

Likely early modules:

- SCM / CCM
- eDMA
- INTC/IACK
- PIT/DMA timers
- GPIO/pin mux
- DSPI0/DSPI1

USB, Ethernet, and the physical OLED remain outside the current evidence-based
boundary. The opt-in `AR_MK2_MOCK_FACTORY_STATE=1` profile now models only the
eSDHC/eMMC behavior proven necessary for stock startup; see
`qemu/MOCK_STORAGE.md` for its volatile-storage and empty-manifest limits.

For a display-free end-to-end check with a caller-supplied MAIN image:

```bash
python qemu/headless_ui_smoke.py \
  --qemu /path/to/qemu-system-m68k \
  --main /path/to/decompressed-main.bin
```

The smoke test boots with the two emulator-only profiles, completes the panel
identity exchange, dismisses the remaining startup modal with `NO`, then
opens `SMP`. It requires distinct stable framebuffer hashes for the modal,
normal UI, and SMP page.

The standalone desktop launcher can expose the stock renderer ring through
QEMU's host-audio backend:

```bash
python qemu/run_desktop_emulator.py \
  --qemu /path/to/qemu-system-m68k \
  --firmware /path/to/Analog-Rytm_MKII_OS1.72.syx \
  --audio
```

`--audio` is a passive tap: it does not manufacture renderer work or enable the
experimental external audio interrupt. QEMU builds need a platform output
driver (for example CoreAudio, PipeWire, PulseAudio, SDL, or OSS). For a
deterministic capture, QEMU can instead be launched with its WAV default audio
driver while `AR_MK2_AUDIO_TAP=1` is set.

## GUI bridge

The desktop GUI scaffold is independent of the physical OLED. Once the firmware-side 128×64 Bitmap source is identified, the QEMU machine can periodically write its 1024-byte framebuffer to:

`emulator/framebuffer.bin`

`ar_panel_gui.py` already polls that format.

## Important

This machine intentionally returns `0` from unimplemented MMIO reads. That is useful for discovery but not expected to boot the full OS unchanged. Peripherals should be added from execution evidence, not guessed wholesale.

## Real firmware framebuffer export

The native screenshot path has now resolved the live framebuffer pointer global:

- pointer global: `0x4026F478`
- frame size: 1024 bytes
- dimensions: 128×64 × 1 bpp

The machine scaffold can export it automatically. Set:

```bash
export AR_MK2_FRAMEBUFFER_OUT=/absolute/path/to/framebuffer.bin
```

before launching QEMU. The machine reads the guest pointer dynamically and writes the current 1024-byte frame roughly every 16 ms of virtual time.

Then run the existing desktop panel separately:

```bash
python ar_panel_gui.py --frame /absolute/path/to/framebuffer.bin
```

This path renders the firmware's software framebuffer directly and does not require physical OLED emulation.
