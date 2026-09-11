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
import sys
import time
import tkinter as tk
from collections.abc import Callable

W, H = 128, 64
RAW_W, RAW_H = 64, 128
FRAME_BYTES = 1024
PROVEN_BUTTONS = ("TRIG", "SYN", "SMP", "FLTR", "AMP", "LFO", "YES", "NO")
PAGE_BUTTONS = PROVEN_BUTTONS[:6]
SKIN_DESIGN_W, SKIN_DESIGN_H = 1508, 864
SKIN_W, SKIN_H = 1400, 802
STATUS_H = 34
PANEL_ASPECT = SKIN_W / (SKIN_H + STATUS_H)
PANEL_BG = "#989da1"
PANEL_INSET = "#8f9498"
PANEL_EDGE = "#707579"
CONTROL_FACE = "#171918"
CONTROL_EDGE = "#303331"
TEXT = "#151817"
MUTED = "#264a5a"
LIGHT_TEXT = "#f1f3f1"
LED_OFF = "#321813"
LED_RED = "#ff4b2e"
LED_ORANGE = "#ff7a32"
OLED_PIXEL = "#d8f8ff"
QWERTY_TRIGS = {
    key: trig
    for trig, key in enumerate("qwertyuiasdfghjk", start=1)
}
TRIG_KEYS = {trig: key.upper() for key, trig in QWERTY_TRIGS.items()}
LFO2_WAVEFORMS = ("TRI", "SQR", "SAW", "RAMP", "SINE", "EXP", "RAND")
LFO2_MODES = ("LOOP", "ONE", "HALF", "HOLD")


def skin_point(x: int, y: int) -> tuple[int, int]:
    """Project approved-source coordinates onto the checked-in skin raster."""
    return round(x * SKIN_W / SKIN_DESIGN_W), round(y * SKIN_H / SKIN_DESIGN_H)


def skin_rect(x1: int, y1: int, x2: int, y2: int) -> tuple[int, int, int, int]:
    left, top = skin_point(x1, y1)
    right, bottom = skin_point(x2, y2)
    return left, top, right, bottom


OLED_RECT = skin_rect(716, 131, 993, 269)
KNOB_CENTERS = {
    "A": skin_point(1084, 146), "B": skin_point(1193, 146),
    "C": skin_point(1300, 146), "D": skin_point(1403, 146),
    "E": skin_point(1084, 257), "F": skin_point(1193, 257),
    "G": skin_point(1300, 257), "H": skin_point(1403, 257),
    "I": skin_point(636, 121),
}
BUTTON_RECTS = {
    "TRIG": skin_rect(1058, 349, 1109, 397),
    "SYN": skin_rect(1124, 349, 1177, 397),
    "SMP": skin_rect(1187, 349, 1240, 397),
    "FLTR": skin_rect(1251, 349, 1304, 397),
    "AMP": skin_rect(1316, 349, 1368, 397),
    "LFO": skin_rect(1381, 349, 1433, 397),
    "YES": skin_rect(752, 349, 803, 397),
    "NO": skin_rect(752, 416, 803, 464),
}
_TRIG_CENTERS = (103, 183, 265, 346, 427, 508, 589, 671,
                 752, 833, 915, 996, 1078, 1160, 1240, 1322)
TRIG_RECTS = {
    trig: skin_rect(center - 35, 702, center + 35, 772)
    for trig, center in enumerate(_TRIG_CENTERS, start=1)
}


def skin_asset_path() -> Path:
    """Resolve the faceplate both in source trees and frozen PyInstaller apps."""
    candidates = [Path(__file__).resolve().parent / "assets" / "photon_panel_neutral.png"]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.insert(0, Path(bundle) / "qemu" / "assets" /
                          "photon_panel_neutral.png")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("photographic panel skin is missing")


def clamp_panel_value(value: int) -> int:
    return max(0, min(127, value))


def panel_window_size(scale: int) -> tuple[int, int]:
    """Return the fixed pixel-registered photographic panel window size."""
    del scale
    return SKIN_W, SKIN_H + STATUS_H


