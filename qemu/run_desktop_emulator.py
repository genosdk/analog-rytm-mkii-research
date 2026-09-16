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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class FirmwarePreparation:
    runtime: Path
    main_image: Path
    filter2_controls: Path
    metadata: dict[str, object]
    main_sha256: str
    filter2_enabled: bool


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


def confirm_stock_boot(version: str | None, digest: str) -> bool:
    """Offer a Finder-usable fallback when Filter 2 has no verified patch base."""
    root = tk.Tk()
    root.withdraw()
    label = f"OS {version}" if version else "this firmware"
    try:
        return messagebox.askyesno(
            "Filter 2 compatibility",
            (
                f"{label} is a valid Analog Rytm MKII update, but the emulator-only "
                "Filter 2/LFO2 extension has only been verified against OS 1.72.\n\n"
                "Boot the selected firmware unchanged with Filter 2/LFO2 disabled?\n\n"
                f"MAIN SHA-256: {digest}"
            ),
            parent=root,
        )
    finally:
        root.destroy()


def confirm_audio_enable() -> bool:
    """Offer bounded emulator audio to a Finder-launched review session."""
    root = tk.Tk()
    root.withdraw()
    try:
        return messagebox.askyesno(
            "Audio audition",
            (
                "Enable experimental bounded audio for this session?\n\n"
                "Pad and QWERTY presses will run eight stock renderer blocks, "
                "and the Filter 2 drawer audition control will be available. "
                "The current output is mono on both channels and is intended "
                "for emulator review, not physical-hardware validation."
            ),
            parent=root,
        )
    finally:
        root.destroy()


def resolve_audio_mode(
    explicit: bool | None,
    *,
    selected_interactively: bool,
    frozen: bool,
    confirm=confirm_audio_enable,
) -> bool:
    """Resolve audio without making command-line or development runs prompt."""
    if explicit is not None:
        return explicit
    if selected_interactively and frozen:
        return bool(confirm())
    return False


def resolve_filter2_mode(
    requested: bool,
    digest: str,
    *,
    selected_interactively: bool,
    version: str | None = None,
    confirm=confirm_stock_boot,
) -> bool:
    """Return whether Filter 2 can run, or stop before altering an unknown MAIN."""
    if not requested:
        return False
    if digest == EXPECTED_MAIN_SHA256:
        return True
    if selected_interactively:
        if confirm(version, digest):
            return False
        raise SystemExit(0)
    raise SystemExit(
        "The live Filter 2 extension currently requires official OS 1.72 "
        f"MAIN ({EXPECTED_MAIN_SHA256}); selected MAIN is {digest}. "
        "Use --no-filter2 to boot it untouched."
    )


def finish_runtime(runtime: Path, keep_runtime: bool) -> None:
    if keep_runtime:
        if sys.stderr is not None:
            print(f"Runtime retained at: {runtime}", file=sys.stderr)
    else:
        shutil.rmtree(runtime, ignore_errors=True)


