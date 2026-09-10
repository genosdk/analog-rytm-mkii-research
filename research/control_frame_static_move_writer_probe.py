#!/usr/bin/env python3
"""Reject eight-lane candidates with explicit stock absolute word writers."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256
from control_frame_canary_persistence_probe import source_address
from machine_pitch_calibration_probe import MAIN_BASE
from renderer_control_ownership_probe import compact_ranges

KNOWN_CONTROL_WORDS = (46, 47, 195, 196)


def occurrences(image: bytes, needle: bytes) -> list[int]:
    result = []
    start = 0
    while True:
        offset = image.find(needle, start)
        if offset < 0:
            return result
        result.append(offset)
        start = offset + 1


def probe(main_path: Path, eight_lane_report_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    ownership = json.loads(eight_lane_report_path.read_text(encoding="utf-8"))
    if ownership["result"] != "PASS" or ownership["main"]["sha256"] != digest:
        raise ValueError("eight-lane report does not match stock MAIN")
    candidates = [
        word
        for row in ownership["ownership"]["writer_free_ranges"]
        for word in range(row["first_word"], row["last_word"] + 1)
    ]

    rejected = []
    for word in candidates:
        field = source_address(word)
        writers = []
        for offset in occurrences(image, field.to_bytes(4, "big")):
            if offset < 2:
                continue
            opcode = int.from_bytes(image[offset - 2:offset], "big")
            # 0x33C0..0x33C7: MOVE.W D0..D7,(absolute-long address).
            if opcode & 0xFFF8 == 0x33C0:
                writers.append({
                    "instruction": f"0x{MAIN_BASE + offset - 2:08X}",
                    "opcode": f"0x{opcode:04X}",
                    "source_register": f"D{opcode & 7}",
                })
        if writers:
            rejected.append({
                "word": word,
                "address": f"0x{field:08X}",
                "writer_count": len(writers),
                "writers": writers,
            })

    rejected_words = {row["word"] for row in rejected}
    remaining = sorted(set(candidates) - rejected_words)
    remaining_set = set(remaining)
    pairs = [
        {
            "words": [word, word + 1],
            "addresses": [
                f"0x{source_address(word):08X}",
                f"0x{source_address(word + 1):08X}",
            ],
            "nearest_known_control_distance_words": min(
                abs(word - control) for control in KNOWN_CONTROL_WORDS
            ),
        }
        for word in remaining
        if word + 1 in remaining_set
    ]
    pairs.sort(key=lambda row: (
        row["nearest_known_control_distance_words"], row["words"][0]
    ))
    if (len(candidates), len(rejected), sum(row["writer_count"] for row in rejected),
            len(remaining), len(pairs)) != (124, 48, 123, 76, 13):
        raise ValueError("static absolute MOVE.W rejection inventory changed")
    if not pairs or pairs[0]["words"] != [281, 282]:
        raise ValueError("post-rejection candidate ranking changed")

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "input": {
            "eight_lane_writer_unobserved_fields": len(candidates),
            "report": str(eight_lane_report_path),
        },
        "direct_absolute_word_writers": {
            "encoding": "0x33C0..0x33C7 = MOVE.W Dn,(absolute-long address)",
            "rejected_field_count": len(rejected),
            "writer_instruction_count": sum(row["writer_count"] for row in rejected),
            "fields": rejected,
        },
        "remaining": {
            "field_count": len(remaining),
            "ranges": compact_ranges(remaining),
            "adjacent_pair_count": len(pairs),
            "ranked_winner": pairs[0],
            "pairs": pairs,
        },
        "conclusion": (
            "Forty-eight callback-unobserved fields have explicit stock absolute word "
            "writers and are rejected, including words 67/68. Seventy-six fields survive "
            "this specific static writer class; absence of this opcode is not proof of no writer."
        ),
        "next_target": (
            "Classify additional absolute and base-relative writer encodings around the "
            "remaining words, beginning with ranked pair 281/282."
        ),
        "safety": "Static read-only stock MAIN analysis; no firmware was modified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("main_image", type=Path)
    parser.add_argument("eight_lane_report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = probe(args.main_image, args.eight_lane_report)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
