#!/usr/bin/env python3
"""Bridge desktop panel events into the AR MKII firmware's native UART parser.

Research-only host component. This script contains no Elektron firmware bytes.

Proven OS 1.72 mappings:
- UART identity query 70 00 -> reply 70 07 05 05 00
- button packet 0x2n + bitmap, where n is an 8-button group
- group 3 bits 0..7 -> Trig 1..8
- group 2 bits 0..7 -> Trig 9..16
- group 5 bits 2..7 -> TRIG/SYN/SMP/FLTR/AMP/LFO pages
- group 5 bit 0 -> YES; group 4 bit 0 -> NO
- encoder packet 0x3n + signed 8-bit movement delta, indices 0..8
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import threading
import time

IDENTITY_QUERY = b"\x70\x00"
IDENTITY_REPLY = bytes.fromhex("70 07 05 05 00")
ENCODERS = {name: i for i, name in enumerate("ABCDEFGHI")}
BUTTONS = {
    "NO": (4, 0),
    "YES": (5, 0),
    "TRIG": (5, 2),
    "SYN": (5, 3),
    "SMP": (5, 4),
    "FLTR": (5, 5),
    "AMP": (5, 6),
    "LFO": (5, 7),
}


class PanelLink:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.lock = threading.Lock()
        self.rx = bytearray()
        self.identity_replied = False
        self.button_groups = {2: 0, 3: 0, 4: 0, 5: 0}

    def send(self, data: bytes, label: str = "host -> firmware") -> None:
        with self.lock:
            self.sock.sendall(data)
        print(f"{label}: {data.hex(' ')}", flush=True)

    def reader(self) -> None:
        self.sock.settimeout(0.1)
        while True:
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            print(f"firmware -> panel: {data.hex(' ')}", flush=True)
            self.rx.extend(data)
            if not self.identity_replied and IDENTITY_QUERY in self.rx:
                self.send(IDENTITY_REPLY, "panel identity -> firmware")
                self.identity_replied = True

    def set_group_bit(self, group: int, bit: int, pressed: bool) -> None:
        value = self.button_groups.get(group, 0)
        mask = 1 << bit
        value = (value | mask) if pressed else (value & ~mask)
        self.button_groups[group] = value
        self.send(bytes((0x20 | group, value)))

    def set_button(self, name: str, pressed: bool) -> None:
        mapping = BUTTONS.get(name.upper())
        if mapping is None:
            return
        self.set_group_bit(*mapping, pressed)

    def set_trig(self, trig: int, pressed: bool) -> None:
        if not 1 <= trig <= 16:
            return
        if trig <= 8:
            group, bit = 3, trig - 1
        else:
            group, bit = 2, trig - 9
        self.set_group_bit(group, bit, pressed)

    def tap_trig(self, trig: int, dwell: float = 0.035) -> None:
        self.set_trig(trig, True)
        time.sleep(max(0.005, min(dwell, 0.25)))
        self.set_trig(trig, False)

    def encoder(self, name: str, delta: int) -> None:
        name = name.upper()
        idx = ENCODERS.get(name)
        if idx is None or not -127 <= delta <= 127:
            return
        if delta:
            self.send(bytes((0x30 | idx, delta & 0xFF)))

    def set_encoder_value(self, name: str, value: int) -> None:
        """Establish an absolute 0..127 value using relative deltas."""
        if not 0 <= value <= 127 or name.upper() not in ENCODERS:
            return
        # Saturate at zero first, then move to the requested value. This is an
        # experimental fallback until native parameter readback is mapped.
        self.encoder(name, -127)
        if value:
            self.encoder(name, value)


def connect_unix(path: Path, timeout: float) -> socket.socket:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect(str(path))
                return s
            except OSError:
                s.close()
        time.sleep(0.03)
    raise TimeoutError(f"UART socket did not become ready: {path}")


def follow_events(path: Path, link: PanelLink, start_at_end: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with path.open("r", encoding="utf-8") as f:
        if start_at_end:
            f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.025)
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = str(event.get("kind", ""))
            name = str(event.get("name", ""))
            value = event.get("value")

            if kind == "button":
                if value in (1, True, "down", "press"):
                    link.set_button(name, True)
                elif value in (0, False, "up", "release"):
                    link.set_button(name, False)
                continue

            if kind == "trig":
                try:
                    trig = int(name)
                except ValueError:
                    continue
                if value == "tap":
                    link.tap_trig(trig)
                elif value in (1, True, "down", "press"):
                    link.set_trig(trig, True)
                elif value in (0, False, "up", "release"):
                    link.set_trig(trig, False)
                continue

            if kind == "encoder":
                try:
                    delta = int(value)
                except (TypeError, ValueError):
                    continue
                link.encoder(name, delta)
                continue

            if kind == "encoder_value":
                try:
                    absolute = int(value)
                except (TypeError, ValueError):
                    continue
                link.set_encoder_value(name, absolute)
                continue

            print(f"unmapped panel event: {event}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", type=Path, default=Path("/tmp/ar_panel.sock"))
    ap.add_argument("--events", type=Path, default=Path("panel_events.jsonl"))
    ap.add_argument("--connect-timeout", type=float, default=15.0)
    ap.add_argument("--replay-existing", action="store_true")
    args = ap.parse_args()

    sock = connect_unix(args.socket, args.connect_timeout)
    link = PanelLink(sock)
    reader = threading.Thread(target=link.reader, daemon=True)
    reader.start()
    try:
        follow_events(args.events, link, start_at_end=not args.replay_existing)
    finally:
        try:
            sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
