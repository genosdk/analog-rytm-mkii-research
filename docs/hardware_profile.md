# AR MKII hardware profile — emulator working model

## CPU / SoC

The firmware matches the **NXP/Freescale MCF5441x ColdFire family** closely enough to use that SoC memory map as the emulator baseline.

For CPU execution, QEMU's `cfv4e` model is the best existing starting point because its feature set includes ColdFire ISA A/B, long branches, FPU and EMAC support. The exact AR MKII package/SKU still needs PCB or bootloader confirmation; the board shim should therefore be named for the AR MKII rather than pretending to be a stock NXP evaluation board.

## External SDRAM

The MCF5441x external SDRAM aperture begins at `0x40000000`.

The AR MAIN image is loaded at:

- base: `0x40000400`
- decompressed length: `2,903,032` bytes
- strong MAIN entry candidate: `0x40000870`

Early entry code changes `A7/SP` to `0x48000000`. The most useful emulator hypothesis is therefore **128 MiB SDRAM at `0x40000000..0x47FFFFFF`, with `0x48000000` as the top-of-stack address**. Treat the 128 MiB size as an inference until hardware/bootloader behavior confirms it.

Important correction from the first scaffold: the MAIN image should be modeled as **initialized executable SDRAM**, not ROM.

## Internal SRAM

The MCF5441x provides a 64 KiB internal SRAM with a backdoor aperture beginning at `0x80000000`.

Known AR runtime structures inside that aperture:

- `0x80005F10` — per-parameter state/update table
- `0x8000E5B0` — live 13×42 parameter table

The emulator should use one 64 KiB SRAM backing store and implement the hardware's backdoor alias/wrap behavior rather than allocating megabytes of independent RAM at `0x8000....`.

## Early boot

Strong entry path: `0x40000870`

Observed behavior:

1. consumes a boot-provided value from the incoming stack frame;
2. stores it to global `0x4025A3B4`;
3. switches SP to `0x48000000`;
4. configures MCF5441x SCM / clock-reset / pin state;
5. initializes eDMA and CPU cache/control registers;
6. calls deeper OS initialization including `0x4015D09A`.

The headless board shim therefore needs a synthetic boot stack before entry, but the firmware rapidly moves to its own SDRAM stack.

## PBC0 peripheral map used by the board model

| Base | Module |
|---|---|
| `0xFC004000` | Crossbar |
| `0xFC008000` | FlexBus |
| `0xFC020000` | FlexCAN0 |
| `0xFC024000` | FlexCAN1 |
| `0xFC038000` | I2C1 |
| `0xFC03C000` | DSPI1 |
| `0xFC040000` | SCM |
| `0xFC044000` | eDMA |
| `0xFC048000` | INTC0 |
| `0xFC04C000` | INTC1 |
| `0xFC050000` | INTC2 |
| `0xFC054000` | IACK |
| `0xFC058000` | I2C0 |
| `0xFC05C000` | DSPI0 |
| `0xFC060000` | UART0 |
| `0xFC064000` | UART1 |
| `0xFC068000` | UART2 |
| `0xFC06C000` | UART3 |
| `0xFC070000` | DMA timer0 |
| `0xFC074000` | DMA timer1 |
| `0xFC078000` | DMA timer2 |
| `0xFC07C000` | DMA timer3 |
| `0xFC080000` | PIT0 |
| `0xFC084000` | PIT1 |
| `0xFC088000` | PIT2 |
| `0xFC08C000` | PIT3 |
| `0xFC090000` | Edge port0 |
| `0xFC094000` | ADC |
| `0xFC098000` | DAC0 |
| `0xFC09C000` | DAC1 |
| `0xFC0A8000` | RTC |
| `0xFC0AC000` | SIM |
| `0xFC0B0000` | USB OTG |
| `0xFC0B4000` | USB host |
| `0xFC0B8000` | DDR controller |
| `0xFC0BC000` | SSI0 |
| `0xFC0C0000` | PLL |
| `0xFC0C4000` | RNG |
| `0xFC0C8000` | SSI1 |
| `0xFC0CC000` | eSDHC |
| `0xFC0D4000` | MAC-NET0 |
| `0xFC0D8000` | MAC-NET1 |

## PBC1 peripheral map used by the board model

| Base | Module |
|---|---|
| `0xEC008000` | 1-Wire |
| `0xEC010000` | I2C2 |
| `0xEC014000` | I2C3 |
| `0xEC018000` | I2C4 |
| `0xEC01C000` | I2C5 |
| `0xEC038000` | DSPI2 |
| `0xEC03C000` | DSPI3 |
| `0xEC060000` | UART4 |
| `0xEC064000` | UART5 |
| `0xEC068000` | UART6 |
| `0xEC06C000` | UART7 |
| `0xEC070000` | UART8 |
| `0xEC074000` | UART9 |
| `0xEC088000` | mcPWM |
| `0xEC090000` | CCM / reset / power management |
| `0xEC094000` | Pin mux / GPIO |

This resolves the earlier `0xEC094...` accesses: they are GPIO/pin-mux traffic, not an unidentified external device.

## Display / GUI

The firmware-native screenshot RPC establishes the software display format:

- width: **128**
- height: **64**
- depth: **1 bit/pixel**
- payload: **1024 bytes**

The emulator's first GUI should consume this software Bitmap/screenshot representation. Physical OLED/DSPI emulation is not required for the first useful GUI milestone.

Relevant firmware anchors:

- screenshot handler region: `0x40087560`
- Bitmap drawing routine used by UI tests: `0x4006EE24`
- `SamplePageView` ctor: `0x40190B5E`

## Initial peripheral-emulation priority

Do not implement the entire MCF5441x at once. Start with:

1. SDRAM + 64 KiB internal SRAM backdoor
2. SCM / CCM writes required to leave boot
3. eDMA register storage / safe status defaults
4. INTC/IACK with interrupts initially quiescent
5. PIT/DMA timers with monotonic counters
6. GPIO/pin mux register storage
7. DSPI register FIFOs/status logging
8. eSDHC/USB as inert stubs unless boot blocks on them

Every unimplemented read/write should be logged with PC, address, width and value so the model can be tightened from actual execution rather than speculation.
