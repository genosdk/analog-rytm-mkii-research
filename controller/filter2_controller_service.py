#!/usr/bin/env python3
"""Local Rytm II research controller backed by the proven emulator shim."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "research"
STATIC = Path(__file__).resolve().parent / "static"
sys.path.insert(0, str(RESEARCH))

from audio_callback_probe import EXPECTED_MAIN_SHA256, prepared_machine  # noqa: E402
from filter2_coefficient_slew_probe import control_to_q31  # noqa: E402
from filter2_publication_shim_probe import (  # noqa: E402
    SHIM_BASE,
    VIRTUAL_INDEX_BASE,
    build_candidate,
    target_address,
)
from filter2_unity_kernel_probe import FILTER2_MASK_ADDRESS, FLAGS_ADDRESS  # noqa: E402
from note_event_constructor_probe import (  # noqa: E402
    EVENT_FLAGS as NOTE_EVENT_FLAGS,
    EVENT_INPUT,
    EVENT_NOTE,
    EVENT_SOURCE_MASK,
    EVENT_TRACK,
    EVENT_TYPE,
    HELD_SOURCE_MASK_BASE,
    NOTE_EVENT_CONSTRUCTOR,
    NOTE_OFF,
    NOTE_ON,
    QWERTY_SOURCE_MASK,
)
from trigger_queue_probe import (  # noqa: E402
    CONTROL_SNAPSHOT_A,
    CONTROL_SNAPSHOT_POINTER,
    EVENT_VALUE,
    QUEUE,
    QUEUE_CAPACITY,
    QUEUE_INITIALIZER,
    QUEUE_INSTALLER,
    QUEUE_RING,
    TRIGGER_RECORD,
    load_emulator,
    run_complete_callback,
    stock_call,
)

LANES = 8
FILTER2_FLAG = 1
ALL_LANES_MASK = 0x00FF
DEFAULT_CONTROL = 64
LIVE_PITCH = 0x80006388
TRACK_CHROMATIC_MODE_SOURCE = 0x412FACA1
LIVE_CHROMATIC_MODE = 0x8000EA18
LIVE_PITCH_READ_PC = 0x4011CA7E
VOICE_RESET_FLAG = 0x80
NOTE_KEYS = {
    "a": 48,
    "w": 49,
    "s": 50,
    "e": 51,
    "d": 52,
    "f": 53,
    "t": 54,
    "g": 55,
    "y": 56,
    "h": 57,
    "u": 58,
    "j": 59,
    "k": 60,
}
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


def clamp_control(value: int) -> int:
    return max(0, min(127, int(value)))


class EmulatorBridge:
    """Long-lived emulator instance that calls the Filter2 publication shim."""

    def __init__(self, stock_main: Path, emulator_path: Path):
        stock = stock_main.read_bytes()
        digest = hashlib.sha256(stock).hexdigest()
        if digest != EXPECTED_MAIN_SHA256:
            raise ValueError(f"unexpected MAIN SHA-256: {digest}")
        candidate, _ = build_candidate(stock, False)
        self._temporary = tempfile.NamedTemporaryFile(suffix=".bin")
        Path(self._temporary.name).write_bytes(candidate)
        self._module = load_emulator(emulator_path)
        self.bus, self.cpu, _ = prepared_machine(self._module, Path(self._temporary.name))
        self._callback_sp = self.cpu.a[7]
        stock_call(self.cpu, QUEUE_INITIALIZER, [QUEUE, 0, QUEUE_RING, QUEUE_CAPACITY])
        stock_call(self.cpu, QUEUE_INSTALLER, [QUEUE])
        self.bus.write(CONTROL_SNAPSHOT_POINTER, 4, CONTROL_SNAPSHOT_A)
        # Stock callback code copies this per-track source byte into its live
        # gate, then selects LIVE_PITCH instead of the fixed note-60 fallback.
        self.bus.write(TRACK_CHROMATIC_MODE_SOURCE, 1, 1)  # 1 = synth
        self.bus.write(FLAGS_ADDRESS, 4, FILTER2_FLAG)
        self.bus.write(FILTER2_MASK_ADDRESS, 2, ALL_LANES_MASK)
        self.lock = threading.RLock()
        self.stock_sha256 = digest
        self.candidate_sha256 = hashlib.sha256(candidate).hexdigest()

    def publish(self, lane: int, value: int) -> dict[str, Any]:
        if not 0 <= lane < LANES:
            raise ValueError("lane must be 0..7")
        value = clamp_control(value)
        virtual_index = VIRTUAL_INDEX_BASE + lane
        expected_address = target_address(lane)
        expected_q31 = control_to_q31(value)
        writes: list[tuple[int, int, int]] = []
        with self.lock:
            original_write = self.bus.write

            def traced_write(address: int, size: int, written: int) -> None:
                if any(address == target_address(item) for item in range(LANES)):
                    writes.append((address, size, written & 0xFFFFFFFF))
                original_write(address, size, written)

            self.bus.write = traced_write
            try:
                steps = stock_call(self.cpu, SHIM_BASE, [virtual_index, value])
            finally:
                self.bus.write = original_write
        if writes != [(expected_address, 4, expected_q31)]:
            raise RuntimeError(f"publication mismatch: {writes!r}")
        return {
            "lane": lane,
            "value": value,
            "virtual_index": f"0x{virtual_index:04X}",
            "q31": f"0x{expected_q31:08X}",
            "target_address": f"0x{expected_address:08X}",
            "instructions": steps,
            "single_aligned_store": expected_address % 4 == 0,
        }

    def trigger_note(self, note: int) -> dict[str, Any]:
        if not 0 <= note <= 127:
            raise ValueError("note must be 0..127")
        pitch_word = note << 16
        with self.lock:
            self._write_note_event(NOTE_ON, note)
            constructor_steps = stock_call(self.cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])
            if self.bus.read(TRIGGER_RECORD, 4) != NOTE_ON:
                raise RuntimeError("stock note-on constructor did not publish trigger state 1")
            pitch_reads: list[dict[str, Any]] = []
            original_read = self.bus.read

            def traced_read(address: int, size: int) -> int:
                value = original_read(address, size)
                if address == LIVE_PITCH and size == 4:
                    pitch_reads.append({
                        "pc": f"0x{self.cpu.pc:08X}",
                        "value": f"0x{value:08X}",
                    })
                return value

            self.bus.read = traced_read
            try:
                callback = run_complete_callback(self.cpu, self._callback_sp, 400_000)
            finally:
                self.bus.read = original_read
            queue_count = self.bus.read(QUEUE + 4, 4)
            if queue_count == 0:
                raise RuntimeError("stock note trigger did not enqueue a command")
            ring_index = (queue_count - 1) & (QUEUE_CAPACITY - 1)
            command_pointer = self.bus.read(QUEUE_RING + ring_index * 4, 4)
            result = {
                "note": note,
                "encoded_pitch": f"0x{pitch_word:08X}",
                "live_pitch": f"0x{self.bus.read(LIVE_PITCH, 4):08X}",
                "constructor_instructions": constructor_steps,
                "callback_cases": callback["cases"],
                "pitch_consumer": {
                    "chromatic_mode": "synth",
                    "source_gate": f"0x{self.bus.read(TRACK_CHROMATIC_MODE_SOURCE, 1):02X}",
                    "live_gate": f"0x{self.bus.read(LIVE_CHROMATIC_MODE, 1):02X}",
                    "reads": pitch_reads,
                    "renderer_input_proven": (
                        bool(pitch_reads)
                        and {item["pc"] for item in pitch_reads} == {f"0x{LIVE_PITCH_READ_PC:08X}"}
                        and {item["value"] for item in pitch_reads} == {f"0x{pitch_word:08X}"}
                    ),
                },
                "one_shot_cleared": self.bus.read(EVENT_VALUE, 4) == 0,
                "queued_command": {
                    "pointer": f"0x{command_pointer:08X}",
                    "code": self.bus.read(command_pointer, 1),
                    "track_mask": f"0x{self.bus.read(command_pointer + 4, 4):08X}",
                    "queue_count": queue_count,
                },
            }
        if result["live_pitch"] != result["encoded_pitch"]:
            raise RuntimeError("stock note trigger did not publish live pitch")
        if not result["pitch_consumer"]["renderer_input_proven"]:
            raise RuntimeError("stock renderer did not consume live note pitch")
        if result["queued_command"]["code"] != 0x1F:
            raise RuntimeError("stock note trigger produced the wrong command")
        if result["queued_command"]["track_mask"] != "0x00000001":
            raise RuntimeError("stock note trigger produced the wrong track mask")
        return result

    def _write_note_event(self, event_type: int, note: int) -> None:
        for offset in range(0, 0x28, 4):
            self.bus.write(EVENT_INPUT + offset, 4, 0)
        self.bus.write(EVENT_INPUT + EVENT_TRACK, 4, 0)
        self.bus.write(EVENT_INPUT + EVENT_NOTE, 4, note)
        self.bus.write(EVENT_INPUT + EVENT_TYPE, 4, event_type)
        self.bus.write(EVENT_INPUT + NOTE_EVENT_FLAGS, 4, VOICE_RESET_FLAG if event_type == NOTE_ON else 0)
        self.bus.write(EVENT_INPUT + EVENT_SOURCE_MASK, 4, QWERTY_SOURCE_MASK)

    def release_note(self, note: int) -> dict[str, Any]:
        if not 0 <= note <= 127:
            raise ValueError("note must be 0..127")
        with self.lock:
            held_before = self.bus.read(HELD_SOURCE_MASK_BASE, 4)
            self._write_note_event(NOTE_OFF, note)
            constructor_steps = stock_call(self.cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])
            accepted = self.bus.read(TRIGGER_RECORD, 4) == NOTE_OFF
            if not accepted:
                raise RuntimeError("stock note-off constructor rejected the held source")
            held_after_constructor = self.bus.read(HELD_SOURCE_MASK_BASE, 4)
            callback = run_complete_callback(self.cpu, self._callback_sp, 400_000)
            result = {
                "note": note,
                "constructor_instructions": constructor_steps,
                "accepted": accepted,
                "held_source_mask_before": f"0x{held_before:08X}",
                "held_source_mask_after": f"0x{held_after_constructor:08X}",
                "callback_cases": callback["cases"],
                "one_shot_cleared": self.bus.read(EVENT_VALUE, 4) == 0,
                "queue_count": self.bus.read(QUEUE + 4, 4),
            }
        if result["held_source_mask_after"] != "0x00000000":
            raise RuntimeError("stock note-off constructor did not release source ownership")
        return result

    def close(self) -> None:
        self._temporary.close()


class ControllerState:
    def __init__(self, bridge: EmulatorBridge, initial_value: int = DEFAULT_CONTROL):
        self.bridge = bridge
        self.values = [clamp_control(initial_value)] * LANES
        self.selected_lane = 0
        self.held_notes: set[int] = set()
        self.events: deque[dict[str, Any]] = deque(maxlen=32)
        self.lock = threading.RLock()
        self.started_at = time.time()
        self.event_sequence = 0
        for lane, value in enumerate(self.values):
            self._publish_filter(lane, value, "initialization")

    def _record(self, event: dict[str, Any]) -> dict[str, Any]:
        self.event_sequence += 1
        event = {"sequence": self.event_sequence, "time": time.time(), **event}
        self.events.append(event)
        return event

    def _publish_filter(self, lane: int, value: int, source: str) -> dict[str, Any]:
        publication = self.bridge.publish(lane, value)
        self.values[lane] = publication["value"]
        self.selected_lane = lane
        return self._record({"type": "filter2", "source": source, **publication})

    def set_filter(self, lane: int, value: int, source: str = "api") -> dict[str, Any]:
        if not isinstance(lane, int) or isinstance(lane, bool) or not 0 <= lane < LANES:
            raise ValueError("lane must be an integer from 0 through 7")
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("value must be an integer from 0 through 127")
        if source not in {"mouse", "wheel", "keyboard", "api"}:
            source = "api"
        with self.lock:
            return self._publish_filter(lane, clamp_control(value), source)

    def note(self, key: str, action: str, velocity: int = 100) -> dict[str, Any]:
        key = key.lower()
        if key not in NOTE_KEYS:
            raise ValueError("unsupported QWERTY note key")
        if action not in {"on", "off"}:
            raise ValueError("note action must be 'on' or 'off'")
        velocity = max(1, min(127, int(velocity)))
        note = NOTE_KEYS[key]
        with self.lock:
            if action == "on":
                self.held_notes.add(note)
                stock_trigger = self.bridge.trigger_note(note)
                transport = "emulated_stock_trigger"
            else:
                if note not in self.held_notes:
                    raise ValueError("note key is not held")
                self.held_notes.discard(note)
                stock_trigger = self.bridge.release_note(note)
                transport = "emulated_stock_release"
            event = {
                "type": "note",
                "source": "qwerty",
                "key": key,
                "action": action,
                "note": note,
                "velocity": velocity if action == "on" else 0,
                "firmware_transport": transport,
            }
            event["stock_trigger" if action == "on" else "stock_release"] = stock_trigger
            return self._record(event)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "status": "ready",
                "filter2": {
                    "values": list(self.values),
                    "selected_lane": self.selected_lane,
                    "virtual_indices": [f"0x{VIRTUAL_INDEX_BASE + lane:04X}" for lane in range(LANES)],
                    "transport": "emulator publication shim",
                },
                "notes": {
                    "keys": NOTE_KEYS,
                    "held": sorted(self.held_notes),
                    "transport": "key-down: stock constructor type 1; key-up: stock constructor type 2",
                },
                "emulator": {
                    "stock_sha256": self.bridge.stock_sha256,
                    "candidate_sha256": self.bridge.candidate_sha256,
                    "runtime_armed_only": True,
                    "flashable_image_created": False,
                },
                "events": list(self.events),
                "uptime_seconds": round(time.time() - self.started_at, 3),
            }


def json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def make_handler(state: ControllerState):
    class ControllerHandler(BaseHTTPRequestHandler):
        server_version = "Rytm2Lab/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: HTTPStatus, payload: Any) -> None:
            self._send(status, json_bytes(payload), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/state":
                self._json(HTTPStatus.OK, state.snapshot())
                return
            relative = "index.html" if path == "/" else path.lstrip("/")
            candidate = (STATIC / relative).resolve()
            if STATIC.resolve() not in candidate.parents or not candidate.is_file():
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            body = candidate.read_bytes()
            self._send(HTTPStatus.OK, body, MIME_TYPES.get(candidate.suffix, "application/octet-stream"))

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4096:
                    raise ValueError("invalid request length")
                payload = json.loads(self.rfile.read(length))
                path = urlparse(self.path).path
                if path == "/api/filter2":
                    event = state.set_filter(payload.get("lane"), payload.get("value"), payload.get("source", "api"))
                elif path == "/api/note":
                    event = state.note(payload.get("key", ""), payload.get("action", ""), payload.get("velocity", 100))
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                self._json(HTTPStatus.OK, {"ok": True, "event": event, "state": state.snapshot()})
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
            except Exception as error:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(error)})

    return ControllerHandler


def default_paths() -> tuple[Path, Path]:
    return (
        RESEARCH / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin",
        ROOT / "recovered_library" / "minicoldfire_audio.py",
    )


def main() -> None:
    stock_default, emulator_default = default_paths()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--stock-main", type=Path, default=stock_default)
    parser.add_argument("--emulator", type=Path, default=emulator_default)
    args = parser.parse_args()
    bridge = EmulatorBridge(args.stock_main.resolve(), args.emulator.resolve())
    state = ControllerState(bridge)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    print(f"Rytm II lab controller: http://{args.host}:{server.server_port}")
    print("Filter2 and QWERTY note-on/note-off are emulator-backed.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        bridge.close()


if __name__ == "__main__":
    main()
