#!/usr/bin/env python3
"""Recover OS 1.72's project sample-slot UI boundary.

This is a read-only signature trace over a caller-supplied MAIN image. It
contains no firmware, sample names, project records, descriptors, or PCM.
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


def c_string(image: bytes, address: int, limit: int = 32) -> str:
    data = at(image, address, limit)
    end = data.find(b"\0")
    if end < 0:
        raise ValueError(f"unterminated string at 0x{address:08X}")
    return data[:end].decode("ascii")


def trace(main_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")

    page_refresh = at(image, 0x400CECE6, 0x92)
    require(page_refresh, bytes.fromhex("48780029"), "Sample Slot parameter ID")
    require(page_refresh, bytes.fromhex("e082"), "Q8 slot conversion")
    require(page_refresh, bytes.fromhex("4878002b"), "sample start parameter ID")
    require(page_refresh, bytes.fromhex("4878002c"), "sample end parameter ID")

    key_handler = at(image, 0x400CF2DC, 0x1EC)
    require(key_handler, bytes.fromhex("7232b280"), "SMP event ID")
    require(key_handler, bytes.fromhex("4eb94006f868"), "picker qualifier test")
    require(key_handler, bytes.fromhex("7229"), "Sample Slot control scan")
    require(key_handler, bytes.fromhex("4ebafa48"), "sample picker dispatch")

    picker = at(image, 0x400E0526, 0x1E2)
    require(picker, bytes.fromhex("0c8400000080"), "128-entry picker loop")
    require(picker, bytes.fromhex("254200a0"), "initial slot field")
    require(picker, bytes.fromhex("222e000c254100d8"), "active track field")

    name_accessor = at(image, 0x40099FAA, 0x10)
    require(name_accessor, bytes.fromhex("41f941928dcc"), "name pointer table")
    require(name_accessor, bytes.fromhex("20300c00"), "four-byte table stride")

    packed_accessor = at(image, 0x40099FBA, 0x18)
    require(packed_accessor, bytes.fromhex("41f9419289cc"), "packed metadata table")
    require(packed_accessor, bytes.fromhex("e288"), "packed metadata shift")

    renderer = at(image, 0x400B4932, 0xA8)
    require(renderer, bytes.fromhex("48794023c983"), "OFF sentinel")
    require(renderer, bytes.fromhex("487940244e1d"), "empty-slot sentinel")
    require(renderer, bytes.fromhex("4eb940099faa"), "slot name lookup")
    require(renderer, bytes.fromhex("4eb940154a24"), "slot name length test")

    sentinels = {
        "off": c_string(image, 0x4023C983),
        "empty": c_string(image, 0x40244E1D),
        "blank_name": c_string(image, 0x40228E97),
    }
    if sentinels != {"off": "OFF", "empty": "---", "blank_name": ""}:
        raise ValueError(f"unexpected sample-slot sentinels: {sentinels!r}")

    return {
        "result": "PASS",
        "main_sha256": digest,
        "sample_slot_parameter": {
            "id": "0x29",
            "encoding": "Q8 fixed point; arithmetic shift right 8 for picker index",
            "zero_meaning": "OFF",
        },
        "picker": {
            "entry": "0x400CEE30",
            "constructor": "0x400E0526",
            "control_index": 3,
            "entry_count": 128,
            "domain": "OFF plus sample slots 1..127",
        },
        "runtime_tables": {
            "name_pointer_table": "0x41928DCC",
            "name_pointer_stride": 4,
            "packed_metadata_table": "0x419289CC",
            "packed_metadata_stride": 4,
        },
        "sentinels": sentinels,
        "empty_predicate": (
            "slot 0 is OFF; slots 1..127 are empty when the name pointer is "
            "null or points to a zero-length string, and render as ---"
        ),
        "remaining_gate": (
            "Trace the writer that replaces the blank-name pointer and zero "
            "packed metadata with a non-proprietary RAM sample descriptor."
        ),
        "safety": (
            "Read-only signature analysis; no firmware, project record, sample "
            "descriptor, sample name, or PCM is embedded or modified."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("main_image", type=Path)
    args = parser.parse_args()
    print(json.dumps(trace(args.main_image), indent=2))


if __name__ == "__main__":
    main()
