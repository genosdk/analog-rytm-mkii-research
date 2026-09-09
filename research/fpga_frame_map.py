#!/usr/bin/env python3
"""Map AR MKII OS 1.72 XC3S200A FDRI blocks to Project Combine frame coordinates.

Read-only. The map is reconstructed from Project Combine's Spartan-3A
`fill_frame_info` ordering plus its XC3S200A / CHIP17 column geometry.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

EXPECTED_SHA256 = "7c8bff3cb411ed93434b3a4eab846738be6241d64aeb23b4061eb11a8aa29b2f"
FDRI_OFFSET = 124
FDRI_WORDS = 74658
FRAME_WORDS = 138
FRAME_BYTES = FRAME_WORDS * 2
PHYSICAL_FRAMES = 540

# Project Combine CHIP17 / XC3S200A columns. X3/X19 are BRAM columns;
# X4..X6 and X20..X22 are BRAM continuation columns and share their BRAM
# column's type-2 main frame allocation rather than owning a type-0 major.
NON_BRAM_COLUMNS = [0, 1, 2, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 23, 24, 25]
BRAM_COLUMNS = [3, 19]


def frame_map() -> list[dict]:
    out: list[dict] = []

    def add(count: int, typ: int, major: int, role: str, column: int | str | None):
        for minor in range(count):
            out.append({
                "frame_index": len(out),
                "type": typ,
                "region": 0,
                "major": major,
                "minor": minor,
                "role": role,
                "column": column,
            })

    # public/virtex2/src/expand.rs::fill_frame_info for Spartan3A:
    # 4 spine frames (because cols_clkv exists), 2 west term frames,
    # 19 main frames for every non-BRAM/non-BRAM-continuation column,
    # 2 east term frames, then type-1 BRAM data and type-2 BRAM main.
    add(4, 0, 0, "spine", None)
    add(2, 0, 1, "term_w", "W")
    major = 2
    for col in NON_BRAM_COLUMNS:
        role = "io" if col in (0, 25) else "logic"
        add(19, 0, major, role, col)
        major += 1
    add(2, 0, major, "term_e", "E")

    for major, col in enumerate(BRAM_COLUMNS):
        add(76, 1, major, "bram_data", col)
    for major, col in enumerate(BRAM_COLUMNS):
        add(19, 2, major, "bram_main", col)

    if len(out) != PHYSICAL_FRAMES:
        raise AssertionError(f"frame map length {len(out)} != {PHYSICAL_FRAMES}")
    return out


def probe(path: Path) -> dict:
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != EXPECTED_SHA256:
        raise ValueError(f"unexpected FPGA SHA-256: {digest}")

    payload = data[FDRI_OFFSET:FDRI_OFFSET + FDRI_WORDS * 2]
    if len(payload) != FDRI_WORDS * 2:
        raise ValueError("truncated FDRI payload")
    blocks = [payload[i:i + FRAME_BYTES] for i in range(0, len(payload), FRAME_BYTES)]
    if len(blocks) != 541:
        raise ValueError(f"expected 541 transmitted frame blocks, got {len(blocks)}")
    if any(blocks[-1]):
        raise ValueError("expected final transmitted pad frame to be all zero")

    mapped = frame_map()
    for entry, block in zip(mapped, blocks[:PHYSICAL_FRAMES]):
        entry["population_count"] = sum(b.bit_count() for b in block)
        entry["nonzero_bytes"] = sum(b != 0 for b in block)
        entry["all_zero"] = not any(block)
        entry["sha256"] = hashlib.sha256(block).hexdigest()

    role_stats = {}
    by_role: dict[str, list[dict]] = defaultdict(list)
    for e in mapped:
        by_role[e["role"]].append(e)
    for role, items in by_role.items():
        role_stats[role] = {
            "frames": len(items),
            "population_count": sum(x["population_count"] for x in items),
            "zero_frames": sum(x["all_zero"] for x in items),
            "max_frame_population": max(x["population_count"] for x in items),
        }

    type_stats = {}
    for typ in (0, 1, 2):
        items = [x for x in mapped if x["type"] == typ]
        type_stats[str(typ)] = {
            "frames": len(items),
            "population_count": sum(x["population_count"] for x in items),
            "zero_frames": sum(x["all_zero"] for x in items),
        }

    column_stats = []
    for col in range(26):
        items = [x for x in mapped if x["column"] == col]
        if not items:
            continue
        column_stats.append({
            "column": col,
            "frames": len(items),
            "types": sorted(set(x["type"] for x in items)),
            "population_count": sum(x["population_count"] for x in items),
            "zero_frames": sum(x["all_zero"] for x in items),
        })

    return {
        "result": "PASS",
        "fpga": {"path": str(path), "sha256": digest},
        "device": {
            "part": "XC3S200A Spartan-3A",
            "project_combine_chip": "CHIP17",
            "columns": 26,
            "rows": 34,
            "frame_bits": 2208,
            "frame_words_16bit": 138,
            "physical_frames": PHYSICAL_FRAMES,
            "transmitted_frame_blocks": 541,
            "trailing_pad_frame_index": 540,
        },
        "frame_allocation": {
            "type0_logic_routing": 350,
            "type1_bram_data": 152,
            "type2_bram_main": 38,
            "total": 540,
            "ordering": [
                "type0 major0: spine (4)",
                "type0 major1: west termination (2)",
                "type0 majors2..19: 18 non-BRAM columns x19",
                "type0 major20: east termination (2)",
                "type1 majors0..1: BRAM columns X3/X19 x76",
                "type2 majors0..1: BRAM columns X3/X19 x19",
            ],
        },
        "type_stats": type_stats,
        "role_stats": role_stats,
        "column_stats": column_stats,
        "frames": mapped,
        "pad_frame": {
            "index": 540,
            "all_zero": True,
            "sha256": hashlib.sha256(blocks[-1]).hexdigest(),
        },
        "interpretation": (
            "The Elektron FDRI payload can now be indexed in the same type/major/minor "
            "coordinate system used by Project Combine for XC3S200A. This does not yet "
            "decode LUT/PIP semantics; it removes the frame-order ambiguity required for that step."
        ),
        "safety": "Read-only frame mapping; no FPGA bits are modified.",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("fpga_image", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--csv", type=Path)
    args = ap.parse_args()
    result = probe(args.fpga_image)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.json:
        args.json.write_text(encoded, encoding="utf-8")
    if args.csv:
        rows = result["frames"]
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader(); w.writerows(rows)
    print(encoded, end="")


if __name__ == "__main__":
    main()
