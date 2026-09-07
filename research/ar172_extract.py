#!/usr/bin/env python3
"""Decode an Analog Rytm MKII OS SysEx image and extract its ELE3 sections.

Read-only research utility. It never produces a flashable SysEx image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def unpack_7bit(encoded: bytes) -> bytes:
    """Elektron packet packing: mask bit 6 belongs to the first data byte."""
    out = bytearray()
    for start in range(0, len(encoded), 8):
        group = encoded[start : start + 8]
        if len(group) < 2:
            break
        mask = group[0]
        for index, value in enumerate(group[1:]):
            out.append(value | (((mask >> (6 - index)) & 1) << 7))
    return bytes(out)


def decode_sysex(path: Path) -> tuple[bytes, dict]:
    blob = path.read_bytes()
    if len(blob) < 32 or blob[:7] != bytes.fromhex("f000203c0c007f"):
        raise ValueError("not an Elektron AR MKII update SysEx")

    first, last = blob[:16], blob[-16:]
    packet_area = blob[16:-16]
    if len(packet_area) % 128:
        raise ValueError("data packet area is not a multiple of 128 bytes")

    decoded = bytearray()
    bad_frames = []
    sequence = []
    for number, start in enumerate(range(0, len(packet_area), 128)):
        packet = packet_area[start : start + 128]
        if packet[:7] != bytes.fromhex("f000203c0c007e") or packet[-1] != 0xF7:
            bad_frames.append(number)
            continue
        sequence.append(int.from_bytes(packet[7:10], "big"))
        decoded.extend(unpack_7bit(packet[10:126]))

    declared_size = int.from_bytes(decoded[0:4], "big")
    declared_checksum = int.from_bytes(decoded[4:8], "big")
    container = bytes(decoded[8 : 8 + declared_size])
    if container[:4] != b"ELE3":
        raise ValueError("decoded payload does not contain an ELE3 container")

    info = {
        "source": str(path),
        "source_size": len(blob),
        "source_sha256": hashlib.sha256(blob).hexdigest(),
        "first_frame": first.hex(),
        "last_frame": last.hex(),
        "packet_count": len(packet_area) // 128,
        "bad_frames": bad_frames,
        "sequence_first": sequence[0],
        "sequence_last": sequence[-1],
        "decoded_bytes_with_padding": len(decoded),
        "container_size": len(container),
        "declared_checksum": f"0x{declared_checksum:08X}",
        "container_byte_sum": f"0x{sum(container) & 0xFFFFFFFF:08X}",
    }
    return container, info


class APLibDepacker:
    def __init__(self, source: bytes):
        self.source = source
        self.pos = 0
        self.tag = 0
        self.bitcount = 0

    def byte(self) -> int:
        if self.pos >= len(self.source):
            raise ValueError("truncated aPLib stream")
        value = self.source[self.pos]
        self.pos += 1
        return value

    def bit(self) -> int:
        if self.bitcount == 0:
            self.tag = self.byte()
            self.bitcount = 8
        value = (self.tag >> 7) & 1
        self.tag = (self.tag << 1) & 0xFF
        self.bitcount -= 1
        return value

    def gamma(self) -> int:
        value = 1
        while True:
            value = (value << 1) + self.bit()
            if not self.bit():
                return value

    @staticmethod
    def copy(out: bytearray, offset: int, length: int) -> None:
        if offset <= 0 or offset > len(out):
            raise ValueError(f"invalid aPLib back-reference {offset} at {len(out)}")
        for _ in range(length):
            out.append(out[-offset])

    def depack(self) -> bytes:
        out = bytearray([self.byte()])
        r0 = -1
        lwm = 0
        while True:
            if not self.bit():
                out.append(self.byte())
                lwm = 0
                continue
            if not self.bit():
                offset = self.gamma()
                if lwm == 0 and offset == 2:
                    offset = r0
                    length = self.gamma()
                else:
                    offset -= 3 if lwm == 0 else 2
                    offset = (offset << 8) + self.byte()
                    length = self.gamma()
                    if offset >= 32000:
                        length += 1
                    if offset >= 1280:
                        length += 1
                    if offset < 128:
                        length += 2
                    r0 = offset
                self.copy(out, offset, length)
                lwm = 1
                continue
            if not self.bit():
                offset = self.byte()
                length = 2 + (offset & 1)
                offset >>= 1
                if offset == 0:
                    return bytes(out)
                self.copy(out, offset, length)
                r0 = offset
                lwm = 1
                continue
            offset = 0
            for _ in range(4):
                offset = (offset << 1) + self.bit()
            if offset > len(out):
                raise ValueError(
                    f"invalid aPLib single-byte back-reference {offset} at {len(out)}"
                )
            out.append(out[-offset] if offset else 0)
            lwm = 0


class NRV2BDepacker:
    """Bounded decoder for the UCL NRV2B 8-bit bit-buffer format."""

    def __init__(self, source: bytes, output_limit: int = 16 * 1024 * 1024):
        self.source = source
        self.output_limit = output_limit
        self.pos = 0
        self.bit_buffer = 0

    def byte(self) -> int:
        if self.pos >= len(self.source):
            raise ValueError("truncated NRV2B stream")
        value = self.source[self.pos]
        self.pos += 1
        return value

    def bit(self) -> int:
        # UCL getbit_8: reload an 8-bit MSB-first control word when its
        # sentinel bit has shifted out.
        if self.bit_buffer & 0x7F:
            self.bit_buffer *= 2
        else:
            self.bit_buffer = self.byte() * 2 + 1
        return (self.bit_buffer >> 8) & 1

    def append(self, out: bytearray, value: int) -> None:
        if len(out) >= self.output_limit:
            raise ValueError("NRV2B output limit exceeded")
        out.append(value)

    def depack(self) -> bytes:
        out = bytearray()
        last_offset = 1
        while True:
            while self.bit():
                self.append(out, self.byte())

            offset_code = 1
            while True:
                offset_code = offset_code * 2 + self.bit()
                if offset_code > 0xFFFFFF + 3:
                    raise ValueError("invalid NRV2B offset code")
                if self.bit():
                    break

            if offset_code == 2:
                offset = last_offset
            else:
                offset = ((offset_code - 3) * 256 + self.byte()) & 0xFFFFFFFF
                if offset == 0xFFFFFFFF:
                    if self.pos != len(self.source):
                        raise ValueError(
                            f"NRV2B ended with {len(self.source) - self.pos} trailing bytes"
                        )
                    return bytes(out)
                offset += 1
                last_offset = offset

            length = self.bit() * 2 + self.bit()
            if length == 0:
                length = 1
                while True:
                    length = length * 2 + self.bit()
                    if length >= self.output_limit:
                        raise ValueError("invalid NRV2B match length")
                    if self.bit():
                        break
                length += 2
            length += offset > 0xD00

            # The UCL loop writes once before its do/while counter, so the
            # number of copied bytes is m_len + 1.
            copy_count = length + 1
            if offset <= 0 or offset > len(out):
                raise ValueError(
                    f"invalid NRV2B back-reference {offset} at {len(out)}"
                )
            if len(out) + copy_count > self.output_limit:
                raise ValueError("NRV2B output limit exceeded")
            for _ in range(copy_count):
                out.append(out[-offset])


def parse_ele3(container: bytes) -> tuple[dict, list[dict]]:
    header_size = int.from_bytes(container[4:8], "big")
    section_count = int.from_bytes(container[0x1C:0x20], "big")
    header = {
        "magic": container[:4].decode("ascii"),
        "header_size": header_size,
        "hardware": container[8:0x14].decode("ascii").rstrip(" \0"),
        "version": container[0x14:0x18].decode("ascii").rstrip("\0"),
        "section_count": section_count,
    }
    sections = []
    for index in range(section_count):
        pos = 0x20 + index * 16
        section_id = int.from_bytes(container[pos : pos + 4], "big")
        offset = int.from_bytes(container[pos + 4 : pos + 8], "big")
        length = int.from_bytes(container[pos + 8 : pos + 12], "big")
        load_address = int.from_bytes(container[pos + 12 : pos + 16], "big")
        data = container[offset : offset + length]
        sections.append(
            {
                "index": index,
                "id": section_id,
                "offset": offset,
                "length": length,
                "load_address": load_address,
                "data": data,
            }
        )
    return header, sections


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("syx", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    container, transport = decode_sysex(args.syx)
    header, sections = parse_ele3(container)
    (args.output / "container.ele3").write_bytes(container)
    report = {"transport": transport, "header": header, "sections": []}

    for section in sections:
        data = section.pop("data")
        stem = f"section_{section['index']}_id_{section['id']}"
        (args.output / f"{stem}.bin").write_bytes(data)
        entry = dict(section)
        entry["sha256"] = hashlib.sha256(data).hexdigest()

        if len(data) >= 9:
            stream_length = int.from_bytes(data[:4], "big")
            stream_sum = int.from_bytes(data[4:8], "big")
            if 0 < stream_length <= len(data) - 8:
                stream = data[8 : 8 + stream_length]
                entry["compression_header"] = {
                    "stream_length": stream_length,
                    "stream_sum": f"0x{stream_sum:08X}",
                    "calculated_stream_sum": f"0x{sum(stream) & 0xFFFFFFFF:08X}",
                }
                try:
                    unpacked = NRV2BDepacker(stream).depack()
                    (args.output / f"{stem}.decompressed.bin").write_bytes(unpacked)
                    entry["compression"] = "UCL NRV2B/8"
                    entry["decompressed_size"] = len(unpacked)
                    entry["decompressed_sha256"] = hashlib.sha256(unpacked).hexdigest()
                except ValueError as exc:
                    entry["decompression_error"] = str(exc)
        report["sections"].append(entry)

    (args.output / "extract_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
