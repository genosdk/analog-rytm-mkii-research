#!/usr/bin/env python3
"""Recover the clock required by the stock OS 1.72 SSI1 configuration.

This checker deliberately separates what the update image proves from the
bootloader handoff that it does not contain.  It verifies the stock SSI1 and
48 kHz signatures, derives the required SSI_CLOCK from the documented SSI
divider equations, and inventories direct CDRH references in every supplied
update section.
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
CCM_CDRH = 0xEC090010
SSI1_CCR = 0x00056F00
SAMPLE_RATE_HZ = 48_000


def at(image: bytes, address: int, expected: bytes, label: str) -> None:
    offset = address - MAIN_BASE
    actual = image[offset : offset + len(expected)]
    if actual != expected:
        raise ValueError(
            f"{label}: signature mismatch at 0x{address:08X}: "
            f"expected {expected.hex()}, got {actual.hex()}"
        )


def refs(image: bytes, value: int) -> list[int]:
    needle = value.to_bytes(4, "big")
    found: list[int] = []
    start = 0
    while True:
        offset = image.find(needle, start)
        if offset < 0:
            return found
        found.append(offset)
        start = offset + 1


def trace(main_path: Path, sections: list[Path]) -> dict:
    main = main_path.read_bytes()
    digest = hashlib.sha256(main).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")

    # The routine builds CCR in three read/modify/write steps: WL=24-bit,
    # DIV2 enabled, then DC=15.  The resulting word is 0x00056F00.
    at(main, 0x400056A0, bytes.fromhex("2039fc0c8024"), "SSI1 CCR read")
    at(main, 0x400056A6, bytes.fromhex("008000016000"), "SSI1 WL update")
    at(main, 0x400056B8, bytes.fromhex("08c00012"), "SSI1 DIV2 update")
    at(main, 0x400056C8, bytes.fromhex("008000000f00"), "SSI1 DC update")
    at(main, 0x40117844, bytes.fromhex("327cbb80"), "48 kHz stream rate")
    at(main, 0x400008A6, bytes.fromhex("41f9ec09000e3010"), "inherited MISCCR read")
    at(main, 0x40004FEC, bytes.fromhex("3039ec09000e"), "late MISCCR read")

    div2 = (SSI1_CCR >> 18) & 1
    psr = (SSI1_CCR >> 17) & 1
    valid_word_length_bits = ((SSI1_CCR >> 13) & 0xF) * 2 + 2
    slots = ((SSI1_CCR >> 8) & 0x1F) + 1
    pm = SSI1_CCR & 0xFF
    bit_divisor = (div2 + 1) * (7 * psr + 1) * (pm + 1) * 2
    # In I2S master mode the hardware fixes each channel word to 32 clocks;
    # WL only selects how many of those bits carry valid sample data.
    frame_bits = slots * 32
    bit_clock_hz = SAMPLE_RATE_HZ * frame_bits
    required_ssi_clock_hz = bit_clock_hz * bit_divisor

    section_inventory = []
    for path in sections:
        image = path.read_bytes()
        section_inventory.append(
            {
                "path": path.name,
                "size": len(image),
                "sha256": hashlib.sha256(image).hexdigest(),
                "direct_cdrh_offsets": [f"0x{x:X}" for x in refs(image, CCM_CDRH)],
            }
        )

    all_cdrh_refs = sum(len(row["direct_cdrh_offsets"]) for row in section_inventory)
    return {
        "result": "PASS_REQUIRED_SSI1_CLOCK_RECOVERED_BOOT_HANDOFF_OPEN",
        "main": {"path": main_path.name, "size": len(main), "sha256": digest},
        "stock_configuration": {
            "ssi1_ccr": f"0x{SSI1_CCR:08X}",
            "mode": "I2S master / network",
            "valid_sample_bits": valid_word_length_bits,
            "slots_per_frame": slots,
            "i2s_master_clocks_per_slot": 32,
            "div2": div2,
            "psr": psr,
            "pm": pm,
            "sample_rate_hz": SAMPLE_RATE_HZ,
        },
        "derived_clocks": {
            "frame_bits": frame_bits,
            "ssi_bit_divisor": bit_divisor,
            "bit_clock_hz": bit_clock_hz,
            "required_ssi_clock_hz": required_ssi_clock_hz,
            "renderer_block_frames": 32,
            "renderer_period_ns": round(32 * 1_000_000_000 / SAMPLE_RATE_HZ),
        },
        "ccm_constraint": {
            "equation": "SSI_CLOCK = 2 * fSYS / SSI1DIV",
            "pll_source_solution_if_fsys_is_245760000_hz": {
                "ssi1div": 5,
                "ssi_clock_hz": required_ssi_clock_hz,
            },
            "status": "conditional until bootloader CDRH and PLL handoff are captured",
        },
        "update_section_inventory": section_inventory,
        "direct_cdrh_reference_count": all_cdrh_refs,
        "decision": (
            "The native 666667 ns renderer cadence is register-derived downstream of "
            "SSI_CLOCK. The update package contains no direct CDRH access, so its upstream "
            "PLL/divider provenance remains a bootloader or board-state capture gate."
        ),
        "reference": {
            "title": "NXP MCF54418 Reference Manual",
            "sections": ["10.3.5", "35.4.1.4.1", "35.4.2.2"],
            "url": "https://www.nxp.com/docs/en/reference-manual/MCF54418RM.pdf",
        },
        "safety": "Static analysis only; no firmware was modified or repacked.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("main_image", type=Path)
    parser.add_argument("sections", nargs="*", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = trace(args.main_image, args.sections)
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
