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
# Apply or manually reproduce meson.build.patch
patch -p1 < /path/to/meson.build.patch
```

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
