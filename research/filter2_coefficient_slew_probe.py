#!/usr/bin/env python3
"""Validate 7-bit Filter 2 control mapping and per-sample coefficient slew."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, RETURN_PC, prepared_machine
from filter2_lfo2_state_canary_probe import STATE_BASE, build_candidate as build_state_candidate, image_offset
from filter2_q31_coefficient_probe import q31_multiply
from filter2_half_kernel_probe import sat32, signed32
from filter2_unity_kernel_probe import (
    ACTIVE_INPUT_WORD,
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
    words_hash,
)
from trigger_queue_probe import load_emulator

FLAG_FILTER2 = 1
CURRENT_COEFFICIENT = FILTER2_STATE0 + 8
TARGET_COEFFICIENT = FILTER2_STATE0 + 12
KERNEL_ENTRY = 0x402B435A
COMMON_RESTORE = 0x402B43C2
MULTIPLY_HELPER = 0x402B43C6

SLEW_KERNEL_BODY = bytes.fromhex(
    "4eb940117f00"      # JSR stock ingress
    "40e7"              # MOVE.W SR,-(SP)
    "4ab9402b4408"      # TST.L flags
    "6772"              # BEQ common restore
    "08390000402b440d"  # BTST #0, lane-mask low byte
    "6768"              # BEQ common restore
    "48e7ffc0"          # MOVEM.L D0-D7/A0-A1,-(SP)
    "41f9800067f8"      # LEA lane 0,A0
    "43f9402b4420"      # LEA Filter 2 state slot 0,A1
    "7020"              # MOVEQ #32,D0
    "28290008"          # MOVE.L 8(A1),D4           current coefficient
    "2a29000c"          # MOVE.L 12(A1),D5          target coefficient
    "2c05"              # MOVE.L D5,D6
    "9c84"              # SUB.L D4,D6              target-current
    "4c86"              # SATS D6
    "4a86"              # TST.L D6
    "6a06"              # BPL positive/zero division
    "06860000001f"      # ADDI.L #31,D6            negative truncation bias
    "7e05"              # MOVEQ #5,D7
    "eea6"              # ASR.L D7,D6             signed delta/32
    "d886"              # loop: ADD.L D6,D4        next coefficient
    "4c84"              # SATS D4
    "2210"              # MOVE.L (A0),D1           x
    "2411"              # MOVE.L (A1),D2           s1
    "9282"              # SUB.L D2,D1
    "4c81"              # SATS D1
    "6130"              # BSR Q1.31 multiply helper
    "d481"              # ADD.L D1,D2
    "4c82"              # SATS D2
    "2282"              # MOVE.L D2,(A1)
    "26290004"          # MOVE.L 4(A1),D3          s2
    "2202"              # MOVE.L D2,D1
    "9283"              # SUB.L D3,D1
    "4c81"              # SATS D1
    "611e"              # BSR Q1.31 multiply helper
    "d681"              # ADD.L D1,D3
    "4c83"              # SATS D3
    "23430004"          # MOVE.L D3,4(A1)
    "20c3"              # MOVE.L D3,(A0)+
    "5380"              # SUBQ.L #1,D0
    "66d2"              # BNE loop
    "2a29000c"          # MOVE.L 12(A1),D5
    "23450008"          # MOVE.L D5,8(A1)          snap stored current to target
    "4cdf03ff"          # MOVEM.L (SP)+,D0-D7/A0-A1
    "46df"              # MOVE.W (SP)+,SR
    "4e75"              # RTS
    "4c041c05"          # helper: MULS.L D4,D1:D5
    "7e1f"              # MOVEQ #31,D7
    "eea9"              # LSR.L D7,D1
    "da85"              # ADD.L D5,D5
    "8285"              # OR.L D5,D1
    "4e75"              # RTS
)


def control_to_q31(control: int) -> int:
    if not 0 <= control <= 127:
        raise ValueError("Filter 2 shadow control must be 0..127")
    return (control * 0x7FFFFFFF + 63) // 127


def trunc_div32(value: int) -> int:
    return (abs(value) // 32) * (1 if value >= 0 else -1)


def oracle(
    words: list[int], current: int, target: int, state1: int = 0, state2: int = 0
) -> tuple[list[int], int, int, list[int]]:
    step = trunc_div32(target - current)
    s1, s2 = signed32(state1), signed32(state2)
    coefficients = []
    output = []
    coefficient = current
    for raw in words:
        coefficient += step
        coefficients.append(coefficient)
        value = signed32(raw)
        difference1 = sat32(value - s1)
        s1 = sat32(s1 + q31_multiply(difference1 & 0xFFFFFFFF, coefficient))
        difference2 = sat32(s1 - s2)
        s2 = sat32(s2 + q31_multiply(difference2 & 0xFFFFFFFF, coefficient))
        output.append(s2 & 0xFFFFFFFF)
    return output, s1 & 0xFFFFFFFF, s2 & 0xFFFFFFFF, coefficients


def build_kernel(stock: bytes, armed: bool) -> tuple[bytes, dict]:
    image, state = build_state_candidate(stock)
    candidate = bytearray(image)
    candidate[image_offset(CAVE) : image_offset(CAVE) + len(SLEW_KERNEL_BODY)] = SLEW_KERNEL_BODY
    if armed:
        candidate[image_offset(FLAGS_ADDRESS) : image_offset(FLAGS_ADDRESS) + 4] = FLAG_FILTER2.to_bytes(4, "big")
        candidate[image_offset(FILTER2_MASK_ADDRESS) : image_offset(FILTER2_MASK_ADDRESS) + 2] = LANE0_MASK.to_bytes(2, "big")
    return bytes(candidate), {
        "state": state,
        "kernel_address": f"0x{CAVE:08X}",
        "kernel_end_exclusive": f"0x{CAVE + len(SLEW_KERNEL_BODY):08X}",
        "kernel_bytes": len(SLEW_KERNEL_BODY),
        "kernel_body": SLEW_KERNEL_BODY.hex(),
        "flags": FLAG_FILTER2 if armed else 0,
        "filter2_lane_mask": LANE0_MASK if armed else 0,
        "changed_byte_positions": sum(left != right for left, right in zip(stock, candidate)),
    }


def direct_ramp(
    module, image_path: Path, words: list[int], current_control: int, target_control: int
) -> dict:
    current = control_to_q31(current_control)
    target = control_to_q31(target_control)
    bus = module.Bus()
    bus.load_main(image_path)
    bus.write(CURRENT_COEFFICIENT, 4, current)
    bus.write(TARGET_COEFFICIENT, 4, target)
    for index, value in enumerate(words):
        bus.write(OUTPUT_PLANE + 4 * index, 4, value)
    cpu = module.CPU(bus)
    cpu.a[7] = module.INITIAL_SP
    cpu.pushl(RETURN_PC)
    cpu.a[7] = (cpu.a[7] - 2) & 0xFFFFFFFF
    bus.write(cpu.a[7], 2, cpu.sr)
    cpu.pc = KERNEL_ENTRY
    observed_coefficients = []
    start = cpu.steps
    for _ in range(5_000):
        if cpu.pc == RETURN_PC:
            break
        if cpu.pc == MULTIPLY_HELPER:
            observed_coefficients.append(cpu.d[4])
        cpu.step()
    else:
        raise ValueError("direct coefficient-slew vector did not return")

    expected, s1, s2, coefficients = oracle(words, current, target)
    actual = lane_words(bus)
    paired = [observed_coefficients[index] for index in range(0, len(observed_coefficients), 2)]
    if (
        actual != expected
        or len(observed_coefficients) != 64
        or paired != coefficients
        or any(observed_coefficients[2 * index] != observed_coefficients[2 * index + 1] for index in range(32))
        or bus.read(CURRENT_COEFFICIENT, 4) != target
        or bus.read(FILTER2_STATE0, 4) != s1
        or bus.read(FILTER2_STATE0 + 4, 4) != s2
    ):
        raise ValueError("target coefficient slew diverged from oracle")
    deltas = [paired[0] - current] + [paired[index] - paired[index - 1] for index in range(1, 32)]
    monotonic = all(delta >= 0 for delta in deltas) if target >= current else all(delta <= 0 for delta in deltas)
    if not monotonic:
        raise ValueError("coefficient ramp is not monotonic")
    return {
        "control": {"from": current_control, "to": target_control},
        "q31": {"from": f"0x{current:08X}", "to": f"0x{target:08X}"},
        "instructions": cpu.steps - start,
        "multiply_calls": len(observed_coefficients),
        "first_applied": f"0x{paired[0]:08X}",
        "last_applied": f"0x{paired[-1]:08X}",
        "stored_after_block": f"0x{bus.read(CURRENT_COEFFICIENT, 4):08X}",
        "per_sample_step": deltas[0],
        "monotonic": monotonic,
        "full_target_jump_avoided_at_first_sample": abs(deltas[0]) < abs(target - current) if target != current else True,
        "input_sha256": words_hash(words),
        "output_sha256": words_hash(actual),
        "oracle_match": True,
    }


def callback_cpu(module, bus, baseline: dict):
    cpu = module.CPU(bus)
    cpu.d = baseline["d"].copy()
    cpu.a = baseline["a"].copy()
    cpu.sr = baseline["sr"]
    cpu.ctrl = baseline["ctrl"].copy()
    cpu.macsr = baseline["macsr"]
    cpu.mac_mask = baseline["mac_mask"]
    cpu.macc = baseline["macc"].copy()
    return cpu


def callback_ramp(bus, cpu, control: int, prior: tuple[int, int, int]) -> tuple[dict, tuple[int, int, int]]:
    prior_s1, prior_s2, prior_coefficient = prior
    target = control_to_q31(control)
    bus.write(TARGET_COEFFICIENT, 4, target)
    install_input(bus, True)
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    before = None
    after = None
    observed = []
    start = cpu.steps
    for _ in range(100_000):
        if cpu.pc == MIXER:
            break
        if cpu.pc == CAVE + 6:
            before = lane_words(bus)
        if cpu.pc == COMMON_RESTORE:
            after = lane_words(bus)
        if cpu.pc == MULTIPLY_HELPER:
            observed.append(cpu.d[4])
        cpu.step()
    else:
        raise ValueError("slewed callback did not reach mixer")
    if before is None or after is None:
        raise ValueError("slewed callback checkpoints not reached")
    expected, s1, s2, coefficients = oracle(before, prior_coefficient, target, prior_s1, prior_s2)
    paired = observed[::2]
    actual_state = (bus.read(FILTER2_STATE0, 4), bus.read(FILTER2_STATE0 + 4, 4))
    current = bus.read(CURRENT_COEFFICIENT, 4)
    if after != expected or paired != coefficients or actual_state != (s1, s2) or current != target:
        raise ValueError("slewed callback state/output diverged from oracle")
    result = {
        "control_target": control,
        "instructions_to_mixer": cpu.steps - start,
        "starting_coefficient": f"0x{prior_coefficient:08X}",
        "target_coefficient": f"0x{target:08X}",
        "first_applied_coefficient": f"0x{paired[0]:08X}",
        "last_applied_coefficient": f"0x{paired[-1]:08X}",
        "ending_stored_coefficient": f"0x{current:08X}",
        "ending_state": [f"0x{s1:08X}", f"0x{s2:08X}"],
        "oracle_match": True,
    }
    return result, (s1, s2, current)


def consecutive_callbacks(module, image_path: Path, stock: bytes) -> list[dict]:
    bus, prepared, _ = prepared_machine(module, image_path)
    install_tables(bus, stock)
    baseline = {
        "d": prepared.d.copy(), "a": prepared.a.copy(), "sr": prepared.sr,
        "ctrl": prepared.ctrl.copy(), "macsr": prepared.macsr,
        "mac_mask": prepared.mac_mask, "macc": prepared.macc.copy(),
    }
    state = (0, 0, 0)
    results = []
    for control in (16, 112, 40, 96, 0):
        result, state = callback_ramp(bus, callback_cpu(module, bus, baseline), control, state)
        results.append(result)
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

    try:
        with tempfile.NamedTemporaryFile(suffix=".bin") as armed_temp:
            armed_path = Path(armed_temp.name)
            armed_path.write_bytes(armed)
            impulse = [0x40000000] + [0] * 31
            alternating = [value for _ in range(16) for value in (0x7FFFFFFF, 0x80000000)]
            ramps = [
                direct_ramp(module, armed_path, impulse, 0, 127),
                direct_ramp(module, armed_path, alternating, 127, 0),
                direct_ramp(module, armed_path, [0x20000000] * 32, 32, 96),
                direct_ramp(module, armed_path, [0xE0000000] * 32, 96, 32),
                direct_ramp(module, armed_path, impulse, 64, 64),
            ]
            callbacks = consecutive_callbacks(module, armed_path, stock)

            # The retained image must remain bit-identical when its enable flags are zero.
            disabled_checks = []
            from filter2_bypass_canary_probe import execute_vector
            for active in (False, True):
                stock_run = execute_vector(module, stock_path, stock, active, False)
                disabled_run = execute_vector(module, disabled_path, stock, active, True)
                fields = (
                    "mixer_entry_registers", "mixer_input_sha256", "ingress_output_sha256",
                    "mixer_instructions", "mixer_output_writes", "mixer_output_sha256",
                )
                equality = {field: stock_run[field] == disabled_run[field] for field in fields}
                if not all(equality.values()):
                    raise ValueError("disabled slew candidate diverged from stock")
                disabled_checks.append({"input": "active" if active else "zero", "bit_identical": equality})
    finally:
        if disabled_temp is not None:
            disabled_temp.close()

    mapping = [{"control": value, "q31": f"0x{control_to_q31(value):08X}"} for value in (0, 1, 16, 32, 64, 96, 126, 127)]
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
        "control_contract": {
            "shadow_control_domain": "0..127",
            "mapping": "round(control * 0x7FFFFFFF / 127)",
            "current_coefficient_address": f"0x{CURRENT_COEFFICIENT:08X}",
            "target_coefficient_address": f"0x{TARGET_COEFFICIENT:08X}",
            "slew_samples": 32,
            "mapping_points": mapping,
        },
        "direct_ramps": ramps,
        "consecutive_callback_ramps": callbacks,
        "disabled_stock_equivalence": disabled_checks,
        "conclusion": (
            "A monotonic 7-bit shadow control now maps to the full nonnegative Q1.31 "
            "coefficient domain. Target changes are distributed across all 32 samples, "
            "with only the discarded division remainder committed after the block. Up, "
            "down, stationary and consecutive-callback ramps match the independent oracle."
        ),
        "scope_limit": (
            "This proves the shadow-control ABI and per-sample slew, not a final musical "
            "cutoff curve or attachment to an existing front-panel parameter. Only lane 0 "
            "is processed and hardware cycle margin remains unmeasured."
        ),
        "next_target": (
            "Replicate the coefficient/state loop across all eight lanes using the existing "
            "32-byte per-lane slots, verify lane isolation and worst-case modeled cost, then "
            "select the safest stock control publication point for the new Filter 2 value."
        ),
        "safety": (
            "Only the default-disabled decompressed MAIN candidate is retained. Armed slew "
            "tests were temporary; no ELE3 container or SysEx was built."
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
