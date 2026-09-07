#!/usr/bin/env python3
"""Trace stock BR output through renderer staging and the outbound audio DMA.

The trace executes only unmodified OS 1.72 MAIN instructions.  Tagged sample
words and one synthetic SRAM output pointer make data geometry observable; the
report keeps those fixtures distinct from hardware-proven addresses.
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
RETURN_PC = 0x400FCECC
PRE_RENDER = 0x40117F00
RENDERER = 0x4010A2E0
HANDOFF = 0x40109F04
TCD30_SETUP = 0x40109C68

VOICE_BASE = 0x800067F8
VOICE_COUNT = 8
SAMPLES_PER_BLOCK = 32
VOICE_BYTES = SAMPLES_PER_BLOCK * 4
VOICE_END = VOICE_BASE + VOICE_COUNT * VOICE_BYTES
FRAME_BASE = 0x80000000
FRAME_STRIDE = 0x40
FRAME_END = FRAME_BASE + SAMPLES_PER_BLOCK * FRAME_STRIDE
FIXED_STAGE = 0x80007A44
FIXED_STAGE_END = FIXED_STAGE + 0x200
OUTPUT_POINTER_GLOBAL = 0x8000DDB0
SYNTHETIC_OUTPUT = 0x80003200
OUTPUT_BLOCK_END = SYNTHETIC_OUTPUT + 0x100

TCD30_BASE = 0xFC0453C0
TCD42_BASE = 0xFC045540
TCD_FIELDS = (
    ("saddr", 0x00, 4),
    ("attr", 0x04, 2),
    ("soff", 0x06, 2),
    ("nbytes", 0x08, 4),
    ("slast", 0x0C, 4),
    ("daddr", 0x10, 4),
    ("citer", 0x14, 2),
    ("doff", 0x16, 2),
    ("dlast_sga", 0x18, 4),
    ("biter", 0x1C, 2),
    ("csr", 0x1E, 2),
)


def at(image: bytes, address: int, expected: bytes, label: str) -> None:
    actual = image[address - MAIN_BASE : address - MAIN_BASE + len(expected)]
    if actual != expected:
        raise ValueError(
            f"{label}: signature mismatch at 0x{address:08X}: "
            f"expected {expected.hex()}, got {actual.hex()}"
        )


def load_emulator(path: Path):
    spec = importlib.util.spec_from_file_location("audio_handoff_minicoldfire", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load emulator module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def attach_sram_trace(bus, cpu) -> tuple[list[dict], list[dict]]:
    reads: list[dict] = []
    writes: list[dict] = []
    raw_read = bus.read
    raw_write = bus.write

    def read(address: int, size: int) -> int:
        value = raw_read(address, size)
        if 0x80000000 <= address < 0x80010000:
            reads.append({"pc": cpu.pc, "address": address, "size": size, "value": value})
        return value

    def write(address: int, size: int, value: int) -> None:
        if 0x80000000 <= address < 0x80010000:
            writes.append(
                {
                    "pc": cpu.pc,
                    "address": address,
                    "size": size,
                    "value": value & ((1 << (size * 8)) - 1),
                }
            )
        raw_write(address, size, value)

    bus.read = read
    bus.write = write
    return reads, writes


def execute_call(cpu, address: int, args: tuple[int, ...] = (), limit: int = 100_000) -> int:
    original_sp = cpu.a[7]
    start_steps = cpu.steps
    for value in reversed(args):
        cpu.pushl(value)
    cpu.pushl(RETURN_PC)
    cpu.pc = address
    for _ in range(limit):
        if cpu.pc == RETURN_PC:
            cpu.a[7] = original_sp
            return cpu.steps - start_steps
        cpu.step()
    raise ValueError(f"call 0x{address:08X} stalled at 0x{cpu.pc:08X}")


def addresses(events: list[dict], start: int, end: int, *, pc: tuple[int, ...] | None = None) -> list[int]:
    return [
        event["address"]
        for event in events
        if start <= event["address"] < end and (pc is None or event["pc"] in pc)
    ]


def tcd_snapshot(bus, base: int) -> dict:
    return {
        name: f"0x{bus._mmio_raw_read(base + offset, size):0{size * 2}X}"
        for name, offset, size in TCD_FIELDS
    }


def decode_elink(word: int) -> dict:
    return {
        "elink": bool(word & 0x8000),
        "link_channel": (word >> 9) & 0x1F,
        "iteration_count": word & 0x1FF,
    }


def prepare_audio_call(module, main_path: Path):
    bus = module.Bus()
    bus.load_main(main_path)
    cpu = module.CPU(bus)
    cpu.a[7] = module.INITIAL_SP - 0x1000
    # Keep interface objects away from renderer work arrays at 0x80007000.
    objects = (0x80003000, 0x80003100)
    for pointer, obj in zip(module.AUDIO_IFACE_PTRS, objects):
        bus.write(pointer, 4, obj)
    bus.write(objects[0] + module.AUDIO_IFACE_STATUS_OFF, 2, 1)
    bus.write(0x80005829, 1, 1)
    return bus, cpu


def run_pipeline(module, main_path: Path) -> dict:
    bus, cpu = prepare_audio_call(module, main_path)
    reads, writes = attach_sram_trace(bus, cpu)

    pre_render_steps = execute_call(cpu, PRE_RENDER)
    stock_voice_writes = addresses(writes, VOICE_BASE, VOICE_END)
    expected_voice_addresses = list(range(VOICE_BASE, VOICE_END, 4))
    if sorted(set(stock_voice_writes)) != expected_voice_addresses:
        raise ValueError("pre-render did not write the complete eight-voice BR slab")

    # Tagged fixture proves the renderer's address permutation independently
    # of the zero-filled peripheral state in the compact board model.
    for voice in range(VOICE_COUNT):
        for sample in range(SAMPLES_PER_BLOCK):
            tag = 0x10000000 | (voice << 16) | sample
            bus.write(VOICE_BASE + voice * VOICE_BYTES + sample * 4, 4, tag)
    reads.clear()
    writes.clear()
    renderer_steps = execute_call(cpu, RENDERER, (FRAME_BASE,))

    renderer_read_events = [event for event in reads if VOICE_BASE <= event["address"] < VOICE_END]
    renderer_reads = [event["address"] for event in renderer_read_events]
    renderer_writes = addresses(writes, FRAME_BASE, FRAME_END)
    if renderer_reads != expected_voice_addresses:
        raise ValueError("renderer did not read every voice-major word exactly once")
    expected_renderer_writes = [
        FRAME_BASE + sample * FRAME_STRIDE + voice * 4
        for voice in range(VOICE_COUNT)
        for sample in range(SAMPLES_PER_BLOCK)
    ]
    if renderer_writes != expected_renderer_writes:
        raise ValueError("renderer did not emit the expected voice-to-frame address permutation")
    for event in renderer_read_events:
        offset = event["address"] - VOICE_BASE
        voice, sample_bytes = divmod(offset, VOICE_BYTES)
        expected = 0x10000000 | (voice << 16) | (sample_bytes // 4)
        if event["value"] != expected:
            raise ValueError("renderer did not consume the tagged source word")

    # A synthetic, non-overlapping SRAM pointer stands in for the runtime ring
    # buffer allocation which is absent from the compact boot fixture.
    bus.write(OUTPUT_POINTER_GLOBAL, 4, SYNTHETIC_OUTPUT)
    bus.write(module.AUDIO_DMA_TCD30_CSR, 2, module.AUDIO_DMA_POLLED_BIT)
    reads.clear()
    writes.clear()
    handoff_steps = execute_call(cpu, HANDOFF, (FRAME_BASE,))

    pack_reads = addresses(reads, FRAME_BASE, FRAME_END, pc=(0x40109FD2,))
    pack_writes = addresses(writes, FIXED_STAGE, FIXED_STAGE_END, pc=(0x40109FEC, 0x40109FF0))
    if len(pack_reads) != 512 or len(set(pack_reads)) != 256:
        raise ValueError("handoff source geometry changed")
    if pack_reads[:256] != pack_reads[256:]:
        raise ValueError("handoff's two output groups no longer share the same source windows")
    if len(pack_writes) != 128 or sorted(set(pack_writes)) != list(range(FIXED_STAGE, FIXED_STAGE_END, 4)):
        raise ValueError("fixed 0x200-byte staging write geometry changed")

    output_writes = addresses(writes, SYNTHETIC_OUTPUT, OUTPUT_BLOCK_END)
    if len(set(output_writes)) != 64 or sorted(set(output_writes)) != list(range(SYNTHETIC_OUTPUT, OUTPUT_BLOCK_END, 4)):
        raise ValueError("downstream pipeline did not populate the full 0x100-byte DMA block")

    tcd42 = tcd_snapshot(bus, TCD42_BASE)
    expected_tcd42 = {
        "saddr": f"0x{SYNTHETIC_OUTPUT:08X}",
        "attr": "0x04B4",
        "soff": "0x0010",
        "nbytes": "0x00000010",
        "slast": "0x00000000",
        "daddr": "0x4B400000",
        "citer": "0xD410",
        "doff": "0x0010",
        "dlast_sga": "0x8000CDA0",
        "biter": "0xD410",
        "csr": "0x0011",
    }
    if tcd42 != expected_tcd42:
        raise ValueError(f"unexpected TCD42 state: {tcd42}")

    first_group = pack_reads[:256]
    return {
        "pre_render": {
            "entry": f"0x{PRE_RENDER:08X}",
            "instructions": pre_render_steps,
            "stock_unique_longword_writes": len(set(stock_voice_writes)),
            "voice_blocks": VOICE_COUNT,
            "samples_per_voice": SAMPLES_PER_BLOCK,
            "range": f"0x{VOICE_BASE:08X}..0x{VOICE_END - 1:08X}",
        },
        "renderer": {
            "entry": f"0x{RENDERER:08X}",
            "instructions": renderer_steps,
            "tagged_input_words_matched": 256,
            "address_permutation_words_matched": 256,
            "numeric_transform": "renderer arithmetic changes sample values; only input provenance and output address permutation are asserted",
            "input_layout": "8 voice-major blocks x 32 signed-fractional longwords",
            "output_layout": "32 frames x 0x40-byte stride; voice slots 0..7 at +0x00..+0x1C",
            "output_range": f"0x{FRAME_BASE:08X}..0x{FRAME_END - 1:08X}",
        },
        "handoff": {
            "entry": f"0x{HANDOFF:08X}",
            "instructions_through_return": handoff_steps,
            "source_reads": len(pack_reads),
            "unique_source_longwords": len(set(pack_reads)),
            "two_source_groups_identical": True,
            "first_source": f"0x{min(first_group):08X}",
            "last_source": f"0x{max(first_group):08X}",
            "initial_stage_writes": len(pack_writes),
            "fixed_stage": f"0x{FIXED_STAGE:08X}..0x{FIXED_STAGE_END - 1:08X}",
            "stage_layout": "2 output groups x 32 frames x 2 longwords",
            "downstream_unique_dma_block_writes": len(set(output_writes)),
            "synthetic_output_pointer_global": f"0x{OUTPUT_POINTER_GLOBAL:08X}",
            "synthetic_output_block": f"0x{SYNTHETIC_OUTPUT:08X}..0x{OUTPUT_BLOCK_END - 1:08X}",
            "tcd30_poll_observations": len(bus.audio_dma_events),
        },
        "outbound_dma_tcd42": {
            "base": f"0x{TCD42_BASE:08X}",
            "channel": 42,
            "direction": "SRAM output block -> external 0x4B400000 window",
            "major_bytes": 16 * 16,
            "citer_decode": decode_elink(0xD410),
            "descriptor": tcd42,
            "source_note": "SADDR equals the synthetic value placed in runtime pointer global 0x8000DDB0.",
        },
    }


def run_tcd30_setup(module, main_path: Path) -> dict:
    bus = module.Bus()
    bus.load_main(main_path)
    cpu = module.CPU(bus)
    cpu.a[7] = module.INITIAL_SP - 0x1000
    instructions = execute_call(cpu, TCD30_SETUP)
    descriptor = tcd_snapshot(bus, TCD30_BASE)
    expected = {
        "saddr": "0x4B7FFFF0",
        "attr": "0xB402",
        "soff": "0x0010",
        "nbytes": "0x00000010",
        "slast": "0x00000000",
        "daddr": "0x8000DDD0",
        "citer": "0xBC11",
        "doff": "0x0004",
        "dlast_sga": "0x8000E1A0",
        "biter": "0xBC11",
        "csr": "0x0011",
    }
    if descriptor != expected:
        raise ValueError(f"unexpected TCD30 state: {descriptor}")
    return {
        "setup_entry": f"0x{TCD30_SETUP:08X}",
        "instructions": instructions,
        "base": f"0x{TCD30_BASE:08X}",
        "channel": 30,
        "direction": "external 0x4B7FFFF0 window -> SRAM 0x8000DDD0",
        "major_bytes": 17 * 16,
        "citer_decode": decode_elink(0xBC11),
        "descriptor": descriptor,
        "classification": "input/capture-side DMA synchronization, not the outbound DAC transfer",
    }


def trace(main_path: Path, emulator_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")

    at(image, 0x4010A2F4, bytes.fromhex("45f9800067f8"), "renderer voice-slab start")
    at(image, 0x4010A2FA, bytes.fromhex("47f980006bf8"), "renderer voice-slab end")
    at(image, HANDOFF, bytes.fromhex("4fefffd048d77cfc"), "handoff prologue")
    at(image, 0x40109FE8, bytes.fromhex("a1ce26cea3ce26ce"), "paired fixed-stage stores")
    at(image, 0x40109FFE, bytes.fromhex("3039fc0453de7210c0814a4066f2"), "TCD30 poll")
    at(image, 0x4011CC4E, bytes.fromhex("4eb940109f04"), "handoff call site")

    module = load_emulator(emulator_path)
    pipeline = run_pipeline(module, main_path)
    tcd30 = run_tcd30_setup(module, main_path)
    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "pipeline": pipeline,
        "input_dma_tcd30": tcd30,
        "filter2_insertion": {
            "preferred_boundary": f"0x{VOICE_BASE:08X}..0x{VOICE_END - 1:08X}, after stock BR and before 0x{RENDERER:08X}",
            "reason": "Samples are still separated into eight 32-longword physical-voice blocks before renderer transposition and shared downstream processing.",
            "semantic_status": "proven by stock execution plus a 256-word tagged transpose test",
            "cycle_status": "not proven; hardware timing or cycle-counter measurements are still required",
        },
        "conclusion": (
            "The per-voice post-BR slab is the last clean, proven voice-separated sample boundary. "
            "TCD30 is input-side; TCD42 is the outbound 256-byte DMA handoff."
        ),
        "safety": "Read-only stock execution with synthetic RAM fixtures; no firmware was modified, repacked, or flashed.",
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
