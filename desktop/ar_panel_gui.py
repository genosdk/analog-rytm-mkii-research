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
from collections.abc import Callable

W, H = 128, 64
FRAME_BYTES = 1024
QWERTY_TRIGS = {
    key: trig
    for trig, key in enumerate("qwertyuiasdfghjk", start=1)
}


def clamp_panel_value(value: int) -> int:
    return max(0, min(127, value))


class VirtualKnob(tk.Canvas):
    def __init__(self, parent, name: str, callback: Callable[[str, int, int], None]):
        super().__init__(parent, width=58, height=76, bg="#181818",
                         highlightthickness=0, cursor="sb_v_double_arrow")
        self.name = name
        self.callback = callback
        self.value = 64
        self.drag_y = 0
        self.drag_value = self.value
        self.bind("<ButtonPress-1>", self.begin_drag)
        self.bind("<B1-Motion>", self.drag)
        self.bind("<MouseWheel>", self.wheel)
        self.bind("<Button-4>", lambda _event: self.adjust(1))
        self.bind("<Button-5>", lambda _event: self.adjust(-1))
        self.redraw()

    def begin_drag(self, event) -> None:
        self.focus_set()
        self.drag_y = event.y_root
        self.drag_value = self.value

    def drag(self, event) -> None:
        self.set_value(self.drag_value + round((self.drag_y - event.y_root) / 2))

    def wheel(self, event) -> str:
        self.adjust(1 if event.delta > 0 else -1)
        return "break"

    def adjust(self, delta: int) -> None:
        self.set_value(self.value + delta)

    def set_value(self, value: int) -> None:
        value = clamp_panel_value(value)
        delta = value - self.value
        if not delta:
            return
        self.value = value
        self.redraw()
        self.callback(self.name, delta, value)

    def redraw(self) -> None:
        import math

        self.delete("all")
        self.create_text(29, 8, text=self.name, fill="white", font=("TkDefaultFont", 9, "bold"))
        self.create_oval(10, 17, 48, 55, fill="#303030", outline="#777777", width=2)
        angle = math.radians(225 - (270 * self.value / 127))
        self.create_line(29, 36, 29 + 14 * math.cos(angle),
                         36 - 14 * math.sin(angle), fill="#f2f2f2", width=3)
        self.create_text(29, 67, text=str(self.value), fill="#bbbbbb")


class App:
    def __init__(self, root: tk.Tk, frame_file: Path, event_file: Path, scale: int = 6):
        self.root = root
        self.frame_file = frame_file
        self.event_file = event_file
        self.scale = scale
        self.last_mtime = 0
        self.photo = None
        self.held_trigs: dict[int, set[str]] = {}
        self.pending_key_releases: dict[str, str] = {}

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
            VirtualKnob(encoders, name, self.encoder).grid(row=0, column=i, padx=1)

        trigs = tk.Frame(outer, bg="#202020")
        trigs.grid(row=3, column=0, columnspan=9, sticky="ew")
        for i in range(16):
            b = tk.Button(trigs, text=str(i + 1), width=4)
            b.grid(row=i // 8, column=i % 8, padx=3, pady=4)
            b.bind("<ButtonPress-1>", lambda _e, n=i + 1: self.trig(n, True))
            b.bind("<ButtonRelease-1>", lambda _e, n=i + 1: self.trig(n, False))

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
            text="Pads: QWERTYUI / ASDFGHJK  •  Knobs: drag vertically or use wheel",
            fg="#888",
            bg="#181818",
        ).grid(row=5, column=0, columnspan=9, sticky="w", pady=(8, 0))

        self.root.bind_all("<KeyPress>", self.key_press, add="+")
        self.root.bind_all("<KeyRelease>", self.key_release, add="+")
        self.root.bind("<FocusOut>", self.focus_lost, add="+")
        self.poll()

    def close(self) -> None:
        self.release_all_trigs()
        self.root.destroy()

    def event(self, kind: str, name: str, value) -> None:
        record = {"t": time.time(), "kind": kind, "name": name, "value": value}
        self.event_file.parent.mkdir(parents=True, exist_ok=True)
        with self.event_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        self.status.set(f"panel: {kind} {name} {value}")

    def trig(self, trig: int, pressed: bool, source: str = "mouse") -> None:
        held = self.held_trigs.setdefault(trig, set())
        was_pressed = bool(held)
        if pressed:
            held.add(source)
        else:
            held.discard(source)
        is_pressed = bool(held)
        if was_pressed != is_pressed:
            self.event("trig", str(trig), "press" if is_pressed else "release")

    def encoder(self, name: str, delta: int, value: int) -> None:
        self.event("encoder", name, delta)
        self.status.set(f"Encoder {name}: {delta:+d} → {value}")

    def key_press(self, event) -> str | None:
        key = event.keysym.lower()
        trig = QWERTY_TRIGS.get(key)
        if trig is None:
            return None
        pending = self.pending_key_releases.pop(key, None)
        if pending is not None:
            self.root.after_cancel(pending)
        self.trig(trig, True, f"key:{key}")
        return "break"

    def key_release(self, event) -> str | None:
        key = event.keysym.lower()
        trig = QWERTY_TRIGS.get(key)
        if trig is None:
            return None
        pending = self.pending_key_releases.pop(key, None)
        if pending is not None:
            self.root.after_cancel(pending)
        self.pending_key_releases[key] = self.root.after(
            12, lambda k=key, t=trig: self.finish_key_release(k, t)
        )
        return "break"

    def finish_key_release(self, key: str, trig: int) -> None:
        self.pending_key_releases.pop(key, None)
        self.trig(trig, False, f"key:{key}")

    def release_all_trigs(self) -> None:
        for callback in self.pending_key_releases.values():
            self.root.after_cancel(callback)
        self.pending_key_releases.clear()
        for trig, sources in list(self.held_trigs.items()):
            if sources:
                sources.clear()
                self.event("trig", str(trig), "release")

    def focus_lost(self, _event=None) -> None:
        self.root.after_idle(self.release_if_unfocused)

    def release_if_unfocused(self) -> None:
        focused = self.root.focus_get()
        if focused is None or focused.winfo_toplevel() != self.root:
            self.release_all_trigs()

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
