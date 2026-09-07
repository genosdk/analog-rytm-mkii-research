#!/usr/bin/env python3
"""Build an emulator-only MCF5208EVB compatibility image for AR MKII MAIN.

Input is a locally extracted/decompressed MAIN image beginning at its native
0x40000400 load address.  Output is a raw QEMU -kernel image mapped at
0x40000000.  This script does NOT emit a flashable SysEx package and does not
modify the validated custom-firmware artifact.
"""
from __future__ import annotations
import argparse
from pathlib import Path

BASE = 0x40000000
MAIN_LOAD = 0x40000400
MAIN_ENTRY = 0x40000870
BOOT_SP = 0x47FFFFE0
DSPI_SCRATCH = 0x47F00000

# Emulator-only direct patches. These adapt one Rytm timer interrupt to the
# MCF5208EVB interrupt/PIT implementation used as a temporary execution host.
PATCHES = {
    # PIT0 scheduler: Rytm INTC2 source 13/vector205 -> QEMU INTC0 source4/vector68.
    0x40000DBE: (bytes.fromhex("40000334"), bytes.fromhex("40000110")),
    0x40000DCC: (bytes.fromhex("FC05004D"), bytes.fromhex("FC048044")),
    0x40000DD0: (bytes.fromhex("700D"), bytes.fromhex("7004")),
    0x40000DD4: (bytes.fromhex("FC05001D"), bytes.fromhex("FC04801D")),
    0x40000440: (bytes.fromhex("FC050014"), bytes.fromhex("FC048014")),

    # DTIM1 one-shot wakeup: Rytm source33/vector97 -> QEMU PIT1 source5/vector69.
    0x40095CF4: (bytes.fromhex("40000184"), bytes.fromhex("40000114")),
    0x40095CFC: (bytes.fromhex("FC048061"), bytes.fromhex("FC048045")),
    0x40095D00: (bytes.fromhex("7021"), bytes.fromhex("7005")),
    0x40095D2C: (bytes.fromhex("FC074004"), bytes.fromhex("FC084002")),
    0x40095D38: (bytes.fromhex("FC074000"), bytes.fromhex("FC084000")),
    0x40095C92: (bytes.fromhex("FC074003"), bytes.fromhex("FC084000")),
    0x40095C9A: (bytes.fromhex("FC074000"), bytes.fromhex("FC084000")),
}


def build(main: bytes) -> tuple[bytes, dict]:
    image = bytearray(b"\x00" * (MAIN_LOAD - BASE) + main)

    # Bootstrap state that the real boot path supplies before MAIN: make the
    # aliased UART usable by QEMU, seed a minimal DSPI status register, set SP,
    # then jump to the observed MAIN entry.
    bootstrap = bytes.fromhex(
        "13FC0001FC060008"      # move.b #1,UART0.CR  (RX enable)
        "13FC0004FC060008"      # move.b #4,UART0.CR  (TX enable)
        "23FC100000F047F0002C"  # move.l #0x100000F0, scratch DSPI.SR
        "2E7C47FFFFE0"          # movea.l #BOOT_SP,A7
        "4EF940000870"          # jmp MAIN_ENTRY
    )
    image[: len(bootstrap)] = bootstrap

    applied = []
    for addr, (old, new) in PATCHES.items():
        off = addr - BASE
        got = bytes(image[off : off + len(old)])
        if got != old:
            raise SystemExit(
                f"patch mismatch at 0x{addr:08X}: expected {old.hex()} got {got.hex()}"
            )
        image[off : off + len(old)] = new
        applied.append(f"0x{addr:08X}")

    # UART8 (MCF5441x, EC070000) -> QEMU MCF5208 UART0 (FC060000).
    uart_aliases = 0
    # DSPI0 -> RAM-backed register scratch. Status is seeded ready; writes to
    # MCR/SR/PUSHR persist, POPR defaults to zero. This is discovery scaffolding,
    # not a behavioral model of the attached SPI device.
    dspi_aliases = 0
    for off in range(0, len(image) - 3, 2):
        v = int.from_bytes(image[off : off + 4], "big")
        if (v & 0xFFFFFF00) == 0xEC070000:
            image[off : off + 4] = (0xFC060000 | (v & 0xFF)).to_bytes(4, "big")
            uart_aliases += 1
        elif (v & 0xFFFFC000) == 0xFC05C000:
            image[off : off + 4] = (DSPI_SCRATCH | (v & 0x3FFF)).to_bytes(4, "big")
            dspi_aliases += 1

    return bytes(image), {
        "bootstrap_bytes": len(bootstrap),
        "fixed_patches": applied,
        "uart8_alias_literals": uart_aliases,
        "dspi0_alias_literals": dspi_aliases,
        "dspi_scratch": f"0x{DSPI_SCRATCH:08X}",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("main_bin", type=Path, help="decompressed MAIN loaded natively at 0x40000400")
    ap.add_argument("output", type=Path, help="raw QEMU -kernel output")
    args = ap.parse_args()
    main_data = args.main_bin.read_bytes()
    out, report = build(main_data)
    args.output.write_bytes(out)
    print(f"wrote {args.output} ({len(out)} bytes)")
    for k, v in report.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
