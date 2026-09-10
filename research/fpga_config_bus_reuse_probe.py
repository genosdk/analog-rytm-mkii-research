#!/usr/bin/env python3
"""Classify DSPI1's FPGA configuration-pin reuse in the stock application.

This joins two already-extracted, non-secret evidence sets: the CPU-side DSPI1
wire mode and the stock XC3S200A IOB inventory.  It does not infer PCB
continuity from a photograph and it does not modify firmware or FPGA data.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent

# Established by the MCF5441x DSPI1 alternate-function/package mapping and the
# FPGA slave-serial configuration path.  D0 is DIN in slave-serial mode.
BOARD_ROUTES = {
    "sck": {
        "cpu": "PG5 / ball A10 / DSPI1_SCK",
        "fpga_package_pin": 53,
        "fpga_configuration_role": "CCLK",
    },
    "sout": {
        "cpu": "PG7 / ball B12 / DSPI1_SOUT",
        "fpga_package_pin": 51,
        "fpga_configuration_role": "D0 (DIN in slave-serial mode)",
    },
}


def probe(inventory_path: Path, wire_path: Path) -> dict:
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    wire = json.loads(wire_path.read_text(encoding="utf-8"))
    if wire["result"] != "PASS_DSPI1_BR_WIRE_MODE_IDENTIFIED":
        raise ValueError("DSPI1 wire-mode prerequisite did not pass")

    by_pin = {row["package_pin"]: row for row in inventory["inventory"]}
    pads = {}
    for signal, route in BOARD_ROUTES.items():
        row = by_pin[route["fpga_package_pin"]]
        expected_shared = "CCLK" if signal == "sck" else "D0"
        if row["shared_config"] != expected_shared:
            raise ValueError(
                f"P{row['package_pin']} expected {expected_shared}, got "
                f"{row['shared_config']}"
            )
        parked = (
            row["ibuf_mode"] == "NONE"
            and not row["input_enabled"]
            and not row["output_enabled"]
            and row["direction"] == "unused"
        )
        if not parked:
            raise ValueError(f"P{row['package_pin']} is not parked: {row}")
        pads[signal] = {
            **route,
            "edge_iob": row["edge_iob"],
            "stock_user_iob": {
                "ibuf_mode": row["ibuf_mode"],
                "input_enabled": row["input_enabled"],
                "output_enabled": row["output_enabled"],
                "direction": row["direction"],
            },
            "parked_after_configuration": True,
        }

    return {
        "schema_version": 1,
        "result": "PASS_DSPI1_FPGA_CONFIG_PADS_PARKED",
        "dspi1_runtime_transport": {
            "wire_mode": wire["wire_mode"],
            "chip_select": "PCS0",
            "known_signals": ["SCK", "SOUT"],
        },
        "configuration_bus_routes": pads,
        "conclusion": (
            "DSPI1 SCK and SOUT reach the FPGA slave-serial configuration pins, "
            "but the stock application image leaves both corresponding user IOBs "
            "disabled. Runtime PCS0 traffic therefore is not evidence of a live "
            "FPGA-fabric receiver on those two pads. PCS0 may select another shared-"
            "bus peripheral or board glue; its consumer remains unresolved."
        ),
        "photo_corroboration": (
            "CPU8251D component-side photographs show U1 (ColdFire), U11 "
            "(XC3S200A), nearby RAM/storage packages, and no obvious dedicated "
            "Xilinx configuration PROM. Traces enter vias/inner layers, so the "
            "photographs do not establish continuity or identify the PCS0 sink."
        ),
        "next_target": (
            "Classify every DSPI1 PCS0 payload position across stock machine "
            "renderers and correlate changed fields with the non-FPGA serial/control "
            "devices visible on CPU8251D."
        ),
        "safety": "Read-only synthesis of committed evidence; no firmware or FPGA bytes are modified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory",
        type=Path,
        default=HERE / "AR172_FPGA_IOB_MODE_INVENTORY.json",
    )
    parser.add_argument(
        "--wire-mode",
        type=Path,
        default=HERE / "AR172_DSPI1_WIRE_MODE_TRACE.json",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = probe(args.inventory, args.wire_mode)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
