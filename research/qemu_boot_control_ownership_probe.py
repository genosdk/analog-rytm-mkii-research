#!/usr/bin/env python3
"""Reduce a QEMU control-buffer write trace to boot-ownership evidence."""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path

SOURCE_FIRST = 0x800063C0
PAYLOAD_HALFWORDS = 492
SOURCE_END = SOURCE_FIRST + 2 * PAYLOAD_HALFWORDS
STATIC_COPY_PC = "0x400007F6"
ZERO_FILL_PCS = {
    "0x40095C50",
    "0x40095C52",
    "0x40095C54",
    "0x40095C56",
    "0x40095C68",
}


def touched_fields(event: dict) -> set[int]:
    address = int(event["paddr"], 16)
    size = event["size"]
    first = max(address, SOURCE_FIRST)
    last = min(address + size, SOURCE_END)
    if first >= last:
        return set()
    first &= ~1
    return set(range(first, last, 2))


def packet_word(address: int) -> int:
    return 1 + (address - SOURCE_FIRST) // 2


def compact_ranges(fields: set[int]) -> list[dict]:
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


def union_fields(events: list[dict]) -> set[int]:
    result: set[int] = set()
    for event in events:
        result.update(touched_fields(event))
    return result


def load_residual_fields(ownership: dict) -> set[int]:
    result = set()
    ranges = ownership["ownership_summary"]["whole_callback_unobserved_ranges"]
    for row in ranges:
        for word in range(row["first_word"], row["last_word"] + 1):
            result.add(SOURCE_FIRST + 2 * (word - 1))
    return result


def phase(events: list[dict], start: int, predicate) -> tuple[list[dict], int]:
    end = start
    while end < len(events) and predicate(events[end]):
        end += 1
    return events[start:end], end


def writer_families(events: list[dict], selected: set[int]) -> list[dict]:
    by_pc: dict[str, set[int]] = defaultdict(set)
    for event in events:
        fields = touched_fields(event) & selected
        if fields:
            by_pc[event["pc"]].update(fields)
    return [
        {
            "pc": pc,
            "field_count": len(fields),
            "word_ranges": compact_ranges(fields),
        }
        for pc, fields in sorted(by_pc.items(), key=lambda item: (min(item[1]), item[0]))
        if fields
    ]


def probe(trace_path: Path, ownership_path: Path) -> dict:
    trace_bytes = trace_path.read_bytes()
    events = [json.loads(line) for line in trace_bytes.splitlines() if line]
    ownership_bytes = ownership_path.read_bytes()
    ownership = json.loads(ownership_bytes)
    universe = set(range(SOURCE_FIRST, SOURCE_END, 2))
    residual = load_residual_fields(ownership)
    pc_matches = all(
        event.get("translated_pc", event["pc"]) == event["pc"]
        for event in events
    )

    static_copy, cursor = phase(
        events, 0, lambda event: event["pc"] == STATIC_COPY_PC
    )
    zero_fill, cursor = phase(
        events,
        cursor,
        lambda event: event["pc"] in ZERO_FILL_PCS
        and int(event["value"], 16) == 0,
    )
    later = events[cursor:]

    static_fields = union_fields(static_copy)
    zero_fields = union_fields(zero_fill)
    later_fields = union_fields(later)
    later_residual = later_fields & residual
    residual_retaining_zero = residual - later_residual

    if len(events) != 845:
        raise ValueError(f"expected 845 trace events, got {len(events)}")
    if not pc_matches:
        raise ValueError("live and translation-time PCs disagree")
    if len(static_copy) != 246 or static_fields != universe:
        raise ValueError("static-data copy does not cover all 492 fields")
    if len(zero_fill) != 246 or zero_fields != universe:
        raise ValueError("runtime zero fill does not cover all 492 fields")
    if any(int(event["value"], 16) for event in zero_fill):
        raise ValueError("runtime fill contains a nonzero value")
    if len(later) != 353 or len(later_fields) != 292:
        raise ValueError("unexpected post-zero startup coverage")
    if len(residual) != 165 or len(later_residual) != 104:
        raise ValueError("unexpected prior-residual startup coverage")

    return {
        "result": "PASS",
        "inputs": {
            "main_sha256": ownership["main"]["sha256"],
            "trace_sha256": hashlib.sha256(trace_bytes).hexdigest(),
            "ownership_report_sha256": hashlib.sha256(ownership_bytes).hexdigest(),
        },
        "capture": {
            "trace_events": len(events),
            "live_pc_matches_translated_pc": pc_matches,
            "mock_calibration": True,
            "mock_factory_state": True,
            "ui_events": ["NO", "SMP"],
            "tracer": "qemu/ar_mk2_control_write_trace.c",
            "harness": "qemu/headless_ui_smoke.py",
        },
        "transmitted_source": {
            "first_halfword": f"0x{SOURCE_FIRST:08X}",
            "last_halfword": f"0x{SOURCE_END - 2:08X}",
            "halfwords": PAYLOAD_HALFWORDS,
        },
        "static_data_copy": {
            "store_pc": STATIC_COPY_PC,
            "instruction": "move.l (a2)+,(a1)",
            "events": len(static_copy),
            "fields": len(static_fields),
            "sequence": [static_copy[0]["sequence"], static_copy[-1]["sequence"]],
        },
        "runtime_zero_fill": {
            "routine_entry": "0x40095C32",
            "store_pcs": sorted(ZERO_FILL_PCS),
            "events": len(zero_fill),
            "fields": len(zero_fields),
            "sequence": [zero_fill[0]["sequence"], zero_fill[-1]["sequence"]],
        },
        "post_zero_startup": {
            "events": len(later),
            "fields_rewritten": len(later_fields),
            "prior_unobserved_fields_rewritten": len(later_residual),
            "prior_unobserved_fields_retaining_zero": len(residual_retaining_zero),
            "rewritten_prior_unobserved_ranges": compact_ranges(later_residual),
            "retained_zero_prior_unobserved_ranges": compact_ranges(
                residual_retaining_zero
            ),
            "prior_unobserved_writer_families": writer_families(
                later, later_residual
            ),
        },
        "ownership_conclusion": {
            "fields_initialized_by_stock_boot": len(static_fields | zero_fields),
            "fields_not_initialized_by_stock_boot": 0,
            "canary_candidates": 0,
            "reason": (
                "Stock startup initializes and then explicitly clears every "
                "transmitted halfword. Later startup code rewrites 292 fields; "
                "the other fields retain the firmware-established zero value."
            ),
        },
        "limitation": (
            "This proves stock-firmware boot ownership under the synthetic empty "
            "factory state. It does not identify the physical PCS0 receiver or "
            "assign semantics to fields that retain zero."
        ),
        "next_target": (
            "Treat the DSPI1 frame as fully stock-owned; continue through "
            "receiver/protocol inference without publishing an in-band canary."
        ),
        "safety": (
            "No firmware bytes or flashable image were modified; the trace came "
            "from stock MAIN execution and emulator-only hardware models."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--ownership", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = probe(args.trace, args.ownership)
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
