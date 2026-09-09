#!/usr/bin/env python3
"""Decode XC3S200A CLB LUT contents from the AR MKII OS 1.72 FPGA image.

Read-only.  The frame loader and tile geometry follow Project Combine's
Spartan-3A parser/database.  No FPGA bits are modified.
"""
from __future__ import annotations
import argparse, csv, hashlib, json
from collections import Counter
from pathlib import Path

EXPECTED = "7c8bff3cb411ed93434b3a4eab846738be6241d64aeb23b4061eb11a8aa29b2f"
FDRI_OFFSET = 124
FRAME_BYTES = 276
FRAME_BITS = 2208
PHYSICAL_FRAMES = 540
LOGIC_COLS = [1,2,7,8,9,10,11,12,13,14,15,16,17,18,23,24]
LUTS = {
    (0,"F"):(0,0), (0,"G"):(0,16),
    (1,"F"):(3,0), (1,"G"):(3,16),
    (2,"F"):(0,32), (2,"G"):(0,48),
    (3,"F"):(3,32), (3,"G"):(3,48),
}

def col_base_frames():
    cols = [0,1,2,7,8,9,10,11,12,13,14,15,16,17,18,23,24,25]
    return {col: 6 + 19*i for i, col in enumerate(cols)}

def is_real_clb(col: int, row: int) -> bool:
    # Project Combine fill_dcm(): Spartan-3A Dcms::Four creates two 8x4 holes
    # around the central clock column.  512 nominal positions - 64 = 448 CLBs.
    return not (9 <= col <= 16 and (1 <= row <= 4 or 29 <= row <= 32))

def frame_bits(block: bytes):
    if len(block) != FRAME_BYTES:
        raise ValueError("bad frame size")
    out = [0] * FRAME_BITS
    # Project Combine insert_spartan3a_frame(): BE u16, Lsb0, loaded from
    # the high end of the 2208-bit frame toward bit zero.
    for i in range(138):
        word = int.from_bytes(block[2*i:2*i+2], "big")
        tgt = FRAME_BITS - (i + 1) * 16
        for j in range(16):
            out[tgt+j] = (word >> j) & 1
    return out

def load_frames(path: Path):
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != EXPECTED:
        raise ValueError(f"unexpected FPGA SHA-256: {digest}")
    payload = data[FDRI_OFFSET:FDRI_OFFSET + 541*FRAME_BYTES]
    blocks = [payload[i*FRAME_BYTES:(i+1)*FRAME_BYTES] for i in range(541)]
    if len(blocks) != 541 or any(blocks[-1]):
        raise ValueError("unexpected FDRI framing/pad")
    return [frame_bits(x) for x in blocks[:PHYSICAL_FRAMES]], digest

def xlat(db_frame: int, db_bit: int, mode: str):
    if mode == "project_combine_rev": return 18-db_frame, 63-db_bit
    if mode == "identity": return db_frame, db_bit
    if mode == "frame_rev": return 18-db_frame, db_bit
    if mode == "bit_rev": return db_frame, 63-db_bit
    raise ValueError(mode)

def inventory(path: Path, mode: str):
    frames, digest = load_frames(path)
    bases = col_base_frames()
    rows = []
    for col in LOGIC_COLS:
        for row in range(1, 33):
            if not is_real_clb(col, row):
                continue
            base_frame = bases[col]
            base_bit = 16 + 64*row
            for (sl, name), (df, db0) in LUTS.items():
                value = 0
                first = None
                for k in range(16):
                    rf, rb = xlat(df, db0+k, mode)
                    af, ab = base_frame+rf, base_bit+rb
                    if first is None: first = (af, ab)
                    # Project Combine CLB database marks every F/G LUT bit !MAIN.
                    logical = 1 - frames[af][ab]
                    value |= logical << k
                rows.append({
                    "col": col, "row": row, "slice": sl, "lut": name,
                    "value": value, "hex": f"0x{value:04X}",
                    "constant": value in (0, 0xFFFF),
                    "frame_first": first[0], "bit_first": first[1],
                })
    count = Counter(r["value"] for r in rows)
    return rows, {
        "mode": mode,
        "sha256": digest,
        "clb_cells": sum(is_real_clb(c,r) for c in LOGIC_COLS for r in range(1,33)),
        "luts": len(rows),
        "unique_lut_values": len(count),
        "constant_luts": count[0] + count[0xFFFF],
        "constant_fraction": (count[0] + count[0xFFFF]) / len(rows),
        "top_values": [{"hex":f"0x{k:04X}","count":v} for k,v in count.most_common(20)],
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fpga_image", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--csv", type=Path)
    args = ap.parse_args()
    comparisons = []
    selected_rows = None
    selected = None
    for mode in ["project_combine_rev", "identity", "frame_rev", "bit_rev"]:
        rows, summary = inventory(args.fpga_image, mode)
        comparisons.append(summary)
        if mode == "project_combine_rev":
            selected_rows, selected = rows, summary
    result = {
        "result": "PASS",
        "transform_selected": "project_combine_rev",
        "transform_basis": {
            "tile_geometry": "Vertical (rev 19, rev 64)",
            "db_to_rect": "raw_frame=18-db_frame; raw_bit=63-db_bit",
            "rect_to_absolute": "absolute_frame=column_frame+raw_frame; absolute_bit=16+64*row+raw_bit",
            "frame_loader": "Project Combine insert_spartan3a_frame: BE u16/Lsb0/reverse word order",
        },
        "candidate_comparison": comparisons,
        "inventory": selected,
        "safety": "Read-only FPGA decode; no configuration bits are modified.",
    }
    if args.json:
        args.json.write_text(json.dumps(result, indent=2) + "\n")
    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(selected_rows[0]))
            w.writeheader(); w.writerows(selected_rows)
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
