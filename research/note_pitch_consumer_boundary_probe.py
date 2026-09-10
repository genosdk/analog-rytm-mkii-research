#!/usr/bin/env python3
"""Prove the stock gated note-pitch consumer through the DSPI1 packet."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, prepared_machine
from br_hardware_sink_probe import CONTROL_DMA_INITIALIZER, PING_PONG_BASE, PING_PONG_SELECTOR, PING_PONG_STRIDE
from note_event_constructor_probe import EVENT_INPUT, NOTE_EVENT_CONSTRUCTOR, NOTE_ON, QWERTY_SOURCE_MASK, write_event
from note_pitch_publication_probe import LIVE_PITCH
from trigger_queue_probe import (
    CONTROL_SNAPSHOT_A, CONTROL_SNAPSHOT_POINTER, FULL_CALLBACK_STOP, QUEUE,
    QUEUE_CAPACITY, QUEUE_INITIALIZER, QUEUE_INSTALLER, RENDERER,
    load_emulator, stock_call,
)

TRACK_PARAMETER_SOURCE_BASE = 0x412FAC37
PITCH_SELECT_SOURCE = TRACK_PARAMETER_SOURCE_BASE + 0x6A
PITCH_SELECT_LIVE = 0x8000EA18
PITCH_SELECT_COPY_PC = 0x4011AEC4
LIVE_PITCH_READ_PC = 0x4011CA7E
SYNTH_PITCH_HIGH = 0x8000641A
SYNTH_PITCH_LOW = 0x8000641C
SYNTH_PITCH_WRITE_PCS = {SYNTH_PITCH_HIGH: 0x4010CF2C, SYNTH_PITCH_LOW: 0x4010CF36}
PACKET_SOURCE_READ_PCS = {SYNTH_PITCH_HIGH: 0x40077D64, SYNTH_PITCH_LOW: 0x40077D66}
PACKET_SOURCE_OFFSET = 0x0C
PACKET_WORDS = 510
NOTES = (48, 60, 72)
EXPECTED_PACKET_HASHES = {
    48: "d263890dd501a6d224f3d4896634d6b79a61237c8c8943f33afa7a09966d4900",
    60: "dfe093a8ffd41447cf3bd7693e91f198f62126ffe5e1e0fee8e54e7e9e07966e",
    72: "d987591faa30ca6f36f56d4942e2ad25c87677fea95b7f392ec5d342d45e2e7c",
}
EXPECTED_SRAM_DIFFERENCES = {
    (48, 60): (0x800048C7, 0x800048CB, 0x800050D7, 0x800050DB,
               0x80006389, 0x8000641B, 0x8000641D, 0x8000FE5B),
    (60, 72): (0x800048C7, 0x800048CB, 0x800050D7, 0x800050DB,
               0x80006389, 0x8000641B, 0x8000641D, 0x8000FE5B),
}


def run_traced_callback(cpu, base_sp: int) -> dict:
    cpu.a[7] = base_sp
    cpu.pc = AUDIO_CALLBACK
    renderer_arguments = []
    start = cpu.steps
    for _ in range(200_000):
        if cpu.pc == RENDERER:
            renderer_arguments.append([cpu.bus.read(cpu.a[7] + 4 + index * 4, 4) for index in range(4)])
        if cpu.pc == FULL_CALLBACK_STOP:
            return {"instructions": cpu.steps - start, "renderer_arguments": renderer_arguments}
        cpu.step()
    raise ValueError("audio interrupt did not reach its final RTE")


def run_vector(module, main_path: Path, note: int, gate: int, callback_count: int = 12) -> dict:
    bus, cpu, _ = prepared_machine(module, main_path)
    callback_sp = cpu.a[7]
    stock_call(cpu, CONTROL_DMA_INITIALIZER, [])
    stock_call(cpu, QUEUE_INITIALIZER, [QUEUE, 0, 0x419531F8, QUEUE_CAPACITY])
    stock_call(cpu, QUEUE_INSTALLER, [QUEUE])
    bus.write(CONTROL_SNAPSHOT_POINTER, 4, CONTROL_SNAPSHOT_A)
    bus.write(PITCH_SELECT_SOURCE, 1, gate)
    write_event(bus, event_type=NOTE_ON, note=note, source_mask=QWERTY_SOURCE_MASK)
    stock_call(cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])

    accesses = []
    original_read, original_write = bus.read, bus.write

    def traced_read(address: int, size: int) -> int:
        value = original_read(address, size)
        if address == LIVE_PITCH and size == 4:
            accesses.append({"kind": "read", "pc": f"0x{cpu.pc:08X}", "address": f"0x{address:08X}", "size": size, "value": f"0x{value:08X}"})
        elif address in PACKET_SOURCE_READ_PCS and size == 2:
            accesses.append({"kind": "packet_source_read", "pc": f"0x{cpu.pc:08X}", "address": f"0x{address:08X}", "size": size, "value": f"0x{value:04X}"})
        return value

    def traced_write(address: int, size: int, value: int) -> None:
        if address == PITCH_SELECT_LIVE and size == 1:
            accesses.append({"kind": "gate_copy", "pc": f"0x{cpu.pc:08X}", "address": f"0x{address:08X}", "size": size, "value": f"0x{value & 0xFF:02X}"})
        elif address in SYNTH_PITCH_WRITE_PCS and size == 2:
            accesses.append({"kind": "renderer_control_write", "pc": f"0x{cpu.pc:08X}", "address": f"0x{address:08X}", "size": size, "value": f"0x{value & 0xFFFF:04X}"})
        original_write(address, size, value)

    bus.read, bus.write = traced_read, traced_write
    try:
        callbacks = [run_traced_callback(cpu, callback_sp) for _ in range(callback_count)]
    finally:
        bus.read, bus.write = original_read, original_write

    selector = bus.read(PING_PONG_SELECTOR, 4)
    packet_base = PING_PONG_BASE + selector * PING_PONG_STRIDE
    packet = bytes(bus.read(packet_base + PACKET_SOURCE_OFFSET + index, 1) for index in range(PACKET_WORDS * 4))
    packet_words = [int.from_bytes(packet[index:index + 4], "big") for index in range(0, len(packet), 4)]
    renderer_pitch_arguments = [args[3] for callback in callbacks for args in callback["renderer_arguments"]]
    return {
        "note": note,
        "source_gate": gate,
        "encoded_pitch": f"0x{note << 16:08X}",
        "live_gate_after_callback": bus.read(PITCH_SELECT_LIVE, 1),
        "accesses": accesses,
        "renderer_pitch_arguments": [f"0x{value:08X}" for value in renderer_pitch_arguments],
        "queue_count_after_callbacks": bus.read(QUEUE + 4, 4),
        "callback_count": callback_count,
        "dspi1_packet": {
            "selector": selector,
            "words": len(packet_words),
            "sha256": hashlib.sha256(packet).hexdigest(),
            "pitch_payload_words": {"46": f"0x{packet_words[46]:08X}", "47": f"0x{packet_words[47]:08X}"},
        },
        "packet_words": packet_words,
        "sram": bytes(bus.sram),
    }


def differing_indices(left: list[int], right: list[int]) -> list[int]:
    return [index for index, (a, b) in enumerate(zip(left, right)) if a != b]


def probe(main_path: Path, emulator_path: Path) -> dict:
    digest = hashlib.sha256(main_path.read_bytes()).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    module = load_emulator(emulator_path)
    fixed = [run_vector(module, main_path, note, 0) for note in NOTES]
    live = [run_vector(module, main_path, note, 1) for note in NOTES]
    mode_vectors = {0: fixed[0], 1: live[0]}
    mode_vectors.update({mode: run_vector(module, main_path, 48, mode) for mode in (2, 3)})

    for vector in fixed:
        if vector["live_gate_after_callback"] != 0 or any(item["kind"] == "read" for item in vector["accesses"]):
            raise ValueError("disabled source gate unexpectedly selected live pitch")
        if set(vector["renderer_pitch_arguments"]) != {"0x003C0000"}:
            raise ValueError("disabled source gate did not select fixed note 60")
        if vector["dspi1_packet"]["sha256"] != EXPECTED_PACKET_HASHES[60]:
            raise ValueError("disabled source gate produced a note-dependent packet")

    for vector in live:
        note = vector["note"]
        reads = [item for item in vector["accesses"] if item["kind"] == "read"]
        copies = [item for item in vector["accesses"] if item["kind"] == "gate_copy"]
        if vector["live_gate_after_callback"] != 1:
            raise ValueError("enabled source gate was not copied to live state")
        if not copies or set(item["pc"] for item in copies) != {f"0x{PITCH_SELECT_COPY_PC:08X}"}:
            raise ValueError(f"unexpected live-gate copy trace for note {note}: {copies}")
        if not reads or set(item["pc"] for item in reads) != {f"0x{LIVE_PITCH_READ_PC:08X}"}:
            raise ValueError(f"unexpected live-pitch read trace for note {note}: {reads}")
        if set(item["value"] for item in reads) != {f"0x{note << 16:08X}"}:
            raise ValueError(f"live-pitch reads diverged for note {note}")
        if set(vector["renderer_pitch_arguments"]) != {f"0x{note << 16:08X}"}:
            raise ValueError(f"renderer did not receive note {note}")
        if vector["dspi1_packet"]["sha256"] != EXPECTED_PACKET_HASHES[note]:
            raise ValueError(f"unexpected DSPI1 packet for note {note}")
        writes = [item for item in vector["accesses"] if item["kind"] == "renderer_control_write"]
        reads_from_packetizer = [item for item in vector["accesses"] if item["kind"] == "packet_source_read"]
        expected_values = set(vector["dspi1_packet"]["pitch_payload_words"].values())
        expected_values = {f"0x{int(value, 16) & 0xFFFF:04X}" for value in expected_values}
        if {item["pc"] for item in writes} != {f"0x{pc:08X}" for pc in SYNTH_PITCH_WRITE_PCS.values()}:
            raise ValueError(f"unexpected renderer control writes for note {note}")
        if {item["pc"] for item in reads_from_packetizer} != {f"0x{pc:08X}" for pc in PACKET_SOURCE_READ_PCS.values()}:
            raise ValueError(f"unexpected packetizer source reads for note {note}")
        if {item["value"] for item in writes} != expected_values or {item["value"] for item in reads_from_packetizer} != expected_values:
            raise ValueError(f"renderer-to-packet values diverged for note {note}")

    chromatic_mode_matrix = []
    for mode, label, synth_live in ((0, "off", False), (1, "synth", True), (2, "sample", False), (3, "synth_and_sample", True)):
        vector = mode_vectors[mode]
        expected_pitch = "0x00300000" if synth_live else "0x003C0000"
        expected_hash = EXPECTED_PACKET_HASHES[48] if synth_live else EXPECTED_PACKET_HASHES[60]
        if set(vector["renderer_pitch_arguments"]) != {expected_pitch} or vector["dspi1_packet"]["sha256"] != expected_hash:
            raise ValueError(f"chromatic mode {mode} did not select the expected synth pitch path")
        chromatic_mode_matrix.append({"value": mode, "mode": label, "synth_uses_live_note": synth_live, "renderer_pitch": expected_pitch, "dspi1_sha256": expected_hash})

    comparisons = []
    for left, right in zip(live, live[1:]):
        notes = (left["note"], right["note"])
        word_differences = differing_indices(left["packet_words"], right["packet_words"])
        expected_words = [46, 47]
        if word_differences != expected_words:
            raise ValueError(f"unexpected {notes} packet differences: {word_differences}")
        sram_differences = tuple(0x80000000 + index for index, (a, b) in enumerate(zip(left["sram"], right["sram"])) if a != b)
        if sram_differences != EXPECTED_SRAM_DIFFERENCES[notes]:
            raise ValueError(f"unexpected {notes} SRAM differences: {sram_differences}")
        comparisons.append({"notes": list(notes), "differing_dspi1_word_indices": word_differences, "differing_sram_bytes": [f"0x{address:08X}" for address in sram_differences]})

    def public(vector: dict) -> dict:
        return {key: value for key, value in vector.items() if key not in {"packet_words", "sram"}}

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "pitch_select_path": {
            "track_source_byte": f"0x{PITCH_SELECT_SOURCE:08X}", "track_source_offset": "0x6A",
            "live_gate": f"0x{PITCH_SELECT_LIVE:08X}", "copy_pc": f"0x{PITCH_SELECT_COPY_PC:08X}",
            "live_pitch": f"0x{LIVE_PITCH:08X}", "read_pc": f"0x{LIVE_PITCH_READ_PC:08X}",
            "renderer": f"0x{RENDERER:08X}", "renderer_argument": 3,
        },
        "semantic_identity": {
            "field": "sound chromatic mode",
            "encoding": {"0": "off", "1": "synth", "2": "sample", "3": "synth_and_sample"},
            "synth_select_bit": 0,
            "evidence": "Modes 1 and 3 select live synth pitch; modes 0 and 2 select fixed note 60, matching independent OS 1.70+ sound-format definitions.",
        },
        "chromatic_mode_matrix": chromatic_mode_matrix,
        "dspi1_serialization": {
            "renderer_control_halfwords": [f"0x{SYNTH_PITCH_HIGH:08X}", f"0x{SYNTH_PITCH_LOW:08X}"],
            "renderer_write_pcs": [f"0x{SYNTH_PITCH_WRITE_PCS[address]:08X}" for address in (SYNTH_PITCH_HIGH, SYNTH_PITCH_LOW)],
            "packetizer_read_pcs": [f"0x{PACKET_SOURCE_READ_PCS[address]:08X}" for address in (SYNTH_PITCH_HIGH, SYNTH_PITCH_LOW)],
            "dspi1_word_indices": [46, 47],
            "packetizer": "0x40077D14",
        },
        "fixed_note_vectors": [public(vector) for vector in fixed],
        "live_note_vectors": [public(vector) for vector in live],
        "comparisons": comparisons,
        "conclusion": (
            "Stock MAIN copies sound chromatic mode at track source byte +0x6A into 0x8000EA18. Modes Off/Sample clear synth-select bit 0 and use fixed note 60; Synth/Synth+Sample set it, read 0x80006388, and pass note<<16 as renderer argument 3. Renderer writes 0x8000641A/1C are read by the packetizer into DSPI1 words 46/47, completing the pitch path to the outbound packet."
        ),
        "next_target": "Determine the board-level encoding represented by synth pitch halfwords 0x8000641A/1C and identify the PCS0 consumer; keep physical MIDI/USB ingress as a separate hardware trace.",
        "safety": "Emulation and synthetic RAM input only; firmware bytes were not modified.",
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
