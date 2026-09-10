#!/usr/bin/env python3
"""Prove the stock track-0 trigger and note-pitch publication contract."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256, prepared_machine
from sample_state_probe import TRIGGER_PITCH
from trigger_queue_probe import (
    CONTROL_SNAPSHOT_A,
    CONTROL_SNAPSHOT_POINTER,
    EVENT_VALUE,
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

LIVE_PITCH = 0x80006388
VOICE_RESET_FLAG = 0x80
TRIGGER_PITCH_READ_PC = 0x4011BDA8
LIVE_PITCH_WRITE_PC = 0x4011BDB6
TRIGGER_PITCH_SECOND_READ_PC = 0x4011BDCC


def run_vector(module, main_path: Path, note: int) -> dict:
    if not 0 <= note <= 127:
        raise ValueError("note must be 0..127")
    bus, cpu, _ = prepared_machine(module, main_path)
    base_sp = cpu.a[7]
    stock_call(cpu, QUEUE_INITIALIZER, [QUEUE, 0, QUEUE_RING, QUEUE_CAPACITY])
    stock_call(cpu, QUEUE_INSTALLER, [QUEUE])
    bus.write(CONTROL_SNAPSHOT_POINTER, 4, CONTROL_SNAPSHOT_A)
    pitch_word = note << 16
    bus.write(TRIGGER_PITCH, 4, pitch_word)
    bus.write(TRIGGER_RECORD, 4, 1)
    bus.write(TRIGGER_FLAGS, 4, VOICE_RESET_FLAG)

    events = []
    original_read, original_write = bus.read, bus.write

    def traced_read(address: int, size: int) -> int:
        value = original_read(address, size)
        if address == TRIGGER_PITCH and size == 4:
            events.append({
                "kind": "read",
                "pc": f"0x{cpu.pc:08X}",
                "address": f"0x{address:08X}",
                "value": f"0x{value:08X}",
            })
        return value

    def traced_write(address: int, size: int, value: int) -> None:
        if address == LIVE_PITCH and size == 4:
            events.append({
                "kind": "write",
                "pc": f"0x{cpu.pc:08X}",
                "address": f"0x{address:08X}",
                "value": f"0x{value & 0xFFFFFFFF:08X}",
            })
        original_write(address, size, value)

    bus.read, bus.write = traced_read, traced_write
    callback = run_complete_callback(cpu, base_sp)
    bus.read, bus.write = original_read, original_write
    command_pointer = bus.read(QUEUE_RING, 4)
    return {
        "note": note,
        "encoded_pitch": f"0x{pitch_word:08X}",
        "events": events,
        "live_pitch_after": f"0x{bus.read(LIVE_PITCH, 4):08X}",
        "callback_cases": callback["cases"],
        "one_shot_cleared": bus.read(EVENT_VALUE, 4) == 0,
        "queued_command": {
            "pointer": f"0x{command_pointer:08X}",
            "code": bus.read(command_pointer, 1),
            "track_mask": f"0x{bus.read(command_pointer + 4, 4):08X}",
            "queue_count": bus.read(QUEUE + 4, 4),
        },
    }


def probe(main_path: Path, emulator_path: Path) -> dict:
    digest = hashlib.sha256(main_path.read_bytes()).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    module = load_emulator(emulator_path)
    vectors = [run_vector(module, main_path, note) for note in (48, 60, 72)]
    expected_pcs = [
        f"0x{TRIGGER_PITCH_READ_PC:08X}",
        f"0x{LIVE_PITCH_WRITE_PC:08X}",
        f"0x{TRIGGER_PITCH_SECOND_READ_PC:08X}",
    ]
    for vector in vectors:
        expected_pitch = f"0x{vector['note'] << 16:08X}"
        if vector["encoded_pitch"] != expected_pitch or vector["live_pitch_after"] != expected_pitch:
            raise ValueError(f"pitch publication diverged: {vector}")
        if [event["pc"] for event in vector["events"]] != expected_pcs:
            raise ValueError(f"unexpected publisher trace: {vector['events']}")
        if any(event["value"] != expected_pitch for event in vector["events"]):
            raise ValueError(f"publisher did not preserve pitch word: {vector['events']}")
        if vector["callback_cases"] != [0] or not vector["one_shot_cleared"]:
            raise ValueError(f"stock trigger did not enter voice state zero: {vector}")
        if vector["queued_command"]["code"] != 0x1F:
            raise ValueError(f"unexpected trigger command: {vector['queued_command']}")
        if vector["queued_command"]["track_mask"] != "0x00000001":
            raise ValueError(f"unexpected trigger track mask: {vector['queued_command']}")

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "contract": {
            "track": 0,
            "trigger_record": f"0x{TRIGGER_RECORD:08X}",
            "pitch_field": f"0x{TRIGGER_PITCH:08X} (+0x08)",
            "encoding": "MIDI-style note number << 16",
            "live_pitch": f"0x{LIVE_PITCH:08X}",
            "publisher_trace_pcs": expected_pcs,
            "voice_reset_flag": f"0x{VOICE_RESET_FLAG:02X}",
            "queued_command_code": "0x1F",
            "queued_track_mask": "0x00000001",
        },
        "vectors": vectors,
        "conclusion": (
            "Stock MAIN accepts a 0..127 note encoded in trigger field +0x08 as note<<16, "
            "publishes it unchanged to track-0 live state, clears the one-shot trigger, "
            "enters renderer state 0 and enqueues command 0x1F for track mask 1. This is "
            "sufficient to bind QWERTY key-down to the authentic emulated stock trigger path."
        ),
        "scope_limit": (
            "The storage-free fixture does not yet prove pitch-dependent audio or DSPI payload "
            "output, and the stock note-off/release encoding remains unidentified."
        ),
        "next_target": (
            "Bind QWERTY key-down to this contract, keep key-up release-pending, then trace the "
            "note-dependent machine/DSPI consumer with initialized sound and board state."
        ),
        "safety": "Emulation and synthetic RAM state only; firmware bytes were not modified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
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
