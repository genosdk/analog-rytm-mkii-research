#!/usr/bin/env python3
"""Inventory stock renderer writes that overlap the outbound DSPI1 control frame.

All 34 public machine renderers are executed at note 60 with their three event
state words set to each value 0..4. Writes are observed only between renderer
entry and return, then mapped to the packetizer's consecutive halfword source
window. This is an emulation-only rejection test for Filter 2 transport fields;
an unobserved write is not by itself proof that a field is globally spare.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from collections import Counter, defaultdict
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256
from machine_pitch_calibration_probe import (
    DISPATCH_INDEX_PC,
    MAIN_BASE,
    PUBLIC_MACHINE_NAMES,
    RENDERER_COUNT,
    RENDERER_TABLE,
)
from note_pitch_consumer_boundary_probe import load_emulator, run_vector

FORCED_STATES = tuple(range(5))
NOTE = 60
VOICE_EVENT_STATE = 0x8000FEF8
PACKET_SOURCE_FIRST = 0x800063C0
PACKET_SOURCE_HALFWORDS = 492
PACKET_SOURCE_END = PACKET_SOURCE_FIRST + 2 * PACKET_SOURCE_HALFWORDS
EXPECTED_MATRIX_SHA256 = "be9a3348bf1c44a95a706f6ac4dec15e71446916bdc2dd8141c75d95374b841b"


def packet_index(field_address: int) -> int:
    if field_address & 1 or not PACKET_SOURCE_FIRST <= field_address < PACKET_SOURCE_END:
        raise ValueError(f"not a packet-source halfword: 0x{field_address:08X}")
    return 1 + (field_address - PACKET_SOURCE_FIRST) // 2


def touched_fields(address: int, size: int) -> list[int]:
    first = max(address, PACKET_SOURCE_FIRST)
    last = min(address + size, PACKET_SOURCE_END)
    if first >= last:
        return []
    first &= ~1
    return list(range(first, last, 2))


def compact_ranges(indices: list[int]) -> list[dict]:
    ranges: list[list[int]] = []
    for index in indices:
        if not ranges or index != ranges[-1][1] + 1:
            ranges.append([index, index])
        else:
            ranges[-1][1] = index
    return [
        {
            "first_word": first,
            "last_word": last,
            "first_address": f"0x{PACKET_SOURCE_FIRST + 2 * (first - 1):08X}",
            "last_address": f"0x{PACKET_SOURCE_FIRST + 2 * (last - 1):08X}",
        }
        for first, last in ranges
    ]


def run_state(module, main_path: Path, machine_id: int, renderer: int, forced_state: int) -> dict:
    original_step = module.CPU.step
    original_write = module.Bus.write
    states: dict[int, dict] = {}
    cpus: dict[int, object] = {}
    events: list[tuple[int, int, int, int]] = []
    visits = 0

    def write(bus, address: int, size: int, value: int) -> None:
        state = states.get(id(bus), {})
        cpu = cpus.get(id(bus))
        if state.get("active") and touched_fields(address, size):
            events.append((cpu.pc, address, size, value & ((1 << (8 * size)) - 1)))
        original_write(bus, address, size, value)

    def step(cpu):
        nonlocal visits
        cpus[id(cpu.bus)] = cpu
        state = states.setdefault(id(cpu.bus), {"active": False, "return": None})
        if cpu.pc == DISPATCH_INDEX_PC and cpu.d[2] == 0:
            cpu.d[1] = machine_id
        if cpu.pc == renderer:
            visits += 1
            # Bypass the tracing wrapper for the three synthetic selector
            # writes: they configure the experiment and are not renderer output.
            for offset in (0, 4, 8):
                original_write(cpu.bus, VOICE_EVENT_STATE + offset, 4, forced_state)
            state["return"] = cpu.bus.read(cpu.a[7], 4)
            state["active"] = True
        result = original_step(cpu)
        if state["active"] and cpu.pc == state["return"]:
            state["active"] = False
        return result

    module.Bus.write = write
    module.CPU.step = step
    run_vector(module, main_path, NOTE, 1, callback_count=1)
    if visits != 1:
        raise ValueError(f"machine {machine_id} state {forced_state} renderer visits: {visits}")

    field_events: dict[int, list[dict]] = defaultdict(list)
    for pc, address, size, value in events:
        for field in touched_fields(address, size):
            field_events[field].append({
                "pc": f"0x{pc:08X}",
                "write_address": f"0x{address:08X}",
                "write_size": size,
                "value": f"0x{value:0{size * 2}X}",
            })
    return {
        "state": forced_state,
        "write_events": len(events),
        "owned_fields": [
            {
                "address": f"0x{field:08X}",
                "dspi1_word_index": packet_index(field),
                "events": field_events[field],
            }
            for field in sorted(field_events)
        ],
    }


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

    machines = []
    owners: dict[int, set[tuple[int, int]]] = defaultdict(set)
    write_counts = Counter()
    for machine_id, machine_name in enumerate(PUBLIC_MACHINE_NAMES):
        states = []
        for forced_state in FORCED_STATES:
            module = load_emulator(emulator_path)
            state = run_state(module, main_path, machine_id, renderers[machine_id], forced_state)
            states.append(state)
            write_counts[(machine_id, forced_state)] = state["write_events"]
            for field in state["owned_fields"]:
                owners[int(field["address"], 16)].add((machine_id, forced_state))
        machines.append({
            "machine_id": machine_id,
            "machine_name": machine_name,
            "renderer": f"0x{renderers[machine_id]:08X}",
            "states": states,
        })

    all_contexts = {(machine_id, state) for machine_id in range(len(PUBLIC_MACHINE_NAMES)) for state in FORCED_STATES}
    union = sorted(owners)
    universal = [field for field in union if owners[field] == all_contexts]
    untouched = [
        PACKET_SOURCE_FIRST + 2 * index
        for index in range(PACKET_SOURCE_HALFWORDS)
        if PACKET_SOURCE_FIRST + 2 * index not in owners
    ]
    fields = []
    for field in union:
        contexts = owners[field]
        fields.append({
            "address": f"0x{field:08X}",
            "dspi1_word_index": packet_index(field),
            "machine_ids": sorted({machine for machine, _ in contexts}),
            "states": sorted({state for _, state in contexts}),
            "context_count": len(contexts),
        })

    # Known identities independently constrain the packet-index equation.
    if packet_index(0x8000641A) != 46 or packet_index(0x8000641C) != 47:
        raise ValueError("pitch packet mapping changed")
    if packet_index(0x80006544) != 195 or packet_index(0x80006546) != 196:
        raise ValueError("packed-control packet mapping changed")
    if not union:
        raise ValueError("no renderer-owned packet fields observed")

    matrix_digest = hashlib.sha256(json.dumps(machines, sort_keys=True).encode()).hexdigest()
    if matrix_digest != EXPECTED_MATRIX_SHA256:
        raise ValueError(f"renderer ownership matrix changed: {matrix_digest}")
    if (len(union), len(universal), len(untouched)) != (117, 0, 375):
        raise ValueError("unexpected renderer ownership summary")
    untouched_words = [packet_index(field) for field in untouched]
    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "coverage": {
            "public_machines": len(PUBLIC_MACHINE_NAMES),
            "forced_states": list(FORCED_STATES),
            "contexts": len(all_contexts),
            "note": NOTE,
            "matrix_sha256": matrix_digest,
        },
        "packet_source": {
            "first_halfword": f"0x{PACKET_SOURCE_FIRST:08X}",
            "last_halfword": f"0x{PACKET_SOURCE_END - 2:08X}",
            "halfwords": PACKET_SOURCE_HALFWORDS,
            "mapping": "dspi1_word_index = 1 + (address - 0x800063C0) / 2",
            "known_fields": {
                "pitch_high": {"address": "0x8000641A", "word": 46},
                "pitch_low": {"address": "0x8000641C", "word": 47},
                "packed_control_high": {"address": "0x80006544", "word": 195},
                "packed_control_low": {"address": "0x80006546", "word": 196},
            },
        },
        "ownership_summary": {
            "observed_owned_field_count": len(union),
            "universal_field_count": len(universal),
            "universal_fields": [f"0x{field:08X}" for field in universal],
            "unobserved_field_count": len(untouched),
            "unobserved_renderer_write_ranges": compact_ranges(untouched_words),
            "warning": (
                "Unobserved means not written by these renderer/state executions; it does "
                "not prove the field is globally unused or safe for Filter 2."
            ),
            "fields": fields,
        },
        "machines": machines,
        "conclusion": (
            "The sweep maps renderer-owned outbound halfwords across every public machine "
            "and forced state. Any observed-owned field is rejected as universal Filter 2 "
            "transport. Unobserved fields require a whole-callback writer and consumer test "
            "before they can be considered candidates."
        ),
        "next_target": (
            "Trace all non-renderer writers and packet consumers for the unobserved fields, "
            "then test canary publication only in fields with no demonstrated stock owner."
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
