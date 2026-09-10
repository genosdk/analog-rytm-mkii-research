#!/usr/bin/env python3
"""Establish the post-ingress Filter 2 numeric contract under emulation.

The probe executes the stock output loop, observes its EMAC mode at every
accumulator extraction, exercises boundary values through the stock extraction
instructions, and measures the semantic-instruction overhead of the inert cave
detour.  It deliberately makes no wall-clock or hardware cycle claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections import Counter
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, RETURN_PC, prepared_machine
from filter2_bypass_canary_probe import build_candidate
from post_voice_ingress_probe import (
    ACTIVE_INPUT_WORD,
    DMA_BLOCK_BYTES,
    EXTERNAL_AUDIO_WINDOW,
    INGRESS,
    INGRESS_RETURN,
    OUTPUT_PLANE,
    OUTPUT_WORDS,
    install_tables,
)
from trigger_queue_probe import load_emulator

MAIN_LOAD = 0x40000400
MIXER = 0x4010A2E0
LOOP_FIRST = 0x4011877A
EXTRACT_ACC2 = 0x40118796
STORE_ACC2 = 0x40118798
EXTRACT_ACC3 = 0x4011879A
STORE_ACC3 = 0x4011879C
LOOP_BRANCH = 0x401187A0
LOOP_SIGNATURE_ADDRESS = 0x40118778
LOOP_SIGNATURE = bytes.fromhex(
    "7010a1c1e5a1a3c6e5a6a0da1817de85ac1a6817de85a0430800"
    "ac830800a5c82cc8a7c82cc8538066d8"
)
FRACTIONAL_MODE = 0x20
SATURATION_ENABLE = 0x80


def read_block(bus, address: int, length: int) -> bytes:
    return bytes(bus.read(address + index, 1) for index in range(length))


def signed32(value: int) -> int:
    return value - 0x1_0000_0000 if value & 0x8000_0000 else value


def install_input(bus, active: bool) -> None:
    payload = ACTIVE_INPUT_WORD.to_bytes(4, "big") * 36 if active else bytes(DMA_BLOCK_BYTES)
    for index, value in enumerate(payload):
        bus.write(EXTERNAL_AUDIO_WINDOW + index, 1, value)


def run_stock_loop(module, main_path: Path, image: bytes) -> dict:
    bus, cpu, _ = prepared_machine(module, main_path)
    install_tables(bus, image)
    install_input(bus, True)
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    for _ in range(100_000):
        if cpu.pc == INGRESS:
            break
        cpu.step()
    else:
        raise ValueError("callback did not reach stock ingress")

    extraction_modes = Counter()
    extraction_counts = Counter()
    store_events = []
    original_write = bus.write

    def traced_write(address: int, size: int, value: int) -> None:
        if OUTPUT_PLANE <= address < OUTPUT_PLANE + OUTPUT_WORDS * 4:
            # Bus writes occur after the instruction word has been consumed, so
            # cpu.pc is the following instruction (0x...879A or 0x...879E).
            store_events.append((cpu.pc, address, size, value & 0xFFFFFFFF))
        original_write(address, size, value)

    bus.write = traced_write
    start = cpu.steps
    for _ in range(30_000):
        if cpu.pc == INGRESS_RETURN:
            break
        if cpu.pc in (EXTRACT_ACC2, EXTRACT_ACC3):
            extraction_counts[cpu.pc] += 1
            extraction_modes[cpu.macsr] += 1
        cpu.step()
    else:
        raise ValueError("stock ingress did not return")
    bus.write = original_write

    addresses = [event[1] for event in store_events]
    expected_addresses = [OUTPUT_PLANE + 4 * index for index in range(OUTPUT_WORDS)]
    if addresses != expected_addresses:
        raise ValueError("post-ingress output is not 256 sequential longwords")
    if extraction_counts != Counter({EXTRACT_ACC2: 128, EXTRACT_ACC3: 128}):
        raise ValueError(f"unexpected extraction cadence: {extraction_counts}")
    if any((mode & 0xE0) != FRACTIONAL_MODE for mode in extraction_modes):
        raise ValueError(f"unexpected EMAC data mode: {extraction_modes}")
    if any(mode & SATURATION_ENABLE for mode in extraction_modes):
        raise ValueError(f"saturation unexpectedly enabled: {extraction_modes}")
    if Counter(event[0] for event in store_events) != Counter({EXTRACT_ACC3: 128, 0x4011879E: 128}):
        raise ValueError("unexpected paired store instruction cadence")

    words = [event[3] for event in store_events]
    signed = [signed32(value) for value in words]
    return {
        "instructions": cpu.steps - start,
        "loop": {
            "inner_iterations": 128,
            "pairs_per_inner_iteration": 1,
            "outer_lanes": 8,
            "words_per_lane": 32,
            "extract_instruction_counts": {
                f"0x{pc:08X}": count for pc, count in sorted(extraction_counts.items())
            },
            "observed_macsr": {
                f"0x{mode:08X}": count for mode, count in sorted(extraction_modes.items())
            },
            "saturation_enable_bit_seen": False,
            "sequential_longword_writes": len(store_events),
            "first": f"0x{addresses[0]:08X}",
            "last": f"0x{addresses[-1]:08X}",
        },
        "active_vector": {
            "input_word": f"0x{ACTIVE_INPUT_WORD:08X}",
            "signed_min": min(signed),
            "signed_max": max(signed),
            "negative_words": sum(value < 0 for value in signed),
            "positive_words": sum(value > 0 for value in signed),
            "zero_words": sum(value == 0 for value in signed),
            "sha256": hashlib.sha256(b"".join(value.to_bytes(4, "big") for value in words)).hexdigest(),
        },
    }


def exercise_stock_extraction(module, main_path: Path) -> list[dict]:
    cases = [
        ("positive_limit", 0x7FFFFFFF << 8, 0x7FFFFFFF),
        ("negative_limit", (-0x80000000 << 8) & 0xFFFFFFFFFFFFFFFF, 0x80000000),
        ("positive_overflow_wrap", 0x80000000 << 8, 0x80000000),
        ("negative_overflow_wrap", (-0x80000001 << 8) & 0xFFFFFFFFFFFFFFFF, 0x7FFFFFFF),
    ]
    results = []
    for name, accumulator, expected in cases:
        bus = module.Bus()
        bus.load_main(main_path)
        cpu = module.CPU(bus)
        cpu.pc = EXTRACT_ACC2
        cpu.macsr = FRACTIONAL_MODE
        cpu.macc[2] = accumulator
        cpu.a[6] = 0x80001000
        if cpu.step() != "FROM_MAC ACC2" or cpu.step() != "MOVE.L":
            raise ValueError("stock extraction instruction sequence changed")
        actual = bus.read(0x80001000, 4)
        if actual != expected:
            raise ValueError(f"{name}: expected 0x{expected:08X}, got 0x{actual:08X}")
        results.append({
            "case": name,
            "accumulator_48_bit_fractional": f"0x{accumulator & ((1 << 48) - 1):012X}",
            "stored_word": f"0x{actual:08X}",
        })
    return results


def instructions_to_mixer(module, main_path: Path, stock_image: bytes, active: bool) -> int:
    bus, cpu, _ = prepared_machine(module, main_path)
    install_tables(bus, stock_image)
    install_input(bus, active)
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    start = cpu.steps
    for _ in range(100_000):
        if cpu.pc == MIXER:
            return cpu.steps - start
        cpu.step()
    raise ValueError("callback did not reach mixer")


def measure_detour(module, stock_path: Path, stock_image: bytes) -> dict:
    candidate, _ = build_candidate(stock_image)
    rows = []
    with tempfile.NamedTemporaryFile(suffix=".bin") as temporary:
        candidate_path = Path(temporary.name)
        candidate_path.write_bytes(candidate)
        for active in (False, True):
            stock = instructions_to_mixer(module, stock_path, stock_image, active)
            patched = instructions_to_mixer(module, candidate_path, stock_image, active)
            if patched - stock != 2:
                raise ValueError(f"unexpected inert-detour overhead: stock={stock}, candidate={patched}")
            rows.append({
                "input": "active" if active else "zero",
                "stock_instructions_to_mixer": stock,
                "candidate_instructions_to_mixer": patched,
                "added_semantic_instructions": patched - stock,
            })
    return {
        "measurement": rows,
        "scope": (
            "Semantic instructions in MiniColdFire, not processor cycles. Cache, SDRAM, "
            "DMA contention, and interrupt timing require measurement on hardware."
        ),
    }


def probe(main_path: Path, emulator_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    signature_offset = LOOP_SIGNATURE_ADDRESS - MAIN_LOAD
    actual_signature = image[signature_offset : signature_offset + len(LOOP_SIGNATURE)]
    if actual_signature != LOOP_SIGNATURE:
        raise ValueError(f"stock output-loop signature changed: {actual_signature.hex()}")

    module = load_emulator(emulator_path)
    loop = run_stock_loop(module, main_path, image)
    boundary = exercise_stock_extraction(module, main_path)
    overhead = measure_detour(module, main_path, image)
    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "stock_output_loop": {
            "signature_address": f"0x{LOOP_SIGNATURE_ADDRESS:08X}",
            "signature": LOOP_SIGNATURE.hex(),
            "extract_acc2": f"0x{EXTRACT_ACC2:08X}",
            "store_acc2": f"0x{STORE_ACC2:08X}",
            "extract_acc3": f"0x{EXTRACT_ACC3:08X}",
            "store_acc3": f"0x{STORE_ACC3:08X}",
            **loop,
        },
        "controlled_boundary_cases": boundary,
        "inert_detour_overhead": overhead,
        "numeric_contract": (
            "The 0x800067F8 plane contains signed 32-bit fractional results extracted "
            "from 48-bit EMAC accumulators (Q1.31-domain words). The stock loop does not "
            "enable EMAC saturation; values outside the signed 32-bit fractional range "
            "wrap at extraction rather than clamp. A Filter 2 must therefore preserve "
            "this word format and choose explicit internal headroom/saturation behavior."
        ),
        "cycle_contract": (
            "The inert detour costs exactly two additional modeled instructions before "
            "the stock mixer for both vectors. This proves structural overhead only; "
            "available real-time cycle margin remains a hardware-timer measurement gate."
        ),
        "next_target": (
            "Audit a larger zero-filled writable-SDRAM cave span for references and runtime "
            "liveness, then place a versioned disabled-by-default Filter 2/LFO2 state header."
        ),
        "safety": "Emulation and read-only stock analysis; no flashable firmware was built.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("main_image", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = probe(args.main_image, args.emulator)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
