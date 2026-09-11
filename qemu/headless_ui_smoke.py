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
import re
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


def log_count(path: Path, needle: str) -> int:
    try:
        return path.read_text(encoding="utf-8", errors="replace").count(needle)
    except FileNotFoundError:
        return 0


def wait_native_release_count(path: Path, deadline: float) -> int:
    pattern = re.compile(r"AR-MK2 AUDIO: final pad release .* completed=(\d+)")
    while time.monotonic() < deadline:
        try:
            match = pattern.search(path.read_text(encoding="utf-8", errors="replace"))
        except FileNotFoundError:
            match = None
        if match is not None:
            return int(match.group(1))
        time.sleep(0.03)
    raise TimeoutError("native pad release marker was not observed")


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


def save_guest_memory(port: int, path: Path, address: int, size: int) -> bytes:
    path.unlink(missing_ok=True)
    response = hmp_command(
        port, f'pmemsave 0x{address:x} 0x{size:x} "{path}"', timeout=5.0
    )
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            data = b""
        if len(data) == size:
            return data
        time.sleep(0.02)
    raise TimeoutError(
        f"guest-memory snapshot incomplete: {path}; monitor response: {response!r}"
    )


def changed_words(before: bytes, after: bytes, base: int) -> list[dict[str, str]]:
    changes = []
    for offset in range(0, min(len(before), len(after)), 2):
        old = int.from_bytes(before[offset:offset + 2], "big")
        new = int.from_bytes(after[offset:offset + 2], "big")
        if old != new:
            changes.append(
                {
                    "address": f"0x{base + offset:08X}",
                    "before": f"0x{old:04X}",
                    "after": f"0x{new:04X}",
                }
            )
    return changes


def read_track_level_state(path: Path) -> tuple[int, list[int]] | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    if len(data) != 30:
        return None
    selected = int.from_bytes(data[:4], "big")
    levels = [
        int.from_bytes(data[offset:offset + 2], "big")
        for offset in range(4, 30, 2)
    ]
    if selected >= len(levels):
        return None
    return selected, levels


def wait_track_level_state(path: Path, deadline: float, predicate):
    while time.monotonic() < deadline:
        state = read_track_level_state(path)
        if state is not None and predicate(state):
            return state
        time.sleep(0.03)
    raise TimeoutError("track-level state did not reach the expected value")


