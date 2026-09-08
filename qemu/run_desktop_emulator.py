#!/usr/bin/env python3
"""One-command host launcher for the AR MKII firmware emulator.

This development launcher expects a custom qemu-system-m68k binary and an
already-decompressed OS 1.72 MAIN image. The release app will perform MAIN
extraction locally from a user-supplied official Elektron .syx file.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time


def wait_for(path: Path, proc: subprocess.Popen, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            raise RuntimeError(f"QEMU exited early with status {proc.returncode}")
        time.sleep(0.03)
    raise TimeoutError(f"Timed out waiting for {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qemu", type=Path, required=True)
    ap.add_argument("--main", type=Path, required=True,
                    help="decompressed AR MKII MAIN image")
    ap.add_argument("--scale", type=int, default=6)
    ap.add_argument("--keep-runtime", action="store_true")
    args = ap.parse_args()

    qemu = args.qemu.expanduser().resolve()
    main_image = args.main.expanduser().resolve()
    if not qemu.is_file():
        raise SystemExit(f"QEMU binary not found: {qemu}")
    if not main_image.is_file():
        raise SystemExit(f"MAIN image not found: {main_image}")

    here = Path(__file__).resolve().parent
    bridge = here / "panel_event_bridge.py"
    panel_ui = here / "desktop_panel.py"
    if not bridge.is_file() or not panel_ui.is_file():
        raise SystemExit("desktop bridge files are incomplete")

    runtime = Path(tempfile.mkdtemp(prefix="ar-mk2-emulator-"))
    uart = runtime / "panel.sock"
    frame = runtime / "front-buffer.bin"
    events = runtime / "panel-events.jsonl"
    log = runtime / "qemu.log"

    env = os.environ.copy()
    env["AR_MK2_FRAMEBUFFER_OUT"] = str(frame)

    qemu_cmd = [
        str(qemu),
        "-machine", "elektron-ar-mk2",
        "-m", "128M",
        "-bios", str(main_image),
        "-display", "none",
        "-serial", f"unix:{uart},server=on,wait=off",
        "-d", "guest_errors",
        "-D", str(log),
    ]

    children: list[subprocess.Popen] = []
    try:
        qemu_proc = subprocess.Popen(qemu_cmd, env=env)
        children.append(qemu_proc)
        wait_for(uart, qemu_proc)

        bridge_proc = subprocess.Popen([
            sys.executable, str(bridge),
            "--socket", str(uart),
            "--events", str(events),
        ])
        children.append(bridge_proc)

        ui_proc = subprocess.Popen([
            sys.executable, str(panel_ui),
            "--frame", str(frame),
            "--events", str(events),
            "--scale", str(args.scale),
        ])
        children.append(ui_proc)

        rc = ui_proc.wait()
        if qemu_proc.poll() is not None and qemu_proc.returncode:
            raise RuntimeError(
                f"QEMU exited with status {qemu_proc.returncode}; log: {log}"
            )
        raise SystemExit(rc)
    finally:
        for proc in reversed(children):
            if proc.poll() is None:
                proc.terminate()
        deadline = time.monotonic() + 2.0
        for proc in reversed(children):
            if proc.poll() is None:
                try:
                    proc.wait(max(0.0, deadline - time.monotonic()))
                except (subprocess.TimeoutExpired, ValueError):
                    proc.kill()
        if args.keep_runtime:
            print(f"Runtime retained at: {runtime}", file=sys.stderr)
        else:
            shutil.rmtree(runtime, ignore_errors=True)


if __name__ == "__main__":
    main()
