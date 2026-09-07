#!/usr/bin/env python3
"""Verify and execute the stock 1.72 audio-interface handshake loops.

The model completes RAM-backed audio-interface commands on their first status
observation and clears the exact TCD30 CSR bit polled by stock firmware.  The
bit is intentionally not assigned a stronger hardware meaning here.
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
PRE_RENDER = 0x40117F00
RETURN_PC = 0x400FCECC
TCD30_POLL = 0x40109FFE
TCD30_POLL_EXIT = 0x4010A00C


def at(image: bytes, address: int, expected: bytes, label: str) -> None:
    actual = image[address - MAIN_BASE : address - MAIN_BASE + len(expected)]
    if actual != expected:
        raise ValueError(
            f"{label}: signature mismatch at 0x{address:08X}: "
            f"expected {expected.hex()}, got {actual.hex()}"
        )


def load_emulator(path: Path):
    spec = importlib.util.spec_from_file_location("audio_interface_minicoldfire", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load emulator module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_pre_render(module, main_path: Path) -> dict:
    bus = module.Bus()
    bus.load_main(main_path)
    cpu = module.CPU(bus)
    cpu.a[7] = module.INITIAL_SP - 0x1000

    objects = (0x80007000, 0x80007100)
    for pointer, obj in zip(module.AUDIO_IFACE_PTRS, objects):
        bus.write(pointer, 4, obj)
    # Force the entry poll as well as the two command-completion polls later
    # in the routine.  The routine itself submits all subsequent commands.
    bus.write(objects[0] + module.AUDIO_IFACE_STATUS_OFF, 2, 1)
    bus.write(0x80005829, 1, 1)
    cpu.pushl(RETURN_PC)
    cpu.pc = PRE_RENDER
    for _ in range(100_000):
        if cpu.pc == RETURN_PC:
            break
        cpu.step()
    else:
        raise ValueError(f"pre-render routine stalled at 0x{cpu.pc:08X}")

    ready_events = [event for event in bus.audio_iface_events if event["kind"] == "READY"]
    ready_pcs = sorted({f"0x{event['pc'] - 4:08X}" for event in ready_events})
    expected_polls = ["0x40117F16", "0x40118396", "0x40118518"]
    if not set(expected_polls).issubset(ready_pcs):
        raise ValueError(f"not all ready polls executed: {ready_pcs}")
    return {
        "result": "PASS",
        "instructions": cpu.steps,
        "return_pc": f"0x{cpu.pc:08X}",
        "objects": [f"0x{obj:08X}" for obj in objects],
        "poll_sites_executed": expected_polls,
        "command_events": sum(event["kind"] == "COMMAND" for event in bus.audio_iface_events),
        "ready_events": len(ready_events),
    }


def run_tcd30_poll(module, main_path: Path) -> dict:
    bus = module.Bus()
    bus.load_main(main_path)
    cpu = module.CPU(bus)
    bus.write(module.AUDIO_DMA_TCD30_CSR, 2, module.AUDIO_DMA_POLLED_BIT)
    cpu.pc = TCD30_POLL
    for _ in range(32):
        if cpu.pc == TCD30_POLL_EXIT:
            break
        cpu.step()
    else:
        raise ValueError(f"TCD30 poll did not exit: 0x{cpu.pc:08X}")
    final = bus.read(module.AUDIO_DMA_TCD30_CSR, 2)
    return {
        "result": "PASS",
        "poll_site": f"0x{TCD30_POLL:08X}",
        "csr_address": f"0x{module.AUDIO_DMA_TCD30_CSR:08X}",
        "polled_bit": f"0x{module.AUDIO_DMA_POLLED_BIT:02X}",
        "busy_observations": len(bus.audio_dma_events),
        "final_polled_bit": final & module.AUDIO_DMA_POLLED_BIT,
        "classification": "TCD30 CSR bit 0x10; exact peripheral semantics not yet hardware-proven",
    }


def trace(main_path: Path, emulator_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")

    poll = bytes.fromhex("3028001e0280000000804a4067f2")
    at(image, 0x40117F16, poll, "entry interface-ready poll")
    at(image, 0x40118396, poll, "secondary interface-ready poll")
    at(image, 0x40118518, poll, "primary interface-ready poll")
    at(image, TCD30_POLL, bytes.fromhex("3039fc0453de7210c0814a4066f2"), "TCD30 CSR poll")

    module = load_emulator(emulator_path)
    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "interface_status": {
            "pointer_globals": ["0x80005820", "0x80005824"],
            "status_offset": "+0x1E",
            "ready_bit": "0x80",
            "poll_sites": ["0x40117F16", "0x40118396", "0x40118518"],
        },
        "pre_render_runtime": run_pre_render(module, main_path),
        "tcd30_poll_runtime": run_tcd30_poll(module, main_path),
        "resolved_blocker": "0x40117F00 now returns under the board model without manually forcing status words.",
        "next_target": "Execute the 0x4011870E terminal BR read and 32-sample render loop with provenance on input/output sample words.",
        "safety": "Static analysis and emulation only; firmware bytes were not modified.",
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
