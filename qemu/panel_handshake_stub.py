#!/usr/bin/env python3
"""Minimal UART panel identity responder for the AR MKII QEMU research harness."""
from __future__ import annotations
import argparse
import socket
import time
from pathlib import Path

IDENTITY_QUERY = b"\x70\x00"
IDENTITY_REPLY = bytes.fromhex("70 07 05 05 00")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("socket", type=Path)
    ap.add_argument("--seconds", type=float, default=10.0)
    args = ap.parse_args()

    deadline = time.monotonic() + args.seconds
    while not args.socket.exists():
        if time.monotonic() >= deadline:
            raise SystemExit(f"serial socket did not appear: {args.socket}")
        time.sleep(0.02)

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(str(args.socket))
    s.settimeout(0.05)
    seen = bytearray()
    replied = False
    try:
        while time.monotonic() < deadline:
            try:
                data = s.recv(4096)
            except socket.timeout:
                continue
            if not data:
                break
            seen.extend(data)
            print(f"firmware -> panel: {data.hex(' ')}", flush=True)
            if not replied and IDENTITY_QUERY in seen:
                s.sendall(IDENTITY_REPLY)
                replied = True
                print(f"panel -> firmware: {IDENTITY_REPLY.hex(' ')}", flush=True)
    finally:
        s.close()


if __name__ == "__main__":
    main()
