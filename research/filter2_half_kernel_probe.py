#!/usr/bin/env python3
"""Validate the first non-unity target-side Filter 2 kernel.

The armed test path is a two-pole cascade of signed-saturating half-step
smoothers.  The retained decompressed MAIN image keeps all enable flags zero.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections import Counter
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256, RETURN_PC
from filter2_bypass_canary_probe import execute_vector
from filter2_lfo2_state_canary_probe import STATE_BASE, build_candidate as build_state_candidate, image_offset
from filter2_unity_kernel_probe import (
    ACTIVE_INPUT_WORD,
    AUDIO_CALLBACK,
    DMA_BLOCK_BYTES,
    EXTERNAL_AUDIO_WINDOW,
    FILTER2_MASK_ADDRESS,
    FILTER2_STATE0,
    FLAGS_ADDRESS,
    LANE0_MASK,
    MIXER,
    OUTPUT_PLANE,
    CAVE,
    install_input,
    install_tables,
    lane_words,
    prepared_machine,
    words_hash,
)
from trigger_queue_probe import load_emulator

FLAG_FILTER2 = 1
KERNEL_ENTRY = 0x402B435A
LOOP_ENTRY = 0x402B436C
COMMON_RESTORE = 0x402B439A
STAGE1_DIFF_SATS = 0x402B4372
STAGE1_STATE_SATS = 0x402B4378
STAGE2_DIFF_SATS = 0x402B4384
STAGE2_STATE_SATS = 0x402B438A
STATE1_STORE = 0x402B437A
STATE2_STORE = 0x402B438C
OUTPUT_STORE = 0x402B4390

HALF_KERNEL_BODY = bytes.fromhex(
    "4eb940117f00"      # JSR stock ingress
    "40e7"              # MOVE.W SR,-(SP)
    "4ab9402b4408"      # TST.L flags
    "674a"              # BEQ common restore
    "08390000402b440d"  # BTST #0, lane-mask low byte
    "6740"              # BEQ common restore
    "48e7f0c0"          # MOVEM.L D0-D3/A0-A1,-(SP)
    "41f9800067f8"      # LEA lane 0,A0
    "43f9402b4420"      # LEA Filter 2 state slot 0,A1
    "7020"              # MOVEQ #32,D0
    "2210"              # loop: MOVE.L (A0),D1        x
    "2411"              # MOVE.L (A1),D2             s1
    "9282"              # SUB.L D2,D1                x-s1
    "4c81"              # SATS D1
    "e281"              # ASR.L #1,D1               half step
    "d481"              # ADD.L D1,D2                new s1
    "4c82"              # SATS D2
    "2282"              # MOVE.L D2,(A1)
    "26290004"          # MOVE.L 4(A1),D3            s2
    "2202"              # MOVE.L D2,D1
    "9283"              # SUB.L D3,D1                s1-s2
    "4c81"              # SATS D1
    "e281"              # ASR.L #1,D1               half step
    "d681"              # ADD.L D1,D3                new s2
    "4c83"              # SATS D3
    "23430004"          # MOVE.L D3,4(A1)
    "20c3"              # MOVE.L D3,(A0)+            y=s2
    "5380"              # SUBQ.L #1,D0
    "66d6"              # BNE loop
    "4cdf030f"          # MOVEM.L (SP)+,D0-D3/A0-A1
    "46df"              # MOVE.W (SP)+,SR
    "4e75"              # RTS
)


def signed32(value: int) -> int:
    return value - 0x1_0000_0000 if value & 0x8000_0000 else value


def sat32(value: int) -> int:
    return max(-0x8000_0000, min(0x7FFF_FFFF, value))


def oracle(words: list[int], state1: int = 0, state2: int = 0) -> tuple[list[int], int, int]:
    output = []
    s1, s2 = signed32(state1), signed32(state2)
    for raw in words:
        value = signed32(raw)
        difference1 = sat32(value - s1)
        s1 = sat32(s1 + (difference1 >> 1))
        difference2 = sat32(s1 - s2)
        s2 = sat32(s2 + (difference2 >> 1))
        output.append(s2 & 0xFFFFFFFF)
    return output, s1 & 0xFFFFFFFF, s2 & 0xFFFFFFFF


def build_kernel(stock: bytes, armed: bool) -> tuple[bytes, dict]:
    image, state = build_state_candidate(stock)
    candidate = bytearray(image)
    candidate[image_offset(CAVE) : image_offset(CAVE) + len(HALF_KERNEL_BODY)] = HALF_KERNEL_BODY
    if armed:
        candidate[image_offset(FLAGS_ADDRESS) : image_offset(FLAGS_ADDRESS) + 4] = FLAG_FILTER2.to_bytes(4, "big")
        candidate[image_offset(FILTER2_MASK_ADDRESS) : image_offset(FILTER2_MASK_ADDRESS) + 2] = LANE0_MASK.to_bytes(2, "big")
    return bytes(candidate), {
        "state": state,
        "kernel_address": f"0x{CAVE:08X}",
        "kernel_end_exclusive": f"0x{CAVE + len(HALF_KERNEL_BODY):08X}",
        "kernel_bytes": len(HALF_KERNEL_BODY),
        "kernel_body": HALF_KERNEL_BODY.hex(),
        "flags": FLAG_FILTER2 if armed else 0,
        "filter2_lane_mask": LANE0_MASK if armed else 0,
        "changed_byte_positions": sum(left != right for left, right in zip(stock, candidate)),
    }


def run_direct_vector(module, image_path: Path, words: list[int]) -> dict:
    bus = module.Bus()
    bus.load_main(image_path)
    cpu = module.CPU(bus)
    for index, value in enumerate(words):
        bus.write(OUTPUT_PLANE + 4 * index, 4, value)
    cpu.a[7] = module.INITIAL_SP
    cpu.pushl(RETURN_PC)
    cpu.a[7] = (cpu.a[7] - 2) & 0xFFFFFFFF
    bus.write(cpu.a[7], 2, cpu.sr)
    cpu.pc = KERNEL_ENTRY
    start = cpu.steps
    for _ in range(2_000):
        if cpu.pc == RETURN_PC:
            break
        cpu.step()
    else:
        raise ValueError("direct half-kernel vector did not return")
    actual = lane_words(bus)
    expected, expected_s1, expected_s2 = oracle(words)
    actual_state = [bus.read(FILTER2_STATE0 + offset, 4) for offset in (0, 4)]
    if actual != expected or actual_state != [expected_s1, expected_s2]:
        raise ValueError("target half-kernel vector diverged from fixed-point oracle")
    return {
        "instructions": cpu.steps - start,
        "input_sha256": words_hash(words),
        "output_sha256": words_hash(actual),
        "first_four_output": [f"0x{value:08X}" for value in actual[:4]],
        "final_state": [f"0x{value:08X}" for value in actual_state],
        "oracle_match": True,
    }


def trace_callback(module, image_path: Path, stock: bytes, active: bool) -> dict:
    bus, cpu, _ = prepared_machine(module, image_path)
    install_tables(bus, stock)
    install_input(bus, active)
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    start = cpu.steps
    before = None
    after = None
    visits = Counter()
    writes = []
    original_write = bus.write

    def traced_write(address: int, size: int, value: int) -> None:
        if CAVE <= cpu.pc < CAVE + len(HALF_KERNEL_BODY):
            writes.append((cpu.pc, address, size, value & 0xFFFFFFFF))
        original_write(address, size, value)

    bus.write = traced_write
    try:
        for _ in range(100_000):
            if cpu.pc == MIXER:
                break
            if CAVE <= cpu.pc < CAVE + len(HALF_KERNEL_BODY):
                visits[cpu.pc] += 1
            if cpu.pc == CAVE + 6:
                before = lane_words(bus)
            if cpu.pc == COMMON_RESTORE:
                after = lane_words(bus)
            cpu.step()
        else:
            raise ValueError("half-kernel callback did not reach mixer")
    finally:
        bus.write = original_write
    if before is None or after is None:
        raise ValueError("half-kernel callback checkpoints were not reached")

    entered = visits[KERNEL_ENTRY] == 1
    expected = before if not entered else oracle(before)[0]
    if after != expected:
        raise ValueError("half-kernel callback diverged from fixed-point oracle")
    plane_writes = [event for event in writes if OUTPUT_PLANE <= event[1] < OUTPUT_PLANE + 128]
    state_writes = [event for event in writes if FILTER2_STATE0 <= event[1] < FILTER2_STATE0 + 8]
    if entered:
        if visits[LOOP_ENTRY] != 32 or len(plane_writes) != 32 or len(state_writes) != 64:
            raise ValueError("half-kernel callback geometry changed")
        for pc in (STAGE1_DIFF_SATS, STAGE1_STATE_SATS, STAGE2_DIFF_SATS, STAGE2_STATE_SATS):
            if visits[pc] != 32:
                raise ValueError(f"saturation stage 0x{pc:08X} did not execute 32 times")
    elif plane_writes or state_writes:
        raise ValueError("disabled half kernel wrote audio or state")
    return {
        "input": "active" if active else "zero",
        "instructions_to_mixer": cpu.steps - start,
        "kernel_entered": entered,
        "loop_iterations": visits[LOOP_ENTRY],
        "sats_executions": sum(visits[pc] for pc in (STAGE1_DIFF_SATS, STAGE1_STATE_SATS, STAGE2_DIFF_SATS, STAGE2_STATE_SATS)),
        "lane0_writes": len(plane_writes),
        "state_writes": len(state_writes),
        "input_sha256": words_hash(before),
        "output_sha256": words_hash(after),
        "oracle_match": after == expected,
        "output_differs_from_input": after != before,
        "final_state": [f"0x{bus.read(FILTER2_STATE0 + offset, 4):08X}" for offset in (0, 4)],
    }


def probe(stock_path: Path, emulator_path: Path, candidate_output: Path | None = None) -> dict:
    stock = stock_path.read_bytes()
    stock_hash = hashlib.sha256(stock).hexdigest()
    if stock_hash != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {stock_hash}")
    disabled, disabled_build = build_kernel(stock, False)
    armed, armed_build = build_kernel(stock, True)
    module = load_emulator(emulator_path)

    disabled_temp = None
    if candidate_output is None:
        disabled_temp = tempfile.NamedTemporaryFile(suffix=".bin")
        disabled_path = Path(disabled_temp.name)
    else:
        disabled_path = candidate_output
    disabled_path.write_bytes(disabled)

    direct_vectors = {}
    traces = {"disabled": [], "armed_half": []}
    comparisons = []
    try:
        with tempfile.NamedTemporaryFile(suffix=".bin") as armed_temp:
            armed_path = Path(armed_temp.name)
            armed_path.write_bytes(armed)
            vectors = {
                "impulse": [0x40000000] + [0] * 31,
                "positive_dc": [0x20000000] * 32,
                "negative_dc": [0xE0000000] * 32,
                "alternating_limits": [value for _ in range(16) for value in (0x7FFFFFFF, 0x80000000)],
            }
            direct_vectors = {name: run_direct_vector(module, armed_path, values) for name, values in vectors.items()}

            for active in (False, True):
                stock_run = execute_vector(module, stock_path, stock, active, False)
                disabled_run = execute_vector(module, disabled_path, stock, active, True)
                armed_run = execute_vector(module, armed_path, stock, active, True)
                fields = (
                    "mixer_entry_registers", "mixer_input_sha256", "ingress_output_sha256",
                    "mixer_instructions", "mixer_output_writes", "mixer_output_sha256",
                )
                disabled_equal = {field: stock_run[field] == disabled_run[field] for field in fields}
                armed_equal = {field: stock_run[field] == armed_run[field] for field in fields}
                if not all(disabled_equal.values()):
                    raise ValueError("disabled half-kernel candidate diverged from stock")
                if not armed_equal["mixer_entry_registers"] or not armed_equal["mixer_instructions"] or not armed_equal["mixer_output_writes"]:
                    raise ValueError("armed half kernel damaged control or mixer execution state")
                if not active and not all(armed_equal.values()):
                    raise ValueError("armed half kernel changed the zero vector")
                if active and armed_equal["mixer_input_sha256"]:
                    raise ValueError("armed half kernel failed to produce a non-unity mixer input")
                comparisons.append({
                    "input": "active" if active else "zero",
                    "disabled_equals_stock": disabled_equal,
                    "armed_equals_stock": armed_equal,
                })
                traces["disabled"].append(trace_callback(module, disabled_path, stock, active))
                traces["armed_half"].append(trace_callback(module, armed_path, stock, active))
    finally:
        if disabled_temp is not None:
            disabled_temp.close()

    return {
        "result": "PASS",
        "stock": {"path": str(stock_path), "sha256": stock_hash},
        "disabled_candidate": {
            "path": str(candidate_output) if candidate_output else "temporary execution image",
            "sha256": hashlib.sha256(disabled).hexdigest(),
            **disabled_build,
        },
        "armed_emulation_probe": {
            "sha256": hashlib.sha256(armed).hexdigest(),
            **armed_build,
            "artifact_retained": False,
        },
        "kernel_contract": {
            "lane": 0,
            "samples_per_callback": 32,
            "input_format": "signed Q1.31-domain longwords",
            "stage_equation": "state=sat32(state + (sat32(input-state) >> 1))",
            "cascade_stages": 2,
            "fixed_step_coefficient": "1/2 (exact arithmetic shift)",
            "response": "non-unity two-pole smoothing/low-pass",
        },
        "direct_oracle_vectors": direct_vectors,
        "callback_traces": traces,
        "stock_comparisons": comparisons,
        "conclusion": (
            "The first non-unity target-side Filter 2 executes over lane 0 and matches "
            "the host signed-saturating oracle for impulse, positive DC, negative DC, "
            "alternating-limit, zero-callback, and active-callback vectors. Its active "
            "response changes the mixer input as intended while preserving register/SR "
            "state and normal mixer execution. The retained image remains disabled."
        ),
        "scope_limit": (
            "The step coefficient is fixed at exactly one half via arithmetic shift. "
            "Programmable cutoff still requires a general fixed-point multiply, and real "
            "processor-cycle margin remains a hardware measurement gate."
        ),
        "next_target": (
            "Replace the fixed half-step with a coefficient loaded from the Filter 2 state "
            "slot and a verified 32x32-to-Q1.31 multiply, then sweep coefficient endpoints "
            "and state continuity across consecutive callbacks."
        ),
        "safety": (
            "Only the default-disabled decompressed MAIN candidate is retained. The armed "
            "audible kernel existed only temporarily; no ELE3 container or SysEx was built."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_main", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument("--candidate-output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = probe(args.stock_main, args.emulator, args.candidate_output)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.report:
        args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
