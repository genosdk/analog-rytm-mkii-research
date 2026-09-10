#!/usr/bin/env python3
"""Recover the stock trigger-to-interpolation-state publication contract.

This emulation-only probe compares a voice-reset trigger with the same trigger
plus flag bit 5.  It records the exact MAIN instructions that publish the two
interpolation words and follows the stock phase/countdown progression for twelve
complete audio interrupts.  It does not patch firmware or infer PCM where no
CPU-side sample-plane write is observed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256, prepared_machine
from trigger_queue_probe import (
    CONTROL_SNAPSHOT_A,
    CONTROL_SNAPSHOT_POINTER,
    QUEUE,
    QUEUE_CAPACITY,
    QUEUE_INITIALIZER,
    QUEUE_INSTALLER,
    QUEUE_RING,
    TRIGGER_FLAGS,
    TRIGGER_RECORD,
    load_emulator,
    run_complete_callback,
    stock_call,
)

TRIGGER_PITCH = TRIGGER_RECORD + 0x08
TRIGGER_INTERPOLATION_A = TRIGGER_RECORD + 0x0C
TRIGGER_INTERPOLATION_B = TRIGGER_RECORD + 0x10
TRIGGER_DURATION = TRIGGER_RECORD + 0x20

LIVE_INTERPOLATION_A = 0x8000E4A0
LIVE_INTERPOLATION_B = 0x8000E46C
INTERPOLATION_PHASE = 0x80005E78
INTERPOLATION_LIMIT = 0x80005E10
INTERPOLATION_RATE_TERM = 0x80005E44
INTERPOLATION_COEFFICIENT = 0x8000E438
LIVE_DURATION = 0x8000FC24

INTERPOLATION_PUBLISH_FLAG = 0x20
VOICE_RESET_FLAG = 0x80
SOURCE_PLANE_FIRST = 0x800067F8
SOURCE_PLANE_LAST = 0x80007440


def run_vector(module, main_path: Path, flags: int) -> dict:
    bus, cpu, _ = prepared_machine(module, main_path)
    base_sp = cpu.a[7]
    stock_call(cpu, QUEUE_INITIALIZER, [QUEUE, 0, QUEUE_RING, QUEUE_CAPACITY])
    stock_call(cpu, QUEUE_INSTALLER, [QUEUE])
    bus.write(CONTROL_SNAPSHOT_POINTER, 4, CONTROL_SNAPSHOT_A)

    # Deliberately distinguish every field. These are numeric interpolation controls,
    # not host pointers: the stock path copies/transforms them without reading
    # memory at these values.
    bus.write(TRIGGER_PITCH, 4, 0x003C0000)
    bus.write(TRIGGER_INTERPOLATION_A, 4, 0x00000100)
    bus.write(TRIGGER_INTERPOLATION_B, 4, 0x00010000)
    bus.write(TRIGGER_DURATION, 4, 0x00100000)
    bus.write(TRIGGER_RECORD, 4, 1)
    bus.write(TRIGGER_FLAGS, 4, flags)

    watched_reads: list[dict] = []
    nonzero_plane_writes: list[dict] = []
    callback_index = 0
    original_read, original_write = bus.read, bus.write

    def traced_read(address: int, size: int) -> int:
        value = original_read(address, size)
        if address in (LIVE_INTERPOLATION_A, LIVE_INTERPOLATION_B):
            watched_reads.append({
                "callback": callback_index,
                "instruction": f"0x{cpu.pc - 4:08X}",
                "address": f"0x{address:08X}",
                "value": f"0x{value:08X}",
            })
        return value

    def traced_write(address: int, size: int, value: int) -> None:
        if SOURCE_PLANE_FIRST <= address < SOURCE_PLANE_LAST and value:
            nonzero_plane_writes.append({
                "callback": callback_index,
                "bus_reported_pc": f"0x{cpu.pc:08X}",
                "address": f"0x{address:08X}",
                "size": size,
                "value": f"0x{value & ((1 << (size * 8)) - 1):0{size * 2}X}",
            })
        original_write(address, size, value)

    bus.read, bus.write = traced_read, traced_write
    callbacks: list[dict] = []
    for index in range(12):
        callback_index = index + 1
        run_complete_callback(cpu, base_sp)
        callbacks.append({
            "callback": callback_index,
            "interpolation_phase": f"0x{original_read(INTERPOLATION_PHASE, 4):08X}",
            "interpolation_limit": f"0x{original_read(INTERPOLATION_LIMIT, 4):08X}",
            "interpolation_rate_term": f"0x{original_read(INTERPOLATION_RATE_TERM, 4):08X}",
            "interpolation_coefficient": f"0x{original_read(INTERPOLATION_COEFFICIENT, 4):08X}",
            "duration_countdown": f"0x{original_read(LIVE_DURATION, 4):08X}",
        })
    bus.read, bus.write = original_read, original_write

    return {
        "flags": f"0x{flags:02X}",
        "watched_reads": watched_reads,
        "callbacks": callbacks,
        "nonzero_source_plane_writes": nonzero_plane_writes,
    }


def probe(main_path: Path, emulator_path: Path) -> dict:
    digest = hashlib.sha256(main_path.read_bytes()).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    module = load_emulator(emulator_path)
    reset_only = run_vector(module, main_path, VOICE_RESET_FLAG)
    interpolation = run_vector(module, main_path, INTERPOLATION_PUBLISH_FLAG | VOICE_RESET_FLAG)

    if reset_only["watched_reads"]:
        raise ValueError("interpolation state was consumed without trigger flag bit 5")
    if [x["address"] for x in interpolation["watched_reads"]] != [
        f"0x{LIVE_INTERPOLATION_A:08X}", f"0x{LIVE_INTERPOLATION_B:08X}"
    ]:
        raise ValueError("flag bit 5 did not publish both live interpolation words")
    if [x["instruction"] for x in interpolation["watched_reads"]] != [
        "0x4011C616", "0x4011C61E"
    ]:
        raise ValueError("unexpected interpolation-state consumers")

    phase = [int(x["interpolation_phase"], 16) for x in interpolation["callbacks"]]
    duration = [int(x["duration_countdown"], 16) for x in interpolation["callbacks"]]
    if phase != [0x7080 * (i + 1) for i in range(12)]:
        raise ValueError(f"unexpected interpolation phase progression: {phase}")
    if duration != [0x100000 - 0x3840 * (i + 1) for i in range(12)]:
        raise ValueError(f"unexpected duration progression: {duration}")
    if any(x["interpolation_limit"] != "0x00010000" for x in interpolation["callbacks"]):
        raise ValueError("interpolation limit did not preserve trigger field +0x10")
    if any(x["interpolation_rate_term"] != "0x00232800" for x in interpolation["callbacks"]):
        raise ValueError("unexpected derived interpolation-rate term")
    control = [int(x["interpolation_coefficient"], 16) for x in interpolation["callbacks"]]
    if control != [
        0, 0, 0,
        0x00074822, 0x00074822,
        0x000E9044, 0x000E9044,
        0x0015D866, 0x0015D866, 0x0015D866,
        0x001D2088, 0x001D2088,
    ]:
        raise ValueError(f"unexpected stepped interpolation coefficient: {control}")
    if any(int(x["interpolation_coefficient"], 16) for x in reset_only["callbacks"]):
        raise ValueError("interpolation coefficient advanced without trigger flag bit 5")
    if interpolation["nonzero_source_plane_writes"]:
        raise ValueError("numeric interpolation state unexpectedly produced a CPU render plane")

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "trigger_interpolation_contract": {
            "record": f"0x{TRIGGER_RECORD:08X}",
            "record_bytes": 0x38,
            "interpolation_publish_flag": "0x20",
            "voice_reset_flag": "0x80",
            "tested_combined_flags": "0xA0",
            "field_map": {
                "+0x08": "0x80006388 (pitch/control word)",
                "+0x0C": f"0x{LIVE_INTERPOLATION_A:08X}",
                "+0x10": f"0x{LIVE_INTERPOLATION_B:08X}",
                "+0x20": f"0x{LIVE_DURATION:08X} (countdown)",
            },
            "interpolation_publish_instructions": ["0x4011C616", "0x4011C61E"],
        },
        "progression": {
            "phase_address": f"0x{INTERPOLATION_PHASE:08X}",
            "phase_step_per_callback": "0x00007080",
            "duration_step_per_callback": "-0x00003840",
            "interpolation_limit_address": f"0x{INTERPOLATION_LIMIT:08X}",
            "derived_rate_term_address": f"0x{INTERPOLATION_RATE_TERM:08X}",
            "stepped_coefficient_address": f"0x{INTERPOLATION_COEFFICIENT:08X}",
            "stepped_coefficients": ["0x00000000", "0x00074822", "0x000E9044"],
        },
        "vectors": [reset_only, interpolation],
        "conclusion": (
            "Trigger flag bit 5 gates control-frame interpolation state. With flags 0xA0, "
            "MAIN consumes fields +0x0C/+0x10 once, advances a stable numeric "
            "phase/countdown state, and emits a stepped per-track coefficient at 0x8000E438. No "
            "nonzero CPU source-plane write appears in the "
            "storage-free fixture. The 13-by-21-longword builder consumes this coefficient "
            "while constructing the hardware control frame; it is not a sample cursor, "
            "CPU PCM pointer, or Filter 2 insertion boundary."
        ),
        "next_target": (
            "Identify the consumer behind peripheral FIFO 0xFC03C034. For Filter 2, "
            "separately trace the CPU audio ingress after the hardware voice return."
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
