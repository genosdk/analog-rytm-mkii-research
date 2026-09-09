# Emulator-only +Drive factory-state profile

Set `AR_MK2_MOCK_FACTORY_STATE=1` to enable the minimum eSDHC/eMMC state
currently accepted by Analog Rytm MKII OS 1.72. The profile is disabled by
default, does not modify the firmware, and does not attach a host disk image.

## Implemented boundary

- board GPIO media-probe loopback used by `0x4008EA1C`;
- self-clearing eSDHC `SYSCTL` reset command;
- DAT0 line state used by the stock bus-width transition;
- latched command/data-complete status with write-one-to-clear semantics;
- INTC2 source 31 for the eSDHC ISR at vector 223;
- MMC `CMD1`, bus-width tuning, and a 2 GiB `EXT_CSD` sector count;
- buffer-ready observations needed by the stock tuning sequence;
- bidirectional eDMA channel 59 FIFO transfers and grouped INTC2 vector 192;
- sparse, RAM-only sector writes and erases used by the stock formatter;
- the stock firmware's default `COKI` system record at logical block
  `0x0007A000`;
- a minimal valid `ekFS` superblock at logical block `0x001C0000`;
- a valid empty format-71 `MaGj` manifest at logical block `0x00180000`.

The virtual device reports `0x00400000` 512-byte sectors. Other sectors read
as zero unless the firmware writes them during the current run. All writes
disappear when QEMU exits.

## Current result

The untouched stock initializer now reports:

- initializer result `0`;
- ready global `0x41901B08 = 1`;
- logical block count `0x41901B2C = 0x00400000`;
- address multiplier `0x41901B30 = 1`;
- `MaGj` validator result `1`;
- sample-verification (`SM`) result `2`;
- drive error global `0x405AFDF0 = 0`.

The panel consequently advances from `+DRIVE ERROR 10/36` to
`NO FACTORY SAMPLES`. That message is expected: the manifest is deliberately
empty, and no project assignment, sample descriptor, or PCM payload is
invented by this profile.

No conclusion from this emulator-only profile applies to physical hardware.
