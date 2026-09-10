#!/usr/bin/env python3
"""Replicate the slewed two-pole Filter 2 kernel across all eight audio lanes."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections import Counter
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, RETURN_PC, prepared_machine
from filter2_bypass_canary_probe import execute_vector
from filter2_coefficient_slew_probe import FLAG_FILTER2, control_to_q31, oracle
from filter2_lfo2_state_canary_probe import STATE_BASE, build_candidate as build_state_candidate, image_offset
from filter2_unity_kernel_probe import (
    CAVE,
    FILTER2_MASK_ADDRESS,
    FLAGS_ADDRESS,
    MIXER,
    OUTPUT_PLANE,
    install_input,
    install_tables,
    words_hash,
)
from trigger_queue_probe import load_emulator

LANES = 8
WORDS_PER_LANE = 32
PLANE_LANE_STRIDE = WORDS_PER_LANE * 4
FILTER2_STATE0 = STATE_BASE + 32
FILTER2_STATE_STRIDE = 32
ALL_LANES_MASK = 0x00FF
EXTENSION_BASE = 0x402B4600


class Builder:
    """Tiny label/fixup builder for the few ColdFire branches used here."""

    def __init__(self, base: int):
        self.base = base
        self.body = bytearray()
        self.labels: dict[str, int] = {}
        self.fixups: list[tuple[int, str]] = []

    @property
    def pc(self) -> int:
        return self.base + len(self.body)

    def emit(self, encoded: str) -> None:
        self.body.extend(bytes.fromhex(encoded))

    def label(self, name: str) -> None:
        self.labels[name] = self.pc

    def branch_word(self, opcode: int, label: str) -> None:
        start = self.pc
        self.body.extend(opcode.to_bytes(2, "big"))
        self.fixups.append((len(self.body), label))
        self.body.extend(b"\0\0")
        # ColdFire branches use the address immediately after the opcode as base.
        assert self.base + len(self.body) == start + 4

    def finish(self) -> tuple[bytes, dict[str, int]]:
        for displacement_offset, label in self.fixups:
            branch_start = self.base + displacement_offset - 2
            displacement = self.labels[label] - (branch_start + 2)
            if not -0x8000 <= displacement <= 0x7FFF:
                raise ValueError(f"branch to {label} is out of range")
            self.body[displacement_offset : displacement_offset + 2] = (displacement & 0xFFFF).to_bytes(2, "big")
        return bytes(self.body), self.labels.copy()


def assemble_extension() -> tuple[bytes, dict[str, int]]:
    b = Builder(EXTENSION_BASE)
    b.label("entry")
    b.emit("4eb940117f00")            # JSR stock ingress
    b.label("post_ingress")
    b.emit("40e7")                    # MOVE.W SR,-(SP)
    b.emit("4ab9402b4408")            # TST.L flags
    b.branch_word(0x6700, "restore_sr")
    b.emit("48e7ffc0")                # MOVEM.L D0-D7/A0-A1,-(SP)

    for lane in range(LANES):
        b.emit(f"0839{lane:04x}402b440d")  # BTST #lane, mask low byte
        b.branch_word(0x6700, f"lane_{lane}_skip")
        b.emit(f"41f9{OUTPUT_PLANE + lane * PLANE_LANE_STRIDE:08x}")
        b.emit(f"43f9{FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE:08x}")
        b.branch_word(0x6100, "process_lane")
        b.label(f"lane_{lane}_skip")

    b.label("restore_registers")
    b.emit("4cdf03ff")                # MOVEM.L (SP)+,D0-D7/A0-A1
    b.label("restore_sr")
    b.emit("46df")                    # MOVE.W (SP)+,SR
    b.emit("4e75")                    # RTS

    b.label("process_lane")
    b.emit("7020")                    # MOVEQ #32,D0
    b.emit("28290008")                # MOVE.L 8(A1),D4 current coefficient
    b.emit("2a29000c")                # MOVE.L 12(A1),D5 target coefficient
    b.emit("2c05")                    # MOVE.L D5,D6
    b.emit("9c84")                    # SUB.L D4,D6
    b.emit("4c86")                    # SATS D6
    b.emit("4a86")                    # TST.L D6
    b.branch_word(0x6A00, "positive_division")
    b.emit("06860000001f")            # ADDI.L #31,D6 negative truncation bias
    b.label("positive_division")
    b.emit("7e05")                    # MOVEQ #5,D7
    b.emit("eea6")                    # ASR.L D7,D6
    b.label("sample_loop")
    b.emit("d886")                    # ADD.L D6,D4
    b.emit("4c84")                    # SATS D4
    b.emit("2210")                    # MOVE.L (A0),D1
    b.emit("2411")                    # MOVE.L (A1),D2
    b.emit("9282")                    # SUB.L D2,D1
    b.emit("4c81")                    # SATS D1
    b.branch_word(0x6100, "multiply")
    b.emit("d481")                    # ADD.L D1,D2
    b.emit("4c82")                    # SATS D2
    b.emit("2282")                    # MOVE.L D2,(A1)
    b.emit("26290004")                # MOVE.L 4(A1),D3
    b.emit("2202")                    # MOVE.L D2,D1
    b.emit("9283")                    # SUB.L D3,D1
    b.emit("4c81")                    # SATS D1
    b.branch_word(0x6100, "multiply")
    b.emit("d681")                    # ADD.L D1,D3
    b.emit("4c83")                    # SATS D3
    b.emit("23430004")                # MOVE.L D3,4(A1)
    b.emit("20c3")                    # MOVE.L D3,(A0)+
    b.emit("5380")                    # SUBQ.L #1,D0
    b.branch_word(0x6600, "sample_loop")
    b.emit("2a29000c")                # MOVE.L 12(A1),D5
    b.emit("23450008")                # MOVE.L D5,8(A1), snap current to target
    b.emit("4e75")                    # RTS

    b.label("multiply")
    b.emit("4c041c05")                # MULS.L D4,D1:D5
    b.emit("7e1f")                    # MOVEQ #31,D7
    b.emit("eea9")                    # LSR.L D7,D1
    b.emit("da85")                    # ADD.L D5,D5
    b.emit("8285")                    # OR.L D5,D1
    b.emit("4e75")                    # RTS
    return b.finish()


EXTENSION_BODY, SYMBOLS = assemble_extension()
CAVE_STUB = bytes.fromhex(f"4ef9{EXTENSION_BASE:08x}")
EXTENSION_END = EXTENSION_BASE + len(EXTENSION_BODY)


def state_address(lane: int) -> int:
    return FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE


def plane_lanes(bus) -> list[list[int]]:
    return [
        [bus.read(OUTPUT_PLANE + lane * PLANE_LANE_STRIDE + index * 4, 4) for index in range(WORDS_PER_LANE)]
        for lane in range(LANES)
    ]


def build_kernel(stock: bytes, armed: bool) -> tuple[bytes, dict]:
    image, state = build_state_candidate(stock)
    candidate = bytearray(image)
    candidate[image_offset(CAVE) : image_offset(CAVE) + len(CAVE_STUB)] = CAVE_STUB
    candidate[image_offset(EXTENSION_BASE) : image_offset(EXTENSION_BASE) + len(EXTENSION_BODY)] = EXTENSION_BODY
    if armed:
        candidate[image_offset(FLAGS_ADDRESS) : image_offset(FLAGS_ADDRESS) + 4] = FLAG_FILTER2.to_bytes(4, "big")
        candidate[image_offset(FILTER2_MASK_ADDRESS) : image_offset(FILTER2_MASK_ADDRESS) + 2] = ALL_LANES_MASK.to_bytes(2, "big")
    return bytes(candidate), {
        "state": state,
        "cave_stub_address": f"0x{CAVE:08X}",
        "cave_stub_body": CAVE_STUB.hex(),
        "extension_address": f"0x{EXTENSION_BASE:08X}",
        "extension_end_exclusive": f"0x{EXTENSION_END:08X}",
        "extension_bytes": len(EXTENSION_BODY),
        "extension_body": EXTENSION_BODY.hex(),
        "symbols": {name: f"0x{address:08X}" for name, address in SYMBOLS.items()},
        "flags": FLAG_FILTER2 if armed else 0,
        "filter2_lane_mask": ALL_LANES_MASK if armed else 0,
        "changed_byte_positions": sum(left != right for left, right in zip(stock, candidate)),
    }


def initial_lane_state(lane: int) -> tuple[int, int, int, int]:
    s1 = (lane + 1) * 0x00100000
    s2 = (lane + 1) * 0x00080000
    current = control_to_q31(120 - lane * 16)
    target = control_to_q31(8 + lane * 16)
    return s1, s2, current, target


def run_mask(module, image_path: Path, stock: bytes, mask: int) -> dict:
    bus, cpu, _ = prepared_machine(module, image_path)
    install_tables(bus, stock)
    install_input(bus, True)
    bus.write(FILTER2_MASK_ADDRESS, 2, mask)
    starting_slots = []
    for lane in range(LANES):
        base = state_address(lane)
        s1, s2, current, target = initial_lane_state(lane)
        values = (s1, s2, current, target, 0xA5000000 | lane, 0x5A000000 | lane, lane, 0xFFFFFFFF - lane)
        for index, value in enumerate(values):
            bus.write(base + index * 4, 4, value)
        starting_slots.append(values)

    before = None
    plane_writes = []
    state_writes = []
    multiply_calls = 0
    original_write = bus.write

    def traced_write(address: int, size: int, value: int) -> None:
        if EXTENSION_BASE <= cpu.pc < EXTENSION_END:
            if OUTPUT_PLANE <= address < OUTPUT_PLANE + LANES * PLANE_LANE_STRIDE:
                plane_writes.append((address, size, value & 0xFFFFFFFF))
            if FILTER2_STATE0 <= address < FILTER2_STATE0 + LANES * FILTER2_STATE_STRIDE:
                state_writes.append((address, size, value & 0xFFFFFFFF))
        original_write(address, size, value)

    bus.write = traced_write
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    start = cpu.steps
    try:
        for _ in range(200_000):
            if cpu.pc == MIXER:
                break
            if cpu.pc == SYMBOLS["post_ingress"]:
                before = plane_lanes(bus)
            if cpu.pc == SYMBOLS["multiply"]:
                multiply_calls += 1
            cpu.step()
        else:
            raise ValueError("eight-lane callback did not reach mixer")
    finally:
        bus.write = original_write
    if before is None:
        raise ValueError("post-ingress checkpoint was not reached")

    after = plane_lanes(bus)
    selected = [lane for lane in range(LANES) if mask & (1 << lane)]
    lane_results = []
    for lane in range(LANES):
        base = state_address(lane)
        final_slot = tuple(bus.read(base + index * 4, 4) for index in range(8))
        s1, s2, current, target = initial_lane_state(lane)
        if lane in selected:
            expected, final_s1, final_s2, _ = oracle(before[lane], current, target, s1, s2)
            expected_slot = (final_s1, final_s2, target) + starting_slots[lane][3:]
            if after[lane] != expected or final_slot != expected_slot:
                raise ValueError(f"lane {lane} diverged from independent oracle")
        else:
            if after[lane] != before[lane] or final_slot != starting_slots[lane]:
                raise ValueError(f"unselected lane {lane} or its state changed")
        lane_results.append({
            "lane": lane,
            "selected": lane in selected,
            "before_sha256": words_hash(before[lane]),
            "after_sha256": words_hash(after[lane]),
            "audio_changed": before[lane] != after[lane],
            "state_changed": final_slot != starting_slots[lane],
            "oracle_match": True,
        })

    expected_plane_addresses = [
        OUTPUT_PLANE + lane * PLANE_LANE_STRIDE + index * 4
        for lane in selected for index in range(WORDS_PER_LANE)
    ]
    if [event[0] for event in plane_writes] != expected_plane_addresses:
        raise ValueError("selected-lane output write geometry changed")
    expected_state_counts = Counter()
    for lane in selected:
        base = state_address(lane)
        expected_state_counts[base] = WORDS_PER_LANE
        expected_state_counts[base + 4] = WORDS_PER_LANE
        expected_state_counts[base + 8] = 1
    if Counter(event[0] for event in state_writes) != expected_state_counts:
        raise ValueError("selected-lane state write geometry changed")
    if multiply_calls != len(selected) * WORDS_PER_LANE * 2:
        raise ValueError("multiply-call count changed")

    return {
        "mask": f"0x{mask:04X}",
        "selected_lanes": selected,
        "instructions_to_mixer": cpu.steps - start,
        "multiply_calls": multiply_calls,
        "plane_writes": len(plane_writes),
        "state_writes": len(state_writes),
        "lanes": lane_results,
        "oracle_match": True,
    }


def stock_instructions_to_mixer(module, stock_path: Path, stock: bytes) -> int:
    bus, cpu, _ = prepared_machine(module, stock_path)
    install_tables(bus, stock)
    install_input(bus, True)
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    start = cpu.steps
    for _ in range(100_000):
        if cpu.pc == MIXER:
            return cpu.steps - start
        cpu.step()
    raise ValueError("stock callback did not reach mixer")


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
            one_hot = [run_mask(module, armed_path, stock, 1 << lane) for lane in range(LANES)]
            full_mask = run_mask(module, armed_path, stock, ALL_LANES_MASK)

            disabled_checks = []
            for active in (False, True):
                stock_run = execute_vector(module, stock_path, stock, active, False)
                disabled_run = execute_vector(module, disabled_path, stock, active, True)
                fields = (
                    "mixer_entry_registers", "mixer_input_sha256", "ingress_output_sha256",
                    "mixer_instructions", "mixer_output_writes", "mixer_output_sha256",
                )
                equality = {field: stock_run[field] == disabled_run[field] for field in fields}
                if not all(equality.values()):
                    raise ValueError("disabled eight-lane candidate diverged from stock")
                disabled_checks.append({"input": "active" if active else "zero", "bit_identical": equality})
    finally:
        if disabled_temp is not None:
            disabled_temp.close()

    stock_callback_instructions = stock_instructions_to_mixer(module, stock_path, stock)
    modeled_delta = full_mask["instructions_to_mixer"] - stock_callback_instructions
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
        "lane_geometry": {
            "lanes": LANES,
            "words_per_lane": WORDS_PER_LANE,
            "audio_lane_stride_bytes": PLANE_LANE_STRIDE,
            "state_lane_stride_bytes": FILTER2_STATE_STRIDE,
            "state_addresses": [f"0x{state_address(lane):08X}" for lane in range(LANES)],
        },
        "one_hot_isolation": one_hot,
        "full_mask": full_mask,
        "modeled_cost": {
            "stock_active_instructions_to_mixer": stock_callback_instructions,
            "eight_lane_active_instructions_to_mixer": full_mask["instructions_to_mixer"],
            "semantic_instruction_delta": modeled_delta,
            "multiply_calls_per_block": full_mask["multiply_calls"],
            "qualification": "Emulator semantic instruction count, not measured ColdFire hardware cycles.",
        },
        "disabled_stock_equivalence": disabled_checks,
        "conclusion": (
            "The slewed two-pole Q1.31 Filter 2 kernel now processes all eight 32-word lanes. "
            "Every one-hot mask changes only its selected audio lane and 32-byte state slot; "
            "the full mask performs 256 output writes and 512 multiplies and matches eight independent oracles."
        ),
        "scope_limit": (
            "The cost is a semantic emulator count, not a hardware-cycle or interrupt-deadline measurement. "
            "The new control value is still written only by the laboratory harness."
        ),
        "next_target": (
            "Trace stock control publication around the callback boundary and select a single-writer shadow "
            "location for the eight Filter 2 target coefficients without lengthening the audio critical section."
        ),
        "safety": (
            "Only the default-disabled decompressed MAIN candidate is retained. The all-lane armed image was "
            "temporary; no ELE3 container or SysEx was built."
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
