#!/usr/bin/env python3
"""Reconstruct the Analog Rytm MKII OS 1.72 stock Bit Reduction quantizer.

Read-only. Verifies fixed stock arithmetic signatures, extracts the stock
exponential coefficient table, and reproduces the BR -> D2/D3 setup exactly.
It accepts either stock MAIN or the known SRR research MAIN; that SRR build
changes the BR read and post-loop control flow but leaves the arithmetic modeled
here stock.

For a signed 32-bit sample x, the stock quantizer core is:
    Q(x) = ((x * D3) >> 31) << D2
under ColdFire EMAC MACSR=0x20 (signed fractional, truncate, no store rounding).
"""
from __future__ import annotations

import argparse, csv, hashlib, json, math
from pathlib import Path

MAIN_BASE = 0x40000400
STOCK_MAIN_SHA256 = "5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772"
SRR_MAIN_SHA256 = "41bfd182f5f933bbc70fe9033fd157fc938d3ce54a64229b45ec2e1709c8ee67"
BR_DESCRIPTOR = 0x401ABC48
BR_DESCRIPTOR_FIELD_18_WORD = 0x401ABC62
BR_READ = 0x4011870E
MACSR_INIT = 0x40117EF0
TABLE_BASE = 0x40217950
BR_SCALE_Q31 = 0x254A952A
BR_POSITIVE_BIAS = 0x53000000
DESCRIPTOR_FIELD_18 = 0x7800
UI_MAX = 127
UI_RAW_MAX = UI_MAX << 8


def s32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def at(image: bytes, address: int, expected: bytes, label: str) -> None:
    off = address - MAIN_BASE
    actual = image[off:off + len(expected)]
    if actual != expected:
        raise ValueError(
            f"{label}: signature mismatch at 0x{address:08X}: "
            f"expected {expected.hex()}, got {actual.hex()}"
        )


def u32(image: bytes, address: int) -> int:
    off = address - MAIN_BASE
    return int.from_bytes(image[off:off + 4], "big")


def table_value(image: bytes, signed_offset: int) -> int:
    if not (-0x2000 <= signed_offset <= 0x2000) or signed_offset & 3:
        raise ValueError(f"invalid coefficient-table offset {signed_offset:+#x}")
    return u32(image, TABLE_BASE + signed_offset)


def q31_mul_positive(a: int, b: int) -> int:
    # This BR setup multiply has non-negative operands. With MACSR=0x20,
    # MAC.L + MOVCLR.L is equivalent here to truncating a signed Q31 product.
    return (a * b) >> 31


