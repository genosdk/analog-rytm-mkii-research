#!/usr/bin/env python3
"""Execute and map MAIN's stock 32-frame by 8-lane audio combiner."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, CALLBACK_STOP, RETURN_PC, prepared_machine

EXPECTED_MAIN_SHA256 = "5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772"
MIXER = 0x4010A2E0
MIXER_RETURN = 0x4011CAE8


def load_emulator(path: Path):
    spec = importlib.util.spec_from_file_location("mixer_minicoldfire", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load emulator module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def probe(main_path: Path, emulator_path: Path) -> dict:
    digest = hashlib.sha256(main_path.read_bytes()).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    module = load_emulator(emulator_path)
    bus, cpu, _ = prepared_machine(module, main_path)
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    for _ in range(100_000):
        if cpu.pc == MIXER:
            break
        cpu.step()
    else:
        raise ValueError("callback did not reach mixer")

    reads: dict[int, list[int]] = {}
    writes: dict[int, list[int]] = {}
    original_read, original_write = bus.read, bus.write

    def traced_read(address: int, size: int) -> int:
        value = original_read(address, size)
        if 0x80000000 <= address < 0x8C000000:
            reads.setdefault(cpu.pc, []).append(address)
        return value

    def traced_write(address: int, size: int, value: int) -> None:
        if 0x80000000 <= address < 0x8C000000:
            writes.setdefault(cpu.pc, []).append(address)
        original_write(address, size, value)

    bus.read, bus.write = traced_read, traced_write
    start = cpu.steps
    for _ in range(10_000):
        if cpu.pc == MIXER_RETURN:
            break
        cpu.step()
    else:
        raise ValueError("mixer did not return to callback")
    bus.read, bus.write = original_read, original_write

    expected_output = {
        0x80000800 + frame * 0x40 + lane * 4
        for frame in range(32)
        for lane in range(8)
    }
    actual_output = set(writes.get(0x4010A3B8, []))
    if actual_output != expected_output:
        raise ValueError("unexpected mixer output geometry")
    sources = {
        "source_a": (0x4010A39C, 0x80006BF8, 0x80006FF4),
        "source_b": (0x4010A3A0, 0x80007040, 0x8000743C),
        "source_c": (0x4010A3A8, 0x800067FC, 0x80006BF8),
    }
    for label, (pc, first, last) in sources.items():
        addresses = reads.get(pc, [])
        if len(addresses) != 256 or min(addresses) != first or max(addresses) != last:
            raise ValueError(f"unexpected {label} geometry")

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "combiner": {
            "address": f"0x{MIXER:08X}",
            "instructions_in_inactive_callback": cpu.steps - start,
            "frames": 32,
            "lanes_written_per_frame": 8,
            "frame_stride_bytes": 64,
            "lane_width_bytes": 4,
            "output_first": "0x80000800",
            "output_last": "0x80000FDC",
            "source_planes": {
                label: {"first": f"0x{first:08X}", "last": f"0x{last:08X}", "reads": 256}
                for label, (_, first, last) in sources.items()
            },
        },
        "interpretation": (
            "0x4010A2E0 combines three 256-longword source planes into eight "
            "32-frame output lanes. The 0x800067F8 plane is populated by the "
            "preceding 0x40117F00 ingress path; the other two planes are not "
            "written during this inactive callback and lead upstream toward "
            "the sample/render producers."
        ),
        "next_target": (
            "Resolve the producer family that writes 0x80006BF8 and 0x80007040, "
            "then activate one stock sample voice there and repeat the BR differential."
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
