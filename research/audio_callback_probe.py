#!/usr/bin/env python3
"""Execute one stock OS 1.72 audio callback and trace the sample-BR frame slot.

The probe calls the stock machine-table initializer, supplies the board-level
audio TCD pointers normally installed by device startup, and stops before the
callback returns into the RTOS. It is emulation-only and never changes firmware.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

EXPECTED_MAIN_SHA256 = "5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772"
RETURN_PC = 0x400FCECC
MACHINE_TABLE_INITIALIZER = 0x4009BB0A
AUDIO_DMA_INITIALIZER = 0x401178FA
AUDIO_CALLBACK = 0x4011B3AE
CALLBACK_STOP = 0x4011CB00
FRAME_BUILDER = 0x4011C542
CONTROL_SMOOTHER = 0x4011C69E
BR_FRAME_ADDRESS = 0x8000F7BE
BR_FRAME_LONGWORD = 0x8000F7BC
BR_TARGET_LONGWORD = 0x8000E5C4

LANDMARKS = (
    0x4011C542, 0x4011C69E, 0x4011C722, 0x40117F00,
    0x4010A2E0, 0x40108944, 0x40105188, CALLBACK_STOP,
)


def load_emulator(path: Path):
    spec = importlib.util.spec_from_file_location("callback_minicoldfire", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load emulator module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def call_until(cpu, address: int, stop: int, limit: int) -> int:
    cpu.pushl(RETURN_PC)
    cpu.pc = address
    start = cpu.steps
    for _ in range(limit):
        if cpu.pc == stop:
            return cpu.steps - start
        cpu.step()
    raise ValueError(f"0x{address:08X} did not reach 0x{stop:08X}")


def prepared_machine(module, main_path: Path):
    bus = module.Bus()
    bus.load_main(main_path)
    cpu = module.CPU(bus)
    cpu.a[7] = module.INITIAL_SP - 0x1000
    machine_init_steps = call_until(cpu, MACHINE_TABLE_INITIALIZER, RETURN_PC, 2_000)
    if bus.read(0x42F7854C, 4) != 0x4192A2E4:
        raise ValueError("stock machine table did not initialize")

    # Earlier board startup installs the channel-32/channel-31 TCD pointers.
    # The stock audio initializer then fills their 16-byte minor-loop geometry.
    # These two pointer seeds and the ready word remain explicit preconditions.
    bus._mmio_raw_write(0xFC03C02C, 4, 0x10000000)
    bus.write(0x80005820, 4, 0xFC045400)
    bus.write(0x80005824, 4, 0xFC0453E0)
    audio_init_steps = call_until(cpu, AUDIO_DMA_INITIALIZER, RETURN_PC, 10_000)
    if bus.read(0x80005820, 4) != 0xFC045400:
        raise ValueError("stock audio initializer did not install channel-32 TCD")
    if bus.read(0x80005824, 4) != 0xFC0453E0:
        raise ValueError("stock audio initializer did not install channel-31 TCD")
    return bus, cpu, {
        "machine_table": machine_init_steps,
        "audio_dma": audio_init_steps,
    }


def run_callback(module, main_path: Path) -> dict:
    bus, cpu, init_steps = prepared_machine(module, main_path)
    accesses: list[dict] = []
    original_read = bus.read

    def traced_read(address: int, size: int) -> int:
        value = original_read(address, size)
        if address <= BR_FRAME_ADDRESS < address + size:
            accesses.append({
                "pc": f"0x{cpu.pc:08X}", "address": f"0x{address:08X}",
                "size": size, "value": f"0x{value:0{size * 2}X}",
            })
        return value

    bus.read = traced_read
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    start = cpu.steps
    reached = Counter()
    for _ in range(100_000):
        if cpu.pc in LANDMARKS:
            reached[cpu.pc] += 1
        if cpu.pc == CALLBACK_STOP:
            break
        cpu.step()
    else:
        raise ValueError("audio callback did not reach its RTOS handoff")
    bus.read = original_read

    if [entry["address"] for entry in accesses] != ["0x8000F7BC"]:
        raise ValueError(f"unexpected BR access set: {accesses}")
    if not all(reached[address] for address in LANDMARKS):
        raise ValueError(f"missing callback landmarks: {reached}")
    channels = Counter(event["ch"] for event in bus.edma_events)
    channel_30 = [event for event in bus.edma_events if event["ch"] == 30]
    if len(channel_30) != 1 or channel_30[0]["done_bytes"] != 272:
        raise ValueError(f"unexpected channel-30 transfer: {channel_30}")
    audio_transfer = channel_30[0]
    ingress = {}
    for channel in (31, 32):
        events = [event for event in bus.edma_events if event["ch"] == channel]
        if len(events) != 8 or any(event["done_bytes"] != 144 for event in events):
            raise ValueError(f"unexpected channel-{channel} transfers: {events}")
        if any(event["saddr"] != 0x4F9372E0 for event in events):
            raise ValueError(f"unexpected channel-{channel} source")
        ingress[str(channel)] = {
            "major_loops": len(events),
            "source": "0x4F9372E0",
            "minor_bytes": events[0]["nbytes"],
            "major_iterations": events[0]["citer"],
            "bytes_per_major_loop": events[0]["done_bytes"],
            "destination_starts": sorted({f"0x{event['daddr']:08X}" for event in events}),
        }
    return {
        "machine_table_initializer": f"0x{MACHINE_TABLE_INITIALIZER:08X}",
        "machine_table_initializer_instructions": init_steps["machine_table"],
        "audio_dma_initializer": f"0x{AUDIO_DMA_INITIALIZER:08X}",
        "audio_dma_initializer_instructions": init_steps["audio_dma"],
        "callback": f"0x{AUDIO_CALLBACK:08X}",
        "stop": f"0x{CALLBACK_STOP:08X}",
        "callback_instructions": cpu.steps - start,
        "landmarks": {f"0x{address:08X}": reached[address] for address in LANDMARKS},
        "edma_major_loops_by_channel": {str(ch): channels[ch] for ch in sorted(channels)},
        "channel_30_audio_transfer": {
            "source": f"0x{audio_transfer['saddr']:08X}",
            "destination": f"0x{audio_transfer['daddr']:08X}",
            "minor_bytes": audio_transfer["nbytes"],
            "major_iterations": audio_transfer["citer"],
            "transferred_bytes": audio_transfer["done_bytes"],
            "scatter_gather_tcd": f"0x{audio_transfer['scatter_gather_tcd']:08X}",
            "next_destination": "0x8000DEE0",
            "source_modulo_bytes": 1 << ((audio_transfer["attr"] >> 11) & 0x1F),
            "source_offset_per_minor_loop": audio_transfer["soff"],
        },
        "channels_31_32_external_ingress": ingress,
        "sample_br_accesses": accesses,
        "interpretation": (
            "MAIN touches track-0 sample BR only in the control smoother during "
            "this storage-free callback; no later direct CPU read is observed. "
            "Channel 30 is a 17-by-16-byte modulo-window snapshot, while channels "
            "31/32 ingest 9-by-16-byte external blocks."
        ),
    }


def run_builder_differential(module, main_path: Path) -> dict:
    bus, cpu, _ = prepared_machine(module, main_path)
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    for _ in range(100_000):
        if cpu.pc == FRAME_BUILDER:
            break
        cpu.step()
    else:
        raise ValueError("callback did not reach frame builder")

    before = bus.read(BR_FRAME_LONGWORD, 4)
    injected = 0x7F000000
    bus.write(BR_TARGET_LONGWORD, 4, injected)
    for _ in range(10_000):
        if cpu.pc == CONTROL_SMOOTHER:
            break
        cpu.step()
    else:
        raise ValueError("frame builder did not reach control smoother")
    after = bus.read(BR_FRAME_LONGWORD, 4)
    if before == after:
        raise ValueError("BR-containing frame longword did not respond to its target")
    return {
        "target_array_base": "0x8000E57C",
        "target_longword_index": 18,
        "target_longword_address": f"0x{BR_TARGET_LONGWORD:08X}",
        "frame_longword_index": 18,
        "frame_longword_address": f"0x{BR_FRAME_LONGWORD:08X}",
        "contained_frame_words": [36, 37],
        "sample_br_word": 37,
        "sample_br_address": f"0x{BR_FRAME_ADDRESS:08X}",
        "injected_target": f"0x{injected:08X}",
        "frame_before": f"0x{before:08X}",
        "frame_after": f"0x{after:08X}",
        "proven_mapping": "frame longword 18 is built from target longword 18",
    }


def probe(main_path: Path, emulator_path: Path) -> dict:
    digest = hashlib.sha256(main_path.read_bytes()).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    module = load_emulator(emulator_path)
    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "emulator": {"path": str(emulator_path)},
        "board_preconditions": {
            "audio_ready_word": {"address": "0xFC03C02C", "value": "0x10000000"},
            "stock_audio_dma_initializer": f"0x{AUDIO_DMA_INITIALIZER:08X}",
            "channel_32_tcd_pointer_seed": {"address": "0x80005820", "value": "0xFC045400"},
            "channel_31_tcd_pointer_seed": {"address": "0x80005824", "value": "0xFC0453E0"},
        },
        "callback_execution": run_callback(module, main_path),
        "control_frame_builder": run_builder_differential(module, main_path),
        "next_target": (
            "Reject section ID 2 as the audio engine, then activate one stock "
            "sample voice and trace the BR-dependent render state inside MAIN."
        ),
        "safety": "Emulation and RAM observation only; firmware bytes were not modified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
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
