#!/usr/bin/env python3
"""Reserve and execute a disabled Filter 2/LFO2 state canary in writable MAIN SDRAM."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import tempfile
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, RETURN_PC, prepared_machine
from filter2_bypass_canary_probe import build_candidate as build_bypass, execute_vector
from post_voice_ingress_probe import (
    ACTIVE_INPUT_WORD,
    DMA_BLOCK_BYTES,
    EXTERNAL_AUDIO_WINDOW,
    install_tables,
)
from trigger_queue_probe import load_emulator

MAIN_LOAD = 0x40000400
IDLE_PC = 0x400FCECC
MIXER = 0x4010A2E0
ZERO_RUN_FIRST = 0x402B41E0
ZERO_RUN_LAST_EXCLUSIVE = 0x402BA820
STATE_BASE = 0x402B4400
HEADER_BYTES = 32
FILTER2_COUNT = 8
FILTER2_STRIDE = 32
LFO2_COUNT = 13
LFO2_STRIDE = 16
STATE_BYTES = HEADER_BYTES + FILTER2_COUNT * FILTER2_STRIDE + LFO2_COUNT * LFO2_STRIDE
STATE_END = STATE_BASE + STATE_BYTES
MAGIC = b"F2L2"
VERSION = 1
FLAGS_DISABLED = 0


def image_offset(address: int) -> int:
    return address - MAIN_LOAD


def state_header() -> bytes:
    return struct.pack(
        ">4sHHIHHHHHH8s",
        MAGIC,
        VERSION,
        HEADER_BYTES,
        FLAGS_DISABLED,
        0,  # Filter 2 enabled-lane mask
        0,  # LFO2 enabled-track mask
        FILTER2_COUNT,
        FILTER2_STRIDE,
        LFO2_COUNT,
        LFO2_STRIDE,
        bytes(8),
    )


def scan_references(stock: bytes) -> dict:
    aligned_literals = []
    absolute_transfers = []
    relative_branches = []
    for position in range(0, len(stock) - 5, 2):
        address = MAIN_LOAD + position
        value = int.from_bytes(stock[position : position + 4], "big")
        if STATE_BASE <= value < STATE_END:
            aligned_literals.append(f"0x{address:08X}")
        opcode = int.from_bytes(stock[position : position + 2], "big")
        absolute_target = int.from_bytes(stock[position + 2 : position + 6], "big")
        if opcode in (0x4EB9, 0x4EF9) and STATE_BASE <= absolute_target < STATE_END:
            absolute_transfers.append(f"0x{address:08X}")
        if opcode & 0xF000 == 0x6000:
            low = opcode & 0xFF
            base = address + 2
            if low == 0:
                displacement = int.from_bytes(stock[position + 2 : position + 4], "big", signed=True)
            elif low == 0xFF:
                displacement = int.from_bytes(stock[position + 2 : position + 6], "big", signed=True)
            else:
                displacement = low - 0x100 if low & 0x80 else low
            target = (base + displacement) & 0xFFFFFFFF
            if STATE_BASE <= target < STATE_END:
                relative_branches.append(f"0x{address:08X}")
    return {
        "aligned_literals": aligned_literals,
        "absolute_jmp_jsr": absolute_transfers,
        "relative_branches": relative_branches,
    }


def build_candidate(stock: bytes) -> tuple[bytes, dict]:
    candidate, bypass = build_bypass(stock)
    region = stock[image_offset(STATE_BASE) : image_offset(STATE_END)]
    if region != bytes(STATE_BYTES):
        raise ValueError("state canary span is not zero-filled in stock MAIN")
    references = scan_references(stock)
    if any(references.values()):
        raise ValueError(f"stock MAIN references state canary span: {references}")

    header = state_header()
    if len(header) != HEADER_BYTES:
        raise ValueError(f"unexpected state header length: {len(header)}")
    patched = bytearray(candidate)
    patched[image_offset(STATE_BASE) : image_offset(STATE_BASE) + HEADER_BYTES] = header
    result = bytes(patched)
    return result, {
        "bypass": bypass,
        "state_base": f"0x{STATE_BASE:08X}",
        "state_end_exclusive": f"0x{STATE_END:08X}",
        "state_bytes_reserved": STATE_BYTES,
        "header_hex": header.hex(),
        "stock_references": references,
    }


def trace_span(bus, cpu, action) -> dict:
    hits = {"reads": 0, "writes": 0, "read_pcs": set(), "write_pcs": set()}
    original_read, original_write = bus.read, bus.write

    def traced_read(address: int, size: int) -> int:
        value = original_read(address, size)
        if address < STATE_END and address + size > STATE_BASE:
            hits["reads"] += 1
            hits["read_pcs"].add(cpu.pc)
        return value

    def traced_write(address: int, size: int, value: int) -> None:
        if address < STATE_END and address + size > STATE_BASE:
            hits["writes"] += 1
            hits["write_pcs"].add(cpu.pc)
        original_write(address, size, value)

    bus.read, bus.write = traced_read, traced_write
    try:
        action()
    finally:
        bus.read, bus.write = original_read, original_write
    return {
        "reads": hits["reads"],
        "writes": hits["writes"],
        "read_pcs": [f"0x{pc:08X}" for pc in sorted(hits["read_pcs"])],
        "write_pcs": [f"0x{pc:08X}" for pc in sorted(hits["write_pcs"])],
    }


def boot_liveness(module, stock_path: Path) -> dict:
    bus = module.Bus()
    bus.load_main(stock_path)
    cpu = module.CPU(bus)
    cpu.a[7] = module.INITIAL_SP
    bus.write(module.INITIAL_SP + 4, 4, 0)

    def execute() -> None:
        cpu.run(12_000_000)

    access = trace_span(bus, cpu, execute)
    if cpu.pc != IDLE_PC or access["reads"] or access["writes"]:
        raise ValueError(f"state span live during stock boot: pc=0x{cpu.pc:08X}, {access}")
    return {"instructions": cpu.steps, "idle_pc": f"0x{cpu.pc:08X}", **access}


def callback_liveness(module, candidate_path: Path, stock: bytes, active: bool) -> dict:
    bus, cpu, _ = prepared_machine(module, candidate_path)
    install_tables(bus, stock)
    payload = ACTIVE_INPUT_WORD.to_bytes(4, "big") * 36 if active else bytes(DMA_BLOCK_BYTES)
    for index, value in enumerate(payload):
        bus.write(EXTERNAL_AUDIO_WINDOW + index, 1, value)
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    start = cpu.steps

    def execute() -> None:
        for _ in range(100_000):
            if cpu.pc == MIXER:
                return
            cpu.step()
        raise ValueError("candidate callback did not reach mixer")

    access = trace_span(bus, cpu, execute)
    if access["reads"] or access["writes"]:
        raise ValueError(f"disabled state span accessed during callback: {access}")
    header = bytes(bus.read(STATE_BASE + index, 1) for index in range(HEADER_BYTES))
    if header != state_header():
        raise ValueError("disabled state header did not survive callback")
    return {
        "input": "active" if active else "zero",
        "instructions_to_mixer": cpu.steps - start,
        "header_preserved": True,
        **access,
    }


def probe(stock_path: Path, emulator_path: Path, candidate_output: Path | None = None) -> dict:
    stock = stock_path.read_bytes()
    stock_hash = hashlib.sha256(stock).hexdigest()
    if stock_hash != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {stock_hash}")
    zero_run = stock[image_offset(ZERO_RUN_FIRST) : image_offset(ZERO_RUN_LAST_EXCLUSIVE)]
    if zero_run != bytes(len(zero_run)):
        raise ValueError("declared containing zero run changed")

    candidate, build = build_candidate(stock)
    candidate_hash = hashlib.sha256(candidate).hexdigest()
    module = load_emulator(emulator_path)
    stock_boot = boot_liveness(module, stock_path)

    temporary = None
    if candidate_output is None:
        temporary = tempfile.NamedTemporaryFile(suffix=".bin")
        candidate_path = Path(temporary.name)
    else:
        candidate_path = candidate_output
    candidate_path.write_bytes(candidate)

    comparisons = []
    callbacks = []
    try:
        for active in (False, True):
            stock_run = execute_vector(module, stock_path, stock, active, False)
            candidate_run = execute_vector(module, candidate_path, stock, active, True)
            fields = (
                "mixer_entry_registers", "mixer_input_sha256", "ingress_output_sha256",
                "mixer_instructions", "mixer_output_writes", "mixer_output_sha256",
            )
            equality = {field: stock_run[field] == candidate_run[field] for field in fields}
            if not all(equality.values()):
                raise ValueError(f"state canary candidate diverged from stock: {equality}")
            comparisons.append({"input": "active" if active else "zero", "bit_identical": equality})
            callbacks.append(callback_liveness(module, candidate_path, stock, active))
    finally:
        if temporary is not None:
            temporary.close()

    return {
        "result": "PASS",
        "stock": {"path": str(stock_path), "sha256": stock_hash},
        "candidate": {
            "path": str(candidate_output) if candidate_output else "temporary execution image",
            "sha256": candidate_hash,
            **build,
        },
        "containing_zero_run": {
            "first": f"0x{ZERO_RUN_FIRST:08X}",
            "last_exclusive": f"0x{ZERO_RUN_LAST_EXCLUSIVE:08X}",
            "bytes": ZERO_RUN_LAST_EXCLUSIVE - ZERO_RUN_FIRST,
        },
        "layout": {
            "magic": MAGIC.decode("ascii"),
            "version": VERSION,
            "header_bytes": HEADER_BYTES,
            "flags": FLAGS_DISABLED,
            "filter2_enabled_lane_mask": 0,
            "lfo2_enabled_track_mask": 0,
            "filter2": {"count": FILTER2_COUNT, "stride": FILTER2_STRIDE},
            "lfo2": {"count": LFO2_COUNT, "stride": LFO2_STRIDE},
        },
        "stock_boot_liveness": stock_boot,
        "candidate_callback_liveness": callbacks,
        "stock_equivalence": comparisons,
        "conclusion": (
            "A 496-byte versioned Filter 2/LFO2 state area fits inside a 26,176-byte "
            "zero run in the writable SDRAM-loaded MAIN image. Stock has no detected "
            "literal, absolute-transfer, or relative-branch reference into the reserved "
            "span and makes no access during modeled boot. With all enable fields zero, "
            "the candidate callback never accesses the payload and remains bit-identical "
            "to stock through the next combiner for zero and active vectors."
        ),
        "scope_limit": (
            "Dynamic liveness covers modeled stock boot and representative audio callbacks, "
            "not every UI, storage, or fault path. The header is a research ABI checkpoint."
        ),
        "next_target": (
            "Extend the cave stub with a flags==0 fast path that returns immediately, then "
            "execute a disabled single-lane Filter 2 dispatcher before adding DSP arithmetic."
        ),
        "safety": (
            "Decompressed MAIN lab image only. No ELE3 container or SysEx was built; "
            "the candidate is not flashable."
        ),
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
    if args.report:
        args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
