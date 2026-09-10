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

FRAME_BYTES = 1024
IDENTITY_REPLY = bytes.fromhex("70 07 05 05 00")


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
            while True:
                try:
                    chunks.append(sock.recv(4096))
                except TimeoutError:
                    break
            return b"".join(chunks).decode("utf-8", errors="replace")
        except OSError:
            time.sleep(0.03)
        finally:
            sock.close()
    raise TimeoutError(f"monitor socket unavailable on port {port}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--main", type=Path, required=True)
    parser.add_argument("--boot-seconds", type=float, default=15.0)
    parser.add_argument("--event-settle-seconds", type=float, default=3.0)
    parser.add_argument("--timeout", type=float, default=35.0)
    parser.add_argument("--keep-runtime", action="store_true")
    parser.add_argument(
        "--plugin",
        type=Path,
        help="optional QEMU plugin shared object",
    )
    parser.add_argument(
        "--plugin-output",
        type=Path,
        help="output path passed to the plugin as out=PATH",
    )
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
    if args.exercise_trigger_audio and "unimp" not in args.qemu_debug.split(","):
        args.qemu_debug = f"unimp,{args.qemu_debug}"

    qemu = args.qemu.expanduser().resolve()
    main_image = args.main.expanduser().resolve()
    if not qemu.is_file() or not main_image.is_file():
        raise SystemExit("--qemu and --main must name existing files")
    plugin = args.plugin.expanduser().resolve() if args.plugin else None
    plugin_output = (
        args.plugin_output.expanduser().resolve() if args.plugin_output else None
    )
    if bool(plugin) != bool(plugin_output):
        parser.error("--plugin and --plugin-output must be supplied together")
    if plugin is not None and not plugin.is_file():
        parser.error("--plugin must name an existing file")

    runtime = Path(tempfile.mkdtemp(prefix="ar-mk2-ui-smoke-"))
    panel_base = runtime / "panel"
    panel_in = runtime / "panel.in"
    panel_out = runtime / "panel.out"
    os.mkfifo(panel_in)
    os.mkfifo(panel_out)
    frame = runtime / "framebuffer.bin"
    log = runtime / "qemu.log"
    monitor_port = unused_local_port()
    diagnostics = runtime / "monitor.txt"
    env = os.environ.copy()
    env["AR_MK2_MOCK_CALIBRATION"] = "1"
    env["AR_MK2_MOCK_FACTORY_STATE"] = "1"
    env["AR_MK2_FRAMEBUFFER_OUT"] = str(frame)
    if args.mock_audio_service:
        env["AR_MK2_MOCK_AUDIO_SERVICE"] = "1"
    else:
        env.pop("AR_MK2_MOCK_AUDIO_SERVICE", None)
    command = [
        str(qemu), "-M", "elektron-ar-mk2", "-m", "256M",
        "-bios", str(main_image), "-display", "none",
        "-serial", f"pipe:{panel_base}",
        "-monitor", f"tcp:127.0.0.1:{monitor_port},server=on,wait=off",
        "-d", args.qemu_debug, "-D", str(log),
    ]
    if plugin is not None:
        plugin_output.parent.mkdir(parents=True, exist_ok=True)
        command.extend(
            ["-plugin", f"file={plugin},out={plugin_output}"]
        )

    proc: subprocess.Popen | None = None
    panel_writer = None
    try:
        started = time.monotonic()
        deadline = started + args.timeout
        proc = subprocess.Popen(command, env=env)
        threading.Thread(
            target=lambda: panel_out.open("rb", buffering=0).read(),
            daemon=True,
        ).start()
        panel_writer = panel_in.open("wb", buffering=0)
        panel_writer.write(IDENTITY_REPLY)
        time.sleep(args.boot_seconds)
        before = wait_frame(frame, deadline)
        panel_writer.write(bytes.fromhex("24 01"))
        time.sleep(0.08)
        panel_writer.write(bytes.fromhex("24 00"))
        time.sleep(args.event_settle_seconds)
        normal_ui = wait_frame(frame, deadline, different_from=before)
        events = [
            {"control": "NO", "press": "24 01", "release": "24 00"},
        ]
        if args.exercise_trigger_audio:
            for index in range(args.trigger_count):
                panel_writer.write(bytes.fromhex("23 01"))
                time.sleep(0.08)
                panel_writer.write(bytes.fromhex("23 00"))
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
        panel_writer.write(bytes.fromhex("25 10"))
        time.sleep(0.08)
        panel_writer.write(bytes.fromhex("25 00"))
        time.sleep(args.event_settle_seconds)
        smp_page = wait_frame(frame, deadline, different_from=normal_ui)
        events.append(
            {"control": "SMP", "press": "25 10", "release": "25 00"}
        )
        print(json.dumps({
            "result": "PASS",
            "events": events,
            "completed_audio_services": max(
                args.minimum_audio_services,
                args.trigger_count * args.services_per_trigger
                if args.exercise_trigger_audio else 0,
            ),
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
