#!/usr/bin/env python3
"""Reconstruct Analog Rytm MKII OS 1.72 sample-BR hardware command encoding.

Read-only. Verifies the stock MAIN image and the renderer case-3 instruction
window, then models the exact nonnegative 16-bit BR-word -> per-voice hardware
command transform used before DSPI packetization.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

MAIN_BASE = 0x40000400
STOCK_MAIN_SHA256 = "5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772"
BR_FRAME_ADDRESS = 0x8000F7BE
BR_FRAME_READ = 0x4010CC58
BR_CASE3 = 0x4010D164
BR_CACHE_READ = 0x4010D16E
BR_PACK_WRITE = 0x4010D1E8
BR_PACK_ADDRESS = 0x80006544
FIXED_MULTIPLIER = 0x40810204
COMMAND_BASE = 0xB3100000

CASE3_TRANSFORM_SIGNATURE = bytes.fromhex(
    "716f004e"
    "223c40810204"
    "4203"
    "7a80"
    "e280"
    "068000003fff"
    "4840"
    "4240"
    "a0010800"
    "a1c0"
)

KNOWN_VECTORS = {
    0x0000: 0xB31407FF,
    0x0001: 0xB31407FF,
    0x0002: 0xB3140810,
    0x0003: 0xB3140810,
    0x00FF: 0xB3140FFF,
    0x0100: 0xB3141010,
    0x1234: 0xB3149AC5,
    0x3FFF: 0xB3160BF7,
    0x4000: 0xB3160C08,
    0x6000: 0xB3170E0C,
    0x7E18: 0xB31800B1,
    0x7F00: 0xB31807FF,
    0x7FFF: 0xB3180FFF,
}


def at(image: bytes, address: int, expected: bytes, label: str) -> None:
    off = address - MAIN_BASE
    got = image[off:off + len(expected)]
    if got != expected:
        raise ValueError(
            f"{label}: signature mismatch at 0x{address:08X}: "
            f"expected {expected.hex()}, got {got.hex()}"
        )


def br_command(raw_br: int) -> int:
    """Exact positive-domain transform used by renderer case 3."""
    if not 0 <= raw_br <= 0x7FFF:
        raise ValueError("raw_br must be in the proven nonnegative domain 0x0000..0x7fff")
    u = (raw_br >> 1) + 0x3FFF
    scalar = (u * FIXED_MULTIPLIER) >> 26
    return COMMAND_BASE | (scalar & 0x000FFFFF)


def validate_formula() -> dict:
    for raw, expected in KNOWN_VECTORS.items():
        got = br_command(raw)
        if got != expected:
            raise AssertionError(f"vector 0x{raw:04X}: got 0x{got:08X}, expected 0x{expected:08X}")

    pair_mismatches = []
    unique = set()
    monotonic = True
    previous = None
    for raw in range(0x8000):
        cmd = br_command(raw)
        unique.add(cmd)
        if previous is not None and cmd < previous:
            monotonic = False
        previous = cmd
        if raw % 2 == 0 and raw + 1 < 0x8000 and cmd != br_command(raw + 1):
            pair_mismatches.append(raw)

    return {
        "known_vectors": len(KNOWN_VECTORS),
        "domain_words": 0x8000,
        "unique_commands": len(unique),
        "effective_input_resolution_bits": 14,
        "adjacent_even_odd_pairs_identical": not pair_mismatches,
        "monotonic": monotonic,
        "command_first": f"0x{br_command(0):08X}",
        "command_last": f"0x{br_command(0x7FFF):08X}",
    }


def probe(main_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != STOCK_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")

    at(image, 0x4010CC50, bytes.fromhex("374000743a2e0008362e00047f6e0006"), "sample parameter cache including BR")
    at(image, BR_CASE3, bytes.fromhex("700fb0ac00046d0000a4"), "case-3 bounded-state entry")
    at(image, BR_CACHE_READ, CASE3_TRANSFORM_SIGNATURE, "case-3 BR transform")
    at(image, BR_PACK_WRITE, bytes.fromhex("2541001c262c0004"), "packed-control write vicinity")

    validation = validate_formula()
    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "proven_path": {
            "track0_br_frame_address": f"0x{BR_FRAME_ADDRESS:08X}",
            "frame_read_instruction": f"0x{BR_FRAME_READ:08X}",
            "case3_entry": f"0x{BR_CASE3:08X}",
            "cached_br_read": f"0x{BR_CACHE_READ:08X}",
            "packed_command_write": f"0x{BR_PACK_WRITE:08X}",
            "packed_command_address": f"0x{BR_PACK_ADDRESS:08X}",
        },
        "exact_positive_domain_equation": {
            "input": "b = nonnegative 16-bit renderer BR word, 0x0000..0x7fff",
            "step_1": "u = (b >> 1) + 0x3fff",
            "step_2": "scalar = (u * 0x40810204) >> 26",
            "step_3": "command = 0xB3100000 | (scalar & 0x000fffff)",
            "note": "The initial arithmetic shift discards bit 0, so adjacent even/odd BR words collapse to one hardware-control value.",
        },
        "validation": validation,
        "selected_vectors": [
            {"raw_br": f"0x{k:04X}", "command": f"0x{v:08X}"}
            for k, v in KNOWN_VECTORS.items()
        ],
        "interpretation": (
            "This is the CPU-side normalized BR hardware-control encoder. It does not establish "
            "the FPGA/audio-engine quantization staircase, rounding rule, or effective audio bit depth."
        ),
        "safety": "Read-only static reconstruction; no firmware bytes are modified.",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("main_image", type=Path)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    result = probe(args.main_image)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
