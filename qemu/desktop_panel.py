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
PAGE_BUTTONS = PROVEN_BUTTONS[:6]
PANEL_ASPECT = 385 / 225
PANEL_BG = "#171918"
PANEL_INSET = "#101211"
PANEL_EDGE = "#373a38"
CONTROL_FACE = "#252826"
CONTROL_EDGE = "#555a56"
TEXT = "#e6e9e5"
MUTED = "#89908b"
LED_OFF = "#4a1c14"
LED_RED = "#ff4b2e"
LED_ORANGE = "#ff7a32"
OLED_PIXEL = "#e5eee8"
QWERTY_TRIGS = {
    key: trig
    for trig, key in enumerate("qwertyuiasdfghjk", start=1)
}
TRIG_KEYS = {trig: key.upper() for key, trig in QWERTY_TRIGS.items()}


def clamp_panel_value(value: int) -> int:
    return max(0, min(127, value))


def panel_window_size(scale: int) -> tuple[int, int]:
    """Return one fixed hardware-proportional window size for a display scale."""
    width = max(1120, W * max(1, scale) + 450)
    width = int(round(width / 10) * 10)
    return width, int(round(width / PANEL_ASPECT))


class PanelButton(tk.Canvas):
    """Drawn panel key with a persistent page LED and momentary press state."""

    def __init__(self, parent, name: str,
                 callback: Callable[[str, bool], None]):
        super().__init__(
            parent,
            width=76,
            height=42,
            bg=PANEL_INSET,
            highlightthickness=0,
            cursor="hand2",
            takefocus=True,
        )
        self.name = name
        self.callback = callback
        self.held = False
        self.active = False
        self.bind("<ButtonPress-1>", self.press)
        self.bind("<ButtonRelease-1>", self.release)
        self.bind("<Leave>", self.release)
        self.bind("<KeyPress-space>", self.press)
        self.bind("<KeyRelease-space>", self.release)
        self.bind("<FocusIn>", lambda _event: self.redraw())
        self.bind("<FocusOut>", lambda _event: self.redraw())
        self.redraw()

    def press(self, _event=None) -> str:
        self.focus_set()
        if not self.held:
            self.held = True
            self.redraw()
            self.callback(self.name, True)
        return "break"

    def release(self, _event=None) -> str:
        if self.held:
            self.held = False
            self.redraw()
            self.callback(self.name, False)
        return "break"

    def set_active(self, active: bool) -> None:
        if self.active != active:
            self.active = active
            self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        focused = self.focus_get() == self
        face = "#353936" if self.held else CONTROL_FACE
        edge = LED_ORANGE if focused else CONTROL_EDGE
        self.create_rectangle(4, 7, 72, 38, fill=face, outline=edge, width=2)
        self.create_text(38, 24, text=self.name, fill=TEXT,
                         font=("TkDefaultFont", 9, "bold"))
        led = LED_RED if self.active or self.held else LED_OFF
        self.create_oval(61, 1, 68, 8, fill=led, outline="")


class TrigPad(tk.Canvas):
    """One firmware trigger key with mouse and QWERTY state feedback."""

    def __init__(self, parent, trig: int, key: str,
                 callback: Callable[[int, bool], None]):
        super().__init__(
            parent,
            width=62,
            height=78,
            bg=PANEL_BG,
            highlightthickness=0,
            cursor="hand2",
        )
        self.trig = trig
        self.key = key
        self.callback = callback
        self.active = False
        self.bind("<ButtonPress-1>", lambda _event: callback(trig, True))
        self.bind("<ButtonRelease-1>", lambda _event: callback(trig, False))
        self.bind("<Leave>", lambda _event: callback(trig, False))
        self.redraw()

    def set_active(self, active: bool) -> None:
        if self.active != active:
            self.active = active
            self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        face = "#4a2a20" if self.active else "#242725"
        edge = LED_ORANGE if self.active else CONTROL_EDGE
        self.create_rectangle(4, 14, 58, 64, fill=face, outline=edge, width=2)
        self.create_text(31, 36, text=f"{self.trig:02d}", fill=TEXT,
                         font=("TkDefaultFont", 11, "bold"))
        self.create_text(31, 72, text=self.key, fill=MUTED,
                         font=("TkDefaultFont", 8, "bold"))
        led = LED_ORANGE if self.active else LED_OFF
        self.create_oval(27, 4, 35, 12, fill=led, outline="")


