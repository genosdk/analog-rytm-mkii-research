#!/usr/bin/env python3
"""Drive LFO2 through the desktop-controller ABI and prove terminal-mode audio."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, RETURN_PC
from filter2_coefficient_slew_probe import oracle
from filter2_eight_lane_probe import FILTER2_STATE0, FILTER2_STATE_STRIDE
from filter2_lfo2_cutoff_binding_probe import (
    FILTER_SYMBOLS,
    LFO2_STATE0,
    LFO2_STATE_STRIDE,
    cpu_from_baseline,
    effective_target,
    modulation_q31,
    plane_lanes,
)
from filter2_unity_kernel_probe import MIXER, install_input, install_tables
from lfo2_extended_waveform_probe import waveform_q31
from lfo2_waveform_mode_probe import MODE_HALF, MODE_HOLD, MODE_ONE, advance_phase

from filter2_controller_service import ControllerState, EmulatorBridge


CALLBACKS = 22
LANES = (0, 1)
TERMINALS = {0: 0xFFFFFFFF, 1: 0x80000000}


def words_hash(words: list[int]) -> str:
    payload = b"".join((word & 0xFFFFFFFF).to_bytes(4, "big") for word in words)
    return hashlib.sha256(payload).hexdigest()


def probe(stock_path: Path, emulator_path: Path, report_path: Path | None = None) -> dict:
    stock = stock_path.read_bytes()
    digest = hashlib.sha256(stock).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")

    bridge = EmulatorBridge(stock_path, emulator_path)
    try:
        state = ControllerState(bridge)
        install_tables(bridge.bus, stock)
        publications = []

        def publish(lane: int, parameter: str, value) -> None:
            publications.append(state.set_lfo2(lane, parameter, value, "api"))

        state.set_filter(0, 56, "api")
        state.set_filter(1, 72, "api")
        for lane, waveform, mode, depth in (
            (0, "saw", "one-shot", 32),
            (1, "ramp", "half-shot", 48),
        ):
            publish(lane, "waveform", waveform)
            publish(lane, "mode", mode)
            publish(lane, "rate", 127)
            publish(lane, "depth", depth)
            publish(lane, "enable", True)
            publish(lane, "reset", 1)

        baseline = {
            "d": bridge.cpu.d.copy(), "a": bridge.cpu.a.copy(), "sr": bridge.cpu.sr,
            "ctrl": bridge.cpu.ctrl.copy(), "macsr": bridge.cpu.macsr,
            "mac_mask": bridge.cpu.mac_mask, "macc": bridge.cpu.macc.copy(),
        }
        expected_phases = [0] * 8
        callbacks = []
        schedule = {
            2: [(0, "waveform", "exponential"), (0, "depth", 80)],
            4: [(1, "mode", "hold")],
            6: [(1, "mode", "half-shot")],
        }

        for callback_index in range(CALLBACKS):
            changes = []
            for lane, parameter, value in schedule.get(callback_index, []):
                publish(lane, parameter, value)
                changes.append({"lane": lane, "parameter": parameter, "value": value})

            prior_filter = {}
            for lane in LANES:
                fbase = FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE
                prior_filter[lane] = tuple(bridge.bus.read(fbase + offset, 4) for offset in (0, 4, 8))

            install_input(bridge.bus, True)
            cpu = cpu_from_baseline(bridge._module, bridge.bus, baseline)
            cpu.pushl(RETURN_PC)
            cpu.pc = AUDIO_CALLBACK
            before = None
            multiply_calls = 0
            for _ in range(230_000):
                if cpu.pc == MIXER:
                    break
                if cpu.pc == FILTER_SYMBOLS["post_ingress"]:
                    before = plane_lanes(bridge.bus)
                if cpu.pc == FILTER_SYMBOLS["multiply"]:
                    multiply_calls += 1
                cpu.step()
            else:
                raise ValueError(f"controller sequence callback {callback_index} did not reach mixer")
            if before is None:
                raise ValueError("controller sequence missed post-ingress checkpoint")
            after = plane_lanes(bridge.bus)

            rows = []
            for lane in LANES:
                waveform = state.lfo2["waveform"][lane]
                mode = state.lfo2["mode"][lane]
                lbase = LFO2_STATE0 + lane * LFO2_STATE_STRIDE
                fbase = FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE
                increment = bridge.bus.read(lbase + 4, 4)
                depth = bridge.bus.read(lbase + 8, 4)
                phase = advance_phase(expected_phases[lane], increment, mode)
                expected_phases[lane] = phase
                wave = waveform_q31(waveform, phase, bridge.bus.read(fbase + 24, 4))
                modulation = modulation_q31(wave, depth)
                base_target = bridge.bus.read(fbase + 12, 4)
                target = effective_target(base_target, modulation)
                s1, s2, current = prior_filter[lane]
                expected_audio, next_s1, next_s2, _ = oracle(before[lane], current, target, s1, s2)
                observed_state = tuple(bridge.bus.read(fbase + offset, 4) for offset in (0, 4, 8, 16))
                wanted_state = (next_s1, next_s2, target, target)
                observed_lfo = (bridge.bus.read(lbase, 4), bridge.bus.read(lbase + 12, 4))
                wanted_lfo = (phase, modulation & 0xFFFFFFFF)
                if after[lane] != expected_audio or observed_state != wanted_state or observed_lfo != wanted_lfo:
                    raise ValueError(
                        f"controller sequence callback {callback_index} lane {lane} diverged: "
                        f"audio={after[lane] == expected_audio} state={observed_state!r}/{wanted_state!r} "
                        f"lfo={observed_lfo!r}/{wanted_lfo!r}"
                    )
                rows.append({
                    "lane": lane,
                    "waveform": waveform,
                    "mode": mode,
                    "phase": f"0x{phase:08X}",
                    "increment": f"0x{increment:08X}",
                    "modulation": f"0x{modulation & 0xFFFFFFFF:08X}",
                    "effective_target": f"0x{target:08X}",
                    "output_sha256": words_hash(after[lane]),
                    "terminal": phase == TERMINALS[lane],
                    "oracle_match": True,
                })
            expected_multiplies = 8 * 64 + 2 + int(state.lfo2["waveform"][0] == 5)
            if multiply_calls != expected_multiplies:
                raise ValueError(
                    f"callback {callback_index} multiply count {multiply_calls} != {expected_multiplies}"
                )
            callbacks.append({
                "index": callback_index,
                "parameter_changes": changes,
                "multiply_calls": multiply_calls,
                "lanes": rows,
                "all_oracles_match": True,
            })

        lane_rows = {lane: [item["lanes"][lane] for item in callbacks] for lane in LANES}
        first_terminal = {}
        for lane in LANES:
            indices = [row for row, value in enumerate(lane_rows[lane]) if value["terminal"]]
            if not indices:
                raise ValueError(f"lane {lane} never reached its terminal phase")
            first_terminal[lane] = indices[0]
            suffix = lane_rows[lane][indices[0]:]
            stable_fields = ("phase", "modulation", "effective_target")
            if any(any(row[field] != suffix[0][field] for field in stable_fields) for row in suffix[1:]):
                raise ValueError(f"lane {lane} terminal LFO state did not remain stable")

        held_phases = [lane_rows[1][index]["phase"] for index in (3, 4, 5)]
        if len(set(held_phases)) != 1:
            raise ValueError("controller hold-mode sequence advanced phase")
        if first_terminal != {0: 14, 1: 9}:
            raise ValueError(f"unexpected terminal callback indices: {first_terminal}")

        result = {
            "result": "PASS",
            "stock": {"path": str(stock_path), "sha256": digest},
            "controller_candidate_sha256": bridge.candidate_sha256,
            "sequence": {
                "callbacks": callbacks,
                "scheduled_parameter_changes": schedule,
                "publication_count": len(publications),
                "all_publications_use_controller_state": True,
            },
            "terminal_behavior": {
                "one_shot_lane": 0,
                "one_shot_terminal": "0xFFFFFFFF",
                "one_shot_first_terminal_callback": first_terminal[0],
                "half_shot_lane": 1,
                "half_shot_terminal": "0x80000000",
                "half_shot_first_terminal_callback": first_terminal[1],
                "hold_callbacks": [4, 5],
                "phase_modulation_and_target_stable_after_terminal": True,
                "every_audio_block_matches_filter_oracle": True,
            },
            "conclusion": (
                "The desktop controller ABI can change waveform, depth and run mode between live "
                "callbacks without discontinuity in the phase contract. One-shot clamps at the final "
                "full-cycle phase, half-shot clamps at the half-cycle phase, hold pauses and resumes, "
                "and every resulting audio block matches the exact Filter2 oracle."
            ),
            "next_target": (
                "Exercise enable/disable and reset transitions during active controller sequences, "
                "then bind observable runtime phase/status telemetry into the desktop state endpoint."
            ),
            "safety": (
                "Runtime-only emulator candidate; no ELE3 container, SysEx package, or flashable "
                "firmware image was created."
            ),
        }
        if report_path:
            report_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return result
    finally:
        bridge.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stock_main", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    print(json.dumps(probe(args.stock_main, args.emulator, args.report), indent=2))


if __name__ == "__main__":
    main()
