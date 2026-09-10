#!/usr/bin/env python3
"""Classify the optional four-word renderer extension at DSPI1 words 321..324."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from audio_callback_probe import EXPECTED_MAIN_SHA256
from renderer_payload_sweep import install_substitution, renderer_addresses

SOURCE_FIRST = 0x80006640
SOURCE_LAST = 0x80006647
PACKET_WORDS = (321, 322, 323, 324)
PACKET_HEADER = 0x80010000
RENDERER_10 = 10


def probe(main_path: Path, emulator_path: Path, probe_root: Path) -> dict:
    main = main_path.read_bytes()
    digest = hashlib.sha256(main).hexdigest()
    if digest != EXPECTED_MAIN_SHA256:
        raise ValueError(f"unexpected MAIN SHA-256: {digest}")

    sys.path.insert(0, str(probe_root.resolve()))
    try:
        from note_pitch_consumer_boundary_probe import load_emulator, run_vector
    finally:
        sys.path.pop(0)

    executions = []
    nonzero_renderers = []
    addresses = renderer_addresses(main)
    for index, renderer in enumerate(addresses):
        module = load_emulator(emulator_path)
        install_substitution(module, renderer)
        original_write = module.Bus.write
        writes = []

        def traced_write(bus, address: int, size: int, value: int) -> None:
            if address <= SOURCE_LAST and address + size > SOURCE_FIRST:
                writes.append({
                    "pc": bus.pc_provider() if bus.pc_provider else 0,
                    "address": address,
                    "size": size,
                    "value": value & ((1 << (size * 8)) - 1),
                })
            original_write(bus, address, size, value)

        module.Bus.write = traced_write
        vector = run_vector(module, main_path, 60, 1, callback_count=1)
        packet_values = [vector["packet_words"][word] for word in PACKET_WORDS]
        sram = vector["sram"]
        offset = SOURCE_FIRST & (len(sram) - 1)
        source_values = [
            int.from_bytes(sram[offset + byte:offset + byte + 2], "big")
            for byte in range(0, 8, 2)
        ]
        if packet_values != [PACKET_HEADER | value for value in source_values]:
            raise ValueError(f"renderer {index} extension did not packetize directly")
        nonzero = any(source_values)
        if nonzero:
            nonzero_renderers.append(index)
        executions.append({
            "index": index,
            "renderer": f"0x{renderer:08X}",
            "nonzero": nonzero,
            "source_halfwords": [f"0x{value:04X}" for value in source_values],
            "packet_words": {
                str(word): f"0x{value:08X}"
                for word, value in zip(PACKET_WORDS, packet_values)
            },
            "writes": [
                {
                    "pc": f"0x{row['pc']:08X}",
                    "address": f"0x{row['address']:08X}",
                    "size": row["size"],
                    "value": f"0x{row['value']:0{2 * row['size']}X}",
                }
                for row in writes
            ],
        })

    expected_nonzero = [11, 12, 19, 20, 25, 51, 52]
    if nonzero_renderers != expected_nonzero:
        raise ValueError(f"unexpected nonzero extension renderers: {nonzero_renderers}")
    renderer_10 = executions[RENDERER_10]
    if renderer_10["writes"] or renderer_10["nonzero"]:
        raise ValueError("renderer 10 unexpectedly populated words 321..324")

    return {
        "result": "PASS",
        "main": {"sha256": digest},
        "emulator": {"sha256": hashlib.sha256(emulator_path.read_bytes()).hexdigest()},
        "method": {
            "renderer_count": len(addresses),
            "common_state": "note 60, synth-live gate, one stock callback",
            "function_pointer_substitution": True,
            "source_byte_range": [f"0x{SOURCE_FIRST:08X}", f"0x{SOURCE_LAST:08X}"],
            "packet_word_indices": list(PACKET_WORDS),
            "packet_encoding": "0x80010000 | source_halfword",
        },
        "classification": {
            "nonzero_renderer_indices": nonzero_renderers,
            "zero_renderer_count": len(addresses) - len(nonzero_renderers),
            "nonzero_renderer_count": len(nonzero_renderers),
            "renderer_10_writes_source": False,
            "renderer_10_packet_words": renderer_10["packet_words"],
            "finding": (
                "Words 321..324 form an optional two-longword renderer extension. "
                "Renderer 10 leaves the source untouched and emits four zero payloads."
            ),
        },
        "executions": executions,
        "interpretation_boundary": (
            "Function-pointer substitution classifies renderer behavior in a common "
            "state; it does not establish authentic machine availability on each track "
            "or identify electrical units."
        ),
        "next_target": (
            "Trace the generic pre-render publication stages that feed the shared "
            "control pointer and identify parameter-dependent DSPI1 words outside the "
            "renderer pitch/setup groups."
        ),
        "safety": "Stock firmware execution and synthetic RAM state only; no image was modified.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("main_image", type=Path)
    parser.add_argument("emulator", type=Path)
    parser.add_argument(
        "--probe-root",
        type=Path,
        default=Path(__file__).resolve().parent,
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = probe(args.main_image, args.emulator, args.probe_root)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