def prepare_firmware(
    selected_main: Path | None,
    selected_syx: Path | None,
    *,
    filter2_requested: bool,
    selected_interactively: bool,
    keep_runtime: bool,
    confirm=confirm_stock_boot,
    runtime_factory=tempfile.mkdtemp,
    extractor=extract_main,
    candidate_builder=build_filter2_candidate,
    controls_publisher=publish_runtime_controls,
) -> FirmwarePreparation:
    """Prepare one temporary launch transaction or leave no runtime behind."""
    if (selected_main is None) == (selected_syx is None):
        raise ValueError("select exactly one MAIN image or firmware update")
    selected = selected_main if selected_main is not None else selected_syx
    assert selected is not None
    if not selected.is_file():
        label = "MAIN image" if selected_main is not None else "firmware file"
        raise SystemExit(f"{label} not found: {selected}")

    runtime = Path(runtime_factory(prefix="ar-mk2-emulator-"))
    filter2_controls = runtime / "filter2-controls.bin"
    try:
        metadata: dict[str, object] = {}
        if selected_main is not None:
            main_image = selected_main
        else:
            assert selected_syx is not None
            main_image = runtime / "main.bin"
            metadata = extractor(selected_syx, main_image)
            if sys.stderr is not None:
                print(
                    f"Extracted OS {metadata['version']} MAIN: "
                    f"{metadata['size']} bytes, sha256={metadata['sha256']}",
                    file=sys.stderr,
                )

        stock = main_image.read_bytes()
        digest = hashlib.sha256(stock).hexdigest()
        filter2_enabled = resolve_filter2_mode(
            filter2_requested,
            digest,
            selected_interactively=selected_interactively,
            version=str(metadata["version"]) if "version" in metadata else None,
            confirm=confirm,
        )
        if filter2_enabled:
            candidate, _ = candidate_builder(stock, True)
            main_image = runtime / "main-filter2-runtime.bin"
            main_image.write_bytes(candidate)
            controls_publisher(filter2_controls, RuntimeControls())
        return FirmwarePreparation(
            runtime=runtime,
            main_image=main_image,
            filter2_controls=filter2_controls,
            metadata=metadata,
            main_sha256=digest,
            filter2_enabled=filter2_enabled,
        )
    except BaseException:
        finish_runtime(runtime, keep_runtime)
        raise


