#!/usr/bin/env python3
"""Recover OS 1.72's independent sample-storage startup gates.

This is a read-only signature trace over a locally supplied MAIN image.  It
does not extract or reproduce any factory sample, project, descriptor or PCM.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

EXPECTED_MAIN_SHA256 = (
    "5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772"
)
MAIN_BASE = 0x40000400


def at(image: bytes, address: int, size: int) -> bytes:
    offset = address - MAIN_BASE
    if offset < 0 or offset + size > len(image):
        raise ValueError(f"address outside MAIN: 0x{address:08X}")
    return image[offset : offset + size]


def require(haystack: bytes, needle: bytes, label: str) -> None:
    if needle not in haystack:
        raise ValueError(f"missing {label} signature")


def trace(main_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")

    card_probe = at(image, 0x4008EA1C, 0x76)
    require(card_probe, bytes.fromhex("1239ec09401a"), "GPIO input read")
    require(card_probe, bytes.fromhex("13c1ec09401b"), "GPIO set write")
    require(card_probe, bytes.fromhex("13c1ec094027"), "GPIO clear write")

    block_read = at(image, 0x4008F4BA, 0x1C8)
    require(block_read, bytes.fromhex("4ab941901b08"), "eSDHC-ready guard")
    require(block_read, bytes.fromhex("23c8fc045770"), "eDMA destination")
    require(block_read, bytes.fromhex("23c4fc0cc008"), "eSDHC block address")

    manifest = at(image, 0x400FCFCC, 0x98)
    require(manifest, bytes.fromhex("2f3c00180000"), "MaGj storage offset")
    require(manifest, bytes.fromhex("48784000"), "MaGj read length")
    require(manifest, bytes.fromhex("203c4d61476a"), "MaGj magic")
    require(manifest, bytes.fromhex("0c80debb20e3"), "MaGj checksum residue")

    sample_status = at(image, 0x4012AED0, 0x40)
    require(sample_status, bytes.fromhex("2f3c00380000"), "SM storage address")
    require(sample_status, bytes.fromhex("4878004c"), "SM read length")
    require(sample_status, bytes.fromhex("48794022ae26"), "SM magic pointer")
    if at(image, 0x4022AE26, 2) != b"SM":
        raise ValueError("SM magic bytes changed")

    startup = at(image, 0x400A0E3A, 0x48)
    require(startup, bytes.fromhex("4eb9400fcfcc"), "startup MaGj call")
    require(startup, bytes.fromhex("4eb94012aed0"), "startup SM call")
    require(startup, bytes.fromhex("7202b28057c6"), "startup SM version check")

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "storage_gates": {
            "media_probe": {
                "function": "0x4008EA1C",
                "boundary": "GPIO/pin-state probe before eSDHC initialization",
                "input_register": "0xEC09401A",
                "set_register": "0xEC09401B",
                "clear_register": "0xEC094027",
            },
            "block_reader": {
                "function": "0x4008F4BA",
                "ready_global": "0x41901B08",
                "block_count_global": "0x41901B2C",
                "block_size_global": "0x41901B30",
                "transport": "eSDHC plus eDMA channel 59",
            },
            "factory_manifest": {
                "validator": "0x400FCFCC",
                "storage": "+Drive/eSDHC",
                "logical_block_address": "0x00180000",
                "read_bytes": "0x4000",
                "magic": "MaGj",
                "record_count_offset": "0x10",
                "entry_bytes": 12,
                "checksum_residue": "0xDEBB20E3",
            },
            "sample_verification": {
                "validator": "0x4012AED0",
                "storage": "DSPI0 SPI NOR",
                "address": "0x00380000",
                "read_bytes": "0x4C",
                "magic_offset": "0x10",
                "magic": "SM",
                "version_offset": "0x12",
                "startup_expected_version": 2,
            },
        },
        "live_negative_control": {
            "esdhc_ready": 0,
            "esdhc_block_count": 0,
            "esdhc_block_size": 0,
            "magj_result": 0,
            "sm_result": -1,
            "drive_error": 10,
            "source": "QEMU GDB trace at the OS 1.72 startup decision",
        },
        "conclusion": (
            "Factory-sample presence and sample-verification state are two "
            "independent records on two transports. The four-byte SM/version "
            "profile can be modeled without fabricating a sample or MaGj body."
        ),
        "remaining_gate": (
            "With the emulator-only GPIO/eSDHC factory profile supplying an "
            "empty MaGj manifest, trace project sample-slot assignment and "
            "the RAM sample descriptor without fabricating factory PCM."
        ),
        "safety": (
            "Read-only firmware signature analysis and emulator metadata only; "
            "no firmware, project, factory sample, descriptor slab or PCM modified."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("main_image", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = trace(args.main_image)
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
