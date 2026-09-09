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


def metrics(data: bytes) -> dict[str, int | str]:
    return {
        "bytes": len(data),
        "nonzero_bytes": sum(value != 0 for value in data),
        "lit_bits": sum(value.bit_count() for value in data),
        "sha256": digest(data),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--main", type=Path, required=True)
    parser.add_argument("--boot-seconds", type=float, default=15.0)
    parser.add_argument("--event-settle-seconds", type=float, default=3.0)
    parser.add_argument("--timeout", type=float, default=35.0)
    parser.add_argument("--keep-runtime", action="store_true")
    args = parser.parse_args()

    qemu = args.qemu.expanduser().resolve()
    main_image = args.main.expanduser().resolve()
    if not qemu.is_file() or not main_image.is_file():
        raise SystemExit("--qemu and --main must name existing files")

    runtime = Path(tempfile.mkdtemp(prefix="ar-mk2-ui-smoke-"))
    panel_base = runtime / "panel"
    panel_in = runtime / "panel.in"
    panel_out = runtime / "panel.out"
    os.mkfifo(panel_in)
    os.mkfifo(panel_out)
    frame = runtime / "framebuffer.bin"
    log = runtime / "qemu.log"
    env = os.environ.copy()
    env["AR_MK2_MOCK_CALIBRATION"] = "1"
    env["AR_MK2_MOCK_FACTORY_STATE"] = "1"
    env["AR_MK2_FRAMEBUFFER_OUT"] = str(frame)
    command = [
        str(qemu), "-M", "elektron-ar-mk2", "-m", "256M",
        "-bios", str(main_image), "-display", "none",
        "-serial", f"pipe:{panel_base}", "-monitor", "none",
        "-d", "guest_errors", "-D", str(log),
    ]

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
        after = wait_frame(frame, deadline, different_from=before)
        print(json.dumps({
            "result": "PASS",
            "event": {"control": "NO", "press": "24 01", "release": "24 00"},
            "before": metrics(before),
            "after": metrics(after),
            "firmware_embedded": False,
        }, indent=2))
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
