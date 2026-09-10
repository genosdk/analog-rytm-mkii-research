#!/usr/bin/env python3
"""Inventory whole-callback writers and readers of the DSPI1 control payload.

All 34 public machine renderers are executed at note 60 with their three event
state words forced to each value 0..4. Accesses to the packetizer's 492 source
halfwords are observed for the complete stock audio callback and tagged as
renderer or non-renderer activity. This is a rejection test: absence from this
finite matrix does not prove that a persistent packet field is unused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from collections import defaultdict
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256
from machine_pitch_calibration_probe import (
    DISPATCH_INDEX_PC,
    MAIN_BASE,
    PUBLIC_MACHINE_NAMES,
    RENDERER_COUNT,
    RENDERER_TABLE,
)
from note_pitch_consumer_boundary_probe import load_emulator, run_vector
from renderer_control_ownership_probe import (
    FORCED_STATES,
    NOTE,
    PACKET_SOURCE_END,
    PACKET_SOURCE_FIRST,
    PACKET_SOURCE_HALFWORDS,
    VOICE_EVENT_STATE,
    compact_ranges,
    packet_index,
    touched_fields,
)

PACKETIZER_READ_PCS = {0x40077D62, 0x40077D64, 0x40077D66, 0x40077D68}
EXPECTED_MATRIX_SHA256 = "88046e164082c48e043ef589970847ba2bc23e42c014e9587bf817bead335bdf"


def run_context(module, main_path: Path, machine_id: int, renderer: int, forced_state: int) -> dict:
    original_step = module.CPU.step
    original_read = module.Bus.read
    original_write = module.Bus.write
    states: dict[int, dict] = {}
    cpus: dict[int, object] = {}
    reads: list[tuple[str, int, int, int]] = []
    writes: list[tuple[str, int, int, int]] = []
    renderer_visits = 0

    def read(bus, address: int, size: int) -> int:
        value = original_read(bus, address, size)
        state = states.get(id(bus), {})
        cpu = cpus.get(id(bus))
        if state.get("callback") and cpu is not None and touched_fields(address, size):
            phase = "renderer" if state.get("renderer") else "non_renderer"
            reads.append((phase, cpu.pc, address, size))
        return value

    def write(bus, address: int, size: int, value: int) -> None:
        state = states.get(id(bus), {})
        cpu = cpus.get(id(bus))
        if state.get("callback") and cpu is not None and touched_fields(address, size):
            phase = "renderer" if state.get("renderer") else "non_renderer"
            writes.append((phase, cpu.pc, address, size))
        original_write(bus, address, size, value)

    def step(cpu):
        nonlocal renderer_visits
        cpus[id(cpu.bus)] = cpu
        state = states.setdefault(
            id(cpu.bus),
            {"callback": False, "renderer": False, "renderer_return": None},
        )
        if cpu.pc == AUDIO_CALLBACK:
            state["callback"] = True
        if cpu.pc == DISPATCH_INDEX_PC and cpu.d[2] == 0:
            cpu.d[1] = machine_id
        if cpu.pc == renderer:
            renderer_visits += 1
            # Experimental selector writes are setup, not stock ownership.
            for offset in (0, 4, 8):
                original_write(cpu.bus, VOICE_EVENT_STATE + offset, 4, forced_state)
            state["renderer_return"] = cpu.bus.read(cpu.a[7], 4)
            state["renderer"] = True
        result = original_step(cpu)
        if state["renderer"] and cpu.pc == state["renderer_return"]:
            state["renderer"] = False
        return result

    module.Bus.read = read
    module.Bus.write = write
    module.CPU.step = step
    run_vector(module, main_path, NOTE, 1, callback_count=1)
    if renderer_visits != 1:
        raise ValueError(
            f"machine {machine_id} state {forced_state} renderer visits: {renderer_visits}"
        )

    def access_summary(events: list[tuple[str, int, int, int]]) -> dict:
        by_phase: dict[str, set[int]] = defaultdict(set)
        pcs_by_field: dict[tuple[str, int], set[int]] = defaultdict(set)
        for phase, pc, address, size in events:
            for field in touched_fields(address, size):
                by_phase[phase].add(field)
                pcs_by_field[(phase, field)].add(pc)
        return {
            "event_count": len(events),
            "renderer_fields": sorted(by_phase["renderer"]),
            "non_renderer_fields": sorted(by_phase["non_renderer"]),
            "pcs_by_field": pcs_by_field,
        }

    return {
        "machine_id": machine_id,
        "state": forced_state,
        "reads": access_summary(reads),
        "writes": access_summary(writes),
    }


def field_rows(fields: set[int], contexts: dict[int, set[tuple[int, int]]], pcs: dict[int, set[int]]) -> list[dict]:
    return [
        {
            "address": f"0x{field:08X}",
            "dspi1_word_index": packet_index(field),
            "context_count": len(contexts[field]),
            "pcs": [f"0x{pc:08X}" for pc in sorted(pcs[field])],
        }
        for field in sorted(fields)
    ]


def probe(main_path: Path, emulator_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    table_offset = RENDERER_TABLE - MAIN_BASE
    renderers = [
        struct.unpack(">I", image[table_offset + 4 * i:table_offset + 4 * i + 4])[0]
        for i in range(RENDERER_COUNT)
    ]

    write_contexts: dict[str, dict[int, set[tuple[int, int]]]] = {
        "renderer": defaultdict(set),
        "non_renderer": defaultdict(set),
    }
    read_contexts: dict[str, dict[int, set[tuple[int, int]]]] = {
        "renderer": defaultdict(set),
        "non_renderer": defaultdict(set),
    }
    write_pcs: dict[str, dict[int, set[int]]] = {
        "renderer": defaultdict(set),
        "non_renderer": defaultdict(set),
    }
    read_pcs: dict[str, dict[int, set[int]]] = {
        "renderer": defaultdict(set),
        "non_renderer": defaultdict(set),
    }
    context_rows = []

    for machine_id, machine_name in enumerate(PUBLIC_MACHINE_NAMES):
        for forced_state in FORCED_STATES:
            context = run_context(
                load_emulator(emulator_path),
                main_path,
                machine_id,
                renderers[machine_id],
                forced_state,
            )
            key = (machine_id, forced_state)
            for kind, contexts, pcs in (
                ("writes", write_contexts, write_pcs),
                ("reads", read_contexts, read_pcs),
            ):
                summary = context[kind]
                for phase in ("renderer", "non_renderer"):
                    for field in summary[f"{phase}_fields"]:
                        contexts[phase][field].add(key)
                        pcs[phase][field].update(summary["pcs_by_field"][(phase, field)])
            context_rows.append({
                "machine_id": machine_id,
                "machine_name": machine_name,
                "state": forced_state,
                "write_events": context["writes"]["event_count"],
                "read_events": context["reads"]["event_count"],
                "renderer_write_fields": len(context["writes"]["renderer_fields"]),
                "non_renderer_write_fields": len(context["writes"]["non_renderer_fields"]),
                "packet_read_fields": len(
                    set(context["reads"]["renderer_fields"])
                    | set(context["reads"]["non_renderer_fields"])
                ),
            })

    renderer_written = set(write_contexts["renderer"])
    non_renderer_written = set(write_contexts["non_renderer"])
    all_written = renderer_written | non_renderer_written
    all_read = set(read_contexts["renderer"]) | set(read_contexts["non_renderer"])
    packetizer_read = {
        field
        for phase in ("renderer", "non_renderer")
        for field, pcs in read_pcs[phase].items()
        if pcs & PACKETIZER_READ_PCS
    }
    packet_fields = {PACKET_SOURCE_FIRST + 2 * i for i in range(PACKET_SOURCE_HALFWORDS)}
    never_written = packet_fields - all_written
    read_never_written = all_read - all_written

    if len(renderer_written) != 117:
        raise ValueError(f"renderer ownership changed: {len(renderer_written)} fields")
    if packetizer_read != packet_fields:
        missing = sorted(packet_fields - packetizer_read)
        raise ValueError(f"packetizer did not read every payload field: {missing}")

    matrix_payload = json.dumps(context_rows, sort_keys=True).encode()
    matrix_digest = hashlib.sha256(matrix_payload).hexdigest()
    if matrix_digest != EXPECTED_MATRIX_SHA256:
        raise ValueError(f"whole-callback matrix changed: {matrix_digest}")
    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "coverage": {
            "public_machines": len(PUBLIC_MACHINE_NAMES),
            "forced_states": list(FORCED_STATES),
            "contexts": len(context_rows),
            "note": NOTE,
            "matrix_sha256": matrix_digest,
        },
        "packet_source": {
            "first_halfword": f"0x{PACKET_SOURCE_FIRST:08X}",
            "last_halfword": f"0x{PACKET_SOURCE_END - 2:08X}",
            "halfwords": PACKET_SOURCE_HALFWORDS,
        },
        "summary": {
            "renderer_written_field_count": len(renderer_written),
            "non_renderer_written_field_count": len(non_renderer_written),
            "all_callback_written_field_count": len(all_written),
            "packet_read_field_count": len(all_read),
            "packetizer_read_field_count": len(packetizer_read),
            "never_callback_written_field_count": len(never_written),
            "read_but_never_callback_written_field_count": len(read_never_written),
            "never_callback_written_ranges": compact_ranges(
                [packet_index(field) for field in sorted(never_written)]
            ),
        },
        "writers": {
            "renderer": field_rows(renderer_written, write_contexts["renderer"], write_pcs["renderer"]),
            "non_renderer": field_rows(
                non_renderer_written,
                write_contexts["non_renderer"],
                write_pcs["non_renderer"],
            ),
        },
        "packetizer_consumer": {
            "entry": "0x40077D14",
            "read_pcs": [f"0x{pc:08X}" for pc in sorted(PACKETIZER_READ_PCS)],
            "consumed_field_count": len(packetizer_read),
            "consumed_ranges": compact_ranges(
                [packet_index(field) for field in sorted(packetizer_read)]
            ),
        },
        "read_but_never_callback_written": field_rows(
            read_never_written,
            {field: read_contexts["renderer"][field] | read_contexts["non_renderer"][field] for field in read_never_written},
            {field: read_pcs["renderer"][field] | read_pcs["non_renderer"][field] for field in read_never_written},
        ),
        "contexts": context_rows,
        "conclusion": (
            "Whole-callback tracing rejects every field written by either a renderer or "
            "other callback code. Fields not written in this matrix remain only candidates: "
            "persistent initialization, other events and other operating modes still require tracing."
        ),
        "next_target": (
            "Trace initialization and non-note event paths for read-but-never-callback-written "
            "fields before attempting an inert canary."
        ),
        "safety": "Stock firmware execution and synthetic renderer-state selection only; no firmware was modified.",
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
