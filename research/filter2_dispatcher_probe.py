#!/usr/bin/env python3
"""Build and execute the first disabled-by-default Filter 2 dispatcher."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, RETURN_PC, prepared_machine
from filter2_bypass_canary_probe import execute_vector
from filter2_lfo2_state_canary_probe import STATE_BASE, build_candidate as build_state_candidate, image_offset
from post_voice_ingress_probe import (
    ACTIVE_INPUT_WORD,
    DMA_BLOCK_BYTES,
    EXTERNAL_AUDIO_WINDOW,
    install_tables,
)
from trigger_queue_probe import load_emulator

CAVE = 0x402B4340
MIXER = 0x4010A2E0
FLAGS_ADDRESS = STATE_BASE + 8
FILTER2_MASK_ADDRESS = STATE_BASE + 12
FILTER2_MASK_BIT0_ADDRESS = FILTER2_MASK_ADDRESS + 1
FLAG_FILTER2 = 1
LANE0_MASK = 1
ENABLED_PLACEHOLDER = 0x402B4358
RETURN_INSTRUCTION = 0x402B435A
DISPATCHER_BODY = bytes.fromhex(
    "4eb940117f00"      # JSR stock ingress
    "4ab9402b4408"      # TST.L flags
    "670c"              # BEQ return
    "08390000402b440d"  # BTST #0, low byte of big-endian Filter 2 lane mask
    "6702"              # BEQ return
    "4e71"              # enabled lane-0 placeholder: NOP
    "4e75"              # RTS
)


def build_dispatcher(stock: bytes, armed: bool) -> tuple[bytes, dict]:
    image, state = build_state_candidate(stock)
    candidate = bytearray(image)
    candidate[image_offset(CAVE) : image_offset(CAVE) + len(DISPATCHER_BODY)] = DISPATCHER_BODY
    if armed:
        candidate[image_offset(FLAGS_ADDRESS) : image_offset(FLAGS_ADDRESS) + 4] = FLAG_FILTER2.to_bytes(4, "big")
        candidate[image_offset(FILTER2_MASK_ADDRESS) : image_offset(FILTER2_MASK_ADDRESS) + 2] = LANE0_MASK.to_bytes(2, "big")
    changed = sum(left != right for left, right in zip(stock, candidate))
    return bytes(candidate), {
        "state": state,
        "dispatcher_address": f"0x{CAVE:08X}",
        "dispatcher_body": DISPATCHER_BODY.hex(),
        "flags": FLAG_FILTER2 if armed else 0,
        "filter2_lane_mask": LANE0_MASK if armed else 0,
        "changed_byte_positions": changed,
    }


def install_input(bus, active: bool) -> None:
    payload = ACTIVE_INPUT_WORD.to_bytes(4, "big") * 36 if active else bytes(DMA_BLOCK_BYTES)
    for index, value in enumerate(payload):
        bus.write(EXTERNAL_AUDIO_WINDOW + index, 1, value)


def trace_dispatch(module, image_path: Path, stock: bytes, active: bool) -> dict:
    bus, cpu, _ = prepared_machine(module, image_path)
    install_tables(bus, stock)
    install_input(bus, active)
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    start = cpu.steps
    cave_pcs = []
    state_reads = []
    original_read = bus.read

    def traced_read(address: int, size: int) -> int:
        value = original_read(address, size)
        if FLAGS_ADDRESS <= address < FILTER2_MASK_ADDRESS + 2:
            state_reads.append((cpu.pc, address, size, value))
        return value

    bus.read = traced_read
    try:
        for _ in range(100_000):
            if cpu.pc == MIXER:
                break
            if CAVE <= cpu.pc < CAVE + len(DISPATCHER_BODY):
                cave_pcs.append(cpu.pc)
            cpu.step()
        else:
            raise ValueError("dispatcher candidate did not reach mixer")
    finally:
        bus.read = original_read
    return {
        "input": "active" if active else "zero",
        "instructions_to_mixer": cpu.steps - start,
        "cave_pcs": [f"0x{pc:08X}" for pc in cave_pcs],
        "enabled_placeholder_executed": ENABLED_PLACEHOLDER in cave_pcs,
        "state_reads": [
            {
                "instruction_next_pc": f"0x{pc:08X}",
                "address": f"0x{address:08X}",
                "size": size,
                "value": f"0x{value:0{size * 2}X}",
            }
            for pc, address, size, value in state_reads
        ],
    }


def probe(stock_path: Path, emulator_path: Path, candidate_output: Path | None = None) -> dict:
    stock = stock_path.read_bytes()
    stock_hash = hashlib.sha256(stock).hexdigest()
    if stock_hash != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {stock_hash}")
    disabled, disabled_build = build_dispatcher(stock, False)
    armed, armed_build = build_dispatcher(stock, True)
    module = load_emulator(emulator_path)

    disabled_temp = None
    if candidate_output is None:
        disabled_temp = tempfile.NamedTemporaryFile(suffix=".bin")
        disabled_path = Path(disabled_temp.name)
    else:
        disabled_path = candidate_output
    disabled_path.write_bytes(disabled)

    comparisons = []
    traces = {"disabled": [], "armed_probe": []}
    try:
        with tempfile.NamedTemporaryFile(suffix=".bin") as armed_temp:
            armed_path = Path(armed_temp.name)
            armed_path.write_bytes(armed)
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
                    raise ValueError("dispatcher placeholder changed stock-visible audio state")
                comparisons.append({
                    "input": "active" if active else "zero",
                    "disabled_equals_stock": disabled_equal,
                    "armed_placeholder_equals_stock": armed_equal,
                })
                disabled_trace = trace_dispatch(module, disabled_path, stock, active)
                armed_trace = trace_dispatch(module, armed_path, stock, active)
                if disabled_trace["enabled_placeholder_executed"]:
                    raise ValueError("disabled dispatcher entered enabled placeholder")
                if not armed_trace["enabled_placeholder_executed"]:
                    raise ValueError("armed dispatcher did not enter enabled placeholder")
                traces["disabled"].append(disabled_trace)
                traces["armed_probe"].append(armed_trace)
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
        "dispatch_traces": traces,
        "stock_equivalence": comparisons,
        "conclusion": (
            "The cave now performs stock ingress, reads the versioned state flags, and "
            "returns immediately when Filter 2 is disabled. An emulation-only armed image "
            "also reads lane mask bit 0 and reaches the dedicated placeholder NOP. Both "
            "paths remain bit-identical to stock through the next combiner, proving the "
            "control dispatcher before any filter arithmetic is introduced."
        ),
        "next_target": (
            "Replace the lane-0 placeholder with one explicitly saturating 2-pole Q1.31 "
            "reference kernel, initially with unity/bypass coefficients, and compare all "
            "256 output words before trying an audible cutoff."
        ),
        "safety": (
            "Only the default-disabled decompressed MAIN candidate is retained. The armed "
            "probe was temporary; no ELE3 container or SysEx was built."
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
