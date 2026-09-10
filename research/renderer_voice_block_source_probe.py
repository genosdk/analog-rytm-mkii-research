#!/usr/bin/env python3
"""Bind the repeated DSPI1 control slots to their stock SRAM producers.

The probe runs machine selector 10 through the stock track-6 routing path.  It
traces the renderer's writes and the packetizer's halfword reads.  This splits
the renderer's fixed setup at words 229..240 from its note-dependent physical-
voice-5 output at words 309..320.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256, prepared_machine
from authentic_renderer_pitch_probe import (
    MACHINE_ID,
    MACHINE_SELECTOR_BASE,
    PHYSICAL_VOICE,
    RENDERER,
    SYNTH_LIVE_GATE_BASE,
    TRACK,
    renderer_addresses,
)
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

VOICE_COUNT = 8
VOICE_SOURCE_BASE = 0x80006588
VOICE_SOURCE_STRIDE = 0x20
VOICE_PACKET_BASE = 229
VOICE_PACKET_STRIDE = 16
RENDERER_OUTPUT_HALFWORDS = 12
PACKETIZER_SOURCE_READ_PCS = {0x40077D62, 0x40077D64, 0x40077D66, 0x40077D68}
PACKETIZER_HEADER = 0x80010000
ANCHOR_NOTES = (48, 60, 72)


def run_vector(module, main_path: Path, renderers: list[int], note: int) -> dict:
    bus, cpu, _ = prepared_machine(module, main_path)
    callback_sp = cpu.a[7]
    stock_call(cpu, CONTROL_DMA_INITIALIZER, [])
    stock_call(cpu, QUEUE_INITIALIZER, [QUEUE, 0, QUEUE_RING, QUEUE_CAPACITY])
    stock_call(cpu, QUEUE_INSTALLER, [QUEUE])
    bus.write(CONTROL_SNAPSHOT_POINTER, 4, CONTROL_SNAPSHOT_A)

    bus.write(MACHINE_SELECTOR_BASE + TRACK, 1, MACHINE_ID)
    write_event(bus, event_type=NOTE_ON, note=note, source_mask=QWERTY_SOURCE_MASK)
    bus.write(EVENT_INPUT + EVENT_TRACK, 4, TRACK)
    stock_call(cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])
    bus.write(SYNTH_LIVE_GATE_BASE + TRACK, 1, 1)

    original_read, original_write = bus.read, bus.write
    renderer_active = False
    renderer_return = None
    calls = []
    renderer_writes = []
    renderer_reads = []
    packetizer_reads = []

    def traced_read(address: int, size: int) -> int:
        value = original_read(address, size)
        if renderer_active and (
            0x80000000 <= address < 0x80080000
            or 0x41000000 <= address < 0x42000000
        ):
            renderer_reads.append({
                "pc": cpu.pc,
                "address": address,
                "size": size,
                "value": value,
            })
        if (
            cpu.pc in PACKETIZER_SOURCE_READ_PCS
            and size == 2
            and VOICE_SOURCE_BASE
            <= address
            < VOICE_SOURCE_BASE + VOICE_COUNT * VOICE_SOURCE_STRIDE
        ):
            packetizer_reads.append({
                "pc": cpu.pc,
                "address": address,
                "value": value,
            })
        return value

    def traced_write(address: int, size: int, value: int) -> None:
        if renderer_active and (
            VOICE_SOURCE_BASE
            <= address
            < VOICE_SOURCE_BASE + VOICE_COUNT * VOICE_SOURCE_STRIDE
        ):
            renderer_writes.append({
                "pc": cpu.pc,
                "address": address,
                "size": size,
                "value": value & ((1 << (8 * size)) - 1),
            })
        original_write(address, size, value)

    bus.read, bus.write = traced_read, traced_write
    try:
        cpu.a[7] = callback_sp
        cpu.pc = 0x4011B3AE
        for _ in range(200_000):
            if cpu.pc in renderers and not renderer_active:
                renderer_active = True
                renderer_return = original_read(cpu.a[7], 4)
                calls.append({
                    "index": renderers.index(cpu.pc),
                    "renderer": cpu.pc,
                    "arguments": [
                        original_read(cpu.a[7] + 4 + 4 * index, 4)
                        for index in range(4)
                    ],
                })
            if renderer_active and cpu.pc == renderer_return:
                renderer_active = False
            if cpu.pc == FULL_CALLBACK_STOP:
                break
            cpu.step()
        else:
            raise ValueError("audio callback did not reach its final RTE")
    finally:
        bus.read, bus.write = original_read, original_write

    selector = bus.read(PING_PONG_SELECTOR, 4)
    packet_base = PING_PONG_BASE + selector * PING_PONG_STRIDE
    words = [bus.read(packet_base + 0x0C + 4 * index, 4) for index in range(510)]
    return {
        "note": note,
        "calls": calls,
        "renderer_writes": renderer_writes,
        "renderer_reads": renderer_reads,
        "packetizer_reads": packetizer_reads,
        "words": words,
    }


def hexadecimal(value: int, width: int = 8) -> str:
    return f"0x{value:0{width}X}"


def probe(main_path: Path, emulator_path: Path) -> dict:
    main = main_path.read_bytes()
    digest = hashlib.sha256(main).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    module = load_emulator(emulator_path)
    renderers = renderer_addresses(main)
    vectors = [run_vector(module, main_path, renderers, note) for note in ANCHOR_NOTES]

    control_slots = []
    for slot in range(VOICE_COUNT):
        source_first = VOICE_SOURCE_BASE + slot * VOICE_SOURCE_STRIDE
        packet_first = VOICE_PACKET_BASE + slot * VOICE_PACKET_STRIDE
        control_slots.append({
            "slot": slot,
            "source_halfword_range": [
                hexadecimal(source_first),
                hexadecimal(source_first + VOICE_SOURCE_STRIDE - 2),
            ],
            "packet_word_range": [packet_first, packet_first + VOICE_PACKET_STRIDE - 1],
            "renderer_output_source_range": [
                hexadecimal(source_first),
                hexadecimal(source_first + 2 * RENDERER_OUTPUT_HALFWORDS - 2),
            ],
            "renderer_output_word_range": [
                packet_first,
                packet_first + RENDERER_OUTPUT_HALFWORDS - 1,
            ],
        })

    output_base = VOICE_SOURCE_BASE + PHYSICAL_VOICE * VOICE_SOURCE_STRIDE
    output_addresses = [output_base + 4 * index for index in range(6)]
    setup_addresses = [VOICE_SOURCE_BASE + 4 * index for index in range(6)]
    output_words = [
        VOICE_PACKET_BASE + PHYSICAL_VOICE * VOICE_PACKET_STRIDE + index
        for index in range(RENDERER_OUTPUT_HALFWORDS)
    ]
    public_vectors = []
    for vector in vectors:
        calls = vector["calls"]
        if len(calls) != 1 or calls[0]["index"] != MACHINE_ID:
            raise ValueError(f"renderer 10 was not naturally selected: {calls}")
        if calls[0]["arguments"][0] != PHYSICAL_VOICE:
            raise ValueError("unexpected physical voice argument")
        if calls[0]["arguments"][3] != vector["note"] << 16:
            raise ValueError("renderer did not receive live note pitch")

        writes = vector["renderer_writes"]
        if [row["address"] for row in writes] != setup_addresses + output_addresses:
            raise ValueError(f"unexpected renderer output addresses: {writes}")
        if any(row["size"] != 4 for row in writes):
            raise ValueError("renderer output was not six longword writes")

        setup_writes = writes[:6]
        output_writes = writes[6:]
        emitted_halfwords = []
        for row in output_writes:
            emitted_halfwords.extend([row["value"] >> 16, row["value"] & 0xFFFF])
        packet_values = [vector["words"][index] for index in output_words]
        if packet_values != [PACKETIZER_HEADER | value for value in emitted_halfwords]:
            raise ValueError("renderer writes did not reach the homologous packet words")
        setup_halfwords = []
        for row in setup_writes:
            setup_halfwords.extend([row["value"] >> 16, row["value"] & 0xFFFF])
        setup_packet_values = [vector["words"][index] for index in range(229, 241)]
        if setup_packet_values != [
            PACKETIZER_HEADER | value for value in setup_halfwords
        ]:
            raise ValueError("renderer setup writes did not reach words 229..240")

        packet_read_pairs = {
            (row["address"], row["value"]) for row in vector["packetizer_reads"]
        }
        for voice in range(VOICE_COUNT):
            for offset in range(VOICE_PACKET_STRIDE):
                address = VOICE_SOURCE_BASE + voice * VOICE_SOURCE_STRIDE + 2 * offset
                packet_index = VOICE_PACKET_BASE + voice * VOICE_PACKET_STRIDE + offset
                value = vector["words"][packet_index] & 0xFFFF
                if (address, value) not in packet_read_pairs:
                    raise ValueError(
                        f"missing packetizer source read for word {packet_index}"
                    )

        external_parameter_reads = [
            row for row in vector["renderer_reads"]
            if 0x41000000 <= row["address"] < 0x42000000
        ]
        if external_parameter_reads:
            raise ValueError(
                f"renderer unexpectedly read the project/data slab: {external_parameter_reads}"
            )
        public_vectors.append({
            "note": vector["note"],
            "renderer_arguments": [hexadecimal(value) for value in calls[0]["arguments"]],
            "fixed_setup_writes": [
                {
                    "pc": hexadecimal(row["pc"]),
                    "address": hexadecimal(row["address"]),
                    "value": hexadecimal(row["value"]),
                }
                for row in setup_writes
            ],
            "note_dependent_output_writes": [
                {
                    "pc": hexadecimal(row["pc"]),
                    "address": hexadecimal(row["address"]),
                    "value": hexadecimal(row["value"]),
                }
                for row in output_writes
            ],
            "packet_words_229_240": {
                str(index): hexadecimal(vector["words"][index])
                for index in range(229, 241)
            },
            "packet_words_309_320": {
                str(index): hexadecimal(vector["words"][index]) for index in output_words
            },
            "renderer_data_reads": [
                {
                    "pc": hexadecimal(row["pc"]),
                    "address": hexadecimal(row["address"]),
                    "size": row["size"],
                    "value": hexadecimal(row["value"], 2 * row["size"]),
                }
                for row in vector["renderer_reads"]
            ],
        })

    return {
        "result": "PASS",
        "main": {"sha256": digest},
        "emulator": {"sha256": hashlib.sha256(emulator_path.read_bytes()).hexdigest()},
        "packetizer_layout": {
            "source_base": hexadecimal(VOICE_SOURCE_BASE),
            "source_stride_bytes_per_slot": VOICE_SOURCE_STRIDE,
            "packet_base_word": VOICE_PACKET_BASE,
            "packet_stride_words_per_slot": VOICE_PACKET_STRIDE,
            "packet_encoding": "0x80010000 | source_halfword",
            "control_slots": control_slots,
        },
        "authentic_renderer_10": {
            "track": TRACK,
            "physical_voice": PHYSICAL_VOICE,
            "machine_id": MACHINE_ID,
            "renderer": hexadecimal(RENDERER),
            "function_pointer_substitution": False,
            "output_base": hexadecimal(output_base),
            "fixed_setup_base": hexadecimal(VOICE_SOURCE_BASE),
            "six_longword_fixed_setup_addresses": [
                hexadecimal(value) for value in setup_addresses
            ],
            "six_longword_output_addresses": [hexadecimal(value) for value in output_addresses],
            "packet_word_range": [output_words[0], output_words[-1]],
            "anchor_vectors": public_vectors,
        },
        "resolved_target": {
            "requested_word_range": [229, 240],
            "source_halfword_range": ["0x80006588", "0x8000659E"],
            "renderer_write_pc_range": ["0x40110BE6", "0x40110BFE"],
            "values_constant_across_notes_48_60_72": len({
                tuple(vector["words"][index] for index in range(229, 241))
                for vector in vectors
            }) == 1,
            "finding": (
                "Renderer 10 writes words 229..240 directly as six fixed "
                "longwords before calculating its note-dependent physical-voice-5 "
                "output at words 309..320."
            ),
        },
        "parameter_boundary": {
            "direct_renderer_arguments": [
                "physical voice",
                "shared control pointer",
                "per-voice state pointer",
                "pitch",
            ],
            "project_data_reads_in_renderer_0x41000000_0x41FFFFFF": 0,
            "storage_free_fixture_renderer_state_reads": [
                "0x8000F9B2",
                "0x8000F9B4",
                "0x8000F9B8",
                "0x8000FF98",
            ],
            "conclusion": (
                "The source feeding words 229..240 is a fixed renderer-10 setup "
                "sequence, not a varying sound parameter in this execution. The "
                "renderer's varying live input is pitch and reaches words 309..320; "
                "no separate project sound-parameter read is exposed here."
            ),
        },
        "next_target": (
            "Classify renderer-10 setup words 229..240 across all machine selectors, "
            "then trace words 321..324 and the higher-level parameter publication path."
        ),
        "safety": "Stock firmware execution and synthetic RAM state only; no image was modified.",
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