class VirtualKnob(tk.Canvas):
    """Mouse-draggable 0..127 control that emits relative encoder deltas."""

    def __init__(self, parent, name: str, callback: Callable[[str, int, int], None],
                 initial_value: int = 64):
        super().__init__(
            parent,
            width=72,
            height=94,
            bg=PANEL_INSET,
            highlightthickness=0,
            cursor="sb_v_double_arrow",
            takefocus=True,
        )
        self.name = name
        self.callback = callback
        self.value = clamp_panel_value(initial_value)
        self.drag_y = 0
        self.drag_value = self.value
        self.bind("<ButtonPress-1>", self.begin_drag)
        self.bind("<B1-Motion>", self.drag)
        self.bind("<MouseWheel>", self.wheel)
        self.bind("<Button-4>", lambda _event: self.adjust(1))
        self.bind("<Button-5>", lambda _event: self.adjust(-1))
        self.bind("<Double-Button-1>", lambda _event: self.set_value(64))
        self.bind("<KeyPress>", self.key_press)
        self.bind("<FocusIn>", lambda _event: self.redraw())
        self.bind("<FocusOut>", lambda _event: self.redraw())
        self.redraw()

    def begin_drag(self, event) -> None:
        self.focus_set()
        self.drag_y = event.y_root
        self.drag_value = self.value

    def drag(self, event) -> None:
        self.set_value(self.drag_value + round((self.drag_y - event.y_root) / 2))

    def wheel(self, event) -> str:
        self.focus_set()
        self.adjust(1 if event.delta > 0 else -1)
        return "break"

    def key_press(self, event) -> str | None:
        key = event.keysym
        if key in {"Up", "Right"}:
            self.adjust(1)
        elif key in {"Down", "Left"}:
            self.adjust(-1)
        elif key == "Prior":
            self.adjust(8)
        elif key == "Next":
            self.adjust(-8)
        elif key == "Home":
            self.set_value(0)
        elif key == "End":
            self.set_value(127)
        else:
            return None
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
        focused = self.focus_get() == self
        self.create_text(36, 9, text=self.name, fill=TEXT,
                         font=("TkDefaultFont", 9, "bold"))
        for index in range(11):
            tick = math.radians(225 - (270 * index / 10))
            x1 = 36 + 25 * math.cos(tick)
            y1 = 44 - 25 * math.sin(tick)
            x2 = 36 + 28 * math.cos(tick)
            y2 = 44 - 28 * math.sin(tick)
            self.create_line(x1, y1, x2, y2, fill="#656a66", width=1)
        outline = LED_ORANGE if focused else CONTROL_EDGE
        self.create_oval(15, 23, 57, 65, fill="#292c2a", outline=outline, width=2)
        self.create_oval(20, 28, 52, 60, fill="#202220", outline="#111211")
        angle = math.radians(225 - (270 * self.value / 127))
        x = 36 + 14 * math.cos(angle)
        y = 44 - 14 * math.sin(angle)
        self.create_line(36, 44, x, y, fill=OLED_PIXEL, width=3)
        self.create_text(36, 82, text=f"{self.value:03d}", fill=MUTED,
                         font=("TkFixedFont", 9, "bold"))


