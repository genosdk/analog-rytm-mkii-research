#!/usr/bin/env python3
"""Reconstruct the Analog Rytm MKII OS 1.72 stock Bit Reduction quantizer.

Read-only research utility. It accepts either the stock MAIN image or the
known SRR research MAIN image (whose arithmetic between the BR pre-hook and
post-hook remains stock), verifies fixed signatures, extracts the exponential
coefficient table, and reproduces the BR -> D2/D3 quantizer setup exactly.

The per-sample stock quantizer is modeled as:
    q = ((signed_sample * D3) >> 31) << D2
where the right shift corresponds to ColdFire EMAC signed-fractional truncate
mode (MACSR=0x20). D3 is the quantization-density coefficient and D2 restores
the truncated result onto the output grid.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

MAIN_BASE = 0x40000400
STOCK_MAIN_SHA256 = "5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772"
SRR_MAIN_SHA256 = "41bfd182f5f933bbc70fe9033fd157fc938d3ce54a64229b45ec2e1709c8ee67"

BR_DESCRIPTOR = 0x401ABC48
BR_DESCRIPTOR_MAX_WORD = 0x401ABC62
BR_READ = 0x4011870E
MACSR_INIT = 0x40117EF0
SETUP_START = 0x40118714
LOOP_START = 0x4011877A
QUANT_MAC_A = 0x4011878E
QUANT_MAC_B = 0x40118792
TABLE_BASE = 0x40217950

BR_SCALE_Q31 = 0x254A952A
BR_POSITIVE_BIAS = 0x53000000
DESCRIPTOR_MAX = 0x7800


def s32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def at(image: bytes, address: int, expected: bytes, label: str) -> None:
    off = address - MAIN_BASE
    actual = image[off : off + len(expected)]
    if actual != expected:
        raise ValueError(
            f"{label}: signature mismatch at 0x{address:08X}: "
            f"expected {expected.hex()}, got {actual.hex()}"
        )


def u32(image: bytes, address: int) -> int:
    off = address - MAIN_BASE
    return int.from_bytes(image[off : off + 4], "big")


def table_value(image: bytes, signed_offset: int) -> int:
    if signed_offset < -0x2000 or signed_offset > 0x2000 or signed_offset & 3:
        raise ValueError(f"invalid BR coefficient-table offset {signed_offset:+#x}")
    return u32(image, TABLE_BASE + signed_offset)


def q31_mul_positive(a: int, b: int) -> int:
    # Inputs in the BR setup path are non-negative. MACSR=0x20 means signed
    # fractional with R/T=0. MAC.L truncates the 64-bit product and MOVCLR.L
    # truncates the accumulator store, equivalent here to >>31.
    return (a * b) >> 31


def reconstruct_raw(image: bytes, raw_br: int) -> dict:
    if not (0 <= raw_br <= 0xFFFF):
        raise ValueError("raw BR must fit uint16")

    swapped = raw_br << 16
    scaled = q31_mul_positive(BR_SCALE_Q31, swapped)
    exponent_word = scaled + BR_POSITIVE_BIAS if scaled > 0 else 0
    exponent_word &= 0xFFFFFFFF

    d2_pre = s32(exponent_word) >> 26
    frac_index = (s32(exponent_word) >> 13) & 0x1FFC

    # Stock setup:
    #   D1 = -frac
    #   D0 = frac - 0x2000
    #   D4 = table[D0] << 2
    #   D3 = table[D1] << 1
    #   D3 >>= D2_pre
    #   D2 = D2_pre + 1
    d4 = (table_value(image, frac_index - 0x2000) << 2) & 0xFFFFFFFF
    d3_pre = (table_value(image, -frac_index) << 1) & 0xFFFFFFFF
    d3 = s32(d3_pre) >> d2_pre
    d3 &= 0xFFFFFFFF
    d2 = d2_pre + 1

    if d3 == 0:
        effective_levels = 0
        equivalent_bits = float("-inf")
    else:
        # For signed Q31 input, floor(x*D3/2^31) spans approximately
        # [-D3, D3-1], i.e. ~2*D3 quantizer codes.
        effective_levels = 2 * d3
        equivalent_bits = math.log2(effective_levels)

    return {
        "raw_br": raw_br,
        "raw_br_hex": f"0x{raw_br:04X}",
        "high_byte": raw_br >> 8,
        "low_byte": raw_br & 0xFF,
        "scaled_q31": scaled,
        "exponent_word": exponent_word,
        "exponent_word_hex": f"0x{exponent_word:08X}",
        "d2_pre": d2_pre,
        "frac_index": frac_index,
        "frac_index_hex": f"0x{frac_index:04X}",
        "d4": d4,
        "d4_hex": f"0x{d4:08X}",
        "d3": d3,
        "d3_hex": f"0x{d3:08X}",
        "d2": d2,
        "output_quantum": 1 << d2,
        "effective_levels_approx": effective_levels,
        "equivalent_bits_approx": equivalent_bits,
        "quantizer": f"Q(x)=((x*{d3})>>31)<<{d2}",
        "one_bit_sign_region": bool(d3 == 1),
    }


def verify(image: bytes) -> dict:
    digest = hashlib.sha256(image).hexdigest()
    if digest not in (STOCK_MAIN_SHA256, SRR_MAIN_SHA256):
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")

    at(image, MACSR_INIT, bytes.fromhex("a93c00000020"), "MACSR fractional/truncate init")

    if digest == STOCK_MAIN_SHA256:
        at(image, BR_READ, bytes.fromhex("71e900064840"), "stock BR read/SWAP")
    else:
        at(image, BR_READ, bytes.fromhex("4ef9402b4200"), "known SRR BR pre-hook")

    at(image, SETUP_START, bytes.fromhex("243c254a952aa0020800721a780da1c0"), "BR scale setup")
    at(image, 0x4011872E, bytes.fromhex("2400e2a2e8a0028000001ffc22004481048000002000"), "BR exponent decomposition")
    at(image, 0x40118744, bytes.fromhex("45f94021795028320800e58426321800"), "BR coefficient lookups")
    at(image, 0x40118762, bytes.fromhex("e383e4a3"), "D3 scale/shift")
    at(image, LOOP_START, bytes.fromhex("a1c1e5a1a3c6e5a6"), "quantizer restore pair")
    at(image, QUANT_MAC_A, bytes.fromhex("a0430800"), "sample A x D3 MAC")
    at(image, QUANT_MAC_B, bytes.fromhex("ac830800"), "sample B x D3 MAC")

    if u32(image, BR_DESCRIPTOR) != 0x4021FBC6:
        raise ValueError("BR descriptor name pointer changed")
    descriptor_max = int.from_bytes(
        image[BR_DESCRIPTOR_MAX_WORD - MAIN_BASE : BR_DESCRIPTOR_MAX_WORD - MAIN_BASE + 2], "big"
    )
    if descriptor_max != DESCRIPTOR_MAX:
        raise ValueError(f"BR descriptor max changed: 0x{descriptor_max:04X}")

    anchors = {
        "-0x2000": table_value(image, -0x2000),
        "-0x1000": table_value(image, -0x1000),
        "0x0000": table_value(image, 0),
        "+0x1000": table_value(image, 0x1000),
        "+0x2000": table_value(image, 0x2000),
    }
    expected = {
        "-0x2000": 0x10000000,
        "-0x1000": 0x16A09E66,
        "0x0000": 0x20000000,
        "+0x1000": 0x2D413CCD,
        "+0x2000": 0x40000000,
    }
    if anchors != expected:
        raise ValueError(f"BR exponent table anchors changed: {anchors}")

    return {
        "sha256": digest,
        "image_kind": "stock" if digest == STOCK_MAIN_SHA256 else "known_srr_research",
        "descriptor_max": f"0x{descriptor_max:04X}",
        "table_anchors": {k: f"0x{v:08X}" for k, v in anchors.items()},
    }


def compact_ranges(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    out: list[dict] = []
    start = 0
    for i in range(1, len(rows) + 1):
        changed = i == len(rows) or any(rows[i][k] != rows[start][k] for k in keys)
        if changed:
            item = {"start_high_byte": rows[start]["high_byte"], "end_high_byte": rows[i-1]["high_byte"]}
            item.update({k: rows[start][k] for k in keys})
            out.append(item)
            start = i
    return out


def build_report(image: bytes, verification: dict) -> tuple[dict, list[dict]]:
    high_byte_rows = [reconstruct_raw(image, n << 8) for n in range(0x79)]

    raw_pairs = set()
    for raw in range(DESCRIPTOR_MAX + 1):
        r = reconstruct_raw(image, raw)
        raw_pairs.add((r["d2"], r["d3"]))

    d3_one_start = next(r for r in high_byte_rows if r["d3"] == 1)
    shift_ranges = compact_ranges(high_byte_rows, ("d2",))
    quantizer_ranges = compact_ranges(high_byte_rows, ("d2", "d3"))
    diagnostic_127 = reconstruct_raw(image, 0x7F00)

    report = {
        "result": "PASS",
        "verification": verification,
        "stock_quantizer": {
            "macsr": "0x20",
            "mode": "signed fractional; truncate MAC.L product; no accumulator-store rounding",
            "sample_expression": "Q(x) = ((x * D3) >> 31) << D2",
            "negative_shift_semantics": "arithmetic right shift / two's-complement truncation",
            "scale_constant": f"0x{BR_SCALE_Q31:08X}",
            "scale_constant_q31": BR_SCALE_Q31 / (1 << 31),
            "scale_constant_vs_37_over_127": (BR_SCALE_Q31 / (1 << 31)) - (37 / 127),
            "positive_bias": f"0x{BR_POSITIVE_BIAS:08X}",
            "coefficient_table": f"0x{TABLE_BASE:08X}",
        },
        "descriptor_domain": {
            "raw_min": "0x0000",
            "raw_max": f"0x{DESCRIPTOR_MAX:04X}",
            "high_byte_nominal_min": 0,
            "high_byte_nominal_max": DESCRIPTOR_MAX >> 8,
            "high_byte_rows": len(high_byte_rows),
            "distinct_high_byte_quantizers": len({(r["d2"], r["d3"]) for r in high_byte_rows}),
            "distinct_quantizers_full_raw_sweep": len(raw_pairs),
            "one_bit_region_starts_high_byte": d3_one_start["high_byte"],
            "one_bit_region_starts_raw": d3_one_start["raw_br_hex"],
        },
        "selected_points": [
            high_byte_rows[n] for n in (0, 1, 8, 16, 32, 48, 64, 80, 96, 104, 112, 114, 120)
        ],
        "d2_shift_ranges": shift_ranges,
        "final_quantizer_ranges": quantizer_ranges,
        "external_ui_mapping_status": {
            "documented_front_panel_range": "0..127",
            "descriptor_field_max": "0x7800",
            "status": "UNRESOLVED",
            "warning": (
                "Do not assume front-panel 0..127 maps directly to raw high byte 0..127. "
                "The stock descriptor stores 0x7800 while published manuals label BR 0..127. "
                "The parameter conversion path must be traced before assigning exact front-panel values."
            ),
            "diagnostic_only_direct_ui_127_as_0x7F00": diagnostic_127,
        },
        "interpretation": {
            "br_zero": (
                "D3=2^30 and D2=1, mathematically clearing one 32-bit LSB. "
                "Whether this is bit-identical for the actual sample representation remains a separate proof."
            ),
            "br_positive": (
                "The first positive high-byte band jumps to roughly ten effective signed levels-bits, "
                "then resolution falls monotonically in discrete integer-coefficient steps."
            ),
            "max_descriptor": (
                "At 0x7800 the reconstructed core is D3=1,D2=30: a one-bit/sign-like truncation core."
            ),
        },
        "safety": "Static reconstruction only; no firmware bytes were modified or repacked.",
    }
    return report, high_byte_rows


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = [
        "high_byte", "raw_br_hex", "d2_pre", "frac_index_hex", "d3", "d3_hex", "d2",
        "output_quantum", "effective_levels_approx", "equivalent_bits_approx", "d4_hex",
        "exponent_word_hex", "quantizer", "one_bit_sign_region",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row[k] for k in fields})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("main_image", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--csv", type=Path)
    args = ap.parse_args()

    image = args.main_image.read_bytes()
    verification = verify(image)
    report, rows = build_report(image, verification)

    encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.json:
        args.json.write_text(encoded, encoding="utf-8")
    if args.csv:
        write_csv(args.csv, rows)
    print(encoded, end="")


if __name__ == "__main__":
    main()
