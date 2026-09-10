#!/usr/bin/env python3
"""Rank adjacent canary-safe DSPI1 fields by callback read isolation."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from collections import defaultdict
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256
from control_frame_canary_persistence_probe import WRITER_FREE_WORDS, source_address
from control_frame_global_ownership_probe import PACKETIZER_READ_PCS
from machine_pitch_calibration_probe import (
    DISPATCH_INDEX_PC, MAIN_BASE, PUBLIC_MACHINE_NAMES, RENDERER_COUNT,
    RENDERER_TABLE,
)
from note_pitch_consumer_boundary_probe import load_emulator, run_vector
from renderer_control_ownership_probe import (
    FORCED_STATES, NOTE, PACKET_SOURCE_END, PACKET_SOURCE_FIRST,
    VOICE_EVENT_STATE, touched_fields,
)
from trigger_queue_probe import FULL_CALLBACK_STOP

KNOWN_CONTROL_WORDS = (46, 47, 195, 196)
EXPECTED_MATRIX_SHA256 = "6136a6adb9a192abff2959aab54ccec94d02bb1a8229e0f7b187a1791c90d376"


def run_context(module, main_path: Path, machine_id: int, renderer: int,
                forced_state: int) -> dict:
    original_step = module.CPU.step
    original_read = module.Bus.read
    original_write = module.Bus.write
    cpus: dict[int, object] = {}
    active: set[int] = set()
    reads = []
    packet_counts = defaultdict(int)
    renderer_visits = 0

    def read(bus, address: int, size: int) -> int:
        value = original_read(bus, address, size)
        cpu = cpus.get(id(bus))
        if cpu is not None and id(bus) in active:
            fields = touched_fields(address, size)
            if fields:
                if cpu.pc in PACKETIZER_READ_PCS and size == 2:
                    for field in fields:
                        packet_counts[field] += 1
                else:
                    reads.append((cpu.pc, address, size, tuple(fields)))
        return value

    def step(cpu):
        nonlocal renderer_visits
        cpus[id(cpu.bus)] = cpu
        if cpu.pc == AUDIO_CALLBACK:
            active.add(id(cpu.bus))
        if cpu.pc == DISPATCH_INDEX_PC and cpu.d[2] == 0:
            cpu.d[1] = machine_id
        if cpu.pc == renderer:
            renderer_visits += 1
            for offset in (0, 4, 8):
                original_write(cpu.bus, VOICE_EVENT_STATE + offset, 4, forced_state)
        result = original_step(cpu)
        if cpu.pc == FULL_CALLBACK_STOP:
            active.discard(id(cpu.bus))
        return result

    module.Bus.read = read
    module.CPU.step = step
    run_vector(module, main_path, NOTE, 1, callback_count=1)
    if renderer_visits != 1:
        raise ValueError(f"machine {machine_id} state {forced_state} renderer visits: {renderer_visits}")
    expected = set(range(PACKET_SOURCE_FIRST, PACKET_SOURCE_END, 2))
    if set(packet_counts) != expected or set(packet_counts.values()) != {1}:
        raise ValueError(f"machine {machine_id} state {forced_state} packet read coverage changed")

    field_events = defaultdict(list)
    for pc, address, size, fields in reads:
        for field in fields:
            field_events[field].append((pc, address, size))
    return {
        "machine_id": machine_id,
        "machine_name": PUBLIC_MACHINE_NAMES[machine_id],
        "renderer": f"0x{renderer:08X}",
        "state": forced_state,
        "nonpacket_read_events": len(reads),
        "read_fields": [
            {"word": 1 + (field - PACKET_SOURCE_FIRST) // 2,
             "pcs": [f"0x{pc:08X}" for pc in sorted({event[0] for event in events})],
             "event_count": len(events)}
            for field, events in sorted(field_events.items())
        ],
    }


def probe(main_path: Path, emulator_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    offset = RENDERER_TABLE - MAIN_BASE
    renderers = [
        struct.unpack(">I", image[offset + 4 * i:offset + 4 * i + 4])[0]
        for i in range(RENDERER_COUNT)
    ]
    contexts = [
        run_context(load_emulator(emulator_path), main_path, machine_id,
                    renderers[machine_id], forced_state)
        for machine_id in range(len(PUBLIC_MACHINE_NAMES))
        for forced_state in FORCED_STATES
    ]
    matrix_digest = hashlib.sha256(json.dumps(contexts, sort_keys=True).encode()).hexdigest()
    if EXPECTED_MATRIX_SHA256 != "TO_BE_LOCKED" and matrix_digest != EXPECTED_MATRIX_SHA256:
        raise ValueError(f"candidate read matrix changed: {matrix_digest}")

    reader_contexts = defaultdict(set)
    reader_pcs = defaultdict(set)
    reader_events = defaultdict(int)
    for row in contexts:
        context = (row["machine_id"], row["state"])
        for field in row["read_fields"]:
            word = field["word"]
            reader_contexts[word].add(context)
            reader_pcs[word].update(field["pcs"])
            reader_events[word] += field["event_count"]

    free = set(WRITER_FREE_WORDS)
    pairs = []
    for first in WRITER_FREE_WORDS:
        second = first + 1
        if second not in free:
            continue
        read_context_count = len(reader_contexts.get(first, set()) | reader_contexts.get(second, set()))
        read_event_count = reader_events.get(first, 0) + reader_events.get(second, 0)
        nearest_control_distance = min(
            abs(first - control) for control in KNOWN_CONTROL_WORDS
        )
        pairs.append({
            "words": [first, second],
            "addresses": [f"0x{source_address(first):08X}", f"0x{source_address(second):08X}"],
            "nonpacket_read_context_count": read_context_count,
            "nonpacket_read_event_count": read_event_count,
            "reader_pcs": sorted(reader_pcs.get(first, set()) | reader_pcs.get(second, set())),
            "nearest_known_control_distance_words": nearest_control_distance,
        })
    pairs.sort(key=lambda row: (
        row["nonpacket_read_context_count"], row["nonpacket_read_event_count"],
        row["nearest_known_control_distance_words"], row["words"][0],
    ))
    isolated = [row for row in pairs if row["nonpacket_read_context_count"] == 0]
    winner = isolated[0] if isolated else pairs[0]

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "coverage": {"public_machines": len(PUBLIC_MACHINE_NAMES),
                     "logical_tracks": [0], "states": list(FORCED_STATES), "contexts": len(contexts),
                     "writer_free_fields": len(WRITER_FREE_WORDS),
                     "matrix_sha256": matrix_digest},
        "read_isolation": {
            "definition": "callback reads excluding the four packetizer loop PCs",
            "fields_with_nonpacket_reads": len(reader_contexts),
            "writer_free_fields_with_nonpacket_reads": len(free & set(reader_contexts)),
            "writer_free_fields_without_nonpacket_reads": len(free - set(reader_contexts)),
        },
        "ranking": {
            "adjacent_writer_free_pairs": len(pairs),
            "fully_read_isolated_pairs": len(isolated),
            "criteria": ["zero non-packet read contexts", "fewest non-packet read events",
                         "nearest known control field", "lowest word index"],
            "winner": winner,
            "pairs": pairs,
        },
        "contexts": contexts,
        "conclusion": (
            "For logical track 0, the ranked winner is an adjacent writer-free pair with the least observed "
            "stock callback consumption outside DSPI1 serialization. This remains a "
            "software-structure result, not an FPGA semantic assignment."
        ),
        "next_target": (
            "Seed only the winning pair at callback entry and prove exact two-word packet "
            "locality against a stock baseline across all public machine/state contexts."
        ),
        "safety": "Stock emulation only; no firmware or hardware state was modified.",
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
