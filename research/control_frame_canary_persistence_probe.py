#!/usr/bin/env python3
"""Prove callback-entry canaries survive through writer-free DSPI1 fields.

This is an emulation-only transport test.  It does not assign semantics to the
FPGA receiver or claim that any field is safe to exercise on live hardware.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256
from control_frame_global_ownership_probe import PACKETIZER_READ_PCS
from machine_pitch_calibration_probe import (
    DISPATCH_INDEX_PC, MAIN_BASE, PUBLIC_MACHINE_NAMES, RENDERER_COUNT,
    RENDERER_TABLE,
)
from note_pitch_consumer_boundary_probe import load_emulator, run_vector
from renderer_control_ownership_probe import (
    FORCED_STATES, NOTE, PACKET_SOURCE_FIRST, VOICE_EVENT_STATE, compact_ranges,
)

WRITER_FREE_WORD_RANGES = (
    (50, 57), (60, 65), (67, 70), (73, 79),
    (150, 150), (152, 152), (154, 154), (156, 156), (158, 158),
    (160, 160), (162, 162), (164, 164), (166, 166), (168, 168),
    (170, 170), (172, 172), (174, 174), (176, 176), (178, 178),
    (180, 180), (197, 198), (200, 202), (204, 206), (208, 210),
    (212, 214), (216, 218), (220, 221), (281, 282), (284, 286),
    (288, 290), (292, 294), (296, 298), (300, 302), (304, 306),
    (308, 308), (334, 340), (342, 342), (344, 344), (346, 346),
    (348, 348), (350, 350), (352, 352), (354, 354), (356, 356),
    (358, 358), (360, 360), (362, 362), (364, 364), (366, 366),
    (368, 368), (370, 370), (372, 372), (374, 374), (376, 376),
    (378, 378), (380, 380), (382, 382), (384, 384), (386, 386),
    (388, 388), (390, 390), (428, 428), (430, 434), (436, 436),
    (438, 442), (444, 444), (446, 450), (452, 452), (454, 454),
    (456, 456), (458, 458), (460, 460), (462, 472), (474, 490),
    (492, 492),
)
WRITER_FREE_WORDS = tuple(
    word for first, last in WRITER_FREE_WORD_RANGES for word in range(first, last + 1)
)
NEGATIVE_CONTROL_WORD = 1
NEGATIVE_CONTROL_CANARY = 0xC001
EXPECTED_MATRIX_SHA256 = "5673a1c343fca7041be2596e357b10e6ca65e71090f5348dba859db56badbcc4"


def source_address(word: int) -> int:
    return PACKET_SOURCE_FIRST + 2 * (word - 1)


def canary(word: int) -> int:
    """Deterministic nonzero per-word marker, kept inside one halfword."""
    return 0xA000 | ((word * 0x31 + 0x05D) & 0x0FFF)


def run_context(module, main_path: Path, machine_id: int, renderer: int,
                forced_state: int) -> dict:
    original_step = module.CPU.step
    original_write = module.Bus.write
    original_read = module.Bus.read
    seeded_buses: set[int] = set()
    cpus: dict[int, object] = {}
    packet_reads: dict[int, list[int]] = {word: [] for word in WRITER_FREE_WORDS}
    renderer_visits = 0

    def read(bus, address: int, size: int) -> int:
        value = original_read(bus, address, size)
        cpu = cpus.get(id(bus))
        if (cpu is not None and cpu.pc in PACKETIZER_READ_PCS and size == 2
                and address >= PACKET_SOURCE_FIRST):
            word = 1 + (address - PACKET_SOURCE_FIRST) // 2
            if word in packet_reads:
                packet_reads[word].append(value)
        return value

    def step(cpu):
        nonlocal renderer_visits
        cpus[id(cpu.bus)] = cpu
        bus_id = id(cpu.bus)
        if cpu.pc == AUDIO_CALLBACK and bus_id not in seeded_buses:
            for word in WRITER_FREE_WORDS:
                original_write(cpu.bus, source_address(word), 2, canary(word))
            original_write(cpu.bus, source_address(NEGATIVE_CONTROL_WORD), 2,
                           NEGATIVE_CONTROL_CANARY)
            seeded_buses.add(bus_id)
        if cpu.pc == DISPATCH_INDEX_PC and cpu.d[2] == 0:
            cpu.d[1] = machine_id
        if cpu.pc == renderer:
            renderer_visits += 1
            for offset in (0, 4, 8):
                original_write(cpu.bus, VOICE_EVENT_STATE + offset, 4, forced_state)
        return original_step(cpu)

    module.Bus.read = read
    module.CPU.step = step
    vector = run_vector(module, main_path, NOTE, 1, callback_count=1)
    if renderer_visits != 1:
        raise ValueError(f"machine {machine_id} state {forced_state} renderer visits: {renderer_visits}")

    source_failures = []
    packet_failures = []
    for word in WRITER_FREE_WORDS:
        expected = canary(word)
        reads = packet_reads[word]
        if reads != [expected]:
            source_failures.append({"word": word, "expected": expected, "reads": reads})
        actual = vector["packet_words"][word] & 0xFFFF
        if actual != expected:
            packet_failures.append({"word": word, "expected": expected, "actual": actual})

    negative_actual = vector["packet_words"][NEGATIVE_CONTROL_WORD] & 0xFFFF
    if source_failures or packet_failures:
        raise ValueError(
            f"machine {machine_id} state {forced_state} canary failures: "
            f"source={source_failures[:3]} packet={packet_failures[:3]}"
        )
    return {
        "machine_id": machine_id,
        "machine_name": PUBLIC_MACHINE_NAMES[machine_id],
        "renderer": f"0x{renderer:08X}",
        "state": forced_state,
        "source_checks": len(WRITER_FREE_WORDS),
        "packet_checks": len(WRITER_FREE_WORDS),
        "payload_sha256": hashlib.sha256(
            b"".join((vector["packet_words"][word] & 0xFFFF).to_bytes(2, "big")
                     for word in WRITER_FREE_WORDS)
        ).hexdigest(),
        "negative_control": {
            "word": NEGATIVE_CONTROL_WORD,
            "seed": f"0x{NEGATIVE_CONTROL_CANARY:04X}",
            "packet_value": f"0x{negative_actual:04X}",
            "overwritten": negative_actual != NEGATIVE_CONTROL_CANARY,
        },
    }


def probe(main_path: Path, emulator_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    if len(WRITER_FREE_WORDS) != 165 or len(set(WRITER_FREE_WORDS)) != 165:
        raise ValueError("writer-free field definition changed")

    offset = RENDERER_TABLE - MAIN_BASE
    renderers = [
        struct.unpack(">I", image[offset + 4 * i:offset + 4 * i + 4])[0]
        for i in range(RENDERER_COUNT)
    ]
    contexts = [
        run_context(load_emulator(emulator_path), main_path, machine_id,
                    renderers[machine_id], forced_state)
        for machine_id in range(len(PUBLIC_MACHINE_NAMES))
        for forced_state in FORCED_STATES
    ]
    matrix_digest = hashlib.sha256(json.dumps(contexts, sort_keys=True).encode()).hexdigest()
    if EXPECTED_MATRIX_SHA256 != "TO_BE_LOCKED" and matrix_digest != EXPECTED_MATRIX_SHA256:
        raise ValueError(f"canary matrix changed: {matrix_digest}")
    if not all(row["negative_control"]["overwritten"] for row in contexts):
        failed = [(row["machine_id"], row["state"]) for row in contexts
                  if not row["negative_control"]["overwritten"]]
        raise ValueError(f"owned-field negative control survived: {failed}")

    checks = len(contexts) * len(WRITER_FREE_WORDS)
    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "coverage": {
            "public_machines": len(PUBLIC_MACHINE_NAMES),
            "logical_tracks": [0],
            "states": list(FORCED_STATES),
            "contexts": len(contexts),
            "writer_free_fields": len(WRITER_FREE_WORDS),
            "source_checks": checks,
            "packet_checks": checks,
            "matrix_sha256": matrix_digest,
        },
        "canaries": {
            "injection_boundary": f"callback entry 0x{AUDIO_CALLBACK:08X}",
            "word_ranges": compact_ranges(list(WRITER_FREE_WORDS)),
            "values": [{"word": word, "address": f"0x{source_address(word):08X}",
                        "value": f"0x{canary(word):04X}"}
                       for word in WRITER_FREE_WORDS],
            "all_survived_to_packetizer_read": True,
            "all_survived_to_final_dspi1_packet": True,
        },
        "negative_control": {
            "universally_owned_word": NEGATIVE_CONTROL_WORD,
            "seed": f"0x{NEGATIVE_CONTROL_CANARY:04X}",
            "overwritten_in_all_contexts": True,
        },
        "contexts": contexts,
        "conclusion": (
            "For logical track 0, all 165 callback-writer-free source halfwords preserve callback-entry "
            "canaries through the packetizer read and final DSPI1 packet in all 170 "
            "public machine/state contexts. The universally owned control is overwritten "
            "in every context, validating the experiment's overwrite sensitivity."
        ),
        "next_target": (
            "Rank adjacent writer-free candidate pairs by non-packet read isolation and "
            "structural proximity, then repeat a two-field-only canary test on the winner."
        ),
        "safety": (
            "Stock emulation and synthetic SRAM markers only; no firmware was modified. "
            "Other logical tracks, FPGA field semantics, and live-hardware safety remain unknown."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("main_image", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    encoded = json.dumps(probe(args.main_image, args.emulator), indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
