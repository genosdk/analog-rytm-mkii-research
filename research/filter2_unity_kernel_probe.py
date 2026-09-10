#!/usr/bin/env python3
"""Build and execute a lane-0 two-stage unity Filter 2 kernel.

The retained image is disabled by default.  A temporary armed image runs all
32 lane-0 Q1.31-domain words through two explicit signed-saturating identity
stages, updates two state words, and must remain bit-identical to stock.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections import Counter
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, RETURN_PC, prepared_machine
from filter2_bypass_canary_probe import execute_vector
from filter2_lfo2_state_canary_probe import STATE_BASE, build_candidate as build_state_candidate, image_offset
from post_voice_ingress_probe import (
    ACTIVE_INPUT_WORD,
    DMA_BLOCK_BYTES,
    EXTERNAL_AUDIO_WINDOW,
    OUTPUT_PLANE,
    install_sample_levels,
    install_tables,
)
from trigger_queue_probe import load_emulator

CAVE = 0x402B4340
INGRESS = 0x40117F00
MIXER = 0x4010A2E0
FLAGS_ADDRESS = STATE_BASE + 8
FILTER2_MASK_ADDRESS = STATE_BASE + 12
FILTER2_MASK_BIT0_ADDRESS = FILTER2_MASK_ADDRESS + 1
FILTER2_STATE0 = STATE_BASE + 32
FLAG_FILTER2 = 1
LANE0_MASK = 1

KERNEL_ENTRY = 0x402B435A
LOOP_ENTRY = 0x402B436E
STAGE1_SATS = 0x402B4376
STAGE2_SATS = 0x402B4382
OUTPUT_STORE = 0x402B4384
COMMON_RESTORE = 0x402B438E

UNITY_KERNEL_BODY = bytes.fromhex(
    "4eb940117f00"      # JSR stock ingress
    "40e7"              # MOVE.W SR,-(SP)
    "4ab9402b4408"      # TST.L flags
    "673e"              # BEQ common restore
    "08390000402b440d"  # BTST #0, lane-mask low byte
    "6734"              # BEQ common restore
    "48e7f0c0"          # MOVEM.L D0-D3/A0-A1,-(SP)
    "41f9800067f8"      # LEA lane 0,A0
    "43f9402b4420"      # LEA Filter 2 state slot 0,A1
    "7020"              # MOVEQ #32,D0
    "7600"              # MOVEQ #0,D3 (unity residual)
    "2210"              # loop: MOVE.L (A0),D1
    "2281"              # MOVE.L D1,(A1)       stage 1 state
    "2411"              # MOVE.L (A1),D2
    "d483"              # ADD.L D3,D2
    "4c82"              # SATS D2              stage 1 clamp
    "23420004"          # MOVE.L D2,4(A1)      stage 2 state
    "22290004"          # MOVE.L 4(A1),D1
    "d283"              # ADD.L D3,D1
    "4c81"              # SATS D1              stage 2 clamp
    "20c1"              # MOVE.L D1,(A0)+
    "5380"              # SUBQ.L #1,D0
    "66e4"              # BNE loop
    "4cdf030f"          # MOVEM.L (SP)+,D0-D3/A0-A1
    "46df"              # MOVE.W (SP)+,SR
    "4e75"              # RTS
)


def build_kernel(stock: bytes, armed: bool) -> tuple[bytes, dict]:
    image, state = build_state_candidate(stock)
    candidate = bytearray(image)
    candidate[image_offset(CAVE) : image_offset(CAVE) + len(UNITY_KERNEL_BODY)] = UNITY_KERNEL_BODY
    if armed:
        candidate[image_offset(FLAGS_ADDRESS) : image_offset(FLAGS_ADDRESS) + 4] = FLAG_FILTER2.to_bytes(4, "big")
        candidate[image_offset(FILTER2_MASK_ADDRESS) : image_offset(FILTER2_MASK_ADDRESS) + 2] = LANE0_MASK.to_bytes(2, "big")
    return bytes(candidate), {
        "state": state,
        "kernel_address": f"0x{CAVE:08X}",
        "kernel_end_exclusive": f"0x{CAVE + len(UNITY_KERNEL_BODY):08X}",
        "kernel_bytes": len(UNITY_KERNEL_BODY),
        "kernel_body": UNITY_KERNEL_BODY.hex(),
        "flags": FLAG_FILTER2 if armed else 0,
        "filter2_lane_mask": LANE0_MASK if armed else 0,
        "changed_byte_positions": sum(left != right for left, right in zip(stock, candidate)),
    }


def install_input(bus, active: bool) -> None:
    payload = ACTIVE_INPUT_WORD.to_bytes(4, "big") * 36 if active else bytes(DMA_BLOCK_BYTES)
    for index, value in enumerate(payload):
        bus.write(EXTERNAL_AUDIO_WINDOW + index, 1, value)


def lane_words(bus) -> list[int]:
    return [bus.read(OUTPUT_PLANE + 4 * index, 4) for index in range(32)]


def words_hash(words: list[int]) -> str:
    return hashlib.sha256(b"".join(value.to_bytes(4, "big") for value in words)).hexdigest()


def trace_kernel(module, image_path: Path, stock: bytes, active: bool) -> dict:
    bus, cpu, _ = prepared_machine(module, image_path)
    install_tables(bus, stock)
    install_input(bus, active)
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    start = cpu.steps
    ingress_words = None
    completed_words = None
    cave_visits = Counter()
    plane_writes = []
    state_writes = []
    original_write = bus.write

    def traced_write(address: int, size: int, value: int) -> None:
        if CAVE <= cpu.pc < CAVE + len(UNITY_KERNEL_BODY):
            if OUTPUT_PLANE <= address < OUTPUT_PLANE + 32 * 4:
                plane_writes.append((cpu.pc, address, size, value & 0xFFFFFFFF))
            if FILTER2_STATE0 <= address < FILTER2_STATE0 + 8:
                state_writes.append((cpu.pc, address, size, value & 0xFFFFFFFF))
        original_write(address, size, value)

    bus.write = traced_write
    try:
        for _ in range(100_000):
            if cpu.pc == MIXER:
                break
            if cpu.pc == INGRESS:
                install_sample_levels(bus)
            if CAVE <= cpu.pc < CAVE + len(UNITY_KERNEL_BODY):
                cave_visits[cpu.pc] += 1
            if cpu.pc == CAVE + 6:
                ingress_words = lane_words(bus)
            if cpu.pc == COMMON_RESTORE:
                completed_words = lane_words(bus)
            cpu.step()
        else:
            raise ValueError("unity-kernel candidate did not reach mixer")
    finally:
        bus.write = original_write

    if ingress_words is None or completed_words is None:
        raise ValueError("unity-kernel checkpoints were not reached")
    if ingress_words != completed_words:
        raise ValueError("unity kernel changed a lane-0 word")
    entered = cave_visits[KERNEL_ENTRY] == 1
    if entered:
        expected_addresses = [OUTPUT_PLANE + 4 * index for index in range(32)]
        if [event[1] for event in plane_writes] != expected_addresses:
            raise ValueError("unity kernel did not write exactly 32 sequential lane-0 words")
        if cave_visits[LOOP_ENTRY] != 32:
            raise ValueError("unity kernel loop count changed")
        if cave_visits[STAGE1_SATS] != 32 or cave_visits[STAGE2_SATS] != 32:
            raise ValueError("unity kernel did not execute both saturation stages 32 times")
        if Counter(event[1] for event in state_writes) != Counter({FILTER2_STATE0: 32, FILTER2_STATE0 + 4: 32}):
            raise ValueError("unity kernel state-write geometry changed")
        if bus.read(FILTER2_STATE0, 4) != ingress_words[-1] or bus.read(FILTER2_STATE0 + 4, 4) != ingress_words[-1]:
            raise ValueError("unity kernel final state does not equal final lane sample")
    elif plane_writes or state_writes:
        raise ValueError("disabled unity kernel wrote audio or state")

    return {
        "input": "active" if active else "zero",
        "instructions_to_mixer": cpu.steps - start,
        "kernel_entered": entered,
        "lane0_words": 32,
        "lane0_before_sha256": words_hash(ingress_words),
        "lane0_after_sha256": words_hash(completed_words),
        "lane0_bit_identical": ingress_words == completed_words,
        "loop_iterations": cave_visits[LOOP_ENTRY],
        "stage1_sats_executions": cave_visits[STAGE1_SATS],
        "stage2_sats_executions": cave_visits[STAGE2_SATS],
        "lane0_writes": len(plane_writes),
        "state_writes": len(state_writes),
        "final_state": [f"0x{bus.read(FILTER2_STATE0 + offset, 4):08X}" for offset in (0, 4)],
        "final_input_word": f"0x{ingress_words[-1]:08X}",
    }


def saturation_boundaries(module, image_path: Path) -> list[dict]:
    cases = [
        ("positive_overflow", 0x7FFFFFFF, 0x00000001, 0x7FFFFFFF),
        ("negative_overflow", 0x80000000, 0xFFFFFFFF, 0x80000000),
    ]
    results = []
    for name, value, residual, expected in cases:
        bus = module.Bus()
        bus.load_main(image_path)
        cpu = module.CPU(bus)
        cpu.pc = STAGE1_SATS - 2  # ADD.L D3,D2 immediately before SATS D2
        cpu.d[2] = value
        cpu.d[3] = residual
        if cpu.step() != "ADD" or cpu.step() != "SATS D2":
            raise ValueError("unity-kernel saturation instruction sequence changed")
        if cpu.d[2] != expected:
            raise ValueError(f"{name}: expected 0x{expected:08X}, got 0x{cpu.d[2]:08X}")
        results.append({
            "case": name,
            "input": f"0x{value:08X}",
            "residual": f"0x{residual:08X}",
            "saturated": f"0x{cpu.d[2]:08X}",
        })
    return results


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

    traces = {"disabled": [], "armed_unity": []}
    comparisons = []
    try:
        with tempfile.NamedTemporaryFile(suffix=".bin") as armed_temp:
            armed_path = Path(armed_temp.name)
            armed_path.write_bytes(armed)
            boundaries = saturation_boundaries(module, armed_path)
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
                if not all(disabled_equal.values()) or not all(armed_equal.values()):
                    raise ValueError("unity kernel changed stock-visible mixer state")
                comparisons.append({
                    "input": "active" if active else "zero",
                    "disabled_equals_stock": disabled_equal,
                    "armed_unity_equals_stock": armed_equal,
                })
                disabled_trace = trace_kernel(module, disabled_path, stock, active)
                armed_trace = trace_kernel(module, armed_path, stock, active)
                if disabled_trace["kernel_entered"] or not armed_trace["kernel_entered"]:
                    raise ValueError("unity-kernel dispatch state is inverted")
                traces["disabled"].append(disabled_trace)
                traces["armed_unity"].append(armed_trace)
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
            "input_format": "signed Q1.31-domain longwords",
            "lane": 0,
            "samples_per_callback": 32,
            "stage_equations": ["s1=sat32(x+0)", "s2=sat32(s1+0)", "y=s2"],
            "unity_transfer": True,
            "state_words_used": 2,
        },
        "saturation_boundary_cases": boundaries,
        "execution_traces": traces,
        "stock_equivalence": comparisons,
        "conclusion": (
            "The armed research path executes a two-stage signed-saturating unity kernel "
            "over all 32 lane-0 words, performs 64 SATS operations, updates two state "
            "words, preserves registers and SR, and remains bit-identical to stock through "
            "the next mixer. The retained candidate is disabled by default."
        ),
        "scope_limit": (
            "Unity uses zero residuals and therefore validates control flow, addressing, "
            "state, saturation instructions, and preservation—not coefficient multiply or "
            "audible filter response. Real processor-cycle margin remains unmeasured."
        ),
        "next_target": (
            "Implement the first non-unity Q1.31 coefficient multiply for the same lane-0 "
            "two-state cascade, validate impulse/DC responses against a host fixed-point "
            "oracle, and keep the retained image disabled by default."
        ),
        "safety": (
            "Only the default-disabled decompressed MAIN candidate is retained. The armed "
            "kernel existed only in a temporary file; no ELE3 container or SysEx was built."
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
