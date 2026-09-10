#!/usr/bin/env python3
"""Inventory all stock writers of the 492 transmitted DSPI1 payload fields.

The experiment executes every public machine renderer with event state values
0..4, but records writes for the entire audio callback rather than only during
renderer execution.  It separates renderer and non-renderer ownership, then
intersects the latter with the fields left untouched by the renderer sweep.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import struct

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256
from machine_pitch_calibration_probe import (
    DISPATCH_INDEX_PC,
    MAIN_BASE,
    PUBLIC_MACHINE_NAMES,
    RENDERER_COUNT,
    RENDERER_TABLE,
)
from note_pitch_consumer_boundary_probe import load_emulator, run_vector
from trigger_queue_probe import FULL_CALLBACK_STOP

FORCED_STATES = tuple(range(5))
NOTE = 60
VOICE_EVENT_STATE = 0x8000FEF8
SOURCE_FIRST = 0x800063C0
PAYLOAD_HALFWORDS = 492
SOURCE_END = SOURCE_FIRST + 2 * PAYLOAD_HALFWORDS
EXPECTED_MATRIX_SHA256 = "2f6dc283256fa2b5a352405174ee4ea508d722a562fafaf61f22641b57b68caf"


def touched_fields(address: int, size: int) -> list[int]:
    first = max(address, SOURCE_FIRST)
    last = min(address + size, SOURCE_END)
    if first >= last:
        return []
    first &= ~1
    return list(range(first, last, 2))


def packet_word(address: int) -> int:
    if address & 1 or not SOURCE_FIRST <= address < SOURCE_END:
        raise ValueError(f"not a transmitted source halfword: 0x{address:08X}")
    return 1 + (address - SOURCE_FIRST) // 2


def compact_ranges(fields: list[int]) -> list[dict]:
    ranges: list[list[int]] = []
    for field in sorted(fields):
        word = packet_word(field)
        if not ranges or word != ranges[-1][1] + 1:
            ranges.append([word, word])
        else:
            ranges[-1][1] = word
    return [
        {
            "first_word": first,
            "last_word": last,
            "first_address": f"0x{SOURCE_FIRST + 2 * (first - 1):08X}",
            "last_address": f"0x{SOURCE_FIRST + 2 * (last - 1):08X}",
        }
        for first, last in ranges
    ]


def run_context(
    module,
    main_path: Path,
    machine_id: int,
    renderer: int,
    forced_state: int,
) -> dict:
    original_step = module.CPU.step
    original_write = module.Bus.write
    states: dict[int, dict] = {}
    cpus: dict[int, object] = {}
    events: list[tuple[str, int, int, int, int]] = []
    visits = 0

    def write(bus, address: int, size: int, value: int) -> None:
        state = states.get(id(bus), {})
        cpu = cpus.get(id(bus))
        fields = touched_fields(address, size)
        if state.get("callback") and fields:
            phase = "renderer" if state.get("renderer") else "non_renderer"
            events.append(
                (phase, cpu.pc, address, size, value & ((1 << (8 * size)) - 1))
            )
        original_write(bus, address, size, value)

    def step(cpu):
        nonlocal visits
        cpus[id(cpu.bus)] = cpu
        state = states.setdefault(
            id(cpu.bus),
            {"callback": False, "renderer": False, "return": None},
        )
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

    module.Bus.write = write
    module.CPU.step = step
    run_vector(module, main_path, NOTE, 1, callback_count=1)
    if visits != 1:
        raise ValueError(
            f"machine {machine_id} state {forced_state} renderer visits: {visits}"
        )

    phase_fields: dict[str, set[int]] = defaultdict(set)
    phase_pcs: dict[str, set[int]] = defaultdict(set)
    for phase, pc, address, size, _value in events:
        phase_pcs[phase].add(pc)
        phase_fields[phase].update(touched_fields(address, size))
    return {
        "machine_id": machine_id,
        "state": forced_state,
        "renderer_write_events": sum(event[0] == "renderer" for event in events),
        "non_renderer_write_events": sum(
            event[0] == "non_renderer" for event in events
        ),
        "renderer_fields": sorted(phase_fields["renderer"]),
        "non_renderer_fields": sorted(phase_fields["non_renderer"]),
        "renderer_pcs": sorted(phase_pcs["renderer"]),
        "non_renderer_pcs": sorted(phase_pcs["non_renderer"]),
        "events": events,
    }


def field_rows(
    fields: set[int],
    contexts: list[dict],
    phase: str,
) -> list[dict]:
    rows = []
    field_key = f"{phase}_fields"
    for field in sorted(fields):
        owning = [
            context for context in contexts if field in context[field_key]
        ]
        pcs = sorted({
            pc
            for context in owning
            for event_phase, pc, address, size, _value in context["events"]
            if event_phase == phase and field in touched_fields(address, size)
        })
        rows.append({
            "address": f"0x{field:08X}",
            "dspi1_word_index": packet_word(field),
            "context_count": len(owning),
            "machine_ids": sorted({row["machine_id"] for row in owning}),
            "states": sorted({row["state"] for row in owning}),
            "writer_pcs": [f"0x{pc:08X}" for pc in pcs],
        })
    return rows


def writer_families(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for row in rows:
        grouped[tuple(row["writer_pcs"])].append(row["dspi1_word_index"])
    families = []
    for pcs, words in sorted(grouped.items(), key=lambda item: min(item[1])):
        ranges: list[list[int]] = []
        for word in sorted(words):
            if not ranges or word != ranges[-1][1] + 1:
                ranges.append([word, word])
            else:
                ranges[-1][1] = word
        families.append({
            "writer_pcs": list(pcs),
            "field_count": len(words),
            "word_ranges": ranges,
        })
    return families


def probe(main_path: Path, emulator_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    table_offset = RENDERER_TABLE - MAIN_BASE
    renderers = [
        struct.unpack(
            ">I", image[table_offset + 4 * index:table_offset + 4 * index + 4]
        )[0]
        for index in range(RENDERER_COUNT)
    ]

    contexts = []
    renderer_owners: set[int] = set()
    non_renderer_owners: set[int] = set()
    for machine_id, _name in enumerate(PUBLIC_MACHINE_NAMES):
        for forced_state in FORCED_STATES:
            module = load_emulator(emulator_path)
            context = run_context(
                module,
                main_path,
                machine_id,
                renderers[machine_id],
                forced_state,
            )
            contexts.append(context)
            renderer_owners.update(context["renderer_fields"])
            non_renderer_owners.update(context["non_renderer_fields"])

    universe = set(range(SOURCE_FIRST, SOURCE_END, 2))
    renderer_candidates = universe - renderer_owners
    candidates_rejected_by_non_renderer = renderer_candidates & non_renderer_owners
    whole_callback_owners = renderer_owners | non_renderer_owners
    whole_callback_unobserved = universe - whole_callback_owners
    digest_rows = [
        {
            key: value
            for key, value in context.items()
            if key != "events"
        }
        for context in contexts
    ]
    matrix_digest = hashlib.sha256(
        json.dumps(digest_rows, sort_keys=True).encode()
    ).hexdigest()
    non_renderer_rows = field_rows(non_renderer_owners, contexts, "non_renderer")
    rejected_rows = field_rows(
        candidates_rejected_by_non_renderer,
        contexts,
        "non_renderer",
    )

    expected_counts = (170, 117, 375, 233, 210, 327, 165)
    observed_counts = (
        len(contexts),
        len(renderer_owners),
        len(renderer_candidates),
        len(non_renderer_owners),
        len(candidates_rejected_by_non_renderer),
        len(whole_callback_owners),
        len(whole_callback_unobserved),
    )
    if observed_counts != expected_counts:
        raise ValueError(f"ownership coverage changed: {observed_counts}")
    if matrix_digest != EXPECTED_MATRIX_SHA256:
        raise ValueError(f"whole-callback matrix changed: {matrix_digest}")
    if packet_word(SOURCE_END - 2) != 492:
        raise ValueError("payload boundary changed")

    return {
        "result": "PASS",
        "main": {"sha256": digest},
        "coverage": {
            "public_machines": len(PUBLIC_MACHINE_NAMES),
            "forced_states": list(FORCED_STATES),
            "contexts": len(contexts),
            "note": NOTE,
            "matrix_sha256": matrix_digest,
        },
        "transmitted_source": {
            "first_halfword": f"0x{SOURCE_FIRST:08X}",
            "last_halfword": f"0x{SOURCE_END - 2:08X}",
            "halfwords": PAYLOAD_HALFWORDS,
            "dspi1_word_indices": [1, 492],
            "mapping": "word = 1 + (address - 0x800063C0) / 2",
            "boundary_correction": (
                "0x80006798 would map to queue index 493, but index 493 is the "
                "fixed 0x5555 end marker rather than an asserted PCS0 payload word."
            ),
        },
        "ownership_summary": {
            "renderer_owned_fields": len(renderer_owners),
            "renderer_unobserved_candidates": len(renderer_candidates),
            "candidate_fields_written_outside_renderer": len(
                candidates_rejected_by_non_renderer
            ),
            "whole_callback_owned_fields": len(whole_callback_owners),
            "whole_callback_unobserved_fields": len(whole_callback_unobserved),
            "whole_callback_unobserved_ranges": compact_ranges(
                list(whole_callback_unobserved)
            ),
        },
        "non_renderer_ownership": {
            "field_count": len(non_renderer_owners),
            "writer_family_count": len(writer_families(non_renderer_rows)),
            "writer_families": writer_families(non_renderer_rows),
            "fields": non_renderer_rows,
        },
        "renderer_candidates_rejected_by_non_renderer": {
            "field_count": len(candidates_rejected_by_non_renderer),
            "fields": rejected_rows,
        },
        "interpretation": {
            "established": (
                "The matrix inventories renderer and non-renderer writes across "
                "the full stock callback for all 170 machine/state contexts."
            ),
            "transport_consumer": (
                "All 492 fields are serialized into asserted-PCS0 DSPI1 payload "
                "words, so every field has a stock transport consumer even when no "
                "writer is observed in this matrix."
            ),
            "limitation": (
                "A field unobserved in these callbacks is not proven electrically "
                "unused; the off-chip PCS0 interpretation remains unresolved."
            ),
        },
        "next_target": (
            "Resolve the writer families for the remaining whole-callback-unobserved "
            "ranges under initialization and project-load phases before any canary "
            "publication is considered."
        ),
        "safety": (
            "Stock firmware execution with synthetic machine/state selection only; "
            "no firmware image was modified."
        ),
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
