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

The storage-failure branch at `0x400A1D76` is no longer taken. A clean boot
reaches one dismissible startup modal. A native `NO` press/release (`24 01`,
`24 00`) clears it to the normal parameter UI, and `SMP` (`25 10`, `25 00`)
opens the SMP page through the live UART/UI queue. The empty manifest still
invents no factory sample, project sample assignment, descriptor, or PCM
payload.

No conclusion from this emulator-only profile applies to physical hardware.

## Generated project-sample descriptor

Set `AR_MK2_MOCK_PROJECT_SAMPLE=1` together with the factory-state profile to
publish one emulator-generated sample descriptor in otherwise-empty slot 1.
This test boundary is disabled by default and contains no firmware-derived or
factory PCM. It generates a 256-frame, 48 kHz, signed 16-bit square wave in
guest RAM and names it `QEMU TEST`.

Publication is deliberately guarded. The injector waits for ekFS readiness,
accepts only the stock blank-name sentinel with zero metadata, and refuses to
replace any non-empty slot. A one-second monitor republishes the descriptor if
later initialization restores the blank sentinel; it does not overwrite user
or firmware content.

The live tables used by OS 1.72 are:

- name pointer: `0x41928DCC + slot * 4`;
- packed byte-length metadata: `0x419289CC + slot * 4`;
- secondary metadata: `0x41928BCC + slot * 4`;
- status byte: `0x4192894C + slot`;
- playback registry: `0x41310D30 + slot * 16`.

For slot 1, the generated sample is stored at `0x4FF00000`, its name at
`0x4FF00400`, and the registry records a 48 kHz rate, 256-frame extent, and
`0x40000000` rate ratio. A live post-initialization trace confirms all of
these values persist.

This gate does not yet produce sample voice output. The blank project still
returns sample-slot parameter 0 (`OFF`), and changing only the observed track
slot byte does not enter the sample renderer. The next boundary is the missing
project parameter provider or voice-enable state; descriptor presence alone is
not treated as proof of playback.
