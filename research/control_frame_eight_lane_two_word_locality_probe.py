#!/usr/bin/env python3
"""Prove two-word DSPI1 packet locality across all eight voice mappings."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, prepared_machine
from br_hardware_sink_probe import (
    CONTROL_DMA_INITIALIZER, PING_PONG_BASE, PING_PONG_SELECTOR,
    PING_PONG_STRIDE,
)
from control_frame_canary_persistence_probe import source_address
from control_frame_eight_lane_ownership_probe import (
    PHYSICAL_VOICE_TO_LOGICAL_TRACK, VOICE_STATE_STRIDE,
)
from machine_pitch_calibration_probe import (
    DISPATCH_INDEX_PC, MAIN_BASE, PUBLIC_MACHINE_NAMES, RENDERER_COUNT,
    RENDERER_TABLE,
)
from note_event_constructor_probe import (
    EVENT_INPUT, EVENT_TRACK, NOTE_EVENT_CONSTRUCTOR, NOTE_ON,
    QWERTY_SOURCE_MASK, write_event,
)
from note_pitch_consumer_boundary_probe import (
    PACKET_SOURCE_OFFSET, PACKET_WORDS, PITCH_SELECT_SOURCE,
    run_traced_callback,
)
from renderer_control_ownership_probe import FORCED_STATES, NOTE, VOICE_EVENT_STATE
from trigger_queue_probe import (
    CONTROL_SNAPSHOT_A, CONTROL_SNAPSHOT_POINTER, QUEUE, QUEUE_CAPACITY,
    QUEUE_INITIALIZER, QUEUE_INSTALLER, load_emulator, stock_call,
)

CANDIDATE_WORDS = (67, 68)
CANDIDATE_VALUES = (0xF243, 0x0DBC)
EXPECTED_MATRIX_SHA256 = "42b91992f68c9cab986234bf4ea1998c9c06d4c0b26dcf29e6665b51fccb8d80"


def run_packet(module, main_path: Path, physical_voice: int, logical_track: int,
               machine_id: int, renderer: int, forced_state: int,
               seed: bool) -> list[int]:
    bus, cpu, _ = prepared_machine(module, main_path)
    callback_sp = cpu.a[7]
    stock_call(cpu, CONTROL_DMA_INITIALIZER, [])
    stock_call(cpu, QUEUE_INITIALIZER, [QUEUE, 0, 0x419531F8, QUEUE_CAPACITY])
    stock_call(cpu, QUEUE_INSTALLER, [QUEUE])
    bus.write(CONTROL_SNAPSHOT_POINTER, 4, CONTROL_SNAPSHOT_A)
    bus.write(PITCH_SELECT_SOURCE + logical_track * 0x54, 1, 1)
    write_event(bus, event_type=NOTE_ON, note=NOTE, source_mask=QWERTY_SOURCE_MASK)
    bus.write(EVENT_INPUT + EVENT_TRACK, 4, logical_track)
    stock_call(cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])

    original_step = module.CPU.step
    original_write = bus.write
    renderer_visits = 0
    seeded = False

    def step(current_cpu):
        nonlocal renderer_visits, seeded
        if seed and current_cpu.pc == AUDIO_CALLBACK and not seeded:
            for word, value in zip(CANDIDATE_WORDS, CANDIDATE_VALUES):
                original_write(source_address(word), 2, value)
            seeded = True
        if current_cpu.pc == DISPATCH_INDEX_PC and current_cpu.d[2] == physical_voice:
            current_cpu.d[1] = machine_id
        if current_cpu.pc == renderer:
            renderer_visits += 1
            state_base = VOICE_EVENT_STATE + physical_voice * VOICE_STATE_STRIDE
            for offset in (0, 4, 8):
                original_write(state_base + offset, 4, forced_state)
        return original_step(current_cpu)

    module.CPU.step = step
    run_traced_callback(cpu, callback_sp)
    if renderer_visits != 1:
        raise ValueError(
            f"voice {physical_voice} machine {machine_id} state {forced_state} "
            f"renderer visits: {renderer_visits}"
        )
    selector = bus.read(PING_PONG_SELECTOR, 4)
    packet_base = PING_PONG_BASE + selector * PING_PONG_STRIDE
    return [
        bus.read(packet_base + PACKET_SOURCE_OFFSET + 4 * index, 4)
        for index in range(PACKET_WORDS)
    ]


def run_lane(main_path_text: str, emulator_path_text: str, physical_voice: int,
             logical_track: int, renderers: list[int]) -> dict:
    main_path = Path(main_path_text)
    emulator_path = Path(emulator_path_text)
    contexts = []
    for machine_id in range(len(PUBLIC_MACHINE_NAMES)):
        for forced_state in FORCED_STATES:
            baseline = run_packet(
                load_emulator(emulator_path), main_path, physical_voice,
                logical_track, machine_id, renderers[machine_id], forced_state, False,
            )
            seeded = run_packet(
                load_emulator(emulator_path), main_path, physical_voice,
                logical_track, machine_id, renderers[machine_id], forced_state, True,
            )
            differences = [
                index for index, (left, right) in enumerate(zip(baseline, seeded))
                if left != right
            ]
            if differences != list(CANDIDATE_WORDS):
                raise ValueError(
                    f"voice {physical_voice} machine {machine_id} state {forced_state} "
                    f"packet differences: {differences}"
                )
            payloads = [seeded[word] & 0xFFFF for word in CANDIDATE_WORDS]
            if payloads != list(CANDIDATE_VALUES):
                raise ValueError(
                    f"voice {physical_voice} machine {machine_id} state {forced_state} "
                    f"candidate payloads: {payloads}"
                )
            contexts.append({
                "machine_id": machine_id,
                "state": forced_state,
                "differences": differences,
                "baseline_values": [baseline[word] & 0xFFFF for word in CANDIDATE_WORDS],
                "seeded_values": payloads,
                "baseline_sha256": hashlib.sha256(
                    b"".join(word.to_bytes(4, "big") for word in baseline)
                ).hexdigest(),
                "seeded_sha256": hashlib.sha256(
                    b"".join(word.to_bytes(4, "big") for word in seeded)
                ).hexdigest(),
            })
    return {
        "physical_voice": physical_voice,
        "logical_track": logical_track,
        "contexts": len(contexts),
        "baseline_callbacks": len(contexts),
        "seeded_callbacks": len(contexts),
        "context_sha256": hashlib.sha256(
            json.dumps(contexts, sort_keys=True).encode()
        ).hexdigest(),
    }


def run_lane_task(task):
    return run_lane(*task)


def probe(main_path: Path, emulator_path: Path, jobs: int = 1) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    offset = RENDERER_TABLE - MAIN_BASE
    renderers = [
        struct.unpack(">I", image[offset + 4 * i:offset + 4 * i + 4])[0]
        for i in range(RENDERER_COUNT)
    ]
    tasks = [
        (str(main_path), str(emulator_path), physical_voice, logical_track, renderers)
        for physical_voice, logical_track in enumerate(PHYSICAL_VOICE_TO_LOGICAL_TRACK)
    ]
    if jobs == 1:
        lanes = [run_lane(*task) for task in tasks]
    else:
        with ProcessPoolExecutor(max_workers=min(jobs, len(tasks))) as executor:
            lanes = list(executor.map(run_lane_task, tasks))
    lanes.sort(key=lambda row: row["physical_voice"])
    matrix_digest = hashlib.sha256(json.dumps(lanes, sort_keys=True).encode()).hexdigest()
    if EXPECTED_MATRIX_SHA256 != "TO_BE_LOCKED" and matrix_digest != EXPECTED_MATRIX_SHA256:
        raise ValueError(f"eight-lane locality matrix changed: {matrix_digest}")
    contexts = sum(row["contexts"] for row in lanes)
    if contexts != 1360:
        raise ValueError(f"unexpected locality context count: {contexts}")
    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "coverage": {
            "public_machines": len(PUBLIC_MACHINE_NAMES),
            "states": list(FORCED_STATES),
            "physical_voice_to_logical_track": list(PHYSICAL_VOICE_TO_LOGICAL_TRACK),
            "contexts": contexts,
            "baseline_callbacks": contexts,
            "seeded_callbacks": contexts,
            "matrix_sha256": matrix_digest,
        },
        "candidate": {
            "words": list(CANDIDATE_WORDS),
            "addresses": [f"0x{source_address(word):08X}" for word in CANDIDATE_WORDS],
            "seed_values": [f"0x{value:04X}" for value in CANDIDATE_VALUES],
            "exact_packet_locality_in_every_context": True,
        },
        "lanes": lanes,
        "conclusion": (
            "Across all eight physical-voice/logical-track mappings, seeding only words "
            "67/68 changes exactly DSPI1 packet words 67/68 against independently executed "
            "stock baselines in every public machine/state context."
        ),
        "next_target": (
            "Resolve the surrounding record geometry and all non-callback initialization "
            "before treating words 67/68 as a transport candidate."
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
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = probe(args.main_image, args.emulator, args.jobs)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
