#!/usr/bin/env python3
"""Execute the stock AR 1.72 BR quantizer and verify its fixed-point equation.

This is a read-only provenance test.  It starts at the terminal BR parameter
read, executes the unmodified MAIN instructions through all 16 loop passes,
and compares the 32 quantizer MAC results with an independent integer model.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

MAIN_BASE = 0x40000400
EXPECTED_MAIN_SHA256 = "5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772"
BR_READ = 0x4011870E
LOOP_START = 0x4011877A
QUANTIZE_0 = 0x4011878E
QUANTIZE_1 = 0x40118792
LOOP_EXIT = 0x401187A6
TABLE_BASE = 0x40217950
SOURCE = 0x8000E1D0
PARAM_OBJECT = 0x43001000
OUTPUT = 0x43002000
BR_LEVELS = (0x0000, 0x0001, 0x0100, 0x1000, 0x2000, 0x4000, 0x6000, 0x7800)


def signed32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def read_u32(image: bytes, address: int) -> int:
    offset = address - MAIN_BASE
    return int.from_bytes(image[offset : offset + 4], "big")


def at(image: bytes, address: int, expected: bytes, label: str) -> None:
    offset = address - MAIN_BASE
    actual = image[offset : offset + len(expected)]
    if actual != expected:
        raise ValueError(
            f"{label}: signature mismatch at 0x{address:08X}: "
            f"expected {expected.hex()}, got {actual.hex()}"
        )


def reconstruct_coefficients(image: bytes, raw_br: int) -> tuple[int, int, int]:
    """Reconstruct D2, D3 and D4 without executing the firmware."""
    if raw_br == 0:
        return 1, 0x40000000, 0x40000000
    scaled = (0x254A952A * ((raw_br & 0xFFFF) << 16)) >> 31
    exponent_word = (scaled + 0x53000000) & 0xFFFFFFFF
    signed_exponent = signed32(exponent_word)
    d2_pre = signed_exponent >> 26
    fraction_index = (signed_exponent >> 13) & 0x1FFC
    d4 = (read_u32(image, TABLE_BASE + fraction_index - 0x2000) << 2) & 0xFFFFFFFF
    d3_pre = (read_u32(image, TABLE_BASE - fraction_index) << 1) & 0xFFFFFFFF
    d3 = d3_pre >> d2_pre
    return d2_pre + 1, d3, d4


def quantizer(sample: int, d3: int, d2: int) -> tuple[int, int]:
    mantissa = (signed32(sample) * signed32(d3)) >> 31
    return mantissa & 0xFFFFFFFF, (mantissa << d2) & 0xFFFFFFFF


def load_emulator(path: Path):
    spec = importlib.util.spec_from_file_location("br_quantizer_minicoldfire", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load emulator module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def source_vector() -> list[int]:
    values = [
        0x00000000,
        0x00000001,
        0x7FFFFFFF,
        0x80000000,
        0xFFFFFFFF,
        0x40000000,
        0xC0000000,
        0x12345678,
        0xEDCBA988,
    ]
    state = 0x17252008
    while len(values) < 34:  # two priming reads plus 32 loop reads
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        values.append(state)
    return values


def execute_level(module, main_path: Path, image: bytes, raw_br: int) -> dict:
    bus = module.Bus()
    bus.load_main(main_path)
    cpu = module.CPU(bus)
    cpu.pc = BR_READ
    cpu.macsr = 0x20  # stock initialization at 0x40117EF0
    cpu.a[1] = PARAM_OBJECT
    cpu.a[6] = OUTPUT
    cpu.a[7] = module.INITIAL_SP - 0x1000
    bus.write(PARAM_OBJECT + 6, 2, raw_br)
    for index, value in enumerate(source_vector()):
        bus.write(SOURCE + 4 * index, 4, value)

    expected_d2, expected_d3, expected_d4 = reconstruct_coefficients(image, raw_br)
    runtime_coefficients = None
    samples: list[dict] = []
    for _ in range(400):
        if cpu.pc == LOOP_START and runtime_coefficients is None:
            runtime_coefficients = (cpu.d[2], cpu.d[3], cpu.d[4])
        if cpu.pc in (QUANTIZE_0, QUANTIZE_1):
            channel = 0 if cpu.pc == QUANTIZE_0 else 1
            accumulator = channel
            operand = cpu.a[0] if channel == 0 else cpu.d[6]
            expected_mantissa, expected_output = quantizer(
                operand, expected_d3, expected_d2
            )
            instruction_pc = cpu.pc
            cpu.step()
            actual_mantissa = cpu._mac_from(accumulator)
            actual_output = (signed32(actual_mantissa) << expected_d2) & 0xFFFFFFFF
            samples.append(
                {
                    "index": len(samples),
                    "pc": f"0x{instruction_pc:08X}",
                    "operand": f"0x{operand:08X}",
                    "mantissa": f"0x{actual_mantissa:08X}",
                    "shifted": f"0x{actual_output:08X}",
                    "match": actual_mantissa == expected_mantissa
                    and actual_output == expected_output,
                }
            )
            continue
        if cpu.pc == LOOP_EXIT:
            break
        cpu.step()
    else:
        raise ValueError(f"BR 0x{raw_br:04X}: loop did not reach 0x{LOOP_EXIT:08X}")

    if runtime_coefficients is None:
        raise ValueError(f"BR 0x{raw_br:04X}: setup did not reach the loop")
    expected_coefficients = (expected_d2, expected_d3, expected_d4)
    if runtime_coefficients != expected_coefficients:
        raise ValueError(
            f"BR 0x{raw_br:04X}: coefficient mismatch: "
            f"runtime={runtime_coefficients!r}, expected={expected_coefficients!r}"
        )
    if len(samples) != 32 or not all(sample["match"] for sample in samples):
        raise ValueError(f"BR 0x{raw_br:04X}: quantizer comparisons failed")

    vector_bytes = b"".join(
        int(sample["shifted"], 16).to_bytes(4, "big") for sample in samples
    )
    return {
        "raw_br": f"0x{raw_br:04X}",
        "instructions": cpu.steps,
        "loop_iterations": 16,
        "quantizer_samples": len(samples),
        "coefficient_match": True,
        "sample_matches": len(samples),
        "d2": expected_d2,
        "d3": f"0x{expected_d3:08X}",
        "d4": f"0x{expected_d4:08X}",
        "shifted_vector_sha256": hashlib.sha256(vector_bytes).hexdigest(),
        "first_samples": samples[:4],
        "last_samples": samples[-2:],
    }


def trace(main_path: Path, emulator_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    at(image, BR_READ, bytes.fromhex("71e900064840"), "terminal BR read")
    at(image, QUANTIZE_0, bytes.fromhex("a0430800"), "first sample quantizer")
    at(image, QUANTIZE_1, bytes.fromhex("ac830800"), "second sample quantizer")
    module = load_emulator(emulator_path)
    levels = [execute_level(module, main_path, image, raw_br) for raw_br in BR_LEVELS]
    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "execution": {
            "entry": f"0x{BR_READ:08X}",
            "exit": f"0x{LOOP_EXIT:08X}",
            "br_levels": len(levels),
            "loop_iterations": sum(level["loop_iterations"] for level in levels),
            "quantizer_samples": sum(level["quantizer_samples"] for level in levels),
            "sample_matches": sum(level["sample_matches"] for level in levels),
        },
        "equation": "Q(x) = (((signed32(x) * signed32(D3)) >> 31) << D2) mod 2^32",
        "mac_mode": "MACSR 0x20: signed fractional, truncate, 32-bit accumulator store",
        "levels": levels,
        "conclusion": (
            "The unmodified terminal BR read, coefficient setup, and 16-pass stock "
            "loop agree with the independent coefficient reconstruction and Q31 equation."
        ),
        "boundary": (
            "Instruction execution is proven; front-panel value mapping and physical "
            "hardware/DAC behavior remain unverified."
        ),
        "safety": "Stock MAIN was executed read-only; firmware bytes were not modified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("main_image", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = trace(args.main_image, args.emulator)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
