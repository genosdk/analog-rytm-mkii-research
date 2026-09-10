#!/usr/bin/env python3
"""Classify pitch-control calibration across all 53 stock machine renderers.

The static half inventories renderer-local constants and helper calls.  The
dynamic half substitutes a table index only at the stock dispatch boundary and
executes authentic note events through selected stock renderers.  Firmware
bytes are never changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256
from note_pitch_consumer_boundary_probe import load_emulator, run_vector
from synth_pitch_encoding_probe import CHANNEL_SCALES, calibrated_halfword

MAIN_BASE = 0x40000400
RENDERER_TABLE = 0x40277FE8
RENDERER_COUNT = 53
DISPATCH_INDEX_PC = 0x4011C9C8
LOG_HELPER_CALL = bytes.fromhex("4eb94011a0f8")
EXP2_WRAPPER_CALL = bytes.fromhex("4eb94011a122")
EXP2_HELPER_CALL = bytes.fromhex("4eb940095a14")
SCALE_PATTERNS = {
    0x00062000: bytes.fromhex("00062000"),
    0x0004168F: bytes.fromhex("0004168f"),
}
NOTES = (48, 60, 72)
PUBLIC_MACHINE_NAMES = (
    "bd hard", "bd classic", "sd hard", "sd classic", "rs hard", "rs classic",
    "cp classic", "bt classic", "xt classic", "ch classic", "oh classic",
    "cy classic", "cb classic", "bd fm", "sd fm", "ut noise", "ut impulse",
    "ch metallic", "oh metallic", "cy metallic", "cb metallic", "bd plastic",
    "bd silky", "sd natural", "hh basic", "cy ride", "bd sharp", "DISABLE",
    "sy dual vco", "sy chip", "bd acoustic", "sd acoustic", "sy raw", "hh lab",
)

# These representatives cover every renderer family with either calibrated
# constants or two stock log/exp conversion paths.  A few additional dynamic
# scale families are included to make the non-universality result concrete.
RUNTIME_MACHINE_IDS = tuple(range(len(PUBLIC_MACHINE_NAMES)))
EXPECTED_RUNTIME = {
    0: ((0x13, 0x0C), (0x26, 0x19), (0x4D, 0x33)),
    1: ((0x13, 0x0C), (0x26, 0x19), (0x4D, 0x33)),
    2: ((0x32, 0x21), (0x65, 0x43), (0xCA, 0x86)),
    3: ((0x2F, 0x2F), (0x5E, 0x5E), (0xBC, 0xBC)),
    4: ((0x14A, 0x5A), (0x295, 0xB4), (0x52B, 0x169)),
    5: ((0x14A, 0x5A), (0x295, 0xB4), (0x52B, 0x169)),
    6: ((0, 0), (0, 0), (0, 0)),
    7: ((0, 0), (0, 0), (0, 0)),
    8: ((0, 0), (0, 0), (0, 0)),
    9: ((0, 0), (0, 0), (0, 0)),
    10: ((0, 0), (0, 0), (0, 0)),
    11: ((0, 0), (0, 0), (0, 0)),
    12: ((0, 0), (0, 0), (0, 0)),
    13: ((0x0C, 0x19), (0x19, 0x33), (0x33, 0x67)),
    14: ((0x2F, 0x2F), (0x5E, 0x5E), (0xBC, 0xBC)),
    15: ((0, 0), (0, 0), (0, 0)),
    16: ((0, 0), (0, 0), (0, 0)),
    17: ((0, 0), (0, 0), (0, 0)),
    18: ((0, 0), (0, 0), (0, 0)),
    19: ((0, 0), (0, 0), (0, 0)),
    20: ((0, 0), (0, 0), (0, 0)),
    21: ((0x0C, 0x0C), (0x19, 0x19), (0x33, 0x33)),
    22: ((0x0C, 0x00), (0x19, 0x00), (0x33, 0x00)),
    23: ((0x00, 0x19), (0x00, 0x33), (0x00, 0x67)),
    24: ((0, 0), (0, 0), (0, 0)),
    25: ((0, 0), (0, 0), (0, 0)),
    26: ((0x00, 0x0C), (0x00, 0x19), (0x00, 0x33)),
    27: ((0, 0), (0, 0), (0, 0)),
    28: ((0x08, 0x08), (0x08, 0x08), (0x10, 0x08)),
    29: ((0x19, 0x19), (0x33, 0x33), (0x67, 0x67)),
    30: ((0x00, 0x0C), (0x00, 0x19), (0x00, 0x33)),
    31: ((0x19, 0x26), (0x33, 0x4D), (0x67, 0x9B)),
    32: ((0x08, 0x19), (0x08, 0x33), (0x08, 0x67)),
    33: ((0, 0), (0, 0), (0, 0)),
}


def occurrences(body: bytes, pattern: bytes, start: int) -> list[str]:
    result = []
    offset = 0
    while True:
        offset = body.find(pattern, offset)
        if offset < 0:
            return result
        result.append(f"0x{start + offset:08X}")
        offset += 1


def run_machine_note(module, main_path: Path, machine_id: int, renderer: int, note: int) -> tuple[int, int]:
    original_step = module.CPU.step
    visits = 0

    def step(cpu):
        nonlocal visits
        # At this PC A0 contains the table base and the following indexed LEA
        # consumes D1 as a longword-scaled table index.
        if cpu.pc == DISPATCH_INDEX_PC and cpu.d[2] == 0:
            cpu.d[1] = machine_id
        if cpu.pc == renderer:
            visits += 1
        return original_step(cpu)

    module.CPU.step = step
    vector = run_vector(module, main_path, note, 1, callback_count=1)
    if visits != 1:
        raise ValueError(f"machine {machine_id} renderer visits: {visits}")
    words = vector["packet_words"]
    return words[46] & 0xFFFF, words[47] & 0xFFFF


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
    if len(set(renderers)) != RENDERER_COUNT or not all(MAIN_BASE <= address < RENDERER_TABLE for address in renderers):
        raise ValueError("renderer table is not 53 unique MAIN code pointers")
    ordered = sorted(renderers)
    rows = []
    for machine_id, start in enumerate(renderers):
        later = [address for address in ordered if address > start]
        end = later[0] if later else 0x4011AE52
        body = image[start - MAIN_BASE:end - MAIN_BASE]
        scale_hits = {
            f"0x{scale:08X}": occurrences(body, pattern, start)
            for scale, pattern in SCALE_PATTERNS.items()
        }
        log_calls = occurrences(body, LOG_HELPER_CALL, start)
        exp_calls = occurrences(body, EXP2_WRAPPER_CALL, start) + occurrences(body, EXP2_HELPER_CALL, start)
        rows.append({
            "machine_id": machine_id,
            "machine_name": PUBLIC_MACHINE_NAMES[machine_id] if machine_id < len(PUBLIC_MACHINE_NAMES) else None,
            "namespace": "public_sound_machine" if machine_id < len(PUBLIC_MACHINE_NAMES) else "internal_or_reserved_renderer",
            "renderer": f"0x{start:08X}",
            "bounded_end": f"0x{end:08X}",
            "scale_hits": scale_hits,
            "log_helper_calls": log_calls,
            "exp2_calls": sorted(exp_calls),
        })

    scale_ids = {
        f"0x{scale:08X}": [row["machine_id"] for row in rows if row["scale_hits"][f"0x{scale:08X}"]]
        for scale in SCALE_PATTERNS
    }
    exp2_ids = [row["machine_id"] for row in rows if row["exp2_calls"]]
    if scale_ids["0x00062000"] != [0, 1]:
        raise ValueError(f"unexpected 0x62000 renderer set: {scale_ids['0x00062000']}")
    if scale_ids["0x0004168F"] != [0, 1, 13, 21, 22, 26, 30, 35, 36, 41]:
        raise ValueError(f"unexpected 0x4168F renderer set: {scale_ids['0x0004168F']}")

    runtime = []
    for machine_id in RUNTIME_MACHINE_IDS:
        vectors = []
        for note in NOTES:
            module = load_emulator(emulator_path)
            pair = run_machine_note(module, main_path, machine_id, renderers[machine_id], note)
            vectors.append({"note": note, "word_46": f"0x{pair[0]:04X}", "word_47": f"0x{pair[1]:04X}"})
        actual = tuple((int(v["word_46"], 16), int(v["word_47"], 16)) for v in vectors)
        if actual != EXPECTED_RUNTIME[machine_id]:
            raise ValueError(f"machine {machine_id} runtime vectors changed: {actual}")
        runtime.append({
            "machine_id": machine_id,
            "machine_name": PUBLIC_MACHINE_NAMES[machine_id],
            "renderer": f"0x{renderers[machine_id]:08X}",
            "vectors": vectors,
        })

    # IDs 0 and 1 must reproduce the already-derived two-scale equation for
    # each anchor. Machine 2 is the immediate counterexample to universality.
    exp2_anchors = (0x0001965F, 0x00032CBF, 0x0006597F)
    calibrated = tuple(
        tuple(calibrated_halfword(value, scale) for scale in CHANNEL_SCALES)
        for value in exp2_anchors
    )
    if EXPECTED_RUNTIME[0] != calibrated or EXPECTED_RUNTIME[1] != calibrated:
        raise ValueError("machine 0/1 calibration identity changed")
    if EXPECTED_RUNTIME[2] == calibrated:
        raise ValueError("counterexample renderer unexpectedly shares the exact pair")

    signature_groups = {}
    for machine_id, signature in EXPECTED_RUNTIME.items():
        key = "/".join(f"{left:04X}:{right:04X}" for left, right in signature)
        signature_groups.setdefault(key, []).append({
            "machine_id": machine_id,
            "machine_name": PUBLIC_MACHINE_NAMES[machine_id],
        })

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "dispatch": {
            "renderer_table": f"0x{RENDERER_TABLE:08X}",
            "entries": RENDERER_COUNT,
            "index_substitution_pc": f"0x{DISPATCH_INDEX_PC:08X}",
            "index_register": "D1",
            "stock_index_source": "MVZ.B live machine byte at 0x8000EA00 + logical track",
            "stock_index_load": "0x4011C9AE..0x4011C9B2",
        },
        "machine_metadata": {
            "public_machine_count": len(PUBLIC_MACHINE_NAMES),
            "renderer_count": RENDERER_COUNT,
            "public_id_range": [0, len(PUBLIC_MACHINE_NAMES) - 1],
            "internal_or_reserved_id_range": [len(PUBLIC_MACHINE_NAMES), RENDERER_COUNT - 1],
            "names": [{"machine_id": index, "machine_name": name} for index, name in enumerate(PUBLIC_MACHINE_NAMES)],
            "source": "pinned external/libanalogrytm sound-format definitions; independently mirrored by pinned rytm-rs",
        },
        "static_classification": {
            "scale_machine_ids": scale_ids,
            "exp2_machine_ids": exp2_ids,
            "exact_two_scale_machine_ids": [0, 1],
            "renderer_rows": rows,
        },
        "runtime_anchors": runtime,
        "runtime_signature_groups": list(signature_groups.values()),
        "identity": {
            "universal_pair": False,
            "exact_pair_scope": "bd hard (0) and bd classic (1) only",
            "shared_channel_evidence": (
                "0x0004168F recurs in ten renderers and appears alone, duplicated, "
                "or on either packet channel; 0x00062000 occurs only in IDs 0 and 1."
            ),
            "interpretation": (
                "DSPI1 words 46/47 are shared physical-voice control slots whose "
                "pitch calibration and active-channel topology are machine-dependent."
            ),
        },
        "conclusion": (
            "The 0x62000/0x4168F pair is not a universal analog-voice convention. "
            "It is the exact dual-channel calibration of machine IDs 0 and 1. Other "
            "stock renderers reuse the same packet slots with symmetric, single-sided, "
            "duplicated, or differently scaled pitch controls."
        ),
        "next_target": (
            "Inventory renderer-owned per-voice control fields across all public machines "
            "and renderer states, then reject any Filter 2 transport candidate with "
            "machine-specific ownership."
        ),
        "safety": "Static analysis and stock execution with synthetic table-index selection only; no firmware was modified.",
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
