#!/usr/bin/env python3
"""Decode the stock DSPI1 wire mode used by the BR/control packet stream."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


MAIN_BASE = 0x40000400
EXPECTED_MAIN_SHA256 = "5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772"
SETUP_ADDRESS = 0x4011D7F6
SETUP_BYTES = bytes.fromhex(
    "2039fc03c0008081223c7e00000023c0fc03c000"
    "203c7e00000142b9fc03c03023c0fc03c00c"
    "303c033523c1fc03c010223c3e00106423c0fc03c014"
    "203c80030c0023c1fc03c018721023c0fc03c000"
)
CTAR_VALUES = (0x7E000001, 0x7E000000, 0x7E000335, 0x3E001064)


def decode_ctar(value: int) -> dict:
    pbr = (2, 3, 5, 7)[(value >> 16) & 3]
    br = (2, 4, 6, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768)[value & 15]
    return {
        "raw": f"0x{value:08X}",
        "frame_bits": ((value >> 27) & 15) + 1,
        "cpol": (value >> 26) & 1,
        "cpha": (value >> 25) & 1,
        "lsb_first": bool(value & (1 << 24)),
        "baud_prescaler": pbr,
        "baud_scaler": br,
        "sck_divider": pbr * br,
    }


def trace(main_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    offset = SETUP_ADDRESS - MAIN_BASE
    if image[offset : offset + len(SETUP_BYTES)] != SETUP_BYTES:
        raise ValueError("stock DSPI1 setup signature changed")
    decoded = [decode_ctar(value) for value in CTAR_VALUES]
    if decoded[0] != {
        "raw": "0x7E000001",
        "frame_bits": 16,
        "cpol": 1,
        "cpha": 1,
        "lsb_first": False,
        "baud_prescaler": 2,
        "baud_scaler": 4,
        "sck_divider": 8,
    }:
        raise ValueError(f"unexpected CTAR0 decode: {decoded[0]}")
    return {
        "schema_version": 1,
        "result": "PASS_DSPI1_BR_WIRE_MODE_IDENTIFIED",
        "main": {"kind": "locally extracted stock OS 1.72 MAIN; not committed", "sha256": digest},
        "setup_address": f"0x{SETUP_ADDRESS:08X}",
        "ctar": decoded,
        "br_packet_ctar": 0,
        "wire_mode": {
            "word_bits": 16,
            "spi_mode": 3,
            "clock_idle": "high",
            "data_change_edge": "falling",
            "data_sample_edge": "rising",
            "bit_order": "MSB-first",
            "sck": "internal bus clock / 8",
        },
        "basis": (
            "BR PUSHR words select CTAR0. CTAR0=0x7E000001 has FMSZ=15, "
            "CPOL=1, CPHA=1, LSBFE=0, PBR=2, and BR=4."
        ),
        "safety": "Static read-only analysis; no firmware bytes are modified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("main_image", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    result = trace(args.main_image)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.json:
        args.json.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
