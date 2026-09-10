#!/usr/bin/env python3
"""Verify stock OS 1.72 audio-interrupt and buffer-stream landmarks.

This conservative, read-only checker proves fixed instruction bytes, table
geometry, and direct absolute call sites. It does not infer that generic
streams 0x81/0x82 are playback streams and cannot modify or repack firmware.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

MAIN_BASE = 0x40000400
EXPECTED_MAIN_SHA256 = (
    "5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772"
)
STREAM_TABLE = 0x41310D30
STREAM_RECORD_BYTES = 0x10
MAX_STREAM_INDEX = 0x82


def at(image: bytes, address: int, expected: bytes, label: str) -> None:
    offset = address - MAIN_BASE
    actual = image[offset : offset + len(expected)]
    if actual != expected:
        raise ValueError(
            f"{label}: signature mismatch at 0x{address:08X}: "
            f"expected {expected.hex()}, got {actual.hex()}"
        )


def absolute_refs(image: bytes, value: int) -> list[str]:
    """Return image addresses containing an exact big-endian 32-bit value."""
    needle = value.to_bytes(4, "big")
    result: list[str] = []
    start = 0
    while True:
        offset = image.find(needle, start)
        if offset < 0:
            return result
        result.append(f"0x{MAIN_BASE + offset:08X}")
        start = offset + 1


def trace(path: Path) -> dict:
    image = path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")

    # SSI1 is the hardware-facing audio clock owner. It is configured for
    # synchronous network mode, 16 slots per frame, 24-bit words, and both
    # receive/transmit DMA. Channel 54 drains one 2048-byte output block to
    # SSI1_TX0 and raises its major-loop interrupt.
    at(image, 0x4000567E, bytes.fromhex("7038"), "SSI1 control seed")
    at(image, 0x40005680, bytes.fromhex("223c0000008b"), "SSI1 transmit configuration")
    at(image, 0x40005686, bytes.fromhex("23c0fc0c8010"), "SSI1 control write")
    at(image, 0x400056D4, bytes.fromhex("23c1fc0c8018"), "SSI1 DMA enable")
    at(image, 0x400057A4, bytes.fromhex("203cfc0c8000"), "eDMA54 SSI1_TX0 destination")
    at(image, 0x400057AA, bytes.fromhex("223c80000920"), "eDMA54 SRAM source")
    at(image, 0x400057C4, bytes.fromhex("303c6a02"), "eDMA54 transfer attributes")
    at(image, 0x400057D0, bytes.fromhex("33c0fc0456c4"), "eDMA54 attributes write")
    at(image, 0x400057D6, bytes.fromhex("203c80000020"), "eDMA54 32-byte minor loop")
    at(image, 0x400057EA, bytes.fromhex("7040"), "eDMA54 64 iterations")
    at(image, 0x400057EC, bytes.fromhex("33c1fc0456d6"), "eDMA54 fixed destination")
    at(image, 0x400057FC, bytes.fromhex("33c0fc0456dc"), "eDMA54 major count")
    at(image, 0x40005802, bytes.fromhex("33c1fc0456de"), "eDMA54 major interrupt")
    at(image, 0x40005812, bytes.fromhex("13c1fc044018"), "eDMA54 request enable")
    at(image, 0x40005822, bytes.fromhex("13c1fc04c06e"), "eDMA54 interrupt priority")

    # eDMA interrupt handler: sample DTIM2, acknowledge interrupt source 54.
    at(image, 0x40118AF2, bytes.fromhex("4feffff448d70103"), "audio ISR prologue")
    at(image, 0x40118B00, bytes.fromhex("2239fc07800c"), "DTIM2 counter read")
    at(image, 0x40118B2A, bytes.fromhex("13c1fc04401c"), "eDMA CINT write")
    at(image, 0x40118B30, bytes.fromhex("8190"), "eDMA channel 54 immediate")
    at(image, 0x40118B3A, bytes.fromhex("4e73"), "audio ISR return")

    # Callback DMA observations and renderer/control dispatch.
    at(image, 0x4011B3C4, bytes.fromhex("2039fc0456c0"), "TCD observation A")
    at(image, 0x4011B3D6, bytes.fromhex("2039fc045690"), "TCD observation B")
    at(image, 0x4011CACC, bytes.fromhex("4eb940117f00"), "pre-render call")
    at(image, 0x4011CAE2, bytes.fromhex("4eb94010a2e0"), "renderer call")
    at(image, 0x4011CAEE, bytes.fromhex("4eb940108944"), "voice bridge call")
    at(image, 0x4011CAFA, bytes.fromhex("4eb940105188"), "control conversion call")

    # Generic stream descriptor: indices 0..0x82, 16-byte records, four
    # 32-bit fields. The default branch installs 48 kHz and 0x40000000.
    at(image, 0x401177E2, bytes.fromhex("0c8200000082626c"), "stream index bound")
    at(image, 0x401177EA, bytes.fromhex("e98a"), "stream index times 16")
    at(image, 0x401177EC, bytes.fromhex("41f941310d30"), "stream field +0")
    at(image, 0x401177FC, bytes.fromhex("47f941310d3c"), "stream field +12")
    at(image, 0x4011780C, bytes.fromhex("41f941310d38"), "stream field +8")
    at(image, 0x40117844, bytes.fromhex("327cbb80"), "48 kHz default")
    at(image, 0x40117848, bytes.fromhex("203c40000000"), "default ratio")

    setup_refs = absolute_refs(image, 0x401177D2)
    reset_refs = absolute_refs(image, 0x401178BA)
    expected_setup = [
        "0x4009A2C8", "0x4009A944", "0x4009B102", "0x4009B2D2", "0x40124AC4"
    ]
    expected_reset = [
        "0x4009A250", "0x4009A5B8", "0x4009A88A", "0x4009A9CE",
        "0x4009B178", "0x40124AA4"
    ]
    if setup_refs != expected_setup:
        raise ValueError(f"unexpected stream setup references: {setup_refs}")
    if reset_refs != expected_reset:
        raise ValueError(f"unexpected stream reset references: {reset_refs}")

    # Stream 0x82 is explicitly reset/configured with 0xBB80. This proves
    # buffer setup, not whether it carries recording, transfer, or playback.
    at(image, 0x40124A9A, bytes.fromhex("48780082"), "stream 0x82 reset index")
    at(image, 0x40124AA2, bytes.fromhex("4eb9401178ba"), "stream 0x82 reset call")
    at(image, 0x40124AA8, bytes.fromhex("2f3c0000bb80"), "stream 0x82 rate")
    at(image, 0x40124ABE, bytes.fromhex("487800824eb9401177d2"), "stream 0x82 setup")

    # Per-voice conversion reaches record words 36..38 and a runtime table.
    # BR is record word 39, so this function does not read BR directly.
    at(image, 0x401051D8, bytes.fromhex("d9fc8000f7ea"), "record word 33 base")
    at(image, 0x4010535C, bytes.fromhex("302c0006"), "record word 36 read")
    at(image, 0x4010520C, bytes.fromhex("382c0008"), "record word 37 read")
    at(image, 0x40105254, bytes.fromhex("2c3c8000f7f4"), "record word 38 base")
    at(image, 0x4010516E, bytes.fromhex("41f941310b20"), "runtime table")

    return {
        "result": "PASS",
        "main": {"path": str(path), "size": len(image), "sha256": digest,
                 "base_address": f"0x{MAIN_BASE:08X}"},
        "audio_tick": {
            "isr": "0x40118AF2", "timer_read": "0xFC07800C",
            "edma_clear_interrupt_register": "0xFC04401C", "edma_channel": 54,
            "callback": "0x4011B3AE",
            "observed_tcd_addresses": ["0xFC0456C0", "0xFC045690"],
            "render_sequence": ["0x40117F00", "0x4010A2E0", "0x40108944", "0x40105188"],
        },
        "audio_clock_provenance": {
            "peripheral": "SSI1",
            "ssi1_tx_register": "0xFC0C8000",
            "ssi1_mode": "synchronous network audio",
            "ssi1_dma_enable_word": "0x00500000",
            "edma_channel": 54,
            "source_base": "0x80000920",
            "minor_loop_bytes": 32,
            "major_loop_iterations": 64,
            "major_loop_bytes": 2048,
            "major_interrupt": True,
            "completion_isr": "0x40118AF2",
            "software_forced_source": 63,
            "renderer_vector": 191,
            "ownership": "SSI1 TX FIFO demand clocks eDMA54; its stock ISR forces the renderer service.",
        },
        "generic_stream_descriptors": {
            "setup": "0x401177D2", "reset": "0x401178BA",
            "table": f"0x{STREAM_TABLE:08X}", "record_bytes": STREAM_RECORD_BYTES,
            "record_count": MAX_STREAM_INDEX + 1, "valid_index_range": "0x00..0x82",
            "field_offsets": ["+0x0", "+0x4", "+0x8", "+0xC"],
            "default_rate": 48000, "default_ratio_word": "0x40000000",
            "setup_call_sites": [f"0x{int(x, 16) - 2:08X}" for x in setup_refs],
            "reset_call_sites": [f"0x{int(x, 16) - 2:08X}" for x in reset_refs],
            "high_index_users": ["0x81", "0x82"],
            "classification": "buffer-stream family; playback/record direction unproven",
        },
        "per_voice_control": {
            "converter": "0x40105188", "record_stride_bytes": "0x54",
            "direct_record_words": [36, 37, 38], "bit_reduction_record_word": 39,
            "bit_reduction_read_directly_here": False,
            "runtime_table": "0x41310B20", "runtime_table_helper": "0x4010515A",
        },
        "insertion_point_status": {
            "filter2": "not yet proven",
            "next_proof": "resolve BR descriptor destinations and trace their consumer to the sample quantizer/DAC path",
            "unsafe_assumption_rejected": "streams 0x81/0x82 are not sample playback absent direction/consumer proof",
        },
        "safety": "Static analysis only; no firmware was modified or repacked.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("main_image", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = trace(args.main_image)
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
