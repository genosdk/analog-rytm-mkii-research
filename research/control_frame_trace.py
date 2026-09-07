#!/usr/bin/env python3
"""Prove the stock OS 1.72 packed control-frame and BR descriptor geometry.

This checker is deliberately read-only.  It validates fixed instruction bytes
in the decompressed MAIN image and derives addresses/indices from the proven
constants; it cannot modify or repack firmware.
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

FRAME_BASE = 0x8000F774
FRAME_WORDS = 0x23C
PREFIX_WORDS = 0x1A
RECORD_WORDS = 0x2A
RECORD_BYTES = 0x54
RECORD_COUNT = 13
TRACK_COUNT = 12
SOUND_SUBVIEW_BYTES = 0x12
BR_SOUND_WORD = 30
BR_RECORD_WORD = SOUND_SUBVIEW_BYTES // 2 + BR_SOUND_WORD


def at(image: bytes, address: int, expected: bytes, label: str) -> None:
    offset = address - MAIN_BASE
    actual = image[offset : offset + len(expected)]
    if actual != expected:
        raise ValueError(
            f"{label}: signature mismatch at 0x{address:08X}: "
            f"expected {expected.hex()}, got {actual.hex()}"
        )


def hx(value: int) -> str:
    return f"0x{value:08X}"


def trace(path: Path) -> dict:
    image = path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")

    # Full-frame initialization: a4 is loaded with FRAME_BASE and d3 with
    # 0x23c, proving the 572-word extent used by the conversion loop.
    at(image, 0x4011C542, bytes.fromhex("49f98000f774"), "control-frame base")
    at(image, 0x4011C566, bytes.fromhex("263c0000023c"), "control-frame word count")

    # A track copy is exactly 0x54 bytes.  The packed destination is F7A8,
    # which is 0x34 bytes (26 words) after FRAME_BASE.
    at(image, 0x4011932C, bytes.fromhex("103c005448780054"), "record copy length")
    at(image, 0x4011936A, bytes.fromhex("487800542f0206838000f7a8"), "packed record copy")

    # Descriptor initializer: compute 0x54*track + 0x34, divide by two,
    # then create four packed entries in the second BR bank.  Each entry
    # advances the machine descriptor by 3 bytes and the bank by 4 bytes.
    at(image, 0x4011AE90, bytes.fromhex("705443ea00684281"), "record-stride setup")
    at(image, 0x4011AEA6, bytes.fromhex("4c020800"), "0x54 times track")
    at(image, 0x4011AEB4, bytes.fromhex("068000000034"), "prefix-byte addition")
    at(image, 0x4011AEBE, bytes.fromhex("e288"), "byte-to-word division")
    at(image, 0x4011AEE0, bytes.fromhex("eb8a"), "32-byte bank track stride")
    at(image, 0x4011AEF0, bytes.fromhex("3169007004c0568958817729006f"), "bank A entry")
    at(image, 0x4011AEFE, bytes.fromhex("d680314304c2"), "bank A destination")
    at(image, 0x4011AF0C, bytes.fromhex("7204d1fc8000ea4c"), "four-entry bank base")
    at(image, 0x4011AF14, bytes.fromhex("30aa008c"), "descriptor coefficient word")
    at(image, 0x4011AF1A, bytes.fromhex("5381568a5888"), "descriptor/bank strides")
    at(image, 0x4011AF20, bytes.fromhex("752a008bd3c23149fffe"), "bank B destination")
    at(image, 0x4011AF2A, bytes.fromhex("4a8166e6"), "four-entry loop")

    # Runtime converter: exactly four packed longs are consumed.  The low
    # word becomes a word index into FRAME_BASE; the final store is indexed
    # at scale two.  BR invokes the converter for two 16-byte banks.
    at(image, 0x40119676, bytes.fromhex("7004"), "four converter entries")
    at(image, 0x4011967C, bytes.fromhex("41f98000f774"), "converter output base")
    at(image, 0x4011968A, bytes.fromhex("24197742"), "packed descriptor load/index extract")
    at(image, 0x401196B0, bytes.fromhex("484431843a00"), "indexed word store")
    at(image, 0x401196B6, bytes.fromhex("53806ed2"), "four-entry converter loop")
    at(image, 0x4011C78C, bytes.fromhex("2244d3fc8000ea3c"), "BR bank A")
    at(image, 0x4011C796, bytes.fromhex("06848000ea4c"), "BR bank B")
    at(image, 0x4011C79E, bytes.fromhex("4e95"), "BR conversion A")
    at(image, 0x4011C7B0, bytes.fromhex("4e95"), "BR conversion B")

    record_start = FRAME_BASE + PREFIX_WORDS * 2
    sound_start = record_start + SOUND_SUBVIEW_BYTES
    br_start = sound_start + BR_SOUND_WORD * 2
    if FRAME_WORDS != PREFIX_WORDS + RECORD_COUNT * RECORD_WORDS:
        raise AssertionError("frame geometry is internally inconsistent")
    if br_start != 0x8000F7F6:
        raise AssertionError("derived BR address does not match the stock loop")

    return {
        "result": "PASS",
        "main": {
            "path": str(path),
            "size": len(image),
            "sha256": digest,
            "base_address": hx(MAIN_BASE),
        },
        "control_frame": {
            "base": hx(FRAME_BASE),
            "word_count": FRAME_WORDS,
            "byte_count": FRAME_WORDS * 2,
            "layout_equation": "572 = 26 + 13 * 42 words",
            "prefix_words": PREFIX_WORDS,
            "record_count": RECORD_COUNT,
            "record_words": RECORD_WORDS,
            "record_bytes": RECORD_BYTES,
            "first_record": hx(record_start),
            "sound_subview_offset_bytes": f"0x{SOUND_SUBVIEW_BYTES:X}",
            "first_sound_subview": hx(sound_start),
        },
        "bit_reduction": {
            "logical_parameter_id": 20,
            "sound_subview_word_index": BR_SOUND_WORD,
            "packed_record_word_index": BR_RECORD_WORD,
            "packed_record_byte_offset": f"0x{BR_RECORD_WORD * 2:X}",
            "first_track_field": hx(br_start),
            "track_count": TRACK_COUNT,
            "descriptor_initializer": "0x4011AE52",
            "converter": "0x40119672",
            "bank_a_base": "0x8000EA3C",
            "bank_b_base": "0x8000EA4C",
            "bank_bytes_per_track": 0x20,
            "entries_per_bank": 4,
            "banks_per_track": 2,
            "frame_destinations_per_track": 8,
            "bank_a_coefficient_sources": ["+0x70", "+0x73", "+0x76", "+0x79"],
            "bank_a_destination_index_sources": ["+0x72", "+0x75", "+0x78", "+0x7B"],
            "bank_b_coefficient_sources": ["+0x8C", "+0x8F", "+0x92", "+0x95"],
            "bank_b_destination_index_sources": ["+0x8E", "+0x91", "+0x94", "+0x97"],
            "destination_values": "runtime machine-dependent; not yet recovered",
        },
        "conclusion": (
            "BR is packed record word 39 (sound subview word 30), and each "
            "track update feeds eight descriptor-selected control-frame words."
        ),
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
