#!/usr/bin/env python3
"""Classify all audio-callback writers and reads of DSPI1 source fields."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from collections import defaultdict
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256
from machine_pitch_calibration_probe import (
    DISPATCH_INDEX_PC, MAIN_BASE, PUBLIC_MACHINE_NAMES, RENDERER_COUNT,
    RENDERER_TABLE,
)
from note_pitch_consumer_boundary_probe import load_emulator, run_vector
from renderer_control_ownership_probe import (
    FORCED_STATES, NOTE, PACKET_SOURCE_END, PACKET_SOURCE_FIRST,
    PACKET_SOURCE_HALFWORDS, VOICE_EVENT_STATE, compact_ranges, packet_index,
    touched_fields,
)
from trigger_queue_probe import FULL_CALLBACK_STOP

PACKETIZER_READ_PCS = {0x40077D62, 0x40077D64, 0x40077D66, 0x40077D68}
EXPECTED_MATRIX_SHA256 = "429d3673ef89f81edc1014f3a8eaa45d0db88690c54951b9b92a7ab9fad88e6f"


def run_context(module, main_path: Path, machine_id: int, renderer: int, forced_state: int) -> dict:
    original_step = module.CPU.step
    original_write = module.Bus.write
    original_read = module.Bus.read
    states, cpus = {}, {}
    renderer_events, nonrenderer_events, packet_reads = [], [], []
    visits = 0

    def write(bus, address: int, size: int, value: int) -> None:
        state = states.get(id(bus), {})
        cpu = cpus.get(id(bus))
        fields = touched_fields(address, size)
        if state.get("callback") and fields:
            target = renderer_events if state.get("renderer") else nonrenderer_events
            target.append((cpu.pc, address, size, tuple(fields)))
        original_write(bus, address, size, value)

    def read(bus, address: int, size: int) -> int:
        value = original_read(bus, address, size)
        state = states.get(id(bus), {})
        cpu = cpus.get(id(bus))
        if (state.get("callback") and cpu is not None and cpu.pc in PACKETIZER_READ_PCS
                and size == 2 and PACKET_SOURCE_FIRST <= address < PACKET_SOURCE_END):
            packet_reads.append((cpu.pc, address, value))
        return value

    def step(cpu):
        nonlocal visits
        cpus[id(cpu.bus)] = cpu
        state = states.setdefault(id(cpu.bus), {"callback": False, "renderer": False, "return": None})
        if cpu.pc == AUDIO_CALLBACK:
            state["callback"] = True
        if cpu.pc == DISPATCH_INDEX_PC and cpu.d[2] == 0:
            cpu.d[1] = machine_id
        if cpu.pc == renderer:
            visits += 1
            for offset in (0, 4, 8):
                original_write(cpu.bus, VOICE_EVENT_STATE + offset, 4, forced_state)
            state["return"] = cpu.bus.read(cpu.a[7], 4)
            state["renderer"] = True
        result = original_step(cpu)
        if state["renderer"] and cpu.pc == state["return"]:
            state["renderer"] = False
        if cpu.pc == FULL_CALLBACK_STOP:
            state["callback"] = False
        return result

    module.Bus.write, module.Bus.read, module.CPU.step = write, read, step
    run_vector(module, main_path, NOTE, 1, callback_count=1)
    if visits != 1:
        raise ValueError(f"machine {machine_id} state {forced_state} renderer visits: {visits}")
    expected_reads = set(range(PACKET_SOURCE_FIRST, PACKET_SOURCE_END, 2))
    read_fields = {address for _, address, _ in packet_reads}
    if read_fields != expected_reads or len(packet_reads) != PACKET_SOURCE_HALFWORDS:
        raise ValueError(f"machine {machine_id} state {forced_state} packetizer reads changed")

    def summarize(events):
        fields = sorted({field for _, _, _, touched in events for field in touched})
        return {
            "event_count": len(events),
            "field_addresses": [f"0x{field:08X}" for field in fields],
            "word_indices": [packet_index(field) for field in fields],
            "writer_pcs": [f"0x{pc:08X}" for pc in sorted({event[0] for event in events})],
        }

    return {
        "state": forced_state,
        "renderer": summarize(renderer_events),
        "nonrenderer": summarize(nonrenderer_events),
        "packetizer_reads": len(packet_reads),
    }


def probe(main_path: Path, emulator_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    offset = RENDERER_TABLE - MAIN_BASE
    renderers = [struct.unpack(">I", image[offset + 4*i:offset + 4*i + 4])[0] for i in range(RENDERER_COUNT)]
    contexts = []
    renderer_owners, nonrenderer_owners = defaultdict(set), defaultdict(set)
    for machine_id, machine_name in enumerate(PUBLIC_MACHINE_NAMES):
        for forced_state in FORCED_STATES:
            result = run_context(load_emulator(emulator_path), main_path, machine_id, renderers[machine_id], forced_state)
            context = (machine_id, forced_state)
            for address in result["renderer"]["field_addresses"]:
                renderer_owners[int(address, 16)].add(context)
            for address in result["nonrenderer"]["field_addresses"]:
                nonrenderer_owners[int(address, 16)].add(context)
            contexts.append({"machine_id": machine_id, "machine_name": machine_name,
                             "renderer": f"0x{renderers[machine_id]:08X}", **result})

    all_fields = set(range(PACKET_SOURCE_FIRST, PACKET_SOURCE_END, 2))
    renderer_fields, nonrenderer_fields = set(renderer_owners), set(nonrenderer_owners)
    any_writer = renderer_fields | nonrenderer_fields
    writer_free = sorted(all_fields - any_writer)
    all_contexts = {(machine, state) for machine in range(len(PUBLIC_MACHINE_NAMES)) for state in FORCED_STATES}

    def ownership_rows(owners):
        return [{"address": f"0x{field:08X}", "dspi1_word_index": packet_index(field),
                 "context_count": len(owners[field]), "universal": owners[field] == all_contexts}
                for field in sorted(owners)]

    matrix_digest = hashlib.sha256(json.dumps(contexts, sort_keys=True).encode()).hexdigest()
    if matrix_digest != EXPECTED_MATRIX_SHA256:
        raise ValueError(f"global ownership matrix changed: {matrix_digest}")
    if (len(renderer_fields), len(nonrenderer_fields), len(renderer_fields & nonrenderer_fields),
            len(any_writer), len(writer_free)) != (117, 233, 23, 327, 165):
        raise ValueError("unexpected global ownership totals")
    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "coverage": {"public_machines": len(PUBLIC_MACHINE_NAMES), "states": list(FORCED_STATES),
                     "contexts": len(contexts), "note": NOTE, "matrix_sha256": matrix_digest},
        "packetizer": {
            "routine": "0x40077D14", "loop_read_pcs": [f"0x{pc:08X}" for pc in sorted(PACKETIZER_READ_PCS)],
            "source_first": f"0x{PACKET_SOURCE_FIRST:08X}", "source_last": f"0x{PACKET_SOURCE_END-2:08X}",
            "source_halfwords_per_context": PACKET_SOURCE_HALFWORDS, "source_word_indices": [1, 492],
            "terminator_word": {"index": 493, "literal": "0x08005555", "instruction": "0x40077D74"},
            "all_source_fields_read_in_every_context": True,
        },
        "ownership": {
            "renderer_field_count": len(renderer_fields), "nonrenderer_field_count": len(nonrenderer_fields),
            "overlap_field_count": len(renderer_fields & nonrenderer_fields),
            "any_callback_writer_field_count": len(any_writer), "writer_free_field_count": len(writer_free),
            "writer_free_ranges": compact_ranges([packet_index(field) for field in writer_free]),
            "renderer_fields": ownership_rows(renderer_owners),
            "nonrenderer_fields": ownership_rows(nonrenderer_owners),
            "warning": "Writer-free means no stock callback write in these 170 synthetic contexts; FPGA meaning remains unknown.",
        },
        "contexts": contexts,
        "conclusion": "All 492 SRAM source halfwords are serialized every callback. Only fields with neither renderer nor non-renderer writes may advance to canary testing.",
        "next_target": "Seed bounded canaries in writer-free fields and prove survival into DSPI1 across all public machine/state contexts.",
        "safety": "Stock emulation and synthetic renderer-state selection only; no firmware was modified.",
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
