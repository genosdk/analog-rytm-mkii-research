#!/usr/bin/env python3
"""Add four table-free LFO2 waveforms and four run modes to the CPU-side engine."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, RETURN_PC, prepared_machine
from filter2_bypass_canary_probe import execute_vector
from filter2_coefficient_slew_probe import control_to_q31, oracle
from filter2_eight_lane_probe import ALL_LANES_MASK, Builder, EXTENSION_BASE, FILTER2_STATE0, FILTER2_STATE_STRIDE, LANES
from filter2_lfo2_cutoff_binding_probe import (
    FILTER_SYMBOLS,
    LFO_UPDATE_BASE,
    LFO2_MASK_ADDRESS,
    LFO2_STATE0,
    LFO2_STATE_STRIDE,
    MODULATED_EXTENSION,
    MODULATED_EXTENSION_END,
    effective_target,
    modulation_q31,
    plane_lanes,
    triangle_q31,
)
from filter2_lfo2_state_canary_probe import image_offset
from filter2_publication_shim_probe import FOREGROUND_SETTER_CALLSITE
from filter2_unity_kernel_probe import FILTER2_MASK_ADDRESS, FLAGS_ADDRESS, MIXER, install_input, install_tables
from lfo2_control_publication_probe import (
    CONTROL_SHIM_BASE,
    DEPTH_INDEX_BASE,
    ENABLE_INDEX_BASE,
    FILTER_INDEX_BASE,
    RATE_INDEX_BASE,
)
from lfo2_note_trigger_reset_probe import build_candidate as build_note_candidate
from trigger_queue_probe import load_emulator, stock_call

WAVE_INDEX_BASE = 0x7FC0
MODE_INDEX_BASE = 0x7FC8
WAVE_TRIANGLE = 0
WAVE_SQUARE = 1
WAVE_SAW = 2
WAVE_RAMP = 3
MODE_LOOP = 0
MODE_ONE = 1
MODE_HALF = 2
MODE_HOLD = 3
CONFIG_OFFSET = 20
WAVE_MASK = 0x07
MODE_MASK = 0x18
WAVE_SHIM_BASE = 0x402B2ECC
WAVE_UPDATE_BASE = 0x402B25E0


def relocate_extension() -> bytes:
    old = bytes.fromhex(f"4eb9{LFO_UPDATE_BASE:08x}")
    new = bytes.fromhex(f"4eb9{WAVE_UPDATE_BASE:08x}")
    if MODULATED_EXTENSION.count(old) != 1:
        raise ValueError("LFO updater call signature changed")
    return MODULATED_EXTENSION.replace(old, new)


WAVE_EXTENSION = relocate_extension()


def assemble_wave_shim_explicit() -> tuple[bytes, dict[str, int]]:
    b = Builder(WAVE_SHIM_BASE)
    b.label("entry")
    b.emit(f"4ab9{FLAGS_ADDRESS:08x}")
    b.branch_word(0x6700, "existing")
    b.emit("202f0004")
    b.emit(f"0c800000{WAVE_INDEX_BASE:04x}")
    b.branch_word(0x6500, "existing")
    b.emit(f"0c800000{MODE_INDEX_BASE + 7:04x}")
    b.branch_word(0x6200, "existing")
    b.emit(f"0c800000{MODE_INDEX_BASE:04x}")
    b.branch_word(0x6500, "wave")

    b.label("mode")
    b.emit(f"04800000{MODE_INDEX_BASE:04x}")
    b.emit("222f0008")
    b.emit("0c8100000003")
    b.branch_word(0x6300, "mode_valid")
    b.emit("7203")
    b.label("mode_valid")
    b.emit("e789")                          # mode << 3
    b.emit("eb88")                          # lane * 32
    b.emit(f"41f9{FILTER2_STATE0 + CONFIG_OFFSET:08x}")
    b.emit("d1c0")
    b.emit("2410")
    b.emit("0282ffffffe7")                  # clear mode bits
    b.emit("8481")                          # OR.L D1,D2
    b.emit("2082")
    b.emit("4e75")

    b.label("wave")
    b.emit(f"04800000{WAVE_INDEX_BASE:04x}")
    b.emit("222f0008")
    b.emit("0c8100000003")
    b.branch_word(0x6300, "wave_valid")
    b.emit("7203")
    b.label("wave_valid")
    b.emit("eb88")
    b.emit(f"41f9{FILTER2_STATE0 + CONFIG_OFFSET:08x}")
    b.emit("d1c0")
    b.emit("2410")
    b.emit("0282fffffff8")                  # clear waveform bits
    b.emit("8481")
    b.emit("2082")
    b.emit("4e75")

    b.label("existing")
    b.emit(f"4ef9{CONTROL_SHIM_BASE:08x}")
    return b.finish()


WAVE_SHIM_BODY, WAVE_SHIM_SYMBOLS = assemble_wave_shim_explicit()
WAVE_SHIM_END = WAVE_SHIM_BASE + len(WAVE_SHIM_BODY)
CALLSITE_PATCH = bytes.fromhex(f"4eb9{WAVE_SHIM_BASE:08x}")


def assemble_wave_updater() -> tuple[bytes, dict[str, int]]:
    b = Builder(WAVE_UPDATE_BASE)
    b.label("entry")
    for lane in range(LANES):
        b.emit(f"3439{LFO2_MASK_ADDRESS:08x}")
        b.emit(f"0802{lane:04x}")
        b.branch_word(0x6700, f"lane_{lane}_copy")
        b.emit(f"41f9{LFO2_STATE0 + lane * LFO2_STATE_STRIDE:08x}")
        b.emit(f"43f9{FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE:08x}")
        b.branch_word(0x6100, "process")
        b.branch_word(0x6000, f"lane_{lane}_done")
        b.label(f"lane_{lane}_copy")
        b.emit(f"43f9{FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE:08x}")
        b.emit("2429000c")
        b.emit("23420010")
        b.label(f"lane_{lane}_done")
    b.emit("4e75")

    b.label("process")
    b.emit("2010")                          # phase
    b.emit("26290014")                      # config,D3
    b.emit("2c03")                          # config copy,D6
    b.emit("028600000018")                  # mode bits
    b.emit("0c8600000018")
    b.branch_word(0x6700, "phase_ready")    # hold
    b.emit("d0a80004")                      # phase += increment
    b.emit("0c8600000008")
    b.branch_word(0x6700, "one_mode")
    b.emit("0c8600000010")
    b.branch_word(0x6700, "half_mode")
    b.branch_word(0x6000, "store_phase")    # loop

    b.label("one_mode")
    b.emit("b0a80004")                      # wrapped result < increment
    b.branch_word(0x6500, "one_clamp")
    b.branch_word(0x6000, "store_phase")
    b.label("one_clamp")
    b.emit("70ff")                          # MOVEQ #-1,D0
    b.branch_word(0x6000, "store_phase")

    b.label("half_mode")
    b.emit("b0a80004")                      # wrapped result < increment
    b.branch_word(0x6500, "half_clamp")
    b.emit("0c8080000000")                  # upper half reached
    b.branch_word(0x6400, "half_clamp")     # BCC unsigned >=
    b.branch_word(0x6000, "store_phase")
    b.label("half_clamp")
    b.emit("203c80000000")

    b.label("store_phase")
    b.emit("2080")
    b.label("phase_ready")
    b.emit("2200")                          # phase -> waveform value
    b.emit("028300000007")                  # waveform selector
    b.emit("0c8300000001")
    b.branch_word(0x6700, "square")
    b.emit("0c8300000002")
    b.branch_word(0x6700, "saw")
    b.emit("0c8300000003")
    b.branch_word(0x6700, "ramp")
    b.emit("4a81")
    b.branch_word(0x6A00, "triangle_first")
    b.emit("4681")
    b.label("triangle_first")
    b.emit("d281")
    b.emit("04817fffffff")
    b.branch_word(0x6000, "apply_depth")

    b.label("square")
    b.emit("4a81")
    b.branch_word(0x6B00, "square_low")      # BMI
    b.emit("223c7fffffff")
    b.branch_word(0x6000, "apply_depth")
    b.label("square_low")
    b.emit("223c80000001")
    b.branch_word(0x6000, "apply_depth")

    b.label("saw")
    b.emit("0a8180000000")                  # phase XOR sign bit
    b.branch_word(0x6000, "apply_depth")
    b.label("ramp")
    b.emit("0a817fffffff")                  # descending saw

    b.label("apply_depth")
    b.emit("28280008")
    b.emit(f"4eb9{FILTER_SYMBOLS['multiply']:08x}")
    b.emit("2141000c")
    b.emit("2429000c")
    b.emit("d481")
    b.emit("4c82")
    b.emit("4a82")
    b.branch_word(0x6A00, "store_effective")
    b.emit("4282")
    b.label("store_effective")
    b.emit("23420010")
    b.emit("4e75")
    return b.finish()


WAVE_UPDATE_BODY, WAVE_UPDATE_SYMBOLS = assemble_wave_updater()
WAVE_UPDATE_END = WAVE_UPDATE_BASE + len(WAVE_UPDATE_BODY)


def waveform_q31(waveform: int, phase: int) -> int:
    if waveform == WAVE_TRIANGLE:
        return triangle_q31(phase)
    if waveform == WAVE_SQUARE:
        return 0x80000001 if phase & 0x80000000 else 0x7FFFFFFF
    if waveform == WAVE_SAW:
        return phase ^ 0x80000000
    if waveform == WAVE_RAMP:
        return phase ^ 0x7FFFFFFF
    raise ValueError("unsupported waveform")


def advance_phase(phase: int, increment: int, mode: int) -> int:
    if mode == MODE_HOLD:
        return phase
    total = phase + increment
    if mode == MODE_ONE:
        return min(0xFFFFFFFF, total)
    if mode == MODE_HALF:
        return min(0x80000000, total)
    return total & 0xFFFFFFFF


def build_candidate(stock: bytes, armed: bool) -> tuple[bytes, dict]:
    image, note_hook = build_note_candidate(stock, armed)
    candidate = bytearray(image)
    candidate[image_offset(FOREGROUND_SETTER_CALLSITE):image_offset(FOREGROUND_SETTER_CALLSITE) + 6] = CALLSITE_PATCH
    candidate[image_offset(EXTENSION_BASE):image_offset(MODULATED_EXTENSION_END)] = WAVE_EXTENSION
    candidate[image_offset(WAVE_SHIM_BASE):image_offset(WAVE_SHIM_END)] = WAVE_SHIM_BODY
    candidate[image_offset(WAVE_UPDATE_BASE):image_offset(WAVE_UPDATE_END)] = WAVE_UPDATE_BODY
    return bytes(candidate), {
        "note_trigger_reset": note_hook,
        "control_shim": [f"0x{WAVE_SHIM_BASE:08X}", f"0x{WAVE_SHIM_END:08X}"],
        "wave_update": [f"0x{WAVE_UPDATE_BASE:08X}", f"0x{WAVE_UPDATE_END:08X}"],
        "config_word_offset": CONFIG_OFFSET,
        "waveforms": {"0": "triangle", "1": "square", "2": "saw", "3": "ramp"},
        "modes": {"0": "loop", "1": "one", "2": "half", "3": "hold"},
        "armed": armed,
        "changed_byte_positions": sum(a != b for a, b in zip(stock, candidate)),
    }


def run_matrix(module, armed_path: Path, stock: bytes) -> dict:
    waveforms = [WAVE_TRIANGLE, WAVE_SQUARE, WAVE_SAW, WAVE_RAMP]
    modes = [MODE_LOOP, MODE_HOLD, MODE_ONE, MODE_HALF]
    phases = [0x20000000, 0x90000000, 0xF0000000, 0x70000000]
    increments = [0x10000000, 0x30000000, 0x20000000, 0x20000000]
    bus, cpu, _ = prepared_machine(module, armed_path)
    install_tables(bus, stock)
    bus.write(FILTER2_MASK_ADDRESS, 2, 0x000F)
    for lane in range(4):
        for index, value in (
            (FILTER_INDEX_BASE + lane, 64), (RATE_INDEX_BASE + lane, 0),
            (DEPTH_INDEX_BASE + lane, 127), (ENABLE_INDEX_BASE + lane, 1),
            (WAVE_INDEX_BASE + lane, waveforms[lane]), (MODE_INDEX_BASE + lane, modes[lane]),
        ):
            stock_call(cpu, WAVE_SHIM_BASE, [index, value])
        lbase = LFO2_STATE0 + lane * LFO2_STATE_STRIDE
        bus.write(lbase, 4, phases[lane]); bus.write(lbase + 4, 4, increments[lane])
        fbase = FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE
        for offset in (0, 4, 8): bus.write(fbase + offset, 4, 0)
    install_input(bus, True)
    cpu.pushl(RETURN_PC); cpu.pc = AUDIO_CALLBACK
    before = None; multiply_calls = 0
    for _ in range(600_000):
        if cpu.pc == MIXER: break
        if cpu.pc == FILTER_SYMBOLS["post_ingress"]: before = plane_lanes(bus)
        if cpu.pc == FILTER_SYMBOLS["multiply"]: multiply_calls += 1
        cpu.step()
    else: raise ValueError("waveform/mode callback did not reach mixer")
    if before is None: raise ValueError("waveform/mode ingress checkpoint missed")
    after = plane_lanes(bus)
    rows = []
    for lane in range(4):
        phase = advance_phase(phases[lane], increments[lane], modes[lane])
        wave = waveform_q31(waveforms[lane], phase)
        modulation = modulation_q31(wave, 0x7FFFFFFF)
        base = control_to_q31(64)
        target = effective_target(base, modulation)
        expected, s1, s2, _ = oracle(before[lane], 0, target)
        lbase = LFO2_STATE0 + lane * LFO2_STATE_STRIDE
        fbase = FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE
        observed = (bus.read(lbase, 4), bus.read(lbase + 12, 4), bus.read(fbase + 16, 4),
                    bus.read(fbase, 4), bus.read(fbase + 4, 4))
        wanted = (phase, modulation & 0xFFFFFFFF, target, s1, s2)
        if observed != wanted or after[lane] != expected:
            raise ValueError(f"waveform/mode lane {lane} diverged")
        rows.append({"lane": lane, "waveform": waveforms[lane], "mode": modes[lane],
                     "phase": f"0x{phase:08X}", "wave": f"0x{wave:08X}",
                     "effective_target": f"0x{target:08X}", "oracle_match": True})
    if multiply_calls != 4 * 65:
        raise ValueError("waveform/mode multiply count changed")
    return {"lanes": rows, "multiply_calls": multiply_calls, "all_oracles_match": True}


def probe(stock_path: Path, emulator_path: Path, candidate_output: Path | None = None) -> dict:
    stock = stock_path.read_bytes(); digest = hashlib.sha256(stock).hexdigest()
    if digest != EXPECTED_MAIN_SHA256: raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    disabled, disabled_build = build_candidate(stock, False)
    armed, armed_build = build_candidate(stock, True)
    module = load_emulator(emulator_path)
    temporary = None
    if candidate_output is None:
        temporary = tempfile.NamedTemporaryFile(suffix=".bin"); disabled_path = Path(temporary.name)
    else: disabled_path = candidate_output
    disabled_path.write_bytes(disabled)
    try:
        with tempfile.NamedTemporaryFile(suffix=".bin") as armed_temp:
            armed_path = Path(armed_temp.name); armed_path.write_bytes(armed)
            matrix = run_matrix(module, armed_path, stock)
        equivalence = []
        for active in (False, True):
            left = execute_vector(module, stock_path, stock, active, False)
            right = execute_vector(module, disabled_path, stock, active, True)
            fields = ("mixer_entry_registers", "mixer_input_sha256", "ingress_output_sha256",
                      "mixer_instructions", "mixer_output_writes", "mixer_output_sha256")
            identity = {field: left[field] == right[field] for field in fields}
            if not all(identity.values()): raise ValueError("disabled waveform candidate diverged")
            equivalence.append({"input": "active" if active else "zero", "bit_identical": identity})
    finally:
        if temporary is not None: temporary.close()
    return {
        "result": "PASS", "stock": {"path": str(stock_path), "sha256": digest},
        "disabled_candidate": {"path": str(candidate_output) if candidate_output else "temporary execution image",
                               "sha256": hashlib.sha256(disabled).hexdigest(), **disabled_build},
        "armed_emulation_probe": {"sha256": hashlib.sha256(armed).hexdigest(), **armed_build,
                                  "artifact_retained": False},
        "waveform_mode_matrix": matrix, "disabled_callback_stock_equivalence": equivalence,
        "conclusion": (
            "Triangle, square, saw and ramp now share the block-rate LFO2 engine. Loop wraps, one-shot "
            "clamps at one cycle, half-shot clamps at half a cycle, and hold preserves phase. One config "
            "word in each Filter2 lane's existing spare bytes carries both selectors."
        ),
        "scope_limit": "Sine, exponential and random waveforms and their extra data requirements remain open.",
        "next_target": "Add sine by lookup table, then define deterministic random state without expanding the slot.",
        "safety": "Default-disabled decompressed MAIN only; no ELE3 container or flashable SysEx was built.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("stock_main", type=Path); parser.add_argument("emulator", type=Path)
    parser.add_argument("--candidate-output", type=Path); parser.add_argument("--report", type=Path); args = parser.parse_args()
    result = probe(args.stock_main, args.emulator, args.candidate_output); encoded = json.dumps(result, indent=2) + "\n"
    if args.report: args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__": main()
