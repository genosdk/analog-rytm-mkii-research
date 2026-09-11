#!/usr/bin/env python3
"""Trace panel events at ColdFire firmware breakpoints through QEMU's GDB stub."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time


REGISTER_NAMES = [
    *(f"d{i}" for i in range(8)),
    *(f"a{i}" for i in range(7)),
    "sp",
    "ps",
    "pc",
]


class RSP:
    def __init__(self, port: int):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.sock.settimeout(15)

    def _read_packet(self) -> str:
        while self.sock.recv(1) != b"$":
            pass
        payload = bytearray()
        while True:
            byte = self.sock.recv(1)
            if byte == b"#":
                break
            payload.extend(byte)
        self.sock.recv(2)
        self.sock.sendall(b"+")
        return payload.decode("ascii")

    def command(self, payload: str, expect_reply: bool = True) -> str | None:
        raw = payload.encode("ascii")
        packet = b"$" + raw + b"#" + f"{sum(raw) & 0xff:02x}".encode("ascii")
        self.sock.sendall(packet)
        if self.sock.recv(1) != b"+":
            raise RuntimeError(f"GDB stub rejected {payload!r}")
        return self._read_packet() if expect_reply else None

    def interrupt(self) -> str:
        self.sock.sendall(b"\x03")
        return self._read_packet()

    def registers(self) -> dict[str, int]:
        raw = bytes.fromhex(self.command("g") or "")
        if len(raw) != len(REGISTER_NAMES) * 4:
            raise RuntimeError(f"unexpected register packet length {len(raw)}")
        return {
            name: int.from_bytes(raw[index * 4 : index * 4 + 4], "big")
            for index, name in enumerate(REGISTER_NAMES)
        }

    def memory(self, address: int, size: int) -> bytes:
        reply = self.command(f"m{address:x},{size:x}") or ""
        if reply.startswith("E"):
            raise RuntimeError(f"memory read failed at 0x{address:08X}: {reply}")
        return bytes.fromhex(reply)


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def format_registers(registers: dict[str, int]) -> str:
    return " ".join(f"{name}=0x{value:08X}" for name, value in registers.items())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--main", type=Path, required=True)
    parser.add_argument("--page", default="", help="panel bytes sent before the event")
    parser.add_argument("--event", required=True, help="panel event bytes")
    parser.add_argument("--breakpoint", action="append", required=True, type=lambda x: int(x, 0))
    parser.add_argument(
        "--memory", action="append", default=[],
        help="additional ADDRESS:SIZE memory range printed at every hit",
    )
    parser.add_argument(
        "--watch", action="append", default=[],
        help="ADDRESS:SIZE range reported as changed byte offsets from pre-event state",
    )
    parser.add_argument("--hits", type=int, default=16)
    parser.add_argument("--boot-seconds", type=float, default=12)
    parser.add_argument("--event-repeats", type=int, default=4)
    parser.add_argument("--event-delay", type=float, default=0.02)
    parser.add_argument("--log-events", action="store_true")
    args = parser.parse_args()

    runtime = Path(tempfile.mkdtemp(prefix="ar-mk2-gdb-trace-"))
    panel_base = runtime / "panel"
    panel_in = runtime / "panel.in"
    panel_out = runtime / "panel.out"
    os.mkfifo(panel_in)
    os.mkfifo(panel_out)
    port = unused_port()
    env = os.environ.copy()
    env["AR_MK2_MOCK_CALIBRATION"] = "1"
    env["AR_MK2_MOCK_FACTORY_STATE"] = "1"
    command = [
        str(args.qemu.resolve()), "-M", "elektron-ar-mk2", "-m", "256M",
        "-bios", str(args.main.resolve()), "-display", "none",
        "-serial", f"pipe:{panel_base}", "-monitor", "none",
        "-gdb", f"tcp:127.0.0.1:{port}",
    ]
    proc = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rsp = None
    writer = None
    try:
        threading.Thread(target=lambda: panel_out.open("rb", buffering=0).read(), daemon=True).start()
        writer = panel_in.open("wb", buffering=0)
        writer.write(bytes.fromhex("70 07 05 05 00"))
        writer.flush()
        time.sleep(args.boot_seconds)
        writer.write(bytes.fromhex("24 01 24 00"))
        writer.flush()
        time.sleep(3)
        if args.page:
            page = bytes.fromhex(args.page)
            for offset in range(0, len(page), 2):
                writer.write(page[offset:offset + 2])
                writer.flush()
                time.sleep(0.08)
            time.sleep(1)

        deadline = time.monotonic() + 10
        while True:
            try:
                rsp = RSP(port)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        rsp.command("qSupported:multiprocess+")
        # QEMU stops the vCPU when a debugger attaches. Query that initial stop;
        # an out-of-band Ctrl-C can be lost while the stub is already stopped.
        rsp.command("?")
        for address in args.breakpoint:
            reply = rsp.command(f"Z0,{address:x},2")
            if reply != "OK":
                raise RuntimeError(f"cannot set breakpoint 0x{address:08X}: {reply}")
        watches = []
        for memory_range in args.watch:
            address_text, size_text = memory_range.split(":", 1)
            address = int(address_text, 0)
            size = int(size_text, 0)
            watches.append((address, rsp.memory(address, size)))
        event = bytes.fromhex(args.event)
        if len(event) % 2:
            raise ValueError("--event must contain complete two-byte panel frames")

        def send_events() -> None:
            for _ in range(args.event_repeats):
                for offset in range(0, len(event), 2):
                    frame = event[offset:offset + 2]
                    writer.write(frame)
                    writer.flush()
                    if args.log_events:
                        print(f"send={frame.hex()}", flush=True)
                    time.sleep(args.event_delay)

        rsp.command("c", expect_reply=False)
        sender = threading.Thread(target=send_events, daemon=True)
        sender.start()

        for hit in range(args.hits):
            stop = rsp._read_packet()
            registers = rsp.registers()
            pc = registers["pc"]
            stack = rsp.memory(registers["sp"], 32).hex()
            print(f"hit={hit + 1} stop={stop} {format_registers(registers)} stack={stack}", flush=True)
            for memory_range in args.memory:
                address_text, size_text = memory_range.split(":", 1)
                address = int(address_text, 0)
                size = int(size_text, 0)
                print(
                    f"memory=0x{address:08X}:0x{size:X} data={rsp.memory(address, size).hex()}",
                    flush=True,
                )
            for address, before in watches:
                after = rsp.memory(address, len(before))
                changes = [
                    f"+0x{offset:X}:{old:02X}->{new:02X}"
                    for offset, (old, new) in enumerate(zip(before, after))
                    if old != new
                ]
                print(
                    f"watch=0x{address:08X}:0x{len(before):X} changes="
                    + (",".join(changes) if changes else "none"),
                    flush=True,
                )
            if pc not in args.breakpoint:
                break
            rsp.command(f"z0,{pc:x},2")
            rsp.command("s")
            rsp.command(f"Z0,{pc:x},2")
            rsp.command("c", expect_reply=False)
        sender.join(timeout=1)
        return 0
    finally:
        if writer is not None:
            writer.close()
        if rsp is not None:
            rsp.sock.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