class SkinState:
    """Compatibility state object used by existing page/Trig dispatch logic."""

    def __init__(self, redraw: Callable[[], None]):
        self.active = False
        self.redraw = redraw

    def set_active(self, active: bool) -> None:
        if self.active != active:
            self.active = active
            self.redraw()


class PanelButton(tk.Canvas):
    """Drawn panel key with a persistent page LED and momentary press state."""

    def __init__(self, parent, name: str,
                 callback: Callable[[str, bool], None]):
        super().__init__(
            parent,
            width=72,
            height=44,
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
        face = "#292c2a" if self.held else CONTROL_FACE
        edge = LED_ORANGE if focused else CONTROL_EDGE
        self.create_rectangle(5, 8, 67, 39, fill="#090a09", outline="#565a57")
        self.create_rectangle(8, 10, 64, 36, fill=face, outline=edge, width=1)
        self.create_text(36, 24, text=self.name, fill=LIGHT_TEXT,
                         font=("Helvetica", 8, "bold"))
        led = LED_RED if self.active or self.held else LED_OFF
        self.create_oval(60, 1, 67, 8, fill=led, outline="#222321")


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
        face = "#4a2a20" if self.active else "#151716"
        edge = LED_ORANGE if self.active else CONTROL_EDGE
        self.create_rectangle(4, 14, 58, 64, fill="#090a09", outline="#656965")
        self.create_rectangle(7, 16, 55, 60, fill=face, outline=edge, width=2)
        self.create_text(31, 37, text=str(self.trig), fill=LIGHT_TEXT,
                         font=("Helvetica", 11))
        self.create_text(31, 72, text=self.key, fill="#314e5b",
                         font=("Helvetica", 7, "bold"))
        led = LED_ORANGE if self.active else LED_OFF
        self.create_oval(27, 4, 35, 12, fill=led, outline="")


class VirtualKnob(tk.Canvas):
    """Mouse-draggable 0..127 control that emits relative encoder deltas."""

    def __init__(self, parent, name: str, callback: Callable[[str, int, int], None],
                 initial_value: int = 64):
        super().__init__(
            parent,
            width=88,
            height=104,
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
        cx, cy = 44, 49
        self.create_text(cx, 8, text=self.name, fill=TEXT,
                         font=("Helvetica", 9, "bold"))
        outline = LED_ORANGE if focused else CONTROL_EDGE
        # Layered face closely follows the broad, low-profile encoder caps in
        # the supplied Photon/hardware reference without depending on a raster.
        self.create_oval(15, 20, 75, 80, fill="#111312", outline="#696d69")
        self.create_oval(18, 22, 72, 76, fill="#202321", outline=outline, width=2)
        self.create_arc(21, 25, 69, 73, start=20, extent=150,
                        style="arc", outline="#343836", width=2)
        self.create_arc(21, 25, 69, 73, start=205, extent=125,
                        style="arc", outline="#0b0c0b", width=2)
        self.create_oval(26, 30, 64, 68, fill="#1b1d1c", outline="#252826")
        angle = math.radians(225 - (270 * self.value / 127))
        x = cx + 17 * math.cos(angle)
        y = cy - 17 * math.sin(angle)
        self.create_line(cx, cy, x, y, fill="#d7dcda", width=3)
        self.create_text(cx, 94, text=f"{self.value:03d}", fill=MUTED,
                         font=("TkFixedFont", 8, "bold"))


class PanelApp:
    def __init__(self, root: tk.Tk, frame_file: Path, event_file: Path,
                 scale: int = 6, filter2_enabled: bool = False,
                 audio_enabled: bool = False) -> None:
        self.root = root
        self.frame_file = frame_file
        self.event_file = event_file
        self.scale = scale
        self.last_mtime = 0
        self.photo = None
        self.held_trigs: dict[int, set[str]] = {}
        self.pending_key_releases: dict[str, str] = {}
        self.page_widgets: dict[str, PanelButton | SkinState] = {}
        self.trig_widgets: dict[int, TrigPad | SkinState] = {}
        self.active_page: str | None = None
        self.filter2_enabled = filter2_enabled
        self.audio_enabled = audio_enabled
        self.filter2_values = [64] * 8
        self.lfo2_rate_values = [64] * 8
        self.lfo2_depth_values = [64] * 8
        self.lfo2_waveforms = [0] * 8
        self.lfo2_modes = [0] * 8
        self.lfo2_enabled = [False] * 8
        self.lfo2_triggered = [False] * 8
        self.lfo2_lane = 0
        self.filter2_window: tk.Toplevel | None = None
        self.lfo2_lane_buttons: list[tk.Button] = []
        self.lfo2_rate_knob: VirtualKnob | None = None
        self.lfo2_depth_knob: VirtualKnob | None = None
        self.lfo2_wave_button: tk.Button | None = None
        self.lfo2_mode_button: tk.Button | None = None
        self.lfo2_enable_button: tk.Button | None = None
        self.lfo2_trigger_button: tk.Button | None = None
        self.lfo2_audition_button: tk.Button | None = None
        self.encoder_values = {name: 64 for name in KNOB_CENTERS}
        self.focused_encoder: str | None = None
        self.button_held: set[str] = set()
        self.mouse_control: tuple[str, str | int] | None = None
        self.drag_y = 0
        self.drag_value = 64

        root.title("Analog Rytm MKII — Firmware Emulator")
        root.configure(bg="#d2d3d1")
        window_w, window_h = panel_window_size(scale)
        root.geometry(f"{window_w}x{window_h}")
        root.resizable(False, False)
        self.canvas = tk.Canvas(root, width=SKIN_W, height=SKIN_H,
                                bg="#d2d3d1", highlightthickness=0,
                                cursor="hand2", takefocus=True)
        self.canvas.pack()
        self.skin_photo = tk.PhotoImage(file=str(skin_asset_path()))
        self.canvas.create_image(0, 0, anchor="nw", image=self.skin_photo,
                                 tags="skin")
        ox1, oy1, ox2, oy2 = OLED_RECT
        self.canvas.create_rectangle(ox1, oy1, ox2, oy2, fill="black",
                                     outline="", tags="oled-background")
        self.image_id = self.canvas.create_image(ox1, oy1, anchor="nw",
                                                 tags="oled")
        self.draw_photon_splash()
        for name in PROVEN_BUTTONS:
            self.page_widgets[name] = SkinState(self.redraw_skin_overlays)
        for trig in range(1, 17):
            self.trig_widgets[trig] = SkinState(self.redraw_skin_overlays)

        self.canvas.bind("<ButtonPress-1>", self.skin_press)
        self.canvas.bind("<ButtonRelease-1>", self.skin_release)
        self.canvas.bind("<B1-Motion>", self.skin_drag)
        self.canvas.bind("<Double-Button-1>", self.skin_double_click)
        self.canvas.bind("<Leave>", self.skin_release)
        self.canvas.bind("<MouseWheel>", self.skin_wheel)
        self.canvas.bind("<Button-4>", lambda event: self.skin_wheel(event, 1))
        self.canvas.bind("<Button-5>", lambda event: self.skin_wheel(event, -1))

        self.status = tk.StringVar(value="STARTING FIRMWARE…")
        self.frame_status = tk.StringVar(value="WAITING FOR FIRMWARE OLED")
        status_bar = tk.Frame(root, bg="#181a19", height=STATUS_H)
        status_bar.pack(fill="x")
        status_bar.pack_propagate(False)
        tk.Label(status_bar, textvariable=self.status, fg=LIGHT_TEXT, bg="#181a19",
                 anchor="w", font=("TkFixedFont", 9)).pack(
                     side="left", fill="x", expand=True, padx=8)
        tk.Label(status_bar, textvariable=self.frame_status, fg=MUTED,
                 bg="#181a19", font=("TkFixedFont", 8)).pack(
                     side="right", padx=(8, 10))
        self.filter2_button = tk.Button(
            status_bar, text="FILTER 2", command=self.toggle_filter2,
            state="normal" if filter2_enabled else "disabled",
            fg=LIGHT_TEXT, bg=CONTROL_FACE, activeforeground=LIGHT_TEXT,
            activebackground="#353936", disabledforeground="#5d625e",
            relief="flat", font=("TkDefaultFont", 8, "bold"), padx=10,
        )
        self.filter2_button.pack(side="right", padx=(4, 0), pady=4)
        tk.Label(status_bar, text="LIVE PANEL BRIDGE", fg=LED_ORANGE,
                 bg="#181a19", font=("TkFixedFont", 8, "bold")).pack(
                     side="right", padx=(8, 4))

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind_all("<KeyPress>", self.key_press, add="+")
        self.root.bind_all("<KeyRelease>", self.key_release, add="+")
        self.root.bind("<FocusOut>", self.focus_lost, add="+")
        self.poll()

    @staticmethod
    def point_in_rect(x: int, y: int,
                      rect: tuple[int, int, int, int]) -> bool:
        x1, y1, x2, y2 = rect
        return x1 <= x <= x2 and y1 <= y <= y2

    def knob_at(self, x: int, y: int) -> str | None:
        radius = round(42 * SKIN_W / SKIN_DESIGN_W)
        for name, (cx, cy) in KNOB_CENTERS.items():
            if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2:
                return name
        return None

    def control_at(self, x: int, y: int) -> tuple[str, str | int] | None:
        knob = self.knob_at(x, y)
        if knob is not None:
            return "encoder", knob
        for name, rect in BUTTON_RECTS.items():
            if self.point_in_rect(x, y, rect):
                return "button", name
        for trig, rect in TRIG_RECTS.items():
            if self.point_in_rect(x, y, rect):
                return "trig", trig
        return None

    def skin_press(self, event) -> str:
        self.canvas.focus_set()
        control = self.control_at(event.x, event.y)
        self.mouse_control = control
        if control is None:
            return "break"
        kind, value = control
        if kind == "encoder":
            name = str(value)
            self.focused_encoder = name
            self.drag_y = event.y_root
            self.drag_value = self.encoder_values[name]
            self.redraw_skin_overlays()
        elif kind == "button":
            name = str(value)
            self.button_held.add(name)
            self.redraw_skin_overlays()
            self.panel_button(name, True)
        else:
            self.trig(int(value), True, "mouse")
        return "break"

    def skin_release(self, _event=None) -> str:
        control = self.mouse_control
        self.mouse_control = None
        if control is None:
            return "break"
        kind, value = control
        if kind == "button":
            name = str(value)
            self.button_held.discard(name)
            self.panel_button(name, False)
            self.redraw_skin_overlays()
        elif kind == "trig":
            self.trig(int(value), False, "mouse")
        return "break"

    def skin_drag(self, event) -> str:
        if self.mouse_control is None or self.mouse_control[0] != "encoder":
            return "break"
        name = str(self.mouse_control[1])
        self.set_skin_encoder(
            name, self.drag_value + round((self.drag_y - event.y_root) / 2)
        )
        return "break"

    def skin_wheel(self, event, direction: int | None = None) -> str:
        name = self.knob_at(event.x, event.y)
        if name is None:
            return "break"
        self.canvas.focus_set()
        self.focused_encoder = name
        if direction is None:
            direction = 1 if event.delta > 0 else -1
        self.set_skin_encoder(name, self.encoder_values[name] + direction)
        return "break"

    def skin_double_click(self, event) -> str:
        name = self.knob_at(event.x, event.y)
        if name is not None:
            self.focused_encoder = name
            self.set_skin_encoder(name, 64)
        return "break"

    def set_skin_encoder(self, name: str, value: int) -> None:
        value = clamp_panel_value(value)
        old = self.encoder_values[name]
        if value == old:
            return
        self.encoder_values[name] = value
        self.encoder(name, value - old, value)
        self.redraw_skin_overlays()

    def redraw_skin_overlays(self) -> None:
        self.canvas.delete("control-overlay")
        orange = LED_ORANGE
        for name, state in self.page_widgets.items():
            if state.active or name in self.button_held:
                self.canvas.create_rectangle(
                    *BUTTON_RECTS[name], outline=orange, width=2,
                    tags="control-overlay",
                )
        for trig, state in self.trig_widgets.items():
            if state.active:
                self.canvas.create_rectangle(
                    *TRIG_RECTS[trig], outline=orange, width=3,
                    tags="control-overlay",
                )
        focused = getattr(self, "focused_encoder", None)
        if focused in KNOB_CENTERS:
            cx, cy = KNOB_CENTERS[focused]
            radius = round(38 * SKIN_W / SKIN_DESIGN_W)
            self.canvas.create_oval(
                cx - radius, cy - radius, cx + radius, cy + radius,
                outline=orange, width=2, tags="control-overlay",
            )
        self.canvas.tag_raise("control-overlay")

    def draw_photon_splash(self) -> None:
        """Show the supplied-reference identity until firmware owns the OLED."""
        x1, y1, x2, y2 = OLED_RECT
        width, height = x2 - x1, y2 - y1
        cyan = OLED_PIXEL
        self.canvas.create_text(
            x1 + width // 2, y1 + height // 2 - 13,
            text="PHOTON", fill=cyan, tags="splash",
            font=("Courier", 17, "bold"),
        )
        self.canvas.create_text(
            x1 + width // 2, y1 + height // 2 + 13,
            text="OS", fill=cyan, tags="splash",
            font=("Courier", 9, "bold"),
        )
        line = 42
        self.canvas.create_line(
            x1 + width // 2 - line, y1 + height // 2 + 30,
            x1 + width // 2 + line, y1 + height // 2 + 30,
            fill=cyan, width=1, tags="splash",
        )

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
        window.title("Filter 2 + LFO2 — Eight-Lane Emulator Extension")
        window.configure(bg=PANEL_BG)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", self.toggle_filter2)
        title = tk.Frame(window, bg=PANEL_BG)
        title.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(title, text="FILTER 2  +  LFO2", fg=TEXT, bg=PANEL_BG,
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
                    self.filter2(lane, delta, value, True),
                self.filter2_values[lane],
            ).grid(row=0, column=lane, padx=1)

        lane_bar = tk.Frame(window, bg=PANEL_BG)
        lane_bar.pack(fill="x", padx=12, pady=(2, 5))
        tk.Label(lane_bar, text="EDIT LFO2 LANE", fg=MUTED, bg=PANEL_BG,
                 font=("TkFixedFont", 8, "bold")).pack(side="left", padx=(2, 8))
        self.lfo2_lane_buttons = []
        for lane in range(8):
            button = tk.Button(
                lane_bar, text=str(lane + 1),
                command=lambda lane=lane: self.select_lfo2_lane(lane),
                width=3, relief="flat", fg=LIGHT_TEXT, bg=CONTROL_FACE,
                activeforeground=LIGHT_TEXT, activebackground="#353936",
                font=("TkDefaultFont", 8, "bold"),
            )
            button.pack(side="left", padx=1)
            self.lfo2_lane_buttons.append(button)

        editor = tk.Frame(window, bg=PANEL_INSET, padx=10, pady=8)
        editor.pack(fill="x", padx=12, pady=(0, 6))
        self.lfo2_rate_knob = VirtualKnob(
            editor, "RATE",
            lambda _name, delta, value: self.lfo2_knob("rate", delta, value),
        )
        self.lfo2_rate_knob.grid(row=0, column=0, rowspan=2, padx=(0, 8))
        self.lfo2_depth_knob = VirtualKnob(
            editor, "DEPTH",
            lambda _name, delta, value: self.lfo2_knob("depth", delta, value),
        )
        self.lfo2_depth_knob.grid(row=0, column=1, rowspan=2, padx=(0, 12))
        self.lfo2_wave_button = self.drawer_button(
            editor, "WAVE", lambda: self.cycle_lfo2("waveform"), 12
        )
        self.lfo2_wave_button.grid(row=0, column=2, padx=2, pady=2)
        self.lfo2_mode_button = self.drawer_button(
            editor, "MODE", lambda: self.cycle_lfo2("mode"), 12
        )
        self.lfo2_mode_button.grid(row=1, column=2, padx=2, pady=2)
        self.lfo2_enable_button = self.drawer_button(
            editor, "LFO OFF", lambda: self.toggle_lfo2_flag("enable"), 11
        )
        self.lfo2_enable_button.grid(row=0, column=3, padx=2, pady=2)
        self.lfo2_trigger_button = self.drawer_button(
            editor, "FREE RUN", lambda: self.toggle_lfo2_flag("trigger"), 11
        )
        self.lfo2_trigger_button.grid(row=1, column=3, padx=2, pady=2)
        self.drawer_button(editor, "RESET PHASE", self.reset_lfo2, 12).grid(
            row=0, column=4, padx=(10, 2), pady=2
        )
        self.lfo2_audition_button = self.drawer_button(
            editor, "AUDITION · 8 BLOCKS", self.audition_lfo2, 18
        )
        self.lfo2_audition_button.configure(
            state="normal" if self.audio_enabled else "disabled"
        )
        self.lfo2_audition_button.grid(row=1, column=4, padx=(10, 2), pady=2)
        self.select_lfo2_lane(self.lfo2_lane)
        tk.Label(
            window,
            text=("Per-lane runtime state  •  selectors cycle on click  •  "
                  + ("audition uses selected Trig"
                     if self.audio_enabled else "start with --audio to audition")),
            fg=MUTED,
            bg=PANEL_BG,
            font=("TkFixedFont", 8),
        ).pack(anchor="w", padx=14, pady=(0, 10))

    @staticmethod
    def drawer_button(parent, text: str, command, width: int) -> tk.Button:
        return tk.Button(
            parent, text=text, command=command, width=width, relief="flat",
            fg=LIGHT_TEXT, bg=CONTROL_FACE, activeforeground=LIGHT_TEXT,
            activebackground="#353936", disabledforeground="#5d625e",
            font=("TkDefaultFont", 8, "bold"), padx=4, pady=5,
        )

    def filter2(self, lane: int, delta: int, value: int | None,
                select: bool = False) -> None:
        if value is None:
            return
        self.filter2_values[lane] = value
        if select:
            self.select_lfo2_lane(lane)
        self.emit("filter2", str(lane), value)
        self.status.set(
            f"FILTER 2  /  LANE {lane + 1}  /  VALUE {value:03d}  /  {delta:+d}"
        )

    def select_lfo2_lane(self, lane: int) -> None:
        self.lfo2_lane = max(0, min(7, lane))
        for index, button in enumerate(self.lfo2_lane_buttons):
            selected = index == self.lfo2_lane
            button.configure(bg="#5a3022" if selected else CONTROL_FACE,
                             fg=LED_ORANGE if selected else LIGHT_TEXT)
        for knob, values in (
            (self.lfo2_rate_knob, self.lfo2_rate_values),
            (self.lfo2_depth_knob, self.lfo2_depth_values),
        ):
            if knob is not None:
                knob.value = values[self.lfo2_lane]
                knob.redraw()
        if self.lfo2_wave_button is not None:
            self.lfo2_wave_button.configure(
                text=f"WAVE · {LFO2_WAVEFORMS[self.lfo2_waveforms[self.lfo2_lane]]}"
            )
        if self.lfo2_mode_button is not None:
            self.lfo2_mode_button.configure(
                text=f"MODE · {LFO2_MODES[self.lfo2_modes[self.lfo2_lane]]}"
            )
        if self.lfo2_enable_button is not None:
            enabled = self.lfo2_enabled[self.lfo2_lane]
            self.lfo2_enable_button.configure(
                text="LFO ON" if enabled else "LFO OFF",
                fg=LED_ORANGE if enabled else LIGHT_TEXT,
            )
        if self.lfo2_trigger_button is not None:
            triggered = self.lfo2_triggered[self.lfo2_lane]
            self.lfo2_trigger_button.configure(
                text="NOTE RETRIG" if triggered else "FREE RUN",
                fg=LED_ORANGE if triggered else LIGHT_TEXT,
            )
        if self.lfo2_audition_button is not None:
            self.lfo2_audition_button.configure(
                text=f"AUDITION TRIG {self.lfo2_lane + 1} · 8 BLOCKS"
            )

    def emit_lfo2(self, parameter: str, value: int) -> None:
        self.emit("lfo2", f"{self.lfo2_lane}:{parameter}", value)
        self.status.set(
            f"LFO2  /  LANE {self.lfo2_lane + 1}  /  {parameter.upper()} {value}"
        )

    def lfo2_knob(self, parameter: str, delta: int, value: int) -> None:
        values = (self.lfo2_rate_values if parameter == "rate"
                  else self.lfo2_depth_values)
        values[self.lfo2_lane] = value
        self.emit_lfo2(parameter, value)

    def cycle_lfo2(self, parameter: str) -> None:
        values, count = (
            (self.lfo2_waveforms, len(LFO2_WAVEFORMS))
            if parameter == "waveform"
            else (self.lfo2_modes, len(LFO2_MODES))
        )
        values[self.lfo2_lane] = (values[self.lfo2_lane] + 1) % count
        self.emit_lfo2(parameter, values[self.lfo2_lane])
        self.select_lfo2_lane(self.lfo2_lane)

    def toggle_lfo2_flag(self, parameter: str) -> None:
        values = (self.lfo2_enabled if parameter == "enable"
                  else self.lfo2_triggered)
        values[self.lfo2_lane] = not values[self.lfo2_lane]
        self.emit_lfo2(parameter, int(values[self.lfo2_lane]))
        self.select_lfo2_lane(self.lfo2_lane)

    def reset_lfo2(self) -> None:
        self.emit_lfo2("reset", 1)

    def audition_lfo2(self) -> None:
        if not self.audio_enabled:
            return
        trig = self.lfo2_lane + 1
        self.trig(trig, True, "audition")
        self.root.after(35, lambda: self.trig(trig, False, "audition"))

    def key_press(self, event) -> str | None:
        key = event.keysym.lower()
        focused = getattr(self, "focused_encoder", None)
        if focused in self.encoder_values:
            changes = {
                "up": 1, "right": 1, "down": -1, "left": -1,
                "prior": 8, "next": -8,
            }
            if key in changes:
                self.set_skin_encoder(
                    focused, self.encoder_values[focused] + changes[key]
                )
                return "break"
            if key in {"home", "end"}:
                self.set_skin_encoder(focused, 0 if key == "home" else 127)
                return "break"
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
        self.canvas.delete("splash")
        pix = self.decode_presented(data)
        x1, y1, x2, y2 = OLED_RECT
        width, height = x2 - x1, y2 - y1
        display = tk.PhotoImage(width=width, height=height)
        display.put("#000000", to=(0, 0, width, height))
        for y, row in enumerate(pix):
            start = 0
            current = row[0]
            for x in range(1, W + 1):
                value = row[x] if x < W else 1 - current
                if value != current:
                    if current:
                        sx1 = round(start * width / W)
                        sx2 = round(x * width / W)
                        sy1 = round(y * height / H)
                        sy2 = round((y + 1) * height / H)
                        display.put(OLED_PIXEL, to=(sx1, sy1, sx2, sy2))
                    start = x
                    current = value
        self.photo = display
        self.canvas.itemconfigure(self.image_id, image=self.photo)
        self.canvas.tag_raise("control-overlay")

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
    PanelApp(root, args.frame, args.events, args.scale, args.filter2, False)
    root.mainloop()


if __name__ == "__main__":
    main()
