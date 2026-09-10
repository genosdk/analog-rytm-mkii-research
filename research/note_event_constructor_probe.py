#!/usr/bin/env python3
"""Recover and execute the stock note-on/note-off constructor contract."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256, prepared_machine
from note_pitch_publication_probe import LIVE_PITCH
from trigger_queue_probe import (
    CONTROL_SNAPSHOT_A,
    CONTROL_SNAPSHOT_POINTER,
    EVENT_VALUE,
    QUEUE,
    QUEUE_CAPACITY,
    QUEUE_INITIALIZER,
    QUEUE_INSTALLER,
    QUEUE_RING,
    TRIGGER_RECORD,
    VOICE_EVENT_STATE,
    load_emulator,
    run_complete_callback,
    stock_call,
)

NOTE_EVENT_CONSTRUCTOR = 0x401188C6
EVENT_INPUT = 0x41960000
HELD_SOURCE_MASK_BASE = 0x42AAF558
HELD_SOURCE_MASK_STRIDE = 0x1A0C
EVENT_TRACK = 0x00
EVENT_NOTE = 0x04
EVENT_TYPE = 0x0C
EVENT_FLAGS = 0x10
EVENT_SOURCE_MASK = 0x20
NOTE_ON = 1
NOTE_OFF = 2
VOICE_RESET_FLAG = 0x80
QWERTY_SOURCE_MASK = 0x00000001


def write_event(bus, *, event_type: int, note: int, source_mask: int) -> None:
    for offset in range(0, 0x28, 4):
        bus.write(EVENT_INPUT + offset, 4, 0)
    bus.write(EVENT_INPUT + EVENT_TRACK, 4, 0)
    bus.write(EVENT_INPUT + EVENT_NOTE, 4, note)
    bus.write(EVENT_INPUT + EVENT_TYPE, 4, event_type)
    bus.write(
        EVENT_INPUT + EVENT_FLAGS,
        4,
        VOICE_RESET_FLAG if event_type == NOTE_ON else 0,
    )
    bus.write(EVENT_INPUT + EVENT_SOURCE_MASK, 4, source_mask)


def probe(main_path: Path, emulator_path: Path) -> dict:
    digest = hashlib.sha256(main_path.read_bytes()).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    module = load_emulator(emulator_path)
    bus, cpu, _ = prepared_machine(module, main_path)
    callback_sp = cpu.a[7]
    stock_call(cpu, QUEUE_INITIALIZER, [QUEUE, 0, QUEUE_RING, QUEUE_CAPACITY])
    stock_call(cpu, QUEUE_INSTALLER, [QUEUE])
    bus.write(CONTROL_SNAPSHOT_POINTER, 4, CONTROL_SNAPSHOT_A)

    note = 60
    pitch_word = note << 16
    held_address = HELD_SOURCE_MASK_BASE
    write_event(bus, event_type=NOTE_ON, note=note, source_mask=QWERTY_SOURCE_MASK)
    note_on_steps = stock_call(cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])
    note_on_constructed = {
        "record_state": bus.read(TRIGGER_RECORD, 4),
        "pitch": f"0x{bus.read(TRIGGER_RECORD + 8, 4):08X}",
        "flags": f"0x{bus.read(TRIGGER_RECORD + 0x14, 4):08X}",
        "held_source_mask": f"0x{bus.read(held_address, 4):08X}",
    }
    note_on_callback = run_complete_callback(cpu, callback_sp)
    command_pointer = bus.read(QUEUE_RING, 4)
    note_on_consumed = {
        "callback_cases": note_on_callback["cases"],
        "live_pitch": f"0x{bus.read(LIVE_PITCH, 4):08X}",
        "record_cleared": bus.read(TRIGGER_RECORD, 4) == 0,
        "event_cleared": bus.read(EVENT_VALUE, 4) == 0,
        "voice_state": bus.read(VOICE_EVENT_STATE, 4),
        "queued_command": {
            "code": bus.read(command_pointer, 1),
            "track_mask": f"0x{bus.read(command_pointer + 4, 4):08X}",
            "queue_count": bus.read(QUEUE + 4, 4),
        },
    }

    write_event(bus, event_type=NOTE_OFF, note=note, source_mask=QWERTY_SOURCE_MASK)
    note_off_steps = stock_call(cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])
    note_off_constructed = {
        "record_state": bus.read(TRIGGER_RECORD, 4),
        "held_source_mask": f"0x{bus.read(held_address, 4):08X}",
    }
    release_callbacks = []
    for index in range(12):
        callback = run_complete_callback(cpu, callback_sp)
        release_callbacks.append({
            "callback": index + 1,
            "cases": callback["cases"],
            "voice_state": bus.read(VOICE_EVENT_STATE, 4),
            "voice_timer": bus.read(VOICE_EVENT_STATE + 4, 4),
            "record_cleared": bus.read(TRIGGER_RECORD, 4) == 0,
        })

    # A repeated release is rejected by the constructor after the ownership bit
    # has been cleared. This is the condition missed by direct state-2 injection.
    write_event(bus, event_type=NOTE_OFF, note=note, source_mask=QWERTY_SOURCE_MASK)
    rejected_steps = stock_call(cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])
    repeated_release = {
        "instructions": rejected_steps,
        "record_state": bus.read(TRIGGER_RECORD, 4),
        "held_source_mask": f"0x{bus.read(held_address, 4):08X}",
    }

    expected_release_cases = [[1]] + [[2]] * 8 + [[3], [4], [4]]
    if note_on_steps != 108 or note_off_steps != 38 or rejected_steps != 24:
        raise ValueError("constructor instruction counts diverged")
    if note_on_constructed != {
        "record_state": 1,
        "pitch": f"0x{pitch_word:08X}",
        "flags": "0x00000080",
        "held_source_mask": "0x00000001",
    }:
        raise ValueError(f"unexpected note-on construction: {note_on_constructed}")
    if note_on_consumed["callback_cases"] != [0]:
        raise ValueError("note-on did not enter renderer case zero")
    if note_on_consumed["live_pitch"] != f"0x{pitch_word:08X}":
        raise ValueError("note-on did not publish pitch")
    if note_on_consumed["queued_command"] != {
        "code": 0x1F,
        "track_mask": "0x00000001",
        "queue_count": 1,
    }:
        raise ValueError("note-on command diverged")
    if note_off_constructed != {"record_state": 2, "held_source_mask": "0x00000000"}:
        raise ValueError(f"unexpected note-off construction: {note_off_constructed}")
    if [item["cases"] for item in release_callbacks] != expected_release_cases:
        raise ValueError("note-off release progression diverged")
    if repeated_release["record_state"] != 0:
        raise ValueError("unowned repeated release was accepted")

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "constructor_contract": {
            "routine": f"0x{NOTE_EVENT_CONSTRUCTOR:08X}",
            "input_record": f"0x{EVENT_INPUT:08X}",
            "track_offset": f"+0x{EVENT_TRACK:02X}",
            "note_offset": f"+0x{EVENT_NOTE:02X}",
            "event_type_offset": f"+0x{EVENT_TYPE:02X}",
            "flags_offset": f"+0x{EVENT_FLAGS:02X}",
            "source_mask_offset": f"+0x{EVENT_SOURCE_MASK:02X}",
            "note_on_type": NOTE_ON,
            "note_off_type": NOTE_OFF,
            "held_source_mask_base": f"0x{HELD_SOURCE_MASK_BASE:08X}",
            "held_source_mask_track_stride": f"0x{HELD_SOURCE_MASK_STRIDE:04X}",
            "trigger_record_track_stride": "0x0038",
        },
        "note_on": {
            "note": note,
            "instructions": note_on_steps,
            "constructed": note_on_constructed,
            "consumed": note_on_consumed,
        },
        "note_off": {
            "instructions": note_off_steps,
            "constructed": note_off_constructed,
            "release_callbacks": release_callbacks,
            "expected_cases": expected_release_cases,
        },
        "repeated_release": repeated_release,
        "conclusion": (
            "Stock routine 0x401188C6 constructs both note-on and note-off records. "
            "Type 1 publishes note<<16, claims a per-track source bit, enters renderer "
            "case 0 and queues command 0x1F. Type 2 requires that ownership bit, clears "
            "it, emits trigger state 2 and advances the native release through cases "
            "1, 2, 3 and into the observed case-4 release tail."
        ),
        "next_target": (
            "Initialize the note-dependent machine/DSPI consumer to prove "
            "pitch-dependent output."
        ),
        "safety": "Emulation and synthetic RAM input only; firmware bytes were not modified.",
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
