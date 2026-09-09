#!/usr/bin/env python3
"""Decode bonded XC3S200A/VQ100 IOB directions in the stock AR MKII FPGA image.

Read-only.  Coordinates are transcribed from Project Combine's Spartan-3A
BOND57 database and IOB_S3A_{W4,E4,S2,N2} tile classes.  The physical rectangle
placement follows ExpandedDevice::tile_bits() / btile_term_{h,v}().
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
from collections import Counter
from pathlib import Path

EXPECTED_SHA256 = "7c8bff3cb411ed93434b3a4eab846738be6241d64aeb23b4061eb11a8aa29b2f"
FDRI_OFFSET = 124
FRAME_BYTES = 276
FRAME_BITS = 2208
PHYSICAL_FRAMES = 540
PROJECT_COMBINE_COMMIT = "234343d23e737e57f2727630e19008b509d7d522"

# VQ100 package pin -> Project Combine EdgeIoCoord.  Non-user pins are omitted.
BOND57 = [
    (3,"W",32,1),(4,"W",32,0),(5,"W",30,1),(6,"W",30,0),(7,"W",29,0),
    (9,"W",19,1),(10,"W",19,0),(12,"W",18,1),(13,"W",18,0),
    (15,"W",15,1),(16,"W",15,0),(19,"W",2,1),(20,"W",2,0),(21,"W",1,1),
    (23,"S",1,0),(24,"S",2,0),(25,"S",1,1),(27,"S",2,1),
    (28,"S",3,0),(29,"S",3,1),(30,"S",5,0),(31,"S",5,1),
    (32,"S",6,0),(33,"S",6,1),(34,"S",8,0),(35,"S",8,1),
    (36,"S",10,0),(37,"S",10,1),(39,"S",11,2),(40,"S",12,0),
    (41,"S",12,1),(43,"S",13,0),(44,"S",13,1),(46,"S",16,1),
    (48,"S",20,0),(49,"S",20,1),(50,"S",22,0),(51,"S",24,0),
    (52,"S",22,1),(53,"S",24,1),(56,"E",3,0),(57,"E",3,1),
    (59,"E",14,0),(60,"E",14,1),(61,"E",15,0),(62,"E",15,1),
    (64,"E",18,0),(65,"E",18,1),(68,"E",28,0),(70,"E",29,0),
    (71,"E",29,1),(72,"E",31,0),(73,"E",31,1),(77,"N",23,1),
    (78,"N",23,0),(82,"N",13,2),(83,"N",14,1),(84,"N",14,0),
    (85,"N",13,1),(86,"N",13,0),(88,"N",12,1),(89,"N",12,0),
    (90,"N",11,0),(93,"N",5,1),(94,"N",5,0),(97,"N",1,2),
    (98,"N",1,1),(99,"N",1,0),
]

# Each coordinate is (TERM rectangle, database frame, database bit).
# Missing OUTPUT_ENABLE means the physical position is input-only.
IOB_BITS = {
 "W": {
  0: (((0,0,13),(0,0,12),(0,0,11)),()),
  1: (((0,0,27),(0,0,30),(0,0,31)),()),
  2: (((1,0,6),(0,0,63),(0,0,62)),((0,0,56),(0,0,55))),
  3: (((1,0,21),(1,0,24),(1,0,25)),((1,0,32),(1,0,30))),
  4: (((2,1,7),(2,0,7),(2,0,6)),((2,0,0),(1,0,63))),
  5: (((2,0,29),(2,0,33),(2,0,34)),((2,0,40),(2,0,39))),
  6: (((3,0,18),(3,0,14),(3,1,14)),((3,0,8),(3,0,7))),
  7: (((3,0,38),(3,0,42),(3,0,41)),((3,1,46),(3,0,48))),
 },
 "E": {
  0: (((3,0,50),(3,0,51),(3,0,52)),()),
  1: (((3,0,36),(3,0,33),(3,0,32)),()),
  2: (((2,0,57),(3,0,0),(3,0,1)),((3,0,8),(3,0,7))),
  3: (((2,0,42),(2,0,39),(2,0,38)),((2,0,33),(2,0,31))),
  4: (((1,1,56),(1,0,56),(1,0,57)),((2,0,0),(1,0,63))),
  5: (((1,0,34),(1,0,30),(1,0,29)),((1,0,24),(1,0,23))),
  6: (((0,0,45),(0,0,49),(0,1,49)),((0,0,56),(0,0,55))),
  7: (((0,0,25),(0,0,21),(0,0,22)),((0,1,17),(0,0,15))),
 },
 "S": {
  0: (((1,5,2),(1,6,5),(1,6,4)),((1,1,1),(1,1,0))),
  1: (((1,10,3),(1,13,5),(1,13,4)),((1,12,1),(1,12,0))),
  2: (((0,18,1),(0,18,2),(0,18,3)),()),
  3: (((0,6,0),(0,5,4),(0,5,3)),((0,4,2),(0,4,1))),
  4: (((0,13,5),(0,14,2),(0,14,3)),((0,15,5),(0,15,1))),
 },
 "N": {
  0: (((0,12,1),(0,14,3),(0,14,1)),((0,16,1),(0,15,4))),
  1: (((0,5,4),(0,4,3),(0,4,1)),((0,3,1),(0,2,4))),
  2: (((1,18,3),(1,18,2),(1,18,1)),()),
  3: (((1,9,1),(1,11,4),(1,9,4)),((1,14,2),(1,12,4))),
  4: (((1,8,5),(1,5,5),(1,13,4)),((1,2,2),(1,1,5))),
 },
}

IBUF_MODES = {
    0: "NONE", 1: "LOOPBACK_T", 2: "LOOPBACK_O", 3: "CMOS_VCCINT",
    4: "CMOS_VCCO", 5: "VREF", 6: "DIFF", 7: "CMOS_VCCAUX",
}

# fill_frame_info() followed by the BRAM fix-up in Project Combine.
NON_BRAM_COLUMNS = [0,1,2,7,8,9,10,11,12,13,14,15,16,17,18,23,24,25]
COLUMN_FRAMES = {col: 6 + 19 * i for i, col in enumerate(NON_BRAM_COLUMNS)}
COLUMN_FRAMES.update({3:502, 4:350, 5:369, 6:388, 19:521, 20:426, 21:445, 22:464})

GCLK = {
    ("S",13,0): "GCLK0", ("S",13,1): "GCLK1",
    ("S",14,0): "GCLK2", ("S",14,1): "GCLK3",
    ("S",11,0): "GCLK4", ("S",11,1): "GCLK5",
    ("S",12,0): "GCLK6", ("S",12,1): "GCLK7",
}

SHARED_CONFIG = {
    ("S",1,0): "M1", ("S",2,0): "M2", ("S",1,1): "M0",
    ("S",2,1): "CSO_B", ("S",3,0): "RDWR_B", ("S",8,0): "D7",
    ("S",8,1): "D6", ("S",10,0): "D5", ("S",10,1): "D4",
    ("S",12,0): "D3", ("S",12,1): "D2", ("S",16,1): "CSI_B",
    ("S",20,0): "INIT_B", ("S",20,1): "D3", ("S",22,0): "D2",
    ("S",22,1): "D1", ("S",24,0): "D0", ("S",24,1): "CCLK",
    ("N",1,0): "HSWAP_EN",
}


def frame_bits(block: bytes) -> list[int]:
    """Mirror insert_spartan3a_frame(): BE u16, Lsb0, reverse word order."""
    if len(block) != FRAME_BYTES:
        raise ValueError("bad frame size")
    out = [0] * FRAME_BITS
    for i in range(138):
        word = int.from_bytes(block[2*i:2*i+2], "big")
        target = FRAME_BITS - (i + 1) * 16
        for j in range(16):
            out[target+j] = (word >> j) & 1
    return out


def load_frames(path: Path) -> tuple[list[list[int]], str]:
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != EXPECTED_SHA256:
        raise ValueError(f"unexpected FPGA SHA-256: {digest}")
    payload = data[FDRI_OFFSET:FDRI_OFFSET + 541 * FRAME_BYTES]
    blocks = [payload[i*FRAME_BYTES:(i+1)*FRAME_BYTES] for i in range(541)]
    if len(blocks) != 541 or any(blocks[-1]):
        raise ValueError("unexpected FDRI framing/pad")
    return [frame_bits(x) for x in blocks[:PHYSICAL_FRAMES]], digest


def bel_for(side: str, coordinate: int, iob: int) -> int:
    if side == "W":
        return 2 * ((coordinate - 1) % 4) + iob
    if side == "E":
        return 7 - 2 * ((coordinate - 1) % 4) - iob
    cell = (coordinate - 1) % 2
    if side == "S":
        return {(0,0):4, (0,1):3, (0,2):2, (1,0):1, (1,1):0}[(cell,iob)]
    return {(0,0):1, (0,1):0, (0,2):2, (1,0):3, (1,1):4}[(cell,iob)]


def absolute_bit(side: str, coordinate: int, db: tuple[int,int,int]) -> tuple[int,int]:
    term, db_frame, db_bit = db
    if side in "WE":
        anchor = coordinate - ((coordinate - 1) % 4)
        frame = (4 if side == "W" else 348) + (1 - db_frame)
        bit = 16 + 64 * (anchor + term) + (63 - db_bit)
    else:
        anchor = coordinate - ((coordinate - 1) % 2)
        frame = COLUMN_FRAMES[anchor + term] + (18 - db_frame)
        bit = (0 if side == "S" else 2192) + (5 - db_bit)
    return frame, bit


def decode_vector(frames: list[list[int]], side: str, coordinate: int,
                  coords: tuple[tuple[int,int,int], ...]) -> tuple[int, list[dict]]:
    value = 0
    evidence = []
    for index, db in enumerate(coords):
        frame, bit = absolute_bit(side, coordinate, db)
        state = frames[frame][bit]
        value |= state << index
        evidence.append({"vector_bit":index, "frame":frame, "bit":bit, "value":state})
    return value, evidence


def classify(ibuf: int, oe: int | None) -> str:
    if oe is None:
        return "input-only" if ibuf else "unused-input-only"
    if ibuf and oe:
        return "bidirectional"
    if ibuf:
        return "input"
    if oe:
        return "output"
    return "unused"


def candidate_clusters(rows: list[dict], limit: int | None = 20) -> list[dict]:
    inputs = [r for r in rows if r["input_enabled"]]
    outputs = [r for r in rows if r["output_enabled"]]
    candidates = []
    for output in outputs:
        for trio in itertools.combinations((r for r in inputs if r is not output), 3):
            quartet = sorted((*trio, output), key=lambda r: r["package_pin"])
            sides = {r["side"] for r in quartet}
            pins = [r["package_pin"] for r in quartet]
            package_span = max(pins) - min(pins)
            same_side = len(sides) == 1
            coordinate_span = (max(r["coordinate"] for r in quartet) -
                               min(r["coordinate"] for r in quartet)) if same_side else None
            gclk_inputs = [r["clock_capability"] for r in trio if r["clock_capability"]]
            # Package locality dominates; a clock-capable FPGA input is a useful tie-breaker.
            score = (package_span * 10 + (coordinate_span or 0) +
                     (len(sides)-1) * 100 - min(len(gclk_inputs), 1) * 8)
            candidates.append({
                "rank_score": score,
                "sin_output": f"P{output['package_pin']} / {output['edge_iob']}",
                "mcu_to_fpga_inputs_unassigned": [f"P{r['package_pin']} / {r['edge_iob']}" for r in trio],
                "package_pins": pins,
                "package_span": package_span,
                "sides": sorted(sides),
                "edge_coordinate_span": coordinate_span,
                "clock_capable_inputs": gclk_inputs,
            })
    candidates.sort(key=lambda x: (x["rank_score"], x["package_span"], x["package_pins"]))
    selected = candidates if limit is None else candidates[:limit]
    for rank, candidate in enumerate(selected, 1):
        candidate["rank"] = rank
    return selected


def inventory(path: Path) -> dict:
    frames, digest = load_frames(path)
    rows = []
    for package_pin, side, coordinate, iob in BOND57:
        bel = bel_for(side, coordinate, iob)
        ibuf_coords, oe_coords = IOB_BITS[side][bel]
        ibuf, ibuf_evidence = decode_vector(frames, side, coordinate, ibuf_coords)
        oe, oe_evidence = (decode_vector(frames, side, coordinate, oe_coords)
                           if oe_coords else (None, []))
        row = {
            "package_pin": package_pin,
            "edge_iob": f"IOB_{side}{coordinate}_{iob}",
            "side": side,
            "coordinate": coordinate,
            "iob_index": iob,
            "internal_bel": f"IOB[{bel}]",
            "physical_kind": "input-only" if not oe_coords else "iob",
            "ibuf_mode_value": ibuf,
            "ibuf_mode": IBUF_MODES[ibuf],
            "output_enable_value": oe,
            "input_enabled": ibuf != 0,
            "output_enabled": oe not in (None, 0),
            "direction": classify(ibuf, oe),
            "clock_capability": GCLK.get((side,coordinate,iob)),
            "shared_config": SHARED_CONFIG.get((side,coordinate,iob)),
            "ibuf_evidence": ibuf_evidence,
            "output_enable_evidence": oe_evidence,
        }
        rows.append(row)
    illegal = [r for r in rows if r["ibuf_mode_value"] not in IBUF_MODES]
    if illegal:
        raise AssertionError(f"illegal IBUF modes: {illegal}")
    candidates = candidate_clusters(rows)
    all_candidates = candidate_clusters(rows, None)
    best_clock_candidate = next(x for x in all_candidates if x["clock_capable_inputs"])
    return {
        "result": "PASS",
        "fpga": {"path": str(path), "sha256": digest},
        "device": {"part":"XC3S200A", "package":"VQ100", "bond":"BOND57"},
        "coordinate_source": {
            "project": "Project Combine",
            "commit": PROJECT_COMBINE_COMMIT,
            "database": "databases/spartan3.txt",
            "placement": "public/virtex2/src/expanded.rs",
            "logical_iob_mapping": "public/virtex2/src/iob.rs",
        },
        "geometry": {
            "west": "term_w_frame=4; four Vertical(rev 2, rev 64) rectangles",
            "east": "term_e_frame=348; four Vertical(rev 2, rev 64) rectangles",
            "south": "owning column frame; two Vertical(rev 19, rev 6) rectangles at bit 0",
            "north": "owning column frame; two Vertical(rev 19, rev 6) rectangles at bit 2192",
            "bram_column_fixups": {str(k):v for k,v in COLUMN_FRAMES.items() if k in (3,4,5,6,19,20,21,22)},
        },
        "summary": {
            "bonded_user_pins": len(rows),
            "directions": dict(sorted(Counter(r["direction"] for r in rows).items())),
            "ibuf_modes": dict(sorted(Counter(r["ibuf_mode"] for r in rows).items())),
            "output_enable_values": dict(sorted(Counter(str(r["output_enable_value"]) for r in rows).items())),
            "ibuf_enum_is_exhaustive": True,
        },
        "inventory": rows,
        "dspi_candidate_clusters": candidates,
        "best_clock_capable_dspi_candidate": best_clock_candidate,
        "interpretation": (
            "The strongest package-local 3-input + 1-output match is P28-P31: "
            "P28/P30/P31 are FPGA inputs and P29 is bidirectional/output-enabled. "
            "This identifies the quartet but does not assign PCS0/SCK/SOUT among the three inputs. "
            "The best clock-capable alternative is centered on P41/P43/P44 and is less package-local."
        ),
        "safety": "Read-only stock-bitstream extraction; no FPGA or firmware bits are modified.",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("fpga_image", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--csv", type=Path)
    args = ap.parse_args()
    result = inventory(args.fpga_image)
    if args.json:
        args.json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if args.csv:
        fields = [
            "package_pin","edge_iob","side","coordinate","iob_index","internal_bel",
            "physical_kind","ibuf_mode_value","ibuf_mode","output_enable_value",
            "input_enabled","output_enabled","direction","clock_capability","shared_config",
        ]
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows({k:r[k] for k in fields} for r in result["inventory"])
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
