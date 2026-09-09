#!/usr/bin/env python3
"""Trace the first configured routing hop around the AR MKII DSPI FPGA pins.

This is a read-only stock-bitstream decoder.  It consumes the locally extracted
FPGA image plus Project Combine's text database; neither proprietary input is
written to the repository.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from fpga_iob_mode_inventory import (
    BOND57,
    COLUMN_FRAMES,
    PROJECT_COMBINE_COMMIT,
    bel_for,
    inventory,
    load_frames,
)


PIN_ENDPOINTS = {
    28: {"column": 3, "ioi": 0, "input_wire": "OUT_FAN[4]"},
    29: {"column": 3, "ioi": 1, "input_wire": "OUT_FAN[5]"},
    30: {"column": 5, "ioi": 0, "input_wire": "OUT_FAN[4]"},
    31: {"column": 5, "ioi": 1, "input_wire": "OUT_FAN[5]"},
}


def braced(text: str, marker: str) -> str:
    start = text.index(marker)
    opening = text.index("{", start)
    depth = 0
    for pos in range(opening, len(text)):
        if text[pos] == "{":
            depth += 1
        elif text[pos] == "}":
            depth -= 1
            if depth == 0:
                return text[opening + 1 : pos]
    raise ValueError(f"unterminated block: {marker}")


def refs(spec: str) -> list[tuple[bool, int, int]]:
    return [
        (inv == "!", int(frame), int(bit))
        for inv, frame, bit in re.findall(r"(!?)MAIN\[(\d+)\]\[(\d+)\]", spec)
    ]


def parse_choices(body: str) -> dict[str, int]:
    return {
        name: int(bits, 2)
        for name, bits in re.findall(r"^\s*([A-Za-z0-9_\.\[\]-]+)\s*=\s*0b([01]+),", body, re.M)
    }


def parse_muxes(tile: str) -> list[dict]:
    out = []
    pat = re.compile(r"\bmux\s+([^\s]+)\s+@\[(.*?)\]\s*\{", re.S)
    for match in pat.finditer(tile):
        block = braced(tile[match.start() :], "mux ")
        out.append({"destination": match.group(1), "refs": refs(match.group(2)), "choices": parse_choices(block)})
    return out


def parse_bel_attribute(tile: str, bel: str, attribute: str) -> dict:
    block = braced(tile, f"bel {bel} ")
    match = re.search(
        rf"attribute\s+{re.escape(attribute)}\s+@\[(.*?)\]\s*\{{",
        block,
        re.S,
    )
    if not match:
        raise ValueError(f"missing {bel}.{attribute}")
    choices = parse_choices(braced(block[match.start() :], f"attribute {attribute}"))
    return {"destination": f"{bel}.{attribute}", "refs": refs(match.group(1)), "choices": choices}


def decode_at(
    frames: list[list[int]], side: str, coordinate: int, item: dict
) -> dict:
    # XC3S200A CHIP17 spans X0..X25 and Y0..Y33.
    column = coordinate if side in "SN" else (0 if side == "W" else 25)
    row = coordinate if side in "WE" else (0 if side == "S" else 33)
    value = 0
    evidence = []
    for index, (inverted, db_frame, db_bit) in enumerate(item["refs"]):
        frame = COLUMN_FRAMES[column] + 18 - db_frame
        # The south-edge IOB termination strip starts at bit 0, but the IOI
        # and INT tiles are ordinary row-0 MAIN tiles and start at bit 16.
        bit = 16 + 64 * row + 63 - db_bit
        physical = frames[frame][bit]
        logical = physical ^ inverted
        value |= logical << index
        evidence.append({
            "vector_bit": index,
            "frame": frame,
            "bit": bit,
            "physical": physical,
            "inverted": inverted,
            "logical": logical,
        })
    selected = [name for name, candidate in item["choices"].items() if candidate == value]
    return {
        "destination": item["destination"],
        "value": value,
        "selected": selected[0] if len(selected) == 1 else selected,
        "evidence": evidence,
    }


def ioi_input(ioi: int, mux_o: str) -> str | None:
    if mux_o == "O1":
        return f"IMUX_DATA[{24 + ioi}]"
    if mux_o == "O2":
        return f"IMUX_DATA[{28 + ioi}]"
    if mux_o in ("FFO1", "FFO2", "FFODDR"):
        return f"registered:{mux_o}"
    return None


def trace(fpga: Path, database: Path) -> dict:
    frames, digest = load_frames(fpga)
    text = database.read_text(encoding="utf-8")
    int_tiles = {
        "SN": braced(text, "tile_class INT_IOI_S3A_SN "),
        "WE": braced(text, "tile_class INT_IOI_S3A_WE "),
    }
    ioi_tiles = {
        "S": braced(text, "tile_class IOI_S3A_S "),
        "N": braced(text, "tile_class IOI_S3A_N "),
        "W": braced(text, "tile_class IOI_S3A_WE "),
        "E": braced(text, "tile_class IOI_S3A_WE "),
    }
    muxes_by_axis = {axis: parse_muxes(tile) for axis, tile in int_tiles.items()}

    pins = {}
    for pin, endpoint in PIN_ENDPOINTS.items():
        column = endpoint["column"]
        input_wire = endpoint["input_wire"]
        consumers = []
        for mux in muxes_by_axis["SN"]:
            decoded = decode_at(frames, "S", column, mux)
            if decoded["selected"] == input_wire:
                consumers.append(decoded)
        pins[str(pin)] = {
            **endpoint,
            "configured_first_hop_consumers": consumers,
        }

    p29_mux_o = decode_at(
        frames,
        "S",
        3,
        parse_bel_attribute(ioi_tiles["S"], "IOI[1]", "MUX_O"),
    )
    output_input = ioi_input(1, p29_mux_o["selected"])
    p29_driver = None
    if output_input and output_input.startswith("IMUX_"):
        candidate = next(
            (mux for mux in muxes_by_axis["SN"] if mux["destination"] == output_input),
            None,
        )
        if candidate:
            p29_driver = decode_at(frames, "S", 3, candidate)

    mode_rows = {row["package_pin"]: row for row in inventory(fpga)["inventory"]}
    package_routes = []
    for package_pin, side, coordinate, iob_index in BOND57:
        bel = bel_for(side, coordinate, iob_index)
        # EdgeIoCoord's suffix is the IOI slot directly.  The separate BEL
        # permutation is only needed to locate IOB configuration bits.
        ioi = iob_index
        axis = "SN" if side in "SN" else "WE"
        muxes = muxes_by_axis[axis]
        raw_wire = f"OUT_FAN[{4 + ioi}]"
        registered_wires = [f"OUT_SEC[{8 + ioi}]", f"OUT_SEC[{12 + ioi}]"]
        consumers = []
        for mux in muxes:
            decoded = decode_at(frames, side, coordinate, mux)
            if decoded["selected"] in [raw_wire, *registered_wires]:
                consumers.append(decoded)
        ioi_tile = ioi_tiles[side]
        mux_ffi = decode_at(
            frames,
            side,
            coordinate,
            parse_bel_attribute(ioi_tile, f"IOI[{ioi}]", "MUX_FFI"),
        )
        mux_o = decode_at(
            frames,
            side,
            coordinate,
            parse_bel_attribute(ioi_tile, f"IOI[{ioi}]", "MUX_O"),
        )
        output_source = ioi_input(ioi, mux_o["selected"])
        output_driver = None
        if output_source and output_source.startswith("IMUX_"):
            driver_mux = next((mux for mux in muxes if mux["destination"] == output_source), None)
            if driver_mux:
                output_driver = decode_at(frames, side, coordinate, driver_mux)
        configured_input = mode_rows[package_pin]["input_enabled"] and bool(consumers)
        configured_output = (
            mode_rows[package_pin]["physical_kind"] != "input-only"
            and mux_o["selected"] != "NONE"
        )
        package_routes.append({
            "package_pin": package_pin,
            "edge_iob": mode_rows[package_pin]["edge_iob"],
            "side": side,
            "coordinate": coordinate,
            "ioi": ioi,
            "ibuf_mode": mode_rows[package_pin]["ibuf_mode"],
            "physical_kind": mode_rows[package_pin]["physical_kind"],
            "raw_input_wire": raw_wire,
            "input_register_mode": mux_ffi["selected"],
            "configured_input_consumers": consumers,
            "configured_input": configured_input,
            "output_mux": mux_o["selected"],
            "output_source": output_source,
            "output_driver": output_driver,
            "configured_output": configured_output,
        })

    quartet_routes = {pin: pins[str(pin)] for pin in PIN_ENDPOINTS}
    quartet_rejected = (
        all(not row["configured_first_hop_consumers"] for row in quartet_routes.values())
        and p29_mux_o["selected"] == "NONE"
    )

    return {
        "schema_version": 1,
        "result": "PASS_P28_P31_QUARTET_REJECTED" if quartet_rejected else "FAIL",
        "fpga": {"kind": "locally extracted stock image; not committed", "sha256": digest},
        "database": {"name": database.name, "project_combine_commit": PROJECT_COMBINE_COMMIT},
        "geometry": "south row-0 IOI/INT MAIN = column frame, Vertical(rev 19, rev 64), bit base 16",
        "pins": pins,
        "p29_sin_output_path": {
            "ioi_mux_o": p29_mux_o,
            "selected_ioi_input": output_input,
            "interconnect_driver": p29_driver,
        },
        "p28_p31_hypothesis": {
            "status": "REJECTED" if quartet_rejected else "NOT_REJECTED",
            "reason": (
                "P28/P29/P30/P31 have no selected OUT_FAN/OUT_SEC first-hop "
                "consumer in their stock INT_IOI tiles, and P29 IOI[1].MUX_O is NONE."
            ),
            "routes": quartet_routes,
        },
        "package_routes": package_routes,
        "local_decode_summary": {
            "selected_input_first_hops": [
                f"P{row['package_pin']} / {row['edge_iob']}"
                for row in package_routes if row["configured_input"]
            ],
            "normal_output_muxes_active": [
                f"P{row['package_pin']} / {row['edge_iob']}"
                for row in package_routes if row["configured_output"]
            ],
        },
        "boundary": (
            "First configured switch-matrix hop plus IOI output mux only. The rejected "
            "quartet result is definitive for normal user-I/O routing, but no replacement "
            "semantic DSPI pin assignment is made here."
        ),
        "safety": "Read-only stock-bitstream extraction; no FPGA or firmware bits are modified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fpga_image", type=Path)
    parser.add_argument("project_combine_database", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    result = trace(args.fpga_image, args.project_combine_database)
    if args.json:
        args.json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
