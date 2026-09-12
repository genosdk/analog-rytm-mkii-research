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
- encoder packet 0x3n + signed 8-bit delta, indices 0..8
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import struct
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
RUNTIME_MAGIC = b"F2L2"
RUNTIME_VERSION = 1
RUNTIME_LANES = 8
RUNTIME_SNAPSHOT_SIZE = 108


def control_to_q31(control: int) -> int:
    control = max(0, min(127, int(control)))
    return (control * 0x7FFFFFFF + 63) // 127


def rate_to_increment(control: int) -> int:
    control = max(0, min(127, int(control)))
    frequency = 0.01 * ((100.0 / 0.01) ** (control / 127.0))
    return min(0xFFFFFFFF, round(frequency * (1 << 32) / 1500.0))


class RuntimeControls:
    """Complete atomic host-to-QEMU Filter2/LFO2 control state."""

    def __init__(self) -> None:
        self.filter2 = [64] * RUNTIME_LANES
        self.waveform = [0] * RUNTIME_LANES
        self.mode = [0] * RUNTIME_LANES
        self.rate = [64] * RUNTIME_LANES
        self.depth = [64] * RUNTIME_LANES
        self.enable_mask = 0
        self.trigger_mask = 0
        self.reset_generation = [0] * RUNTIME_LANES

    def encode(self) -> bytes:
        snapshot = struct.pack(
            ">4sBBH8B8B8B8I8IHH8B",
            RUNTIME_MAGIC,
            RUNTIME_VERSION,
            0,
            0,
            *self.filter2,
            *self.waveform,
            *self.mode,
            *(rate_to_increment(value) for value in self.rate),
            *(control_to_q31(value) for value in self.depth),
            self.enable_mask,
            self.trigger_mask,
            *self.reset_generation,
        )
        if len(snapshot) != RUNTIME_SNAPSHOT_SIZE:
            raise AssertionError("runtime control snapshot layout changed")
        return snapshot

    @classmethod
    def decode(cls, snapshot: bytes) -> "RuntimeControls":
        if len(snapshot) != RUNTIME_SNAPSHOT_SIZE:
            raise ValueError("wrong runtime control snapshot size")
        values = struct.unpack(">4sBBH8B8B8B8I8IHH8B", snapshot)
        if values[:2] != (RUNTIME_MAGIC, RUNTIME_VERSION):
            raise ValueError("unsupported runtime control snapshot")
        state = cls()
        state.filter2 = list(values[4:12])
        state.waveform = list(values[12:20])
        state.mode = list(values[20:28])
        state.rate = [
            min(
                range(128),
                key=lambda control: abs(rate_to_increment(control) - raw),
            )
            for raw in values[28:36]
        ]
        state.depth = [
            min(
                range(128),
                key=lambda control: abs(control_to_q31(control) - raw),
            )
            for raw in values[36:44]
        ]
        state.enable_mask = values[44]
        state.trigger_mask = values[45]
        state.reset_generation = list(values[46:54])
        return state


class PanelLink:
    def __init__(self, sock: socket.socket, verbose: bool = True) -> None:
        self.sock = sock
        self.verbose = verbose
        self.lock = threading.Lock()
        self.rx = bytearray()
        self.identity_replied = False
        self.button_groups = {2: 0, 3: 0, 4: 0, 5: 0}

    def send(self, data: bytes, label: str = "host -> firmware") -> None:
        with self.lock:
            self.sock.sendall(data)
        if self.verbose:
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
            if self.verbose:
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
        idx = ENCODERS.get(name.upper())
        if idx is None or not -128 <= delta <= 127:
            return
        self.send(bytes((0x30 | idx, delta & 0xFF)))


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


def publish_filter2_controls(path: Path, controls: bytearray) -> None:
    """Atomically publish all eight absolute controls for QEMU's live bridge."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(controls)
    os.replace(temporary, path)


def publish_runtime_controls(path: Path, controls: RuntimeControls) -> None:
    """Atomically publish one complete, versioned Filter2/LFO2 snapshot."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(controls.encode())
    os.replace(temporary, path)


def follow_events(path: Path, link: PanelLink, start_at_end: bool,
                  filter2_control_file: Path | None = None,
                  stop_event: threading.Event | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    runtime_controls = RuntimeControls()
    if filter2_control_file is not None and filter2_control_file.is_file():
        existing = filter2_control_file.read_bytes()
        if len(existing) == 8:
            runtime_controls.filter2[:] = existing
        elif len(existing) == RUNTIME_SNAPSHOT_SIZE:
            try:
                runtime_controls = RuntimeControls.decode(existing)
            except ValueError:
                pass
    with path.open("r", encoding="utf-8") as f:
        if start_at_end:
            f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if not line:
                if stop_event is not None and stop_event.is_set():
                    return
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

            if kind == "filter2" and filter2_control_file is not None:
                try:
                    lane = int(name)
                    control = max(0, min(127, int(value)))
                except (TypeError, ValueError):
                    continue
                if 0 <= lane < RUNTIME_LANES:
                    runtime_controls.filter2[lane] = control
                    publish_runtime_controls(filter2_control_file, runtime_controls)
                continue

            if kind == "lfo2" and filter2_control_file is not None:
                try:
                    lane_text, parameter = name.split(":", 1)
                    lane = int(lane_text)
                    control = int(value)
                except (TypeError, ValueError):
                    continue
                if not 0 <= lane < RUNTIME_LANES:
                    continue
                if parameter in {"rate", "depth"}:
                    getattr(runtime_controls, parameter)[lane] = max(
                        0, min(127, control)
                    )
                elif parameter == "waveform":
                    runtime_controls.waveform[lane] = max(0, min(6, control))
                elif parameter == "mode":
                    runtime_controls.mode[lane] = max(0, min(3, control))
                elif parameter in {"enable", "trigger"}:
                    attribute = f"{parameter}_mask"
                    mask = getattr(runtime_controls, attribute)
                    bit = 1 << lane
                    setattr(runtime_controls, attribute,
                            (mask | bit) if control else (mask & ~bit))
                elif parameter == "reset":
                    runtime_controls.reset_generation[lane] = (
                        runtime_controls.reset_generation[lane] + 1
                    ) & 0xFF
                else:
                    continue
                publish_runtime_controls(filter2_control_file, runtime_controls)
                continue

            print(f"unmapped panel event: {event}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", type=Path, default=Path("/tmp/ar_panel.sock"))
    ap.add_argument("--events", type=Path, default=Path("panel_events.jsonl"))
    ap.add_argument("--connect-timeout", type=float, default=15.0)
    ap.add_argument("--replay-existing", action="store_true")
    ap.add_argument("--filter2-controls", type=Path)
    args = ap.parse_args()

    sock = connect_unix(args.socket, args.connect_timeout)
    link = PanelLink(sock)
    reader = threading.Thread(target=link.reader, daemon=True)
    reader.start()
    try:
        follow_events(
            args.events,
            link,
            start_at_end=not args.replay_existing,
            filter2_control_file=args.filter2_controls,
        )
    finally:
        try:
            sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