def reconstruct_raw(image: bytes, raw_br: int) -> dict:
    if not (0 <= raw_br <= 0xFFFF):
        raise ValueError("raw BR must fit uint16")

    # Stock 0x4011870E: MVZ.W 6(A1),D0; SWAP D0.
    swapped = raw_br << 16
    scaled = q31_mul_positive(BR_SCALE_Q31, swapped)
    exponent_word = (scaled + BR_POSITIVE_BIAS if scaled > 0 else 0) & 0xFFFFFFFF

    # Stock 0x4011872E..0x40118764.
    d2_pre = s32(exponent_word) >> 26
    frac_index = (s32(exponent_word) >> 13) & 0x1FFC
    d4 = (table_value(image, frac_index - 0x2000) << 2) & 0xFFFFFFFF
    d3_pre = (table_value(image, -frac_index) << 1) & 0xFFFFFFFF
    d3 = (s32(d3_pre) >> d2_pre) & 0xFFFFFFFF
    d2 = d2_pre + 1

    effective_levels = 2 * d3 if d3 else 0
    equivalent_bits = math.log2(effective_levels) if effective_levels else float("-inf")
    quantizer_gain = d3 * (1 << d2) / (1 << 31)
    d4_gain = d4 / (1 << 31)
    # BR=0 has D4=0.5. Express D4*quantizer scaling relative to that baseline.
    combined_rel = 2.0 * d4_gain * quantizer_gain

    n = raw_br >> 8
    if (raw_br & 0xFF) == 0 and 1 <= n <= 127:
        ideal_exponent = 20.75 + 37 * n / (4 * 127)
        ideal_bits = 31.0 - ideal_exponent
    elif raw_br == 0:
        ideal_exponent, ideal_bits = 0.0, 31.0
    else:
        ideal_exponent = ideal_bits = None

    return {
        "raw_br": raw_br,
        "raw_br_hex": f"0x{raw_br:04X}",
        "high_byte": n,
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
        "ideal_exponent": ideal_exponent,
        "ideal_equivalent_bits": ideal_bits,
        "quantizer_linear_gain": quantizer_gain,
        "d4_gain_scale": d4_gain,
        "combined_gain_relative_to_br0": combined_rel,
        "combined_gain_db_relative_to_br0": 20 * math.log10(combined_rel) if combined_rel > 0 else None,
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

    at(image, 0x40118714, bytes.fromhex("243c254a952aa0020800721a780da1c0"), "BR scale setup")
    at(image, 0x4011872E, bytes.fromhex("2400e2a2e8a0028000001ffc22004481048000002000"), "BR exponent decomposition")
    at(image, 0x40118744, bytes.fromhex("45f94021795028320800e58426321800"), "BR coefficient lookups")
    at(image, 0x40118762, bytes.fromhex("e383e4a3"), "D3 scale/shift")
    at(image, 0x4011877A, bytes.fromhex("a1c1e5a1a3c6e5a6"), "quantizer restore pair")
    at(image, 0x4011878E, bytes.fromhex("a0430800"), "sample A x D3 MAC")
    at(image, 0x40118792, bytes.fromhex("ac830800"), "sample B x D3 MAC")

    if u32(image, BR_DESCRIPTOR) != 0x4021FBC6:
        raise ValueError("BR descriptor name pointer changed")
    field18 = int.from_bytes(
        image[BR_DESCRIPTOR_FIELD_18_WORD - MAIN_BASE:BR_DESCRIPTOR_FIELD_18_WORD - MAIN_BASE + 2], "big"
    )
    if field18 != DESCRIPTOR_FIELD_18:
        raise ValueError(f"BR descriptor +0x18 field changed: 0x{field18:04X}")

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
        raise ValueError(f"coefficient-table anchors changed: {anchors}")

    return {
        "sha256": digest,
        "image_kind": "stock" if digest == STOCK_MAIN_SHA256 else "known_srr_research",
        "descriptor_plus_0x18_field": f"0x{field18:04X}",
        "table_anchors": {k: f"0x{v:08X}" for k, v in anchors.items()},
    }


def compact_ranges(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    out, start = [], 0
    for i in range(1, len(rows) + 1):
        changed = i == len(rows) or any(rows[i][k] != rows[start][k] for k in keys)
        if changed:
            item = {"start_ui": rows[start]["high_byte"], "end_ui": rows[i - 1]["high_byte"]}
            item.update({k: rows[start][k] for k in keys})
            out.append(item)
            start = i
    return out


def build_report(image: bytes, verification: dict) -> tuple[dict, list[dict]]:
    ui_rows = [reconstruct_raw(image, n << 8) for n in range(UI_MAX + 1)]
    raw_pairs = {
        (reconstruct_raw(image, raw)["d2"], reconstruct_raw(image, raw)["d3"])
        for raw in range(UI_RAW_MAX + 1)
    }
    one_bit_start = next(r for r in ui_rows if r["d3"] == 1)

    report = {
        "result": "PASS",
        "verification": verification,
        "stock_quantizer": {
            "macsr": "0x20",
            "mode": "signed fractional; truncate MAC.L product; no accumulator-store rounding",
            "sample_expression": "Q(x) = ((x * D3) >> 31) << D2",
            "scale_constant": f"0x{BR_SCALE_Q31:08X}",
            "scale_constant_q31": BR_SCALE_Q31 / (1 << 31),
            "target_ratio": 37 / 127,
            "error_vs_37_over_127": BR_SCALE_Q31 / (1 << 31) - 37 / 127,
            "positive_bias": f"0x{BR_POSITIVE_BIAS:08X}",
            "coefficient_table": f"0x{TABLE_BASE:08X}",
        },
        "ui_and_runtime_domain": {
            "ui_min": 0,
            "ui_max": UI_MAX,
            "storage_encoding": "sample_br uint8 followed by unused zero byte -> natural 16-bit word BR<<8",
            "raw_ui_min": "0x0000",
            "raw_ui_max": f"0x{UI_RAW_MAX:04X}",
            "mapping_confidence": "HIGH",
            "mapping_evidence": [
                "FW1.70 sound storage uses uint8 sample_br followed by an unused zero pad",
                "FW1.70 pattern p-lock BR domain is 0..127 while START is separately 0..120",
                "0x254A952A ~= Q31(37/127); BR=127 encoded as 0x7F00 yields exponent 0x77FFFFFF, one truncation LSB below the designed 0x78000000 endpoint",
            ],
            "descriptor_plus_0x18_field": f"0x{DESCRIPTOR_FIELD_18:04X}",
            "descriptor_field_interpretation": "UNKNOWN; previous label as BR internal maximum is superseded",
            "distinct_ui_core_quantizers": len({(r["d2"], r["d3"]) for r in ui_rows}),
            "distinct_core_quantizers_raw_sweep_0_to_0x7F00": len(raw_pairs),
            "one_bit_core_starts_ui": one_bit_start["high_byte"],
            "one_bit_core_starts_raw": one_bit_start["raw_br_hex"],
        },
        "design_equation": {
            "for_ui_n_1_to_127": "S(n) = 20.75 + 37*n/(4*127)",
            "ideal_bit_density": "B_ideal(n) = 31-S(n) = 10.25 - 37*n/(4*127)",
            "endpoint": "n=127 -> S=30 -> 1 ideal bit",
            "integer_effect": "D3 is integer-truncated, so actual code-count resolution becomes coarser than the ideal curve near the top end",
        },
        "selected_points": [
            ui_rows[n] for n in (0, 1, 8, 16, 32, 48, 64, 80, 96, 104, 112, 114, 120, 127)
        ],
        "d2_shift_ranges": compact_ranges(ui_rows, ("d2",)),
        "core_quantizer_ranges": compact_ranges(ui_rows, ("d2", "d3")),
        "interpretation": {
            "br_zero": "D3=2^30,D2=1: mathematically a one-LSB-clear on 32-bit data; actual sample alignment/bypass equivalence remains to be proven",
            "first_positive": "UI 1 immediately moves to about 10.175 effective code-count bits",
            "one_bit_region": "The D3/D2 core reaches D3=1,D2=30 at UI 114; D4 continues changing through UI 127",
            "top_endpoint": "UI 127/raw 0x7F00 is explicitly engineered to end at a one-bit/sign-like truncation core with D4 compensation nearly back at BR=0 baseline gain",
        },
        "safety": "Static reconstruction only; no firmware bytes were modified or repacked.",
    }
    return report, ui_rows


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = [
        "high_byte", "raw_br_hex", "d2_pre", "frac_index_hex", "d3", "d3_hex", "d2",
        "output_quantum", "effective_levels_approx", "equivalent_bits_approx",
        "ideal_equivalent_bits", "d4_hex", "quantizer_linear_gain", "d4_gain_scale",
        "combined_gain_relative_to_br0", "combined_gain_db_relative_to_br0",
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
    report, rows = build_report(image, verify(image))
    encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.json:
        args.json.write_text(encoded, encoding="utf-8")
    if args.csv:
        write_csv(args.csv, rows)
    print(encoded, end="")


if __name__ == "__main__":
    main()
