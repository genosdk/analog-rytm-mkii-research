#!/usr/bin/env python3
"""Inventory DSPI1 callback writers across all eight physical voice lanes."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256, prepared_machine
from br_hardware_sink_probe import CONTROL_DMA_INITIALIZER
from control_frame_global_ownership_probe import PACKETIZER_READ_PCS
from machine_pitch_calibration_probe import (
    DISPATCH_INDEX_PC, MAIN_BASE, PUBLIC_MACHINE_NAMES, RENDERER_COUNT,
    RENDERER_TABLE,
)
from note_event_constructor_probe import (
    EVENT_INPUT, EVENT_TRACK, NOTE_EVENT_CONSTRUCTOR, NOTE_ON,
    QWERTY_SOURCE_MASK, write_event,
)
from note_pitch_consumer_boundary_probe import PITCH_SELECT_SOURCE, run_traced_callback
from renderer_control_ownership_probe import (
    FORCED_STATES, NOTE, PACKET_SOURCE_END, PACKET_SOURCE_FIRST,
    VOICE_EVENT_STATE, compact_ranges, packet_index, touched_fields,
)
from trigger_queue_probe import (
    CONTROL_SNAPSHOT_A, CONTROL_SNAPSHOT_POINTER, QUEUE, QUEUE_CAPACITY,
    QUEUE_INITIALIZER, QUEUE_INSTALLER, load_emulator, stock_call,
)

PHYSICAL_VOICE_TO_LOGICAL_TRACK = (0, 4, 1, 5, 8, 6, 10, 2)
VOICE_STATE_STRIDE = 0x20
EXPECTED_MATRIX_SHA256 = "960e108d7305c968231f2bf6059c93f20bdb11711bc1f802faf2961eb4c2377b"
KNOWN_CONTROL_WORDS = (46, 47, 195, 196)


def run_context(module, main_path: Path, physical_voice: int, logical_track: int,
                machine_id: int, renderer: int, forced_state: int) -> dict:
    bus, cpu, _ = prepared_machine(module, main_path)
    callback_sp = cpu.a[7]
    stock_call(cpu, CONTROL_DMA_INITIALIZER, [])
    stock_call(cpu, QUEUE_INITIALIZER, [QUEUE, 0, 0x419531F8, QUEUE_CAPACITY])
    stock_call(cpu, QUEUE_INSTALLER, [QUEUE])
    bus.write(CONTROL_SNAPSHOT_POINTER, 4, CONTROL_SNAPSHOT_A)
    bus.write(PITCH_SELECT_SOURCE + logical_track * 0x54, 1, 1)
    write_event(bus, event_type=NOTE_ON, note=NOTE, source_mask=QWERTY_SOURCE_MASK)
    bus.write(EVENT_INPUT + EVENT_TRACK, 4, logical_track)
    stock_call(cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])

    original_step = module.CPU.step
    original_read = bus.read
    original_write = bus.write
    renderer_active = False
    renderer_return = None
    renderer_fields: set[int] = set()
    nonrenderer_fields: set[int] = set()
    nonpacket_read_fields: set[int] = set()
    packetizer_read_fields: set[int] = set()
    renderer_arguments = []
    renderer_visits = 0

    def write(address: int, size: int, value: int) -> None:
        fields = touched_fields(address, size)
        if fields:
            (renderer_fields if renderer_active else nonrenderer_fields).update(fields)
        original_write(address, size, value)

    def read(address: int, size: int) -> int:
        value = original_read(address, size)
        fields = touched_fields(address, size)
        if fields:
            if cpu.pc in PACKETIZER_READ_PCS and size == 2:
                packetizer_read_fields.update(fields)
            else:
                nonpacket_read_fields.update(fields)
        return value

    def step(current_cpu):
        nonlocal renderer_active, renderer_return, renderer_visits
        if current_cpu.pc == DISPATCH_INDEX_PC and current_cpu.d[2] == physical_voice:
            current_cpu.d[1] = machine_id
        if current_cpu.pc == renderer:
            renderer_visits += 1
            renderer_arguments.append([
                original_read(current_cpu.a[7] + 4 + index * 4, 4)
                for index in range(4)
            ])
            state_base = VOICE_EVENT_STATE + physical_voice * VOICE_STATE_STRIDE
            for offset in (0, 4, 8):
                original_write(state_base + offset, 4, forced_state)
            renderer_return = current_cpu.bus.read(current_cpu.a[7], 4)
            renderer_active = True
        result = original_step(current_cpu)
        if renderer_active and current_cpu.pc == renderer_return:
            renderer_active = False
        return result

    bus.write = write
    bus.read = read
    module.CPU.step = step
    run_traced_callback(cpu, callback_sp)
    if renderer_visits != 1:
        raise ValueError(
            f"voice {physical_voice} track {logical_track} machine {machine_id} "
            f"state {forced_state} renderer visits: {renderer_visits}"
        )
    if len(renderer_arguments) != 1 or renderer_arguments[0][0] != physical_voice:
        raise ValueError(
            f"track {logical_track} did not dispatch physical voice {physical_voice}: "
            f"{renderer_arguments}"
        )
    expected_packet_fields = set(range(PACKET_SOURCE_FIRST, PACKET_SOURCE_END, 2))
    if packetizer_read_fields != expected_packet_fields:
        raise ValueError(
            f"voice {physical_voice} track {logical_track} packetizer coverage changed"
        )
    return {
        "machine_id": machine_id,
        "state": forced_state,
        "renderer_fields": sorted(renderer_fields),
        "nonrenderer_fields": sorted(nonrenderer_fields),
        "nonpacket_read_fields": sorted(nonpacket_read_fields),
    }


def run_lane(main_path_text: str, emulator_path_text: str, physical_voice: int,
             logical_track: int, renderers: list[int]) -> dict:
    main_path = Path(main_path_text)
    emulator_path = Path(emulator_path_text)
    contexts = [
        run_context(
            load_emulator(emulator_path), main_path, physical_voice, logical_track,
            machine_id, renderers[machine_id], forced_state,
        )
        for machine_id in range(len(PUBLIC_MACHINE_NAMES))
        for forced_state in FORCED_STATES
    ]
    renderer_owners: dict[int, set[tuple[int, int]]] = defaultdict(set)
    nonrenderer_owners: dict[int, set[tuple[int, int]]] = defaultdict(set)
    nonpacket_readers: dict[int, set[tuple[int, int]]] = defaultdict(set)
    for row in contexts:
        key = (row["machine_id"], row["state"])
        for field in row["renderer_fields"]:
            renderer_owners[field].add(key)
        for field in row["nonrenderer_fields"]:
            nonrenderer_owners[field].add(key)
        for field in row["nonpacket_read_fields"]:
            nonpacket_readers[field].add(key)
    return {
        "physical_voice": physical_voice,
        "logical_track": logical_track,
        "context_count": len(contexts),
        "context_sha256": hashlib.sha256(
            json.dumps(contexts, sort_keys=True).encode()
        ).hexdigest(),
        "renderer_fields": sorted(renderer_owners),
        "nonrenderer_fields": sorted(nonrenderer_owners),
        "nonpacket_read_fields": sorted(nonpacket_readers),
    }


def probe(main_path: Path, emulator_path: Path, jobs: int = 1) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    offset = RENDERER_TABLE - MAIN_BASE
    renderers = [
        struct.unpack(">I", image[offset + 4 * i:offset + 4 * i + 4])[0]
        for i in range(RENDERER_COUNT)
    ]
    tasks = [
        (str(main_path), str(emulator_path), physical_voice, logical_track, renderers)
        for physical_voice, logical_track in enumerate(PHYSICAL_VOICE_TO_LOGICAL_TRACK)
    ]
    if jobs == 1:
        lanes = [run_lane(*task) for task in tasks]
    else:
        with ProcessPoolExecutor(max_workers=min(jobs, len(tasks))) as executor:
            lanes = list(executor.map(run_lane_task, tasks))
    lanes.sort(key=lambda row: row["physical_voice"])

    renderer_lanes: dict[int, set[int]] = defaultdict(set)
    nonrenderer_lanes: dict[int, set[int]] = defaultdict(set)
    reader_lanes: dict[int, set[int]] = defaultdict(set)
    lane_digests = []
    public_lanes = []
    for lane in lanes:
        physical_voice = lane["physical_voice"]
        renderer_fields = set(lane["renderer_fields"])
        nonrenderer_fields = set(lane["nonrenderer_fields"])
        nonpacket_read_fields = set(lane["nonpacket_read_fields"])
        for field in renderer_fields:
            renderer_lanes[field].add(physical_voice)
        for field in nonrenderer_fields:
            nonrenderer_lanes[field].add(physical_voice)
        for field in nonpacket_read_fields:
            reader_lanes[field].add(physical_voice)
        lane_digests.append({
            "physical_voice": physical_voice,
            "logical_track": lane["logical_track"],
            "context_count": lane["context_count"],
            "context_sha256": lane["context_sha256"],
        })
        public_lanes.append({
            "physical_voice": physical_voice,
            "logical_track": lane["logical_track"],
            "contexts": lane["context_count"],
            "context_sha256": lane["context_sha256"],
            "renderer_field_count": len(renderer_fields),
            "nonrenderer_field_count": len(nonrenderer_fields),
            "all_writer_field_count": len(renderer_fields | nonrenderer_fields),
            "writer_free_field_count": 492 - len(renderer_fields | nonrenderer_fields),
            "nonpacket_read_field_count": len(nonpacket_read_fields),
        })

    all_fields = set(range(PACKET_SOURCE_FIRST, PACKET_SOURCE_END, 2))
    renderer_fields = set(renderer_lanes)
    nonrenderer_fields = set(nonrenderer_lanes)
    written = renderer_fields | nonrenderer_fields
    writer_free = sorted(all_fields - written)
    matrix_digest = hashlib.sha256(json.dumps(lane_digests, sort_keys=True).encode()).hexdigest()
    if EXPECTED_MATRIX_SHA256 != "TO_BE_LOCKED" and matrix_digest != EXPECTED_MATRIX_SHA256:
        raise ValueError(f"eight-lane matrix changed: {matrix_digest}")
    if (len(renderer_fields), len(nonrenderer_fields), len(written), len(writer_free)) != (
        164, 233, 368, 124,
    ):
        raise ValueError("unexpected eight-lane ownership totals")
    if {197, 198} & {packet_index(field) for field in writer_free}:
        raise ValueError("known logical-track-1 lane fields survived ownership rejection")

    writer_free_words = [packet_index(field) for field in writer_free]
    writer_free_word_set = set(writer_free_words)
    pairs = []
    for first in writer_free_words:
        second = first + 1
        if second not in writer_free_word_set:
            continue
        first_field = PACKET_SOURCE_FIRST + 2 * (first - 1)
        second_field = first_field + 2
        read_lanes = reader_lanes[first_field] | reader_lanes[second_field]
        pairs.append({
            "words": [first, second],
            "addresses": [f"0x{first_field:08X}", f"0x{second_field:08X}"],
            "nonpacket_read_physical_voices": sorted(read_lanes),
            "nonpacket_read_logical_tracks": sorted(
                PHYSICAL_VOICE_TO_LOGICAL_TRACK[voice] for voice in read_lanes
            ),
            "nearest_known_control_distance_words": min(
                abs(first - control) for control in KNOWN_CONTROL_WORDS
            ),
        })
    pairs.sort(key=lambda row: (
        len(row["nonpacket_read_physical_voices"]),
        row["nearest_known_control_distance_words"],
        row["words"][0],
    ))
    isolated_pairs = [row for row in pairs if not row["nonpacket_read_physical_voices"]]

    def field_rows(fields: set[int], lane_map: dict[int, set[int]]) -> list[dict]:
        return [
            {
                "address": f"0x{field:08X}",
                "dspi1_word_index": packet_index(field),
                "physical_voices": sorted(lane_map[field]),
                "logical_tracks": sorted(
                    PHYSICAL_VOICE_TO_LOGICAL_TRACK[voice] for voice in lane_map[field]
                ),
            }
            for field in sorted(fields)
        ]

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "coverage": {
            "public_machines": len(PUBLIC_MACHINE_NAMES),
            "states": list(FORCED_STATES),
            "physical_voice_to_logical_track": list(PHYSICAL_VOICE_TO_LOGICAL_TRACK),
            "contexts_per_lane": len(PUBLIC_MACHINE_NAMES) * len(FORCED_STATES),
            "contexts": sum(lane["context_count"] for lane in lanes),
            "matrix_sha256": matrix_digest,
        },
        "lanes": public_lanes,
        "ownership": {
            "renderer_field_count": len(renderer_fields),
            "nonrenderer_field_count": len(nonrenderer_fields),
            "any_callback_writer_field_count": len(written),
            "writer_free_field_count": len(writer_free),
            "writer_free_ranges": compact_ranges([packet_index(field) for field in writer_free]),
            "renderer_fields": field_rows(renderer_fields, renderer_lanes),
            "nonrenderer_fields": field_rows(nonrenderer_fields, nonrenderer_lanes),
            "warning": (
                "Eight-lane writer-free means unobserved in 1,360 synthetic note contexts; "
                "it does not establish FPGA semantics or live-hardware safety."
            ),
        },
        "read_isolation": {
            "definition": "callback reads excluding the four packetizer loop PCs",
            "writer_free_fields_with_nonpacket_reads": len(set(writer_free) & set(reader_lanes)),
            "writer_free_fields_without_nonpacket_reads": len(set(writer_free) - set(reader_lanes)),
        },
        "ranking": {
            "adjacent_writer_free_pairs": len(pairs),
            "fully_read_isolated_pairs": len(isolated_pairs),
            "criteria": [
                "fewest physical voices with non-packet reads",
                "nearest known control field",
                "lowest word index",
            ],
            "winner": isolated_pairs[0] if isolated_pairs else pairs[0],
            "pairs": pairs,
        },
        "conclusion": (
            "Ownership now covers every stock physical-voice/logical-track mapping across "
            "all public machines and forced states. Only the resulting eight-lane writer-free "
            "fields may advance to renewed candidate ranking."
        ),
        "next_target": (
            "Rank adjacent pairs from the eight-lane writer-free set, then apply bounded "
            "software-only persistence and packet-locality canaries."
        ),
        "safety": "Stock emulation and synthetic state selection only; no firmware was modified.",
    }


def run_lane_task(task):
    return run_lane(*task)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("main_image", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = probe(args.main_image, args.emulator, args.jobs)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
