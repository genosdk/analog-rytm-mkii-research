#!/usr/bin/env python3
"""Identify the stock foreground control-publication boundary for Filter 2."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, RETURN_PC, prepared_machine
from filter2_unity_kernel_probe import MIXER, install_input, install_tables
from trigger_queue_probe import load_emulator, stock_call

MAIN_BASE = 0x40000400
TARGET_ARRAY = 0x8000E57C
TARGET_WORDS = 572
TARGET_END = TARGET_ARRAY + TARGET_WORDS * 2
WORD_SETTER = 0x4011AF4C
WORD_GETTER = 0x4011AF60
FOREGROUND_SETTER_CALLSITE = 0x400B5936
FRAME_BUILDER = 0x4011C542

SETTER_BODY = bytes.fromhex("202f000441f98000e57c43ef000a31910a004e75")
GETTER_BODY = bytes.fromhex("202f000441f98000e57c71f00a004e75")
CALLSITE_BODY = bytes.fromhex("4eb94011af4c")


def at(image: bytes, address: int, expected: bytes, label: str) -> None:
    offset = address - MAIN_BASE
    actual = image[offset : offset + len(expected)]
    if actual != expected:
        raise ValueError(
            f"{label}: signature mismatch at 0x{address:08X}: "
            f"expected {expected.hex()}, got {actual.hex()}"
        )


def absolute_jsr_callsites(image: bytes, target: int) -> list[int]:
    pattern = bytes.fromhex("4eb9") + target.to_bytes(4, "big")
    result = []
    cursor = 0
    while True:
        offset = image.find(pattern, cursor)
        if offset < 0:
            return result
        result.append(MAIN_BASE + offset)
        cursor = offset + 1


def setter_transactions(module, main_path: Path) -> list[dict]:
    bus = module.Bus()
    bus.load_main(main_path)
    cpu = module.CPU(bus)
    cpu.a[7] = module.INITIAL_SP - 0x2000
    vectors = [
        (0, 0x0000), (1, 0x007F), (7, 0x1234), (31, 0x8000),
        (127, 0xFFFF), (255, 0x55AA), (510, 0xAA55), (571, 0x7FFF),
    ]
    results = []
    original_write = bus.write
    for index, value in vectors:
        writes = []

        def traced_write(address: int, size: int, stored: int) -> None:
            if TARGET_ARRAY <= address < TARGET_END:
                writes.append((address, size, stored & ((1 << (size * 8)) - 1)))
            original_write(address, size, stored)

        bus.write = traced_write
        setter_steps = stock_call(cpu, WORD_SETTER, [index, value])
        bus.write = original_write
        getter_steps = stock_call(cpu, WORD_GETTER, [index])
        expected_address = TARGET_ARRAY + index * 2
        if writes != [(expected_address, 2, value)] or cpu.d[0] & 0xFFFF != value:
            raise ValueError(f"indexed setter/getter transaction {index} diverged")
        results.append({
            "word_index": index,
            "value": f"0x{value:04X}",
            "write_address": f"0x{expected_address:08X}",
            "write_size_bytes": 2,
            "setter_instructions": setter_steps,
            "getter_instructions": getter_steps,
            "round_trip_match": True,
        })
    return results


def callback_ownership(module, main_path: Path, stock: bytes) -> dict:
    bus, cpu, _ = prepared_machine(module, main_path)
    install_tables(bus, stock)
    install_input(bus, True)
    reads = []
    writes = []
    visits = Counter()
    original_read, original_write = bus.read, bus.write

    def traced_read(address: int, size: int) -> int:
        value = original_read(address, size)
        if TARGET_ARRAY <= address < TARGET_END:
            reads.append((cpu.pc, address, size, value))
        return value

    def traced_write(address: int, size: int, value: int) -> None:
        if TARGET_ARRAY <= address < TARGET_END:
            writes.append((cpu.pc, address, size, value))
        original_write(address, size, value)

    bus.read, bus.write = traced_read, traced_write
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    start = cpu.steps
    try:
        for _ in range(100_000):
            if cpu.pc == MIXER:
                break
            if cpu.pc in (WORD_SETTER, WORD_GETTER, FOREGROUND_SETTER_CALLSITE, FRAME_BUILDER):
                visits[cpu.pc] += 1
            cpu.step()
        else:
            raise ValueError("stock callback did not reach mixer")
    finally:
        bus.read, bus.write = original_read, original_write

    if writes:
        raise ValueError("audio callback wrote the stock control-target array")
    if visits[WORD_SETTER] or visits[WORD_GETTER] or visits[FOREGROUND_SETTER_CALLSITE]:
        raise ValueError("foreground target accessor executed inside the audio callback")
    if visits[FRAME_BUILDER] != 1:
        raise ValueError("stock frame builder visit count changed")
    read_pcs = Counter(pc for pc, _, _, _ in reads)
    if set(read_pcs) != {0x4011C56E, 0x4011C582, 0x4011C5AA}:
        raise ValueError(f"unexpected target-array callback readers: {read_pcs}")

    return {
        "instructions_callback_to_mixer": cpu.steps - start,
        "target_array_reads": len(reads),
        "target_array_writes": len(writes),
        "read_instruction_counts": {f"0x{pc:08X}": count for pc, count in sorted(read_pcs.items())},
        "frame_builder_visits": visits[FRAME_BUILDER],
        "word_setter_visits": visits[WORD_SETTER],
        "word_getter_visits": visits[WORD_GETTER],
        "foreground_callsite_visits": visits[FOREGROUND_SETTER_CALLSITE],
        "ownership_proved": "callback reader only; indexed setter path absent",
    }


def probe(main_path: Path, emulator_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    at(image, WORD_SETTER, SETTER_BODY, "indexed target-word setter")
    at(image, WORD_GETTER, GETTER_BODY, "indexed target-word getter")
    at(image, FOREGROUND_SETTER_CALLSITE, CALLSITE_BODY, "foreground setter callsite")
    setter_calls = absolute_jsr_callsites(image, WORD_SETTER)
    if setter_calls != [FOREGROUND_SETTER_CALLSITE]:
        raise ValueError(f"indexed word setter is not uniquely called: {setter_calls}")

    module = load_emulator(emulator_path)
    transactions = setter_transactions(module, main_path)
    ownership = callback_ownership(module, main_path, image)
    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "stock_control_target_array": {
            "base": f"0x{TARGET_ARRAY:08X}",
            "word_count": TARGET_WORDS,
            "end_exclusive": f"0x{TARGET_END:08X}",
            "indexed_word_address_equation": "0x8000E57C + 2 * word_index",
        },
        "publication_boundary": {
            "selected_interception_callsite": f"0x{FOREGROUND_SETTER_CALLSITE:08X}",
            "stock_word_setter": f"0x{WORD_SETTER:08X}",
            "stock_word_getter": f"0x{WORD_GETTER:08X}",
            "absolute_setter_callsites": [f"0x{address:08X}" for address in setter_calls],
            "selection_reason": (
                "The callsite is the sole absolute caller of the five-instruction indexed "
                "word setter and is never reached by the audio callback. A future shim can "
                "tail-call the untouched setter for ordinary indices and intercept only a "
                "separately proved Filter 2 command range."
            ),
        },
        "synthetic_setter_transactions": transactions,
        "audio_callback_ownership": ownership,
        "conclusion": (
            "The stock control handoff is split cleanly: the unique foreground callsite "
            "0x400B5936 publishes indexed 16-bit target words through 0x4011AF4C, while the "
            "audio callback only reads the target array during frame construction. This is "
            "the safest identified interception point for publishing Filter 2 shadow controls "
            "without adding mapping work to the audio-critical callback."
        ),
        "scope_limit": (
            "This identifies and executes the stock publication primitive but does not yet "
            "assign collision-free virtual parameter indices or patch the callsite. Atomic "
            "aligned 32-bit Q1.31 shadow stores must be proved in the next gate."
        ),
        "next_target": (
            "Build a default-pass-through publication shim at 0x400B5936. Prove ordinary "
            "stock word writes remain identical, then use an isolated synthetic command range "
            "to map eight 7-bit controls into eight aligned Q1.31 Filter 2 target words."
        ),
        "safety": "Read-only firmware analysis and emulated RAM transactions; no candidate image or SysEx was built.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_main", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = probe(args.stock_main, args.emulator)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.report:
        args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
