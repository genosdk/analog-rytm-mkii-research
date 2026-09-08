#!/usr/bin/env python3
"""Reconstruct the AR MKII OS 1.72 stock BR audio render block.

Read-only static/model utility. It verifies the machine-code landmarks around
0x401186B2..0x401187A4 and builds two independent integer models of the
32-sample post-BR render block:

  source -> BR Q31 truncation -> binary shift restoration
         -> BR-dependent D4 amplitude compensation
         -> 32-sample sample-level ramp -> output

The block model is exact conditional on its two incoming sample-level ramp
states (previous and current). The upstream logic that derives the current
state is also documented and signature-checked, but some voice/status gating
conditions remain semantically unlabeled.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

MAIN_BASE = 0x40000400
STOCK_MAIN_SHA256 = "5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772"
SRR_MAIN_SHA256 = "41bfd182f5f933bbc70fe9033fd157fc938d3ce54a64229b45ec2e1709c8ee67"

TABLE_BASE = 0x40217950
BR_SCALE_Q31 = 0x254A952A
BR_POSITIVE_BIAS = 0x53000000
UI_MAX = 127


def u32(v: int) -> int:
    return v & 0xFFFFFFFF


def s32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def asl32(v: int, n: int) -> int:
    if n >= 32:
        return 0
    return s32(u32(v) << n)


def asr32(v: int, n: int) -> int:
    if n >= 32:
        return -1 if s32(v) < 0 else 0
    return s32(v) >> n


def q31_trunc(a: int, b: int) -> int:
    """ColdFire MACSR=0x20 signed-fractional MAC.L+MOVCLR with zero ACC."""
    return s32((s32(a) * s32(b)) >> 31)


def q31_reference_alignment(a: int, b: int) -> int:
    """Independent EMAC fractional-alignment formulation."""
    prod2 = s32(a) * s32(b) * 2
    return s32(prod2 >> 32)


def at(image: bytes, address: int, expected: bytes, label: str) -> None:
    off = address - MAIN_BASE
    got = image[off:off + len(expected)]
    if got != expected:
        raise ValueError(
            f"{label}: signature mismatch at 0x{address:08X}: "
            f"expected {expected.hex()}, got {got.hex()}"
        )


def read_u32(image: bytes, address: int) -> int:
    off = address - MAIN_BASE
    return int.from_bytes(image[off:off + 4], "big")


def table_value(image: bytes, signed_offset: int) -> int:
    if not (-0x2000 <= signed_offset <= 0x2000) or signed_offset & 3:
        raise ValueError(f"invalid table offset {signed_offset:+#x}")
    return read_u32(image, TABLE_BASE + signed_offset)


def br_coefficients(image: bytes, ui_br: int) -> dict:
    if not 0 <= ui_br <= UI_MAX:
        raise ValueError("BR UI value must be 0..127")
    raw_br = ui_br << 8
    swapped = raw_br << 16
    scaled = (BR_SCALE_Q31 * swapped) >> 31
    exponent = u32(scaled + BR_POSITIVE_BIAS if scaled > 0 else 0)
    d2_pre = s32(exponent) >> 26
    frac_index = (s32(exponent) >> 13) & 0x1FFC
    d4 = u32(table_value(image, frac_index - 0x2000) << 2)
    d3_pre = u32(table_value(image, -frac_index) << 1)
    d3 = u32(s32(d3_pre) >> d2_pre)
    d2 = d2_pre + 1
    return {
        "ui_br": ui_br,
        "raw_br": raw_br,
        "raw_br_hex": f"0x{raw_br:04X}",
        "exponent_hex": f"0x{exponent:08X}",
        "frac_index_hex": f"0x{frac_index:04X}",
        "d2_pre": d2_pre,
        "d2": d2,
        "d3": d3,
        "d3_hex": f"0x{d3:08X}",
        "d4": d4,
        "d4_hex": f"0x{d4:08X}",
    }


def quantize_one(sample: int, d3: int, d2: int) -> int:
    return asl32(q31_trunc(sample, d3), d2)


def instruction_order_block(samples: list[int], d3: int, d2: int, d4: int, ramp_prev: int, ramp_next: int) -> dict:
    if len(samples) < 34:
        raise ValueError("instruction-order model requires >=34 source samples")
    d7 = q31_trunc(ramp_prev, d4)
    d5_scaled = q31_trunc(ramp_next, d4)
    d5 = asr32(s32(d5_scaled - d7), 5)
    acc0 = q31_trunc(samples[0], d3)
    acc1 = q31_trunc(samples[1], d3)
    source_i = 2
    out, ramp_used, quantized = [], [], []
    for _ in range(16):
        q0 = asl32(acc0, d2)
        q1 = asl32(acc1, d2)
        ramp_used.append(d7); quantized.append(q0)
        y0 = q31_trunc(d7, q0)
        d7 = s32(d7 + d5)
        ramp_used.append(d7); quantized.append(q1)
        y1 = q31_trunc(d7, q1)
        d7 = s32(d7 + d5)
        acc0 = q31_trunc(samples[source_i], d3)
        acc1 = q31_trunc(samples[source_i + 1], d3)
        source_i += 2
        out.extend((y0, y1))
    return {
        "output": out,
        "quantized": quantized,
        "ramp_used": ramp_used,
        "ramp_start_scaled": q31_trunc(ramp_prev, d4),
        "ramp_target_scaled": d5_scaled,
        "ramp_step": d5,
        "ramp_after_32": d7,
        "prefetched_next_pair_q31": [acc0, acc1],
        "accumulators_clear_on_exit": True,
    }


def closed_form_block(samples: list[int], d3: int, d2: int, d4: int, ramp_prev: int, ramp_next: int) -> dict:
    if len(samples) < 32:
        raise ValueError("closed-form model requires >=32 source samples")
    r0 = q31_trunc(ramp_prev, d4)
    r1 = q31_trunc(ramp_next, d4)
    step = asr32(s32(r1 - r0), 5)
    out, ramps, quantized = [], [], []
    r = r0
    for x in samples[:32]:
        q = quantize_one(x, d3, d2)
        quantized.append(q); ramps.append(r)
        out.append(q31_trunc(q, r))
        r = s32(r + step)
    return {
        "output": out,
        "quantized": quantized,
        "ramp_used": ramps,
        "ramp_start_scaled": r0,
        "ramp_target_scaled": r1,
        "ramp_step": step,
        "ramp_after_32": r,
    }


def verify_signatures(image: bytes) -> dict:
    digest = hashlib.sha256(image).hexdigest()
    if digest not in (STOCK_MAIN_SHA256, SRR_MAIN_SHA256):
        raise ValueError(f"unexpected MAIN SHA-256 {digest}")
    at(image, 0x40117EF0, bytes.fromhex("a93c00000020"), "MACSR=0x20")
    at(image, 0x401186B2, bytes.fromhex("2e2b0010"), "load previous ramp state A3+0x10")
    at(image, 0x401186C6, bytes.fromhex("d3fc8000f776"), "runtime record base")
    at(image, 0x401186CC, bytes.fromhex("7b69000e"), "load current sample-level word A1+0x0E")
    at(image, 0x401186E6, bytes.fromhex("4c055800da85"), "square level and double")
    at(image, 0x401186EC, bytes.fromhex("4a2b0029"), "voice/status gate")
    at(image, 0x401186F2, bytes.fromhex("4a29000c66042a07"), "sample-subview gate / hold previous")
    at(image, 0x401186FA, bytes.fromhex("e485202f005827450010"), "scale and persist current ramp state")
    at(image, 0x40118704, bytes.fromhex("ef880680800067f82c40"), "select 0x80-byte output block")
    at(image, 0x40118714, bytes.fromhex("243c254a952aa0020800721a780d"), "BR exponential scale")
    at(image, 0x40118744, bytes.fromhex("45f94021795028320800e58426321800"), "BR D4/D3 coefficient lookup")
    at(image, 0x40118754, bytes.fromhex("45f98000e1d0"), "source buffer 0x8000E1D0")
    at(image, 0x4011875A, bytes.fromhex("a8070800a05a4805"), "D4 ramp-endpoint MAC pair")
    at(image, 0x40118762, bytes.fromhex("e383e4a3a1c7a3c59a87ea85"), "D3 scale and 32-step ramp setup")
    at(image, 0x4011876E, bytes.fromhex("ac9a8803ac83080052827010"), "first source-pair prequantize")
    at(image, 0x4011877A, bytes.fromhex("a1c1e5a1a3c6e5a6"), "restore quantized pair")
    at(image, 0x40118782, bytes.fromhex("a0da1817de85ac1a6817de85"), "post-BR multiplicative ramp pair")
    at(image, 0x4011878E, bytes.fromhex("a0430800ac830800"), "next source-pair prequantize")
    at(image, 0x40118796, bytes.fromhex("a5c82cc8a7c82cc8538066d8"), "emit pair / 16-iteration loop")
    at(image, 0x401187A2, bytes.fromhex("a1c8a3c6"), "clear prefetched ACC0/ACC1 at exit")
    return {
        "sha256": digest,
        "image_kind": "stock" if digest == STOCK_MAIN_SHA256 else "known_srr_research",
        "macsr": "0x20",
        "render_source": "0x8000E1D0",
        "render_output_block_bytes": "0x80",
        "render_samples": 32,
        "render_pair_iterations": 16,
    }


def randomized_tests(image: bytes, cases: int, seed: int) -> dict:
    rng = random.Random(seed)
    pairs = [(0,0),(1,1),(-1,-1),(-0x80000000,-0x80000000),(-0x80000000,0x7fffffff),(0x7fffffff,0x7fffffff)]
    pairs += [(rng.randint(-0x80000000,0x7fffffff), rng.randint(-0x80000000,0x7fffffff)) for _ in range(cases)]
    for a,b in pairs:
        if q31_trunc(a,b) != q31_reference_alignment(a,b):
            raise AssertionError(f"Q31 mismatch for {a},{b}")
    for _ in range(cases):
        br = rng.randrange(128)
        c = br_coefficients(image, br)
        samples = [rng.randint(-0x80000000,0x7fffffff) for _ in range(34)]
        rprev = rng.randint(-0x80000000,0x7fffffff)
        rnext = rng.randint(-0x80000000,0x7fffffff)
        a = instruction_order_block(samples,c["d3"],c["d2"],c["d4"],rprev,rnext)
        b = closed_form_block(samples,c["d3"],c["d2"],c["d4"],rprev,rnext)
        for key in ("output","quantized","ramp_used","ramp_start_scaled","ramp_target_scaled","ramp_step","ramp_after_32"):
            if a[key] != b[key]:
                raise AssertionError(f"block mismatch BR={br} key={key}")
    return {"seed":seed,"q31_equivalence_cases":len(pairs),"block_equivalence_cases":cases,"samples_compared_per_block":32,"result":"PASS"}


def report(image: bytes, cases: int, seed: int) -> dict:
    sig = verify_signatures(image)
    tests = randomized_tests(image,cases,seed)
    return {
        "result":"PASS",
        "verification":sig,
        "full_block_equation":{
            "coefficients":"(D2,D3,D4) = stock BR setup(ui_br)",
            "quantizer":"q[i] = wrap32((truncQ31(sample[i] * D3)) << D2)",
            "ramp_start":"r0 = truncQ31(previous_level_state * D4)",
            "ramp_target":"r1 = truncQ31(current_level_state * D4)",
            "ramp_step":"dr = wrap32(r1-r0) >> 5",
            "output":"y[i] = truncQ31(q[i] * wrap32(r0 + i*dr)), i=0..31",
            "pipeline_note":"stock prequantizes the next source pair before writing each emitted pair; two additional Q31 values are prefetched at block exit"
        },
        "semantic_trace":{
            "previous_level_state":"D7 <- 0x10(A3) at 0x401186B2",
            "current_level_source":"signed word 0x0E(A1) at 0x401186CC",
            "current_level_shaping":"conditional gate; square; double; optional hold-previous; arithmetic >>2; persisted back to 0x10(A3)",
            "classification":"post-quantizer sample-level/amplitude smoothing ramp with BR-dependent D4 compensation",
            "confidence":"high for arithmetic/field role; some gating flags remain unlabeled"
        },
        "accumulator_invariant":{
            "ACC2_ACC3":"each output MOVCLR at 0x40118796/9A clears the post-ramp accumulators",
            "ACC0_ACC1":"0x401187A2/A4 clear the two prefetched quantizer accumulators at block exit",
            "consequence":"all four EMAC accumulators are clear at normal block exit, making the zero-ACC Q31 model self-maintaining across blocks"
        },
        "selected_br_coefficients":[br_coefficients(image,n) for n in (0,1,32,64,96,112,114,120,127)],
        "randomized_equivalence":tests,
        "resolved":[
            "D4 is applied to previous/current sample-level ramp states before the 32-sample block",
            "D5 becomes the arithmetic /32 interpolation delta after D4 compensation",
            "ACC2/ACC3 apply the interpolated multiplicative level to the D3/D2-quantized samples",
            "the 16-iteration loop emits exactly 32 32-bit samples and prefetches the next pair",
            "normal loop exit clears ACC0..ACC3"
        ],
        "open_questions":[
            "name the A3+0x28/A3+0x29 status flags and the global bitmask gate at 0x8000586C",
            "resolve the exact control-frame bridge from modulation destination SMP_BR to terminal renderer 6(A1)",
            "validate the model against captured hardware output once the physical unit is available"
        ],
        "safety":"Static reconstruction/model only; no firmware bytes are modified or repacked."
    }


def main() -> None:
    ap=argparse.ArgumentParser(); ap.add_argument("main_image",type=Path); ap.add_argument("--cases",type=int,default=5000); ap.add_argument("--seed",type=lambda x:int(x,0),default=0xA172B17); ap.add_argument("--output",type=Path); args=ap.parse_args()
    result=report(args.main_image.read_bytes(),args.cases,args.seed)
    text=json.dumps(result,indent=2)+"\n"
    if args.output: args.output.write_text(text,encoding="utf-8")
    print(text,end="")

if __name__ == "__main__": main()
