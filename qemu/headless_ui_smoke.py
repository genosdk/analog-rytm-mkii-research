#!/usr/bin/env python3
"""Boot OS 1.72 headlessly and prove one native panel/UI round trip.

The firmware image and custom QEMU binary are supplied by the caller. Nothing
proprietary is embedded in this harness.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import wave

try:
    from .panel_event_bridge import (
        PanelLink,
        RuntimeControls,
        control_to_q31,
        follow_events,
        publish_runtime_controls,
    )
except ImportError:
    from panel_event_bridge import (
        PanelLink,
        RuntimeControls,
        control_to_q31,
        follow_events,
        publish_runtime_controls,
    )

FRAME_BYTES = 1024
IDENTITY_REPLY = bytes.fromhex("70 07 05 05 00")


class PipeWriterSocket:
    """Small sendall adapter so the desktop PanelLink can drive a QEMU pipe."""

    def __init__(self, writer) -> None:
        self.writer = writer

    def sendall(self, data: bytes) -> None:
        self.writer.write(data)
        self.writer.flush()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_frame(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    return data[:FRAME_BYTES] if len(data) >= FRAME_BYTES else None


def wait_frame(path: Path, deadline: float, different_from: bytes | None = None) -> bytes:
    candidate: bytes | None = None
    unchanged_since = time.monotonic()
    while time.monotonic() < deadline:
        current = read_frame(path)
        if current is None or current == different_from:
            time.sleep(0.03)
            continue
        if current != candidate:
            candidate = current
            unchanged_since = time.monotonic()
        elif time.monotonic() - unchanged_since >= 0.5:
            return current
        time.sleep(0.03)
    raise TimeoutError("no stable qualifying framebuffer before timeout")


def wait_log_count(path: Path, needle: str, count: int, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            observed = path.read_text(encoding="utf-8", errors="replace").count(needle)
        except FileNotFoundError:
            observed = 0
        if observed >= count:
            return
        time.sleep(0.03)
    raise TimeoutError(f"observed {observed}/{count} log markers: {needle}")


def metrics(data: bytes) -> dict[str, int | str]:
    return {
        "bytes": len(data),
        "nonzero_bytes": sum(value != 0 for value in data),
        "lit_bits": sum(value.bit_count() for value in data),
        "sha256": digest(data),
    }


def unused_local_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def hmp_command(port: int, command: str, timeout: float = 2.0) -> str:
    """Run one human-monitor command for a failure-time guest snapshot."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect(("127.0.0.1", port))
            sock.settimeout(0.2)
            chunks = []
            try:
                chunks.append(sock.recv(4096))
            except TimeoutError:
                pass
            sock.sendall(command.encode("ascii") + b"\n")
            response = bytearray()
            expected_prompts = command.count("\n") + 1
            while True:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    response.extend(chunk)
                    if response.count(b"(qemu) ") >= expected_prompts:
                        break
                except TimeoutError:
                    break
            return b"".join(chunks).decode("utf-8", errors="replace")
        except OSError:
            time.sleep(0.03)
        finally:
            sock.close()
    raise TimeoutError(f"monitor socket unavailable on port {port}")


def gdb_write_memory(port: int, address: int, data: bytes,
                     timeout: float = 2.0) -> None:
    """Write halted guest memory through QEMU's local GDB stub."""
    payload = f"M{address:x},{len(data):x}:{data.hex()}".encode("ascii")
    checksum = f"{sum(payload) & 0xff:02x}".encode("ascii")
    packet = b"$" + payload + b"#" + checksum
    deadline = time.monotonic() + timeout
    last = b""
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(
                    ("127.0.0.1", port), timeout=0.2) as sock:
                sock.settimeout(0.2)
                sock.sendall(packet)
                response = bytearray()
                while time.monotonic() < deadline:
                    try:
                        response.extend(sock.recv(4096))
                    except TimeoutError:
                        continue
                    if b"$OK#" in response:
                        return
                    last = bytes(response)
        except OSError:
            time.sleep(0.03)
    raise TimeoutError(
        f"GDB memory write to 0x{address:08x} failed: {last!r}"
    )


def seed_lfo2_retrigger_state(monitor_port: int, gdb_port: int,
                              deadline: float) -> dict[str, str]:
    sentinels = {
        "phase": (0x402B4520, 0x12345678),
        "last_modulation": (0x402B452C, 0x23456789),
        "random_index": (0x402B4438, 0x3456789A),
    }
    hmp_command(monitor_port, "stop")
    try:
        for address, value in sentinels.values():
            gdb_write_memory(gdb_port, address, value.to_bytes(4, "big"))
    finally:
        hmp_command(monitor_port, "cont")
    return {
        name: wait_hmp_value(monitor_port, address, "w", value, deadline)
        for name, (address, value) in sentinels.items()
    }


