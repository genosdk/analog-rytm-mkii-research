#!/usr/bin/env python3
"""One-command host launcher for the AR MKII firmware emulator.

Elektron firmware is never bundled. The launcher accepts either a user-supplied
official update .syx or a decompressed MAIN image for development/testing.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from firmware_loader import extract_main


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
    fw = ap.add_mutually_exclusive_group(required=True)
    fw.add_argument("--firmware", type=Path,
                    help="official Elektron Analog Rytm MKII update .syx")
    fw.add_argument("--main", type=Path,
                    help="already-decompressed MAIN image (development only)")
    ap.add_argument("--scale", type=int, default=6)
    ap.add_argument("--keep-runtime", action="store_true")
    args = ap.parse_args()

    qemu = args.qemu.expanduser().resolve()
    if not qemu.is_file():
        raise SystemExit(f"QEMU binary not found: {qemu}")

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

    if args.firmware:
        syx = args.firmware.expanduser().resolve()
        if not syx.is_file():
            raise SystemExit(f"firmware file not found: {syx}")
        main_image = runtime / "main.bin"
        metadata = extract_main(syx, main_image)
        print(
            f"Extracted MAIN: {metadata['size']} bytes, "
            f"sha256={metadata['sha256']}",
            file=sys.stderr,
        )
    else:
        main_image = args.main.expanduser().resolve()
        if not main_image.is_file():
            raise SystemExit(f"MAIN image not found: {main_image}")

    env = os.environ.copy()
    env["AR_MK2_FRAMEBUFFER_OUT"] = str(frame)

    qemu_cmd = [
        str(qemu),
        "-machine", "elektron-ar-mk2",
        "-m", "256M",
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
