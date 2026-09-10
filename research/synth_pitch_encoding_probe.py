#!/usr/bin/env python3
"""Model stock synth-pitch encoding from note input to DSPI1 words 46/47.

This is an emulation-only characterization. It executes one authentic trigger
callback for every MIDI note and traces the stock logarithmic/exponential
helper only while renderer 0 is active. It does not modify firmware.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256
from note_pitch_consumer_boundary_probe import (
    NOTES,
    RENDERER,
    load_emulator,
    run_vector,
)

LOG_HELPER = 0x4011A0F8
LOG_HELPER_RETURN = 0x4011A120
EXP2_HELPER = 0x40095A14
EXP2_HELPER_RETURN = 0x40095A4E
EXP2_TABLE = 0x401C4318
CHANNEL_SCALES = (0x00062000, 0x0004168F)


def calibrated_halfword(exp2_output: int, scale: int) -> int:
    return max(0, min(0x7FFF, ((exp2_output + 1) * scale) >> 31))


def install_renderer_trace(module):
    original_step = module.CPU.step
    traces: dict[int, list[dict]] = {}
    states: dict[int, dict] = {}

    def step(cpu):
        key = id(cpu)
        state = states.setdefault(key, {"active": False, "return": None})
        pc = cpu.pc
        if pc == RENDERER:
            state["active"] = True
            state["return"] = cpu.bus.read(cpu.a[7], 4)
        if state["active"] and pc in (
            LOG_HELPER, LOG_HELPER_RETURN, EXP2_HELPER, EXP2_HELPER_RETURN
        ):
            traces.setdefault(key, []).append({
                "pc": f"0x{pc:08X}",
                "stack_argument": f"0x{cpu.bus.read(cpu.a[7] + 4, 4):08X}",
                "d0": f"0x{cpu.d[0]:08X}",
                "d1": f"0x{cpu.d[1]:08X}",
            })
        result = original_step(cpu)
        if state["active"] and cpu.pc == state["return"]:
            state["active"] = False
        return result

    module.CPU.step = step
    return traces


def compact_vector(vector: dict, trace: list[dict]) -> dict:
    exp_inputs = [item["stack_argument"] for item in trace if item["pc"] == f"0x{EXP2_HELPER:08X}"]
    exp_outputs = [item["d0"] for item in trace if item["pc"] == f"0x{EXP2_HELPER_RETURN:08X}"]
    if len(exp_inputs) != 2 or len(exp_outputs) != 2 or len(set(exp_inputs)) != 1 or len(set(exp_outputs)) != 1:
        raise ValueError(f"unexpected renderer pitch-helper trace: {trace}")
    words = vector["dspi1_packet"]["pitch_payload_words"]
    return {
        "note": vector["note"],
        "note_q16": vector["encoded_pitch"],
        "exp2_input": exp_inputs[0],
        "exp2_output": exp_outputs[0],
        "control_halfwords": [
            f"0x{int(words['46'], 16) & 0xFFFF:04X}",
            f"0x{int(words['47'], 16) & 0xFFFF:04X}",
        ],
        "dspi1_words": [words["46"], words["47"]],
        "dspi1_sha256": vector["dspi1_packet"]["sha256"],
    }


def probe(main_path: Path, emulator_path: Path) -> dict:
    digest = hashlib.sha256(main_path.read_bytes()).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    module = load_emulator(emulator_path)
    traces = install_renderer_trace(module)
    note_vectors = []
    for note in range(128):
        before = {key: len(value) for key, value in traces.items()}
        vector = run_vector(module, main_path, note, 1, callback_count=1)
        changed = [key for key, value in traces.items() if len(value) != before.get(key, 0)]
        if len(changed) != 1:
            raise ValueError(f"could not isolate renderer trace for note {note}")
        key = changed[0]
        note_vectors.append(compact_vector(vector, traces[key][before.get(key, 0):]))

    exp = [int(item["exp2_output"], 16) for item in note_vectors]
    channels = [
        [int(item["control_halfwords"][channel], 16) for item in note_vectors]
        for channel in range(2)
    ]
    if exp != sorted(exp) or any(values != sorted(values) for values in channels):
        raise ValueError("pitch encoding is not monotonic over MIDI notes 0..127")
    for index, helper_output in enumerate(exp):
        expected = [calibrated_halfword(helper_output, scale) for scale in CHANNEL_SCALES]
        actual = [values[index] for values in channels]
        if actual != expected:
            raise ValueError(f"calibration equation diverged at note {index}: {actual} != {expected}")
    # Wrapper 0x4011A122 clamps the low end to 0xF0000000; the octave
    # identity begins once note input has left that floor.
    for note in range(30, 116):
        if exp[note + 12] not in (2 * exp[note], 2 * exp[note] + 1):
            raise ValueError(f"exp2 octave law diverged at note {note}")
    anchors = {item["note"]: item for item in note_vectors if item["note"] in NOTES}
    expected_anchors = {
        48: ("0x0001965F", ["0x0013", "0x000C"]),
        60: ("0x00032CBF", ["0x0026", "0x0019"]),
        72: ("0x0006597F", ["0x004D", "0x0033"]),
    }
    for note, (helper, halfwords) in expected_anchors.items():
        if anchors[note]["exp2_output"] != helper or anchors[note]["control_halfwords"] != halfwords:
            raise ValueError(f"unexpected pitch anchor for note {note}: {anchors[note]}")

    mode_vectors = []
    for mode, label, live in (
        (0, "off", False), (1, "synth", True),
        (2, "sample", False), (3, "synth_and_sample", True),
    ):
        vector = run_vector(module, main_path, 48, mode, callback_count=1)
        words = vector["dspi1_packet"]["pitch_payload_words"]
        mode_vectors.append({
            "value": mode,
            "mode": label,
            "synth_uses_live_note": live,
            "renderer_pitch": vector["renderer_pitch_arguments"][0],
            "control_halfwords": [
                f"0x{int(words['46'], 16) & 0xFFFF:04X}",
                f"0x{int(words['47'], 16) & 0xFFFF:04X}",
            ],
        })

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "stock_math": {
            "log_helper": f"0x{LOG_HELPER:08X}",
            "exp2_helper": f"0x{EXP2_HELPER:08X}",
            "exp2_table": f"0x{EXP2_TABLE:08X}",
            "fractional_emac": "signed Q1.31 product, implicit left shift, extension bit 8 selects MAC/MSAC",
            "octave_law": "exp2_output[note+12] is exactly 2*output or 2*output+1",
            "low_note_floor": "notes 0..29 clamp to exp2 input 0xF0000000",
            "monotonic_notes_0_127": True,
        },
        "serialization": {
            "renderer_halfwords": ["0x8000641A", "0x8000641C"],
            "packetizer": "0x40077D14",
            "dspi1_word_indices": [46, 47],
            "word_tag": "0x80010000",
        },
        "calibration": {
            "channel_scales": [f"0x{scale:08X}" for scale in CHANNEL_SCALES],
            "equation": "clamp_0_7fff(((exp2_output + 1) * scale) >> 31)",
            "renderer_sequences": ["0x4010CE18..0x4010CE46", "0x4010CEC6..0x4010CEF4"],
            "all_128_vectors_match": True,
        },
        "board_route_boundary": {
            "established": [
                "DSPI1 SCK / processor A10 reaches FPGA CCLK / P53",
                "DSPI1 SOUT / processor B12 reaches FPGA DIN / P51",
            ],
            "unresolved": [
                "DSPI1 PCS0 / processor B13 consumer or glue-enable role",
                "DSPI1 SIN / processor C11 board connection",
                "FPGA application-fabric interpretation of words 46/47",
            ],
            "caution": "The parked high-impedance FPGA configuration image is not the live application fabric.",
        },
        "chromatic_mode_matrix_note_48": mode_vectors,
        "anchors": [anchors[note] for note in NOTES],
        "note_vectors": note_vectors,
        "conclusion": (
            "The stock renderer converts note Q16 through a monotonic exp2 helper, then "
            "emits two calibrated integer control channels in DSPI1 words 46/47. Both "
            "channels approximately double per octave; their exact board-level units "
            "remain an FPGA/application-hardware question."
        ),
        "next_target": (
            "Correlate the calibrated channel pair with other stock machine renderers and "
            "packet fields to infer its logical hardware role without physical pin hunting."
        ),
        "safety": "Stock firmware execution and synthetic note events under emulation only; no flashable image was produced.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("main_image", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = probe(args.main_image, args.emulator)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