class PanelApp:
    def __init__(self, root: tk.Tk, frame_file: Path, event_file: Path,
                 scale: int = 6, filter2_enabled: bool = False) -> None:
        self.root = root
        self.frame_file = frame_file
        self.event_file = event_file
        self.scale = scale
        self.last_mtime = 0
        self.photo = None
        self.held_trigs: dict[int, set[str]] = {}
        self.pending_key_releases: dict[str, str] = {}
        self.page_widgets: dict[str, PanelButton] = {}
        self.trig_widgets: dict[int, TrigPad] = {}
        self.active_page: str | None = None
        self.filter2_enabled = filter2_enabled
        self.filter2_values = [64] * 8
        self.filter2_window: tk.Toplevel | None = None

        root.title("Analog Rytm MKII — Firmware Emulator")
        root.configure(bg="#0b0c0c")
        window_w, window_h = panel_window_size(scale)
        root.geometry(f"{window_w}x{window_h}")
        root.resizable(False, False)

        shell = tk.Frame(
            root,
            bg=PANEL_BG,
            width=window_w - 24,
            height=window_h - 24,
            highlightthickness=1,
            highlightbackground=PANEL_EDGE,
        )
        shell.pack(padx=12, pady=12, fill="both", expand=True)
        shell.pack_propagate(False)

        header = tk.Frame(shell, bg=PANEL_BG, height=38)
        header.pack(fill="x", padx=18, pady=(10, 4))
        header.pack_propagate(False)
        tk.Label(header, text="PHOTON OS", fg=TEXT, bg=PANEL_BG,
                 font=("TkDefaultFont", 15, "bold")).pack(side="left")
        tk.Label(header, text="AR MKII  /  OS 1.72 EMULATOR", fg=MUTED,
                 bg=PANEL_BG, font=("TkDefaultFont", 9, "bold")).pack(
                     side="left", padx=(14, 0), pady=(5, 0))
        self.frame_status = tk.StringVar(value="WAITING FOR FIRMWARE OLED")
        tk.Label(header, textvariable=self.frame_status, fg=MUTED, bg=PANEL_BG,
                 font=("TkFixedFont", 8)).pack(side="right", pady=(5, 0))
        self.filter2_button = tk.Button(
            header,
            text="FILTER 2",
            command=self.toggle_filter2,
            state="normal" if filter2_enabled else "disabled",
            fg=TEXT,
            bg=CONTROL_FACE,
            activeforeground=TEXT,
            activebackground="#353936",
            disabledforeground="#5d625e",
            relief="flat",
            font=("TkDefaultFont", 8, "bold"),
            padx=10,
            pady=2,
        )
        self.filter2_button.pack(side="right", padx=(0, 12), pady=(1, 0))

        work = tk.Frame(shell, bg=PANEL_BG)
        work.pack(fill="x", padx=18)

        screen_panel = tk.Frame(
            work,
            bg="#090a09",
            padx=10,
            pady=10,
            highlightthickness=2,
            highlightbackground="#414542",
        )
        screen_panel.pack(side="left", anchor="n")
        self.canvas = tk.Canvas(
            screen_panel,
            width=W * scale,
            height=H * scale,
            bg="black",
            highlightthickness=0,
        )
        self.canvas.pack()
        self.image_id = self.canvas.create_image(0, 0, anchor="nw")

        controls = tk.Frame(work, bg=PANEL_INSET, padx=8, pady=6)
        controls.pack(side="right", fill="both", expand=True, padx=(14, 0))

        enc_frame = tk.Frame(controls, bg=PANEL_INSET)
        enc_frame.pack()
        for index, name in enumerate("ABCDEFGH"):
            VirtualKnob(enc_frame, name, self.encoder).grid(
                row=index // 4, column=index % 4, padx=1, pady=1)

        lower_controls = tk.Frame(controls, bg=PANEL_INSET)
        lower_controls.pack(pady=(4, 0))
        VirtualKnob(lower_controls, "I", self.encoder).grid(
            row=0, column=0, rowspan=3, padx=(0, 5))
        for index, name in enumerate(PROVEN_BUTTONS):
            widget = PanelButton(lower_controls, name, self.panel_button)
            widget.grid(row=index // 3, column=(index % 3) + 1, padx=1, pady=1)
            self.page_widgets[name] = widget

        self.status = tk.StringVar(value="STARTING FIRMWARE…")
        status_bar = tk.Frame(shell, bg="#111311", height=28)
        status_bar.pack(fill="x", padx=18, pady=(8, 4))
        status_bar.pack_propagate(False)
        tk.Label(status_bar, textvariable=self.status, fg=TEXT, bg="#111311",
                 anchor="w", font=("TkFixedFont", 9)).pack(
                     side="left", fill="x", expand=True, padx=8)
        tk.Label(status_bar, text="LIVE PANEL BRIDGE", fg=LED_ORANGE,
                 bg="#111311", font=("TkFixedFont", 8, "bold")).pack(
                     side="right", padx=8)

        trig_frame = tk.Frame(shell, bg=PANEL_BG)
        trig_frame.pack(padx=18, pady=(2, 0))
        for trig in range(1, 17):
            widget = TrigPad(trig_frame, trig, TRIG_KEYS[trig], self.trig)
            widget.grid(row=0, column=trig - 1, padx=2)
            self.trig_widgets[trig] = widget

        tk.Label(
            shell,
            text=("QWERTYUI / ASDFGHJK  •  DRAG OR SCROLL ENCODERS  •  "
                  "ARROWS ±1  •  PAGE ±8  •  HOME/END 0/127"),
            fg=MUTED,
            bg=PANEL_BG,
            font=("TkFixedFont", 8),
        ).pack(anchor="w", padx=22, pady=(3, 0))

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
        if pressed and name in PAGE_BUTTONS:
            self.active_page = name
            for page_name in PAGE_BUTTONS:
                self.page_widgets[page_name].set_active(page_name == name)
        self.emit("button", name, "press" if pressed else "release")
        if pressed:
            label = "PAGE" if name in PAGE_BUTTONS else "KEY"
            self.status.set(f"{label} {name}  /  PRESS")
        elif name not in PAGE_BUTTONS:
            self.status.set(f"KEY {name}  /  RELEASE")

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
        widget = self.trig_widgets.get(trig)
        if widget is not None:
            widget.set_active(is_pressed)
        self.emit("trig", str(trig), "press" if is_pressed else "release")
        state = "ON" if is_pressed else "OFF"
        self.status.set(f"TRIG {trig:02d}  /  {state}  /  {source.upper()}")

    def encoder(self, name: str, delta: int, value: int | None = None) -> None:
        self.emit("encoder", name, delta)
        suffix = f"  /  VALUE {value:03d}" if value is not None else ""
        self.status.set(f"ENCODER {name}  /  DELTA {delta:+d}{suffix}")

    def toggle_filter2(self) -> None:
        if not self.filter2_enabled:
            return
        if self.filter2_window is not None and self.filter2_window.winfo_exists():
            self.filter2_window.destroy()
            self.filter2_window = None
            return

        window = tk.Toplevel(self.root)
        self.filter2_window = window
        window.title("Filter 2 — Eight-Lane Emulator Extension")
        window.configure(bg=PANEL_BG)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", self.toggle_filter2)
        title = tk.Frame(window, bg=PANEL_BG)
        title.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(title, text="FILTER 2", fg=TEXT, bg=PANEL_BG,
                 font=("TkDefaultFont", 13, "bold")).pack(side="left")
        tk.Label(title, text="LIVE QEMU  /  LANES 1–8", fg=LED_ORANGE,
                 bg=PANEL_BG, font=("TkFixedFont", 8, "bold")).pack(
                     side="right", pady=(3, 0))
        knobs = tk.Frame(window, bg=PANEL_INSET, padx=8, pady=8)
        knobs.pack(padx=12, pady=(0, 6))
        for lane in range(8):
            VirtualKnob(
                knobs,
                str(lane + 1),
                lambda _name, delta, value, lane=lane:
                    self.filter2(lane, delta, value),
                self.filter2_values[lane],
            ).grid(row=0, column=lane, padx=1)
        tk.Label(
            window,
            text="Per-lane Q1.31 coefficient  •  default 064  •  runtime only",
            fg=MUTED,
            bg=PANEL_BG,
            font=("TkFixedFont", 8),
        ).pack(anchor="w", padx=14, pady=(0, 10))

    def filter2(self, lane: int, delta: int, value: int | None) -> None:
        if value is None:
            return
        self.filter2_values[lane] = value
        self.emit("filter2", str(lane), value)
        self.status.set(
            f"FILTER 2  /  LANE {lane + 1}  /  VALUE {value:03d}  /  {delta:+d}"
        )

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
                widget = self.trig_widgets.get(trig)
                if widget is not None:
                    widget.set_active(False)

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
                        OLED_PIXEL if current else "#000000",
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
            self.frame_status.set(f"OLED {len(data)}/1024 B")
            return
        self.draw(data[:FRAME_BYTES])
        self.last_mtime = st.st_mtime_ns
        nonzero = sum(v != 0 for v in data[:FRAME_BYTES])
        self.frame_status.set(f"FIRMWARE OLED  /  {nonzero:04d} ACTIVE BYTES")

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
    ap.add_argument("--filter2", action="store_true")
    args = ap.parse_args()
    root = tk.Tk()
    PanelApp(root, args.frame, args.events, args.scale, args.filter2)
    root.mainloop()


if __name__ == "__main__":
    main()
