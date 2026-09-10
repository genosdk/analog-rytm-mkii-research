#!/usr/bin/env python3
"""Build and execute a foreground Filter 2 control-publication shim."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, RETURN_PC, prepared_machine
from filter2_bypass_canary_probe import execute_vector
from filter2_coefficient_slew_probe import control_to_q31, oracle
from filter2_control_publication_probe import (
    FOREGROUND_SETTER_CALLSITE,
    TARGET_ARRAY,
    TARGET_END,
    WORD_SETTER,
)
from filter2_eight_lane_probe import (
    ALL_LANES_MASK,
    Builder,
    FILTER2_STATE0,
    FILTER2_STATE_STRIDE,
    LANES,
    SYMBOLS as FILTER_SYMBOLS,
    build_kernel as build_eight_lane_kernel,
    plane_lanes,
)
from filter2_lfo2_state_canary_probe import image_offset
from filter2_unity_kernel_probe import (
    FILTER2_MASK_ADDRESS,
    FLAGS_ADDRESS,
    MIXER,
    install_input,
    install_tables,
    words_hash,
)
from trigger_queue_probe import load_emulator, stock_call

VIRTUAL_INDEX_BASE = 0x7FF8
VIRTUAL_INDEX_END = VIRTUAL_INDEX_BASE + LANES
SHIM_BASE = 0x402B4820
TABLE_BASE = 0x402B4A00
FILTER2_FLAG = 1
TARGET_OFFSET = 12


def assemble_shim() -> tuple[bytes, dict[str, int]]:
    b = Builder(SHIM_BASE)
    b.label("entry")
    b.emit("4ab9402b4408")                  # TST.L flags
    b.branch_word(0x6700, "stock")          # disabled: exact stock tail-call
    b.emit("202f0004")                      # MOVE.L 4(SP),D0 index
    b.emit("0c8000007ff8")                  # CMPI.L #0x7FF8,D0
    b.branch_word(0x6500, "stock")          # BCS below virtual range
    b.emit("0c8000007fff")                  # CMPI.L #0x7FFF,D0
    b.branch_word(0x6200, "stock")          # BHI above virtual range
    b.emit("222f0008")                      # MOVE.L 8(SP),D1 control
    b.emit("0c810000007f")                  # CMPI.L #127,D1
    b.branch_word(0x6300, "control_valid")  # BLS 0..127
    b.emit("727f")                          # MOVEQ #127,D1 clamp invalid/high
    b.label("control_valid")
    b.emit("e589")                          # LSL.L #2,D1 table byte offset
    b.emit(f"43f9{TABLE_BASE:08x}")         # LEA Q1.31 table,A1
    b.emit("d3c1")                          # ADDA.L D1,A1
    b.emit("2211")                          # MOVE.L (A1),D1 mapped coefficient
    b.emit("048000007ff8")                  # SUBI.L #0x7FF8,D0 lane
    b.emit("eb88")                          # LSL.L #5,D0 32-byte state stride
    b.emit(f"41f9{FILTER2_STATE0 + TARGET_OFFSET:08x}")
    b.emit("d1c0")                          # ADDA.L D0,A0
    b.label("shadow_store")
    b.emit("2081")                          # MOVE.L D1,(A0), one aligned store
    b.emit("4e75")                          # RTS
    b.label("stock")
    b.emit(f"4ef9{WORD_SETTER:08x}")        # JMP untouched stock setter
    return b.finish()


SHIM_BODY, SHIM_SYMBOLS = assemble_shim()
Q31_TABLE = b"".join(control_to_q31(value).to_bytes(4, "big") for value in range(128))
CALLSITE_PATCH = bytes.fromhex(f"4eb9{SHIM_BASE:08x}")


def target_address(lane: int) -> int:
    return FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE + TARGET_OFFSET


def build_candidate(stock: bytes, armed: bool) -> tuple[bytes, dict]:
    image, eight_lane = build_eight_lane_kernel(stock, armed)
    candidate = bytearray(image)
    candidate[
        image_offset(FOREGROUND_SETTER_CALLSITE) : image_offset(FOREGROUND_SETTER_CALLSITE) + 6
    ] = CALLSITE_PATCH
    candidate[image_offset(SHIM_BASE) : image_offset(SHIM_BASE) + len(SHIM_BODY)] = SHIM_BODY
    candidate[image_offset(TABLE_BASE) : image_offset(TABLE_BASE) + len(Q31_TABLE)] = Q31_TABLE
    return bytes(candidate), {
        "eight_lane_kernel": eight_lane,
        "callsite": f"0x{FOREGROUND_SETTER_CALLSITE:08X}",
        "callsite_patch": CALLSITE_PATCH.hex(),
        "shim_address": f"0x{SHIM_BASE:08X}",
        "shim_end_exclusive": f"0x{SHIM_BASE + len(SHIM_BODY):08X}",
        "shim_bytes": len(SHIM_BODY),
        "shim_body": SHIM_BODY.hex(),
        "shim_symbols": {name: f"0x{address:08X}" for name, address in SHIM_SYMBOLS.items()},
        "table_address": f"0x{TABLE_BASE:08X}",
        "table_end_exclusive": f"0x{TABLE_BASE + len(Q31_TABLE):08X}",
        "table_bytes": len(Q31_TABLE),
        "virtual_index_range": [f"0x{VIRTUAL_INDEX_BASE:04X}", f"0x{VIRTUAL_INDEX_END - 1:04X}"],
        "armed": armed,
        "changed_byte_positions": sum(left != right for left, right in zip(stock, candidate)),
    }


def invoke(
    module, image_path: Path, index: int, value: int, armed: bool, entry: int = SHIM_BASE
) -> dict:
    bus = module.Bus()
    bus.load_main(image_path)
    cpu = module.CPU(bus)
    cpu.a[7] = module.INITIAL_SP - 0x2000
    writes = []
    original_write = bus.write

    def traced_write(address: int, size: int, stored: int) -> None:
        if TARGET_ARRAY <= address < TARGET_END or any(
            address == target_address(lane) for lane in range(LANES)
        ):
            writes.append((address, size, stored & ((1 << (size * 8)) - 1)))
        original_write(address, size, stored)

    bus.write = traced_write
    steps = stock_call(cpu, entry, [index, value])
    bus.write = original_write
    return {
        "index": f"0x{index:04X}",
        "value": value,
        "armed": armed,
        "instructions": steps,
        "writes": [
            {"address": f"0x{address:08X}", "size": size, "value": f"0x{stored:0{size * 2}X}"}
            for address, size, stored in writes
        ],
        "d0": cpu.d[0],
        "d1": cpu.d[1],
        "sr": cpu.sr,
    }


def ordinary_passthrough(module, stock_path: Path, disabled_path: Path, armed_path: Path) -> list[dict]:
    vectors = [(0, 0x1234), (37, 0xABCD), (571, 0x007F)]
    results = []
    for index, value in vectors:
        expected_address = TARGET_ARRAY + index * 2
        stock = invoke(module, stock_path, index, value, False, WORD_SETTER)
        disabled = invoke(module, disabled_path, index, value, False)
        armed = invoke(module, armed_path, index, value, True)
        expected_write = [{"address": f"0x{expected_address:08X}", "size": 2, "value": f"0x{value:04X}"}]
        if stock["writes"] != expected_write or disabled["writes"] != expected_write or armed["writes"] != expected_write:
            raise ValueError("ordinary target write did not pass through the shim")
        if (stock["d0"], stock["d1"], stock["sr"]) != (disabled["d0"], disabled["d1"], disabled["sr"]):
            raise ValueError("disabled shim changed stock volatile return state")
        results.append({
            "word_index": index,
            "value": f"0x{value:04X}",
            "write_address": f"0x{expected_address:08X}",
            "stock_instructions": stock["instructions"],
            "disabled_shim_instructions": disabled["instructions"],
            "armed_ordinary_instructions": armed["instructions"],
            "stock_write_identical": True,
            "disabled_return_state_identical": True,
        })
    return results


def virtual_publications(module, armed_path: Path) -> list[dict]:
    controls = [0, 1, 16, 32, 64, 96, 126, 127]
    results = []
    for lane, control in enumerate(controls):
        result = invoke(module, armed_path, VIRTUAL_INDEX_BASE + lane, control, True)
        coefficient = control_to_q31(control)
        expected = [{
            "address": f"0x{target_address(lane):08X}",
            "size": 4,
            "value": f"0x{coefficient:08X}",
        }]
        if result["writes"] != expected:
            raise ValueError(f"virtual publication for lane {lane} diverged")
        results.append({
            "lane": lane,
            "virtual_index": f"0x{VIRTUAL_INDEX_BASE + lane:04X}",
            "control": control,
            "q31": f"0x{coefficient:08X}",
            "target_address": f"0x{target_address(lane):08X}",
            "write_size_bytes": 4,
            "instructions": result["instructions"],
            "single_aligned_store": target_address(lane) % 4 == 0,
        })
    return results


def integrated_publication_to_filter(module, armed_path: Path, stock: bytes) -> dict:
    controls = [8, 24, 40, 56, 72, 88, 104, 120]
    bus, cpu, _ = prepared_machine(module, armed_path)
    for lane, control in enumerate(controls):
        base = FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE
        bus.write(base, 4, 0)
        bus.write(base + 4, 4, 0)
        bus.write(base + 8, 4, 0)
        stock_call(cpu, SHIM_BASE, [VIRTUAL_INDEX_BASE + lane, control])
        if bus.read(target_address(lane), 4) != control_to_q31(control):
            raise ValueError("published target was not stored before callback")

    install_tables(bus, stock)
    install_input(bus, True)
    before = None
    multiply_calls = 0
    cpu.pushl(RETURN_PC)
    cpu.pc = AUDIO_CALLBACK
    start = cpu.steps
    for _ in range(400_000):
        if cpu.pc == MIXER:
            break
        if cpu.pc == FILTER_SYMBOLS["post_ingress"]:
            before = plane_lanes(bus)
        if cpu.pc == FILTER_SYMBOLS["multiply"]:
            multiply_calls += 1
        cpu.step()
    else:
        raise ValueError("published-control callback did not reach mixer")
    if before is None:
        raise ValueError("published-control callback missed ingress checkpoint")

    after = plane_lanes(bus)
    lanes = []
    for lane, control in enumerate(controls):
        target = control_to_q31(control)
        expected, s1, s2, _ = oracle(before[lane], 0, target)
        base = FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE
        if after[lane] != expected:
            raise ValueError(f"published control did not drive lane {lane} oracle")
        if (bus.read(base, 4), bus.read(base + 4, 4), bus.read(base + 8, 4)) != (s1, s2, target):
            raise ValueError(f"published control lane {lane} final state diverged")
        lanes.append({
            "lane": lane,
            "control": control,
            "target_q31": f"0x{target:08X}",
            "input_sha256": words_hash(before[lane]),
            "output_sha256": words_hash(after[lane]),
            "oracle_match": True,
        })
    if multiply_calls != 512:
        raise ValueError("integrated publication/filter multiply count changed")
    return {
        "controls": controls,
        "instructions_callback_to_mixer": cpu.steps - start,
        "multiply_calls": multiply_calls,
        "lanes": lanes,
        "all_oracles_match": True,
    }


def probe(stock_path: Path, emulator_path: Path, candidate_output: Path | None = None) -> dict:
    stock = stock_path.read_bytes()
    digest = hashlib.sha256(stock).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")
    if SHIM_BASE + len(SHIM_BODY) > TABLE_BASE:
        raise ValueError("publication shim overlaps Q1.31 lookup table")
    disabled, disabled_build = build_candidate(stock, False)
    armed, armed_build = build_candidate(stock, True)
    module = load_emulator(emulator_path)

    temporary = None
    if candidate_output is None:
        temporary = tempfile.NamedTemporaryFile(suffix=".bin")
        disabled_path = Path(temporary.name)
    else:
        disabled_path = candidate_output
    disabled_path.write_bytes(disabled)
    try:
        with tempfile.NamedTemporaryFile(suffix=".bin") as armed_temp:
            armed_path = Path(armed_temp.name)
            armed_path.write_bytes(armed)
            passthrough = ordinary_passthrough(module, stock_path, disabled_path, armed_path)
            publications = virtual_publications(module, armed_path)
            integration = integrated_publication_to_filter(module, armed_path, stock)
            callback_equivalence = []
            for active in (False, True):
                stock_run = execute_vector(module, stock_path, stock, active, False)
                disabled_run = execute_vector(module, disabled_path, stock, active, True)
                fields = (
                    "mixer_entry_registers", "mixer_input_sha256", "ingress_output_sha256",
                    "mixer_instructions", "mixer_output_writes", "mixer_output_sha256",
                )
                equality = {field: stock_run[field] == disabled_run[field] for field in fields}
                if not all(equality.values()):
                    raise ValueError("disabled publication candidate diverged from stock callback")
                callback_equivalence.append({"input": "active" if active else "zero", "bit_identical": equality})
    finally:
        if temporary is not None:
            temporary.close()

    return {
        "result": "PASS",
        "stock": {"path": str(stock_path), "sha256": digest},
        "disabled_candidate": {
            "path": str(candidate_output) if candidate_output else "temporary execution image",
            "sha256": hashlib.sha256(disabled).hexdigest(),
            **disabled_build,
        },
        "armed_emulation_probe": {
            "sha256": hashlib.sha256(armed).hexdigest(),
            **armed_build,
            "artifact_retained": False,
        },
        "ordinary_stock_passthrough": passthrough,
        "virtual_filter2_publications": publications,
        "publication_to_filter_integration": integration,
        "disabled_callback_stock_equivalence": callback_equivalence,
        "conclusion": (
            "The foreground callsite now detours through a flag-gated publication shim. "
            "Disabled and ordinary indexed writes tail-call the untouched stock setter. "
            "Armed virtual indices 0x7FF8..0x7FFF map eight 0..127 controls through a "
            "128-entry Q1.31 table and publish each lane target with one aligned 32-bit store. "
            "A following callback consumes all eight published targets and matches independent oracles."
        ),
        "scope_limit": (
            "The virtual command range is proven only at the internal setter ABI. No front-panel, "
            "MIDI, mouse or QWERTY transport is attached yet, and hardware cycle margin remains unmeasured."
        ),
        "next_target": (
            "Identify the least invasive existing external-control ingress that can issue the virtual "
            "Filter 2 indices, with the desktop mouse/QWERTY bridge as the initial laboratory controller."
        ),
        "safety": (
            "Only a default-disabled decompressed MAIN lab candidate is retained. The armed image was "
            "temporary; no ELE3 container or flashable SysEx was built."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stock_main", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument("--candidate-output", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = probe(args.stock_main, args.emulator, args.candidate_output)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.report:
        args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
