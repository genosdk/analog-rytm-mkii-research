#!/usr/bin/env python3
"""Build and execute an inert post-ingress Filter 2 canary in MAIN.

This produces a decompressed research-only MAIN image, never a flashable SysEx.
The detour calls the untouched 0x40117F00 routine, returns without modifying
state, then executes the next stock function at 0x4010A2E0.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, RETURN_PC, prepared_machine
from post_voice_ingress_probe import (
    ACTIVE_INPUT_WORD,
    DMA_BLOCK_BYTES,
    EXTERNAL_AUDIO_WINDOW,
    OUTPUT_PLANE,
    install_sample_levels,
    install_tables,
)
from trigger_queue_probe import load_emulator

MAIN_LOAD = 0x40000400
CALL_SITE = 0x4011CACC
ORIGINAL_CALL = bytes.fromhex("4eb940117f00")
CAVE = 0x402B4340
CAVE_BODY = bytes.fromhex("4eb940117f004e75")
INGRESS = 0x40117F00
INGRESS_RETURN = 0x4011CAD2
MIXER = 0x4010A2E0
MIXER_RETURN = 0x4011CAE8
MIX_INPUT_FIRST = 0x800067F8
MIX_INPUT_BYTES = 0xC48
MIX_OUTPUT_FIRST = 0x80000800
MIX_FRAMES = 32
MIX_LANES = 8


def offset(address: int) -> int:
    return address - MAIN_LOAD


def build_candidate(stock: bytes) -> tuple[bytes, dict]:
    call_offset = offset(CALL_SITE)
    cave_offset = offset(CAVE)
    if stock[call_offset : call_offset + len(ORIGINAL_CALL)] != ORIGINAL_CALL:
        raise ValueError("stock ingress call-site signature changed")
    if stock[cave_offset : cave_offset + len(CAVE_BODY)] != bytes(len(CAVE_BODY)):
        raise ValueError("selected cave is not zero-filled in stock MAIN")

    cave_end = CAVE + len(CAVE_BODY)
    aligned_literals = []
    absolute_transfers = []
    relative_branches = []
    for position in range(0, len(stock) - 5, 2):
        address = MAIN_LOAD + position
        value = int.from_bytes(stock[position : position + 4], "big")
        if CAVE <= value < cave_end:
            aligned_literals.append(f"0x{address:08X}")
        opcode = int.from_bytes(stock[position : position + 2], "big")
        absolute_target = int.from_bytes(stock[position + 2 : position + 6], "big")
        if opcode in (0x4EB9, 0x4EF9) and CAVE <= absolute_target < cave_end:
            absolute_transfers.append(f"0x{address:08X}")
        if opcode & 0xF000 == 0x6000:
            low = opcode & 0xFF
            base = address + 2
            if low == 0 and position + 4 <= len(stock):
                displacement = int.from_bytes(stock[position + 2 : position + 4], "big", signed=True)
            elif low == 0xFF and position + 6 <= len(stock):
                displacement = int.from_bytes(stock[position + 2 : position + 6], "big", signed=True)
            else:
                displacement = low - 0x100 if low & 0x80 else low
            target = (base + displacement) & 0xFFFFFFFF
            if CAVE <= target < cave_end:
                relative_branches.append(f"0x{address:08X}")
    if aligned_literals or absolute_transfers or relative_branches:
        raise ValueError("stock MAIN already references selected cave")

    candidate = bytearray(stock)
    replacement_call = bytes.fromhex("4eb9") + CAVE.to_bytes(4, "big")
    candidate[call_offset : call_offset + len(replacement_call)] = replacement_call
    candidate[cave_offset : cave_offset + len(CAVE_BODY)] = CAVE_BODY
    changed = [index for index, pair in enumerate(zip(stock, candidate)) if pair[0] != pair[1]]
    return bytes(candidate), {
        "call_site": f"0x{CALL_SITE:08X}",
        "original_call": ORIGINAL_CALL.hex(),
        "replacement_call": replacement_call.hex(),
        "cave": f"0x{CAVE:08X}",
        "cave_body": CAVE_BODY.hex(),
        "changed_bytes": len(changed),
        "changed_ranges": [
            {"address": f"0x{CALL_SITE:08X}", "bytes": len(replacement_call)},
            {"address": f"0x{CAVE:08X}", "bytes": len(CAVE_BODY)},
        ],
        "stock_cave_references": {
            "aligned_literals": aligned_literals,
            "absolute_jmp_jsr": absolute_transfers,
            "relative_branches": relative_branches,
        },
    }


def read_bytes(bus, address: int, length: int) -> bytes:
    return bytes(bus.read(address + index, 1) for index in range(length))


def register_state(cpu) -> dict:
    return {
        "d": [f"0x{value:08X}" for value in cpu.d],
        "a": [f"0x{value:08X}" for value in cpu.a],
        "sr": f"0x{cpu.sr:04X}",
        "macsr": f"0x{cpu.macsr:08X}",
        "mask": f"0x{cpu.mac_mask:08X}",
        "accumulators": [f"0x{value & 0xFFFFFFFFFFFFFFFF:016X}" for value in cpu.macc],
    }


def execute_vector(module, main_path: Path, stock_image: bytes, active: bool, candidate: bool) -> dict:
    bus, cpu, _ = prepared_machine(module, main_path)
    install_tables(bus, stock_image)
    payload = ACTIVE_INPUT_WORD.to_bytes(4, "big") * 36 if active else bytes(DMA_BLOCK_BYTES)
    for index, value in enumerate(payload):
        bus.write(EXTERNAL_AUDIO_WINDOW + index, 1, value)

    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    landmarks = []
    for _ in range(100_000):
        if cpu.pc in (CAVE, INGRESS, INGRESS_RETURN, MIXER):
            landmarks.append(f"0x{cpu.pc:08X}")
        if cpu.pc == INGRESS:
            install_sample_levels(bus)
        if cpu.pc == MIXER:
            break
        cpu.step()
    else:
        raise ValueError("callback did not reach the stock combiner")

    mixer_input = read_bytes(bus, MIX_INPUT_FIRST, MIX_INPUT_BYTES)
    at_mixer = register_state(cpu)
    output_writes = []
    original_write = bus.write

    def traced_write(address: int, size: int, value: int) -> None:
        if 0x80000800 <= address < 0x80001000:
            output_writes.append((cpu.pc, address, size))
        original_write(address, size, value)

    bus.write = traced_write
    mixer_start = cpu.steps
    for _ in range(10_000):
        if cpu.pc == MIXER_RETURN:
            break
        cpu.step()
    else:
        raise ValueError("stock combiner did not return")
    bus.write = original_write

    expected_addresses = [
        MIX_OUTPUT_FIRST + frame * 0x40 + lane * 4
        for lane in range(MIX_LANES)
        for frame in range(MIX_FRAMES)
    ]
    actual_addresses = [address for pc, address, size in output_writes if pc == 0x4010A3B8 and size == 4]
    if actual_addresses != expected_addresses:
        raise ValueError(
            f"unexpected combiner write order: count={len(actual_addresses)} "
            f"first={[hex(x) for x in actual_addresses[:16]]}"
        )
    output_words = b"".join(bus.read(address, 4).to_bytes(4, "big") for address in expected_addresses)
    expected_route = [f"0x{INGRESS:08X}", f"0x{INGRESS_RETURN:08X}", f"0x{MIXER:08X}"]
    if candidate:
        expected_route.insert(0, f"0x{CAVE:08X}")
    if landmarks != expected_route:
        raise ValueError(f"unexpected callback route: {landmarks}")
    return {
        "image": "candidate" if candidate else "stock",
        "input": "active" if active else "zero",
        "landmarks": landmarks,
        "mixer_entry_registers": at_mixer,
        "mixer_input_sha256": hashlib.sha256(mixer_input).hexdigest(),
        "ingress_output_sha256": hashlib.sha256(read_bytes(bus, OUTPUT_PLANE, 0x400)).hexdigest(),
        "mixer_instructions": cpu.steps - mixer_start,
        "mixer_output_writes": len(actual_addresses),
        "mixer_output_sha256": hashlib.sha256(output_words).hexdigest(),
    }


def probe(stock_path: Path, emulator_path: Path, candidate_output: Path | None = None) -> dict:
    stock = stock_path.read_bytes()
    stock_hash = hashlib.sha256(stock).hexdigest()
    if stock_hash != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {stock_hash}")
    candidate, build = build_candidate(stock)
    candidate_hash = hashlib.sha256(candidate).hexdigest()

    temporary = None
    if candidate_output is None:
        temporary = tempfile.NamedTemporaryFile(suffix=".bin")
        candidate_path = Path(temporary.name)
    else:
        candidate_path = candidate_output
    candidate_path.write_bytes(candidate)

    module = load_emulator(emulator_path)
    comparisons = []
    try:
        for active in (False, True):
            stock_run = execute_vector(module, stock_path, stock, active, False)
            candidate_run = execute_vector(module, candidate_path, stock, active, True)
            comparable_fields = (
                "mixer_entry_registers", "mixer_input_sha256", "ingress_output_sha256",
                "mixer_instructions", "mixer_output_writes", "mixer_output_sha256",
            )
            equality = {field: stock_run[field] == candidate_run[field] for field in comparable_fields}
            if not all(equality.values()):
                raise ValueError(f"candidate diverged from stock: {equality}")
            comparisons.append({
                "input": "active" if active else "zero",
                "stock": stock_run,
                "candidate": candidate_run,
                "bit_identical": equality,
            })
    finally:
        if temporary is not None:
            temporary.close()

    return {
        "result": "PASS",
        "stock": {"path": str(stock_path), "sha256": stock_hash},
        "candidate": {
            "path": str(candidate_output) if candidate_output else "temporary execution image",
            "sha256": candidate_hash,
            **build,
        },
        "comparisons": comparisons,
        "conclusion": (
            "The inert cave detour calls the untouched external-audio ingress and returns "
            "with mixer-entry registers and all relevant input bytes identical to stock. "
            "The next function, stock combiner 0x4010A2E0, then executes 3,227 instructions, "
            "performs 256 writes, and produces a bit-identical output for zero and active "
            "external-input vectors. The control-flow hook is viable under emulation."
        ),
        "safety": (
            "Decompressed MAIN lab image only. No ELE3 container or SysEx was built; "
            "the candidate is not flashable."
        ),
        "next_target": (
            "Measure cycle headroom and establish the 0x800067F8 numeric/saturation contract "
            "before adding one disabled-by-default Filter 2 state structure."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_main", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument("--candidate-output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = probe(args.stock_main, args.emulator, args.candidate_output)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.report:
        args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
