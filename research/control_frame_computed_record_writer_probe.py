#!/usr/bin/env python3
"""Quarantine DSPI1 candidates inside a stock computed-address record array."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256
from control_frame_canary_persistence_probe import source_address
from machine_pitch_calibration_probe import MAIN_BASE
from renderer_control_ownership_probe import compact_ranges


PACKET_BASE = 0x800063C0
RECORD_BYTES = 8
FIRST_RECORD = 21
RECORD_COUNT = 56
MEMBER_OFFSET = 4
PAIR_ARRAY_BASE = 0x80006658
PAIR_SLOT_BYTES = 4
PAIR_SLOT_COUNT = 80


def at(image: bytes, address: int, expected: bytes, label: str) -> None:
    offset = address - MAIN_BASE
    actual = image[offset:offset + len(expected)]
    if actual != expected:
        raise ValueError(
            f"{label}: signature mismatch at 0x{address:08X}: "
            f"expected {expected.hex()}, got {actual.hex()}"
        )


def expand_ranges(rows: list[dict]) -> list[int]:
    return [
        word
        for row in rows
        for word in range(row["first_word"], row["last_word"] + 1)
    ]


def occurrences(image: bytes, needle: bytes) -> list[int]:
    result = []
    start = 0
    while True:
        offset = image.find(needle, start)
        if offset < 0:
            return result
        result.append(offset)
        start = offset + 1


def probe(main_path: Path, static_report_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    previous = json.loads(static_report_path.read_text(encoding="utf-8"))
    if previous["result"] != "PASS" or previous["main"]["sha256"] != digest:
        raise ValueError("static-writer report does not match stock MAIN")

    # The constructor first zeroes the complete 492-halfword payload buffer.
    at(
        image,
        0x4011A3AA,
        bytes.fromhex(
            "2f0a2f02243c800063c0487803d842a72f024eb940095c34"
        ),
        "492-word packet-source zero initialization",
    )

    # After packet publication, stock walks 56 eight-byte records. The member
    # address is PACKET_BASE + ((counter + 21) << 3) + 4 and is cleared with a
    # computed indexed MOVE.B at 0x4011CD38. This is precisely the structure
    # that surrounds the surviving 281..308 candidate cluster.
    at(
        image,
        0x4011CD20,
        bytes.fromhex(
            "428041f9800063c02200068100000015e78942025280763811821804b68066e8"
        ),
        "computed 56-record member-clear loop",
    )

    candidates = expand_ranges(previous["remaining"]["ranges"])
    candidate_set = set(candidates)
    records = []
    quarantined = set()
    for counter in range(RECORD_COUNT):
        record_index = FIRST_RECORD + counter
        record_start = PACKET_BASE + record_index * RECORD_BYTES
        member_address = record_start + MEMBER_OFFSET
        record_words = [
            word
            for word in candidates
            if record_start <= source_address(word) < record_start + RECORD_BYTES
        ]
        if not record_words:
            continue
        quarantined.update(record_words)
        records.append({
            "loop_counter": counter,
            "record_index": record_index,
            "record_start": f"0x{record_start:08X}",
            "record_end": f"0x{record_start + RECORD_BYTES - 1:08X}",
            "computed_member_address": f"0x{member_address:08X}",
            "candidate_words_quarantined": record_words,
        })

    after_records = sorted(candidate_set - quarantined)
    if (len(candidates), len(records), len(quarantined), len(after_records)) != (
        76, 15, 37, 39,
    ):
        raise ValueError("computed-record structural inventory changed")
    if [row["record_index"] for row in records] != list(range(37, 45)) + list(range(70, 77)):
        raise ValueError("unexpected computed-record candidate intersections")

    # A second constructor structure is an 80-entry array of paired halfwords.
    # A2 is fixed at the first slot; stock initializes the first halfword of
    # every four-byte slot to 0x4000. All 39 fields left above are the companion
    # halfwords of those explicitly managed slots.
    at(
        image,
        0x4011A3C2,
        bytes.fromhex("45f980006658"),
        "paired-slot array base",
    )
    at(
        image,
        0x4011A886,
        bytes.fromhex("34bc4000"),
        "paired-slot zero index initialization",
    )
    slot_writers = [{
        "slot_index": 0,
        "instruction": "0x4011A886",
        "initialized_halfword": f"0x{PAIR_ARRAY_BASE:08X}",
        "companion_halfword": f"0x{PAIR_ARRAY_BASE + 2:08X}",
    }]
    for slot_index in range(1, PAIR_SLOT_COUNT):
        address = PAIR_ARRAY_BASE + PAIR_SLOT_BYTES * slot_index
        needle = bytes.fromhex("33c1") + address.to_bytes(4, "big")
        hits = [MAIN_BASE + offset for offset in occurrences(image, needle)]
        initializer_hits = [pc for pc in hits if 0x4011A880 <= pc < 0x4011AA66]
        if len(initializer_hits) != 1:
            raise ValueError(
                f"paired-slot {slot_index} initializer changed: {initializer_hits}"
            )
        slot_writers.append({
            "slot_index": slot_index,
            "instruction": f"0x{initializer_hits[0]:08X}",
            "initialized_halfword": f"0x{address:08X}",
            "companion_halfword": f"0x{address + 2:08X}",
        })

    companion_addresses = {
        PAIR_ARRAY_BASE + PAIR_SLOT_BYTES * slot_index + 2
        for slot_index in range(PAIR_SLOT_COUNT)
    }
    paired_slot_candidates = {
        word for word in after_records if source_address(word) in companion_addresses
    }
    remaining = sorted(set(after_records) - paired_slot_candidates)
    remaining_set = set(remaining)
    pairs = [[word, word + 1] for word in remaining if word + 1 in remaining_set]
    if (len(paired_slot_candidates), len(remaining), len(pairs)) != (39, 0, 0):
        raise ValueError("paired-slot structural inventory changed")

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "input": {
            "post_absolute_move_candidate_fields": len(candidates),
            "report": str(static_report_path),
        },
        "packet_source_initialization": {
            "routine": "0x4011A3AA",
            "base": f"0x{PACKET_BASE:08X}",
            "zeroed_bytes": 0x3D8,
            "zeroed_halfwords": 492,
        },
        "computed_record_writer": {
            "loop": "0x4011CD20..0x4011CD40",
            "store_instruction": "0x4011CD38",
            "store": "MOVE.B D2,4(A0,D1.L)",
            "address_equation": (
                "0x800063C0 + 8 * (21 + counter) + 4, counter = 0..55"
            ),
            "record_bytes": RECORD_BYTES,
            "member_offset": MEMBER_OFFSET,
            "record_count": RECORD_COUNT,
            "candidate_intersecting_record_count": len(records),
            "candidate_fields_quarantined": len(quarantined),
            "candidate_ranges_quarantined": compact_ranges(sorted(quarantined)),
            "records": records,
        },
        "paired_slot_initializer": {
            "base_load": "0x4011A3C2",
            "initializer": "0x4011A886..0x4011AA60",
            "slot_bytes": PAIR_SLOT_BYTES,
            "slot_count": PAIR_SLOT_COUNT,
            "initialized_member_offset": 0,
            "companion_member_offset": 2,
            "candidate_companion_fields_quarantined": len(paired_slot_candidates),
            "candidate_ranges_quarantined": compact_ranges(
                sorted(paired_slot_candidates)
            ),
            "slot_writers": slot_writers,
        },
        "remaining": {
            "field_count": len(remaining),
            "ranges": compact_ranges(remaining),
            "adjacent_pair_count": len(pairs),
            "pairs": pairs,
        },
        "conclusion": (
            "The apparent 281..308 gap is seven members of a stock-managed "
            "56-by-8-byte record array, not an unstructured payload hole. Together "
            "with sixteen earlier candidates in the same array, 37 fields are "
            "conservatively quarantined. The last 39 isolated candidates are companion "
            "halfwords in a separate stock-initialized 80-by-4-byte slot array. Under "
            "the conservative structure rule, no candidate field survives."
        ),
        "next_target": (
            "Do not spend another locality sweep on words 281/282 and do not claim a "
            "spare adjacent pair. Continue through named stock control destinations or "
            "an explicit versioned transport extension instead."
        ),
        "safety": "Static read-only stock MAIN analysis; no firmware was modified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("main_image", type=Path)
    parser.add_argument("static_writer_report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = probe(args.main_image, args.static_writer_report)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
