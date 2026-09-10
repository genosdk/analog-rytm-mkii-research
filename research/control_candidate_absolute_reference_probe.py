#!/usr/bin/env python3
"""Index exact stock-MAIN literals for callback-unwritten DSPI1 fields.

This prioritizes later dynamic traces. Exact literals can be code operands or
data and their absence does not reject base-register-relative access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256
from machine_pitch_calibration_probe import MAIN_BASE
from renderer_control_ownership_probe import compact_ranges, packet_index

EXPECTED_REFERENCED_FIELDS = 77
EXPECTED_LITERAL_REFERENCES = 255
EXPECTED_CLUSTERS = 24


def occurrences(image: bytes, needle: bytes) -> list[int]:
    result = []
    start = 0
    while True:
        offset = image.find(needle, start)
        if offset < 0:
            return result
        result.append(offset)
        start = offset + 1


def clusters(addresses: list[int], maximum_gap: int = 0x100) -> list[list[int]]:
    result: list[list[int]] = []
    for address in addresses:
        if not result or address - result[-1][-1] > maximum_gap:
            result.append([address])
        else:
            result[-1].append(address)
    return result


def probe(main_path: Path, callback_report_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    callback_report = json.loads(callback_report_path.read_text(encoding="utf-8"))
    if callback_report["result"] != "PASS" or callback_report["main"]["sha256"] != digest:
        raise ValueError("whole-callback report does not match stock MAIN")

    candidates = [
        (row["dspi1_word_index"], int(row["address"], 16))
        for row in callback_report["read_but_never_callback_written"]
    ]
    field_rows = []
    all_literals = []
    unreferenced = []
    for word, field in candidates:
        offsets = occurrences(image, field.to_bytes(4, "big"))
        if not offsets:
            unreferenced.append(field)
            continue
        references = []
        for offset in offsets:
            literal = MAIN_BASE + offset
            all_literals.append(literal)
            preceding_word = int.from_bytes(image[max(0, offset - 2):offset], "big")
            references.append({
                "literal_address": f"0x{literal:08X}",
                "preceding_word": f"0x{preceding_word:04X}",
            })
        field_rows.append({
            "address": f"0x{field:08X}",
            "dspi1_word_index": word,
            "reference_count": len(references),
            "references": references,
        })

    literal_clusters = clusters(sorted(all_literals))
    if (
        len(field_rows),
        len(all_literals),
        len(literal_clusters),
    ) != (EXPECTED_REFERENCED_FIELDS, EXPECTED_LITERAL_REFERENCES, EXPECTED_CLUSTERS):
        raise ValueError("absolute-reference inventory changed")

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "source_candidates": len(candidates),
        "absolute_reference_summary": {
            "referenced_field_count": len(field_rows),
            "unreferenced_field_count": len(unreferenced),
            "literal_reference_count": len(all_literals),
            "cluster_count": len(literal_clusters),
            "cluster_maximum_gap": "0x100",
            "clusters": [
                {
                    "first_literal": f"0x{cluster[0]:08X}",
                    "last_literal": f"0x{cluster[-1]:08X}",
                    "literal_count": len(cluster),
                }
                for cluster in literal_clusters
            ],
            "unreferenced_field_ranges": compact_ranges(
                [packet_index(field) for field in unreferenced]
            ),
        },
        "referenced_fields": field_rows,
        "interpretation": (
            "The 24 literal clusters prioritize routines or tables for dynamic tracing. "
            "A literal is not proof of a write, and 88 fields without exact literals can "
            "still be accessed through base registers or computed addresses."
        ),
        "next_target": (
            "Classify the high-density literal clusters around 0x401040B2..0x401049A0, "
            "0x40105B58..0x401063D8 and 0x4011A404..0x4011AA5C, then execute their callers."
        ),
        "safety": "Static read-only analysis of stock MAIN; no firmware was modified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("main_image", type=Path)
    parser.add_argument("whole_callback_report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = probe(args.main_image, args.whole_callback_report)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
