#!/usr/bin/env python3
"""Validate a state-loaded 32x32-to-Q1.31 Filter 2 coefficient kernel."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, RETURN_PC, prepared_machine
from filter2_bypass_canary_probe import execute_vector
from filter2_half_kernel_probe import sat32, signed32
from filter2_lfo2_state_canary_probe import STATE_BASE, build_candidate as build_state_candidate, image_offset
from filter2_unity_kernel_probe import (
    ACTIVE_INPUT_WORD,
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
    words_hash,
)
from trigger_queue_probe import load_emulator

FLAG_FILTER2 = 1
COEFFICIENT_ADDRESS = FILTER2_STATE0 + 8
KERNEL_ENTRY = 0x402B435A
LOOP_ENTRY = 0x402B4370
COMMON_RESTORE = 0x402B439E
MULTIPLY_HELPER = 0x402B43A2

Q31_KERNEL_BODY = bytes.fromhex(
    "4eb940117f00"      # JSR stock ingress
    "40e7"              # MOVE.W SR,-(SP)
    "4ab9402b4408"      # TST.L flags
    "674e"              # BEQ common restore
    "08390000402b440d"  # BTST #0, lane-mask low byte
    "6744"              # BEQ common restore
    "48e7ffc0"          # MOVEM.L D0-D7/A0-A1,-(SP)
    "41f9800067f8"      # LEA lane 0,A0
    "43f9402b4420"      # LEA Filter 2 state slot 0,A1
    "7020"              # MOVEQ #32,D0
    "28290008"          # MOVE.L 8(A1),D4           coefficient
    "2210"              # loop: MOVE.L (A0),D1      x
    "2411"              # MOVE.L (A1),D2           s1
    "9282"              # SUB.L D2,D1              x-s1
    "4c81"              # SATS D1
    "6128"              # BSR Q31 multiply helper
    "d481"              # ADD.L D1,D2
    "4c82"              # SATS D2
    "2282"              # MOVE.L D2,(A1)
    "26290004"          # MOVE.L 4(A1),D3          s2
    "2202"              # MOVE.L D2,D1
    "9283"              # SUB.L D3,D1              s1-s2
    "4c81"              # SATS D1
    "6116"              # BSR Q31 multiply helper
    "d681"              # ADD.L D1,D3
    "4c83"              # SATS D3
    "23430004"          # MOVE.L D3,4(A1)
    "20c3"              # MOVE.L D3,(A0)+          y=s2
    "5380"              # SUBQ.L #1,D0
    "66d6"              # BNE loop
    "4cdf03ff"          # MOVEM.L (SP)+,D0-D7/A0-A1
    "46df"              # MOVE.W (SP)+,SR
    "4e75"              # RTS
    # D1 = signed Q1.31 value, D4 = nonnegative Q1.31 coefficient.
    "2c01"              # helper: MOVE.L D1,D6
    "4c041c05"          # MULS.L D4,D1:D5
    "7e1f"              # MOVEQ #31,D7
    "eea9"              # LSR.L D7,D1             product low >> 31
    "da85"              # ADD.L D5,D5             product high << 1
    "8285"              # OR.L D5,D1              signed product >> 31
    "4e75"              # RTS
)


def q31_multiply(value: int, coefficient: int) -> int:
    if not 0 <= coefficient <= 0x7FFFFFFF:
        raise ValueError("coefficient outside nonnegative Q1.31 range")
    return (signed32(value & 0xFFFFFFFF) * coefficient) >> 31


def oracle(
    words: list[int], coefficient: int, state1: int = 0, state2: int = 0
) -> tuple[list[int], int, int]:
    output = []
    s1, s2 = signed32(state1), signed32(state2)
    for raw in words:
        value = signed32(raw)
        difference1 = sat32(value - s1)
        s1 = sat32(s1 + q31_multiply(difference1 & 0xFFFFFFFF, coefficient))
        difference2 = sat32(s1 - s2)
        s2 = sat32(s2 + q31_multiply(difference2 & 0xFFFFFFFF, coefficient))
        output.append(s2 & 0xFFFFFFFF)
    return output, s1 & 0xFFFFFFFF, s2 & 0xFFFFFFFF


def build_kernel(stock: bytes, armed: bool, coefficient: int = 0) -> tuple[bytes, dict]:
    if not 0 <= coefficient <= 0x7FFFFFFF:
        raise ValueError("coefficient outside nonnegative Q1.31 range")
    image, state = build_state_candidate(stock)
    candidate = bytearray(image)
    candidate[image_offset(CAVE) : image_offset(CAVE) + len(Q31_KERNEL_BODY)] = Q31_KERNEL_BODY
    if armed:
        candidate[image_offset(FLAGS_ADDRESS) : image_offset(FLAGS_ADDRESS) + 4] = FLAG_FILTER2.to_bytes(4, "big")
        candidate[image_offset(FILTER2_MASK_ADDRESS) : image_offset(FILTER2_MASK_ADDRESS) + 2] = LANE0_MASK.to_bytes(2, "big")
        candidate[image_offset(COEFFICIENT_ADDRESS) : image_offset(COEFFICIENT_ADDRESS) + 4] = coefficient.to_bytes(4, "big")
    return bytes(candidate), {
        "state": state,
        "kernel_address": f"0x{CAVE:08X}",
        "kernel_end_exclusive": f"0x{CAVE + len(Q31_KERNEL_BODY):08X}",
        "multiply_helper": f"0x{MULTIPLY_HELPER:08X}",
        "kernel_bytes": len(Q31_KERNEL_BODY),
        "kernel_body": Q31_KERNEL_BODY.hex(),
        "flags": FLAG_FILTER2 if armed else 0,
        "filter2_lane_mask": LANE0_MASK if armed else 0,
        "coefficient": f"0x{coefficient:08X}" if armed else "0x00000000",
        "changed_byte_positions": sum(left != right for left, right in zip(stock, candidate)),
    }


def direct_kernel_vector(module, image: bytes, words: list[int], coefficient: int) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".bin") as temporary:
        path = Path(temporary.name)
        path.write_bytes(image)
        bus = module.Bus()
        bus.load_main(path)
        bus.write(COEFFICIENT_ADDRESS, 4, coefficient)
        for index, value in enumerate(words):
            bus.write(OUTPUT_PLANE + 4 * index, 4, value)
        cpu = module.CPU(bus)
        cpu.a[7] = module.INITIAL_SP
        cpu.pushl(RETURN_PC)
        cpu.a[7] = (cpu.a[7] - 2) & 0xFFFFFFFF
        bus.write(cpu.a[7], 2, cpu.sr)
        cpu.pc = KERNEL_ENTRY
        start = cpu.steps
        multiply_calls = 0
        for _ in range(4_000):
            if cpu.pc == RETURN_PC:
                break
            if cpu.pc == MULTIPLY_HELPER:
                multiply_calls += 1
            cpu.step()
        else:
            raise ValueError("direct Q1.31 kernel vector did not return")
        actual = lane_words(bus)
        expected, expected_s1, expected_s2 = oracle(words, coefficient)
        actual_state = [bus.read(FILTER2_STATE0 + offset, 4) for offset in (0, 4)]
        if actual != expected or actual_state != [expected_s1, expected_s2] or multiply_calls != 64:
            raise ValueError("target Q1.31 kernel diverged from oracle")
        return {
            "coefficient": f"0x{coefficient:08X}",
            "instructions": cpu.steps - start,
            "multiply_calls": multiply_calls,
            "input_sha256": words_hash(words),
            "output_sha256": words_hash(actual),
            "first_four_output": [f"0x{value:08X}" for value in actual[:4]],
            "final_state": [f"0x{value:08X}" for value in actual_state],
            "oracle_match": True,
        }


def run_callback_block(bus, cpu, active: bool, prior_state: tuple[int, int]) -> dict:
    install_input(bus, active)
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    start = cpu.steps
    before = None
    after = None
    coefficient = bus.read(COEFFICIENT_ADDRESS, 4)
    for _ in range(100_000):
        if cpu.pc == MIXER:
            break
        if cpu.pc == CAVE + 6:
            before = lane_words(bus)
        if cpu.pc == COMMON_RESTORE:
            after = lane_words(bus)
        cpu.step()
    else:
        raise ValueError("Q1.31 callback did not reach the mixer boundary")
    if before is None or after is None:
        raise ValueError("Q1.31 callback checkpoints not reached")
    expected, s1, s2 = oracle(before, coefficient, *prior_state)
    actual_state = (bus.read(FILTER2_STATE0, 4), bus.read(FILTER2_STATE0 + 4, 4))
    if after != expected or actual_state != (s1, s2):
        raise ValueError("consecutive callback state diverged from oracle")
    return {
        "input": "active" if active else "zero",
        "instructions": cpu.steps - start,
        "input_sha256": words_hash(before),
        "output_sha256": words_hash(after),
        "starting_state": [f"0x{value:08X}" for value in prior_state],
        "ending_state": [f"0x{value:08X}" for value in actual_state],
        "oracle_match": True,
        "_state": actual_state,
    }


def continuity_probe(module, armed_path: Path, stock: bytes) -> list[dict]:
    bus, prepared_cpu, _ = prepared_machine(module, armed_path)
    install_tables(bus, stock)
    baseline = {
        "d": prepared_cpu.d.copy(),
        "a": prepared_cpu.a.copy(),
        "sr": prepared_cpu.sr,
        "ctrl": prepared_cpu.ctrl.copy(),
        "macsr": prepared_cpu.macsr,
        "mac_mask": prepared_cpu.mac_mask,
        "macc": prepared_cpu.macc.copy(),
    }

    def callback_cpu():
        cpu = module.CPU(bus)
        cpu.d = baseline["d"].copy()
        cpu.a = baseline["a"].copy()
        cpu.sr = baseline["sr"]
        cpu.ctrl = baseline["ctrl"].copy()
        cpu.macsr = baseline["macsr"]
        cpu.mac_mask = baseline["mac_mask"]
        cpu.macc = baseline["macc"].copy()
        return cpu

    state = (0, 0)
    results = []
    for active in (True, False, True):
        cpu = callback_cpu()
        result = run_callback_block(bus, cpu, active, state)
        state = result.pop("_state")
        results.append(result)
    if results[1]["starting_state"] == ["0x00000000", "0x00000000"]:
        raise ValueError("callback continuity test did not carry nonzero state")
    return results


def probe(stock_path: Path, emulator_path: Path, candidate_output: Path | None = None) -> dict:
    stock = stock_path.read_bytes()
    stock_hash = hashlib.sha256(stock).hexdigest()
    if stock_hash != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {stock_hash}")
    disabled, disabled_build = build_kernel(stock, False)
    armed, armed_build = build_kernel(stock, True, 0x40000000)
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
            coefficient_vectors = [
                direct_kernel_vector(module, armed, impulse, coefficient)
                for coefficient in (0x00000000, 0x10000000, 0x20000000, 0x40000000, 0x7FFFFFFF)
            ]
            coefficient_vectors.append(direct_kernel_vector(module, armed, alternating, 0x7FFFFFFF))
            continuity = continuity_probe(module, armed_path, stock)

            comparisons = []
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
                    raise ValueError("disabled Q1.31 candidate diverged from stock")
                if not armed_equal["mixer_entry_registers"] or not armed_equal["mixer_instructions"] or not armed_equal["mixer_output_writes"]:
                    raise ValueError("armed Q1.31 kernel damaged callback execution state")
                if not active and not all(armed_equal.values()):
                    raise ValueError("armed Q1.31 kernel changed zero input from zero state")
                if active and armed_equal["mixer_input_sha256"]:
                    raise ValueError("armed Q1.31 kernel did not alter active mixer input")
                comparisons.append({
                    "input": "active" if active else "zero",
                    "disabled_equals_stock": disabled_equal,
                    "armed_half_coefficient_equals_stock": armed_equal,
                })
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
            "coefficient_storage": f"0x{COEFFICIENT_ADDRESS:08X}",
            "coefficient_domain": "0x00000000..0x7FFFFFFF nonnegative Q1.31",
            "multiply": "signed 32x32 -> signed 64, arithmetic >>31",
            "cascade_stages": 2,
            "stage_equation": "state=sat32(state + q31(sat32(input-state)*coefficient))",
        },
        "coefficient_vectors": coefficient_vectors,
        "consecutive_callback_entries_through_mixer": continuity,
        "stock_comparisons": comparisons,
        "conclusion": (
            "The Filter 2 target kernel now loads its coefficient from the versioned state "
            "slot and performs a real signed 32x32-to-Q1.31 multiply twice per sample. "
            "Endpoint/intermediate impulse vectors, an alternating signed-limit vector, "
            "and active-zero-active consecutive callback entries through the mixer boundary "
            "all match the independent oracle."
        ),
        "scope_limit": (
            "The coefficient is writable state but is not yet connected to a Rytm parameter "
            "or cutoff mapping. Only lane 0 is implemented, and hardware cycle margin remains "
            "unmeasured."
        ),
        "next_target": (
            "Map a stable control value into the coefficient field with smoothing, prove "
            "zipper-free coefficient changes across callbacks, then replicate the kernel "
            "across all eight lanes and measure worst-case modeled instruction cost."
        ),
        "safety": (
            "Only the default-disabled decompressed MAIN candidate is retained. Armed kernels "
            "were temporary; no ELE3 container or SysEx was built."
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
