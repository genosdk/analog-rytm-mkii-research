#!/usr/bin/env python3
"""Complete the seven-waveform CPU-side LFO2 engine."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import tempfile
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, RETURN_PC, prepared_machine
from filter2_bypass_canary_probe import execute_vector
from filter2_coefficient_slew_probe import control_to_q31, oracle
from filter2_eight_lane_probe import Builder, FILTER2_STATE0, FILTER2_STATE_STRIDE, LANES
from filter2_lfo2_cutoff_binding_probe import (
    COMBINED_FLAGS,
    FILTER_SYMBOLS,
    LFO2_MASK_ADDRESS,
    LFO2_STATE0,
    LFO2_STATE_STRIDE,
    effective_target,
    cpu_from_baseline,
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
    RANDOM_INDEX_OFFSET,
    RESET_INDEX_BASE,
    TRIGGER_INDEX_BASE,
)
from lfo2_waveform_mode_probe import (
    CONFIG_OFFSET,
    MODE_HOLD,
    MODE_INDEX_BASE,
    MODE_LOOP,
    MODE_MASK,
    MODE_ONE,
    MODE_HALF,
    WAVE_INDEX_BASE,
    WAVE_MASK,
    WAVE_RAMP,
    WAVE_SAW,
    WAVE_SHIM_BASE,
    WAVE_SQUARE,
    WAVE_TRIANGLE,
    WAVE_UPDATE_BASE,
    advance_phase,
    build_candidate as build_wave_candidate,
)
from trigger_queue_probe import EVENT_VALUE, TRIGGER_RECORD, load_emulator, stock_call
from note_event_constructor_probe import (
    EVENT_INPUT,
    EVENT_TRACK,
    NOTE_EVENT_CONSTRUCTOR,
    NOTE_ON,
    QWERTY_SOURCE_MASK,
    write_event,
)


WAVE_SINE = 4
WAVE_EXPONENTIAL = 5
WAVE_RANDOM = 6
SINE_TABLE_BASE = 0x402B5800
RANDOM_TABLE_BASE = 0x402B5C00
TABLE_ENTRIES = 256


def signed_word(value: float) -> int:
    return int(round(value * 0x7FFFFFFF)) & 0xFFFFFFFF


SINE_VALUES = tuple(
    signed_word(math.sin(math.tau * index / TABLE_ENTRIES))
    for index in range(TABLE_ENTRIES)
)
SINE_TABLE = b"".join(value.to_bytes(4, "big") for value in SINE_VALUES)
_random = random.Random(0x172)
RANDOM_VALUES = tuple(_random.getrandbits(32) for _ in range(TABLE_ENTRIES))
RANDOM_TABLE = b"".join(value.to_bytes(4, "big") for value in RANDOM_VALUES)
EXPECTED_SINE_TABLE_SHA256 = "b32ed46792587c4b2a7ada85662bdd2281688c7def10113c1491b5c2ffd625db"
EXPECTED_RANDOM_TABLE_SHA256 = "07122443e735318f486cb566be03bc57799a527f985d687fc69ef71bee9e5df9"


def assemble_extended_wave_shim() -> tuple[bytes, dict[str, int]]:
    b = Builder(WAVE_SHIM_BASE)
    b.label("entry")
    b.emit(f"4ab9{FLAGS_ADDRESS:08x}")
    b.branch_word(0x6700, "existing")
    b.emit("202f0004")
    b.emit(f"0c800000{WAVE_INDEX_BASE:04x}")
    b.branch_word(0x6500, "existing")
    b.emit(f"0c800000{MODE_INDEX_BASE + LANES - 1:04x}")
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
    b.emit("e789")
    b.emit("eb88")
    b.emit(f"41f9{FILTER2_STATE0 + CONFIG_OFFSET:08x}")
    b.emit("d1c0")
    b.emit("2410")
    b.emit(f"0282{(~MODE_MASK & 0xFFFFFFFF):08x}")
    b.emit("8481")
    b.emit("2082")
    b.emit("4e75")

    b.label("wave")
    b.emit(f"04800000{WAVE_INDEX_BASE:04x}")
    b.emit("222f0008")
    b.emit(f"0c810000{WAVE_RANDOM:04x}")
    b.branch_word(0x6300, "wave_valid")
    b.emit(f"72{WAVE_RANDOM:02x}")
    b.label("wave_valid")
    b.emit("eb88")
    b.emit(f"41f9{FILTER2_STATE0 + CONFIG_OFFSET:08x}")
    b.emit("d1c0")
    b.emit("2410")
    b.emit(f"0282{(~WAVE_MASK & 0xFFFFFFFF):08x}")
    b.emit("8481")
    b.emit("2082")
    b.emit("4e75")

    b.label("existing")
    b.emit(f"4ef9{CONTROL_SHIM_BASE:08x}")
    return b.finish()


EXTENDED_WAVE_SHIM, EXTENDED_SHIM_SYMBOLS = assemble_extended_wave_shim()
EXTENDED_WAVE_SHIM_END = WAVE_SHIM_BASE + len(EXTENDED_WAVE_SHIM)


def assemble_extended_updater() -> tuple[bytes, dict[str, int]]:
    b = Builder(WAVE_UPDATE_BASE)
    b.label("entry")
    for lane in range(LANES):
        b.emit(f"0839{lane:04x}{LFO2_MASK_ADDRESS + 1:08x}")
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
    b.emit("2a00")                          # old phase for random wrap detection
    b.emit("26290014")                      # config,D3
    b.emit("2c03")
    b.emit(f"0286{MODE_MASK:08x}")
    b.emit(f"0c860000{MODE_HOLD << 3:04x}")
    b.branch_word(0x6700, "phase_ready")
    b.emit("d0a80004")
    b.emit(f"0c860000{MODE_ONE << 3:04x}")
    b.branch_word(0x6700, "one_mode")
    b.emit(f"0c860000{MODE_HALF << 3:04x}")
    b.branch_word(0x6700, "half_mode")
    b.branch_word(0x6000, "store_phase")

    b.label("one_mode")
    b.emit("b0a80004")
    b.branch_word(0x6500, "one_clamp")
    b.branch_word(0x6000, "store_phase")
    b.label("one_clamp")
    b.emit("70ff")
    b.branch_word(0x6000, "store_phase")

    b.label("half_mode")
    b.emit("b0a80004")
    b.branch_word(0x6500, "half_clamp")
    b.emit("0c8000008000")
    b.branch_word(0x6400, "half_clamp")
    b.branch_word(0x6000, "store_phase")
    b.label("half_clamp")
    b.emit("203c80000000")

    b.label("store_phase")
    b.emit("2080")
    b.label("phase_ready")
    b.emit("2200")
    b.emit(f"0283{WAVE_MASK:08x}")
    for waveform, label in (
        (WAVE_SQUARE, "square"),
        (WAVE_SAW, "saw"),
        (WAVE_RAMP, "ramp"),
        (WAVE_SINE, "sine"),
        (WAVE_EXPONENTIAL, "exponential"),
        (WAVE_RANDOM, "random"),
    ):
        b.emit(f"0c830000{waveform:04x}")
        b.branch_word(0x6700, label)

    b.emit("4a81")
    b.branch_word(0x6A00, "triangle_first")
    b.emit("4681")
    b.label("triangle_first")
    b.emit("d281")
    b.emit("04817fffffff")
    b.branch_word(0x6000, "apply_depth")

    b.label("square")
    b.emit("4a81")
    b.branch_word(0x6B00, "square_low")
    b.emit("223c7fffffff")
    b.branch_word(0x6000, "apply_depth")
    b.label("square_low")
    b.emit("223c80000001")
    b.branch_word(0x6000, "apply_depth")

    b.label("saw")
    b.emit("0a8180000000")
    b.branch_word(0x6000, "apply_depth")
    b.label("ramp")
    b.emit("0a817fffffff")
    b.branch_word(0x6000, "apply_depth")

    b.label("sine")
    b.emit("2200")
    b.emit("e089e089e089")                 # phase >> 24
    b.emit("0281000000ff")
    b.emit("e589")                          # table index * 4
    b.emit(f"45f9{SINE_TABLE_BASE:08x}")
    b.emit("d5c1")
    b.emit("2212")
    b.branch_word(0x6000, "apply_depth")

    b.label("exponential")
    b.emit("2200")
    b.emit("e289")                          # unsigned phase / 2 -> Q1.31
    b.emit("2801")
    b.emit(f"4eb9{FILTER_SYMBOLS['multiply']:08x}")
    b.emit("d281")                          # 2*p^2
    b.emit("0a8180000000")                 # 2*p^2 - 1 modulo Q1.31
    b.branch_word(0x6000, "apply_depth")

    b.label("random")
    b.emit(f"2229{RANDOM_INDEX_OFFSET:04x}")
    b.emit("b085")                          # new phase < old phase means wrap
    b.branch_word(0x6400, "random_lookup")
    b.emit("5281")
    b.emit("0281000000ff")
    b.emit(f"2341{RANDOM_INDEX_OFFSET:04x}")
    b.label("random_lookup")
    b.emit("e589")
    b.emit(f"45f9{RANDOM_TABLE_BASE:08x}")
    b.emit("d5c1")
    b.emit("2212")

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


EXTENDED_UPDATE, EXTENDED_UPDATE_SYMBOLS = assemble_extended_updater()
EXTENDED_UPDATE_END = WAVE_UPDATE_BASE + len(EXTENDED_UPDATE)


def waveform_q31(waveform: int, phase: int, random_index: int) -> int:
    if waveform == WAVE_TRIANGLE:
        return triangle_q31(phase)
    if waveform == WAVE_SQUARE:
        return 0x80000001 if phase & 0x80000000 else 0x7FFFFFFF
    if waveform == WAVE_SAW:
        return phase ^ 0x80000000
    if waveform == WAVE_RAMP:
        return phase ^ 0x7FFFFFFF
    if waveform == WAVE_SINE:
        return SINE_VALUES[(phase >> 24) & 0xFF]
    if waveform == WAVE_EXPONENTIAL:
        p = phase >> 1
        squared = ((p * p) >> 31) & 0xFFFFFFFF
        return ((squared << 1) & 0xFFFFFFFF) ^ 0x80000000
    if waveform == WAVE_RANDOM:
        return RANDOM_VALUES[random_index]
    raise ValueError("unsupported waveform")


def build_candidate(stock: bytes, armed: bool) -> tuple[bytes, dict]:
    image, inherited = build_wave_candidate(stock, armed)
    candidate = bytearray(image)
    candidate[image_offset(WAVE_SHIM_BASE):image_offset(EXTENDED_WAVE_SHIM_END)] = EXTENDED_WAVE_SHIM
    candidate[image_offset(WAVE_UPDATE_BASE):image_offset(EXTENDED_UPDATE_END)] = EXTENDED_UPDATE
    candidate[image_offset(SINE_TABLE_BASE):image_offset(SINE_TABLE_BASE) + len(SINE_TABLE)] = SINE_TABLE
    candidate[image_offset(RANDOM_TABLE_BASE):image_offset(RANDOM_TABLE_BASE) + len(RANDOM_TABLE)] = RANDOM_TABLE
    return bytes(candidate), {
        "inherited": inherited,
        "wave_shim": [f"0x{WAVE_SHIM_BASE:08X}", f"0x{EXTENDED_WAVE_SHIM_END:08X}"],
        "wave_update": [f"0x{WAVE_UPDATE_BASE:08X}", f"0x{EXTENDED_UPDATE_END:08X}"],
        "sine_table": [f"0x{SINE_TABLE_BASE:08X}", f"0x{SINE_TABLE_BASE + len(SINE_TABLE):08X}"],
        "random_table": [f"0x{RANDOM_TABLE_BASE:08X}", f"0x{RANDOM_TABLE_BASE + len(RANDOM_TABLE):08X}"],
        "waveforms": {
            "0": "triangle", "1": "square", "2": "saw", "3": "ramp",
            "4": "sine", "5": "exponential", "6": "deterministic random",
        },
        "changed_byte_positions": sum(a != b for a, b in zip(stock, candidate)),
        "armed": armed,
    }


def run_matrix(module, armed_path: Path, stock: bytes) -> dict:
    waveforms = list(range(7))
    phases = [
        0x10000000, 0x70000000, 0x20000000, 0x30000000,
        0x34000000, 0x50000000, 0xF8000000,
    ]
    increments = [
        0x08000000, 0x20000000, 0x18000000, 0x10000000,
        0x03000000, 0x04000000, 0x10000000,
    ]
    initial_random_indices = [0, 0, 0, 0, 0, 0, 17]
    bus, cpu, _ = prepared_machine(module, armed_path)
    install_tables(bus, stock)
    bus.write(FILTER2_MASK_ADDRESS, 2, 0x007F)
    for lane, waveform in enumerate(waveforms):
        for index, value in (
            (FILTER_INDEX_BASE + lane, 64),
            (RATE_INDEX_BASE + lane, 0),
            (DEPTH_INDEX_BASE + lane, 127),
            (ENABLE_INDEX_BASE + lane, 1),
            (WAVE_INDEX_BASE + lane, waveform),
            (MODE_INDEX_BASE + lane, MODE_LOOP),
        ):
            stock_call(cpu, WAVE_SHIM_BASE, [index, value])
        lbase = LFO2_STATE0 + lane * LFO2_STATE_STRIDE
        bus.write(lbase, 4, phases[lane])
        bus.write(lbase + 4, 4, increments[lane])
        fbase = FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE
        for offset in (0, 4, 8):
            bus.write(fbase + offset, 4, 0)
        bus.write(fbase + RANDOM_INDEX_OFFSET, 4, initial_random_indices[lane])
    install_input(bus, True)
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    before = None
    multiply_calls = 0
    for _ in range(190_000):
        if cpu.pc == MIXER:
            break
        if cpu.pc == FILTER_SYMBOLS["post_ingress"]:
            before = plane_lanes(bus)
        if cpu.pc == FILTER_SYMBOLS["multiply"]:
            multiply_calls += 1
        cpu.step()
    else:
        raise ValueError("extended-waveform callback did not reach mixer")
    if before is None:
        raise ValueError("extended-waveform ingress checkpoint missed")
    after = plane_lanes(bus)
    rows = []
    for lane, waveform in enumerate(waveforms):
        phase = advance_phase(phases[lane], increments[lane], MODE_LOOP)
        wrapped = phase < phases[lane]
        random_index = (initial_random_indices[lane] + int(wrapped)) & 0xFF
        wave = waveform_q31(waveform, phase, random_index)
        modulation = modulation_q31(wave, 0x7FFFFFFF)
        target = effective_target(control_to_q31(64), modulation)
        expected, s1, s2, _ = oracle(before[lane], 0, target)
        lbase = LFO2_STATE0 + lane * LFO2_STATE_STRIDE
        fbase = FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE
        observed = (
            bus.read(lbase, 4), bus.read(lbase + 12, 4),
            bus.read(fbase + 16, 4), bus.read(fbase, 4), bus.read(fbase + 4, 4),
            bus.read(fbase + RANDOM_INDEX_OFFSET, 4),
        )
        wanted = (phase, modulation & 0xFFFFFFFF, target, s1, s2, random_index)
        if observed != wanted or after[lane] != expected:
            raise ValueError(
                f"extended waveform lane {lane} diverged: {observed} != {wanted}"
            )
        rows.append({
            "lane": lane,
            "waveform": waveform,
            "phase": f"0x{phase:08X}",
            "wave": f"0x{wave:08X}",
            "random_index": random_index,
            "wrapped": wrapped,
            "oracle_match": True,
        })
    expected_calls = 7 * 65 + 1  # exponential shaping adds one Q1.31 multiply
    if multiply_calls != expected_calls:
        raise ValueError(f"extended waveform multiply count changed: {multiply_calls}")
    return {
        "lanes": rows,
        "multiply_calls": multiply_calls,
        "all_seven_waveforms_match": True,
    }


def run_nonlinear_reset_matrix(module, armed_path: Path, stock: bytes) -> dict:
    """Prove deterministic nonlinear state across callbacks and both reset paths."""
    lanes = (0, 1, 2)
    waveforms = (WAVE_SINE, WAVE_EXPONENTIAL, WAVE_RANDOM)
    increments = (0x28000000, 0x38000000, 0x60000000)
    depth = 0x7FFFFFFF
    bus, prepared, _ = prepared_machine(module, armed_path)
    baseline = {
        "d": prepared.d.copy(), "a": prepared.a.copy(), "sr": prepared.sr,
        "ctrl": prepared.ctrl.copy(), "macsr": prepared.macsr,
        "mac_mask": prepared.mac_mask, "macc": prepared.macc.copy(),
    }
    install_tables(bus, stock)
    bus.write(FLAGS_ADDRESS, 4, COMBINED_FLAGS)
    bus.write(FILTER2_MASK_ADDRESS, 2, 0x0007)
    for lane, waveform in zip(lanes, waveforms):
        for index, value in (
            (FILTER_INDEX_BASE + lane, 64),
            (DEPTH_INDEX_BASE + lane, 127),
            (ENABLE_INDEX_BASE + lane, 1),
            (WAVE_INDEX_BASE + lane, waveform),
            (MODE_INDEX_BASE + lane, MODE_LOOP),
        ):
            stock_call(prepared, WAVE_SHIM_BASE, [index, value])
        lbase = LFO2_STATE0 + lane * LFO2_STATE_STRIDE
        bus.write(lbase + 4, 4, increments[lane])
        fbase = FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE
        for offset in (0, 4, 8):
            bus.write(fbase + offset, 4, 0)

    def callback_snapshot(expected_phases: list[int], expected_random: list[int],
                          updater_only: bool = False) -> list[dict]:
        cpu = cpu_from_baseline(module, bus, baseline)
        if updater_only:
            stock_call(cpu, WAVE_UPDATE_BASE, [])
        else:
            install_input(bus, True)
            cpu.pushl(RETURN_PC)
            cpu.pc = AUDIO_CALLBACK
            for _ in range(220_000):
                if cpu.pc == MIXER:
                    break
                cpu.step()
            else:
                raise ValueError("nonlinear reset callback did not reach mixer")
        rows = []
        for lane, waveform in zip(lanes, waveforms):
            old_phase = expected_phases[lane]
            phase = advance_phase(old_phase, increments[lane], MODE_LOOP)
            expected_phases[lane] = phase
            if waveform == WAVE_RANDOM and phase < old_phase:
                expected_random[lane] = (expected_random[lane] + 1) & 0xFF
            wave = waveform_q31(waveform, phase, expected_random[lane])
            modulation = modulation_q31(wave, depth) & 0xFFFFFFFF
            lbase = LFO2_STATE0 + lane * LFO2_STATE_STRIDE
            fbase = FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE
            observed = {
                "lane": lane,
                "waveform": waveform,
                "phase": bus.read(lbase, 4),
                "last_modulation": bus.read(lbase + 12, 4),
                "random_index": bus.read(fbase + RANDOM_INDEX_OFFSET, 4),
            }
            wanted = (phase, modulation, expected_random[lane])
            if (observed["phase"], observed["last_modulation"], observed["random_index"]) != wanted:
                raise ValueError(f"nonlinear repeated callback lane {lane} diverged")
            rows.append({**observed, "phase": f"0x{phase:08X}",
                         "last_modulation": f"0x{modulation:08X}", "match": True})
        return rows

    def reset_state(kind: str) -> None:
        for lane in lanes:
            cpu = cpu_from_baseline(module, bus, baseline)
            if kind == "explicit":
                stock_call(cpu, WAVE_SHIM_BASE, [RESET_INDEX_BASE + lane, 1])
            else:
                stock_call(cpu, WAVE_SHIM_BASE, [TRIGGER_INDEX_BASE + lane, 1])
                write_event(bus, event_type=NOTE_ON, note=60 + lane, source_mask=QWERTY_SOURCE_MASK)
                bus.write(EVENT_INPUT + EVENT_TRACK, 4, lane)
                stock_call(cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])
            lbase = LFO2_STATE0 + lane * LFO2_STATE_STRIDE
            fbase = FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE
            observed = (bus.read(lbase, 4), bus.read(lbase + 12, 4),
                        bus.read(fbase + RANDOM_INDEX_OFFSET, 4))
            if observed != (0, 0, 0):
                raise ValueError(f"{kind} nonlinear reset lane {lane} diverged: {observed}")
        if kind == "trigger":
            # The reset itself is attached to the authentic constructor. The
            # storage-free callback fixture has no board-initialized command
            # queue, so consume its pending note record before the LFO-only run.
            bus.write(TRIGGER_RECORD, 4, 0)
            bus.write(EVENT_VALUE, 4, 0)

    phases = [0] * LANES
    random_indices = [0] * LANES
    uninterrupted = [callback_snapshot(phases, random_indices) for _ in range(6)]
    reset_state("explicit")
    phases = [0] * LANES
    random_indices = [0] * LANES
    explicit_prefix = [callback_snapshot(phases, random_indices, True) for _ in range(4)]
    # Move away from the deterministic prefix before exercising note retrigger.
    callback_snapshot(phases, random_indices, True)
    callback_snapshot(phases, random_indices, True)
    reset_state("trigger")
    phases = [0] * LANES
    random_indices = [0] * LANES
    trigger_prefix = [callback_snapshot(phases, random_indices, True) for _ in range(4)]
    if explicit_prefix != trigger_prefix:
        raise ValueError("explicit reset and authentic note retrigger prefixes differ")
    return {
        "waveforms": ["sine", "exponential", "deterministic random"],
        "uninterrupted_callbacks": uninterrupted,
        "explicit_reset_prefix": explicit_prefix,
        "note_retrigger_prefix": trigger_prefix,
        "callbacks_before_each_prefix": 6,
        "prefix_callbacks": 4,
        "prefix_execution": "direct updater calls after reset isolation",
        "explicit_and_note_prefixes_identical": True,
        "random_wraps_exercised": sum(
            row[2]["random_index"] for row in uninterrupted
        ) > 0,
    }


def probe(stock_path: Path, emulator_path: Path, candidate_output: Path | None = None) -> dict:
    stock = stock_path.read_bytes()
    digest = hashlib.sha256(stock).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    table_hashes = (
        hashlib.sha256(SINE_TABLE).hexdigest(),
        hashlib.sha256(RANDOM_TABLE).hexdigest(),
    )
    if table_hashes != (EXPECTED_SINE_TABLE_SHA256, EXPECTED_RANDOM_TABLE_SHA256):
        raise ValueError("extended LFO2 lookup-table digest changed")
    for start, data, label in (
        (SINE_TABLE_BASE, SINE_TABLE, "sine table"),
        (RANDOM_TABLE_BASE, RANDOM_TABLE, "random table"),
    ):
        existing = stock[image_offset(start):image_offset(start) + len(data)]
        if existing != bytes(len(data)):
            raise ValueError(f"{label} span is not stock-zero")
    if EXTENDED_UPDATE_END > SINE_TABLE_BASE:
        raise ValueError("extended updater overlaps sine table")

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
            armed_path = Path(armed_temp.name)
            armed_path.write_bytes(armed)
            matrix = run_matrix(module, armed_path, stock)
            nonlinear_reset = run_nonlinear_reset_matrix(module, armed_path, stock)
        equivalence = []
        for active in (False, True):
            left = execute_vector(module, stock_path, stock, active, False)
            right = execute_vector(module, disabled_path, stock, active, True)
            fields = (
                "mixer_entry_registers", "mixer_input_sha256", "ingress_output_sha256",
                "mixer_instructions", "mixer_output_writes", "mixer_output_sha256",
            )
            identity = {field: left[field] == right[field] for field in fields}
            if not all(identity.values()):
                raise ValueError("disabled extended-waveform candidate diverged")
            equivalence.append({
                "input": "active" if active else "zero",
                "bit_identical": identity,
            })
    finally:
        if temporary is not None:
            temporary.close()

    return {
        "result": "PASS",
        "stock": {"path": str(stock_path), "sha256": digest},
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
        "tables": {
            "entries_each": TABLE_ENTRIES,
            "sine_sha256": table_hashes[0],
            "random_seed": "0x172",
            "random_sha256": table_hashes[1],
        },
        "waveform_matrix": matrix,
        "nonlinear_reset_matrix": nonlinear_reset,
        "disabled_callback_stock_equivalence": equivalence,
        "conclusion": (
            "The CPU-side LFO2 engine now implements all seven target waveforms: "
            "triangle, square, saw, ramp, sine, exponential and deterministic random. "
            "Sine and random use locked 256-entry tables. Six consecutive nonlinear callbacks "
            "and matching four-step prefixes prove that explicit reset and authentic trigger-mode "
            "note-on restart phase, modulation and the random sequence deterministically without "
            "expanding the 16-byte LFO2 slot."
        ),
        "next_target": (
            "Exercise controller-driven parameter sequences through consecutive callbacks and "
            "characterize one-shot and half-shot terminal-state audio behavior."
        ),
        "safety": (
            "Default-disabled decompressed MAIN only; no ELE3 container or flashable SysEx was built."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
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
