#!/usr/bin/env python3
"""Prove exact packet locality for the selected two-halfword candidate slot."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256
from control_frame_canary_persistence_probe import source_address
from machine_pitch_calibration_probe import (
    DISPATCH_INDEX_PC, MAIN_BASE, PUBLIC_MACHINE_NAMES, RENDERER_COUNT,
    RENDERER_TABLE,
)
from note_pitch_consumer_boundary_probe import load_emulator, run_vector
from renderer_control_ownership_probe import FORCED_STATES, NOTE, VOICE_EVENT_STATE

CANDIDATE_WORDS = (197, 198)
CANDIDATE_VALUES = (0xF27A, 0x0D85)
EXPECTED_MATRIX_SHA256 = "f94b4c0703e612885f51674c47e255ccbbc34da2b7fc63ba2b572139f57b70b0"


def run_packet(module, main_path: Path, machine_id: int, renderer: int,
               forced_state: int, seed: bool) -> list[int]:
    original_step = module.CPU.step
    original_write = module.Bus.write
    seeded_buses: set[int] = set()
    renderer_visits = 0

    def step(cpu):
        nonlocal renderer_visits
        bus_id = id(cpu.bus)
        if seed and cpu.pc == AUDIO_CALLBACK and bus_id not in seeded_buses:
            for word, value in zip(CANDIDATE_WORDS, CANDIDATE_VALUES):
                original_write(cpu.bus, source_address(word), 2, value)
            seeded_buses.add(bus_id)
        if cpu.pc == DISPATCH_INDEX_PC and cpu.d[2] == 0:
            cpu.d[1] = machine_id
        if cpu.pc == renderer:
            renderer_visits += 1
            for offset in (0, 4, 8):
                original_write(cpu.bus, VOICE_EVENT_STATE + offset, 4, forced_state)
        return original_step(cpu)

    module.CPU.step = step
    vector = run_vector(module, main_path, NOTE, 1, callback_count=1)
    if renderer_visits != 1:
        raise ValueError(f"machine {machine_id} state {forced_state} renderer visits: {renderer_visits}")
    return vector["packet_words"]


def probe(main_path: Path, emulator_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    offset = RENDERER_TABLE - MAIN_BASE
    renderers = [
        struct.unpack(">I", image[offset + 4 * i:offset + 4 * i + 4])[0]
        for i in range(RENDERER_COUNT)
    ]

    contexts = []
    for machine_id, machine_name in enumerate(PUBLIC_MACHINE_NAMES):
        for forced_state in FORCED_STATES:
            baseline = run_packet(load_emulator(emulator_path), main_path, machine_id,
                                  renderers[machine_id], forced_state, False)
            seeded = run_packet(load_emulator(emulator_path), main_path, machine_id,
                                renderers[machine_id], forced_state, True)
            differences = [index for index, (left, right) in enumerate(zip(baseline, seeded))
                           if left != right]
            if differences != list(CANDIDATE_WORDS):
                raise ValueError(
                    f"machine {machine_id} state {forced_state} packet differences: {differences}"
                )
            seeded_payloads = [seeded[word] & 0xFFFF for word in CANDIDATE_WORDS]
            if seeded_payloads != list(CANDIDATE_VALUES):
                raise ValueError(
                    f"machine {machine_id} state {forced_state} candidate payloads: {seeded_payloads}"
                )
            contexts.append({
                "machine_id": machine_id,
                "machine_name": machine_name,
                "renderer": f"0x{renderers[machine_id]:08X}",
                "state": forced_state,
                "differing_word_indices": differences,
                "baseline_values": [f"0x{baseline[word] & 0xFFFF:04X}" for word in CANDIDATE_WORDS],
                "seeded_values": [f"0x{seeded[word] & 0xFFFF:04X}" for word in CANDIDATE_WORDS],
                "baseline_packet_sha256": hashlib.sha256(
                    b"".join(word.to_bytes(4, "big") for word in baseline)
                ).hexdigest(),
                "seeded_packet_sha256": hashlib.sha256(
                    b"".join(word.to_bytes(4, "big") for word in seeded)
                ).hexdigest(),
            })

    matrix_digest = hashlib.sha256(json.dumps(contexts, sort_keys=True).encode()).hexdigest()
    if EXPECTED_MATRIX_SHA256 != "TO_BE_LOCKED" and matrix_digest != EXPECTED_MATRIX_SHA256:
        raise ValueError(f"two-word locality matrix changed: {matrix_digest}")
    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "coverage": {"public_machines": len(PUBLIC_MACHINE_NAMES),
                     "logical_tracks": [0], "states": list(FORCED_STATES), "contexts": len(contexts),
                     "baseline_callbacks": len(contexts), "seeded_callbacks": len(contexts),
                     "matrix_sha256": matrix_digest},
        "candidate": {
            "words": list(CANDIDATE_WORDS),
            "addresses": [f"0x{source_address(word):08X}" for word in CANDIDATE_WORDS],
            "seed_values": [f"0x{value:04X}" for value in CANDIDATE_VALUES],
            "exact_packet_locality_in_every_context": True,
        },
        "contexts": contexts,
        "conclusion": (
            "On logical track 0, seeding only words 197/198 changes exactly DSPI1 packet words 197/198 "
            "against a separately executed stock baseline in every public machine/state context."
        ),
        "next_target": (
            "Trace initialization and neighboring word-195/196 construction to determine "
            "whether 197/198 are deliberate reserved padding or part of a wider packed lane."
        ),
        "safety": (
            "Emulation-only differential; no firmware was modified and no FPGA semantic "
            "or live-hardware safety claim is made."
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
