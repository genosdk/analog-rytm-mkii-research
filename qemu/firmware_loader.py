#!/usr/bin/env python3
"""Local-only loader for official Analog Rytm MKII update SysEx files.

No Elektron firmware bytes are distributed with the emulator. This module
extracts and decompresses the MAIN section from a user-supplied official .syx
at runtime.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

AR_MAIN_LOAD_ADDRESS = 0x40000400


def unpack_7bit(encoded: bytes) -> bytes:
    out = bytearray()
    for start in range(0, len(encoded), 8):
        group = encoded[start:start + 8]
        if len(group) < 2:
            break
        mask = group[0]
        for index, value in enumerate(group[1:]):
            out.append(value | (((mask >> (6 - index)) & 1) << 7))
    return bytes(out)


def decode_ele3(path: Path) -> bytes:
    blob = path.read_bytes()
    if len(blob) < 32 or blob[:7] != bytes.fromhex("f000203c0c007f"):
        raise ValueError("not an Elektron Analog Rytm MKII update SysEx")
    packet_area = blob[16:-16]
    if len(packet_area) % 128:
        raise ValueError("invalid SysEx packet area")

    decoded = bytearray()
    for start in range(0, len(packet_area), 128):
        packet = packet_area[start:start + 128]
        if packet[:7] != bytes.fromhex("f000203c0c007e") or packet[-1] != 0xF7:
            raise ValueError("invalid Elektron data frame")
        decoded.extend(unpack_7bit(packet[10:126]))

    declared_size = int.from_bytes(decoded[0:4], "big")
    container = bytes(decoded[8:8 + declared_size])
    if container[:4] != b"ELE3":
        raise ValueError("decoded update does not contain an ELE3 container")
    return container


class NRV2BDepacker:
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
                        raise ValueError("NRV2B stream has trailing bytes")
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
            copy_count = length + 1
            if offset <= 0 or offset > len(out):
                raise ValueError("invalid NRV2B back-reference")
            if len(out) + copy_count > self.output_limit:
                raise ValueError("NRV2B output limit exceeded")
            for _ in range(copy_count):
                out.append(out[-offset])


def iter_sections(container: bytes):
    section_count = int.from_bytes(container[0x1C:0x20], "big")
    for index in range(section_count):
        pos = 0x20 + index * 16
        section_id = int.from_bytes(container[pos:pos + 4], "big")
        offset = int.from_bytes(container[pos + 4:pos + 8], "big")
        length = int.from_bytes(container[pos + 8:pos + 12], "big")
        load_address = int.from_bytes(container[pos + 12:pos + 16], "big")
        yield index, section_id, load_address, container[offset:offset + length]


def extract_main(syx: Path, destination: Path) -> dict:
    container = decode_ele3(syx)
    for index, section_id, load_address, data in iter_sections(container):
        if load_address != AR_MAIN_LOAD_ADDRESS:
            continue
        if len(data) < 9:
            raise ValueError("MAIN section is too short")
        stream_length = int.from_bytes(data[:4], "big")
        if not 0 < stream_length <= len(data) - 8:
            raise ValueError("MAIN compression header is invalid")
        stream = data[8:8 + stream_length]
        main = NRV2BDepacker(stream).depack()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(main)
        return {
            "section_index": index,
            "section_id": section_id,
            "load_address": load_address,
            "size": len(main),
            "sha256": hashlib.sha256(main).hexdigest(),
        }
    raise ValueError("MAIN section at 0x40000400 was not found")
