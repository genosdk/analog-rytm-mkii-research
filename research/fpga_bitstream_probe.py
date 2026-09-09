#!/usr/bin/env python3
"""Inspect the Analog Rytm MKII OS 1.72 FPGA configuration image.

Read-only. The section is a 16-bit Xilinx Spartan-3-generation configuration
stream, not executable ColdFire code. This probe validates its stock hash,
decodes the pre-FDRI command sequence, identifies the array IDCODE, and splits
the FDRI payload into XC3S200A frame-sized blocks for later bit-level research.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

EXPECTED_SHA256 = "7c8bff3cb411ed93434b3a4eab846738be6241d64aeb23b4061eb11a8aa29b2f"
SYNC = bytes.fromhex("AA9930A1")
XC3S200A_IDCODE = 0x02218093
XC3S200A_FRAME_BITS = 2208
XC3S200A_FRAME_WORDS = XC3S200A_FRAME_BITS // 16
XC3S200A_FRAMES = 540

REG_NAMES = {
    0: "CRC", 1: "FAR_MAJ", 2: "FAR_MIN", 3: "FDRI", 4: "FDRO", 5: "CMD",
    6: "CTL", 7: "MASK", 8: "STAT", 9: "LOUT", 10: "COR1", 11: "COR2",
    12: "PWRDN_REG", 13: "FLR", 14: "IDCODE", 16: "HC_OPT_REG",
    19: "GENERAL1_REG", 20: "GENERAL2_REG", 21: "MODE_REG",
    22: "PU_GWE", 23: "PU_GTS", 25: "CCLK_FREQ", 26: "SEU_OPT_REG",
    27: "EXP_SIGN_REG",
}


def words16(data: bytes):
    if len(data) % 2:
        raise ValueError("FPGA section length is not 16-bit aligned")
    return [int.from_bytes(data[i:i+2], "big") for i in range(0, len(data), 2)]


def decode_type1(word: int) -> dict | None:
    if (word >> 13) != 0b001:
        return None
    opcode = (word >> 11) & 0x3
    reg = (word >> 5) & 0x3F
    count = word & 0x1F
    return {
        "header": f"0x{word:04X}",
        "opcode": {0: "NOP", 1: "READ", 2: "WRITE", 3: "RESERVED"}[opcode],
        "register": reg,
        "register_name": REG_NAMES.get(reg, f"REG_{reg}"),
        "word_count": count,
    }


def frame_summary(frame: bytes, index: int) -> dict:
    return {
        "index": index,
        "sha256": hashlib.sha256(frame).hexdigest(),
        "population_count": sum(byte.bit_count() for byte in frame),
        "all_zero": not any(frame),
    }


def probe(path: Path) -> dict:
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != EXPECTED_SHA256:
        raise ValueError(f"unexpected FPGA SHA-256: {digest}")
    if data[:32] != b"\xff" * 32:
        raise ValueError("expected 32-byte 0xff preamble")
    if data[32:36] != SYNC:
        raise ValueError(f"unexpected sync/start bytes {data[32:36].hex()}")

    w = words16(data)
    pos = 16
    if w[pos:pos+2] != [0xAA99, 0x30A1]:
        raise ValueError("sync sequence moved")

    packets = []
    idcode = None
    fdri = None
    pos += 2
    while pos < len(w):
        header = w[pos]
        t1 = decode_type1(header)
        if t1 is None:
            pos += 1
            continue
        pos += 1
        count = t1["word_count"]
        payload = w[pos:pos+count]
        pos += count
        t1["data"] = [f"0x{x:04X}" for x in payload]
        packets.append(t1)
        if t1["register_name"] == "IDCODE" and count == 2:
            idcode = (payload[0] << 16) | payload[1]

        if pos + 2 < len(w) and w[pos] in (0x5060, 0x5062):
            type2_header = w[pos]
            wc = (w[pos+1] << 16) | w[pos+2]
            data_word_start = pos + 3
            data_word_end = data_word_start + wc
            if data_word_end > len(w):
                raise ValueError("FDRI word count exceeds section")
            fdri = {
                "type2_header": f"0x{type2_header:04X}",
                "word_count_16bit": wc,
                "byte_offset": data_word_start * 2,
                "byte_length": wc * 2,
                "end_byte_offset": data_word_end * 2,
            }
            pos = data_word_end
            break

    if idcode != XC3S200A_IDCODE:
        raise ValueError(f"unexpected IDCODE 0x{idcode:08X}")
    if fdri is None:
        raise ValueError("FDRI Type-2 payload not found")
    if fdri["word_count_16bit"] % XC3S200A_FRAME_WORDS:
        raise ValueError("FDRI payload is not frame-word aligned")

    payload = data[fdri["byte_offset"]:fdri["end_byte_offset"]]
    block_count = fdri["word_count_16bit"] // XC3S200A_FRAME_WORDS
    frames = [
        payload[i * XC3S200A_FRAME_WORDS * 2:(i + 1) * XC3S200A_FRAME_WORDS * 2]
        for i in range(block_count)
    ]
    summaries = [frame_summary(frame, i) for i, frame in enumerate(frames)]
    zero_indices = [x["index"] for x in summaries if x["all_zero"]]
    trailing = data[fdri["end_byte_offset"]:]

    return {
        "result": "PASS",
        "fpga": {"path": str(path), "size": len(data), "sha256": digest},
        "format": {
            "bus_width": 16,
            "preamble_ff_bytes": 32,
            "sync_and_first_packet": data[32:36].hex().upper(),
            "array_idcode": f"0x{idcode:08X}",
            "identified_device": "Xilinx XC3S200A Spartan-3A",
        },
        "pre_fdri_type1_packets": packets,
        "fdri": {
            **fdri,
            "device_frame_bits": XC3S200A_FRAME_BITS,
            "device_frame_words_16bit": XC3S200A_FRAME_WORDS,
            "device_configuration_frames": XC3S200A_FRAMES,
            "transmitted_frame_sized_blocks": block_count,
            "extra_frame_sized_blocks_vs_device": block_count - XC3S200A_FRAMES,
            "last_block_all_zero": summaries[-1]["all_zero"],
            "all_zero_block_count": len(zero_indices),
            "all_zero_block_indices": zero_indices,
            "first_five": summaries[:5],
            "last_five": summaries[-5:],
        },
        "trailing_configuration_bytes": {
            "length": len(trailing),
            "hex": trailing.hex().upper(),
        },
        "interpretation": (
            "The OS payload is a complete XC3S200A configuration stream. The FDRI block "
            "contains 541 frame-sized chunks for a 540-frame device; the final chunk is all zero, "
            "consistent with a frame-sized pad/dummy block. A single bitstream does not by itself "
            "identify which configuration bits implement the BR command decoder."
        ),
        "next_target": (
            "Use Spartan-3A bitstream databases/tooling (Project Combine spartan3 database) to "
            "map FDRI bits to tiles/PIPs/LUTs, then trace the DSPI command receiver around the "
            "BR control field. Hardware BR audio characterization remains the independent oracle."
        ),
        "safety": "Read-only FPGA bitstream inspection; no configuration bits are modified.",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("fpga_image", type=Path)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    result = probe(args.fpga_image)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
