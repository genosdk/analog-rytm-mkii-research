# Emulator-only sample-verification status

Set `AR_MK2_MOCK_FACTORY_STATE=1` to expose the smallest sample-verification
record accepted by Analog Rytm MKII OS 1.72 through the existing DSPI0 SPI-NOR
model, together with the synthetic eMMC factory state. The profile is disabled
by default and does not modify the firmware.

## Recovered read and validator

The firmware function at `0x4012AED0` reads `0x4C` bytes from SPI-NOR address
`0x00380000` through `0x40128BBE`. It then:

1. compares the two bytes at record offset `0x10` with `SM`;
2. returns the signed 16-bit value at offset `0x12`;
3. expects version/status `2` during startup at `0x400A0E58`.

The emulator profile therefore supplies only four nonzero bytes:

- offset `0x10`: `53 4D` (`SM`)
- offset `0x12`: `00 02`

All other bytes in the `0x4C`-byte window remain zero.

## Boundary

This record proves only that OS 1.72 accepts its sample-verification metadata.
The shared factory-state profile separately supplies a valid empty `MaGj`
manifest at +Drive/eSDHC logical block `0x00180000`; it still supplies no
factory sample, project assignment, sample descriptor, or PCM payload.

No conclusion from this emulator-only profile applies to physical hardware.
