#!/usr/bin/env python3
"""Verify the stock OS 1.72 Bit Reduction control-to-runtime bridge.

This is a static, read-only signature checker. It consumes the decompressed
MAIN image and emits JSON; it never modifies or repacks firmware.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


MAIN_BASE = 0x40000400
EXPECTED_MAIN_SHA256 = (
    "5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772"
)


def at(image: bytes, address: int, expected: bytes, label: str) -> None:
    offset = address - MAIN_BASE
    actual = image[offset : offset + len(expected)]
    if actual != expected:
        raise ValueError(
            f"{label}: signature mismatch at 0x{address:08X}: "
            f"expected {expected.hex()}, got {actual.hex()}"
        )


def trace(path: Path) -> dict:
    image = path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")

    # The render callback invokes the renderer, then the 8-voice bridge, then
    # the following control conversion routine, all on the same control block.
    at(image, 0x4011CAE2, bytes.fromhex("4eb94010a2e0"), "8-channel renderer call")
    at(image, 0x4011CAE8, bytes.fromhex("4879800063c0"), "control-block argument")
    at(image, 0x4011CAEE, bytes.fromhex("4eb940108944"), "8-voice bridge call")
    at(image, 0x4011CAF4, bytes.fromhex("4879800063c0"), "control-block argument 2")
    at(image, 0x4011CAFA, bytes.fromhex("4eb940105188"), "control conversion call")

    # One record is 0x54 bytes: 42 packed 16-bit parameter values. The table
    # maps the eight physical voice slots to logical tracks.
    mapping_address = 0x40278A44
    mapping_offset = mapping_address - MAIN_BASE
    voice_to_track = list(image[mapping_offset : mapping_offset + 8])
    if voice_to_track != [0, 4, 1, 5, 8, 6, 10, 2]:
        raise ValueError(f"unexpected voice mapping: {voice_to_track}")
    at(image, 0x40108978, bytes.fromhex("7054"), "0x54-byte record stride")
    at(image, 0x401051CE, bytes.fromhex("7a54"), "0x54-byte record stride 2")

    # BR is word 30 within the sound_t subview, which begins 0x12 bytes into
    # the packed record.  It is therefore packed-record word 39 (+0x4e).
    # This loop walks the field directly for 12 tracks and converts it twice.
    at(image, 0x4011C722, bytes.fromhex("2e3c8000f7f6"), "BR field base")
    at(image, 0x4011C77A, bytes.fromhex("068700000054"), "next track record")
    at(image, 0x4011C7AC, bytes.fromhex("5482"), "next track index")
    at(image, 0x4011C7B6, bytes.fromhex("7218b282669c"), "12-track loop bound")
    at(image, 0x4011C79E, bytes.fromhex("4e95"), "BR conversion A")
    at(image, 0x4011C7B0, bytes.fromhex("4e95"), "BR conversion B")
    at(image, 0x40119672, bytes.fromhex("4feffff0700448d7003c"), "converter prologue")

    return {
        "result": "PASS",
        "main": {
            "path": str(path),
            "size": len(image),
            "sha256": digest,
            "base_address": f"0x{MAIN_BASE:08X}",
        },
        "render_sequence": [
            {"address": "0x4010A2E0", "role": "8-channel renderer"},
            {"address": "0x40108944", "role": "8-physical-voice control bridge"},
            {"address": "0x40105188", "role": "per-voice control conversion"},
        ],
        "track_parameter_record": {
            "stride_bytes": 0x54,
            "word_count": 42,
            "physical_voice_to_logical_track": voice_to_track,
        },
        "bit_reduction": {
            "logical_parameter_id": 20,
            "sound_subview_word_index": 30,
            "sound_subview_byte_offset": "0x3C",
            "packed_record_word_index": 39,
            "packed_record_byte_offset": "0x4E",
            "field_base": "0x8000F7F6",
            "loop_address": "0x4011C722",
            "track_count": 12,
            "modulation_flags_base": "0x8000E53C",
            "modulation_values_base": "0x800067C4",
            "coefficient_table_a": "0x8000EA3C",
            "coefficient_table_b": "0x8000EA4C",
            "converter": "0x40119672",
            "converter_output_base": "0x8000F774",
        },
        "safety": "Static analysis only; no firmware was modified or repacked.",
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
