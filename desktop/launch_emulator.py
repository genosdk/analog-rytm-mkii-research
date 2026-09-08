#!/usr/bin/env python3
"""Launch the AR MKII custom QEMU machine, panel bridge, and desktop UI."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


def terminate(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qemu", type=Path, required=True, help="custom qemu-system-m68k binary")
    ap.add_argument("--main", type=Path, required=True, help="decompressed OS MAIN image")
    ap.add_argument("--bridge", type=Path, required=True, help="panel_event_bridge.py")
    ap.add_argument("--gui", type=Path, required=True, help="ar_panel_gui.py")
    ap.add_argument("--workdir", type=Path)
    ap.add_argument("--scale", type=int, default=6)
    args = ap.parse_args()

    for path in (args.qemu, args.main, args.bridge, args.gui):
        if not path.exists():
            raise SystemExit(f"missing required file: {path}")

    temporary = None
    if args.workdir is None:
        temporary = tempfile.TemporaryDirectory(prefix="ar-mk2-emulator-")
        workdir = Path(temporary.name)
    else:
        workdir = args.workdir
        workdir.mkdir(parents=True, exist_ok=True)

    socket_path = workdir / "panel.sock"
    framebuffer = workdir / "framebuffer.bin"
    events = workdir / "panel_events.jsonl"
    events.touch(exist_ok=True)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass

    env = os.environ.copy()
    env["AR_MK2_FRAMEBUFFER_OUT"] = str(framebuffer)

    qemu_cmd = [
        str(args.qemu),
        "-M", "elektron-ar-mk2",
        "-m", "128M",
        "-bios", str(args.main),
        "-display", "none",
        "-monitor", "none",
        "-serial", f"unix:{socket_path},server=on,wait=off",
    ]

    qemu = bridge = gui = None
    try:
        qemu = subprocess.Popen(qemu_cmd, env=env, cwd=workdir)
        bridge = subprocess.Popen([
            sys.executable,
            str(args.bridge),
            "--socket", str(socket_path),
            "--events", str(events),
        ], cwd=workdir)
        gui = subprocess.Popen([
            sys.executable,
            str(args.gui),
            "--frame", str(framebuffer),
            "--events", str(events),
            "--scale", str(args.scale),
        ], cwd=workdir)

        while gui.poll() is None:
            if qemu.poll() is not None:
                raise SystemExit(f"QEMU exited unexpectedly with status {qemu.returncode}")
            if bridge.poll() is not None:
                raise SystemExit(f"panel bridge exited unexpectedly with status {bridge.returncode}")
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        terminate(gui)
        terminate(bridge)
        terminate(qemu)
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    main()
