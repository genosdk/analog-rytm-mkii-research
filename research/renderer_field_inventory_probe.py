#!/usr/bin/env python3
"""Inventory DSPI1 payload fields written by every public stock renderer.

Each vector substitutes only the renderer-table index at the proven stock
dispatch boundary, sends an authentic note event, and records writes made
between entry to that renderer and its return (including nested helpers).
Firmware bytes are never modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from collections import defaultdict
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256
from machine_pitch_calibration_probe import (
    DISPATCH_INDEX_PC,
    MAIN_BASE,
    NOTES,
    PUBLIC_MACHINE_NAMES,
    RENDERER_COUNT,
    RENDERER_TABLE,
)
from note_pitch_consumer_boundary_probe import (
    PACKET_SOURCE_OFFSET,
    PACKET_WORDS,
    load_emulator,
    run_vector,
)

CONTROL_SOURCE_BASE = 0x800063BE
CONTROL_SOURCE_END = CONTROL_SOURCE_BASE + PACKET_WORDS * 2


def covered_indices(address: int, size: int) -> list[int]:
    start = max(address, CONTROL_SOURCE_BASE)
    end = min(address + size, CONTROL_SOURCE_END)
    if start >= end:
        return []
    first = (start - CONTROL_SOURCE_BASE) // 2
    last = (end - 1 - CONTROL_SOURCE_BASE) // 2
    return list(range(first, last + 1))


def run_machine_vector(
    emulator_path: Path,
    main_path: Path,
    machine_id: int,
    renderer: int,
    note: int,
) -> dict:
    module = load_emulator(emulator_path)
    holder: dict[str, object] = {}
    active = False
    return_pc: int | None = None
    visits = 0
    writes: list[dict] = []

    original_init = module.CPU.__init__
    original_step = module.CPU.step
    original_write = module.Bus.write

    def cpu_init(cpu, *args, **kwargs):
        original_init(cpu, *args, **kwargs)
        holder["cpu"] = cpu

    def step(cpu):
        nonlocal active, return_pc, visits
        if cpu.pc == DISPATCH_INDEX_PC and cpu.d[2] == 0:
            cpu.d[1] = machine_id
        if cpu.pc == renderer and not active:
            visits += 1
            active = True
            return_pc = cpu.bus.read(cpu.a[7], 4)
        elif active and return_pc is not None and cpu.pc == return_pc:
            active = False
        return original_step(cpu)

    def write(bus, address: int, size: int, value: int) -> None:
        cpu = holder.get("cpu")
        indices = covered_indices(address, size) if active and cpu is not None else []
        if indices:
            writes.append({
                "pc": cpu.pc,
                "address": address,
                "size": size,
                "value": value & ((1 << (size * 8)) - 1),
                "indices": indices,
            })
        original_write(bus, address, size, value)

    module.CPU.__init__ = cpu_init
    module.CPU.step = step
    module.Bus.write = write
    vector = run_vector(module, main_path, note, 1, callback_count=1)
    if visits != 1:
        raise ValueError(f"machine {machine_id} renderer visits: {visits}")

    by_index: dict[int, dict] = {}
    for event in writes:
        for index in event["indices"]:
            item = by_index.setdefault(index, {"write_count": 0, "pcs": set()})
            item["write_count"] += 1
            item["pcs"].add(event["pc"])
    words = vector["packet_words"]
    return {
        "note": note,
        "renderer_write_events": len(writes),
        "fields": {
            index: {
                "value": words[index] & 0xFFFF,
                "write_count": item["write_count"],
                "pcs": sorted(item["pcs"]),
            }
            for index, item in sorted(by_index.items())
        },
    }


def ranges(indices: list[int]) -> list[list[int]]:
    if not indices:
        return []
    result = []
    start = previous = indices[0]
    for index in indices[1:]:
        if index != previous + 1:
            result.append([start, previous])
            start = index
        previous = index
    result.append([start, previous])
    return result


def probe(main_path: Path, emulator_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")

    offset = RENDERER_TABLE - MAIN_BASE
    renderers = [
        struct.unpack(">I", image[offset + 4 * index:offset + 4 * index + 4])[0]
        for index in range(RENDERER_COUNT)
    ]
    machines = []
    owners: dict[int, list[int]] = defaultdict(list)
    variant_owners: dict[int, list[int]] = defaultdict(list)

    for machine_id, machine_name in enumerate(PUBLIC_MACHINE_NAMES):
        vectors = [
            run_machine_vector(emulator_path, main_path, machine_id, renderers[machine_id], note)
            for note in NOTES
        ]
        sets = [set(vector["fields"]) for vector in vectors]
        owned = sorted(set.union(*sets))
        always = sorted(set.intersection(*sets))
        variant = []
        fields = []
        for index in owned:
            values = [vector["fields"].get(index, {}).get("value") for vector in vectors]
            write_pcs = sorted({
                pc
                for vector in vectors
                for pc in vector["fields"].get(index, {}).get("pcs", [])
            })
            if len(set(values)) > 1:
                variant.append(index)
                variant_owners[index].append(machine_id)
            owners[index].append(machine_id)
            fields.append({
                "index": index,
                "source_halfword": f"0x{CONTROL_SOURCE_BASE + 2 * index:08X}",
                "values_by_note": {
                    str(note): None if value is None else f"0x{value:04X}"
                    for note, value in zip(NOTES, values)
                },
                "write_pcs": [f"0x{pc:08X}" for pc in write_pcs],
            })
        machines.append({
            "machine_id": machine_id,
            "machine_name": machine_name,
            "renderer": f"0x{renderers[machine_id]:08X}",
            "owned_indices": owned,
            "owned_ranges": ranges(owned),
            "always_written_indices": always,
            "note_variant_indices": variant,
            "fields": fields,
            "vectors": [{
                "note": vector["note"],
                "renderer_write_events": vector["renderer_write_events"],
                "field_count": len(vector["fields"]),
            } for vector in vectors],
        })

    owned_indices = sorted(owners)
    universal = sorted(index for index, machine_ids in owners.items() if len(machine_ids) == len(PUBLIC_MACHINE_NAMES))
    machine_specific = sorted(index for index, machine_ids in owners.items() if len(machine_ids) < len(PUBLIC_MACHINE_NAMES))
    if 46 not in owners or 47 not in owners:
        raise ValueError("known renderer pitch fields 46/47 were not observed")
    if owners[46][:2] != [0, 1] or owners[47][:2] != [0, 1]:
        raise ValueError("BD Hard/Classic pitch-field ownership changed")

    ownership_rows = [{
        "index": index,
        "source_halfword": f"0x{CONTROL_SOURCE_BASE + 2 * index:08X}",
        "machine_ids": owners[index],
        "machine_count": len(owners[index]),
        "note_variant_machine_ids": variant_owners.get(index, []),
    } for index in owned_indices]
    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "scope": {
            "public_machine_ids": [0, len(PUBLIC_MACHINE_NAMES) - 1],
            "notes": list(NOTES),
            "renderer_table": f"0x{RENDERER_TABLE:08X}",
            "dspi1_payload_halfword_base": f"0x{CONTROL_SOURCE_BASE:08X}",
            "dspi1_payload_words": PACKET_WORDS,
            "packet_source_offset": PACKET_SOURCE_OFFSET,
        },
        "summary": {
            "renderer_owned_indices": owned_indices,
            "renderer_owned_ranges": ranges(owned_indices),
            "universal_renderer_owned_indices": universal,
            "machine_specific_renderer_owned_indices": machine_specific,
            "unowned_indices": [index for index in range(PACKET_WORDS) if index not in owners],
        },
        "ownership": ownership_rows,
        "machines": machines,
        "filter2_transport_rule": (
            "Reject any proposed shared Filter 2 DSPI1 field that appears in the "
            "machine-specific renderer-owned set. Unowned indices remain only candidates; "
            "common callback and packetizer ownership must still be excluded separately."
        ),
        "next_target": (
            "Subtract common callback/packetizer writes from the unowned set and classify "
            "the remaining stable DSPI1 payload fields across non-note renderer states."
        ),
        "safety": "Stock execution with synthetic renderer-index selection only; no firmware bytes were modified.",
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
