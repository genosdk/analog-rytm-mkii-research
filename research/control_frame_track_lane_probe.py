#!/usr/bin/env python3
"""Resolve selected DSPI1 words against logical-track renderer lanes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256, prepared_machine
from br_hardware_sink_probe import (
    CONTROL_DMA_INITIALIZER, PING_PONG_BASE, PING_PONG_SELECTOR,
    PING_PONG_STRIDE,
)
from control_frame_canary_persistence_probe import source_address
from note_event_constructor_probe import (
    EVENT_INPUT, EVENT_TRACK, NOTE_EVENT_CONSTRUCTOR, NOTE_ON,
    QWERTY_SOURCE_MASK, write_event,
)
from note_pitch_consumer_boundary_probe import (
    PACKET_SOURCE_OFFSET, PACKET_WORDS, PITCH_SELECT_SOURCE,
    load_emulator, run_traced_callback,
)
from trigger_queue_probe import (
    CONTROL_SNAPSHOT_A, CONTROL_SNAPSHOT_POINTER, QUEUE, QUEUE_CAPACITY,
    QUEUE_INITIALIZER, QUEUE_INSTALLER, stock_call,
)

CANDIDATE_WORDS = (197, 198)
CANDIDATE_VALUES = (0xF27A, 0x0D85)
TRACKS = (0, 1, 2)
EXPECTED_MATRIX_SHA256 = "3fa47e5551718ace38890f7682df8c6744f7c2c64cb2f3e9f101a75f2b0b418d"


def run_track(module, main_path: Path, track: int) -> dict:
    bus, cpu, _ = prepared_machine(module, main_path)
    callback_sp = cpu.a[7]
    stock_call(cpu, CONTROL_DMA_INITIALIZER, [])
    stock_call(cpu, QUEUE_INITIALIZER, [QUEUE, 0, 0x419531F8, QUEUE_CAPACITY])
    stock_call(cpu, QUEUE_INSTALLER, [QUEUE])
    bus.write(CONTROL_SNAPSHOT_POINTER, 4, CONTROL_SNAPSHOT_A)
    bus.write(PITCH_SELECT_SOURCE + track * 0x54, 1, 1)
    write_event(bus, event_type=NOTE_ON, note=60, source_mask=QWERTY_SOURCE_MASK)
    bus.write(EVENT_INPUT + EVENT_TRACK, 4, track)
    stock_call(cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])
    for word, value in zip(CANDIDATE_WORDS, CANDIDATE_VALUES):
        bus.write(source_address(word), 2, value)

    events = []
    original_write = bus.write

    def traced_write(address: int, size: int, value: int) -> None:
        if 0x80006520 <= address < 0x80006580:
            events.append({"pc": f"0x{cpu.pc:08X}", "address": f"0x{address:08X}",
                           "size": size, "value": f"0x{value & ((1 << (8 * size)) - 1):0{size * 2}X}"})
        original_write(address, size, value)

    bus.write = traced_write
    callback = run_traced_callback(cpu, callback_sp)
    selector = bus.read(PING_PONG_SELECTOR, 4)
    packet_base = PING_PONG_BASE + selector * PING_PONG_STRIDE
    packet_words = [
        bus.read(packet_base + PACKET_SOURCE_OFFSET + 4 * index, 4)
        for index in range(PACKET_WORDS)
    ]
    candidate_events = [event for event in events
                        if int(event["address"], 16) in {source_address(word) for word in CANDIDATE_WORDS}]
    return {
        "track": track,
        "renderer_arguments": callback["renderer_arguments"],
        "candidate_write_events": candidate_events,
        "candidate_packet_values": [f"0x{packet_words[word] & 0xFFFF:04X}" for word in CANDIDATE_WORDS],
        "all_control_writes": events,
    }


def probe(main_path: Path, emulator_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")

    # 0x4011CD20 loop: A0=0x800063C0, D0=0, D1=(D0+21)<<3,
    # clear byte 4(A0,D1), then increment D0 until 56 iterations.
    expected = bytes.fromhex(
        "428041f9800063c02200068100000015e78942025280763811821804b68066e8"
    )
    offset = 0x4011CD20 - 0x40000400
    if image[offset:offset + len(expected)] != expected:
        raise ValueError("8-byte record clear loop changed")

    rows = [run_track(load_emulator(emulator_path), main_path, track) for track in TRACKS]
    track1 = rows[1]
    if not any(event["address"] == "0x80006548" and event["size"] == 2
               for event in track1["candidate_write_events"]):
        raise ValueError("logical track 1 did not own candidate word 197")
    if track1["candidate_packet_values"][0] == f"0x{CANDIDATE_VALUES[0]:04X}":
        raise ValueError("logical track 1 unexpectedly preserved candidate word 197")

    matrix_digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    if EXPECTED_MATRIX_SHA256 != "TO_BE_LOCKED" and matrix_digest != EXPECTED_MATRIX_SHA256:
        raise ValueError(f"track-lane matrix changed: {matrix_digest}")
    clear_addresses = [0x800063C0 + 4 + 8 * (21 + index) for index in range(56)]
    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "record_clear_loop": {
            "pc": "0x4011CD20", "write_pc": "0x4011CD3C",
            "record_stride_bytes": 8, "records": 56,
            "first_cleared_byte": f"0x{clear_addresses[0]:08X}",
            "last_cleared_byte": f"0x{clear_addresses[-1]:08X}",
            "cleared_offset_in_record": 4,
        },
        "candidate": {
            "words": list(CANDIDATE_WORDS),
            "addresses": [f"0x{source_address(word):08X}" for word in CANDIDATE_WORDS],
            "record_base": "0x80006548", "record_clear_byte": "0x8000654C",
            "classification": "logical-track renderer lane, not globally spare padding",
            "rejected_for_universal_filter2_transport": True,
        },
        "coverage": {"logical_tracks": list(TRACKS), "matrix_sha256": matrix_digest},
        "tracks": rows,
        "conclusion": (
            "Words 197/198 are the first four bytes of an 8-byte renderer-control record. "
            "A logical-track-1 note writes word 197 at 0x80006548 and destroys its seeded "
            "canary, so the track-0-only candidate is not globally spare."
        ),
        "next_target": (
            "Extend ownership across all eight physical-voice/logical-track mappings before "
            "selecting any replacement Filter 2 transport slot."
        ),
        "safety": "Stock emulation and static byte checks only; firmware was not modified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("main_image", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    encoded = json.dumps(probe(args.main_image, args.emulator), indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
