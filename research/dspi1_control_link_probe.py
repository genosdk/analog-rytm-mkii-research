#!/usr/bin/env python3
"""Classify the stock BR control transport at the DSPI1 hardware boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from audio_callback_probe import EXPECTED_MAIN_SHA256, prepared_machine
from br_hardware_sink_probe import (
    CONTROL_DMA_CHANNEL,
    CONTROL_DMA_INITIALIZER,
    CONTROL_DMA_STARTER,
    FIFO_STATUS,
    PING_PONG_BASE,
    PING_PONG_SELECTOR,
    PING_PONG_STRIDE,
)
from trigger_queue_probe import (
    CONTROL_SNAPSHOT_A,
    CONTROL_SNAPSHOT_POINTER,
    QUEUE,
    QUEUE_CAPACITY,
    QUEUE_INITIALIZER,
    QUEUE_INSTALLER,
    QUEUE_RING,
    TRACK_BR_SOURCE,
    TRIGGER_FLAGS,
    TRIGGER_RECORD,
    load_emulator,
    run_complete_callback,
    stock_call,
)

DSPI1_BASE = 0xFC03C000
DSPI1_MCR = DSPI1_BASE + 0x00
DSPI1_CTAR0 = DSPI1_BASE + 0x0C
DSPI1_SR = DSPI1_BASE + 0x2C
DSPI1_RSER = DSPI1_BASE + 0x30
DSPI1_PUSHR = DSPI1_BASE + 0x34
EDMA_TCD15 = 0xFC045000 + 0x20 * CONTROL_DMA_CHANNEL
PACKET_SOURCE_OFFSET = 0x0C
PACKET_WORDS = 510
MAIN_LOAD_ADDRESS = 0x40000400
DSPI1_SETUP = 0x4011D7F6
DSPI1_SETUP_BYTES = bytes.fromhex(
    "2039fc03c0008081223c7e00000023c0fc03c000"
    "203c7e00000142b9fc03c03023c0fc03c00c"
    "303c033523c1fc03c010223c3e00106423c0fc03c014"
    "203c80030c0023c1fc03c018721023c0fc03c000"
)
CTAR_VALUES = (0x7E000001, 0x7E000000, 0x7E000335, 0x3E001064)


def decode_ctar(value: int) -> dict:
    pbr = (2, 3, 5, 7)[(value >> 16) & 0x3]
    br = (2, 4, 6, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768)[value & 0xF]
    return {
        "raw": f"0x{value:08X}",
        "frame_bits": ((value >> 27) & 0xF) + 1,
        "cpol": (value >> 26) & 1,
        "cpha": (value >> 25) & 1,
        "lsb_first": bool(value & (1 << 24)),
        "baud_prescaler": pbr,
        "baud_scaler": br,
        "sck_divider": pbr * br,
    }


def decode_pushr(value: int) -> dict:
    return {
        "raw": f"0x{value:08X}",
        "continuous_chip_select": bool(value & 0x80000000),
        "ctar": (value >> 28) & 0x7,
        "end_of_queue": bool(value & 0x08000000),
        "clear_transfer_counter": bool(value & 0x04000000),
        "pcs_mask": f"0x{(value >> 16) & 0xFF:02X}",
        "tx_data": f"0x{value & 0xFFFF:04X}",
    }


def run_vector(module, main_path: Path, source_br: int) -> dict:
    bus, cpu, _ = prepared_machine(module, main_path)
    base_sp = cpu.a[7]
    accesses: list[dict] = []
    original_read, original_write = bus.read, bus.write

    def traced_read(address: int, size: int) -> int:
        value = original_read(address, size)
        if DSPI1_BASE <= address < DSPI1_BASE + 0x100:
            accesses.append({
                "kind": "read", "pc": f"0x{cpu.pc:08X}",
                "address": f"0x{address:08X}", "size": size,
                "value": f"0x{value:0{size * 2}X}",
            })
        return value

    def traced_write(address: int, size: int, value: int) -> None:
        if DSPI1_BASE <= address < DSPI1_BASE + 0x100:
            accesses.append({
                "kind": "write", "pc": f"0x{cpu.pc:08X}",
                "address": f"0x{address:08X}", "size": size,
                "value": f"0x{value & ((1 << (size * 8)) - 1):0{size * 2}X}",
            })
        original_write(address, size, value)

    bus.read, bus.write = traced_read, traced_write
    stock_call(cpu, CONTROL_DMA_INITIALIZER, [])
    bus.read, bus.write = original_read, original_write

    stock_call(cpu, QUEUE_INITIALIZER, [QUEUE, 0, QUEUE_RING, QUEUE_CAPACITY])
    stock_call(cpu, QUEUE_INSTALLER, [QUEUE])
    bus.write(CONTROL_SNAPSHOT_POINTER, 4, CONTROL_SNAPSHOT_A)
    bus.write(TRACK_BR_SOURCE, 4, source_br)
    bus.write(TRIGGER_RECORD, 4, 1)
    bus.write(TRIGGER_FLAGS, 4, 0x80)
    callbacks = [run_complete_callback(cpu, base_sp) for _ in range(11)]

    selector = bus.read(PING_PONG_SELECTOR, 4)
    packet_base = PING_PONG_BASE + selector * PING_PONG_STRIDE
    words = [
        bus.read(packet_base + PACKET_SOURCE_OFFSET + 4 * index, 4)
        for index in range(PACKET_WORDS)
    ]
    decoded_counts = Counter(
        (
            bool(word & 0x80000000), (word >> 28) & 7,
            bool(word & 0x08000000), bool(word & 0x04000000),
            (word >> 16) & 0xFF,
        )
        for word in words
    )
    special_words = [
        {"index": index, **decode_pushr(word)}
        for index, word in enumerate(words)
        if ((word >> 16) & 0xFF) != 1 or (word & 0x0C000000)
    ]

    # EOQF marks completion of the prior queue. The stock starter clears it,
    # enables DSPI1 TX FIFO DMA, and arms eDMA channel 15.
    bus.write(FIFO_STATUS, 4, 1 << 28)
    bus.read, bus.write = traced_read, traced_write
    stock_call(cpu, CONTROL_DMA_STARTER, [])
    bus.read, bus.write = original_read, original_write
    if CONTROL_DMA_CHANNEL not in bus.edma_erq:
        raise ValueError("stock starter did not enable eDMA channel 15")
    if not bus._edma_service(CONTROL_DMA_CHANNEL, force=True):
        raise ValueError("DSPI1 transmit request did not service eDMA channel 15")
    event = [x for x in bus.edma_events if x["ch"] == CONTROL_DMA_CHANNEL][-1]

    return {
        "source_br": f"0x{source_br:08X}",
        "case_3_br": f"0x{callbacks[-1]['br_frame_at_renderer'][0]:04X}",
        "dspi1_registers_after_start": {
            "mcr": f"0x{bus.read(DSPI1_MCR, 4):08X}",
            "ctar0": f"0x{bus.read(DSPI1_CTAR0, 4):08X}",
            "sr": f"0x{bus.read(DSPI1_SR, 4):08X}",
            "rser": f"0x{bus.read(DSPI1_RSER, 4):08X}",
        },
        "stock_dspi1_accesses": accesses,
        "packet": {
            "base": f"0x{packet_base:08X}",
            "source": f"0x{packet_base + PACKET_SOURCE_OFFSET:08X}",
            "pushr_words": len(words),
            "command_shapes": [
                {
                    "count": count,
                    "continuous_chip_select": shape[0],
                    "ctar": shape[1],
                    "end_of_queue": shape[2],
                    "clear_transfer_counter": shape[3],
                    "pcs_mask": f"0x{shape[4]:02X}",
                }
                for shape, count in sorted(decoded_counts.items())
            ],
            "special_words": special_words,
            "first": decode_pushr(words[0]),
            "last": decode_pushr(words[-1]),
        },
        "dma": {
            "channel": event["ch"],
            "source": f"0x{event['saddr']:08X}",
            "destination": f"0x{event['daddr']:08X}",
            "minor_bytes": event["nbytes"],
            "iterations": event["citer"],
            "transferred_bytes": event["done_bytes"],
            "request_source": "DSPI1_SR[TFFF]",
        },
    }


def probe(main_path: Path, emulator_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    setup_offset = DSPI1_SETUP - MAIN_LOAD_ADDRESS
    actual_setup = image[setup_offset : setup_offset + len(DSPI1_SETUP_BYTES)]
    if actual_setup != DSPI1_SETUP_BYTES:
        raise ValueError("stock DSPI1 setup signature changed")
    ctar0 = decode_ctar(CTAR_VALUES[0])
    if ctar0 != {
        "raw": "0x7E000001", "frame_bits": 16, "cpol": 1, "cpha": 1,
        "lsb_first": False, "baud_prescaler": 2, "baud_scaler": 4,
        "sck_divider": 8,
    }:
        raise ValueError(f"unexpected CTAR0 decode: {ctar0}")
    module = load_emulator(emulator_path)
    zero = run_vector(module, main_path, 0)
    high = run_vector(module, main_path, 0x7F000000)
    for vector in (zero, high):
        if vector["dma"] != {
            "channel": 15,
            "source": vector["packet"]["source"],
            "destination": "0xFC03C034",
            "minor_bytes": 4,
            "iterations": 510,
            "transferred_bytes": 2040,
            "request_source": "DSPI1_SR[TFFF]",
        }:
            raise ValueError(f"unexpected DSPI1 DMA geometry: {vector['dma']}")
        shapes = vector["packet"]["command_shapes"]
        if sum(item["count"] for item in shapes) != 510:
            raise ValueError(f"unexpected DSPI1 PUSHR word count: {shapes}")
        pcs0_payload = [
            item for item in shapes
            if item["continuous_chip_select"] and item["pcs_mask"] == "0x01"
            and not item["end_of_queue"]
        ]
        if sum(item["count"] for item in pcs0_payload) != 492:
            raise ValueError(f"unexpected DSPI1 PCS0 payload count: {shapes}")
        framing = vector["packet"]["special_words"]
        if (
            framing[0]["index"] != 0 or framing[0]["raw"] != "0x8000AAAA"
            or framing[1]["index"] != 1 or framing[1]["raw"] != "0x84010000"
            or framing[2]["index"] != 493 or framing[2]["raw"] != "0x08005555"
            or [item["index"] for item in framing[3:]] != list(range(494, 510))
            or any(item["raw"] != "0x00000000" for item in framing[3:])
        ):
            raise ValueError(f"unexpected DSPI1 frame boundaries: {framing}")

    return {
        "result": "PASS",
        "main": {"path": str(main_path), "sha256": digest},
        "hardware_identity": {
            "base": "0xFC03C000",
            "peripheral": "MCF5441x DSPI1",
            "status_register": "0xFC03C02C (DSPI1_SR)",
            "transmit_fifo": "0xFC03C034 (DSPI1_PUSHR)",
            "edma_request": "channel 15 = DSPI1_SR[TFFF] transmit FIFO fill",
        },
        "stock_configuration": {
            "setup_address": f"0x{DSPI1_SETUP:08X}",
            "mcr_final": "0x80030C00",
            "ctar": [decode_ctar(value) for value in CTAR_VALUES],
            "rser_initial": "0x00000000",
            "ctar0_wire_mode": "16-bit, CPOL=1, CPHA=1, MSB-first",
            "ctar0_sck": "internal bus clock / 8",
            "pin_mux": {
                "register": "PAR_SDHCL at 0xEC094055",
                "route": "SDHC alternate-function group to DSPI1",
                "pcs0": "PF2 / ball B13 / SDHC_DAT3",
                "sout": "PG7 / ball B12 / SDHC_DAT0",
                "sin": "PG6 / ball C11 / SDHC_CMD",
                "sck": "PG5 / ball A10 / SDHC_CLK",
                "slew_control": "SRCR_SDHC = 2 at 0xEC09406E",
            },
            "reference": "NXP MCF5441x Reference Manual, DSPI and pin-mux chapters",
        },
        "vectors": [zero, high],
        "conclusion": (
            "The BR-bearing DMA image is a 510-entry DSPI1 queue. "
            "Every eDMA longword is a DSPI1_PUSHR command. The frame contains 492 "
            "continuous CTAR0/PCS0 entries (one also clears the transfer counter), "
            "plus a 0xAAAA synchronizer, a 0x5555 end-of-queue marker, and 16 "
            "trailing padding entries. "
            "The off-chip PCS0 device is therefore the immediate control consumer; "
            "the MCF5441x manual alone cannot identify that board-level device."
        ),
        "next_target": (
            "Identify the board-level DSPI1 PCS0 destination and map the 492 payload "
            "positions to analog control fields; independently trace the producers of "
            "the three 0x40117F00 input planes for the Filter 2 boundary."
        ),
        "safety": "Emulation and MMIO observation only; firmware bytes were not modified.",
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