def wait_file_slice(path: Path, offset: int, expected: bytes,
                    deadline: float) -> bytes:
    while time.monotonic() < deadline:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            data = b""
        if data[offset:offset + len(expected)] == expected:
            return data
        time.sleep(0.03)
    raise TimeoutError(
        f"{path.name} offset 0x{offset:X} did not become {expected.hex()}"
    )


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
        "--exercise-encoder",
        action="store_true",
        help="send signed encoder-A probe vectors and require a framebuffer change",
    )
    parser.add_argument(
        "--exercise-track-level",
        action="store_true",
        help="select Trig 3 and prove encoder I mutates only track-3 Level",
    )
    parser.add_argument(
        "--exercise-demo-sample",
        action="store_true",
        help="assign generated slot 1 through SMP encoder D and require QWERTY audio",
    )
    parser.add_argument(
        "--exercise-held-audio",
        action="store_true",
        help=(
            "hold QWERTY Trig 1 beyond the initial eight services, release it, "
            "and require the fixed tail to stop cleanly"
        ),
    )
    parser.add_argument(
        "--held-services",
        type=int,
        default=12,
        help="services required before releasing Trig 1 in held-audio mode",
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
    parser.add_argument(
        "--require-nonzero-audio",
        action="store_true",
        help="require a nonzero stock renderer block to reach the host tap",
    )
    args = parser.parse_args()

    if args.trigger_count < 1:
        parser.error("--trigger-count must be at least 1")
    if args.services_per_trigger < 1:
        parser.error("--services-per-trigger must be at least 1")
    if args.minimum_audio_services < 0:
        parser.error("--minimum-audio-services cannot be negative")
    if args.held_services <= args.services_per_trigger:
        parser.error("--held-services must exceed --services-per-trigger")
    if args.exercise_held_audio:
        args.exercise_demo_sample = True
    if args.exercise_trigger_audio and "unimp" not in args.qemu_debug.split(","):
        args.qemu_debug = f"unimp,{args.qemu_debug}"
    if args.exercise_demo_sample:
        args.exercise_trigger_audio = True
        args.require_nonzero_audio = True
        args.minimum_audio_services = max(args.minimum_audio_services, 8)
        if "unimp" not in args.qemu_debug.split(","):
            args.qemu_debug = f"unimp,{args.qemu_debug}"

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
    track_levels = runtime / "track-level-state.bin"
    parameters = runtime / "parameter-state.bin"
    log = runtime / "qemu.log"
    monitor_port = unused_local_port()
    diagnostics = runtime / "monitor.txt"
    env = os.environ.copy()
    env["AR_MK2_MOCK_CALIBRATION"] = "1"
    env["AR_MK2_MOCK_FACTORY_STATE"] = "1"
    env["AR_MK2_FRAMEBUFFER_OUT"] = str(frame)
    if args.exercise_track_level:
        env["AR_MK2_TRACK_LEVEL_STATE_OUT"] = str(track_levels)
    if args.exercise_demo_sample:
        env["AR_MK2_MOCK_PROJECT_SAMPLE"] = "1"
        if args.exercise_held_audio:
            env["AR_MK2_MOCK_PROJECT_SAMPLE_FRAMES"] = "4096"
        env["AR_MK2_PARAMETER_STATE_OUT"] = str(parameters)
        env["AR_MK2_AUDIO_TRIGGER_SERVICE"] = "1"
    if args.mock_audio_service:
        env["AR_MK2_MOCK_AUDIO_SERVICE"] = "1"
    else:
        env.pop("AR_MK2_MOCK_AUDIO_SERVICE", None)
    if args.require_nonzero_audio:
        env["AR_MK2_AUDIO_TAP"] = "1"
    command = [
        str(qemu), "-M", "elektron-ar-mk2", "-m", "256M",
        "-bios", str(main_image), "-display", "none",
        "-serial", f"pipe:{panel_base}",
        "-monitor", f"tcp:127.0.0.1:{monitor_port},server=on,wait=off",
        "-d", args.qemu_debug, "-D", str(log),
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
        normal_ui = wait_frame(frame, deadline, different_from=before)
        events = [
            {"control": "NO", "press": "24 01", "release": "24 00"},
        ]
        encoder_frame = None
        encoder_state_changes = None
        track_level_result = None
        demo_sample_result = None
        held_audio_result = None
        expected_audio_services = 0
        if args.exercise_demo_sample:
            wait_log_count(
                log, "injected generated 16-bit test sample in slot 1", 1, deadline
            )
            wait_file_slice(parameters, 0x26, b"\x00\x00", deadline)
            panel_writer.write(bytes.fromhex("25 10"))
            time.sleep(0.08)
            panel_writer.write(bytes.fromhex("25 00"))
            time.sleep(0.6)
            for _ in range(4):
                panel_writer.write(bytes.fromhex("33 08"))
                time.sleep(0.1)
            wait_file_slice(parameters, 0x26, b"\x01\x00", deadline)
            demo_sample_result = {
                "provider": "QEMU TEST",
                "sample_slot": 1,
                "assignment": "four native encoder-D +8 frames",
            }
            events.append({
                "control": "SMP + ENCODER D",
                "result": "Sample Slot 1 assigned through stock setter",
            })
        if args.exercise_track_level:
            initial_index, initial_levels = wait_track_level_state(
                track_levels, deadline, lambda state: state[0] < 13
            )
            panel_writer.write(bytes.fromhex("23 04"))
            time.sleep(0.08)
            panel_writer.write(bytes.fromhex("23 00"))
            selected_index, selected_levels = wait_track_level_state(
                track_levels, deadline, lambda state: state[0] == 2
            )
            for _ in range(6):
                panel_writer.write(bytes.fromhex("38 7f"))
                time.sleep(0.1)
            final_index, final_levels = wait_track_level_state(
                track_levels,
                deadline,
                lambda state: state[0] == 2 and state[1][2] != selected_levels[2],
            )
            changed = [
                index for index, (old, new) in enumerate(
                    zip(selected_levels, final_levels)
                ) if old != new
            ]
            if changed != [2]:
                raise AssertionError(f"unexpected Level mutations: {changed}")
            track_level_result = {
                "initial_index": initial_index,
                "selected_index": selected_index,
                "final_index": final_index,
                "changed_words": changed,
                "before": f"0x{selected_levels[2]:04X}",
                "after": f"0x{final_levels[2]:04X}",
            }
            events.append({
                "control": "TRIG 3 + ENCODER I",
                "selection": "23 04 / 23 00",
                "encoder_frames": 6,
            })
        if args.exercise_encoder:
            state_base = 0x80005F00
            state_size = 0x8800
            state_before = save_guest_memory(
                monitor_port, runtime / "encoder-state-before.bin", state_base, state_size
            )
            encoder_frames = [bytes((0x30, 0x81)), bytes((0x30, 0x40))]
            panel_writer.write(b"".join(encoder_frames))
            time.sleep(args.event_settle_seconds)
            state_after = save_guest_memory(
                monitor_port, runtime / "encoder-state-after.bin", state_base, state_size
            )
            encoder_state_changes = changed_words(state_before, state_after, state_base)
            encoder_frame = wait_frame(frame, deadline, different_from=normal_ui)
            events.append(
                {
                    "control": "ENCODER A",
                    "probe": "signed-delta transport",
                    "delta_frames": len(encoder_frames),
                    "signed_deltas": [-127, 64],
                }
            )
        if args.exercise_trigger_audio:
            if args.exercise_held_audio:
                hold_started = time.monotonic()
                panel_writer.write(bytes.fromhex("23 01"))
                wait_log_count(
                    log,
                    "AR-MK2 AUDIO: completed vector 191 service",
                    args.held_services,
                    deadline,
                )
                hold_elapsed = time.monotonic() - hold_started
                release_started = time.monotonic()
                panel_writer.write(bytes.fromhex("23 00"))
                services_at_native_release = wait_native_release_count(
                    log, deadline
                )
                expected_audio_services = (
                    services_at_native_release + args.services_per_trigger
                )
                wait_log_count(
                    log,
                    "AR-MK2 AUDIO: completed vector 191 service",
                    expected_audio_services,
                    deadline,
                )
                release_elapsed = time.monotonic() - release_started
                stopped_count = log_count(
                    log, "AR-MK2 AUDIO: completed vector 191 service"
                )
                time.sleep(1.0)
                final_count = log_count(
                    log, "AR-MK2 AUDIO: completed vector 191 service"
                )
                if final_count != stopped_count:
                    raise AssertionError(
                        "audio service continued after the release tail: "
                        f"{stopped_count} -> {final_count}"
                    )
                held_audio_result = {
                    "minimum_services_before_host_release": args.held_services,
                    "services_at_native_release": services_at_native_release,
                    "release_tail_services": args.services_per_trigger,
                    "services_after_stop_check": final_count,
                    "stopped_after_release": True,
                    "held_service_wall_seconds": round(hold_elapsed, 3),
                    "held_service_rate_hz": round(
                        args.held_services / hold_elapsed, 3
                    ),
                    "release_tail_wall_seconds": round(release_elapsed, 3),
                }
                events.append(
                    {
                        "control": "TRIG 1",
                        "press": "23 01",
                        "release": "23 00",
                        "mode": "held",
                    }
                )
            else:
                for index in range(args.trigger_count):
                    panel_writer.write(bytes.fromhex("23 01"))
                    if args.exercise_demo_sample:
                        wait_log_count(
                            log,
                            "AR-MK2 AUDIO: completed vector 191 service",
                            index * args.services_per_trigger + 1,
                            deadline,
                        )
                    else:
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
                expected_audio_services = (
                    args.trigger_count * args.services_per_trigger
                )
        if args.minimum_audio_services:
            wait_log_count(
                log,
                "AR-MK2 AUDIO: completed vector 191 service",
                args.minimum_audio_services,
                deadline,
            )
        if args.require_nonzero_audio:
            wait_log_count(
                log,
                "AR-MK2 AUDIO: streaming stock renderer ring",
                1,
                deadline,
            )
        time.sleep(1.0)
        panel_writer.write(bytes.fromhex("25 10"))
        time.sleep(0.08)
        panel_writer.write(bytes.fromhex("25 00"))
        time.sleep(args.event_settle_seconds)
        smp_page = wait_frame(
            frame, deadline, different_from=encoder_frame or normal_ui
        )
        events.append(
            {"control": "SMP", "press": "25 10", "release": "25 00"}
        )
        print(json.dumps({
            "result": "PASS",
            "events": events,
            "completed_audio_services": max(
                args.minimum_audio_services,
                expected_audio_services,
            ),
            "nonzero_host_audio": args.require_nonzero_audio,
            "startup_modal": metrics(before),
            "normal_ui": metrics(normal_ui),
            "encoder_frame": metrics(encoder_frame) if encoder_frame else None,
            "encoder_state_changes": encoder_state_changes,
            "track_level": track_level_result,
            "demo_sample": demo_sample_result,
            "held_audio": held_audio_result,
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
                    "xp/8wx 0xfc0456c0",
                    "xp/8wx 0xfc044008",
                    "xp/4bx 0xfc040034",
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
