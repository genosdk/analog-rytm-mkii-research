#!/usr/bin/env python3
"""Generate the machine-readable build validation for the local controller."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from filter2_controller_service import ControllerState, EmulatorBridge, NOTE_KEYS, STATIC, default_paths


def validate(stock_main: Path | None = None, emulator: Path | None = None) -> dict:
    default_stock, default_emulator = default_paths()
    bridge = EmulatorBridge((stock_main or default_stock).resolve(), (emulator or default_emulator).resolve())
    try:
        state = ControllerState(bridge)
        publications = []
        for lane, value in enumerate((0, 16, 32, 48, 64, 80, 96, 127)):
            publications.append(state.set_filter(lane, value, "mouse"))
        before_notes = list(state.values)
        waveform_publications = [state.set_lfo2(lane, "waveform", lane % 7, "mouse") for lane in range(8)]
        mode_publications = [state.set_lfo2(lane, "mode", lane % 4, "mouse") for lane in range(8)]
        state.set_lfo2(0, "enable", True, "mouse")
        state.set_lfo2(0, "trigger", True, "mouse")
        state.set_lfo2(0, "reset", 1, "mouse")
        note_on = state.note("a", "on", 100)
        note_off = state.note("a", "off", 100)
        snapshot = state.snapshot()
        assets = {}
        for name in ("index.html", "styles.css", "app.js"):
            path = STATIC / name
            assets[name] = {"exists": path.is_file(), "bytes": path.stat().st_size if path.is_file() else 0}
        checks = {
            "eight_real_publications": len(publications) == 8,
            "all_single_aligned_store": all(item["single_aligned_store"] for item in publications),
            "virtual_index_range_exact": [item["virtual_index"] for item in publications]
            == [f"0x{0x7FF8 + lane:04X}" for lane in range(8)],
            "final_controls_exact": snapshot["filter2"]["values"] == [0, 16, 32, 48, 64, 80, 96, 127],
            "note_path_did_not_mutate_filter2": snapshot["filter2"]["values"] == before_notes,
            "seven_waveforms_publish": snapshot["lfo2"]["waveform"] == [0, 1, 2, 3, 4, 5, 6, 0],
            "four_modes_publish": snapshot["lfo2"]["mode"] == [0, 1, 2, 3, 0, 1, 2, 3],
            "waveform_index_range_exact": [item["virtual_index"] for item in waveform_publications]
            == [f"0x{0x7FC0 + lane:04X}" for lane in range(8)],
            "mode_index_range_exact": [item["virtual_index"] for item in mode_publications]
            == [f"0x{0x7FC8 + lane:04X}" for lane in range(8)],
            "note_on_off_exact": note_on["note"] == 48 and note_off["note"] == 48 and not snapshot["notes"]["held"],
            "firmware_keydown_transport_exact": (
                note_on["firmware_transport"] == "emulated_stock_trigger"
                and note_on["stock_trigger"]["encoded_pitch"] == "0x00300000"
                and note_on["stock_trigger"]["live_pitch"] == "0x00300000"
                and note_on["stock_trigger"]["pitch_consumer"]["source_gate"] == "0x01"
                and note_on["stock_trigger"]["pitch_consumer"]["chromatic_mode"] == "synth"
                and note_on["stock_trigger"]["pitch_consumer"]["live_gate"] == "0x01"
                and note_on["stock_trigger"]["pitch_consumer"]["renderer_input_proven"]
                and note_on["stock_trigger"]["pitch_consumer"]["reads"]
                == [{"pc": "0x4011CA7E", "value": "0x00300000"}]
                and note_on["stock_trigger"]["queued_command"]["code"] == 0x1F
                and note_on["stock_trigger"]["queued_command"]["track_mask"] == "0x00000001"
            ),
            "firmware_keyup_transport_exact": (
                note_off["firmware_transport"] == "emulated_stock_release"
                and note_off["stock_release"]["accepted"]
                and note_off["stock_release"]["constructor_instructions"] == 45
                and note_off["stock_release"]["held_source_mask_after"] == "0x00000000"
                and note_off["stock_release"]["callback_cases"] == [1]
            ),
            "all_static_assets_present": all(item["exists"] and item["bytes"] > 0 for item in assets.values()),
            "no_flashable_image_created": not snapshot["emulator"]["flashable_image_created"],
        }
        if not all(checks.values()):
            raise ValueError(f"controller validation failed: {checks}")
        return {
            "result": "PASS",
            "controller": {
                "filter2_lanes": 8,
                "control_domain": [0, 127],
                "mouse_drag": True,
                "mouse_wheel": True,
                "keyboard_adjustment": True,
                "qwerty_note_keys": NOTE_KEYS,
                "lfo2_waveforms": list(snapshot["lfo2"]["waveforms"]),
                "lfo2_modes": list(snapshot["lfo2"]["modes"]),
            },
            "publications": publications,
            "lfo2_publications": waveform_publications + mode_publications,
            "note_events": [note_on, note_off],
            "assets": assets,
            "checks": checks,
            "scope_limit": "QWERTY key-down and key-up use the stock constructor; Sound Chromatic Mode Synth selects live pitch at the renderer input. Physical MIDI/USB ingress remains untraced.",
            "safety": "Runtime emulator arming only; no ELE3, SysEx, or flashable image was created.",
        }
    finally:
        bridge.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-main", type=Path)
    parser.add_argument("--emulator", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate(args.stock_main, args.emulator)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
