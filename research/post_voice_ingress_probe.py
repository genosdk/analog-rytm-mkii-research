#!/usr/bin/env python3
"""Classify MAIN's table-driven external-audio ingress at 0x40117F00.

The legacy filename is retained because this probe supersedes an earlier,
incorrect interpretation of three initialized DSP tables as signal planes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, RETURN_PC, prepared_machine
from trigger_queue_probe import load_emulator

MAIN_LOAD = 0x40000400
IDLE_PC = 0x400FCECC
DATA_COPY_PC = 0x4000081C
INGRESS = 0x40117F00
INGRESS_RETURN = 0x4011CAD2
TABLE_DESTINATIONS = (0x8000BC00, 0x8000C000, 0x8000C400)
TABLE_SOURCES = (0x402C0C00, 0x402C1000, 0x402C1400)
TABLE_BYTES = 0x400
EXTERNAL_AUDIO_WINDOW = 0x4F9372E0
DMA_BLOCK_BYTES = 144
OUTPUT_PLANE = 0x800067F8
OUTPUT_WORDS = 256
ACTIVE_INPUT_WORD = 0x10000000
ACTIVE_SAMPLE_LEVEL = 0x4000
RUNTIME_RECORD_BASE = 0x8000F776
RUNTIME_RECORD_BYTES = 0x54
SAMPLE_LEVEL_OFFSET = 0x0E
PHYSICAL_VOICE_RECORDS = (1, 5, 2, 6, 9, 7, 11, 3)


def image_block(image: bytes, address: int, length: int) -> bytes:
    offset = address - MAIN_LOAD
    return image[offset : offset + length]


def read_block(bus, address: int, length: int) -> bytes:
    return bytes(bus.read(address + index, 1) for index in range(length))


def install_tables(bus, image: bytes) -> None:
    for destination, source in zip(TABLE_DESTINATIONS, TABLE_SOURCES):
        for index, value in enumerate(image_block(image, source, TABLE_BYTES)):
            bus.write(destination + index, 1, value)


def install_sample_levels(bus, level: int = ACTIVE_SAMPLE_LEVEL) -> None:
    """Seed the eight renderer records after the callback frame builder."""
    for record in PHYSICAL_VOICE_RECORDS:
        bus.write(
            RUNTIME_RECORD_BASE + record * RUNTIME_RECORD_BYTES + SAMPLE_LEVEL_OFFSET,
            2,
            level,
        )


def trace_stock_startup(module, main_path: Path, image: bytes) -> dict:
    bus = module.Bus()
    bus.load_main(main_path)
    cpu = module.CPU(bus)
    cpu.a[7] = module.INITIAL_SP
    bus.write(module.INITIAL_SP + 4, 4, 0)
    writes = Counter()
    original_write = bus.write

    def traced_write(address: int, size: int, value: int) -> None:
        for destination in TABLE_DESTINATIONS:
            if destination <= address < destination + TABLE_BYTES:
                writes[(cpu.pc, destination, size)] += 1
        original_write(address, size, value)

    bus.write = traced_write
    cpu.run(12_000_000)
    bus.write = original_write
    if cpu.pc != IDLE_PC:
        raise ValueError(f"stock startup did not reach idle: 0x{cpu.pc:08X}")

    entries = []
    for destination, source in zip(TABLE_DESTINATIONS, TABLE_SOURCES):
        ram = read_block(bus, destination, TABLE_BYTES)
        rom = image_block(image, source, TABLE_BYTES)
        count = writes[(DATA_COPY_PC, destination, 4)]
        if ram != rom or count != 256:
            raise ValueError(f"unexpected initialized table copy at 0x{destination:08X}")
        entries.append({
            "rom_source": f"0x{source:08X}",
            "ram_destination": f"0x{destination:08X}",
            "bytes": TABLE_BYTES,
            "copy_pc": f"0x{DATA_COPY_PC:08X}",
            "longword_writes": count,
            "sha256": hashlib.sha256(ram).hexdigest(),
            "first_16_bytes": ram[:16].hex(),
        })
    if sum(writes.values()) != 768:
        raise ValueError(f"unexpected writers into initialized tables: {writes}")
    return {"boot_steps": cpu.steps, "idle_pc": f"0x{cpu.pc:08X}", "tables": entries}


def run_vector(module, main_path: Path, image: bytes, active: bool) -> dict:
    bus, cpu, _ = prepared_machine(module, main_path)
    install_tables(bus, image)
    payload = (ACTIVE_INPUT_WORD.to_bytes(4, "big") * 36) if active else bytes(DMA_BLOCK_BYTES)
    for index, value in enumerate(payload):
        bus.write(EXTERNAL_AUDIO_WINDOW + index, 1, value)

    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    for _ in range(100_000):
        if cpu.pc == INGRESS:
            break
        cpu.step()
    else:
        raise ValueError("callback did not reach external-audio ingress")
    install_sample_levels(bus)

    reads = Counter()
    writes = Counter()
    original_read, original_write = bus.read, bus.write

    def traced_read(address: int, size: int) -> int:
        value = original_read(address, size)
        for destination in TABLE_DESTINATIONS:
            if destination <= address < destination + TABLE_BYTES:
                reads[destination] += 1
        return value

    def traced_write(address: int, size: int, value: int) -> None:
        for destination in TABLE_DESTINATIONS:
            if destination <= address < destination + TABLE_BYTES:
                writes[destination] += 1
        original_write(address, size, value)

    bus.read, bus.write = traced_read, traced_write
    start = cpu.steps
    for _ in range(30_000):
        if cpu.pc == INGRESS_RETURN:
            break
        cpu.step()
    else:
        raise ValueError("external-audio ingress did not return")
    bus.read, bus.write = original_read, original_write

    values = [bus.read(OUTPUT_PLANE + 4 * index, 4) for index in range(OUTPUT_WORDS)]
    raw = b"".join(value.to_bytes(4, "big") for value in values)
    zero_indices = [index for index, value in enumerate(values) if value == 0]
    audio_events = [event for event in bus.edma_events if event["ch"] in (31, 32)]
    return {
        "input": "repeated 0x10000000 words" if active else "zero",
        "sample_level": f"0x{ACTIVE_SAMPLE_LEVEL:04X}",
        "instructions": cpu.steps - start,
        "table_reads": [reads[address] for address in TABLE_DESTINATIONS],
        "table_writes": [writes[address] for address in TABLE_DESTINATIONS],
        "dma_major_loops": {
            str(channel): sum(event["ch"] == channel for event in audio_events)
            for channel in (31, 32)
        },
        "dma_bytes_per_loop": sorted({event["done_bytes"] for event in audio_events}),
        "dma_source_addresses": sorted({f"0x{event['saddr']:08X}" for event in audio_events}),
        "output_nonzero_words": sum(value != 0 for value in values),
        "output_zero_indices": "all" if len(zero_indices) == OUTPUT_WORDS else zero_indices,
        "output_sha256": hashlib.sha256(raw).hexdigest(),
    }


def probe(main_path: Path, emulator_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    module = load_emulator(emulator_path)
    startup = trace_stock_startup(module, main_path, image)
    zero = run_vector(module, main_path, image, False)
    active = run_vector(module, main_path, image, True)

    expected_reads = [520, 512, 512]
    for vector in (zero, active):
        if vector["table_reads"] != expected_reads or vector["table_writes"] != [0, 0, 0]:
            raise ValueError(f"unexpected table access contract: {vector}")
        if vector["dma_major_loops"] != {"31": 8, "32": 8}:
            raise ValueError(f"unexpected ingress DMA cadence: {vector}")
        if vector["dma_bytes_per_loop"] != [144]:
            raise ValueError(f"unexpected ingress DMA block: {vector}")
    if zero["output_nonzero_words"] != 0:
        raise ValueError(f"zero external input changed output: {zero}")
    if (
        active["output_nonzero_words"] != 248
        or active["output_zero_indices"] != list(range(0, 256, 32))
        or active["output_sha256"] != "c48151676a4c06fc65db773dc947d38718e0d357a6f32a74bbaa1dc6205fc6e9"
    ):
        raise ValueError(f"unexpected active external-input response: {active}")

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "corrected_classification": {
            "entry": f"0x{INGRESS:08X}",
            "return": f"0x{INGRESS_RETURN:08X}",
            "external_source": f"0x{EXTERNAL_AUDIO_WINDOW:08X}",
            "ingress_dma_channels": [31, 32],
            "output_plane": f"0x{OUTPUT_PLANE:08X}",
            "output_geometry": "8 lanes x 32 longwords",
            "initialized_tables_not_signal_planes": [
                f"0x{address:08X}" for address in TABLE_DESTINATIONS
            ],
        },
        "stock_startup_provenance": startup,
        "explicit_level_precondition": {
            "load_site": "0x401186CC",
            "field": "signed word 0x0E(A1)",
            "runtime_record_base": f"0x{RUNTIME_RECORD_BASE:08X}",
            "runtime_record_bytes": RUNTIME_RECORD_BYTES,
            "physical_voice_records": list(PHYSICAL_VOICE_RECORDS),
            "seeded_level": f"0x{ACTIVE_SAMPLE_LEVEL:04X}",
            "timing": "after callback frame builder and before external ingress",
        },
        "vectors": [zero, active],
        "conclusion": (
            "The three 0x400-byte regions previously called source planes are immutable "
            "DSP tables copied byte-exactly from MAIN during startup. The actual signal "
            "dependency enters from external window 0x4F9372E0 through alternating eDMA "
            "channels 31/32. With the stock tables installed and an explicit nonzero "
            "sample-level word seeded after the callback frame builder, zero external input yields "
            "zero output, while a repeated nonzero input produces 248 nonzero words in "
            "an 8-by-32 layout at 0x800067F8. This corrects the earlier interpretation "
            "and establishes 0x800067F8 as a strong post-conversion Filter 2 candidate."
        ),
        "scope_limit": (
            "The repeated-window emulator stimulus proves routing and geometry, not the "
            "physical sample rate, exact fixed-point format, or which off-chip converter "
            "drives the hardware window."
        ),
        "next_target": (
            "Determine the numeric format and saturation at 0x800067F8, then insert an "
            "exact disabled bypass after 0x40117F00 and prove bit-identical combiner input."
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
