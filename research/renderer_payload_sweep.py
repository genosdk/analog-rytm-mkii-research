#!/usr/bin/env python3
"""Classify DSPI1 asserted-PCS0 payload words across stock renderers.

This emulation-only boundary experiment substitutes each of the 53 function
pointers in the stock renderer table for physical voice 0, runs one otherwise
identical note-60 callback, and compares the resulting 492-word PCS0 payload.
It does not modify the firmware image or claim that the common initialized
state is an authentic preset for every renderer.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys

EXPECTED_MAIN_SHA256 = (
    "5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772"
)
MAIN_LOAD = 0x40000400
RENDERER_TABLE = 0x40277FE8
RENDERER_COUNT = 53
VOICE0_RENDERER_POINTER = 0x419B0B84
RENDERER_DISPATCH = 0x4011CA50
PAYLOAD_FIRST_WORD = 1
PAYLOAD_WORD_COUNT = 492


def renderer_addresses(main: bytes) -> list[int]:
    offset = RENDERER_TABLE - MAIN_LOAD
    end = offset + 4 * RENDERER_COUNT
    if offset < 0 or end > len(main):
        raise ValueError("renderer table falls outside MAIN")
    return [
        int.from_bytes(main[index:index + 4], "big")
        for index in range(offset, end, 4)
    ]


def install_substitution(module, renderer: int) -> None:
    original_step = module.CPU.step
    substituted: set[int] = set()

    def step(cpu):
        if (
            cpu.pc == RENDERER_DISPATCH
            and cpu.d[2] == 0
            and id(cpu) not in substituted
        ):
            cpu.bus.write(VOICE0_RENDERER_POINTER, 4, renderer)
            substituted.add(id(cpu))
        return original_step(cpu)

    module.CPU.step = step


def payload_words(vector: dict) -> list[int]:
    words = vector["packet_words"]
    stop = PAYLOAD_FIRST_WORD + PAYLOAD_WORD_COUNT
    if len(words) != 510:
        raise ValueError(f"unexpected control queue length: {len(words)}")
    return words[PAYLOAD_FIRST_WORD:stop]


def contiguous_groups(indices: list[int]) -> list[dict]:
    groups: list[dict] = []
    for index in indices:
        if not groups or index != groups[-1]["last"] + 1:
            groups.append({"first": index, "last": index, "count": 1})
        else:
            groups[-1]["last"] = index
            groups[-1]["count"] += 1
    return groups


def probe(main_path: Path, emulator_path: Path, probe_root: Path) -> dict:
    main = main_path.read_bytes()
    digest = hashlib.sha256(main).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")

    sys.path.insert(0, str(probe_root.resolve()))
    try:
        from note_pitch_consumer_boundary_probe import load_emulator, run_vector
    finally:
        sys.path.pop(0)

    addresses = renderer_addresses(main)
    executions: list[dict] = []
    successful_payloads: dict[int, list[int]] = {}
    for index, address in enumerate(addresses):
        try:
            module = load_emulator(emulator_path)
            install_substitution(module, address)
            vector = run_vector(module, main_path, 60, 1, callback_count=1)
            payload = payload_words(vector)
            successful_payloads[index] = payload
            executions.append({
                "index": index,
                "renderer": f"0x{address:08X}",
                "status": "PASS",
                "packet_sha256": vector["dspi1_packet"]["sha256"],
            })
        except Exception as exc:  # Preserve partial coverage as explicit evidence.
            executions.append({
                "index": index,
                "renderer": f"0x{address:08X}",
                "status": "EMULATOR_GAP",
                "error": str(exc),
            })

    if 0 not in successful_payloads:
        raise ValueError("baseline renderer 0 did not execute")
    baseline = successful_payloads[0]
    renderer_differences = []
    for execution in executions:
        index = execution["index"]
        if index not in successful_payloads:
            continue
        payload = successful_payloads[index]
        differences = [
            PAYLOAD_FIRST_WORD + offset
            for offset, (left, right) in enumerate(zip(baseline, payload))
            if left != right
        ]
        renderer_differences.append({
            "index": index,
            "renderer": execution["renderer"],
            "differing_word_indices_vs_renderer0": differences,
            "values": {
                str(word_index): f"0x{payload[word_index - PAYLOAD_FIRST_WORD]:08X}"
                for word_index in differences
            },
        })

    word_map = []
    sensitive_indices = []
    for offset in range(PAYLOAD_WORD_COUNT):
        word_index = PAYLOAD_FIRST_WORD + offset
        values = sorted({payload[offset] for payload in successful_payloads.values()})
        classification = "renderer-sensitive" if len(values) > 1 else "invariant"
        if len(values) > 1:
            sensitive_indices.append(word_index)
        word_map.append({
            "word_index": word_index,
            "classification": classification,
            "distinct_value_count": len(values),
            "values": [f"0x{value:08X}" for value in values],
        })

    signature_families: dict[tuple[int, ...], list[int]] = defaultdict(list)
    for row in renderer_differences:
        signature_families[tuple(row["differing_word_indices_vs_renderer0"])].append(
            row["index"]
        )
    families = [
        {
            "renderer_indices": indices,
            "differing_word_indices_vs_renderer0": list(signature),
        }
        for signature, indices in sorted(
            signature_families.items(), key=lambda item: (len(item[0]), item[0])
        )
    ]

    failures = [row for row in executions if row["status"] != "PASS"]
    result = {
        "result": "PASS_WITH_EMULATOR_COVERAGE_GAP" if failures else "PASS",
        "main": {"sha256": digest},
        "emulator": {
            "sha256": hashlib.sha256(emulator_path.read_bytes()).hexdigest(),
        },
        "method": {
            "renderer_table": f"0x{RENDERER_TABLE:08X}",
            "renderer_count": RENDERER_COUNT,
            "voice0_renderer_pointer": f"0x{VOICE0_RENDERER_POINTER:08X}",
            "substitution_pc": f"0x{RENDERER_DISPATCH:08X}",
            "common_state": "note 60, synth-live gate, one stock callback",
            "payload_word_indices": [
                PAYLOAD_FIRST_WORD,
                PAYLOAD_FIRST_WORD + PAYLOAD_WORD_COUNT - 1,
            ],
            "payload_word_count": PAYLOAD_WORD_COUNT,
        },
        "summary": {
            "successful_renderers": len(successful_payloads),
            "emulator_coverage_gaps": len(failures),
            "distinct_packet_hashes": len({
                row["packet_sha256"]
                for row in executions
                if row["status"] == "PASS"
            }),
            "difference_signature_families": len(families),
            "renderer_sensitive_words": len(sensitive_indices),
            "invariant_words": PAYLOAD_WORD_COUNT - len(sensitive_indices),
            "renderer_sensitive_word_indices": sensitive_indices,
            "renderer_sensitive_contiguous_groups": contiguous_groups(
                sensitive_indices
            ),
        },
        "executions": executions,
        "difference_signature_families": families,
        "renderer_differences": renderer_differences,
        "word_map": word_map,
        "interpretation": {
            "established": (
                "The common-state sweep partitions all 492 asserted-PCS0 payload "
                "positions into renderer-sensitive and invariant sets."
            ),
            "inference": (
                "Repeated tagged value groups are consistent with a framed "
                "multi-register analog-control protocol, but do not identify an "
                "off-chip device or physical units."
            ),
            "limitation": (
                "Function-pointer substitution does not install each renderer's "
                "authentic machine descriptor or preset state. Invariant positions "
                "may vary under other parameters or runtime phases."
            ),
        },
        "next_target": (
            "Vary known parameters inside one authentic renderer/descriptor pairing, "
            "starting with words 46/47 and the dense 229..244 and 309..320 blocks."
        ),
        "safety": (
            "Stock firmware execution under emulation only; no flashable image was "
            "produced."
        ),
    }

    if len(addresses) != 53 or len(successful_payloads) < 52:
        raise ValueError("renderer coverage regressed")
    if 46 not in sensitive_indices or 47 not in sensitive_indices:
        raise ValueError("known pitch pair was not renderer-sensitive")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("main_image", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument(
        "--probe-root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="directory containing the callback-probe dependency chain",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = probe(args.main_image, args.emulator, args.probe_root)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
