#!/usr/bin/env python3
"""Desktop panel for the AR MKII firmware emulator.

Only controls whose OS 1.72 UART mappings have been proven are exposed. The
OLED shows the firmware's presented 1 KiB framebuffer in its native internal
layout: 64x128 row-major MSB, rotated 90 degrees to the physical 128x64 screen.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
import tkinter as tk

W, H = 128, 64
RAW_W, RAW_H = 64, 128
FRAME_BYTES = 1024
PROVEN_BUTTONS = ("TRIG", "SYN", "SMP", "FLTR", "AMP", "LFO", "YES", "NO")


class PanelApp:
    def __init__(self, root: tk.Tk, frame_file: Path, event_file: Path,
                 scale: int = 6) -> None:
        self.root = root
        self.frame_file = frame_file
        self.event_file = event_file
        self.scale = scale
        self.last_mtime = 0
        self.photo = None

        root.title("Analog Rytm MKII — Firmware Emulator")
        root.configure(bg="#181818")

        shell = tk.Frame(root, bg="#181818")
        shell.pack(padx=16, pady=16)

        self.canvas = tk.Canvas(
            shell,
            width=W * scale,
            height=H * scale,
            bg="black",
            highlightthickness=1,
            highlightbackground="#555555",
        )
        self.canvas.grid(row=0, column=0, columnspan=9, pady=(0, 10))
        self.image_id = self.canvas.create_image(0, 0, anchor="nw")

        self.status = tk.StringVar(value="Starting firmware…")
        tk.Label(
            shell,
            textvariable=self.status,
            fg="#bbbbbb",
            bg="#181818",
            anchor="w",
        ).grid(row=1, column=0, columnspan=9, sticky="ew", pady=(0, 10))

        page_frame = tk.Frame(shell, bg="#181818")
        page_frame.grid(row=2, column=0, columnspan=9, sticky="ew", pady=(0, 10))
        for col, name in enumerate(PROVEN_BUTTONS):
            b = tk.Button(page_frame, text=name, width=6)
            b.grid(row=0, column=col, padx=3, pady=3)
            b.bind("<ButtonPress-1>", lambda _e, n=name: self.panel_button(n, True))
            b.bind("<ButtonRelease-1>", lambda _e, n=name: self.panel_button(n, False))

        trig_frame = tk.Frame(shell, bg="#202020")
        trig_frame.grid(row=3, column=0, columnspan=9, sticky="ew", pady=(0, 10))
        for i in range(16):
            trig = i + 1
            row = i // 8
            col = i % 8
            b = tk.Button(trig_frame, text=str(trig), width=5)
            b.grid(row=row, column=col, padx=3, pady=4)
            b.bind("<ButtonPress-1>", lambda _e, t=trig: self.trig(t, True))
            b.bind("<ButtonRelease-1>", lambda _e, t=trig: self.trig(t, False))

        enc_frame = tk.Frame(shell, bg="#181818")
        enc_frame.grid(row=4, column=0, columnspan=9)
        for col, name in enumerate("ABCDEFGHI"):
            f = tk.Frame(enc_frame, bg="#181818")
            f.grid(row=0, column=col, padx=4)
            tk.Label(f, text=name, fg="white", bg="#181818").pack()
            tk.Button(
                f, text="−", width=2,
                command=lambda n=name: self.encoder(n, -1),
            ).pack(side="left")
            tk.Button(
                f, text="+", width=2,
                command=lambda n=name: self.encoder(n, +1),
            ).pack(side="left")

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll()

    def emit(self, kind: str, name: str, value) -> None:
        rec = {"t": time.time(), "kind": kind, "name": name, "value": value}
        self.event_file.parent.mkdir(parents=True, exist_ok=True)
        with self.event_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def panel_button(self, name: str, pressed: bool) -> None:
        self.emit("button", name, "press" if pressed else "release")
        self.status.set(f"{name} {'down' if pressed else 'up'}")

    def trig(self, trig: int, pressed: bool) -> None:
        self.emit("trig", str(trig), "press" if pressed else "release")
        self.status.set(f"Trig {trig} {'down' if pressed else 'up'}")

    def encoder(self, name: str, delta: int) -> None:
        self.emit("encoder", name, delta)
        self.status.set(f"Encoder {name}: {delta:+d}")

    @staticmethod
    def decode_presented(data: bytes) -> list[list[int]]:
        """Decode 64x128 row-major MSB storage and rotate 90° CCW."""
        raw = [[0] * RAW_W for _ in range(RAW_H)]
        for y in range(RAW_H):
            rowoff = y * (RAW_W // 8)
            for x in range(RAW_W):
                value = data[rowoff + x // 8]
                raw[y][x] = (value >> (7 - (x & 7))) & 1

        # PIL's visually verified rotate(90, expand=True) equivalent:
        # dest(x,y) = raw[x][RAW_W - 1 - y].
        pix = [[0] * W for _ in range(H)]
        for y in range(H):
            for x in range(W):
                pix[y][x] = raw[x][RAW_W - 1 - y]
        return pix

    def draw(self, data: bytes) -> None:
        pix = self.decode_presented(data)
        small = tk.PhotoImage(width=W, height=H)
        for y, row in enumerate(pix):
            start = 0
            current = row[0]
            for x in range(1, W + 1):
                value = row[x] if x < W else 1 - current
                if value != current:
                    small.put(
                        "#f2f2f2" if current else "#000000",
                        to=(start, y, x, y + 1),
                    )
                    start = x
                    current = value
        self.photo = small.zoom(self.scale, self.scale)
        self.canvas.itemconfigure(self.image_id, image=self.photo)

    def reload_frame(self) -> None:
        try:
            st = self.frame_file.stat()
        except FileNotFoundError:
            return
        if st.st_mtime_ns == self.last_mtime:
            return
        data = self.frame_file.read_bytes()
        if len(data) < FRAME_BYTES:
            self.status.set(f"Framebuffer incomplete: {len(data)}/1024 bytes")
            return
        self.draw(data[:FRAME_BYTES])
        self.last_mtime = st.st_mtime_ns
        nonzero = sum(v != 0 for v in data[:FRAME_BYTES])
        self.status.set(f"Firmware OLED — {nonzero} nonzero bytes")

    def poll(self) -> None:
        self.reload_frame()
        self.root.after(33, self.poll)

    def close(self) -> None:
        self.root.destroy()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame", type=Path, required=True)
    ap.add_argument("--events", type=Path, required=True)
    ap.add_argument("--scale", type=int, default=6)
    args = ap.parse_args()
    root = tk.Tk()
    PanelApp(root, args.frame, args.events, args.scale)
    root.mainloop()


if __name__ == "__main__":
    main()
