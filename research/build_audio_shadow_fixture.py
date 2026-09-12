#!/usr/bin/env python3
"""Build a non-proprietary ColdFire fixture for ar_audio_shadow.c."""

from __future__ import annotations

import argparse
from pathlib import Path

BASE = 0x40000000
START = 0x40000020
END = 0x4000002F
EXIT = 0x4000000C
DATA = 0x40000100
STACK = 0x47FFFFE0


def build() -> bytes:
    image = bytearray(0x104)
    caller = bytes.fromhex(
        "2E7C47FFFFE0"      # movea.l #STACK,a7
        "4EB940000020"      # loop: jsr START
        "60F8"              # bra.s loop; EXIT is this instruction
    )
    kernel = bytes.fromhex(
        "203940000100"      # move.l DATA,d0
        "5280"              # addq.l #1,d0
        "23C040000100"      # move.l d0,DATA
        "4E75"              # rts
    )
    image[: len(caller)] = caller
    image[START - BASE : START - BASE + len(kernel)] = kernel
    return bytes(image)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    payload = build()
    args.output.write_bytes(payload)
    print(f"wrote {args.output} ({len(payload)} bytes)")


if __name__ == "__main__":
    main()
