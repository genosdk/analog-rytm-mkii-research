#!/usr/bin/env python3
"""Validate and compare native audio-kernel contract traces."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

HEX = re.compile(r"^0x[0-9a-f]+$")


class TraceError(ValueError):
    """Raised when a trace is incomplete or structurally invalid."""


def _hex(value: Any, field: str) -> str:
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise TraceError(f"{field} must be canonical lower-case hexadecimal")
    return value


def load_trace(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TraceError(f"cannot read {path}: {exc}") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TraceError(f"{path}:{number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(record, dict):
            raise TraceError(f"{path}:{number}: record is not an object")
        records.append(record)
    validate_trace(records, str(path))
    return records


def validate_trace(records: list[dict[str, Any]], label: str = "trace") -> None:
    if len(records) < 4:
        raise TraceError(f"{label}: incomplete trace")
    header, footer = records[0], records[-1]
    if header.get("kind") != "header" or header.get("schema_version") != 1:
        raise TraceError(f"{label}: missing schema-version-1 header")
    for field in ("start_pc", "end_pc", "exit_pc"):
        _hex(header.get(field), f"{label} header {field}")
    if footer.get("kind") != "footer" or footer.get("complete") is not True:
        raise TraceError(f"{label}: trace did not reach a complete footer")

    kinds = [record.get("kind") for record in records]
    if (
        kinds[1] != "boundary"
        or kinds[-2] != "boundary"
        or any(kind != "memory" for kind in kinds[2:-2])
    ):
        raise TraceError(f"{label}: records are not in contract order")

    boundaries = [record for record in records if record.get("kind") == "boundary"]
    if [record.get("phase") for record in boundaries] != ["entry", "exit"]:
        raise TraceError(f"{label}: requires exactly one entry and one exit boundary")
    register_names: tuple[str, ...] | None = None
    for boundary in boundaries:
        _hex(boundary.get("pc"), f"{label} {boundary.get('phase')} pc")
        values = boundary.get("registers")
        if not isinstance(values, dict) or not values:
            raise TraceError(f"{label}: empty boundary register set")
        for name, value in values.items():
            if not isinstance(name, str) or not name:
                raise TraceError(f"{label}: invalid register name")
            _hex(value, f"{label} register {name}")
        names = tuple(values)
        if register_names is not None and names != register_names:
            raise TraceError(f"{label}: entry/exit register sets differ")
        register_names = names
    if boundaries[0]["pc"] != header["start_pc"]:
        raise TraceError(f"{label}: entry PC does not match header")
    if boundaries[1]["pc"] != header["exit_pc"]:
        raise TraceError(f"{label}: exit PC does not match header")

    memory = [record for record in records if record.get("kind") == "memory"]
    if not memory:
        raise TraceError(f"{label}: no memory events captured")
    start = int(header["start_pc"], 16)
    end = int(header["end_pc"], 16)
    for sequence, record in enumerate(memory):
        if record.get("sequence") != sequence:
            raise TraceError(f"{label}: non-contiguous memory sequence")
        pc = int(_hex(record.get("pc"), f"{label} memory pc"), 16)
        if not start <= pc <= end:
            raise TraceError(f"{label}: memory event outside kernel window")
        if record.get("operation") not in ("load", "store"):
            raise TraceError(f"{label}: invalid memory operation")
        _hex(record.get("address"), f"{label} memory address")
        size = record.get("size")
        if size not in (1, 2, 4, 8, 16):
            raise TraceError(f"{label}: invalid memory size")
        value = _hex(record.get("value"), f"{label} memory value")
        if len(value) != 2 + size * 2:
            raise TraceError(f"{label}: memory value width does not match size")

    loads = sum(record["operation"] == "load" for record in memory)
    stores = len(memory) - loads
    expected = (len(memory), loads, stores)
    observed = tuple(footer.get(key) for key in ("memory_events", "loads", "stores"))
    if observed != expected:
        raise TraceError(f"{label}: footer counts do not match captured events")


def signature(records: list[dict[str, Any]], mode: str) -> list[Any]:
    if mode not in ("strict", "topology"):
        raise ValueError(f"unknown comparison mode: {mode}")
    result: list[Any] = []
    for record in records:
        kind = record["kind"]
        if kind == "header":
            result.append((kind, record["schema_version"], record["start_pc"],
                           record["end_pc"], record["exit_pc"]))
        elif kind == "boundary":
            registers = record["registers"]
            reg_signature = (tuple(registers.items()) if mode == "strict"
                             else tuple(registers))
            result.append((kind, record["phase"], record["pc"], reg_signature))
        elif kind == "memory":
            base = (kind, record["pc"], record["operation"],
                    record["address"], record["size"])
            result.append(base + ((record["value"],) if mode == "strict" else ()))
    return result


def compare(left: list[dict[str, Any]], right: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    left_signature = signature(left, mode)
    right_signature = signature(right, mode)
    matched = left_signature == right_signature
    first_difference = None
    if not matched:
        shared = min(len(left_signature), len(right_signature))
        first_difference = next(
            (index for index in range(shared)
             if left_signature[index] != right_signature[index]), shared
        )
    return {
        "schema_version": 1,
        "mode": mode,
        "match": matched,
        "left_records": len(left_signature),
        "right_records": len(right_signature),
        "first_difference": first_difference,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--mode", choices=("strict", "topology"), default="strict")
    args = parser.parse_args(argv)
    try:
        result = compare(load_trace(args.left), load_trace(args.right), args.mode)
    except TraceError as exc:
        print(json.dumps({"error": str(exc), "valid": False}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["match"] else 1


if __name__ == "__main__":
    sys.exit(main())
