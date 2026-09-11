#!/usr/bin/env python3
"""Attach trigger-mode LFO2 phase reset to the authentic stock note-on constructor."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256, prepared_machine
from filter2_eight_lane_probe import Builder, FILTER2_STATE0, FILTER2_STATE_STRIDE, LANES
from filter2_lfo2_cutoff_binding_probe import COMBINED_FLAGS, LFO2_STATE0, LFO2_STATE_STRIDE
from filter2_lfo2_state_canary_probe import image_offset
from filter2_unity_kernel_probe import FLAGS_ADDRESS
from lfo2_control_publication_probe import (
    CONTROL_SHIM_BASE,
    RANDOM_INDEX_OFFSET,
    TRIGGER_INDEX_BASE,
    TRIGGER_MASK_ADDRESS,
    build_candidate as build_publication_candidate,
)
from note_event_constructor_probe import (
    EVENT_INPUT,
    EVENT_TRACK,
    NOTE_EVENT_CONSTRUCTOR,
    NOTE_OFF,
    NOTE_ON,
    QWERTY_SOURCE_MASK,
    write_event,
)
from trigger_queue_probe import load_emulator, stock_call

EPILOGUE_PATCH = 0x40118AE6
EPILOGUE_CONTINUE = 0x40118AEC
EPILOGUE_ORIGINAL = bytes.fromhex("46c14cd7041c")
NOTE_HOOK_BASE = 0x402B2F5C
CALLSITE_PATCH = bytes.fromhex(f"4eb9{NOTE_HOOK_BASE:08x}")


def assemble_note_hook() -> tuple[bytes, dict[str, int]]:
    b = Builder(NOTE_HOOK_BASE)
    b.label("entry")
    b.emit("241f")                          # discard JSR return into restored D2
    b.emit(f"4ab9{FLAGS_ADDRESS:08x}")
    b.branch_word(0x6700, "epilogue")
    b.emit("0ca800000001000c")              # CMPI.L #NOTE_ON,12(A0)
    b.branch_word(0x6600, "epilogue")
    b.emit("2410")                          # MOVE.L event track,D2
    b.emit("0c8200000007")
    b.branch_word(0x6200, "epilogue")       # only eight audio lanes
    b.emit(f"0539{TRIGGER_MASK_ADDRESS + 1:08x}")  # BTST D2,trigger mask
    b.branch_word(0x6700, "epilogue")
    b.emit("2602")                          # preserve lane for Filter2 stride
    b.emit("e98a")                          # lane * 16
    b.emit(f"43f9{LFO2_STATE0:08x}")
    b.emit("d3c2")                          # ADDA.L D2,A1
    b.emit("4291")                          # CLR.L phase
    b.emit("42a9000c")                      # CLR.L last modulation
    b.emit("eb8b")                          # preserved lane * 32
    b.emit(f"43f9{FILTER2_STATE0 + 24:08x}")
    b.emit("d3c3")
    b.emit("4291")                          # deterministic random table index
    b.label("epilogue")
    b.emit(EPILOGUE_ORIGINAL.hex())
    b.emit(f"4ef9{EPILOGUE_CONTINUE:08x}")
    return b.finish()


NOTE_HOOK_BODY, NOTE_HOOK_SYMBOLS = assemble_note_hook()
NOTE_HOOK_END = NOTE_HOOK_BASE + len(NOTE_HOOK_BODY)


def build_candidate(stock: bytes, armed: bool) -> tuple[bytes, dict]:
    image, publication = build_publication_candidate(stock, armed)
    if stock[image_offset(EPILOGUE_PATCH):image_offset(EPILOGUE_PATCH) + 6] != EPILOGUE_ORIGINAL:
        raise ValueError("note-constructor epilogue signature changed")
    candidate = bytearray(image)
    candidate[image_offset(EPILOGUE_PATCH):image_offset(EPILOGUE_PATCH) + 6] = CALLSITE_PATCH
    candidate[image_offset(NOTE_HOOK_BASE):image_offset(NOTE_HOOK_END)] = NOTE_HOOK_BODY
    return bytes(candidate), {
        "publication": publication,
        "epilogue_patch": f"0x{EPILOGUE_PATCH:08X}",
        "original": EPILOGUE_ORIGINAL.hex(),
        "replacement": CALLSITE_PATCH.hex(),
        "hook": [f"0x{NOTE_HOOK_BASE:08X}", f"0x{NOTE_HOOK_END:08X}"],
        "hook_bytes": len(NOTE_HOOK_BODY),
        "symbols": {name: f"0x{address:08X}" for name, address in NOTE_HOOK_SYMBOLS.items()},
        "armed": armed,
        "changed_byte_positions": sum(a != b for a, b in zip(stock, candidate)),
    }


def lfo_slot(track: int) -> int:
    return LFO2_STATE0 + track * LFO2_STATE_STRIDE


def constructor_call(module, image_path: Path, track: int, event_type: int,
                     trigger_mode: bool, phase: int, last: int) -> dict:
    bus, cpu, _ = prepared_machine(module, image_path)
    bus.write(FLAGS_ADDRESS, 4, COMBINED_FLAGS)
    stock_call(cpu, CONTROL_SHIM_BASE, [TRIGGER_INDEX_BASE + track, int(trigger_mode)])
    slot = lfo_slot(track)
    bus.write(slot, 4, phase)
    bus.write(slot + 4, 4, 0x01020304)
    bus.write(slot + 8, 4, 0x50607080)
    bus.write(slot + 12, 4, last)
    filter_slot = FILTER2_STATE0 + track * FILTER2_STATE_STRIDE
    random_index = 0x60 + track
    bus.write(filter_slot + RANDOM_INDEX_OFFSET, 4, random_index)
    write_event(bus, event_type=event_type, note=60 + track, source_mask=QWERTY_SOURCE_MASK)
    bus.write(EVENT_INPUT + EVENT_TRACK, 4, track)
    steps = stock_call(cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])
    state = [bus.read(slot + offset, 4) for offset in (0, 4, 8, 12)]
    return {
        "steps": steps,
        "state": state,
        "random_index": bus.read(filter_slot + RANDOM_INDEX_OFFSET, 4),
        "random_index_before": random_index,
        "trigger_mask": bus.read(TRIGGER_MASK_ADDRESS, 2),
    }


def mode_matrix(module, armed_path: Path) -> list[dict]:
    rows = []
    for track in range(LANES):
        trigger_mode = not bool(track & 1)
        phase = 0xA0000000 | track
        last = 0xB0000000 | track
        result = constructor_call(module, armed_path, track, NOTE_ON, trigger_mode, phase, last)
        expected = [0, 0x01020304, 0x50607080, 0] if trigger_mode else [phase, 0x01020304, 0x50607080, last]
        if result["state"] != expected:
            raise ValueError(f"track {track} trigger/free note-on gating diverged")
        expected_random = 0 if trigger_mode else result["random_index_before"]
        if result["random_index"] != expected_random:
            raise ValueError(f"track {track} random-index reset gating diverged")
        rows.append({"track": track, "mode": "trigger" if trigger_mode else "free",
                     "constructor_instructions": result["steps"],
                     "phase_after": f"0x{result['state'][0]:08X}",
                     "last_after": f"0x{result['state'][3]:08X}",
                     "random_index_after": result["random_index"],
                     "reset_match": True})
    return rows


def note_off_nonreset(module, armed_path: Path) -> dict:
    track = 2
    bus, cpu, _ = prepared_machine(module, armed_path)
    bus.write(FLAGS_ADDRESS, 4, COMBINED_FLAGS)
    stock_call(cpu, CONTROL_SHIM_BASE, [TRIGGER_INDEX_BASE + track, 1])
    write_event(bus, event_type=NOTE_ON, note=64, source_mask=QWERTY_SOURCE_MASK)
    bus.write(EVENT_INPUT + EVENT_TRACK, 4, track)
    stock_call(cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])
    slot = lfo_slot(track)
    before = [0xDEADBEEF, 0x01020304, 0x50607080, 0xCAFEBABE]
    for index, value in enumerate(before):
        bus.write(slot + index * 4, 4, value)
    filter_slot = FILTER2_STATE0 + track * FILTER2_STATE_STRIDE
    bus.write(filter_slot + RANDOM_INDEX_OFFSET, 4, 0x55)
    write_event(bus, event_type=NOTE_OFF, note=64, source_mask=QWERTY_SOURCE_MASK)
    bus.write(EVENT_INPUT + EVENT_TRACK, 4, track)
    steps = stock_call(cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])
    after = [bus.read(slot + index * 4, 4) for index in range(4)]
    if after != before:
        raise ValueError("note-off reset LFO2 state")
    if bus.read(filter_slot + RANDOM_INDEX_OFFSET, 4) != 0x55:
        raise ValueError("note-off reset deterministic random index")
    return {"track": track, "constructor_instructions": steps,
            "state_preserved": [f"0x{value:08X}" for value in after]}


def constructor_snapshot(module, image_path: Path, event_type: int) -> dict:
    bus, cpu, _ = prepared_machine(module, image_path)
    write_event(bus, event_type=event_type, note=60, source_mask=QWERTY_SOURCE_MASK)
    steps = stock_call(cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])
    watched = [(EVENT_INPUT, 0x28), (0x42AC4038, 0x38), (0x42AAF558, 4)]
    digest = hashlib.sha256()
    for address, size in watched:
        digest.update(bytes(bus.read(address + offset, 1) for offset in range(size)))
    return {"instructions": steps, "watched_sha256": digest.hexdigest(),
            "d": cpu.d, "a": cpu.a, "sr": cpu.sr}


def probe(stock_path: Path, emulator_path: Path, candidate_output: Path | None = None) -> dict:
    stock = stock_path.read_bytes()
    digest = hashlib.sha256(stock).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
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
            matrix = mode_matrix(module, armed_path)
            note_off = note_off_nonreset(module, armed_path)
        disabled_equivalence = []
        for event_type in (NOTE_ON, NOTE_OFF):
            stock_result = constructor_snapshot(module, stock_path, event_type)
            disabled_result = constructor_snapshot(module, disabled_path, event_type)
            semantic_fields = ("watched_sha256", "d", "a", "sr")
            identity = {field: stock_result[field] == disabled_result[field] for field in semantic_fields}
            if not all(identity.values()):
                raise ValueError("disabled note hook changed constructor semantics")
            disabled_equivalence.append({"event": "note_on" if event_type == NOTE_ON else "note_off",
                                         "semantic_identity": identity,
                                         "added_instructions": disabled_result["instructions"] - stock_result["instructions"]})
    finally:
        if temporary is not None: temporary.close()
    return {
        "result": "PASS", "stock": {"path": str(stock_path), "sha256": digest},
        "disabled_candidate": {"path": str(candidate_output) if candidate_output else "temporary execution image",
                               "sha256": hashlib.sha256(disabled).hexdigest(), **disabled_build},
        "armed_emulation_probe": {"sha256": hashlib.sha256(armed).hexdigest(), **armed_build,
                                  "artifact_retained": False},
        "note_on_mode_matrix": matrix, "note_off_nonreset": note_off,
        "disabled_constructor_equivalence": disabled_equivalence,
        "conclusion": (
            "The authentic stock note constructor now detours only at its shared epilogue. Note-on resets "
            "phase and last modulation for each trigger-mode audio lane, free-mode note-on preserves them, "
            "and note-off never resets. The hook then executes the displaced stock epilogue exactly."
        ),
        "scope_limit": (
            "The hook covers the eight audio lanes currently processed by Filter2. Tracks 8..12 and the "
            "remaining LFO modes/waveforms are intentionally outside this gate."
        ),
        "next_target": (
            "Pack waveform and one-shot/half/hold mode selection into the existing ABI, beginning with "
            "square, saw and ramp because they need no lookup table."
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
