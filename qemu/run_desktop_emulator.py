#!/usr/bin/env python3
"""Standalone host launcher for the AR MKII firmware emulator.

Elektron firmware is never bundled. The launcher accepts a user-supplied
official update .syx (or a decompressed MAIN for development), starts QEMU
paused, connects the front-panel UART bridge, then releases the firmware CPU.
The desktop UI and panel bridge run in-process so a frozen application needs
only this executable plus the custom qemu-system-m68k backend.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
import traceback

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))

from audio_callback_probe import EXPECTED_MAIN_SHA256
from lfo2_extended_waveform_probe import build_candidate as build_filter2_candidate
from firmware_loader import extract_main
from panel_event_bridge import (
    PanelLink,
    RuntimeControls,
    connect_unix,
    follow_events,
    publish_runtime_controls,
)
from desktop_panel import (
    PanelApp,
    SKIN_H,
    SKIN_W,
    skin_asset_path,
    skin_runtime_self_test,
    validate_skin_geometry,
)

DIAGNOSTIC_SCHEMA = 1


def file_identity(path: Path) -> dict[str, object]:
    """Return reproducible metadata without exposing file contents or paths."""
    try:
        if not path.is_file():
            return {"name": path.name, "present": False}
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        size = path.stat().st_size
    except OSError as error:
        return {
            "name": path.name,
            "present": False,
            "error": type(error).__name__,
        }
    return {
        "name": path.name,
        "present": True,
        "bytes": size,
        "sha256": digest.hexdigest(),
    }


def skin_identity(filename: str) -> dict[str, object]:
    try:
        return file_identity(skin_asset_path(filename))
    except (FileNotFoundError, OSError) as error:
        return {"name": filename, "present": False, "error": str(error)}


def write_diagnostic_report(
    error: BaseException,
    trace: str,
    directory: Path | None = None,
) -> Path:
    """Persist a privacy-bounded first-launch report outside the runtime tree."""
    if directory is None:
        directory = (
            Path.home() / "Library" / "Logs" / "Photon OS" / "AR MKII Emulator"
        )
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = directory / f"diagnostic-{stamp}.json"
    qemu = bundle_dir() / "qemu-system-m68k"
    report = {
        "schema": DIAGNOSTIC_SCHEMA,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "application": "AR MKII Emulator",
        "frozen": bool(getattr(sys, "frozen", False)),
        "system": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "mac_version": platform.mac_ver()[0],
            "python": platform.python_version(),
            "tk": str(tk.TkVersion),
            "tcl": str(tk.TclVersion),
        },
        "backend": file_identity(qemu),
        "skins": [
            skin_identity(filename)
            for filename in (
                "photon_panel_neutral.png",
                "photon_panel_active.png",
            )
        ],
        "error": {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": trace,
        },
        "excluded": [
            "firmware bytes",
            "firmware path",
            "OLED framebuffer",
            "panel event history",
        ],
    }
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    return output


def show_failure_dialog(error: BaseException, report: Path) -> None:
    """Make frozen-app failures visible even though the bundle has no console."""
    root = tk.Tk()
    root.withdraw()
    try:
        messagebox.showerror(
            "AR MKII Emulator could not start",
            f"{error}\n\nDiagnostic saved to:\n{report}",
            parent=root,
        )
    finally:
        root.destroy()


def install_callback_reporter(root: tk.Tk) -> None:
    """Convert otherwise-console-only Tk callback errors into useful reports."""
    def report_callback_exception(exc_type, error, tb) -> None:
        trace = "".join(traceback.format_exception(exc_type, error, tb))
        report = write_diagnostic_report(error, trace)
        messagebox.showerror(
            "AR MKII Emulator encountered an error",
            f"{error}\n\nDiagnostic saved to:\n{report}",
            parent=root,
        )

    root.report_callback_exception = report_callback_exception


def wait_for(path: Path, proc: subprocess.Popen, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            raise RuntimeError(f"QEMU exited early with status {proc.returncode}")
        time.sleep(0.03)
    raise TimeoutError(f"Timed out waiting for {path}")


def choose_firmware() -> Path:
    root = tk.Tk()
    root.withdraw()
    try:
        selected = filedialog.askopenfilename(
            title="Select official Analog Rytm MKII firmware",
            filetypes=[("Elektron SysEx update", "*.syx"), ("All files", "*")],
        )
    finally:
        root.destroy()
    if not selected:
        raise SystemExit(0)
    return Path(selected).expanduser().resolve()


def bundle_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resolve_qemu(value: Path | None) -> Path:
    if value is not None:
        qemu = value.expanduser().resolve()
    else:
        qemu = bundle_dir() / "qemu-system-m68k"
    if not qemu.is_file():
        raise SystemExit(f"QEMU binary not found: {qemu}")
    return qemu


def self_test(qemu: Path) -> None:
    version = subprocess.run(
        [str(qemu), "-version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
        check=True,
    )
    machines = subprocess.run(
        [str(qemu), "-machine", "help"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
        check=True,
    )
    if "QEMU emulator version" not in version.stdout:
        raise RuntimeError("bundled QEMU did not report a valid version")
    if "elektron-ar-mk2" not in machines.stdout:
        raise RuntimeError("bundled QEMU is missing the elektron-ar-mk2 machine")
    expected_png_header = b"\x89PNG\r\n\x1a\n"
    expected_size = SKIN_W.to_bytes(4, "big") + SKIN_H.to_bytes(4, "big")
    for filename in ("photon_panel_neutral.png", "photon_panel_active.png"):
        data = skin_asset_path(filename).read_bytes()[:24]
        if data[:8] != expected_png_header or data[16:24] != expected_size:
            raise RuntimeError(
                f"bundled photographic skin is invalid: {filename}"
            )
    validate_skin_geometry()
    with tempfile.TemporaryDirectory(prefix="ar-mk2-skin-test-") as directory:
        runtime = Path(directory)
        skin_runtime_self_test(runtime / "frame.bin", runtime / "events.jsonl")
        diagnostic = write_diagnostic_report(
            RuntimeError("packaged diagnostic probe"),
            "packaged diagnostic traceback probe",
            runtime,
        )
        report = json.loads(diagnostic.read_text(encoding="utf-8"))
        if not report["backend"]["present"]:
            raise RuntimeError("packaged diagnostic could not identify QEMU")
        if not all(row["present"] for row in report["skins"]):
            raise RuntimeError("packaged diagnostic could not identify skins")
        if report["error"]["message"] != "packaged diagnostic probe":
            raise RuntimeError("packaged diagnostic error record is invalid")


def hmp_continue(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(str(path))
            s.settimeout(0.5)
            try:
                s.recv(4096)
            except socket.timeout:
                pass
            s.sendall(b"cont\n")
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.03)
        finally:
            s.close()
    raise TimeoutError(f"could not continue QEMU through monitor: {last_error}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qemu", type=Path,
                    help="custom qemu-system-m68k; defaults to bundled sibling")
    fw = ap.add_mutually_exclusive_group(required=False)
    fw.add_argument("--firmware", type=Path,
                    help="official Elektron Analog Rytm MKII update .syx")
    fw.add_argument("--main", type=Path,
                    help="already-decompressed MAIN image (development only)")
    ap.add_argument("--scale", type=int, default=6)
    ap.add_argument("--keep-runtime", action="store_true")
    ap.add_argument(
        "--no-mock-calibration",
        action="store_true",
        help=(
            "disable emulator-only passed-calibration SPI state and expose the "
            "firmware's real missing-calibration path (research/hardware-validation mode)"
        ),
    )
    ap.add_argument(
        "--no-mock-factory-state",
        action="store_true",
        help=(
            "disable emulator-only empty factory metadata/eMMC state and expose "
            "the firmware's physical-storage startup path"
        ),
    )
    ap.add_argument(
        "--audio",
        action="store_true",
        help=(
            "enable the 48 kHz stereo renderer tap and eight bounded stock audio "
            "service passes per rising pad/QWERTY edge"
        ),
    )
    ap.add_argument(
        "--no-filter2",
        action="store_true",
        help="boot untouched MAIN and disable the emulator-only eight-lane Filter 2 controller",
    )
    ap.add_argument(
        "--mock-audio-service",
        action="store_true",
        help=(
            "enable the experimental external audio-service clock; this is "
            "slow under TCG and intended only for tracing"
        ),
    )
    ap.add_argument(
        "--self-test",
        action="store_true",
        help="verify the bundled QEMU backend and photographic UI, then exit",
    )
    args = ap.parse_args()

    qemu = resolve_qemu(args.qemu)
    if args.self_test:
        self_test(qemu)
        return

    runtime = Path(tempfile.mkdtemp(prefix="ar-mk2-emulator-"))
    uart = runtime / "panel.sock"
    monitor = runtime / "monitor.sock"
    frame = runtime / "front-buffer.bin"
    events = runtime / "panel-events.jsonl"
    filter2_controls = runtime / "filter2-controls.bin"
    log = runtime / "qemu.log"

    if args.main:
        main_image = args.main.expanduser().resolve()
        if not main_image.is_file():
            raise SystemExit(f"MAIN image not found: {main_image}")
    else:
        syx = (args.firmware.expanduser().resolve()
               if args.firmware else choose_firmware())
        if not syx.is_file():
            raise SystemExit(f"firmware file not found: {syx}")
        main_image = runtime / "main.bin"
        metadata = extract_main(syx, main_image)
        if sys.stderr is not None:
            print(
                f"Extracted MAIN: {metadata['size']} bytes, "
                f"sha256={metadata['sha256']}",
                file=sys.stderr,
            )

    filter2_enabled = not args.no_filter2
    if filter2_enabled:
        stock = main_image.read_bytes()
        digest = hashlib.sha256(stock).hexdigest()
        if digest != EXPECTED_MAIN_SHA256:
            raise SystemExit(
                "The live Filter 2 extension currently requires official OS 1.72 "
                f"MAIN ({EXPECTED_MAIN_SHA256}); selected MAIN is {digest}. "
                "Use --no-filter2 to boot it untouched."
            )
        candidate, _ = build_filter2_candidate(stock, True)
        main_image = runtime / "main-filter2-runtime.bin"
        main_image.write_bytes(candidate)
        publish_runtime_controls(filter2_controls, RuntimeControls())

    env = os.environ.copy()
    env["AR_MK2_FRAMEBUFFER_OUT"] = str(frame)
    if filter2_enabled:
        env["AR_MK2_FILTER2_CONTROL_IN"] = str(filter2_controls)
    else:
        env.pop("AR_MK2_FILTER2_CONTROL_IN", None)
    if args.no_mock_calibration:
        env.pop("AR_MK2_MOCK_CALIBRATION", None)
    else:
        # Desktop emulation has no physical analog circuitry to measure. Feed
        # the untouched firmware a structurally valid synthetic factory
        # calibration record through the emulated SPI NOR. Raw/research mode
        # can disable this explicitly; the firmware image itself is unchanged.
        env["AR_MK2_MOCK_CALIBRATION"] = "1"
    if args.no_mock_factory_state:
        env.pop("AR_MK2_MOCK_FACTORY_STATE", None)
    else:
        # Supply only non-proprietary metadata and volatile storage. The empty
        # manifest boots the normal UI but provides no sample assignment/PCM.
        env["AR_MK2_MOCK_FACTORY_STATE"] = "1"
    if args.mock_audio_service:
        env["AR_MK2_MOCK_AUDIO_SERVICE"] = "1"
    else:
        env.pop("AR_MK2_MOCK_AUDIO_SERVICE", None)
    if args.audio:
        env["AR_MK2_AUDIO_TAP"] = "1"
        env["AR_MK2_AUDIO_TRIGGER_SERVICE"] = "1"
    else:
        env.pop("AR_MK2_AUDIO_TAP", None)
        env.pop("AR_MK2_AUDIO_TRIGGER_SERVICE", None)

    qemu_cmd = [
        str(qemu),
        "-machine", "elektron-ar-mk2",
        "-m", "256M",
        "-bios", str(main_image),
        "-display", "none",
        "-serial", f"unix:{uart},server=on,wait=off",
        "-monitor", f"unix:{monitor},server=on,wait=off",
        "-S",
        "-d", "guest_errors",
        "-D", str(log),
    ]

    qemu_proc: subprocess.Popen | None = None
    panel_sock: socket.socket | None = None
    try:
        qemu_proc = subprocess.Popen(qemu_cmd, env=env)
        wait_for(uart, qemu_proc)
        wait_for(monitor, qemu_proc)

        # Attach the panel before the first guest instruction so the firmware's
        # initial identity query cannot be lost to a host-side startup race.
        panel_sock = connect_unix(uart, 5.0)
        link = PanelLink(panel_sock)
        threading.Thread(target=link.reader, daemon=True).start()
        threading.Thread(
            target=follow_events,
            args=(events, link, True, filter2_controls if filter2_enabled else None),
            daemon=True,
        ).start()

        hmp_continue(monitor)

        root = tk.Tk()
        install_callback_reporter(root)
        PanelApp(root, frame, events, args.scale, filter2_enabled, args.audio)
        root.mainloop()

        if qemu_proc.poll() is not None and qemu_proc.returncode:
            raise RuntimeError(
                f"QEMU exited with status {qemu_proc.returncode}; log: {log}"
            )
    finally:
        if panel_sock is not None:
            try:
                panel_sock.close()
            except OSError:
                pass
        if qemu_proc is not None and qemu_proc.poll() is None:
            qemu_proc.terminate()
            try:
                qemu_proc.wait(2.0)
            except subprocess.TimeoutExpired:
                qemu_proc.kill()
        if args.keep_runtime:
            if sys.stderr is not None:
                print(f"Runtime retained at: {runtime}", file=sys.stderr)
        else:
            shutil.rmtree(runtime, ignore_errors=True)


def guarded_main() -> None:
    """Give Finder-launched failures a native error path and sanitized report."""
    try:
        main()
    except SystemExit as error:
        if error.code not in (None, 0) and getattr(sys, "frozen", False):
            report = write_diagnostic_report(error, traceback.format_exc())
            if "--self-test" not in sys.argv:
                show_failure_dialog(error, report)
        raise
    except Exception as error:
        if getattr(sys, "frozen", False):
            report = write_diagnostic_report(error, traceback.format_exc())
            if "--self-test" not in sys.argv:
                show_failure_dialog(error, report)
        raise


if __name__ == "__main__":
    guarded_main()
