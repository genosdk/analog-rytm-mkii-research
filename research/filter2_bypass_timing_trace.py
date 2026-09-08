#!/usr/bin/env python3
"""Prove an exact post-BR Filter 2 bypass and bound its scheduling cost.

The candidate exists only in emulator memory.  It replaces the renderer call
with a call into a zero-filled cave whose entire body is a tail JMP back to the
stock renderer.  No firmware or SysEx artifact is emitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from audio_handoff_trace import (
    EXPECTED_MAIN_SHA256,
    FIXED_STAGE,
    FIXED_STAGE_END,
    FRAME_BASE,
    FRAME_END,
    HANDOFF,
    OUTPUT_BLOCK_END,
    OUTPUT_POINTER_GLOBAL,
    PRE_RENDER,
    SYNTHETIC_OUTPUT,
    TCD42_BASE,
    VOICE_BASE,
    VOICE_BYTES,
    VOICE_COUNT,
    VOICE_END,
    SAMPLES_PER_BLOCK,
    execute_call,
    load_emulator,
    prepare_audio_call,
    tcd_snapshot,
)

MAIN_BASE = 0x40000400
RENDERER = 0x4010A2E0
RENDERER_CALL = 0x4011CAE2
RENDERER_RETURN = 0x4011CAE8
VOICE_BRIDGE = 0x40108944
CONTROL_CONVERTER = 0x40105188
CONTROL_BLOCK = 0x800063C0
BYPASS_CAVE = 0x402B4800
STOCK_CALL = bytes.fromhex("4eb94010a2e0")
PATCHED_CALL = bytes.fromhex("4eb9402b4800")
BYPASS_BODY = bytes.fromhex("4ef94010a2e0")
SAMPLE_RATE = 48_000


def memory_bytes(bus, start: int, end: int) -> bytes:
    return bytes(bus.sram[address & 0xFFFF] for address in range(start, end))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_image_bytes(bus, address: int, data: bytes) -> None:
    for offset, value in enumerate(data):
        bus.write(address + offset, 1, value)


def tag_voice_slab(bus) -> None:
    for voice in range(VOICE_COUNT):
        for sample in range(SAMPLES_PER_BLOCK):
            value = 0x10000000 | (voice << 16) | sample
            bus.write(VOICE_BASE + voice * VOICE_BYTES + sample * 4, 4, value)


def execute_renderer_call_site(cpu) -> int:
    original_sp = cpu.a[7]
    cpu.pushl(FRAME_BASE)
    start_steps = cpu.steps
    cpu.pc = RENDERER_CALL
    for _ in range(10_000):
        if cpu.pc == RENDERER_RETURN:
            steps = cpu.steps - start_steps
            if cpu.a[7] != original_sp - 4:
                raise ValueError("renderer call did not preserve its caller argument")
            cpu.a[7] = original_sp
            return steps
        cpu.step()
    raise ValueError(f"renderer call-site execution stalled at 0x{cpu.pc:08X}")


def renderer_state(cpu) -> dict:
    return {
        "d": list(cpu.d),
        "a": list(cpu.a),
        "sr": cpu.sr,
        "macsr": cpu.macsr,
        "mac_mask": cpu.mac_mask,
        "macc": list(cpu.macc),
    }


def run_case(module, main_path: Path, patched: bool) -> dict:
    bus, cpu = prepare_audio_call(module, main_path)
    stock_at_call = bytes(bus.read(RENDERER_CALL + i, 1) for i in range(6))
    stock_at_cave = bytes(bus.read(BYPASS_CAVE + i, 1) for i in range(6))
    if stock_at_call != STOCK_CALL:
        raise ValueError(f"unexpected renderer call bytes: {stock_at_call.hex()}")
    if stock_at_cave != bytes(6):
        raise ValueError(f"bypass cave is not zero-filled: {stock_at_cave.hex()}")
    if patched:
        write_image_bytes(bus, RENDERER_CALL, PATCHED_CALL)
        write_image_bytes(bus, BYPASS_CAVE, BYPASS_BODY)

    pre_render_steps = execute_call(cpu, PRE_RENDER)
    tag_voice_slab(bus)
    voice_before = memory_bytes(bus, VOICE_BASE, VOICE_END)
    renderer_steps = execute_renderer_call_site(cpu)
    state_after_renderer = renderer_state(cpu)
    voice_after = memory_bytes(bus, VOICE_BASE, VOICE_END)
    if voice_after != voice_before:
        raise ValueError("renderer or bypass changed the post-BR source slab")

    frame = memory_bytes(bus, FRAME_BASE, FRAME_END)
    bridge_steps = execute_call(cpu, VOICE_BRIDGE, (CONTROL_BLOCK,))
    converter_steps = execute_call(cpu, CONTROL_CONVERTER, (CONTROL_BLOCK,))
    bus.write(OUTPUT_POINTER_GLOBAL, 4, SYNTHETIC_OUTPUT)
    bus.write(module.AUDIO_DMA_TCD30_CSR, 2, module.AUDIO_DMA_POLLED_BIT)
    handoff_steps = execute_call(cpu, HANDOFF, (FRAME_BASE,))
    fixed_stage = memory_bytes(bus, FIXED_STAGE, FIXED_STAGE_END)
    output_block = memory_bytes(bus, SYNTHETIC_OUTPUT, OUTPUT_BLOCK_END)

    return {
        "patched": patched,
        "instructions": {
            "pre_render": pre_render_steps,
            "renderer_call_site": renderer_steps,
            "voice_bridge": bridge_steps,
            "control_converter": converter_steps,
            "handoff": handoff_steps,
            "traced_component_total": (
                pre_render_steps
                + renderer_steps
                + bridge_steps
                + converter_steps
                + handoff_steps
            ),
        },
        "renderer_state": state_after_renderer,
        "hashes": {
            "post_br_voice_slab": digest(voice_after),
            "renderer_frame_slab": digest(frame),
            "fixed_stage": digest(fixed_stage),
            "outbound_dma_block": digest(output_block),
        },
        "nonzero_bytes": {
            "renderer_frame_slab": sum(value != 0 for value in frame),
            "fixed_stage": sum(value != 0 for value in fixed_stage),
            "outbound_dma_block": sum(value != 0 for value in output_block),
        },
        "tcd42": tcd_snapshot(bus, TCD42_BASE),
    }


def trace(main_path: Path, emulator_path: Path) -> dict:
    image = main_path.read_bytes()
    main_digest = hashlib.sha256(image).hexdigest()
    if main_digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {main_digest}")
    if image[RENDERER_CALL - MAIN_BASE : RENDERER_CALL - MAIN_BASE + 6] != STOCK_CALL:
        raise ValueError("renderer call signature mismatch")
    if image[BYPASS_CAVE - MAIN_BASE : BYPASS_CAVE - MAIN_BASE + 6] != bytes(6):
        raise ValueError("candidate bypass cave is not zero-filled in stock MAIN")
    literal_refs = image.count(BYPASS_CAVE.to_bytes(4, "big"))
    if literal_refs:
        raise ValueError("stock MAIN contains an absolute reference to the candidate cave")

    module = load_emulator(emulator_path)
    baseline = run_case(module, main_path, False)
    bypass = run_case(module, main_path, True)

    if baseline["renderer_state"] != bypass["renderer_state"]:
        raise ValueError("disabled bypass changed renderer return state")
    if baseline["hashes"] != bypass["hashes"]:
        raise ValueError("disabled bypass changed downstream audio buffers")
    if baseline["tcd42"] != bypass["tcd42"]:
        raise ValueError("disabled bypass changed outbound DMA programming")

    overhead = (
        bypass["instructions"]["renderer_call_site"]
        - baseline["instructions"]["renderer_call_site"]
    )
    if overhead != 1:
        raise ValueError(f"unexpected bypass overhead: {overhead} semantic instructions")

    frames_per_block = SAMPLES_PER_BLOCK
    blocks_per_second = SAMPLE_RATE / frames_per_block
    block_microseconds = 1_000_000 / blocks_per_second
    observed_steps = baseline["instructions"]["traced_component_total"]
    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": main_digest},
        "in_memory_candidate": {
            "call_site": f"0x{RENDERER_CALL:08X}",
            "stock_call": STOCK_CALL.hex(),
            "patched_call": PATCHED_CALL.hex(),
            "cave": f"0x{BYPASS_CAVE:08X}",
            "stock_cave_bytes": bytes(6).hex(),
            "bypass_body": BYPASS_BODY.hex(),
            "behavior": "JSR cave; cave tail-JMPs to the stock renderer",
            "stock_absolute_cave_references": literal_refs,
            "artifact_emitted": False,
        },
        "exact_bypass": {
            "renderer_return_state_identical": True,
            "post_br_voice_slab_identical": True,
            "renderer_frame_slab_identical": True,
            "fixed_stage_identical": True,
            "outbound_dma_block_identical": True,
            "tcd42_descriptor_identical": True,
            "hashes": baseline["hashes"],
            "nonzero_bytes": baseline["nonzero_bytes"],
            "signal_coverage": (
                "The renderer-frame comparison is nonzero and signal-bearing. "
                "The compact model leaves final mixer coefficients uninitialized, "
                "so fixed-stage and outbound-block equality is structural zero-state evidence."
            ),
        },
        "instruction_measurement": {
            "baseline": baseline["instructions"],
            "disabled_bypass": bypass["instructions"],
            "added_semantic_instructions_per_32_frame_block": overhead,
            "limitation": "MiniColdFire counts semantic instructions, not hardware cycles or SDRAM/cache stalls.",
        },
        "deadline_bound": {
            "sample_rate_hz": SAMPLE_RATE,
            "frames_per_block": frames_per_block,
            "blocks_per_second": blocks_per_second,
            "block_deadline_microseconds": block_microseconds,
            "traced_stock_component_instructions": observed_steps,
            "minimum_semantic_mips_for_traced_components_at_one_instruction_per_cycle": (
                observed_steps * blocks_per_second / 1_000_000
            ),
            "coverage": "lower bound; scheduler glue and untraced callback work are not included",
            "mcf54418_family_ceiling_mhz": 250,
            "family_clock_source": "https://www.nxp.com/part/MCF54418CMJ250",
            "board_clock_status": "unverified until PCB/bootloader or hardware timer measurement",
        },
        "gate_status": {
            "semantic_hook": "PASS",
            "disabled_bit_identity": "PASS through the nonzero renderer frame under emulation",
            "cycle_safe": "OPEN; one extra semantic instruction is measured, but total hardware timing margin is not",
        },
        "safety": "In-memory emulation only; no modified MAIN or SysEx file was written or flashed.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("main_image", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = trace(args.main_image, args.emulator)
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
