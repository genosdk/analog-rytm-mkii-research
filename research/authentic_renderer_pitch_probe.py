#!/usr/bin/env python3
"""Correlate note pitch with renderer 10's dense DSPI1 control block.

The storage-free fixture lacks a loaded project, so this probe seeds only the
runtime machine selector and synth-live gate. Track routing, note construction,
physical-voice allocation, renderer dispatch, rendering, and packetization all
remain stock OS 1.72 behavior. No function pointer is substituted.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256, prepared_machine
from br_hardware_sink_probe import (
    CONTROL_DMA_INITIALIZER,
    PING_PONG_BASE,
    PING_PONG_SELECTOR,
    PING_PONG_STRIDE,
)
from note_event_constructor_probe import (
    EVENT_INPUT,
    EVENT_TRACK,
    NOTE_EVENT_CONSTRUCTOR,
    NOTE_ON,
    QWERTY_SOURCE_MASK,
    write_event,
)
from trigger_queue_probe import (
    CONTROL_SNAPSHOT_A,
    CONTROL_SNAPSHOT_POINTER,
    FULL_CALLBACK_STOP,
    QUEUE,
    QUEUE_CAPACITY,
    QUEUE_INITIALIZER,
    QUEUE_INSTALLER,
    QUEUE_RING,
    load_emulator,
    stock_call,
)

MAIN_LOAD = 0x40000400
RENDERER_TABLE = 0x40277FE8
RENDERER_COUNT = 53
TRACK_ROUTING_TABLE = 0x40278A44
MACHINE_SELECTOR_BASE = 0x8000EA00
SYNTH_LIVE_GATE_BASE = 0x8000EA18
TRACK = 6
MACHINE_ID = 10
PHYSICAL_VOICE = 5
RENDERER = 0x40110B18
ANCHOR_NOTES = (48, 60, 72)
PITCH_VALUE_WORDS = (310, 312, 314, 316, 318, 320)
PITCH_TAG_WORDS = (309, 311, 313, 315, 317, 319)


def renderer_addresses(main: bytes) -> list[int]:
    offset = RENDERER_TABLE - MAIN_LOAD
    return [
        int.from_bytes(main[offset + 4 * index:offset + 4 * index + 4], "big")
        for index in range(RENDERER_COUNT)
    ]


def run_vector(module, main_path: Path, renderers: list[int], note: int) -> dict:
    bus, cpu, _ = prepared_machine(module, main_path)
    callback_sp = cpu.a[7]
    stock_call(cpu, CONTROL_DMA_INITIALIZER, [])
    stock_call(cpu, QUEUE_INITIALIZER, [QUEUE, 0, QUEUE_RING, QUEUE_CAPACITY])
    stock_call(cpu, QUEUE_INSTALLER, [QUEUE])
    bus.write(CONTROL_SNAPSHOT_POINTER, 4, CONTROL_SNAPSHOT_A)

    bus.write(MACHINE_SELECTOR_BASE + TRACK, 1, MACHINE_ID)
    write_event(
        bus,
        event_type=NOTE_ON,
        note=note,
        source_mask=QWERTY_SOURCE_MASK,
    )
    bus.write(EVENT_INPUT + EVENT_TRACK, 4, TRACK)
    stock_call(cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])

    # Project ingestion normally copies sound chromatic mode into this runtime
    # byte. The storage-free fixture has no sound object, so seed only that
    # already-proven one-bit boundary after constructing the stock note event.
    bus.write(SYNTH_LIVE_GATE_BASE + TRACK, 1, 1)

    calls = []
    cpu.a[7] = callback_sp
    cpu.pc = 0x4011B3AE
    for _ in range(200_000):
        if cpu.pc in renderers:
            calls.append({
                "index": renderers.index(cpu.pc),
                "renderer": f"0x{cpu.pc:08X}",
                "arguments": [
                    f"0x{bus.read(cpu.a[7] + 4 + 4 * index, 4):08X}"
                    for index in range(4)
                ],
            })
        if cpu.pc == FULL_CALLBACK_STOP:
            break
        cpu.step()
    else:
        raise ValueError("audio callback did not reach its final RTE")

    selector = bus.read(PING_PONG_SELECTOR, 4)
    packet_base = PING_PONG_BASE + selector * PING_PONG_STRIDE
    words = [bus.read(packet_base + 0x0C + 4 * index, 4) for index in range(510)]
    return {
        "note": note,
        "calls": calls,
        "packet_sha256": hashlib.sha256(
            b"".join(word.to_bytes(4, "big") for word in words)
        ).hexdigest(),
        "pitch_block": {
            str(index): f"0x{words[index]:08X}"
            for index in PITCH_TAG_WORDS + PITCH_VALUE_WORDS
        },
        "words": words,
    }


def run_note_task(arguments: tuple[Path, Path, list[int], int]) -> dict:
    main_path, emulator_path, renderers, note = arguments
    module = load_emulator(emulator_path)
    return run_vector(module, main_path, renderers, note)


def probe(main_path: Path, emulator_path: Path, jobs: int = 1) -> dict:
    main = main_path.read_bytes()
    digest = hashlib.sha256(main).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    renderers = renderer_addresses(main)
    tasks = [(main_path, emulator_path, renderers, note) for note in range(128)]
    if jobs == 1:
        vectors = [run_note_task(task) for task in tasks]
    else:
        with ProcessPoolExecutor(max_workers=jobs) as executor:
            vectors = list(executor.map(run_note_task, tasks))

    routing = [main[TRACK_ROUTING_TABLE - MAIN_LOAD + index] for index in range(8)]
    if routing != [0, 4, 1, 5, 8, 6, 10, 2]:
        raise ValueError(f"unexpected stock track routing table: {routing}")
    if routing.index(TRACK) != PHYSICAL_VOICE:
        raise ValueError("track 6 did not map to physical voice 5")
    anchors = [vectors[note] for note in ANCHOR_NOTES]
    for vector in vectors:
        if [call["index"] for call in vector["calls"]] != [MACHINE_ID]:
            raise ValueError(f"renderer 10 was not naturally selected: {vector['calls']}")
        arguments = vector["calls"][0]["arguments"]
        if int(arguments[0], 16) != PHYSICAL_VOICE:
            raise ValueError(f"unexpected physical voice argument: {arguments}")
        if int(arguments[3], 16) != vector["note"] << 16:
            raise ValueError(f"renderer did not receive live note pitch: {arguments}")
    for vector in anchors:
        words = vector["words"]
        if any(words[index] != 0x80014040 for index in PITCH_TAG_WORDS):
            raise ValueError("interleaved pitch-block tags changed at anchor notes")

    comparisons = []
    for left, right in zip(anchors, anchors[1:]):
        differences = [
            index
            for index, (a, b) in enumerate(zip(left["words"], right["words"]))
            if a != b
        ]
        if differences != list(PITCH_VALUE_WORDS):
            raise ValueError(
                f"unexpected note {left['note']}->{right['note']} differences: "
                f"{differences}"
            )
        comparisons.append({
            "notes": [left["note"], right["note"]],
            "differing_word_indices": differences,
        })
    channel_laws = []
    for channel, word_index in enumerate(PITCH_VALUE_WORDS):
        values = [vector["words"][word_index] & 0xFFFF for vector in vectors]
        tag_index = PITCH_TAG_WORDS[channel]
        pages = [
            (vector["words"][tag_index] & 0xFF) - 0x40 for vector in vectors
        ]
        extended_values = [
            (page << 16) | value for page, value in zip(pages, values)
        ]
        monotonic = values == sorted(values)
        octave_law = all(
            values[note + 12] in (2 * values[note], 2 * values[note] + 1)
            for note in range(30, 116)
        )
        decreases = [
            note for note in range(127) if values[note + 1] < values[note]
        ]
        octave_mismatches = [
            note
            for note in range(30, 116)
            if values[note + 12] not in (2 * values[note], 2 * values[note] + 1)
        ]
        extended_monotonic = extended_values == sorted(extended_values)
        extended_octave_mismatches = [
            note
            for note in range(30, 116)
            if extended_values[note + 12]
            not in (2 * extended_values[note], 2 * extended_values[note] + 1)
        ]
        octave_residuals = [
            extended_values[note + 12] - 2 * extended_values[note]
            for note in range(30, 116)
        ]
        if not extended_monotonic or not set(octave_residuals).issubset({0, 1, 2}):
            raise ValueError(
                f"reconstructed channel {channel} pitch law diverged: "
                f"monotonic={extended_monotonic}, "
                f"octave_residuals={sorted(set(octave_residuals))}"
            )
        channel_laws.append({
            "channel": channel,
            "tag_word_index": tag_index,
            "word_index": word_index,
            "raw_low16_monotonic_notes_0_127": monotonic,
            "raw_low16_octave_law_notes_30_127": octave_law,
            "raw_low16_decrease_after_notes": decreases,
            "raw_low16_octave_law_mismatch_start_notes": octave_mismatches,
            "reconstruction": "((tag_low8 - 0x40) << 16) | value_low16",
            "reconstructed_monotonic_notes_0_127": extended_monotonic,
            "reconstructed_octave_residuals_notes_30_127": sorted(
                set(octave_residuals)
            ),
            "reconstructed_exact_0_or_1_octave_mismatch_start_notes": (
                extended_octave_mismatches
            ),
            "tag_pages": pages,
            "raw_low16_values": [f"0x{value:04X}" for value in values],
            "reconstructed_values": [
                f"0x{value:05X}" for value in extended_values
            ],
        })

    def public(vector: dict) -> dict:
        return {key: value for key, value in vector.items() if key != "words"}

    return {
        "result": "PASS",
        "main": {"sha256": digest},
        "emulator": {
            "sha256": hashlib.sha256(emulator_path.read_bytes()).hexdigest(),
        },
        "authentic_selection": {
            "track_routing_table": f"0x{TRACK_ROUTING_TABLE:08X}",
            "routing_values": routing,
            "track": TRACK,
            "physical_voice": PHYSICAL_VOICE,
            "machine_selector": f"0x{MACHINE_SELECTOR_BASE + TRACK:08X}",
            "machine_id": MACHINE_ID,
            "renderer_table": f"0x{RENDERER_TABLE:08X}",
            "renderer": f"0x{RENDERER:08X}",
            "function_pointer_substitution": False,
        },
        "storage_free_precondition": {
            "runtime_synth_live_gate": f"0x{SYNTH_LIVE_GATE_BASE + TRACK:08X}",
            "seeded_value": 1,
            "reason": (
                "The fixture has no loaded project sound from which stock MAIN can "
                "copy sound chromatic mode."
            ),
        },
        "pitch_block": {
            "tag_word_indices": list(PITCH_TAG_WORDS),
            "anchor_tag_value": "0x80014040",
            "tag_values_over_notes_0_127": {
                str(word_index): sorted({
                    f"0x{vector['words'][word_index]:08X}" for vector in vectors
                })
                for word_index in PITCH_TAG_WORDS
            },
            "value_word_indices": list(PITCH_VALUE_WORDS),
            "all_six_values_increase_over_anchor_notes_48_60_72": True,
            "channel_laws": channel_laws,
        },
        "anchor_vectors": [public(vector) for vector in anchors],
        "comparisons": comparisons,
        "conclusion": (
            "Under stock track routing and renderer dispatch, note pitch changes "
            "exactly six interleaved value words at 310,312,314,316,318,320. "
            "The fixed 0x80014040 words between them behave as tags or selectors."
        ),
        "interpretation_boundary": (
            "This establishes a six-channel pitch-derived control group. It does "
            "not yet identify the off-chip device, electrical units, or channel pins."
        ),
        "next_target": (
            "Sweep all 128 notes to recover each channel's numeric law, then vary "
            "one renderer-10 sound parameter at a time against words 229..240."
        ),
        "safety": "Stock firmware execution and synthetic RAM state only; no image was modified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("main_image", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    result = probe(args.main_image, args.emulator, args.jobs)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
