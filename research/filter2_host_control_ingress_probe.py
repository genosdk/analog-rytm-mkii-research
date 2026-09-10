#!/usr/bin/env python3
"""Prove the initial desktop mouse/QWERTY ingress for Filter 2 emulation."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256
from filter2_coefficient_slew_probe import control_to_q31
from filter2_publication_shim_probe import (
    SHIM_BASE,
    VIRTUAL_INDEX_BASE,
    build_candidate,
    target_address,
)
from filter2_unity_kernel_probe import FILTER2_MASK_ADDRESS, FLAGS_ADDRESS
from trigger_queue_probe import load_emulator, stock_call

LANES = 8
FILTER2_FLAG = 1
ALL_LANES_MASK = 0x00FF
IDLE_PC = 0x400FCECC
DEFAULT_INTERRUPT_HANDLER = 0x40000D6E
UART8_RX_CHANNEL = 34
UART8_TX_CHANNEL = 35
UART8_RX_VECTOR = 154
UART8_TX_VECTOR = 155


@dataclass
class DesktopController:
    """Shared 0..127 controller contract used by mouse and QWERTY inputs."""

    selected_lane: int = 0
    values: list[int] = field(default_factory=lambda: [0] * LANES)

    @staticmethod
    def clamp(value: int) -> int:
        return max(0, min(127, value))

    def mouse(self, lane: int, value: int) -> dict:
        if not 0 <= lane < LANES:
            raise ValueError("mouse lane must be 0..7")
        self.selected_lane = lane
        self.values[lane] = self.clamp(value)
        return self.command("mouse_absolute")

    def key(self, key: str) -> dict | None:
        if key.startswith("Digit") and key[5:].isdigit():
            lane = int(key[5:]) - 1
            if 0 <= lane < LANES:
                self.selected_lane = lane
                return None
        deltas = {
            "ArrowUp": 1, "ArrowRight": 1,
            "ArrowDown": -1, "ArrowLeft": -1,
            "PageUp": 8, "PageDown": -8,
        }
        if key in deltas:
            self.values[self.selected_lane] = self.clamp(
                self.values[self.selected_lane] + deltas[key]
            )
            return self.command("keyboard_delta")
        if key == "Home":
            self.values[self.selected_lane] = 0
            return self.command("keyboard_home")
        if key == "End":
            self.values[self.selected_lane] = 127
            return self.command("keyboard_end")
        raise ValueError(f"unsupported controller key: {key}")

    def command(self, source: str) -> dict:
        lane = self.selected_lane
        value = self.values[lane]
        return {
            "source": source,
            "lane": lane,
            "value": value,
            "virtual_index": VIRTUAL_INDEX_BASE + lane,
        }


def audit_storage_free_uarts(module, main_path: Path) -> dict:
    bus = module.Bus()
    bus.load_main(main_path)
    cpu = module.CPU(bus)
    cpu.a[7] = module.INITIAL_SP
    bus.write(module.INITIAL_SP + 4, 4, 0)
    cpu.run(12_000_000)
    if cpu.pc != IDLE_PC:
        raise ValueError(f"storage-free boot did not reach idle: 0x{cpu.pc:08X}")

    channels = []
    for channel in (UART8_RX_CHANNEL, UART8_TX_CHANNEL):
        address = module.EDMA_TCD_BASE + channel * module.EDMA_TCD_STRIDE
        raw = bytes(bus._mmio_raw_read(address + offset, 1) for offset in range(module.EDMA_TCD_STRIDE))
        channels.append({
            "channel": channel,
            "tcd_hex": raw.hex(),
            "tcd_all_zero": not any(raw),
            "request_enabled": channel in bus.edma_erq,
        })
    vectors = {
        str(vector): f"0x{bus.read(cpu.ctrl.get(0x801, 0) + vector * 4, 4):08X}"
        for vector in (UART8_RX_VECTOR, UART8_TX_VECTOR)
    }
    if not all(entry["tcd_all_zero"] and not entry["request_enabled"] for entry in channels):
        raise ValueError("storage-free boot unexpectedly activated UART8 DMA")
    if set(vectors.values()) != {f"0x{DEFAULT_INTERRUPT_HANDLER:08X}"}:
        raise ValueError("storage-free UART8 vectors are no longer default")
    return {
        "boot_pc": f"0x{cpu.pc:08X}",
        "semantic_boot_steps": cpu.steps,
        "uart8_dma_channels": channels,
        "uart8_vectors": vectors,
        "result": "NOT_ACTIVE_IN_STORAGE_FREE_BOOT",
        "interpretation": (
            "This rejects UART8 as the first emulator ingress; it does not prove that "
            "physical MIDI/USB is absent on initialized hardware."
        ),
    }


def event_sequence() -> tuple[list[dict], list[int]]:
    controller = DesktopController()
    commands = []
    for lane, value in enumerate((0, 16, 32, 48, 64, 80, 96, 112)):
        commands.append(controller.mouse(lane, value))
    for key in ("Digit8", "End", "Digit1", "ArrowUp", "Digit4", "PageDown"):
        command = controller.key(key)
        if command is not None:
            commands.append(command)
    expected = [1, 16, 32, 40, 64, 80, 96, 127]
    if controller.values != expected:
        raise ValueError(f"desktop controller event mapping diverged: {controller.values}")
    return commands, expected


def execute_host_bridge(module, candidate_path: Path) -> dict:
    bus = module.Bus()
    bus.load_main(candidate_path)
    cpu = module.CPU(bus)
    cpu.a[7] = module.INITIAL_SP - 0x2000

    # Runtime-only arming: the retained decompressed image remains default-disabled.
    bus.write(FLAGS_ADDRESS, 4, FILTER2_FLAG)
    bus.write(FILTER2_MASK_ADDRESS, 2, ALL_LANES_MASK)
    commands, expected = event_sequence()
    executed = []
    original_write = bus.write
    for sequence, command in enumerate(commands):
        writes = []

        def traced_write(address: int, size: int, value: int) -> None:
            if any(address == target_address(lane) for lane in range(LANES)):
                writes.append((address, size, value & 0xFFFFFFFF))
            original_write(address, size, value)

        bus.write = traced_write
        steps = stock_call(cpu, SHIM_BASE, [command["virtual_index"], command["value"]])
        bus.write = original_write
        lane = command["lane"]
        coefficient = control_to_q31(command["value"])
        if writes != [(target_address(lane), 4, coefficient)]:
            raise ValueError(f"host command {sequence} did not publish one aligned lane store")
        executed.append({
            "sequence": sequence,
            **command,
            "virtual_index": f"0x{command['virtual_index']:04X}",
            "q31": f"0x{coefficient:08X}",
            "target_address": f"0x{target_address(lane):08X}",
            "instructions": steps,
            "single_aligned_store": target_address(lane) % 4 == 0,
        })

    final = [bus.read(target_address(lane), 4) for lane in range(LANES)]
    expected_q31 = [control_to_q31(value) for value in expected]
    if final != expected_q31:
        raise ValueError("final host-controlled Q1.31 target vector diverged")
    return {
        "runtime_arming_only": True,
        "commands_executed": len(executed),
        "events": executed,
        "final_controls": expected,
        "final_q31": [f"0x{value:08X}" for value in final],
        "all_commands_single_aligned_store": all(event["single_aligned_store"] for event in executed),
        "result": "PASS",
    }


def probe(stock_path: Path, emulator_path: Path) -> dict:
    stock = stock_path.read_bytes()
    digest = hashlib.sha256(stock).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    candidate, build = build_candidate(stock, False)
    module = load_emulator(emulator_path)
    uart_audit = audit_storage_free_uarts(module, stock_path)
    with tempfile.NamedTemporaryFile(suffix=".bin") as temporary:
        candidate_path = Path(temporary.name)
        candidate_path.write_bytes(candidate)
        host_bridge = execute_host_bridge(module, candidate_path)

    return {
        "result": "PASS",
        "stock": {"path": str(stock_path), "sha256": digest},
        "candidate_reused": {
            "sha256": hashlib.sha256(candidate).hexdigest(),
            "expected_publication_shim_sha256": "25dd3dbfc0276f4da630e2804851d1e542f48aacc9a29cc91242a23ecaf69549",
            "new_firmware_bytes_created": False,
            "build": build,
        },
        "storage_free_uart_audit": uart_audit,
        "selected_initial_ingress": {
            "name": "host/emulator command bridge",
            "transport": "direct call to internal virtual setter ABI",
            "virtual_indices": [f"0x{VIRTUAL_INDEX_BASE:04X}", f"0x{VIRTUAL_INDEX_BASE + LANES - 1:04X}"],
            "control_domain": "0..127",
            "why_selected": (
                "It adds no firmware instructions, reuses the proven shim, and is independent "
                "of USB/MIDI peripherals not yet active in the storage-free board model."
            ),
        },
        "desktop_event_contract": {
            "mouse": "absolute lane value, clamped to 0..127",
            "lane_selection_keys": "Digit1..Digit8",
            "fine_keys": {"ArrowUp/ArrowRight": "+1", "ArrowDown/ArrowLeft": "-1"},
            "coarse_keys": {"PageUp": "+8", "PageDown": "-8"},
            "endpoints": {"Home": 0, "End": 127},
        },
        "host_bridge_execution": host_bridge,
        "conclusion": (
            "The first external laboratory ingress is now selected and executed: mouse and "
            "QWERTY events share one deterministic 0..127 desktop contract and publish through "
            "the existing virtual setter ABI. UART8 is not active in the storage-free boot and "
            "is therefore not used as an assumed MIDI path."
        ),
        "scope_limit": (
            "This is the controller/backend contract, not yet a rendered knob interface or a "
            "physical MIDI/USB proof. QWERTY note triggering remains separate from Filter 2 control."
        ),
        "next_target": (
            "Wrap the host bridge in a local controller service and render eight mouse-draggable "
            "knobs with keyboard focus/state feedback. Then separately trace physical MIDI/USB "
            "for eventual hardware transport."
        ),
        "safety": (
            "The already-retained default-disabled publication candidate was reused temporarily. "
            "Only emulator RAM was armed; no new MAIN image, ELE3 container or SysEx was built."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_main", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = probe(args.stock_main, args.emulator)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.report:
        args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
