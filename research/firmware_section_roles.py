#!/usr/bin/env python3
"""Conservatively classify the non-MAIN payloads in AR MKII OS 1.72.

This probe exists to prevent a dangerous architectural mistake: section ID 2
is executable ColdFire code, but it is the temporary upgrade/bootstrap image,
not evidence of a second runtime audio processor. Section ID 1 has the framing
of a 16-bit FPGA configuration stream and contains no ColdFire RTS opcodes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


EXPECTED = {
    2: (42302, "9b7ed34c1ce6ff5c2842b581f00140a864b30b185a128c61ef784a23db301dbc"),
    1: (149516, "7c8bff3cb411ed93434b3a4eab846738be6241d64aeb23b4061eb11a8aa29b2f"),
}


def checked(path: Path, section_id: int) -> bytes:
    data = path.read_bytes()
    size, digest = EXPECTED[section_id]
    actual = hashlib.sha256(data).hexdigest()
    if len(data) != size or actual != digest:
        raise ValueError(
            f"unexpected section ID {section_id}: size={len(data)}, sha256={actual}"
        )
    return data


def offset(data: bytes, value: bytes, label: str) -> str:
    found = data.find(value)
    if found < 0:
        raise ValueError(f"missing {label}")
    return f"0x{found:X}"


def probe(section_id_2: Path, section_id_1: Path) -> dict:
    bootstrap = checked(section_id_2, 2)
    fpga = checked(section_id_1, 1)

    declared_body = int.from_bytes(bootstrap[:4], "big")
    if declared_body != len(bootstrap) - 8:
        raise ValueError("bootstrap inner length does not cover file minus 8-byte header")
    if fpga[:32] != b"\xFF" * 32 or fpga[32:36] != bytes.fromhex("AA9930A1"):
        raise ValueError("section ID 1 lacks the expected configuration preamble")
    if bytes.fromhex("4E75") in fpga:
        raise ValueError("section ID 1 unexpectedly contains a ColdFire RTS word")

    bootstrap_strings = {
        text.decode("ascii"): offset(bootstrap, text, text.decode("ascii"))
        for text in (
            b"BOOTSTRAP UPGRADE",
            b"DO NOT TURN OFF!",
            b"UPGRADE FAILED",
            b"CRC CHECK",
            b"VERSION CHECK",
            b"LENGTH ERROR",
            b"FPGA:(%c) %s",
            b"AUDIO:(+)",
        )
    }
    return {
        "result": "PASS",
        "section_id_2": {
            "path": str(section_id_2),
            "size": len(bootstrap),
            "sha256": EXPECTED[2][1],
            "inner_declared_body_bytes": declared_body,
            "header_word_1": f"0x{int.from_bytes(bootstrap[4:8], 'big'):08X}",
            "diagnostic_strings": bootstrap_strings,
            "classification": "temporary ColdFire bootstrap/updater and service UI",
            "rejected_role": "runtime sample DSP/audio engine",
        },
        "section_id_1": {
            "path": str(section_id_1),
            "size": len(fpga),
            "sha256": EXPECTED[1][1],
            "preamble_ff_bytes": 32,
            "configuration_sync_and_first_packet": fpga[32:36].hex().upper(),
            "coldfire_rts_word_count": fpga.count(bytes.fromhex("4E75")),
            "classification": "16-bit FPGA configuration stream",
        },
        "architecture_correction": (
            "The live BR quantizer and sample renderer must not be assigned to "
            "section ID 2. Continue from MAIN's active render state and treat "
            "section ID 1 as programmable-logic configuration, not CPU code."
        ),
        "safety": "Static inspection only; no firmware bytes were modified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("section_id_2", type=Path)
    parser.add_argument("section_id_1", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = probe(args.section_id_2, args.section_id_1)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
