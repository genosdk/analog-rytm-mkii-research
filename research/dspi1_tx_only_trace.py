#!/usr/bin/env python3
"""Prove that the stock BR/control DSPI1 transport is transmit-only in software.

Read-only.  The checker validates the eDMA15 destination and every literal
DSPI1 POPR reference in the decompressed OS 1.72 MAIN image.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


MAIN_BASE = 0x40000400
EXPECTED_MAIN_SHA256 = "5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772"
DSPI1_PUSHR = 0xFC03C034
DSPI1_POPR = 0xFC03C038
EDMA15_TCD_DADDR = 0xFC0451F0


def hx(value: int) -> str:
    return f"0x{value:08X}"


def at(image: bytes, address: int, expected: bytes, label: str) -> None:
    offset = address - MAIN_BASE
    actual = image[offset : offset + len(expected)]
    if actual != expected:
        raise ValueError(
            f"{label}: signature mismatch at {hx(address)}: "
            f"expected {expected.hex()}, got {actual.hex()}"
        )


def literal_references(image: bytes, value: int) -> list[int]:
    needle = value.to_bytes(4, "big")
    result = []
    start = 0
    while True:
        offset = image.find(needle, start)
        if offset < 0:
            return result
        result.append(MAIN_BASE + offset)
        start = offset + 1


def trace(main_path: Path) -> dict:
    image = main_path.read_bytes()
    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")

    # MOVE.L #DSPI1_PUSHR,D0; MOVE.L D0,TCD15_DADDR
    at(
        image,
        0x40077C5E,
        bytes.fromhex("203cfc03c03423c0fc0451f0"),
        "eDMA15 DSPI1 destination",
    )

    # The two initialization helpers drain four and three receive words.
    # Every MOVE.L POPR,D0 overwrites the preceding value; the helpers then return.
    at(
        image,
        0x40005514,
        bytes.fromhex(
            "2039fc03c0382039fc03c0382039fc03c0382039fc03c0384e75"
        ),
        "four-word POPR drain",
    )
    at(
        image,
        0x40005576,
        bytes.fromhex("2039fc03c0382039fc03c0382039fc03c0384e75"),
        "three-word POPR drain",
    )

    # The runtime helper loads D1=16, reads POPR into D0, decrements D1, and
    # loops until zero.  D0 is not inspected or stored.
    at(
        image,
        0x40077DF4,
        bytes.fromhex("72102039fc03c038538166f64e75"),
        "sixteen-word runtime POPR drain",
    )

    refs = literal_references(image, DSPI1_POPR)
    expected_refs = [
        0x40005516,
        0x4000551C,
        0x40005522,
        0x40005528,
        0x40005578,
        0x4000557E,
        0x40005584,
        0x40077DF8,
    ]
    if refs != expected_refs:
        raise ValueError(f"unexpected DSPI1 POPR reference set: {[hx(x) for x in refs]}")
    for literal in refs:
        # 0x2039 is MOVE.L (absolute long),D0 on ColdFire/m68k.
        at(image, literal - 2, bytes.fromhex("2039"), "POPR load-to-D0 opcode")

    return {
        "result": "PASS_DSPI1_BR_TRANSPORT_TX_ONLY_IN_SOFTWARE",
        "main": {
            "kind": "locally extracted stock OS 1.72 MAIN; not committed",
            "sha256": digest,
            "base": hx(MAIN_BASE),
        },
        "transmit_path": {
            "edma_channel": 15,
            "tcd_daddr": hx(EDMA15_TCD_DADDR),
            "destination": hx(DSPI1_PUSHR),
            "signature": hx(0x40077C5E),
        },
        "receive_reads": {
            "register": hx(DSPI1_POPR),
            "literal_reference_count": len(refs),
            "literal_addresses": [hx(x) for x in refs],
            "destinations": ["D0"] * len(refs),
            "drain_idioms": [
                {"address": hx(0x40005514), "words": 4, "terminator": "RTS"},
                {"address": hx(0x40005576), "words": 3, "terminator": "RTS"},
                {
                    "address": hx(0x40077DF4),
                    "words": 16,
                    "loop": "MOVEQ #16,D1; MOVE.L POPR,D0; SUBQ.L #1,D1; BNE",
                },
            ],
            "classification": (
                "FIFO draining only: every literal POPR access loads D0; the values are "
                "overwritten, discarded, or left as the final drain value."
            ),
        },
        "conclusion": (
            "The BR/control data path proves three required MCU-to-FPGA wires: PCS0, SCK, "
            "and SOUT. DSPI1 SIN may still be physically bonded, but no received value is "
            "consumed by the proven BR transport, so a live FPGA-to-MCU return is not a "
            "requirement for identifying the ingress cluster."
        ),
        "next_target": (
            "Rank and continue-route bonded three-input clusters without requiring a colocated "
            "FPGA output; retain SIN only as an optional board-continuity question."
        ),
        "safety": "Static read-only analysis; no firmware or FPGA bytes are modified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("main_image", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    result = trace(args.main_image)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.json:
        args.json.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
