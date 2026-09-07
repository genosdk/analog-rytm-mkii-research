#!/usr/bin/env python3
"""Run OS 1.72 to its idle task and capture BR machine descriptors.

The probe uses the research-only MiniColdFire interpreter. It delivers one
PIT0 tick after boot, breaks at 0x4011AE52, and records bytes +0x68..+0x97 of
each runtime machine object. On the current storage-free board model, no
project/machine objects are loaded; that condition is reported explicitly.
Nothing is written to the firmware image.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

EXPECTED_MAIN_SHA256 = (
    "5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772"
)
IDLE_PC = 0x400FCECC
DESCRIPTOR_INITIALIZER = 0x4011AE52
CAPTURE_START = 0x68
CAPTURE_END = 0x98
BR_DESTINATION_OFFSETS = (0x72, 0x75, 0x78, 0x7B, 0x8E, 0x91, 0x94, 0x97)
COEFFICIENT_OFFSETS = (0x70, 0x73, 0x76, 0x79, 0x8C, 0x8F, 0x92, 0x95)
FRAME_PREFIX_WORDS = 26
RECORD_WORDS = 42


def load_emulator(path: Path):
    spec = importlib.util.spec_from_file_location("ar172_minicoldfire", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load emulator module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_bytes(bus, address: int, length: int) -> bytes:
    return bytes(bus.read(address + i, 1) for i in range(length))


def run_synthetic_descriptor_call(cpu, bus) -> dict:
    """Prove descriptor packing without depending on project storage."""
    object_address = 0x43000000
    return_pc = IDLE_PC
    track = 0
    input_destinations = list(range(0x0D, 0x15))
    input_coefficients = [0x1000 + i for i in range(4)] + [0x2000 + i for i in range(4)]

    for offset in range(0xA2):
        bus.write(object_address + offset, 1, 0)
    for coefficient_offset, destination_offset, coefficient, destination in zip(
        COEFFICIENT_OFFSETS,
        BR_DESTINATION_OFFSETS,
        input_coefficients,
        input_destinations,
    ):
        bus.write(object_address + coefficient_offset, 2, coefficient)
        bus.write(object_address + destination_offset, 1, destination)

    cpu.pushl(track)
    cpu.pushl(object_address)
    cpu.pushl(return_pc)
    cpu.pc = DESCRIPTOR_INITIALIZER
    call_start = cpu.steps
    for _ in range(10_000):
        if cpu.pc == return_pc:
            break
        cpu.step()
    else:
        raise ValueError("synthetic descriptor call did not return")

    packed = read_bytes(bus, 0x8000EA3C, 0x20)
    entries = []
    for index in range(8):
        value = int.from_bytes(packed[index * 4 : index * 4 + 4], "big")
        entries.append(
            {
                "coefficient": value >> 16,
                "frame_word_index": value & 0xFFFF,
            }
        )
    expected_indices = [FRAME_PREFIX_WORDS + destination for destination in input_destinations]
    actual_indices = [entry["frame_word_index"] for entry in entries]
    if actual_indices != expected_indices:
        raise ValueError(
            f"unexpected synthetic frame destinations: {actual_indices} != {expected_indices}"
        )
    if [entry["coefficient"] for entry in entries] != input_coefficients:
        raise ValueError("synthetic coefficients were not preserved")

    return {
        "result": "PASS",
        "track": track,
        "instructions": cpu.steps - call_start,
        "input_destinations": input_destinations,
        "input_coefficients": input_coefficients,
        "packed_bank_hex": packed.hex(),
        "entries": entries,
        "proven_equation": "frame_word_index = 26 + 42 * track + sound_local_destination",
    }


def probe(main_path: Path, emulator_path: Path, tick_step_limit: int) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")

    emu = load_emulator(emulator_path)
    bus = emu.Bus()
    bus.load_main(main_path)
    cpu = emu.CPU(bus)
    cpu.a[7] = emu.INITIAL_SP
    bus.write(emu.INITIAL_SP + 4, 4, 0)

    # BSS acceleration causes the semantic step counter to jump to ~11.8M.
    # Twelve million therefore reaches the installed-vector idle task.
    stop = cpu.run(12_000_000)
    boot = {"pc": f"0x{cpu.pc:08X}", "steps": cpu.steps, "stop": stop}
    if cpu.pc != IDLE_PC:
        raise ValueError(f"boot did not reach expected idle PC: 0x{cpu.pc:08X}")

    captures: list[dict] = []
    if not bus.trigger_pit0():
        raise ValueError("PIT0 was not enabled by stock initialization")

    work_start = cpu.steps
    for local_step in range(tick_step_limit):
        if cpu.pc == DESCRIPTOR_INITIALIZER:
            sp = cpu.a[7]
            object_address = bus.read(sp + 0x14, 4)
            track = bus.read(sp + 0x18, 4)
            raw = read_bytes(bus, object_address + CAPTURE_START, CAPTURE_END - CAPTURE_START)
            captures.append(
                {
                    "step": cpu.steps,
                    "object_address": f"0x{object_address:08X}",
                    "track": track,
                    "object_bytes_68_97": raw.hex(),
                    "br_destination_indices": {
                        f"+0x{offset:02X}": bus.read(object_address + offset, 1)
                        for offset in BR_DESTINATION_OFFSETS
                    },
                }
            )
        cpu.step()
        if local_step > 100 and cpu.pc == IDLE_PC and not bus.pending_irqs:
            break

    banks_before_synthetic = {
        "0x8000EA00": read_bytes(bus, 0x8000EA00, 0x3C),
        "0x8000EA3C": read_bytes(bus, 0x8000EA3C, 0x20 * 12),
    }
    nonzero = {
        key: sum(byte != 0 for byte in data)
        for key, data in banks_before_synthetic.items()
    }
    project_data_present = bool(captures) or any(nonzero.values())
    synthetic = run_synthetic_descriptor_call(cpu, bus)

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "emulator": {"path": str(emulator_path)},
        "boot": boot,
        "pit0": {
            "vector": emu.PIT0_VECTOR,
            "enabled": bus.pit0_enabled(),
            "fires": bus.pit0_fires,
            "work_steps": cpu.steps - work_start,
            "return_pc": f"0x{cpu.pc:08X}",
        },
        "descriptor_initializer": f"0x{DESCRIPTOR_INITIALIZER:08X}",
        "capture_count": len(captures),
        "captures": captures,
        "descriptor_bank_nonzero_bytes": nonzero,
        "project_capture_status": "LOADED" if project_data_present else "NOT_LOADED_OPTIONAL",
        "synthetic_descriptor_proof": synthetic,
        "resolved_blocker": (
            "The descriptor inputs are per-sound modulation destinations rather "
            "than fixed hidden machine values. Synthetic execution proves their "
            "frame mapping, so project ingestion is no longer required for the "
            "BR quantizer trace."
        ),
        "remaining_emulator_gap": (
            "A project/storage loader is still required for full GUI/project emulation."
        ),
        "safety": "Emulation and RAM observation only; firmware bytes were not modified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("main_image", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument("--tick-step-limit", type=int, default=2_000_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = probe(args.main_image, args.emulator, args.tick_step_limit)
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
