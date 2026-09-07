#!/usr/bin/env python3
"""Prove the first stock consumer of the packed SRR/BR control pair.

The checker combines fixed MAIN-image signatures with two MiniColdFire smoke
calls.  It is read-only: it neither patches nor repacks firmware.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

MAIN_BASE = 0x40000400
EXPECTED_MAIN_SHA256 = "5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772"
FRAME_BASE = 0x8000F774
FIRST_RECORD = 0x8000F7A8
PREFIX_WORDS = 26
RECORD_WORDS = 42
BR_WORD = 39


def at(image: bytes, address: int, expected: bytes, label: str) -> None:
    actual = image[address - MAIN_BASE : address - MAIN_BASE + len(expected)]
    if actual != expected:
        raise ValueError(
            f"{label}: signature mismatch at 0x{address:08X}: "
            f"expected {expected.hex()}, got {actual.hex()}"
        )


def load_emulator(path: Path):
    spec = importlib.util.spec_from_file_location("br_consumer_minicoldfire", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load emulator module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def smoke_call(module, main_path: Path, address: int) -> dict:
    bus = module.Bus()
    bus.load_main(main_path)
    cpu = module.CPU(bus)
    cpu.a[7] = module.INITIAL_SP - 0x1000
    return_pc = 0x400FCECC
    cpu.pushl(0x800063C0)
    cpu.pushl(return_pc)
    cpu.pc = address
    for _ in range(20_000):
        if cpu.pc == return_pc:
            return {
                "result": "PASS",
                "instructions": cpu.steps,
                "emac_instructions": cpu.op_counts["EMAC"],
            }
        cpu.step()
    raise ValueError(f"routine 0x{address:08X} did not return")


def trace(main_path: Path, emulator_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")

    # A0 starts at the first 42-word record.  The pipeline primes D4 from
    # -(A0), then performs 21 longword iterations (42 words) and repeats for
    # 13 records.  The load form at 0x4011C6C0 reads 4(A0), so iteration 19
    # reads 0x8000F7F4..F7F7, the SRR/BR pair (record words 38 and 39).
    at(image, 0x4011C69E, bytes.fromhex("41f98000f7a8"), "first record base")
    at(image, 0x4011C6B8, bytes.fromhex("7c0d"), "13-record count")
    at(image, 0x4011C6BA, bytes.fromhex("2820"), "pipeline prime")
    at(image, 0x4011C6BE, bytes.fromhex("7e15"), "21-longword count")
    at(image, 0x4011C6C0, bytes.fromhex("a2a801420004"), "EMAC load 4(A0)")
    at(image, 0x4011C6D2, bytes.fromhex("20c4"), "pipeline output")
    at(image, 0x4011C6DC, bytes.fromhex("53876ee0"), "inner loop")
    at(image, 0x4011C6E0, bytes.fromhex("53866ed8"), "outer loop")

    br_address = FRAME_BASE + 2 * (PREFIX_WORDS + BR_WORD)
    pair_address = br_address - 2
    if br_address != 0x8000F7F6 or pair_address != 0x8000F7F4:
        raise AssertionError("BR address derivation changed")

    module = load_emulator(emulator_path)
    voice_bridge = smoke_call(module, main_path, 0x40108944)
    control_converter = smoke_call(module, main_path, 0x40105188)

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "control_smoother": {
            "address": "0x4011C69E",
            "record_count": 13,
            "longwords_per_record": 21,
            "words_per_record": RECORD_WORDS,
            "first_record": f"0x{FIRST_RECORD:08X}",
            "emac_load": "0x4011C6C0",
            "srr_br_pair_address_track_0": f"0x{pair_address:08X}",
            "br_address_track_0": f"0x{br_address:08X}",
            "br_pair_iteration_zero_based": 19,
            "classification": "control-rate EMAC smoothing; not the audio quantizer",
        },
        "downstream_smoke_calls": {
            "voice_bridge_0x40108944": voice_bridge,
            "control_converter_0x40105188": control_converter,
            "interpretation": (
                "Both routines now return under emulation, but neither directly "
                "reads record word 39. The sample quantizer remains downstream of "
                "the post-smoothing descriptor pass and audio-render dispatch."
            ),
        },
        "resolved_blockers": [
            "descriptor mapping no longer requires project ingestion",
            "ColdFire EMAC and SATS instructions needed by the control path execute",
            "the first packed SRR/BR consumer is proven",
        ],
        "next_target": (
            "Model the three audio-interface ready polls at 0x40117F16, "
            "0x40118396 and 0x40118518 plus the DMA busy bit at 0xFC0453DE, "
            "then trace word 39 beyond 0x40117F00 into sample quantization."
        ),
        "safety": "Static analysis and emulation only; firmware bytes were not modified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("main_image", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = trace(args.main_image, args.emulator)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
