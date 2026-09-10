#!/usr/bin/env python3
"""Bind eight deterministic CPU-side LFO2 lanes to Filter 2 cutoff targets."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, RETURN_PC, prepared_machine
from filter2_bypass_canary_probe import execute_vector
from filter2_coefficient_slew_probe import control_to_q31, oracle
from filter2_eight_lane_probe import (
    ALL_LANES_MASK,
    Builder,
    EXTENSION_BASE,
    FILTER2_STATE0,
    FILTER2_STATE_STRIDE,
    LANES,
    PLANE_LANE_STRIDE,
    plane_lanes,
)
from filter2_half_kernel_probe import sat32, signed32
from filter2_lfo2_state_canary_probe import HEADER_BYTES, STATE_BASE, image_offset
from filter2_publication_shim_probe import (
    SHIM_BASE,
    TABLE_BASE,
    VIRTUAL_INDEX_BASE,
    build_candidate as build_publication_candidate,
)
from filter2_unity_kernel_probe import (
    FILTER2_MASK_ADDRESS,
    FLAGS_ADDRESS,
    MIXER,
    OUTPUT_PLANE,
    install_input,
    install_tables,
    words_hash,
)
from trigger_queue_probe import load_emulator, stock_call

FLAG_FILTER2 = 0x00000001
FLAG_LFO2 = 0x00000002
COMBINED_FLAGS = FLAG_FILTER2 | FLAG_LFO2
LFO2_MASK_ADDRESS = STATE_BASE + 14
LFO2_STATE0 = STATE_BASE + HEADER_BYTES + LANES * FILTER2_STATE_STRIDE
LFO2_STATE_STRIDE = 16
BASE_TARGET_OFFSET = 12
EFFECTIVE_TARGET_OFFSET = 16
LFO_UPDATE_BASE = TABLE_BASE + 0x200


def assemble_extension() -> tuple[bytes, dict[str, int]]:
    """Eight-lane Filter 2 kernel with one block-rate LFO update call."""
    b = Builder(EXTENSION_BASE)
    b.label("entry")
    b.emit("4eb940117f00")                  # JSR stock ingress
    b.label("post_ingress")
    b.emit("40e7")                          # MOVE.W SR,-(SP)
    b.emit("4ab9402b4408")                  # TST.L flags
    b.branch_word(0x6700, "restore_sr")
    b.emit("48e7ffc0")                      # MOVEM.L D0-D7/A0-A1,-(SP)
    b.emit(f"4eb9{LFO_UPDATE_BASE:08x}")    # JSR block-rate LFO2 updater

    for lane in range(LANES):
        b.emit(f"0839{lane:04x}402b440d")
        b.branch_word(0x6700, f"lane_{lane}_skip")
        b.emit(f"41f9{OUTPUT_PLANE + lane * PLANE_LANE_STRIDE:08x}")
        b.emit(f"43f9{FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE:08x}")
        b.branch_word(0x6100, "process_lane")
        b.label(f"lane_{lane}_skip")

    b.label("restore_registers")
    b.emit("4cdf03ff")                      # MOVEM.L (SP)+,D0-D7/A0-A1
    b.label("restore_sr")
    b.emit("46df")                          # MOVE.W (SP)+,SR
    b.emit("4e75")                          # RTS

    b.label("process_lane")
    b.emit("7020")                          # MOVEQ #32,D0
    b.emit("28290008")                      # MOVE.L 8(A1),D4 current
    b.emit("2a290010")                      # MOVE.L 16(A1),D5 effective target
    b.emit("2c05")
    b.emit("9c84")
    b.emit("4c86")
    b.emit("4a86")
    b.branch_word(0x6A00, "positive_division")
    b.emit("06860000001f")
    b.label("positive_division")
    b.emit("7e05")
    b.emit("eea6")
    b.label("sample_loop")
    b.emit("d886")
    b.emit("4c84")
    b.emit("2210")
    b.emit("2411")
    b.emit("9282")
    b.emit("4c81")
    b.branch_word(0x6100, "multiply")
    b.emit("d481")
    b.emit("4c82")
    b.emit("2282")
    b.emit("26290004")
    b.emit("2202")
    b.emit("9283")
    b.emit("4c81")
    b.branch_word(0x6100, "multiply")
    b.emit("d681")
    b.emit("4c83")
    b.emit("23430004")
    b.emit("20c3")
    b.emit("5380")
    b.branch_word(0x6600, "sample_loop")
    b.emit("2a290010")                      # reload effective target
    b.emit("23450008")                      # snap current to effective target
    b.emit("4e75")

    b.label("multiply")
    b.emit("4c041c05")                      # signed 32x32 -> D1:D5
    b.emit("7e1f")
    b.emit("eea9")
    b.emit("da85")
    b.emit("8285")
    b.emit("4e75")
    return b.finish()


MODULATED_EXTENSION, FILTER_SYMBOLS = assemble_extension()
MODULATED_EXTENSION_END = EXTENSION_BASE + len(MODULATED_EXTENSION)


def assemble_lfo_updater() -> tuple[bytes, dict[str, int]]:
    """Unrolled mask dispatch plus a bipolar triangle/Q1.31 depth helper."""
    b = Builder(LFO_UPDATE_BASE)
    b.label("entry")
    for lane in range(LANES):
        b.emit(f"0839{lane:04x}{LFO2_MASK_ADDRESS + 1:08x}")
        b.branch_word(0x6700, f"lane_{lane}_copy_base")
        b.emit(f"41f9{LFO2_STATE0 + lane * LFO2_STATE_STRIDE:08x}")
        b.emit(f"43f9{FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE:08x}")
        b.branch_word(0x6100, "process_lfo")
        b.branch_word(0x6000, f"lane_{lane}_done")
        b.label(f"lane_{lane}_copy_base")
        b.emit(f"43f9{FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE:08x}")
        b.emit("2429000c")                  # MOVE.L base target,D2
        b.emit("23420010")                  # MOVE.L D2,effective target
        b.label(f"lane_{lane}_done")
    b.emit("4e75")

    b.label("process_lfo")
    b.emit("2010")                          # MOVE.L phase,D0
    b.emit("d0a80004")                      # ADD.L increment,D0
    b.emit("2080")                          # store advanced phase
    b.emit("2200")                          # MOVE.L D0,D1
    b.emit("4a81")                          # TST.L D1
    b.branch_word(0x6A00, "first_half")
    b.emit("4681")                          # NOT.L D1, fold second half
    b.label("first_half")
    b.emit("d281")                          # ADD.L D1,D1
    b.emit("04817fffffff")                  # SUBI.L #0x7FFFFFFF,D1
    b.emit("28280008")                      # MOVE.L depth,D4
    b.emit(f"4eb9{FILTER_SYMBOLS['multiply']:08x}")
    b.emit("2141000c")                      # store last signed modulation
    b.emit("2429000c")                      # MOVE.L base target,D2
    b.emit("d481")                          # ADD.L modulation,D2
    b.emit("4c82")                          # signed saturation
    b.emit("4a82")
    b.branch_word(0x6A00, "store_effective")
    b.emit("4282")                          # clamp negative cutoff to zero
    b.label("store_effective")
    b.emit("23420010")                      # store effective target
    b.emit("4e75")
    return b.finish()


LFO_UPDATE_BODY, LFO_SYMBOLS = assemble_lfo_updater()
LFO_UPDATE_END = LFO_UPDATE_BASE + len(LFO_UPDATE_BODY)


def filter_state(lane: int) -> int:
    return FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE


def lfo_state(lane: int) -> int:
    return LFO2_STATE0 + lane * LFO2_STATE_STRIDE


def triangle_q31(phase: int) -> int:
    folded = (~phase & 0xFFFFFFFF) if phase & 0x80000000 else phase
    return ((folded << 1) - 0x7FFFFFFF) & 0xFFFFFFFF


def modulation_q31(wave: int, depth: int) -> int:
    if not 0 <= depth <= 0x7FFFFFFF:
        raise ValueError("initial LFO2 depth contract is nonnegative Q1.31")
    return (signed32(wave) * depth) >> 31


def effective_target(base: int, modulation: int) -> int:
    return max(0, min(0x7FFFFFFF, sat32(base + modulation)))


def build_candidate(stock: bytes, armed: bool) -> tuple[bytes, dict]:
    image, publication = build_publication_candidate(stock, armed)
    candidate = bytearray(image)
    candidate[image_offset(EXTENSION_BASE):image_offset(EXTENSION_BASE) + len(MODULATED_EXTENSION)] = MODULATED_EXTENSION
    candidate[image_offset(LFO_UPDATE_BASE):image_offset(LFO_UPDATE_END)] = LFO_UPDATE_BODY
    if armed:
        candidate[image_offset(FLAGS_ADDRESS):image_offset(FLAGS_ADDRESS) + 4] = COMBINED_FLAGS.to_bytes(4, "big")
        candidate[image_offset(FILTER2_MASK_ADDRESS):image_offset(FILTER2_MASK_ADDRESS) + 2] = ALL_LANES_MASK.to_bytes(2, "big")
    return bytes(candidate), {
        "publication": publication,
        "flags": COMBINED_FLAGS if armed else 0,
        "filter_extension": [f"0x{EXTENSION_BASE:08X}", f"0x{MODULATED_EXTENSION_END:08X}"],
        "lfo_update_extension": [f"0x{LFO_UPDATE_BASE:08X}", f"0x{LFO_UPDATE_END:08X}"],
        "lfo2_state0": f"0x{LFO2_STATE0:08X}",
        "lfo2_mask": f"0x{LFO2_MASK_ADDRESS:08X}",
        "state_contract": {
            "lfo2_stride_bytes": LFO2_STATE_STRIDE,
            "lfo2_fields": {"phase": 0, "increment": 4, "depth": 8, "last_modulation": 12},
            "filter2_base_target_offset": BASE_TARGET_OFFSET,
            "filter2_effective_target_offset": EFFECTIVE_TARGET_OFFSET,
        },
        "changed_byte_positions": sum(a != b for a, b in zip(stock, candidate)),
    }


def cpu_from_baseline(module, bus, baseline: dict):
    cpu = module.CPU(bus)
    cpu.d = baseline["d"].copy()
    cpu.a = baseline["a"].copy()
    cpu.sr = baseline["sr"]
    cpu.ctrl = baseline["ctrl"].copy()
    cpu.macsr = baseline["macsr"]
    cpu.mac_mask = baseline["mac_mask"]
    cpu.macc = baseline["macc"].copy()
    return cpu


def run_integration(module, armed_path: Path, stock: bytes) -> dict:
    controls = [2, 24, 48, 64, 80, 104, 120, 127]
    phases = [0x00000000, 0x3FFFFFF0, 0x7FFFFFF0, 0x80000000,
              0xBFFFFFF0, 0xFFFFFFF0, 0x20000000, 0xE0000000]
    increments = [0x10000000, 0x20, 0x40, 0x10000000, 0x20000000, 0x40, 0x08000000, 0x30000000]
    depths = [0x60000000, 0x40000000, 0x7FFFFFFF, 0x20000000,
              0x30000000, 0x7FFFFFFF, 0x10000000, 0x50000000]
    lfo_mask = 0b10101101

    bus, prepared, _ = prepared_machine(module, armed_path)
    baseline = {
        "d": prepared.d.copy(), "a": prepared.a.copy(), "sr": prepared.sr,
        "ctrl": prepared.ctrl.copy(), "macsr": prepared.macsr,
        "mac_mask": prepared.mac_mask, "macc": prepared.macc.copy(),
    }
    install_tables(bus, stock)
    bus.write(LFO2_MASK_ADDRESS, 2, lfo_mask)
    bus.write(FILTER2_MASK_ADDRESS, 2, ALL_LANES_MASK)
    for lane, control in enumerate(controls):
        base = filter_state(lane)
        target = control_to_q31(control)
        for offset, value in ((0, 0), (4, 0), (8, 0), (16, target)):
            bus.write(base + offset, 4, value)
        stock_call(prepared, SHIM_BASE, [VIRTUAL_INDEX_BASE + lane, control])
        slot = lfo_state(lane)
        for offset, value in ((0, phases[lane]), (4, increments[lane]), (8, depths[lane]), (12, 0xA5A50000 | lane)):
            bus.write(slot + offset, 4, value)

    callbacks = []
    prior_filter = [(0, 0, 0) for _ in range(LANES)]
    expected_phases = phases.copy()
    disabled_last = [0xA5A50000 | lane for lane in range(LANES)]
    for callback_index in range(4):
        install_input(bus, True)
        cpu = cpu_from_baseline(module, bus, baseline)
        cpu.pushl(RETURN_PC)
        cpu.pc = AUDIO_CALLBACK
        before = None
        multiply_calls = 0
        start = cpu.steps
        for _ in range(240_000):
            if cpu.pc == MIXER:
                break
            if cpu.pc == FILTER_SYMBOLS["post_ingress"]:
                before = plane_lanes(bus)
            if cpu.pc == FILTER_SYMBOLS["multiply"]:
                multiply_calls += 1
            cpu.step()
        else:
            raise ValueError("LFO2/Filter2 callback did not reach mixer")
        if before is None:
            raise ValueError("LFO2/Filter2 callback missed post-ingress checkpoint")
        after = plane_lanes(bus)
        lanes = []
        for lane, control in enumerate(controls):
            enabled = bool(lfo_mask & (1 << lane))
            base_target = control_to_q31(control)
            if enabled:
                expected_phases[lane] = (expected_phases[lane] + increments[lane]) & 0xFFFFFFFF
                wave = triangle_q31(expected_phases[lane])
                modulation = modulation_q31(wave, depths[lane])
                disabled_last[lane] = modulation & 0xFFFFFFFF
                target = effective_target(base_target, modulation)
            else:
                modulation = None
                target = base_target
            s1, s2, current = prior_filter[lane]
            expected, next_s1, next_s2, _ = oracle(before[lane], current, target, s1, s2)
            if after[lane] != expected:
                raise ValueError(f"callback {callback_index} lane {lane} audio diverged")
            fbase, lbase = filter_state(lane), lfo_state(lane)
            observed_filter = tuple(bus.read(fbase + offset, 4) for offset in (0, 4, 8, 12, 16))
            expected_filter = (next_s1, next_s2, target, base_target, target)
            observed_lfo = tuple(bus.read(lbase + offset, 4) for offset in (0, 4, 8, 12))
            expected_lfo = (expected_phases[lane], increments[lane], depths[lane], disabled_last[lane])
            if observed_filter != expected_filter or observed_lfo != expected_lfo:
                raise ValueError(f"callback {callback_index} lane {lane} state diverged")
            prior_filter[lane] = (next_s1, next_s2, target)
            lanes.append({
                "lane": lane,
                "lfo_enabled": enabled,
                "phase": f"0x{expected_phases[lane]:08X}",
                "modulation": None if modulation is None else modulation,
                "base_target": f"0x{base_target:08X}",
                "effective_target": f"0x{target:08X}",
                "output_sha256": words_hash(after[lane]),
                "oracle_match": True,
            })
        expected_multiplies = LANES * 32 * 2 + lfo_mask.bit_count()
        if multiply_calls != expected_multiplies:
            raise ValueError("LFO2 plus Filter2 multiply count changed")
        callbacks.append({
            "index": callback_index,
            "instructions_to_mixer": cpu.steps - start,
            "multiply_calls": multiply_calls,
            "lanes": lanes,
            "all_oracles_match": True,
        })
    return {
        "controls": controls,
        "lfo_mask": f"0x{lfo_mask:04X}",
        "callbacks": callbacks,
        "phase_wrap_lanes": [2, 5],
        "base_targets_immutable": True,
        "lfo_off_uses_base_target": True,
        "all_oracles_match": True,
    }


def probe(stock_path: Path, emulator_path: Path, candidate_output: Path | None = None) -> dict:
    stock = stock_path.read_bytes()
    digest = hashlib.sha256(stock).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    if MODULATED_EXTENSION_END > SHIM_BASE:
        raise ValueError("modulated Filter2 extension overlaps publication shim")
    if LFO_UPDATE_BASE < TABLE_BASE + 512:
        raise ValueError("LFO2 updater overlaps Q1.31 control table")

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
            integration = run_integration(module, armed_path, stock)
            equivalence = []
            for active in (False, True):
                stock_run = execute_vector(module, stock_path, stock, active, False)
                disabled_run = execute_vector(module, disabled_path, stock, active, True)
                fields = ("mixer_entry_registers", "mixer_input_sha256", "ingress_output_sha256",
                          "mixer_instructions", "mixer_output_writes", "mixer_output_sha256")
                identity = {field: stock_run[field] == disabled_run[field] for field in fields}
                if not all(identity.values()):
                    raise ValueError("disabled LFO2 binding candidate diverged from stock")
                equivalence.append({"input": "active" if active else "zero", "bit_identical": identity})
    finally:
        if temporary is not None:
            temporary.close()

    return {
        "result": "PASS",
        "stock": {"path": str(stock_path), "sha256": digest},
        "disabled_candidate": {"path": str(candidate_output) if candidate_output else "temporary execution image",
                               "sha256": hashlib.sha256(disabled).hexdigest(), **disabled_build},
        "armed_emulation_probe": {"sha256": hashlib.sha256(armed).hexdigest(), **armed_build,
                                  "artifact_retained": False},
        "lfo2_filter2_integration": integration,
        "disabled_callback_stock_equivalence": equivalence,
        "conclusion": (
            "Eight block-rate triangle LFO2 lanes now advance phase in CPU state, apply signed Q1.31 depth, "
            "clamp a separate effective cutoff target, and drive the proven slewed two-pole Filter 2 kernel. "
            "The published base cutoff is never modified, and mask-off lanes consume it exactly."
        ),
        "scope_limit": (
            "This gate proves triangle waveform, free-running block-rate phase and cutoff depth for the eight "
            "audio lanes. The remaining five logical LFO2 slots, waveform/mode selection, trigger reset, "
            "parameter publication and physical hardware timing are not yet bound."
        ),
        "next_target": (
            "Add foreground publication for LFO2 increment/depth plus trigger-reset semantics, then extend "
            "the updater from the triangle/free subset to the complete reference waveform and mode contract."
        ),
        "safety": (
            "Only a default-disabled decompressed MAIN laboratory candidate is retained. The armed image was "
            "temporary; no ELE3 container or flashable SysEx was built."
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