def emit_event(path: Path, kind: str, name: str, value) -> None:
    record = {"t": time.time(), "kind": kind, "name": name, "value": value}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, separators=(",", ":")) + "\n")


def capture_pipe(reader_path: Path, output_path: Path) -> None:
    """Drain UART8 without backpressure while retaining failure diagnostics."""
    with (reader_path.open("rb", buffering=0) as reader,
          output_path.open("wb") as output):
        while chunk := reader.read(4096):
            output.write(chunk)


def wait_snapshot(path: Path, expected: bytes, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            if path.read_bytes() == expected:
                return
        except FileNotFoundError:
            pass
        time.sleep(0.03)
    raise TimeoutError("desktop event follower did not publish expected snapshot")


def wait_hmp_value(port: int, address: int, width: str,
                   expected: int, deadline: float) -> str:
    digits = 4 if width == "h" else 8
    marker = f"0x{expected:0{digits}x}"
    command = f"xp /1{width}x 0x{address:08x}"
    last = ""
    while time.monotonic() < deadline:
        last = hmp_command(port, command)
        if marker in last.lower():
            return marker
        time.sleep(0.03)
    raise TimeoutError(f"{command} did not reach {marker}: {last!r}")


def wait_guest_words(port: int, expected: dict[int, int],
                     deadline: float) -> dict[str, str]:
    commands = ["stop"]
    commands.extend(f"xp /1wx 0x{address:08x}" for address in expected)
    commands.append("cont")
    command = "\n".join(commands)
    last = ""
    while time.monotonic() < deadline:
        last = hmp_command(port, command).lower()
        if all(f"{address:08x}: 0x{value:08x}" in last
               for address, value in expected.items()):
            return {
                f"0x{address:08X}": f"0x{value:08X}"
                for address, value in expected.items()
            }
        time.sleep(0.01)
    raise TimeoutError(f"atomic guest words not observed: {last!r}")


def wait_lfo2_retrigger_reset(port: int, deadline: float) -> dict[str, str]:
    addresses = {
        "phase": 0x402B4520,
        "last_modulation": 0x402B452C,
        "random_index": 0x402B4438,
    }
    commands = ["stop"]
    commands.extend(f"xp /1wx 0x{address:08x}" for address in addresses.values())
    commands.append("cont")
    command = "\n".join(commands)
    last = ""
    while time.monotonic() < deadline:
        last = hmp_command(port, command).lower()
        if all(f"{address:08x}: 0x00000000" in last
               for address in addresses.values()):
            return {name: "0x00000000" for name in addresses}
        time.sleep(0.01)
    raise TimeoutError(f"native LFO2 reset was not observed atomically: {last!r}")


def seed_lfo2_retrigger_matrix(monitor_port: int, gdb_port: int,
                               deadline: float,
                               note_marker: tuple[int, int] | None = None
                               ) -> dict[int, dict[str, tuple[int, int]]]:
    sentinels = {
        lane: {
            "phase": (0x402B4520 + lane * 16, 0x12000001 + lane),
            "last_modulation": (0x402B452C + lane * 16, 0x23000001 + lane),
            "random_index": (0x402B4438 + lane * 32, 0x34000001 + lane),
        }
        for lane in range(8)
    }
    hmp_command(monitor_port, "stop")
    try:
        for lane in sentinels.values():
            for address, value in lane.values():
                gdb_write_memory(gdb_port, address, value.to_bytes(4, "big"))
        if note_marker is not None:
            marker_address, marker_value = note_marker
            gdb_write_memory(
                gdb_port, marker_address, marker_value.to_bytes(4, "big")
            )
    finally:
        hmp_command(monitor_port, "cont")
    for lane in sentinels.values():
        for address, value in lane.values():
            wait_hmp_value(monitor_port, address, "w", value, deadline)
    if note_marker is not None:
        marker_address, marker_value = note_marker
        wait_hmp_value(
            monitor_port, marker_address, "w", marker_value, deadline
        )
    return sentinels


def wait_lfo2_selective_reset(
        port: int, target_lane: int,
        sentinels: dict[int, dict[str, tuple[int, int]]],
        deadline: float) -> dict[str, object]:
    commands = ["stop"]
    for lane in sentinels.values():
        commands.extend(f"xp /1wx 0x{address:08x}"
                        for address, _ in lane.values())
    commands.append("cont")
    command = "\n".join(commands)
    last = ""
    while time.monotonic() < deadline:
        last = hmp_command(port, command).lower()
        matched = True
        for lane_index, lane in sentinels.items():
            for address, sentinel in lane.values():
                expected = 0 if lane_index == target_lane else sentinel
                if f"{address:08x}: 0x{expected:08x}" not in last:
                    matched = False
                    break
            if not matched:
                break
        if matched:
            return {
                "target_lane": target_lane,
                "target_words_cleared": 3,
                "non_target_words_preserved": 21,
                "non_target_lanes_preserved": [
                    lane for lane in range(8) if lane != target_lane
                ],
            }
        time.sleep(0.01)
    raise TimeoutError(
        f"selective LFO2 reset for lane {target_lane} not observed: {last!r}"
    )


def wait_lfo2_matrix_preserved(
        port: int, sentinels: dict[int, dict[str, tuple[int, int]]],
        deadline: float,
        event_record: tuple[int, int] | None = None) -> dict[str, object]:
    commands = ["stop"]
    if event_record is not None:
        commands.append(f"xp /1wx 0x{event_record[0]:08x}")
    for lane in sentinels.values():
        commands.extend(f"xp /1wx 0x{address:08x}"
                        for address, _ in lane.values())
    commands.append("cont")
    command = "\n".join(commands)
    last = ""
    while time.monotonic() < deadline:
        last = hmp_command(port, command).lower()
        event_matched = (
            event_record is None or
            f"{event_record[0]:08x}: 0x{event_record[1]:08x}" in last
        )
        if event_matched and all(
            f"{address:08x}: 0x{sentinel:08x}" in last
            for lane in sentinels.values()
            for address, sentinel in lane.values()
        ):
            return {
                "state_words_preserved": 24,
                "lanes_preserved": list(range(8)),
                "accepted_event_type": (
                    event_record[1] if event_record is not None else None
                ),
            }
        time.sleep(0.01)
    raise TimeoutError(f"LFO2 state preservation was not observed: {last!r}")


def wav_metrics(path: Path) -> dict[str, int | str | bool]:
    header_repaired = False
    with path.open("r+b") as stream:
        header = stream.read(44)
        data_bytes = path.stat().st_size - 44
        if (header[:4] == b"RIFF" and header[8:12] == b"WAVE" and
                header[36:40] == b"data" and
                header[4:8] == bytes(4) and header[40:44] == bytes(4) and
                0 <= data_bytes <= 0xFFFFFFFF - 36):
            # QEMU's WAV backend leaves placeholder sizes behind when the
            # monitor quits the process.  The PCM payload is complete, so
            # close the container deterministically before parsing it.
            stream.seek(4)
            stream.write((data_bytes + 36).to_bytes(4, "little"))
            stream.seek(40)
            stream.write(data_bytes.to_bytes(4, "little"))
            header_repaired = True
    with wave.open(str(path), "rb") as stream:
        frames = stream.readframes(stream.getnframes())
        return {
            "header_repaired": header_repaired,
            "channels": stream.getnchannels(),
            "sample_width_bytes": stream.getsampwidth(),
            "sample_rate_hz": stream.getframerate(),
            "frames": stream.getnframes(),
            "pcm_bytes": len(frames),
            "nonzero_pcm_bytes": sum(value != 0 for value in frames),
            "contains_nonzero_pcm": any(frames),
            "sha256": digest(frames),
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--main", type=Path, required=True)
    parser.add_argument("--boot-seconds", type=float, default=15.0)
    parser.add_argument("--event-settle-seconds", type=float, default=3.0)
    parser.add_argument("--timeout", type=float, default=35.0)
    parser.add_argument("--keep-runtime", action="store_true")
    parser.add_argument(
        "--qemu-debug",
        default="guest_errors",
        help="comma-separated QEMU -d log categories",
    )
    parser.add_argument(
        "--exercise-trigger-audio",
        action="store_true",
        help="press and release Trig 1 before the SMP-page responsiveness check",
    )
    parser.add_argument(
        "--exercise-runtime-controls",
        action="store_true",
        help="exercise desktop Filter2/LFO2 events against a full runtime candidate",
    )
    parser.add_argument(
        "--exercise-active-retrigger",
        action="store_true",
        help="prove Trig 1 clears seeded native LFO2 state before audio service",
    )
    parser.add_argument(
        "--exercise-retrigger-matrix",
        action="store_true",
        help="prove Trigs 1-8 reset only their corresponding native LFO2 lane",
    )
    parser.add_argument(
        "--exercise-retrigger-negative-controls",
        action="store_true",
        help="prove free-mode note-on and trigger-mode note-off preserve LFO2 state",
    )
    parser.add_argument(
        "--exercise-explicit-reset-matrix",
        action="store_true",
        help="prove desktop reset generations selectively clear native LFO2 state",
    )
    parser.add_argument(
        "--exercise-trigger-chord",
        action="store_true",
        help="prove overlapping Trig 1/2 press and staggered release state",
    )
    parser.add_argument(
        "--capture-audio-wav",
        action="store_true",
        help="capture the bounded renderer tap through QEMU's WAV backend",
    )
    parser.add_argument(
        "--trigger-count",
        type=int,
        default=1,
        help="number of bounded Trig-1 edges to issue with --exercise-trigger-audio",
    )
    parser.add_argument(
        "--services-per-trigger",
        type=int,
        default=8,
        help="completed vector-191 services required for each trigger edge",
    )
    parser.add_argument(
        "--mock-audio-service",
        action="store_true",
        help="run the backpressured continuous audio-service clock",
    )
    parser.add_argument(
        "--minimum-audio-services",
        type=int,
        default=0,
        help="completed vector-191 services required before the SMP check",
    )
    args = parser.parse_args()

    if args.trigger_count < 1:
        parser.error("--trigger-count must be at least 1")
    if args.services_per_trigger < 1:
        parser.error("--services-per-trigger must be at least 1")
    if args.minimum_audio_services < 0:
        parser.error("--minimum-audio-services cannot be negative")
    if args.capture_audio_wav and not args.exercise_trigger_audio:
        parser.error("--capture-audio-wav requires --exercise-trigger-audio")
    if args.exercise_active_retrigger and not args.exercise_runtime_controls:
        parser.error("--exercise-active-retrigger requires --exercise-runtime-controls")
    if args.exercise_active_retrigger and not args.exercise_trigger_audio:
        parser.error("--exercise-active-retrigger requires --exercise-trigger-audio")
    if args.exercise_retrigger_matrix and not args.exercise_runtime_controls:
        parser.error("--exercise-retrigger-matrix requires --exercise-runtime-controls")
    if args.exercise_retrigger_matrix and not args.exercise_trigger_audio:
        parser.error("--exercise-retrigger-matrix requires --exercise-trigger-audio")
    if args.exercise_retrigger_matrix and args.exercise_active_retrigger:
        parser.error("select only one retrigger gate")
    if (args.exercise_retrigger_negative_controls
            and not args.exercise_runtime_controls):
        parser.error(
            "--exercise-retrigger-negative-controls requires "
            "--exercise-runtime-controls"
        )
    if (args.exercise_retrigger_negative_controls
            and not args.exercise_trigger_audio):
        parser.error(
            "--exercise-retrigger-negative-controls requires "
            "--exercise-trigger-audio"
        )
    if (args.exercise_retrigger_negative_controls
            and (args.exercise_retrigger_matrix
                 or args.exercise_active_retrigger)):
        parser.error("select only one retrigger gate")
    if (args.exercise_explicit_reset_matrix
            and not args.exercise_runtime_controls):
        parser.error(
            "--exercise-explicit-reset-matrix requires "
            "--exercise-runtime-controls"
        )
    if (args.exercise_explicit_reset_matrix
            and (args.exercise_retrigger_matrix
                 or args.exercise_retrigger_negative_controls
                 or args.exercise_active_retrigger)):
        parser.error("select only one LFO2 state-reset gate")
    if (args.exercise_trigger_chord
            and (args.exercise_retrigger_matrix
                 or args.exercise_retrigger_negative_controls
                 or args.exercise_active_retrigger
                 or args.exercise_explicit_reset_matrix)):
        parser.error("select the trigger chord or one LFO2 state-reset gate")
    if args.capture_audio_wav and args.services_per_trigger < 8:
        parser.error("--capture-audio-wav requires at least 8 services per trigger")
    if args.exercise_trigger_audio and "unimp" not in args.qemu_debug.split(","):
        args.qemu_debug = f"unimp,{args.qemu_debug}"

    qemu = args.qemu.expanduser().resolve()
    main_image = args.main.expanduser().resolve()
    if not qemu.is_file() or not main_image.is_file():
        raise SystemExit("--qemu and --main must name existing files")

    runtime = Path(tempfile.mkdtemp(prefix="ar-mk2-ui-smoke-"))
    panel_base = runtime / "panel"
    panel_in = runtime / "panel.in"
    panel_out = runtime / "panel.out"
    panel_capture = runtime / "panel-uart8.bin"
    os.mkfifo(panel_in)
    os.mkfifo(panel_out)
    frame = runtime / "framebuffer.bin"
    events_file = runtime / "panel-events.jsonl"
    events_file.touch()
    controls_file = runtime / "runtime-controls.bin"
    audio_wav = runtime / "bounded-renderer.wav"
    log = runtime / "qemu.log"
    monitor_port = unused_local_port()
    gdb_port = unused_local_port()
    while gdb_port == monitor_port:
        gdb_port = unused_local_port()
    diagnostics = runtime / "monitor.txt"
    env = os.environ.copy()
    env["AR_MK2_MOCK_CALIBRATION"] = "1"
    env["AR_MK2_MOCK_FACTORY_STATE"] = "1"
    env["AR_MK2_FRAMEBUFFER_OUT"] = str(frame)
    if args.exercise_runtime_controls:
        publish_runtime_controls(controls_file, RuntimeControls())
        env["AR_MK2_FILTER2_CONTROL_IN"] = str(controls_file)
    else:
        env.pop("AR_MK2_FILTER2_CONTROL_IN", None)
    if args.exercise_trigger_audio:
        env["AR_MK2_AUDIO_TRIGGER_SERVICE"] = "1"
    else:
        env.pop("AR_MK2_AUDIO_TRIGGER_SERVICE", None)
    if args.capture_audio_wav:
        env["AR_MK2_AUDIO_TAP"] = "1"
        env["AR_MK2_MOCK_PROJECT_SAMPLE"] = "1"
    else:
        env.pop("AR_MK2_AUDIO_TAP", None)
        env.pop("AR_MK2_MOCK_PROJECT_SAMPLE", None)
    if args.mock_audio_service:
        env["AR_MK2_MOCK_AUDIO_SERVICE"] = "1"
    else:
        env.pop("AR_MK2_MOCK_AUDIO_SERVICE", None)
    command = [
        str(qemu), "-M", "elektron-ar-mk2", "-m", "256M",
        "-bios", str(main_image), "-display", "none",
        "-serial", f"pipe:{panel_base}",
        "-monitor", f"tcp:127.0.0.1:{monitor_port},server=on,wait=off",
        "-gdb", f"tcp:127.0.0.1:{gdb_port}",
        "-d", args.qemu_debug, "-D", str(log),
    ]
    if args.capture_audio_wav:
        command.extend(("-audio", f"wav,path={audio_wav}"))

    proc: subprocess.Popen | None = None
    panel_writer = None
    try:
        started = time.monotonic()
        deadline = started + args.timeout
        proc = subprocess.Popen(command, env=env)
        threading.Thread(
            target=capture_pipe,
            args=(panel_out, panel_capture),
            daemon=True,
        ).start()
        panel_writer = panel_in.open("wb", buffering=0)
        panel_writer.write(IDENTITY_REPLY)
        panel_link = PanelLink(PipeWriterSocket(panel_writer), verbose=False)
        threading.Thread(
            target=follow_events,
            args=(
                events_file,
                panel_link,
                True,
                controls_file if args.exercise_runtime_controls else None,
            ),
            daemon=True,
        ).start()
        time.sleep(0.1)
        time.sleep(args.boot_seconds)
        before = wait_frame(frame, deadline)
        emit_event(events_file, "button", "NO", "press")
        time.sleep(0.08)
        emit_event(events_file, "button", "NO", "release")
        time.sleep(args.event_settle_seconds)
        normal_ui = wait_frame(frame, deadline, different_from=before)
        events = [
            {"control": "NO", "press": "24 01", "release": "24 00"},
        ]
        runtime_transitions = []
        if args.exercise_runtime_controls:
            first = RuntimeControls()
            all_lanes_gate = (args.exercise_retrigger_matrix or
                              args.exercise_retrigger_negative_controls or
                              args.exercise_explicit_reset_matrix)
            configured_lanes = range(8) if all_lanes_gate else range(1)
            for lane in configured_lanes:
                first.filter2[lane] = 127
                first.waveform[lane] = 6
                first.mode[lane] = 3
                first.rate[lane] = 127
                first.depth[lane] = 32
                first.enable_mask |= 1 << lane
                if not (args.exercise_retrigger_negative_controls or
                        args.exercise_explicit_reset_matrix):
                    first.trigger_mask |= 1 << lane
                for parameter, value in (
                    ("filter2", 127), ("waveform", 6), ("mode", 3),
                    ("rate", 127), ("depth", 32), ("enable", 1),
                    ("trigger", 0 if (args.exercise_retrigger_negative_controls
                                      or args.exercise_explicit_reset_matrix)
                     else 1),
                ):
                    if parameter == "filter2":
                        emit_event(events_file, "filter2", str(lane), value)
                    else:
                        emit_event(
                            events_file, "lfo2", f"{lane}:{parameter}", value
                        )
            wait_snapshot(controls_file, first.encode(), deadline)
            active_mask = 0x00FF if all_lanes_gate else 0x0001
            retrigger_mask = (0x0000 if (args.exercise_retrigger_negative_controls
                                         or args.exercise_explicit_reset_matrix)
                              else active_mask)
            first_memory = {
                "filter2_target": wait_hmp_value(
                    monitor_port, 0x402B442C, "w", 0x7FFFFFFF, deadline),
                "config": wait_hmp_value(
                    monitor_port, 0x402B4434, "w", 0x0000001E, deadline),
                "increment": wait_hmp_value(
                    monitor_port, 0x402B4524, "w", 0x11111111, deadline),
                "depth": wait_hmp_value(
                    monitor_port, 0x402B4528, "w", 0x20408102, deadline),
                "enable_mask": wait_hmp_value(
                    monitor_port, 0x402B440E, "h", active_mask, deadline),
                "retrigger_mask": wait_hmp_value(
                    monitor_port, 0x402B4418, "h", retrigger_mask, deadline),
            }
            runtime_transitions.append({
                "name": (
                    "all_lanes_explicit_reset_hold"
                    if args.exercise_explicit_reset_matrix else
                    "all_lanes_free_random_hold"
                    if args.exercise_retrigger_negative_controls else
                    "all_lanes_random_hold" if args.exercise_retrigger_matrix
                    else "maximum_random_hold"
                ),
                **first_memory,
            })

            if args.exercise_active_retrigger:
                for index in range(args.trigger_count):
                    seeded = seed_lfo2_retrigger_state(
                        monitor_port, gdb_port, deadline
                    )
                    emit_event(events_file, "trig", "1", "press")
                    reset = wait_lfo2_retrigger_reset(monitor_port, deadline)
                    time.sleep(0.08)
                    emit_event(events_file, "trig", "1", "release")
                    wait_log_count(
                        log,
                        "AR-MK2 AUDIO: completed vector 191 service",
                        (index + 1) * args.services_per_trigger,
                        deadline,
                    )
                    events.append(
                        {
                            "control": "TRIG 1",
                            "ordinal": index + 1,
                            "press": "23 01",
                            "release": "23 00",
                            "seeded_native_state": seeded,
                            "native_retrigger_reset": reset,
                        }
                    )

            if args.exercise_retrigger_matrix:
                for lane in range(8):
                    sentinels = seed_lfo2_retrigger_matrix(
                        monitor_port, gdb_port, deadline
                    )
                    emit_event(events_file, "trig", str(lane + 1), "press")
                    selective = wait_lfo2_selective_reset(
                        monitor_port, lane, sentinels, deadline
                    )
                    time.sleep(0.08)
                    emit_event(events_file, "trig", str(lane + 1), "release")
                    wait_log_count(
                        log,
                        "AR-MK2 AUDIO: completed vector 191 service",
                        (lane + 1) * args.services_per_trigger,
                        deadline,
                    )
                    events.append({
                        "control": f"TRIG {lane + 1}",
                        "press": f"23 {1 << lane:02x}",
                        "release": "23 00",
                        "selective_native_retrigger": selective,
                    })

            if args.exercise_retrigger_negative_controls:
                for lane in range(8):
                    note_record = 0x42AC4038 + lane * 0x38
                    sentinels = seed_lfo2_retrigger_matrix(
                        monitor_port, gdb_port, deadline,
                        (note_record, 0xFFFFFFFF)
                    )
                    emit_event(events_file, "trig", str(lane + 1), "press")
                    note_on = wait_lfo2_matrix_preserved(
                        monitor_port, sentinels, deadline, (note_record, 1)
                    )
                    wait_log_count(
                        log,
                        "AR-MK2 AUDIO: completed vector 191 service",
                        (lane + 1) * args.services_per_trigger,
                        deadline,
                    )
                    emit_event(
                        events_file, "lfo2", f"{lane}:trigger", 1
                    )
                    note_off_mask = wait_hmp_value(
                        monitor_port, 0x402B4418, "h",
                        (1 << (lane + 1)) - 1, deadline
                    )
                    release_sentinels = seed_lfo2_retrigger_matrix(
                        monitor_port, gdb_port, deadline,
                        (note_record, 0xFFFFFFFF)
                    )
                    emit_event(events_file, "trig", str(lane + 1), "release")
                    note_off = wait_lfo2_matrix_preserved(
                        monitor_port, release_sentinels, deadline,
                        (note_record, 2)
                    )
                    events.append({
                        "control": f"TRIG {lane + 1}",
                        "press": f"23 {1 << lane:02x}",
                        "release": "23 00",
                        "note_record": f"0x{note_record:08x}",
                        "free_mode_note_on": note_on,
                        "note_off_retrigger_mask": note_off_mask,
                        "note_off": note_off,
                    })

            if args.exercise_explicit_reset_matrix:
                for lane in range(8):
                    sentinels = seed_lfo2_retrigger_matrix(
                        monitor_port, gdb_port, deadline
                    )
                    emit_event(events_file, "lfo2", f"{lane}:reset", 1)
                    first.reset_generation[lane] = 1
                    wait_snapshot(controls_file, first.encode(), deadline)
                    selective = wait_lfo2_selective_reset(
                        monitor_port, lane, sentinels, deadline
                    )
                    events.append({
                        "control": f"LFO2 RESET {lane + 1}",
                        "reset_generation": 1,
                        "selective_explicit_reset": selective,
                    })

                preserved_sentinels = seed_lfo2_retrigger_matrix(
                    monitor_port, gdb_port, deadline
                )
                emit_event(events_file, "lfo2", "7:depth", 33)
                first.depth[7] = 33
                wait_snapshot(controls_file, first.encode(), deadline)
                wait_hmp_value(
                    monitor_port, 0x402B4598, "w",
                    control_to_q31(33), deadline
                )
                unchanged_generation = wait_lfo2_matrix_preserved(
                    monitor_port, preserved_sentinels, deadline
                )
                events.append({
                    "control": "LFO2 DEPTH 8",
                    "value": 33,
                    "unchanged_reset_generations": list(
                        first.reset_generation
                    ),
                    "state_after_unrelated_publication": unchanged_generation,
                })

                second_sentinels = seed_lfo2_retrigger_matrix(
                    monitor_port, gdb_port, deadline
                )
                emit_event(events_file, "lfo2", "0:reset", 1)
                first.reset_generation[0] = 2
                wait_snapshot(controls_file, first.encode(), deadline)
                second_reset = wait_lfo2_selective_reset(
                    monitor_port, 0, second_sentinels, deadline
                )
                events.append({
                    "control": "LFO2 RESET 1",
                    "reset_generation": 2,
                    "selective_explicit_reset": second_reset,
                })

            second = RuntimeControls()
            if args.exercise_explicit_reset_matrix:
                second.reset_generation = [
                    (generation + 1) & 0xFF
                    for generation in first.reset_generation
                ]
            second.filter2[0] = 0
            second.waveform[0] = 4
            second.mode[0] = 2
            second.rate[0] = 0
            second.depth[0] = 127
            for lane in configured_lanes:
                second.filter2[lane] = 0
                second.waveform[lane] = 4
                second.mode[lane] = 2
                second.rate[lane] = 0
                second.depth[lane] = 127
                for parameter, value in (
                    ("filter2", 0), ("waveform", 4), ("mode", 2),
                    ("rate", 0), ("depth", 127), ("enable", 0), ("trigger", 0),
                ):
                    if parameter == "filter2":
                        emit_event(events_file, "filter2", str(lane), value)
                    else:
                        emit_event(
                            events_file, "lfo2", f"{lane}:{parameter}", value
                        )
                if args.exercise_explicit_reset_matrix:
                    emit_event(events_file, "lfo2", f"{lane}:reset", 1)
            wait_snapshot(controls_file, second.encode(), deadline)
            second_memory = {
                "filter2_target": wait_hmp_value(
                    monitor_port, 0x402B442C, "w", 0x00000000, deadline),
                "config": wait_hmp_value(
                    monitor_port, 0x402B4434, "w", 0x00000014, deadline),
                "increment": wait_hmp_value(
                    monitor_port, 0x402B4524, "w", 0x00006FD9, deadline),
                "depth": wait_hmp_value(
                    monitor_port, 0x402B4528, "w", 0x7FFFFFFF, deadline),
                "enable_mask": wait_hmp_value(
                    monitor_port, 0x402B440E, "h", 0x0000, deadline),
                "retrigger_mask": wait_hmp_value(
                    monitor_port, 0x402B4418, "h", 0x0000, deadline),
            }
            runtime_transitions.append({
                "name": "sine_half_minimum",
                **second_memory,
                **({"reset_generations": second.reset_generation}
                   if args.exercise_explicit_reset_matrix else {}),
            })
        if args.exercise_trigger_chord:
            note_words = (0x42AC4038, 0x42AC4070)
            hmp_command(monitor_port, "stop")
            try:
                for address in note_words:
                    gdb_write_memory(
                        gdb_port, address, (0xFFFFFFFF).to_bytes(4, "big")
                    )
            finally:
                hmp_command(monitor_port, "cont")
            chord_states = [{
                "name": "seeded",
                "words": wait_guest_words(
                    monitor_port,
                    {note_words[0]: 0xFFFFFFFF, note_words[1]: 0xFFFFFFFF},
                    deadline,
                ),
            }]
            for name, trig, pressed, expected in (
                ("trig1_on", 1, True, (1, 0xFFFFFFFF)),
                ("trig1_trig2_on", 2, True, (1, 1)),
                ("trig1_off_trig2_on", 1, False, (2, 1)),
                ("trig1_trig2_off", 2, False, (2, 2)),
            ):
                emit_event(
                    events_file, "trig", str(trig),
                    "press" if pressed else "release",
                )
                chord_states.append({
                    "name": name,
                    "words": wait_guest_words(
                        monitor_port,
                        {note_words[0]: expected[0], note_words[1]: expected[1]},
                        deadline,
                    ),
                })
            events.append({
                "control": "TRIG 1 + TRIG 2 CHORD",
                "host_gated_uart_frames": [
                    "23 01", "23 03", "23 02", "23 00"
                ],
                "native_note_states": chord_states,
            })
        if (args.exercise_trigger_audio and not args.exercise_active_retrigger
                and not args.exercise_retrigger_matrix
                and not args.exercise_retrigger_negative_controls):
            for index in range(args.trigger_count):
                emit_event(events_file, "trig", "1", "press")
                time.sleep(0.08)
                emit_event(events_file, "trig", "1", "release")
                wait_log_count(
                    log,
                    "AR-MK2 AUDIO: completed vector 191 service",
                    (index + 1) * args.services_per_trigger,
                    deadline,
                )
                events.append(
                    {
                        "control": "TRIG 1",
                        "ordinal": index + 1,
                        "press": "23 01",
                        "release": "23 00",
                    }
                )
        if args.minimum_audio_services:
            wait_log_count(
                log,
                "AR-MK2 AUDIO: completed vector 191 service",
                args.minimum_audio_services,
                deadline,
            )
        time.sleep(1.0)
        emit_event(events_file, "button", "SMP", "press")
        time.sleep(0.08)
        emit_event(events_file, "button", "SMP", "release")
        time.sleep(args.event_settle_seconds)
        smp_page = wait_frame(frame, deadline, different_from=normal_ui)
        events.append(
            {"control": "SMP", "press": "25 10", "release": "25 00"}
        )
        audio_metrics = None
        if args.capture_audio_wav:
            hmp_command(monitor_port, "quit")
            proc.wait(3.0)
            audio_metrics = wav_metrics(audio_wav)
            if not audio_metrics["contains_nonzero_pcm"]:
                raise RuntimeError("bounded renderer WAV contains no nonzero PCM")
        print(json.dumps({
            "result": "PASS",
            "events": events,
            "completed_audio_services": max(
                args.minimum_audio_services,
                (8 if (args.exercise_retrigger_matrix or
                       args.exercise_retrigger_negative_controls)
                 else args.trigger_count)
                * args.services_per_trigger
                if args.exercise_trigger_audio else 0,
            ),
            "runtime_control_transitions": runtime_transitions,
            "audio_wav": audio_metrics,
            "startup_modal": metrics(before),
            "normal_ui": metrics(normal_ui),
            "smp_page": metrics(smp_page),
            "firmware_embedded": False,
        }, indent=2))
    except Exception:
        if proc is not None and proc.poll() is None:
            try:
                snapshots = []
                for command in (
                    "info registers",
                    "x/24i $pc-24",
                    "x/32wx $sp",
                ):
                    snapshots.append(f"## {command}\n")
                    snapshots.append(hmp_command(monitor_port, command))
                diagnostics.write_text(
                    "\n".join(snapshots), encoding="utf-8"
                )
            except Exception as exc:
                diagnostics.write_text(
                    f"monitor snapshot failed: {exc}\n", encoding="utf-8"
                )
        raise
    finally:
        if panel_writer is not None:
            panel_writer.close()
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        if args.keep_runtime:
            print(f"runtime={runtime}")
        else:
            shutil.rmtree(runtime, ignore_errors=True)


if __name__ == "__main__":
    main()
