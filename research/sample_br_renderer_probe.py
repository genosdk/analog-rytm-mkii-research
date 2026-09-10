#!/usr/bin/env python3
"""Prove MAIN's stock machine renderer consumes and encodes a sample-BR frame.

This emulation-only probe installs stock machine 0 for physical voice 0, then
injects a synthetic BR halfword at the renderer boundary and selects the
bounded state-machine case that handles it. It does not patch firmware or
claim that the resulting packed word is PCM. Natural BR construction is
covered separately by trigger_queue_probe.py.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from audio_callback_probe import (
    AUDIO_CALLBACK,
    BR_TARGET_LONGWORD,
    CALLBACK_STOP,
    EXPECTED_MAIN_SHA256,
    RETURN_PC,
    prepared_machine,
)

RENDERER = 0x4010CBA8
DISPATCH_DIRTY_POINT = 0x4011C9B8
VOICE_EVENT_STATE = 0x8000FEF8
VOICE_CONTROL_STATE = 0x80006540
BR_FRAME_ADDRESS = 0x8000F7BE
BR_CACHE_ADDRESS = 0x47FFEF06
BR_READ_INSTRUCTION = 0x4010CC58
BR_CACHE_READ_INSTRUCTION = 0x4010D16E
BR_PACK_WRITE_INSTRUCTION = 0x4010D1E8
FIXED_DIVIDE_WRAPPER = 0x4011A0B6
CASE_ONE_PACKED_READ = 0x4010D08A


def load_emulator(path: Path):
    spec = importlib.util.spec_from_file_location("renderer_minicoldfire", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load emulator module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_vector(module, main_path: Path, injected: int) -> dict:
    bus, cpu, _ = prepared_machine(module, main_path)
    bus.write(BR_TARGET_LONGWORD, 4, injected)
    events: list[dict] = []
    helper_calls = 0
    original_read, original_write = bus.read, bus.write

    # Bus callbacks observe PC after the opcode and its extension words have
    # been fetched. Keep both the reported PC and decoded instruction address.
    def traced_read(address: int, size: int) -> int:
        value = original_read(address, size)
        if address in (BR_FRAME_ADDRESS, BR_CACHE_ADDRESS):
            instruction = {
                BR_FRAME_ADDRESS: BR_READ_INSTRUCTION,
                BR_CACHE_ADDRESS: BR_CACHE_READ_INSTRUCTION,
            }[address]
            events.append({
                "kind": "read",
                "instruction": f"0x{instruction:08X}",
                "bus_reported_pc": f"0x{cpu.pc:08X}",
                "address": f"0x{address:08X}",
                "size": size,
                "value": f"0x{value:0{size * 2}X}",
            })
        return value

    def traced_write(address: int, size: int, value: int) -> None:
        if address == VOICE_CONTROL_STATE + 4 and size == 4:
            events.append({
                "kind": "write",
                "instruction": f"0x{BR_PACK_WRITE_INSTRUCTION:08X}",
                "bus_reported_pc": f"0x{cpu.pc:08X}",
                "address": f"0x{address:08X}",
                "size": size,
                "value": f"0x{value & 0xFFFFFFFF:08X}",
            })
        original_write(address, size, value)

    bus.read, bus.write = traced_read, traced_write
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    start = cpu.steps
    for _ in range(200_000):
        # The callback's normal all-zero fixture has no dirty voices. Mark only
        # physical voice 0 so the stock machine table installs renderer 0.
        if cpu.pc == DISPATCH_DIRTY_POINT and cpu.d[2] == 0:
            bus.write((cpu.a[6] - 0x54) & 0xFFFFFFFF, 4, 1)
        if cpu.pc == RENDERER:
            # The renderer has a five-entry bounded jump table. Case 3 is the
            # one that consumes cached BR; current=end=3 is the minimal valid
            # state satisfying its range guard.
            for offset in (0, 4, 8):
                bus.write(VOICE_EVENT_STATE + offset, 4, 3)
            bus.write(BR_FRAME_ADDRESS, 2, (injected >> 16) & 0xFFFF)
        if cpu.pc == FIXED_DIVIDE_WRAPPER:
            helper_calls += 1
        if cpu.pc == CALLBACK_STOP:
            break
        cpu.step()
    else:
        raise ValueError("BR-active callback did not reach its RTOS handoff")
    bus.read, bus.write = original_read, original_write

    br_reads = [e for e in events if e["address"] == f"0x{BR_FRAME_ADDRESS:08X}"]
    cache_reads = [e for e in events if e["address"] == f"0x{BR_CACHE_ADDRESS:08X}"]
    packed_writes = [e for e in events if e["address"] == f"0x{VOICE_CONTROL_STATE + 4:08X}"]
    if len(br_reads) != 1 or len(cache_reads) != 1 or len(packed_writes) != 1:
        raise ValueError(f"unexpected BR renderer event set: {events}")
    if br_reads[0]["value"] != cache_reads[0]["value"]:
        raise ValueError("renderer BR cache did not preserve the frame word")
    if helper_calls != 1:
        raise ValueError(f"unexpected fixed-point helper call count: {helper_calls}")
    return {
        "injected_target": f"0x{injected:08X}",
        "callback_instructions": cpu.steps - start,
        "br_frame_word": br_reads[0]["value"],
        "packed_voice_control": packed_writes[0]["value"],
        "fixed_divide_wrapper_calls": helper_calls,
        "events": events,
    }


def run_case_one_vector(module, main_path: Path, packed_seed: int) -> dict:
    """Follow the packed word into the next bounded case in isolation."""
    bus, cpu, _ = prepared_machine(module, main_path)
    packed_reads: list[int] = []
    original_read = bus.read

    def traced_read(address: int, size: int) -> int:
        value = original_read(address, size)
        if cpu.pc == 0x4010D08E and address == VOICE_CONTROL_STATE + 4 and size == 4:
            packed_reads.append(value)
        return value

    bus.read = traced_read
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    for _ in range(200_000):
        if cpu.pc == DISPATCH_DIRTY_POINT and cpu.d[2] == 0:
            bus.write((cpu.a[6] - 0x54) & 0xFFFFFFFF, 4, 1)
        if cpu.pc == RENDERER:
            for offset in (0, 4, 8):
                bus.write(VOICE_EVENT_STATE + offset, 4, 1)
            bus.write(VOICE_CONTROL_STATE + 4, 4, packed_seed)
        if cpu.pc == CALLBACK_STOP:
            break
        cpu.step()
    else:
        raise ValueError("case-1 follow-up callback did not reach its RTOS handoff")
    bus.read = original_read
    if len(packed_reads) != 1:
        raise ValueError(f"unexpected case-1 packed reads: {packed_reads}")

    def region_hash(first: int, last: int) -> str:
        return hashlib.sha256(bytes(bus.read(a, 1) for a in range(first, last))).hexdigest()

    return {
        "seed": f"0x{packed_seed:08X}",
        "read_instruction": f"0x{CASE_ONE_PACKED_READ:08X}",
        "read_value_after_case_header": f"0x{packed_reads[0]:08X}",
        "final_value": f"0x{bus.read(VOICE_CONTROL_STATE + 4, 4):08X}",
        "region_sha256": {
            "source_plane_a": region_hash(0x80006BF8, 0x80007040),
            "source_plane_b": region_hash(0x80007040, 0x80007440),
            "source_plane_c": region_hash(0x800067FC, 0x80006BF8),
            "combined_output": region_hash(0x80000800, 0x80001000),
        },
    }


def probe(main_path: Path, emulator_path: Path) -> dict:
    digest = hashlib.sha256(main_path.read_bytes()).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    module = load_emulator(emulator_path)
    zero = run_vector(module, main_path, 0)
    high = run_vector(module, main_path, 0x7F000000)
    if zero["br_frame_word"] != "0x0000" or high["br_frame_word"] != "0x7F00":
        raise ValueError("unexpected synthetic BR-frame vectors")
    if zero["packed_voice_control"] == high["packed_voice_control"]:
        raise ValueError("packed voice control did not respond to BR")
    follow_zero = run_case_one_vector(module, main_path, int(zero["packed_voice_control"], 16))
    follow_high = run_case_one_vector(module, main_path, int(high["packed_voice_control"], 16))
    if follow_zero["final_value"] != "0xF0100000" or follow_high["final_value"] != "0xF0100000":
        raise ValueError("case 1 did not canonicalize its packed control word")
    if follow_zero["region_sha256"] != follow_high["region_sha256"]:
        raise ValueError("inactive case-1 fixture unexpectedly produced a BR-dependent render plane")
    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "machine_dispatch": {
            "renderer_table": "0x40277FE8",
            "machine_0_renderer": f"0x{RENDERER:08X}",
            "physical_voice": 0,
            "logical_track": 0,
        },
        "bounded_gate": {
            "state_address": f"0x{VOICE_EVENT_STATE:08X}",
            "jump_table_cases": {
                "0": "0x4010CFD2", "1": "0x4010D028", "2": "0x4010D102",
                "3": "0x4010D164", "4": "0x4010D204",
            },
            "selected_case": 3,
            "reason": "case 3 is the sole case that reads the renderer's cached BR word",
        },
        "br_control_path": {
            "frame_read_instruction": f"0x{BR_READ_INSTRUCTION:08X}",
            "frame_address": f"0x{BR_FRAME_ADDRESS:08X}",
            "cache_read_instruction": f"0x{BR_CACHE_READ_INSTRUCTION:08X}",
            "packed_state_address": f"0x{VOICE_CONTROL_STATE + 4:08X}",
            "fixed_divide_wrapper": f"0x{FIXED_DIVIDE_WRAPPER:08X}",
        },
        "vectors": [zero, high],
        "case_one_followup": {
            "vectors": [follow_zero, follow_high],
            "interpretation": (
                "Instruction 0x4010D08A reads the packed word, but case 1 masks both "
                "synthetic vectors to 0xF0100000 and produces identical inactive render planes."
            ),
        },
        "conclusion": (
            "Stock machine renderer 0 directly consumes the track-0 sample-BR frame and "
            "encodes it into a per-voice control word. This isolates the downstream BR "
            "control encoder; natural frame construction and hardware serialization are "
            "proved by the trigger-queue and hardware-sink probes."
        ),
        "next_target": (
            "Replace the synthetic event case with genuine trigger/sample metadata, then trace "
            "where case-3's BR-dependent coefficient reaches sample data or a render plane."
        ),
        "safety": "Emulation and synthetic RAM state only; firmware bytes were not modified.",
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
