#!/usr/bin/env python3
"""Trace pre-callback stock writers of persistent DSPI1 control fields."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256, prepared_machine
from br_hardware_sink_probe import CONTROL_DMA_INITIALIZER
from note_event_constructor_probe import (
    EVENT_INPUT,
    NOTE_EVENT_CONSTRUCTOR,
    NOTE_ON,
    NOTE_OFF,
    QWERTY_SOURCE_MASK,
    write_event,
)
from note_pitch_consumer_boundary_probe import PITCH_SELECT_SOURCE, load_emulator
from renderer_control_ownership_probe import (
    PACKET_SOURCE_END,
    PACKET_SOURCE_FIRST,
    PACKET_SOURCE_HALFWORDS,
    compact_ranges,
    packet_index,
    touched_fields,
)
from trigger_queue_probe import (
    CONTROL_SNAPSHOT_A,
    CONTROL_SNAPSHOT_POINTER,
    QUEUE,
    QUEUE_CAPACITY,
    QUEUE_INITIALIZER,
    QUEUE_INSTALLER,
    run_complete_callback,
    stock_call,
)

NOTE = 60
PHASES = (
    "machine_preparation",
    "control_dma_initializer",
    "queue_initializer",
    "queue_installer",
    "note_on_constructor",
    "note_off_constructor",
)


def trace_setup(module, main_path: Path) -> dict:
    original_step = module.CPU.step
    original_write = module.Bus.write
    cpus: dict[int, object] = {}
    phase = "machine_preparation"
    events: list[tuple[str, int, int, int]] = []

    def write(bus, address: int, size: int, value: int) -> None:
        cpu = cpus.get(id(bus))
        if cpu is not None and phase in PHASES and touched_fields(address, size):
            events.append((phase, cpu.pc, address, size))
        original_write(bus, address, size, value)

    def step(cpu):
        cpus[id(cpu.bus)] = cpu
        return original_step(cpu)

    module.Bus.write = write
    module.CPU.step = step
    bus, cpu, _ = prepared_machine(module, main_path)
    callback_sp = cpu.a[7]

    phase = "control_dma_initializer"
    stock_call(cpu, CONTROL_DMA_INITIALIZER, [])
    phase = "queue_initializer"
    stock_call(cpu, QUEUE_INITIALIZER, [QUEUE, 0, 0x419531F8, QUEUE_CAPACITY])
    phase = "queue_installer"
    stock_call(cpu, QUEUE_INSTALLER, [QUEUE])
    bus.write(CONTROL_SNAPSHOT_POINTER, 4, CONTROL_SNAPSHOT_A)
    bus.write(PITCH_SELECT_SOURCE, 1, 1)
    write_event(bus, event_type=NOTE_ON, note=NOTE, source_mask=QWERTY_SOURCE_MASK)
    phase = "note_on_constructor"
    stock_call(cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])
    phase = "callback_ignored"
    run_complete_callback(cpu, callback_sp)
    write_event(bus, event_type=NOTE_OFF, note=NOTE, source_mask=QWERTY_SOURCE_MASK)
    phase = "note_off_constructor"
    stock_call(cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])

    fields_by_phase: dict[str, set[int]] = defaultdict(set)
    pcs_by_phase: dict[str, set[int]] = defaultdict(set)
    for event_phase, pc, address, size in events:
        pcs_by_phase[event_phase].add(pc)
        fields_by_phase[event_phase].update(touched_fields(address, size))
    return {
        "event_count": len(events),
        "fields": set().union(*fields_by_phase.values()),
        "phases": [
            {
                "phase": item,
                "write_event_count": sum(1 for event in events if event[0] == item),
                "field_count": len(fields_by_phase[item]),
                "fields": sorted(fields_by_phase[item]),
                "pcs": sorted(pcs_by_phase[item]),
            }
            for item in PHASES
        ],
    }


def probe(main_path: Path, emulator_path: Path, callback_report_path: Path) -> dict:
    digest = hashlib.sha256(main_path.read_bytes()).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    callback_report = json.loads(callback_report_path.read_text(encoding="utf-8"))
    if callback_report["result"] != "PASS" or callback_report["main"]["sha256"] != digest:
        raise ValueError("whole-callback report does not match stock MAIN")
    if callback_report["packet_source"]["halfwords"] != PACKET_SOURCE_HALFWORDS:
        raise ValueError("whole-callback report has incompatible packet geometry")

    traced = trace_setup(load_emulator(emulator_path), main_path)
    callback_unwritten = {
        int(row["address"], 16)
        for row in callback_report["read_but_never_callback_written"]
    }
    setup_written = traced["fields"]
    resolved = setup_written & callback_unwritten
    remaining = callback_unwritten - setup_written
    packet_fields = {PACKET_SOURCE_FIRST + 2 * i for i in range(PACKET_SOURCE_HALFWORDS)}
    if not setup_written <= packet_fields:
        raise ValueError("setup trace escaped packet geometry")
    if traced["event_count"] != 0 or setup_written or resolved or len(remaining) != 165:
        raise ValueError("pre-callback/event-constructor ownership result changed")

    def public_phase(row: dict) -> dict:
        return {
            **{key: value for key, value in row.items() if key != "fields"},
            "field_ranges": compact_ranges([packet_index(field) for field in row["fields"]]),
            "pcs": [f"0x{pc:08X}" for pc in row["pcs"]],
        }

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "packet_source": {
            "first_halfword": f"0x{PACKET_SOURCE_FIRST:08X}",
            "last_halfword": f"0x{PACKET_SOURCE_END - 2:08X}",
            "halfwords": PACKET_SOURCE_HALFWORDS,
        },
        "setup": {
            "note": NOTE,
            "write_event_count": traced["event_count"],
            "written_field_count": len(setup_written),
            "written_ranges": compact_ranges(
                [packet_index(field) for field in sorted(setup_written)]
            ),
            "phases": [public_phase(row) for row in traced["phases"]],
        },
        "candidate_rejection": {
            "callback_unwritten_field_count": len(callback_unwritten),
            "setup_written_callback_candidate_count": len(resolved),
            "remaining_unobserved_field_count": len(remaining),
            "remaining_unobserved_ranges": compact_ranges(
                [packet_index(field) for field in sorted(remaining)]
            ),
        },
        "conclusion": (
            "Authentic stock preparation plus note-on and note-off constructors write none "
            "of the packet-source fields. All callback-unwritten fields therefore survive "
            "this gate, but remain unobserved candidates rather than proven spare capacity."
        ),
        "next_target": (
            "Trace parameter-change and other non-note event paths plus operating modes that "
            "can populate the remaining unobserved fields."
        ),
        "safety": "Stock firmware execution and synthetic RAM input only; no firmware was modified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("main_image", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument("whole_callback_report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = probe(args.main_image, args.emulator, args.whole_callback_report)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
