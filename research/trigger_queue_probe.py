#!/usr/bin/env python3
"""Exercise MAIN's authentic trigger record and runtime command queue.

This emulation-only probe reconstructs the stock queue initialization omitted
by the storage-free fixture, submits track 0's real 56-byte trigger record, and
runs complete audio interrupts through their final RTE.  It proves natural
renderer state progression without forcing a renderer case.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from audio_callback_probe import (
    AUDIO_CALLBACK,
    EXPECTED_MAIN_SHA256,
    RETURN_PC,
    call_until,
    prepared_machine,
)

QUEUE_INITIALIZER = 0x40001350
QUEUE_INSTALLER = 0x4011CF58
QUEUE = 0x4192A9D0
QUEUE_RING = 0x419531F8
QUEUE_CAPACITY = 0x400
QUEUE_POINTER_CELL = 0x402782C0
QUEUE_SENTINEL = 0x42F4D850

TRIGGER_RECORD = 0x42AC4038
TRIGGER_FLAGS = TRIGGER_RECORD + 0x14
TRACK_PARAMETER_RECORD = 0x412FAC4B
TRACK_BR_SOURCE = TRACK_PARAMETER_RECORD + 0x14
EVENT_VALUE = 0x8000E508
EVENT_FLAGS = 0x8000E53C

# Channel 30 alternates two 0x110-byte control snapshots at 0x8000DDD0 and
# 0x8000DEE0.  Board/DMA scheduling normally publishes the current half here.
CONTROL_SNAPSHOT_POINTER = 0x8000DDB0
CONTROL_SNAPSHOT_A = 0x8000DDD0

FULL_CALLBACK_STOP = 0x4011CF12  # final RTE; do not execute without an IRQ frame
RENDERER = 0x4010CBA8
VOICE_EVENT_STATE = 0x8000FEF8
VOICE_CONTROL_WORD = 0x80006544
BR_FRAME_WORD = 0x8000F7BE
CASES = {
    0x4010CFD2: 0,
    0x4010D028: 1,
    0x4010D102: 2,
    0x4010D164: 3,
    0x4010D204: 4,
}


def load_emulator(path: Path):
    spec = importlib.util.spec_from_file_location("trigger_queue_minicoldfire", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load emulator module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def stock_call(cpu, address: int, arguments: list[int], limit: int = 300_000) -> int:
    """Call a stock C routine, preserving its normal right-to-left ABI."""
    for value in reversed(arguments):
        cpu.pushl(value)
    steps = call_until(cpu, address, RETURN_PC, limit)
    cpu.a[7] = (cpu.a[7] + 4 * len(arguments)) & 0xFFFFFFFF
    return steps


def run_complete_callback(cpu, base_sp: int) -> dict:
    """Run one interrupt body up to, but not including, its final RTE."""
    cpu.a[7] = base_sp
    cpu.pc = AUDIO_CALLBACK
    cases: list[int] = []
    br_at_renderer: list[int] = []
    start = cpu.steps
    for _ in range(200_000):
        if cpu.pc in CASES:
            cases.append(CASES[cpu.pc])
        if cpu.pc == RENDERER:
            br_at_renderer.append(cpu.bus.read(BR_FRAME_WORD, 2))
        if cpu.pc == FULL_CALLBACK_STOP:
            return {
                "instructions": cpu.steps - start,
                "cases": cases,
                "br_frame_at_renderer": br_at_renderer,
            }
        cpu.step()
    raise ValueError("audio interrupt did not reach its final RTE")


def run_vector(module, main_path: Path, source_br: int) -> dict:
    bus, cpu, _ = prepared_machine(module, main_path)
    base_sp = cpu.a[7]

    # Reproduce the exact startup calls seen at 0x400A02F8..0x400A030E and
    # 0x400A0BCC..0x400A0BD8.
    queue_init_steps = stock_call(
        cpu,
        QUEUE_INITIALIZER,
        [QUEUE, 0, QUEUE_RING, QUEUE_CAPACITY],
    )
    queue_install_steps = stock_call(cpu, QUEUE_INSTALLER, [QUEUE])
    if bus.read(QUEUE_POINTER_CELL, 4) != QUEUE:
        raise ValueError("stock queue installer did not publish the runtime queue")
    if bus.read(QUEUE + 0x10, 4) != QUEUE_CAPACITY - 1:
        raise ValueError("stock queue initializer did not install its ring mask")

    # Publish the channel-30 snapshot half that the board/DMA scheduler normally
    # supplies. This lets the interrupt continue from the mix stage to cleanup.
    bus.write(CONTROL_SNAPSHOT_POINTER, 4, CONTROL_SNAPSHOT_A)

    # Track 0, slot 0: state 1 is a live trigger and flag bit 7 requests the
    # physical-voice reset. The source record is the genuine input copied by
    # stock routine 0x40119316 into the live 0x54-byte parameter records.
    bus.write(TRACK_BR_SOURCE, 4, source_br)
    bus.write(TRIGGER_RECORD, 4, 1)
    bus.write(TRIGGER_FLAGS, 4, 0x80)

    callbacks: list[dict] = []
    for index in range(12):
        item = run_complete_callback(cpu, base_sp)
        item.update({
            "callback": index + 1,
            "event_value_after": bus.read(EVENT_VALUE, 4),
            "voice_state_after": bus.read(VOICE_EVENT_STATE, 4),
            "voice_timer_after": bus.read(VOICE_EVENT_STATE + 4, 4),
            "packed_control_after": f"0x{bus.read(VOICE_CONTROL_WORD, 4):08X}",
        })
        callbacks.append(item)

    command_pointer = bus.read(QUEUE_RING, 4)
    if command_pointer == 0:
        raise ValueError("authentic trigger did not enqueue a command")
    return {
        "source_br": f"0x{source_br:08X}",
        "queue_initialization": {
            "initializer": f"0x{QUEUE_INITIALIZER:08X}",
            "installer": f"0x{QUEUE_INSTALLER:08X}",
            "queue": f"0x{QUEUE:08X}",
            "ring": f"0x{QUEUE_RING:08X}",
            "capacity": QUEUE_CAPACITY,
            "mask": f"0x{bus.read(QUEUE + 0x10, 4):08X}",
            "initializer_instructions": queue_init_steps,
            "installer_instructions": queue_install_steps,
        },
        "queued_command": {
            "pointer": f"0x{command_pointer:08X}",
            "code": bus.read(command_pointer, 1),
            "track_mask": f"0x{bus.read(command_pointer + 4, 4):08X}",
            "queue_count": bus.read(QUEUE + 4, 4),
        },
        "callbacks": callbacks,
        "final_packed_control": f"0x{bus.read(VOICE_CONTROL_WORD, 4):08X}",
    }


def probe(main_path: Path, emulator_path: Path) -> dict:
    digest = hashlib.sha256(main_path.read_bytes()).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    module = load_emulator(emulator_path)
    zero = run_vector(module, main_path, 0)
    high = run_vector(module, main_path, 0x7F000000)

    expected_cases = [[0], [1], [2], [2], [2], [2], [2], [2], [2], [2], [3], [4]]
    for vector in (zero, high):
        if [item["cases"] for item in vector["callbacks"]] != expected_cases:
            raise ValueError("authentic trigger did not follow the bounded state machine")
        if vector["callbacks"][0]["event_value_after"] != 0:
            raise ValueError("full interrupt did not clear the one-shot event")
        command = vector["queued_command"]
        if command["code"] != 0x1F or command["track_mask"] != "0x00000001":
            raise ValueError(f"unexpected trigger command: {command}")

    high_frames = [item["br_frame_at_renderer"][0] for item in high["callbacks"]]
    zero_frames = [item["br_frame_at_renderer"][0] for item in zero["callbacks"]]
    expected_high_frames = [31537] * 12
    if zero_frames != [0] * 12 or high_frames != expected_high_frames:
        raise ValueError("unexpected storage-free BR-frame lifetime")
    if zero["final_packed_control"] == high["final_packed_control"]:
        raise ValueError("sustained BR did not reach the natural case-3 encoder")

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "recovered_runtime_contract": {
            "sentinel_queue": f"0x{QUEUE_SENTINEL:08X}",
            "runtime_queue": f"0x{QUEUE:08X}",
            "queue_ring": f"0x{QUEUE_RING:08X}",
            "queue_capacity": QUEUE_CAPACITY,
            "full_interrupt_stop": f"0x{FULL_CALLBACK_STOP:08X}",
            "one_shot_clear_instruction": "0x4011CC64",
            "track_parameter_record": f"0x{TRACK_PARAMETER_RECORD:08X}",
            "br_source_longword": f"0x{TRACK_BR_SOURCE:08X}",
        },
        "vectors": [zero, high],
        "conclusion": (
            "A stock 56-byte trigger record now reaches the real command queue, is cleared "
            "by the complete interrupt, and advances renderer 0 naturally through cases "
            "0, 1, 2, 3, and 4. The corrected EMAC addressing model preserves the BR "
            "ramp across all twelve callbacks. High BR reaches natural case 3 and leaves "
            "a distinct packed control word; this proves sustained control encoding, not "
            "PCM quantization."
        ),
        "next_target": (
            "Identify the consumer behind peripheral FIFO 0xFC03C034; separately trace "
            "post-hardware-return CPU audio ingress for a Filter 2 insertion boundary."
        ),
        "safety": "Emulation and RAM initialization only; firmware bytes were not modified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
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
