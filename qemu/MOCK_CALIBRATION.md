# Emulator-only calibration profile

This profile exists only to let Analog Rytm MKII OS 1.72 boot normally inside the digital emulator when no physical analog voice circuitry exists to measure.

It does **not** patch the Elektron firmware image and it must never be interpreted as evidence that physical hardware can safely run with synthetic calibration data.

## Separation of modes

- Desktop emulator: enables `AR_MK2_MOCK_CALIBRATION=1` by default.
- Raw/research QEMU: mock calibration is disabled unless the environment variable is explicitly supplied.
- Desktop `--no-mock-calibration`: removes the environment variable and exposes the firmware's normal missing-calibration path.
- Hardware validation: must use the untouched firmware calibration path and the physical unit's real persistent records and measurements. Emulator mock state is prohibited for hardware conclusions.

## OS 1.72 record validation recovered

The firmware reads its primary calibration record from SPI NOR address `0x340000`, with a fallback at `0x300000`.

Recovered v5 record properties:

- total record size: `110706` bytes (`0x1B072`)
- offset `0x0000`: magic `0x52424F57` (`RBOW`)
- offset `0x0004`: header/format field `12490` (`0x30CA`)
- offset `0x0008`: version, current OS 1.72 value `5`
- offset `0x000C`: checksum 1
- offset `0x0010`: calibration status; `1` is the normal-current valid state
- offset `0x30CC`: total record size
- offset `0x30D0`: checksum 2
- offsets `0x3DD0..0x3DD5`: six fields initialized to `1` by the firmware's v4-to-v5 migration

The checksum routine at firmware `0x400F707C` starts with zero and, for each byte using a 1-based index, adds the full 32-bit value `byte XOR index` modulo `2^32`.

For the current neutral synthetic record:

- checksum 1 over `record + 0x10`, length `0x30BA`: `0x04A33BF0`
- checksum 2 over `record + 0x30D4`, length `record_size - 0x30E4`: `0x1F55D929`
- synthetic record SHA-256: `80df03668f8cd930cc62900c862b3c9e49434eab1224a7f2fc51e4352eac8ff7`

## Firmware behavior

The emulator provides the record through DSPI0's normal SPI-NOR `0x03` READ transaction. The firmware still performs its own magic, version, size, checksum, and status validation.

A valid version-5/status-1 record is expected to suppress both calibration-derived startup paths:

1. the improved-tuning calibration prompt, which checks calibration version/current status;
2. `ANALOG CALIBRATION MISSING`, whose startup closure receives the same current-calibration result.

## Not calibration

The following startup states are separate and are intentionally **not** faked by this profile:

- `+DRIVE ERROR 10`: +Drive/eSDHC/filesystem health;
- factory sample presence: separate factory storage metadata;
- filesystem readiness: verified `ekFS` state;
- sample verification: separate `SM` persistent status.

These should be modeled at their own storage/peripheral boundaries if needed.

## Hardware-validation requirement

The `RBOW` record contains substantial per-unit data beyond status and checksums. Before any firmware modification is considered safe for physical hardware, hardware work must trace where those measured coefficients are consumed by oscillator, filter, VCA, DAC/control, or other analog-facing code. A mock record passing metadata validation does not substitute for those measurements.