def firmware_display_identity(prepared: FirmwarePreparation) -> str:
    version = prepared.metadata.get("version")
    if version:
        firmware = f"OS {version}"
    elif prepared.main_sha256 == EXPECTED_MAIN_SHA256:
        firmware = "OS 1.72"
    else:
        firmware = f"MAIN {prepared.main_sha256[:8].upper()}"
    if prepared.filter2_enabled:
        mode = "FILTER 2 + LFO2 VERIFIED"
    elif prepared.main_sha256 == EXPECTED_MAIN_SHA256:
        mode = "STOCK MODE / EXTENSION OFF"
    else:
        mode = "UNCHANGED STOCK FALLBACK"
    return f"{firmware}  /  {mode}"


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
    fallback_calls: list[tuple[str | None, str]] = []
    if resolve_filter2_mode(
        True,
        "self-test-unverified-main",
        selected_interactively=True,
        version="self-test",
        confirm=lambda version, digest: fallback_calls.append((version, digest))
        or True,
    ):
        raise RuntimeError("unverified MAIN incorrectly retained Filter 2")
    if fallback_calls != [("self-test", "self-test-unverified-main")]:
        raise RuntimeError("stock-firmware fallback confirmation was not exercised")
    if not resolve_filter2_mode(
        True,
        EXPECTED_MAIN_SHA256,
        selected_interactively=True,
        confirm=lambda _version, _digest: False,
    ):
        raise RuntimeError("verified OS 1.72 MAIN incorrectly disabled Filter 2")
    audio_calls: list[str] = []
    if not resolve_audio_mode(
        None,
        selected_interactively=True,
        frozen=True,
        confirm=lambda: audio_calls.append("enable") or True,
    ):
        raise RuntimeError("Finder audio confirmation did not enable audio")
    if audio_calls != ["enable"]:
        raise RuntimeError("Finder audio confirmation was not exercised once")
    if resolve_audio_mode(
        None,
        selected_interactively=True,
        frozen=True,
        confirm=lambda: False,
    ):
        raise RuntimeError("declined Finder audio confirmation enabled audio")
    with tempfile.TemporaryDirectory(prefix="ar-mk2-skin-test-") as directory:
        runtime = Path(directory)
        update = runtime / "synthetic-update.syx"
        update.write_bytes(b"synthetic update marker")
        transaction_runtimes: list[Path] = []

        def transaction_runtime_factory(*, prefix: str) -> str:
            transaction = runtime / f"{prefix}{len(transaction_runtimes)}"
            transaction.mkdir()
            transaction_runtimes.append(transaction)
            return str(transaction)

        def extract_transaction_fixture(_source: Path, destination: Path) -> dict:
            main = b"synthetic unverified MAIN"
            destination.write_bytes(main)
            return {
                "hardware": "0162",
                "version": "self-test",
                "size": len(main),
                "sha256": hashlib.sha256(main).hexdigest(),
            }

        prepared = prepare_firmware(
            None,
            update,
            filter2_requested=True,
            selected_interactively=True,
            keep_runtime=False,
            confirm=lambda _version, _digest: True,
            runtime_factory=transaction_runtime_factory,
            extractor=extract_transaction_fixture,
        )
        if prepared.filter2_enabled or prepared.filter2_controls.exists():
            raise RuntimeError("stock fallback transaction enabled Filter 2")
        if prepared.main_image.read_bytes() != b"synthetic unverified MAIN":
            raise RuntimeError("stock fallback transaction changed MAIN")
        finish_runtime(prepared.runtime, False)
        if prepared.runtime.exists():
            raise RuntimeError("successful transaction runtime was not removed")

        try:
            prepare_firmware(
                None,
                update,
                filter2_requested=True,
                selected_interactively=True,
                keep_runtime=False,
                confirm=lambda _version, _digest: False,
                runtime_factory=transaction_runtime_factory,
                extractor=extract_transaction_fixture,
            )
        except SystemExit as error:
            if error.code != 0:
                raise RuntimeError("fallback cancellation did not exit cleanly")
        else:
            raise RuntimeError("fallback cancellation did not stop preparation")
        if transaction_runtimes[-1].exists():
            raise RuntimeError("cancelled transaction runtime was not removed")

        try:
            prepare_firmware(
                None,
                update,
                filter2_requested=True,
                selected_interactively=True,
                keep_runtime=False,
                runtime_factory=transaction_runtime_factory,
            )
        except ValueError:
            pass
        else:
            raise RuntimeError("malformed update passed preparation")
        if transaction_runtimes[-1].exists():
            raise RuntimeError("failed transaction runtime was not removed")

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
    audio = ap.add_mutually_exclusive_group()
    audio.add_argument(
        "--audio",
        dest="audio",
        action="store_true",
        default=None,
        help=(
            "enable the 48 kHz stereo renderer tap and eight bounded stock audio "
            "service passes per rising pad/QWERTY edge"
        ),
    )
    audio.add_argument(
        "--no-audio",
        dest="audio",
        action="store_false",
        help="disable audio without showing the Finder launch prompt",
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

    selected_interactively = args.main is None and args.firmware is None
    selected_main: Path | None = None
    selected_syx: Path | None = None
    if args.main:
        selected_main = args.main.expanduser().resolve()
    else:
        selected_syx = (
            args.firmware.expanduser().resolve()
            if args.firmware
            else choose_firmware()
        )
    prepared = prepare_firmware(
        selected_main,
        selected_syx,
        filter2_requested=not args.no_filter2,
        selected_interactively=selected_interactively,
        keep_runtime=args.keep_runtime,
    )
    try:
        audio_enabled = resolve_audio_mode(
            args.audio,
            selected_interactively=selected_interactively,
            frozen=bool(getattr(sys, "frozen", False)),
        )
    except BaseException:
        finish_runtime(prepared.runtime, args.keep_runtime)
        raise
    runtime = prepared.runtime
    uart = runtime / "panel.sock"
    monitor = runtime / "monitor.sock"
    frame = runtime / "front-buffer.bin"
    events = runtime / "panel-events.jsonl"
    filter2_controls = prepared.filter2_controls
    log = runtime / "qemu.log"
    main_image = prepared.main_image
    filter2_enabled = prepared.filter2_enabled
    firmware_identity = firmware_display_identity(prepared)

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
    if audio_enabled:
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
        PanelApp(
            root,
            frame,
            events,
            args.scale,
            filter2_enabled,
            audio_enabled,
            firmware_identity,
        )
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
        finish_runtime(runtime, args.keep_runtime)


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
