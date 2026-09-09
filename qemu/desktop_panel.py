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
from collections.abc import Callable

W, H = 128, 64
RAW_W, RAW_H = 64, 128
FRAME_BYTES = 1024
PROVEN_BUTTONS = ("TRIG", "SYN", "SMP", "FLTR", "AMP", "LFO", "YES", "NO")
QWERTY_TRIGS = {
    key: trig
    for trig, key in enumerate("qwertyuiasdfghjk", start=1)
}


def clamp_panel_value(value: int) -> int:
    return max(0, min(127, value))


class VirtualKnob(tk.Canvas):
    """Mouse-draggable 0..127 control that emits relative encoder deltas."""

    def __init__(self, parent, name: str, callback: Callable[[str, int, int], None]):
        super().__init__(
            parent,
            width=58,
            height=76,
            bg="#181818",
            highlightthickness=0,
            cursor="sb_v_double_arrow",
        )
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
        x = 29 + 14 * math.cos(angle)
        y = 36 - 14 * math.sin(angle)
        self.create_line(29, 36, x, y, fill="#f2f2f2", width=3)
        self.create_text(29, 67, text=str(self.value), fill="#bbbbbb")


class PanelApp:
    def __init__(self, root: tk.Tk, frame_file: Path, event_file: Path,
                 scale: int = 6) -> None:
        self.root = root
        self.frame_file = frame_file
        self.event_file = event_file
        self.scale = scale
        self.last_mtime = 0
        self.photo = None
        self.held_trigs: dict[int, set[str]] = {}
        self.pending_key_releases: dict[str, str] = {}

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
            VirtualKnob(enc_frame, name, self.encoder).grid(row=0, column=col, padx=1)

        tk.Label(
            shell,
            text="Pads: QWERTYUI / ASDFGHJK  •  Knobs: drag vertically or use wheel",
            fg="#888888",
            bg="#181818",
        ).grid(row=5, column=0, columnspan=9, sticky="w", pady=(8, 0))

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind_all("<KeyPress>", self.key_press, add="+")
        self.root.bind_all("<KeyRelease>", self.key_release, add="+")
        self.root.bind("<FocusOut>", self.focus_lost, add="+")
        self.poll()

    def emit(self, kind: str, name: str, value) -> None:
        rec = {"t": time.time(), "kind": kind, "name": name, "value": value}
        self.event_file.parent.mkdir(parents=True, exist_ok=True)
        with self.event_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def panel_button(self, name: str, pressed: bool) -> None:
        self.emit("button", name, "press" if pressed else "release")
        self.status.set(f"{name} {'down' if pressed else 'up'}")

    def trig(self, trig: int, pressed: bool, source: str = "mouse") -> None:
        held = self.held_trigs.setdefault(trig, set())
        was_pressed = bool(held)
        if pressed:
            held.add(source)
        else:
            held.discard(source)
        is_pressed = bool(held)
        if was_pressed == is_pressed:
            return
        self.emit("trig", str(trig), "press" if is_pressed else "release")
        self.status.set(f"Trig {trig} {'down' if is_pressed else 'up'}")

    def encoder(self, name: str, delta: int, value: int | None = None) -> None:
        self.emit("encoder", name, delta)
        suffix = f" → {value}" if value is not None else ""
        self.status.set(f"Encoder {name}: {delta:+d}{suffix}")

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
                self.emit("trig", str(trig), "release")

    def focus_lost(self, _event=None) -> None:
        self.root.after_idle(self.release_if_unfocused)

    def release_if_unfocused(self) -> None:
        focused = self.root.focus_get()
        if focused is None or focused.winfo_toplevel() != self.root:
            self.release_all_trigs()

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
        self.release_all_trigs()
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
