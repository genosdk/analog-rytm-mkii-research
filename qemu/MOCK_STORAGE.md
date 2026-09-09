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
- MMC `CMD1`, `CMD2`/`CMD10` CID responses, bus-width tuning, and the
  `EXT_CSD` identity/capacity fields for the firmware's known Toshiba
  `004GE0` profile;
- buffer-ready observations needed by the stock tuning sequence;
- bidirectional eDMA channel 59 FIFO transfers and grouped INTC2 vector 192;
- sparse, RAM-only sector writes and erases used by the stock formatter;
- the stock firmware's default `COKI` system record at logical block
  `0x0007A000`;
- a minimal valid `ekFS` superblock at logical block `0x001C0000`;
- a valid empty format-71 `MaGj` manifest at logical block `0x00180000`.

The active partition reports `0x003B0000` 512-byte sectors, matching the
allowlisted profile selected by `EXT_CSD[0x98] = 1`. Other sectors read as
zero unless the firmware writes them during the current run. All writes
disappear when QEMU exits. The reconstructed `COKI`, `ekFS`, and `MaGj`
records remain immutable overlays so a formatter write cannot accidentally
replace the emulator boundary fixtures.

## Current result

The untouched stock initializer now reports:

- initializer result `0`;
- ready global `0x41901B08 = 1`;
- CID/EXT_CSD identity check `0x4008F260 = 0` (`MID 0x11`, `004GE0`);
- logical block count `0x41901B2C = 0x003B0000`;
- address multiplier `0x41901B30 = 1`;
- stock ekFS creator entered at `0x4007C690` and readiness set to `1`;
- `MaGj` validator result `1`;
- sample-verification (`SM`) result `2`;
- drive error global `0x405AFDF0 = 0`.

The storage-failure branch at `0x400A1D76` is no longer taken. The last
exported panel frame still reads `NO FACTORY SAMPLES`: the manifest is
deliberately empty, and no project assignment, sample descriptor, or PCM
payload is invented by this profile.

No conclusion from this emulator-only profile applies to physical hardware.
