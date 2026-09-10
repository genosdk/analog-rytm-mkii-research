#!/usr/bin/env python3
"""Trace authentic BR control through MAIN's outbound hardware packet."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256, prepared_machine
from trigger_queue_probe import (
    CONTROL_SNAPSHOT_A, CONTROL_SNAPSHOT_POINTER, QUEUE, QUEUE_CAPACITY,
    QUEUE_INITIALIZER, QUEUE_INSTALLER, QUEUE_RING, TRACK_BR_SOURCE,
    TRIGGER_FLAGS, TRIGGER_RECORD, load_emulator, run_complete_callback,
    stock_call,
)

CONTROL_DMA_INITIALIZER = 0x40077C54
CONTROL_PACKETIZER = 0x40077D14
CONTROL_DMA_STARTER = 0x40077DA2
CONTROL_DMA_CHANNEL = 15
FIFO_STATUS = 0xFC03C02C
FIFO_DATA = 0xFC03C034
PING_PONG_SELECTOR = 0x417CA338
PING_PONG_BASE = 0x80004800
PING_PONG_STRIDE = 0x810
VOICE_CONTROL_WORD = 0x80006544
WIRE_HIGH_OFFSET = 0x318
WIRE_LOW_OFFSET = 0x31C


def run_vector(module, main_path: Path, source_br: int) -> dict:
    bus, cpu, _ = prepared_machine(module, main_path)
    base_sp = cpu.a[7]
    stock_call(cpu, CONTROL_DMA_INITIALIZER, [])
    stock_call(cpu, QUEUE_INITIALIZER, [QUEUE, 0, QUEUE_RING, QUEUE_CAPACITY])
    stock_call(cpu, QUEUE_INSTALLER, [QUEUE])
    bus.write(CONTROL_SNAPSHOT_POINTER, 4, CONTROL_SNAPSHOT_A)
    bus.write(TRACK_BR_SOURCE, 4, source_br)
    bus.write(TRIGGER_RECORD, 4, 1)
    bus.write(TRIGGER_FLAGS, 4, 0x80)

    callbacks = [run_complete_callback(cpu, base_sp) for _ in range(11)]
    packed = bus.read(VOICE_CONTROL_WORD, 4)
    selector = bus.read(PING_PONG_SELECTOR, 4)
    packet_base = PING_PONG_BASE + selector * PING_PONG_STRIDE
    wire_high = bus.read(packet_base + WIRE_HIGH_OFFSET, 4)
    wire_low = bus.read(packet_base + WIRE_LOW_OFFSET, 4)
    if wire_high != (0x8001B000 | ((packed >> 16) & 0xFFFF)):
        raise ValueError("packetizer did not serialize the packed high word")
    if wire_low != (0x80010000 | (packed & 0xFFFF)):
        raise ValueError("packetizer did not serialize the packed low word")

    # Hardware normally supplies the request. Expose FIFO-ready status, let
    # stock code select/enable channel 15, then deliver one modeled request.
    bus.write(FIFO_STATUS, 4, 1 << 28)
    stock_call(cpu, CONTROL_DMA_STARTER, [])
    if CONTROL_DMA_CHANNEL not in bus.edma_erq:
        raise ValueError("stock starter did not enable eDMA channel 15")
    if not bus._edma_service(CONTROL_DMA_CHANNEL, force=True):
        raise ValueError("modeled peripheral request did not service channel 15")
    event = [x for x in bus.edma_events if x["ch"] == CONTROL_DMA_CHANNEL][-1]
    payload = bytes(event["bytes"])
    expected_source = packet_base + 0x0C
    if (
        event["saddr"] != expected_source or event["daddr"] != FIFO_DATA
        or event["nbytes"] != 4 or event["citer"] != 510
        or event["done_bytes"] != 2040
    ):
        raise ValueError(f"unexpected channel-15 transfer: {event}")
    high_payload_offset = WIRE_HIGH_OFFSET - 0x0C
    low_payload_offset = WIRE_LOW_OFFSET - 0x0C
    if int.from_bytes(payload[high_payload_offset:high_payload_offset + 4], "big") != wire_high:
        raise ValueError("DMA payload lost packed high word")
    if int.from_bytes(payload[low_payload_offset:low_payload_offset + 4], "big") != wire_low:
        raise ValueError("DMA payload lost packed low word")

    return {
        "source_br": f"0x{source_br:08X}",
        "case_3_br": f"0x{callbacks[-1]['br_frame_at_renderer'][0]:04X}",
        "packed_control": f"0x{packed:08X}",
        "packet": {
            "selector": selector, "base": f"0x{packet_base:08X}",
            "wire_high_address": f"0x{packet_base + WIRE_HIGH_OFFSET:08X}",
            "wire_high": f"0x{wire_high:08X}",
            "wire_low_address": f"0x{packet_base + WIRE_LOW_OFFSET:08X}",
            "wire_low": f"0x{wire_low:08X}",
        },
        "dma": {
            "channel": CONTROL_DMA_CHANNEL, "source": f"0x{event['saddr']:08X}",
            "destination": f"0x{event['daddr']:08X}",
            "minor_bytes": event["nbytes"], "iterations": event["citer"],
            "transferred_bytes": event["done_bytes"],
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "payload_hex_near_voice_control": payload[
                high_payload_offset - 4:low_payload_offset + 8
            ].hex(),
        },
        "payload": payload,
    }


def probe(main_path: Path, emulator_path: Path) -> dict:
    digest = hashlib.sha256(main_path.read_bytes()).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    module = load_emulator(emulator_path)
    zero = run_vector(module, main_path, 0)
    high = run_vector(module, main_path, 0x7F000000)
    if zero["case_3_br"] != "0x0000" or high["case_3_br"] != "0x0000":
        raise ValueError("unexpected natural case-3 BR vectors")
    if zero["packed_control"] != "0x00180FFF" or high["packed_control"] != "0x00180FFF":
        raise ValueError("unexpected natural packed-control vectors")
    differences = [
        index for index, (left, right) in enumerate(zip(zero["payload"], high["payload"]))
        if left != right
    ]
    if differences != [0xBA, 0xBB, 0xBE, 0xBF]:
        raise ValueError(f"unexpected raw-source DMA differences: {differences}")
    for vector in (zero, high):
        del vector["payload"]

    return {
        "result": "PASS_DMA_SINK_BR_PATH_RETRACTED",
        "main": {"path": str(main_path), "sha256": digest},
        "emac_correction": {
            "normal_loads_ignore_mask": True,
            "load_form_is_single_accumulate": True,
            "mask_addressing_modifier": "MACL/MSACL extension MAM bit 5",
            "effect": "the earlier natural case-3 BR result is no longer reproducible",
        },
        "control_path": {
            "voice_control_word": f"0x{VOICE_CONTROL_WORD:08X}",
            "packetizer": f"0x{CONTROL_PACKETIZER:08X}",
            "dma_initializer": f"0x{CONTROL_DMA_INITIALIZER:08X}",
            "dma_starter": f"0x{CONTROL_DMA_STARTER:08X}",
            "ping_pong_base": f"0x{PING_PONG_BASE:08X}",
            "ping_pong_stride": f"0x{PING_PONG_STRIDE:X}",
            "fifo": f"0x{FIFO_DATA:08X}",
        },
        "vectors": [zero, high],
        "payload_difference_offsets": [f"0x{x:03X}" for x in differences],
        "retracted_claim": {
            "claim": "TRACK_BR_SOURCE reaches natural case-3 packed-control words",
            "reason": (
                "The previous 0x7B31/0x001FD285 result required the stale dual-EMAC "
                "decoder. Correct load-form semantics leave case-3 BR at zero."
            ),
        },
        "conclusion": (
            "The stock packetizer and eDMA channel 15 still transfer a 2,040-byte control "
            "frame to peripheral FIFO 0xFC03C034. The current high TRACK_BR_SOURCE fixture "
            "changes four earlier payload bytes but does not alter the natural case-3 BR "
            "word under corrected EMAC semantics, so that propagation claim is retracted."
        ),
        "next_target": (
            "Recover the correct stock BR publication source and repeat the packet "
            "differential; channel-15 transport geometry itself remains proven."
        ),
        "safety": "Emulation and RAM/MMIO modeling only; firmware bytes were not modified.",
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
