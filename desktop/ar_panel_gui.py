#!/usr/bin/env python3
"""Desktop UI for the AR MKII firmware emulator.

Displays the firmware's presented 128x64 1bpp framebuffer and emits only panel
controls whose native UART mappings have been established from OS 1.72.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
import tkinter as tk

W, H = 128, 64
FRAME_BYTES = 1024


class App:
    def __init__(self, root: tk.Tk, frame_file: Path, event_file: Path, scale: int = 6):
        self.root = root
        self.frame_file = frame_file
        self.event_file = event_file
        self.scale = scale
        self.last_mtime = 0
        self.photo = None

        root.title("Analog Rytm MKII — firmware emulator")
        root.configure(bg="#181818")
        root.protocol("WM_DELETE_WINDOW", self.close)

        outer = tk.Frame(root, bg="#181818")
        outer.pack(padx=14, pady=14)

        self.canvas = tk.Canvas(
            outer,
            width=W * scale,
            height=H * scale,
            bg="black",
            highlightthickness=1,
            highlightbackground="#555",
        )
        self.canvas.grid(row=0, column=0, columnspan=9, pady=(0, 10))
        self.image_id = self.canvas.create_image(0, 0, anchor="nw")

        self.status = tk.StringVar(value="booting firmware…")
        tk.Label(outer, textvariable=self.status, fg="#bbb", bg="#181818").grid(
            row=1, column=0, columnspan=9, sticky="w", pady=(0, 8)
        )

        encoders = tk.Frame(outer, bg="#181818")
        encoders.grid(row=2, column=0, columnspan=9, pady=(0, 10))
        for i, name in enumerate("ABCDEFGHI"):
            box = tk.Frame(encoders, bg="#181818")
            box.grid(row=0, column=i, padx=3)
            tk.Label(box, text=name, fg="white", bg="#181818").pack()
            tk.Button(box, text="−", width=2,
                      command=lambda n=name: self.event("encoder", n, -1)).pack(side="left")
            tk.Button(box, text="+", width=2,
                      command=lambda n=name: self.event("encoder", n, +1)).pack(side="left")

        trigs = tk.Frame(outer, bg="#202020")
        trigs.grid(row=3, column=0, columnspan=9, sticky="ew")
        for i in range(16):
            b = tk.Button(trigs, text=str(i + 1), width=4)
            b.grid(row=i // 8, column=i % 8, padx=3, pady=4)
            b.bind("<ButtonPress-1>", lambda _e, n=i + 1: self.event("trig", str(n), "press"))
            b.bind("<ButtonRelease-1>", lambda _e, n=i + 1: self.event("trig", str(n), "release"))

        pages = tk.Frame(outer, bg="#181818")
        pages.grid(row=4, column=0, columnspan=9, pady=(10, 0))
        for i, name in enumerate(("NO", "YES", "TRIG", "SYN", "SMP", "FLTR", "AMP", "LFO")):
            button = tk.Button(pages, text=name, width=5)
            button.grid(row=0, column=i, padx=3)
            button.bind("<ButtonPress-1>",
                        lambda _e, n=name: self.event("button", n, "press"))
            button.bind("<ButtonRelease-1>",
                        lambda _e, n=name: self.event("button", n, "release"))

        tk.Label(
            outer,
            text="Native mappings: page keys, NO/YES, Trig 1–16, and encoders A–I",
            fg="#888",
            bg="#181818",
        ).grid(row=5, column=0, columnspan=9, sticky="w", pady=(8, 0))

        self.poll()

    def close(self) -> None:
        self.root.destroy()

    def event(self, kind: str, name: str, value) -> None:
        record = {"t": time.time(), "kind": kind, "name": name, "value": value}
        self.event_file.parent.mkdir(parents=True, exist_ok=True)
        with self.event_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        self.status.set(f"panel: {kind} {name} {value}")

    @staticmethod
    def decode_firmware_layout(data: bytes) -> list[list[int]]:
        """Decode x-major OLED pages: eight vertical LSB-first pixels per byte."""
        pixels = [[0] * W for _ in range(H)]
        for y in range(H):
            for x in range(W):
                value = data[x * 8 + y // 8]
                pixels[y][x] = (value >> (y & 7)) & 1
        return pixels

    def draw(self, data: bytes) -> None:
        pixels = self.decode_firmware_layout(data)
        small = tk.PhotoImage(width=W, height=H)
        for y, row in enumerate(pixels):
            start = 0
            current = row[0]
            for x in range(1, W + 1):
                value = row[x] if x < W else 1 - current
                if value != current:
                    small.put("#f2f2f2" if current else "#000000", to=(start, y, x, y + 1))
                    start = x
                    current = value
        self.photo = small.zoom(self.scale, self.scale)
        self.canvas.itemconfigure(self.image_id, image=self.photo)

    def reload(self) -> None:
        try:
            stat = self.frame_file.stat()
        except FileNotFoundError:
            return
        if stat.st_mtime_ns == self.last_mtime:
            return
        data = self.frame_file.read_bytes()
        if len(data) < FRAME_BYTES:
            self.status.set(f"waiting for complete framebuffer ({len(data)}/1024 bytes)")
            return
        self.draw(data[:FRAME_BYTES])
        self.last_mtime = stat.st_mtime_ns
        lit = sum(byte.bit_count() for byte in data[:FRAME_BYTES])
        self.status.set(f"firmware framebuffer live — {lit} lit pixels")

    def poll(self) -> None:
        self.reload()
        if self.root.winfo_exists():
            self.root.after(33, self.poll)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame", type=Path, required=True)
    ap.add_argument("--events", type=Path, required=True)
    ap.add_argument("--scale", type=int, default=6)
    args = ap.parse_args()

    root = tk.Tk()
    App(root, args.frame, args.events, max(2, min(args.scale, 10)))
    root.mainloop()


if __name__ == "__main__":
    main()
