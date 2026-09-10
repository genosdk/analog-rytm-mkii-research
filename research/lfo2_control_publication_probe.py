#!/usr/bin/env python3
"""Publish LFO2 enable/reset/rate/depth through the proven foreground setter ABI."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import tempfile
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, RETURN_PC, prepared_machine
from filter2_bypass_canary_probe import execute_vector
from filter2_coefficient_slew_probe import control_to_q31, oracle
from filter2_eight_lane_probe import ALL_LANES_MASK, Builder, FILTER2_STATE0, FILTER2_STATE_STRIDE, LANES
from filter2_lfo2_cutoff_binding_probe import (
    FILTER_SYMBOLS,
    LFO2_MASK_ADDRESS,
    LFO2_STATE0,
    LFO2_STATE_STRIDE,
    build_candidate as build_binding_candidate,
    effective_target,
    modulation_q31,
    plane_lanes,
    triangle_q31,
)
from filter2_lfo2_state_canary_probe import image_offset
from filter2_publication_shim_probe import (
    FOREGROUND_SETTER_CALLSITE,
    SHIM_BASE,
    TABLE_BASE as Q31_TABLE_BASE,
    WORD_SETTER,
)
from filter2_control_publication_probe import TARGET_ARRAY
from filter2_unity_kernel_probe import FILTER2_MASK_ADDRESS, FLAGS_ADDRESS, MIXER, install_input, install_tables
from trigger_queue_probe import load_emulator, stock_call

TRIGGER_INDEX_BASE = 0x7FD0
ENABLE_INDEX_BASE = 0x7FD8
RESET_INDEX_BASE = 0x7FE0
RATE_INDEX_BASE = 0x7FE8
DEPTH_INDEX_BASE = 0x7FF0
FILTER_INDEX_BASE = 0x7FF8
CONTROL_INDEX_END = 0x8000
# The ColdFire-safe Filter 2 kernel occupies two extra bytes beyond the
# original 0x402B4800 boundary.  Keep the later LFO2 regions disjoint by
# moving this shim and its rate table one 0x200-byte page forward.
CONTROL_SHIM_BASE = 0x402B4E00
RATE_TABLE_BASE = 0x402B5000
TRIGGER_MASK_ADDRESS = 0x402B4418
RANDOM_INDEX_OFFSET = 24
CALLBACKS_PER_SECOND = 1500.0
MIN_RATE_HZ = 0.01
MAX_RATE_HZ = 100.0


def rate_to_increment(control: int) -> int:
    if not 0 <= control <= 127:
        raise ValueError("LFO2 rate control must be 0..127")
    frequency = MIN_RATE_HZ * ((MAX_RATE_HZ / MIN_RATE_HZ) ** (control / 127.0))
    return min(0xFFFFFFFF, round(frequency * (1 << 32) / CALLBACKS_PER_SECOND))


RATE_TABLE = b"".join(rate_to_increment(value).to_bytes(4, "big") for value in range(128))


def assemble_control_shim() -> tuple[bytes, dict[str, int]]:
    b = Builder(CONTROL_SHIM_BASE)
    b.label("entry")
    b.emit(f"4ab9{FLAGS_ADDRESS:08x}")
    b.branch_word(0x6700, "stock")
    b.emit("202f0004")                      # index
    b.emit(f"0c800000{TRIGGER_INDEX_BASE:04x}")
    b.branch_word(0x6500, "stock")
    b.emit(f"0c800000{CONTROL_INDEX_END - 1:04x}")
    b.branch_word(0x6200, "stock")
    b.emit(f"0c800000{ENABLE_INDEX_BASE:04x}")
    b.branch_word(0x6500, "trigger_mode")
    b.emit(f"0c800000{RESET_INDEX_BASE:04x}")
    b.branch_word(0x6500, "enable")
    b.emit(f"0c800000{RATE_INDEX_BASE:04x}")
    b.branch_word(0x6500, "reset")
    b.emit(f"0c800000{DEPTH_INDEX_BASE:04x}")
    b.branch_word(0x6500, "rate")
    b.emit(f"0c800000{FILTER_INDEX_BASE:04x}")
    b.branch_word(0x6500, "depth")
    b.emit(f"4ef9{SHIM_BASE:08x}")          # existing Filter2 publication shim

    b.label("trigger_mode")
    b.emit(f"04800000{TRIGGER_INDEX_BASE:04x}")
    b.emit("222f0008")
    b.emit("4a81")
    b.branch_word(0x6700, "free_mode_bit")
    b.emit(f"01f9{TRIGGER_MASK_ADDRESS + 1:08x}")  # BSET D0,trigger mask
    b.emit("4e75")
    b.label("free_mode_bit")
    b.emit(f"01b9{TRIGGER_MASK_ADDRESS + 1:08x}")  # BCLR D0,trigger mask
    b.emit("4e75")

    b.label("enable")
    b.emit(f"04800000{ENABLE_INDEX_BASE:04x}")
    b.emit("222f0008")
    b.emit("4a81")
    b.branch_word(0x6700, "disable_bit")
    b.emit(f"01f9{LFO2_MASK_ADDRESS + 1:08x}")  # BSET D0,mask low byte
    b.emit("4e75")
    b.label("disable_bit")
    b.emit(f"01b9{LFO2_MASK_ADDRESS + 1:08x}")  # BCLR D0,mask low byte
    b.emit("4e75")

    b.label("reset")
    b.emit(f"04800000{RESET_INDEX_BASE:04x}")
    b.emit("2400")                          # preserve lane for Filter2 stride
    b.emit("e988")                          # lane * 16
    b.emit(f"41f9{LFO2_STATE0:08x}")
    b.emit("d1c0")
    b.emit("4290")                          # CLR.L phase
    b.emit("42a8000c")                      # CLR.L last modulation
    b.emit("eb8a")                          # preserved lane * 32
    b.emit(f"41f9{FILTER2_STATE0 + RANDOM_INDEX_OFFSET:08x}")
    b.emit("d1c2")
    b.emit("4290")                          # deterministic random table index
    b.emit("4e75")

    b.label("rate")
    b.emit(f"04800000{RATE_INDEX_BASE:04x}")
    b.emit("222f0008")
    b.emit("0c810000007f")
    b.branch_word(0x6300, "rate_valid")
    b.emit("727f")
    b.label("rate_valid")
    b.emit("e589")                          # control * 4
    b.emit(f"43f9{RATE_TABLE_BASE:08x}")
    b.emit("d3c1")
    b.emit("2211")                          # mapped increment
    b.emit("e988")                          # lane * 16
    b.emit(f"41f9{LFO2_STATE0 + 4:08x}")
    b.emit("d1c0")
    b.emit("2081")
    b.emit("4e75")

    b.label("depth")
    b.emit(f"04800000{DEPTH_INDEX_BASE:04x}")
    b.emit("222f0008")
    b.emit("0c810000007f")
    b.branch_word(0x6300, "depth_valid")
    b.emit("727f")
    b.label("depth_valid")
    b.emit("e589")
    b.emit(f"43f9{Q31_TABLE_BASE:08x}")
    b.emit("d3c1")
    b.emit("2211")
    b.emit("e988")
    b.emit(f"41f9{LFO2_STATE0 + 8:08x}")
    b.emit("d1c0")
    b.emit("2081")
    b.emit("4e75")

    b.label("stock")
    b.emit(f"4ef9{WORD_SETTER:08x}")
    return b.finish()


CONTROL_SHIM_BODY, CONTROL_SYMBOLS = assemble_control_shim()
CONTROL_SHIM_END = CONTROL_SHIM_BASE + len(CONTROL_SHIM_BODY)
CALLSITE_PATCH = bytes.fromhex(f"4eb9{CONTROL_SHIM_BASE:08x}")


def build_candidate(stock: bytes, armed: bool) -> tuple[bytes, dict]:
    image, binding = build_binding_candidate(stock, armed)
    candidate = bytearray(image)
    candidate[image_offset(FOREGROUND_SETTER_CALLSITE):image_offset(FOREGROUND_SETTER_CALLSITE) + 6] = CALLSITE_PATCH
    candidate[image_offset(CONTROL_SHIM_BASE):image_offset(CONTROL_SHIM_END)] = CONTROL_SHIM_BODY
    candidate[image_offset(RATE_TABLE_BASE):image_offset(RATE_TABLE_BASE) + len(RATE_TABLE)] = RATE_TABLE
    return bytes(candidate), {
        "binding": binding,
        "callsite": f"0x{FOREGROUND_SETTER_CALLSITE:08X}",
        "callsite_patch": CALLSITE_PATCH.hex(),
        "shim": [f"0x{CONTROL_SHIM_BASE:08X}", f"0x{CONTROL_SHIM_END:08X}"],
        "shim_bytes": len(CONTROL_SHIM_BODY),
        "symbols": {name: f"0x{address:08X}" for name, address in CONTROL_SYMBOLS.items()},
        "rate_table": [f"0x{RATE_TABLE_BASE:08X}", f"0x{RATE_TABLE_BASE + len(RATE_TABLE):08X}"],
        "rate_table_sha256": hashlib.sha256(RATE_TABLE).hexdigest(),
        "index_banks": {
            "trigger_mode": [f"0x{TRIGGER_INDEX_BASE:04X}", f"0x{TRIGGER_INDEX_BASE + 7:04X}"],
            "enable": [f"0x{ENABLE_INDEX_BASE:04X}", f"0x{ENABLE_INDEX_BASE + 7:04X}"],
            "reset": [f"0x{RESET_INDEX_BASE:04X}", f"0x{RESET_INDEX_BASE + 7:04X}"],
            "rate": [f"0x{RATE_INDEX_BASE:04X}", f"0x{RATE_INDEX_BASE + 7:04X}"],
            "depth": [f"0x{DEPTH_INDEX_BASE:04X}", f"0x{DEPTH_INDEX_BASE + 7:04X}"],
            "filter2": [f"0x{FILTER_INDEX_BASE:04X}", f"0x{FILTER_INDEX_BASE + 7:04X}"],
        },
        "armed": armed,
        "changed_byte_positions": sum(a != b for a, b in zip(stock, candidate)),
    }


def invoke(module, image_path: Path, index: int, value: int) -> dict:
    bus = module.Bus()
    bus.load_main(image_path)
    cpu = module.CPU(bus)
    cpu.a[7] = module.INITIAL_SP - 0x2000
    writes = []
    original_write = bus.write

    def traced_write(address: int, size: int, stored: int) -> None:
        if (LFO2_MASK_ADDRESS <= address < LFO2_MASK_ADDRESS + 2 or
                LFO2_STATE0 <= address < LFO2_STATE0 + LANES * LFO2_STATE_STRIDE):
            writes.append((address, size, stored & ((1 << (size * 8)) - 1)))
        original_write(address, size, stored)

    bus.write = traced_write
    steps = stock_call(cpu, CONTROL_SHIM_BASE, [index, value])
    bus.write = original_write
    return {"instructions": steps, "writes": writes, "bus": bus}


def publication_vectors(module, armed_path: Path) -> dict:
    rate_controls = [0, 64, 127]
    rates = []
    for lane, control in enumerate(rate_controls):
        result = invoke(module, armed_path, RATE_INDEX_BASE + lane, control)
        expected = rate_to_increment(control)
        observed = result["bus"].read(LFO2_STATE0 + lane * LFO2_STATE_STRIDE + 4, 4)
        if observed != expected:
            raise ValueError("rate publication diverged")
        rates.append({"lane": lane, "control": control, "increment": f"0x{observed:08X}",
                      "instructions": result["instructions"]})

    depths = []
    for lane, control in enumerate((0, 32, 96, 127)):
        result = invoke(module, armed_path, DEPTH_INDEX_BASE + lane, control)
        expected = control_to_q31(control)
        observed = result["bus"].read(LFO2_STATE0 + lane * LFO2_STATE_STRIDE + 8, 4)
        if observed != expected:
            raise ValueError("depth publication diverged")
        depths.append({"lane": lane, "control": control, "depth": f"0x{observed:08X}",
                       "instructions": result["instructions"]})

    mask_bus = module.Bus()
    mask_bus.load_main(armed_path)
    mask_cpu = module.CPU(mask_bus)
    mask_cpu.a[7] = module.INITIAL_SP - 0x2000
    for lane in (0, 3, 7):
        stock_call(mask_cpu, CONTROL_SHIM_BASE, [ENABLE_INDEX_BASE + lane, 1])
    stock_call(mask_cpu, CONTROL_SHIM_BASE, [ENABLE_INDEX_BASE + 3, 0])
    mask = mask_bus.read(LFO2_MASK_ADDRESS, 2)
    if mask != 0x0081:
        raise ValueError("enable/disable publication diverged")

    trigger_bus = module.Bus()
    trigger_bus.load_main(armed_path)
    trigger_cpu = module.CPU(trigger_bus)
    trigger_cpu.a[7] = module.INITIAL_SP - 0x2000
    for lane in (1, 4, 6):
        stock_call(trigger_cpu, CONTROL_SHIM_BASE, [TRIGGER_INDEX_BASE + lane, 1])
    stock_call(trigger_cpu, CONTROL_SHIM_BASE, [TRIGGER_INDEX_BASE + 4, 0])
    trigger_mask = trigger_bus.read(TRIGGER_MASK_ADDRESS, 2)
    if trigger_mask != 0x0042:
        raise ValueError("trigger/free mode publication diverged")

    reset_bus = module.Bus()
    reset_bus.load_main(armed_path)
    slot = LFO2_STATE0 + 5 * LFO2_STATE_STRIDE
    filter_slot = FILTER2_STATE0 + 5 * FILTER2_STATE_STRIDE
    for offset, value in ((0, 0xDEADBEEF), (4, 0x11111111), (8, 0x22222222), (12, 0xCAFEBABE)):
        reset_bus.write(slot + offset, 4, value)
    reset_bus.write(filter_slot + RANDOM_INDEX_OFFSET, 4, 0x00000063)
    reset_cpu = module.CPU(reset_bus)
    reset_cpu.a[7] = module.INITIAL_SP - 0x2000
    stock_call(reset_cpu, CONTROL_SHIM_BASE, [RESET_INDEX_BASE + 5, 99])
    reset_state = [reset_bus.read(slot + offset, 4) for offset in (0, 4, 8, 12)]
    if reset_state != [0, 0x11111111, 0x22222222, 0]:
        raise ValueError("trigger reset touched fields outside phase/output")
    random_index = reset_bus.read(filter_slot + RANDOM_INDEX_OFFSET, 4)
    if random_index != 0:
        raise ValueError("explicit reset did not restore deterministic random index")
    return {"rates": rates, "depths": depths, "final_enable_mask": f"0x{mask:04X}",
            "final_trigger_mask": f"0x{trigger_mask:04X}",
            "reset_lane5_state": [f"0x{value:08X}" for value in reset_state],
            "reset_lane5_random_index": random_index}


def end_to_end(module, armed_path: Path, stock: bytes) -> dict:
    bus, cpu, _ = prepared_machine(module, armed_path)
    install_tables(bus, stock)
    lane = 0
    commands = [
        (FILTER_INDEX_BASE + lane, 72),
        (TRIGGER_INDEX_BASE + lane, 1),
        (RATE_INDEX_BASE + lane, 127),
        (DEPTH_INDEX_BASE + lane, 80),
        (RESET_INDEX_BASE + lane, 0),
        (ENABLE_INDEX_BASE + lane, 1),
    ]
    for index, value in commands:
        stock_call(cpu, CONTROL_SHIM_BASE, [index, value])
    bus.write(FILTER2_MASK_ADDRESS, 2, 1)
    fbase = FILTER2_STATE0
    bus.write(fbase, 4, 0)
    bus.write(fbase + 4, 4, 0)
    bus.write(fbase + 8, 4, 0)
    install_input(bus, True)
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    before = None
    for _ in range(150_000):
        if cpu.pc == MIXER:
            break
        if cpu.pc == FILTER_SYMBOLS["post_ingress"]:
            before = plane_lanes(bus)
        cpu.step()
    else:
        raise ValueError("published LFO2 callback did not reach mixer")
    if before is None:
        raise ValueError("published LFO2 callback missed ingress")
    phase = rate_to_increment(127)
    depth = control_to_q31(80)
    modulation = modulation_q31(triangle_q31(phase), depth)
    base_target = control_to_q31(72)
    target = effective_target(base_target, modulation)
    expected, s1, s2, _ = oracle(before[lane], 0, target)
    after = plane_lanes(bus)[lane]
    observed = {
        "phase": bus.read(LFO2_STATE0, 4),
        "increment": bus.read(LFO2_STATE0 + 4, 4),
        "depth": bus.read(LFO2_STATE0 + 8, 4),
        "base": bus.read(fbase + 12, 4),
        "effective": bus.read(fbase + 16, 4),
        "s1": bus.read(fbase, 4), "s2": bus.read(fbase + 4, 4),
    }
    wanted = {"phase": phase, "increment": phase, "depth": depth, "base": base_target,
              "effective": target, "s1": s1, "s2": s2}
    if observed != wanted or after != expected:
        raise ValueError("published LFO2 controls did not drive Filter2 oracle")
    return {"commands": [{"index": f"0x{i:04X}", "value": v} for i, v in commands],
            "phase": f"0x{phase:08X}", "depth": f"0x{depth:08X}",
            "base_target": f"0x{base_target:08X}", "effective_target": f"0x{target:08X}",
            "oracle_match": True}


def ordinary_passthrough(module, stock_path: Path, candidate_path: Path) -> list[dict]:
    rows = []
    for index, value in ((0, 0x1234), (37, 0xABCD), (571, 0x007F)):
        stock_bus = module.Bus(); stock_bus.load_main(stock_path)
        stock_cpu = module.CPU(stock_bus); stock_cpu.a[7] = module.INITIAL_SP - 0x2000
        stock_steps = stock_call(stock_cpu, WORD_SETTER, [index, value])
        candidate_bus = module.Bus(); candidate_bus.load_main(candidate_path)
        candidate_cpu = module.CPU(candidate_bus); candidate_cpu.a[7] = module.INITIAL_SP - 0x2000
        candidate_steps = stock_call(candidate_cpu, CONTROL_SHIM_BASE, [index, value])
        address = TARGET_ARRAY + index * 2
        identical = stock_bus.read(address, 2) == candidate_bus.read(address, 2) == value
        if not identical:
            raise ValueError("ordinary stock setter passthrough diverged")
        rows.append({"index": index, "value": value, "stock_instructions": stock_steps,
                     "shim_instructions": candidate_steps, "stock_write_identical": True})
    return rows


def probe(stock_path: Path, emulator_path: Path, candidate_output: Path | None = None) -> dict:
    stock = stock_path.read_bytes()
    digest = hashlib.sha256(stock).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    if CONTROL_SHIM_END > RATE_TABLE_BASE:
        raise ValueError("LFO2 control shim overlaps rate table")
    disabled, disabled_build = build_candidate(stock, False)
    armed, armed_build = build_candidate(stock, True)
    module = load_emulator(emulator_path)
    temporary = None
    if candidate_output is None:
        temporary = tempfile.NamedTemporaryFile(suffix=".bin")
        disabled_path = Path(temporary.name)
    else:
        disabled_path = candidate_output
    disabled_path.write_bytes(disabled)
    try:
        with tempfile.NamedTemporaryFile(suffix=".bin") as armed_temp:
            armed_path = Path(armed_temp.name); armed_path.write_bytes(armed)
            vectors = publication_vectors(module, armed_path)
            integrated = end_to_end(module, armed_path, stock)
            passthrough = ordinary_passthrough(module, stock_path, armed_path)
            equivalence = []
            for active in (False, True):
                stock_run = execute_vector(module, stock_path, stock, active, False)
                disabled_run = execute_vector(module, disabled_path, stock, active, True)
                fields = ("mixer_entry_registers", "mixer_input_sha256", "ingress_output_sha256",
                          "mixer_instructions", "mixer_output_writes", "mixer_output_sha256")
                identity = {field: stock_run[field] == disabled_run[field] for field in fields}
                if not all(identity.values()):
                    raise ValueError("disabled LFO2 publication candidate diverged from stock")
                equivalence.append({"input": "active" if active else "zero", "bit_identical": identity})
    finally:
        if temporary is not None: temporary.close()
    return {
        "result": "PASS", "stock": {"path": str(stock_path), "sha256": digest},
        "disabled_candidate": {"path": str(candidate_output) if candidate_output else "temporary execution image",
                               "sha256": hashlib.sha256(disabled).hexdigest(), **disabled_build},
        "armed_emulation_probe": {"sha256": hashlib.sha256(armed).hexdigest(), **armed_build,
                                  "artifact_retained": False},
        "publication_vectors": vectors, "published_control_to_audio": integrated,
        "ordinary_stock_passthrough": passthrough, "disabled_callback_stock_equivalence": equivalence,
        "rate_contract": {"callback_rate_hz": CALLBACKS_PER_SECOND, "minimum_hz": MIN_RATE_HZ,
                          "maximum_hz": MAX_RATE_HZ,
                          "control_anchors": {str(v): f"0x{rate_to_increment(v):08X}" for v in (0, 64, 127)}},
        "conclusion": (
            "One foreground setter detour now publishes eight-lane LFO2 trigger/free mode, enable, phase reset, logarithmic "
            "rate and depth controls, while delegating cutoff commands to the proven Filter2 shim and all "
            "ordinary indices to the untouched stock setter. A published five-command lane-0 transaction "
            "advances the oscillator and drives audio exactly to the combined oracle."
        ),
        "scope_limit": (
            "The rate table assumes a 48 kHz stream and one LFO update per 32-sample callback. Trigger reset "
            "is proven at the setter ABI but is not yet attached to the stock note-event constructor."
        ),
        "next_target": (
            "Attach the trigger-mode reset to the authentic note-on path, then add waveform and "
            "mode fields without enlarging the 16-byte per-track state slot."
        ),
        "safety": "Default-disabled decompressed MAIN only; no ELE3 container or flashable SysEx was built.",
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
    if args.report: args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
