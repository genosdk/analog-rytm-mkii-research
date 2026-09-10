#!/usr/bin/env python3
"""Local Rytm II research controller backed by the proven emulator shim."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import sys
import tempfile
import threading
import time
import wave
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "research"
STATIC = Path(__file__).resolve().parent / "static"
sys.path.insert(0, str(RESEARCH))

from audio_callback_probe import AUDIO_CALLBACK, EXPECTED_MAIN_SHA256, RETURN_PC, prepared_machine  # noqa: E402
from filter2_coefficient_slew_probe import control_to_q31  # noqa: E402
from filter2_publication_shim_probe import VIRTUAL_INDEX_BASE, target_address  # noqa: E402
from filter2_eight_lane_probe import FILTER2_STATE0, FILTER2_STATE_STRIDE  # noqa: E402
from filter2_lfo2_cutoff_binding_probe import (  # noqa: E402
    COMBINED_FLAGS,
    FILTER_SYMBOLS,
    LFO2_MASK_ADDRESS,
    LFO2_STATE0,
    LFO2_STATE_STRIDE,
    cpu_from_baseline,
    plane_lanes,
)
from filter2_unity_kernel_probe import (  # noqa: E402
    FILTER2_MASK_ADDRESS,
    FLAGS_ADDRESS,
    MIXER,
    install_input,
    install_tables,
)
from lfo2_control_publication_probe import (  # noqa: E402
    DEPTH_INDEX_BASE,
    ENABLE_INDEX_BASE,
    RATE_INDEX_BASE,
    RESET_INDEX_BASE,
    RANDOM_INDEX_OFFSET,
    TRIGGER_INDEX_BASE,
    TRIGGER_MASK_ADDRESS,
)
from lfo2_extended_waveform_probe import build_candidate  # noqa: E402
from lfo2_waveform_mode_probe import (  # noqa: E402
    CONFIG_OFFSET,
    MODE_INDEX_BASE,
    WAVE_INDEX_BASE,
    WAVE_SHIM_BASE,
)
from note_event_constructor_probe import (  # noqa: E402
    EVENT_FLAGS as NOTE_EVENT_FLAGS,
    EVENT_INPUT,
    EVENT_NOTE,
    EVENT_SOURCE_MASK,
    EVENT_TRACK,
    EVENT_TYPE,
    HELD_SOURCE_MASK_BASE,
    NOTE_EVENT_CONSTRUCTOR,
    NOTE_OFF,
    NOTE_ON,
    QWERTY_SOURCE_MASK,
)
from trigger_queue_probe import (  # noqa: E402
    CONTROL_SNAPSHOT_A,
    CONTROL_SNAPSHOT_POINTER,
    EVENT_VALUE,
    QUEUE,
    QUEUE_CAPACITY,
    QUEUE_INITIALIZER,
    QUEUE_INSTALLER,
    QUEUE_RING,
    TRIGGER_RECORD,
    load_emulator,
    run_complete_callback,
    stock_call,
)

LANES = 8
ALL_LANES_MASK = 0x00FF
DEFAULT_CONTROL = 64
WAVEFORMS = {
    "triangle": 0,
    "square": 1,
    "saw": 2,
    "ramp": 3,
    "sine": 4,
    "exponential": 5,
    "random": 6,
}
MODES = {"loop": 0, "one-shot": 1, "half-shot": 2, "hold": 3}
LFO2_PARAMETERS = {
    "waveform": WAVE_INDEX_BASE,
    "mode": MODE_INDEX_BASE,
    "trigger": TRIGGER_INDEX_BASE,
    "enable": ENABLE_INDEX_BASE,
    "reset": RESET_INDEX_BASE,
    "rate": RATE_INDEX_BASE,
    "depth": DEPTH_INDEX_BASE,
}
LIVE_PITCH = 0x80006388
TRACK_CHROMATIC_MODE_SOURCE = 0x412FACA1
LIVE_CHROMATIC_MODE = 0x8000EA18
LIVE_PITCH_READ_PC = 0x4011CA7E
MIXER_RETURN = 0x4011CAE8
SAMPLE_RATE = 48_000
PREVIEW_SECONDS = 0.75
PREVIEW_AMPLITUDE = 0x10000000
MIXER_GUARD_BITS = 6
VOICE_RESET_FLAG = 0x80
NOTE_KEYS = {
    "a": 48,
    "w": 49,
    "s": 50,
    "e": 51,
    "d": 52,
    "f": 53,
    "t": 54,
    "g": 55,
    "y": 56,
    "h": 57,
    "u": 58,
    "j": 59,
    "k": 60,
}
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


def clamp_control(value: int) -> int:
    return max(0, min(127, int(value)))


class EmulatorBridge:
    """Long-lived emulator instance that calls the Filter2 publication shim."""

    def __init__(self, stock_main: Path, emulator_path: Path):
        stock = stock_main.read_bytes()
        digest = hashlib.sha256(stock).hexdigest()
        if digest != EXPECTED_MAIN_SHA256:
            raise ValueError(f"unexpected MAIN SHA-256: {digest}")
        candidate, _ = build_candidate(stock, False)
        self._stock = stock
        self._temporary = tempfile.NamedTemporaryFile(suffix=".bin")
        Path(self._temporary.name).write_bytes(candidate)
        self._module = load_emulator(emulator_path)
        self.bus, self.cpu, _ = prepared_machine(self._module, Path(self._temporary.name))
        self._callback_sp = self.cpu.a[7]
        stock_call(self.cpu, QUEUE_INITIALIZER, [QUEUE, 0, QUEUE_RING, QUEUE_CAPACITY])
        stock_call(self.cpu, QUEUE_INSTALLER, [QUEUE])
        self.bus.write(CONTROL_SNAPSHOT_POINTER, 4, CONTROL_SNAPSHOT_A)
        # Stock callback code copies this per-track source byte into its live
        # gate, then selects LIVE_PITCH instead of the fixed note-60 fallback.
        self.bus.write(TRACK_CHROMATIC_MODE_SOURCE, 1, 1)  # 1 = synth
        self.bus.write(FLAGS_ADDRESS, 4, COMBINED_FLAGS)
        self.bus.write(FILTER2_MASK_ADDRESS, 2, ALL_LANES_MASK)
        self.lock = threading.RLock()
        self._callback_baseline: dict[str, Any] | None = None
        self.callback_count = 0
        self.stock_sha256 = digest
        self.candidate_sha256 = hashlib.sha256(candidate).hexdigest()

    def capture_callback_baseline(self) -> None:
        """Freeze clean CPU registers while retaining the bridge's shared RAM."""
        with self.lock:
            self._callback_baseline = {
                "d": self.cpu.d.copy(), "a": self.cpu.a.copy(), "sr": self.cpu.sr,
                "ctrl": self.cpu.ctrl.copy(), "macsr": self.cpu.macsr,
                "mac_mask": self.cpu.mac_mask, "macc": self.cpu.macc.copy(),
            }

    @staticmethod
    def _plane_hash(lanes: list[list[int]]) -> str:
        payload = b"".join(
            (word & 0xFFFFFFFF).to_bytes(4, "big")
            for lane in lanes for word in lane
        )
        return hashlib.sha256(payload).hexdigest()

    def step_callbacks(self, count: int = 1) -> dict[str, Any]:
        """Advance the authentic callback to the proven pre-mixer boundary."""
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 32:
            raise ValueError("count must be an integer from 1 through 32")
        with self.lock:
            if self._callback_baseline is None:
                raise RuntimeError("callback baseline has not been captured")
            install_tables(self.bus, self._stock)
            callbacks = []
            for _ in range(count):
                install_input(self.bus, True)
                before_runtime = self.lfo2_runtime_snapshot()
                cpu = cpu_from_baseline(self._module, self.bus, self._callback_baseline)
                cpu.pushl(RETURN_PC)
                cpu.pc = AUDIO_CALLBACK
                start_steps = cpu.steps
                ingress = None
                multiply_calls = 0
                for _instruction in range(230_000):
                    if cpu.pc == MIXER:
                        break
                    if cpu.pc == FILTER_SYMBOLS["post_ingress"]:
                        ingress = plane_lanes(self.bus)
                    if cpu.pc == FILTER_SYMBOLS["multiply"]:
                        multiply_calls += 1
                    cpu.step()
                else:
                    raise RuntimeError("callback step did not reach the pre-mixer boundary")
                if ingress is None:
                    raise RuntimeError("callback step missed the post-ingress checkpoint")
                output = plane_lanes(self.bus)
                self.callback_count += 1
                callbacks.append({
                    "index": self.callback_count,
                    "instructions": cpu.steps - start_steps,
                    "multiply_calls": multiply_calls,
                    "ingress_sha256": self._plane_hash(ingress),
                    "output_sha256": self._plane_hash(output),
                    "phase_before": [lane["phase"] for lane in before_runtime["lanes"]],
                    "phase_after": [
                        lane["phase"] for lane in self.lfo2_runtime_snapshot()["lanes"]
                    ],
                    "boundary": f"0x{MIXER:08X}",
                })
            return {
                "count": count,
                "callback_count": self.callback_count,
                "callbacks": callbacks,
                "boundary": "pre-mixer",
                "synthetic_input": True,
            }

    @staticmethod
    def _signed32(value: int) -> int:
        return value - 0x100000000 if value & 0x80000000 else value

    def render_audio_preview(self, note: int, lane: int, count: int = 32) -> tuple[bytes, dict[str, Any]]:
        """Render a bounded generated tone through Filter2 and the stock mixer."""
        if not isinstance(note, int) or isinstance(note, bool) or not 0 <= note <= 127:
            raise ValueError("note must be an integer from 0 through 127")
        if not isinstance(lane, int) or isinstance(lane, bool) or not 0 <= lane < LANES:
            raise ValueError("lane must be an integer from 0 through 7")
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 32:
            raise ValueError("count must be an integer from 1 through 32")
        with self.lock:
            if self._callback_baseline is None:
                raise RuntimeError("callback baseline has not been captured")
            install_tables(self.bus, self._stock)
            frequency = 440.0 * (2.0 ** ((note - 69) / 12.0))
            phase = 0.0
            phase_step = 2.0 * math.pi * frequency / SAMPLE_RATE
            rendered_samples: list[int] = []
            callback_hashes = []
            start_callback_count = self.callback_count
            for _ in range(count):
                install_input(self.bus, False)
                source = []
                for _frame in range(32):
                    sample = int(round(math.sin(phase) * PREVIEW_AMPLITUDE))
                    source.append(sample & 0xFFFFFFFF)
                    phase = (phase + phase_step) % (2.0 * math.pi)
                cpu = cpu_from_baseline(self._module, self.bus, self._callback_baseline)
                cpu.pushl(RETURN_PC)
                cpu.pc = AUDIO_CALLBACK
                injected = False
                for _instruction in range(230_000):
                    if cpu.pc == FILTER_SYMBOLS["post_ingress"]:
                        for item_lane in range(LANES):
                            base = 0x800067F8 + item_lane * 0x80
                            words = source if item_lane == lane else [0] * 32
                            for frame, word in enumerate(words):
                                self.bus.write(base + frame * 4, 4, word)
                        injected = True
                    if cpu.pc == MIXER:
                        break
                    cpu.step()
                else:
                    raise RuntimeError("audio preview did not reach the stock mixer")
                if not injected:
                    raise RuntimeError("audio preview missed its generated-source boundary")
                filtered = plane_lanes(self.bus)
                mixer_writes: list[tuple[int, int]] = []
                original_write = self.bus.write

                def traced_write(address: int, size: int, value: int) -> None:
                    if cpu.pc == 0x4010A3B8 and size == 4:
                        mixer_writes.append((address, value & 0xFFFFFFFF))
                    original_write(address, size, value)

                self.bus.write = traced_write
                try:
                    for _instruction in range(10_000):
                        if cpu.pc == MIXER_RETURN:
                            break
                        cpu.step()
                    else:
                        raise RuntimeError("audio preview mixer did not return")
                finally:
                    self.bus.write = original_write
                if len(mixer_writes) != 256 or len({address for address, _ in mixer_writes}) != 256:
                    raise RuntimeError("audio preview observed unexpected stock mixer geometry")
                mixer_values = dict(mixer_writes)
                mixer_base = min(mixer_values)
                mixer_output = [
                    mixer_values[0x80000800 + frame * 0x40 + item_lane * 4]
                    for frame in range(32)
                    for item_lane in range(LANES)
                ]
                block = [
                    max(-32768, min(32767, self._signed32(
                        mixer_values[0x80000800 + frame * 0x40 + lane * 4]
                    ) >> MIXER_GUARD_BITS))
                    for frame in range(32)
                ]
                rendered_samples.extend(block)
                self.callback_count += 1
                callback_hashes.append({
                    "filter2_output": self._plane_hash(filtered),
                    "stock_mixer_base": f"0x{mixer_base:08X}",
                    "stock_mixer_nonzero_words": sum(value != 0 for value in mixer_values.values()),
                    "stock_mixer_output_sha256": hashlib.sha256(b"".join(
                        word.to_bytes(4, "big") for word in mixer_output
                    )).hexdigest(),
                })

            raw_pcm = b"".join(
                sample.to_bytes(2, "little", signed=True) * 2
                for sample in rendered_samples
            )
            target_frames = int(SAMPLE_RATE * PREVIEW_SECONDS)
            repeats = math.ceil(target_frames / len(rendered_samples))
            playback_samples = (rendered_samples * repeats)[:target_frames]
            playback_pcm = b"".join(
                sample.to_bytes(2, "little", signed=True) * 2
                for sample in playback_samples
            )
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setnchannels(2)
                wav.setsampwidth(2)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes(playback_pcm)
            wav_bytes = output.getvalue()
            metadata = {
                "note": note,
                "frequency_hz": round(frequency, 6),
                "lane": lane,
                "callbacks": count,
                "start_callback_count": start_callback_count,
                "callback_count": self.callback_count,
                "rendered_frames": len(rendered_samples),
                "playback_frames": len(playback_samples),
                "sample_rate_hz": SAMPLE_RATE,
                "channels": 2,
                "format": "signed 16-bit little-endian PCM WAV",
                "mixer_guard_bits": MIXER_GUARD_BITS,
                "source": "generated sine injected after stock external-audio ingress",
                "monitor_boundary": "selected stock mixer output lane",
                "processing": "runtime Filter2/LFO2 candidate followed by stock mixer 0x4010A2E0",
                "stock_mixer_fixture_state": "external/Filter2 source path active; other source planes remain fixture-dependent",
                "repeat_packaging": repeats > 1,
                "raw_pcm_sha256": hashlib.sha256(raw_pcm).hexdigest(),
                "wav_sha256": hashlib.sha256(wav_bytes).hexdigest(),
                "nonzero_samples": sum(sample != 0 for sample in rendered_samples),
                "minimum": min(rendered_samples),
                "maximum": max(rendered_samples),
                "callback_audit": callback_hashes,
            }
            return wav_bytes, metadata

    def publish(self, lane: int, value: int) -> dict[str, Any]:
        if not 0 <= lane < LANES:
            raise ValueError("lane must be 0..7")
        value = clamp_control(value)
        virtual_index = VIRTUAL_INDEX_BASE + lane
        expected_address = target_address(lane)
        expected_q31 = control_to_q31(value)
        writes: list[tuple[int, int, int]] = []
        with self.lock:
            original_write = self.bus.write

            def traced_write(address: int, size: int, written: int) -> None:
                if any(address == target_address(item) for item in range(LANES)):
                    writes.append((address, size, written & 0xFFFFFFFF))
                original_write(address, size, written)

            self.bus.write = traced_write
            try:
                steps = stock_call(self.cpu, WAVE_SHIM_BASE, [virtual_index, value])
            finally:
                self.bus.write = original_write
        if writes != [(expected_address, 4, expected_q31)]:
            raise RuntimeError(f"publication mismatch: {writes!r}")
        return {
            "lane": lane,
            "value": value,
            "virtual_index": f"0x{virtual_index:04X}",
            "q31": f"0x{expected_q31:08X}",
            "target_address": f"0x{expected_address:08X}",
            "instructions": steps,
            "single_aligned_store": expected_address % 4 == 0,
        }

    def publish_lfo2(self, lane: int, parameter: str, value: int) -> dict[str, Any]:
        if not 0 <= lane < LANES:
            raise ValueError("lane must be 0..7")
        if parameter not in LFO2_PARAMETERS:
            raise ValueError("unsupported LFO2 parameter")
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("LFO2 value must be an integer")
        limits = {"waveform": 6, "mode": 3, "trigger": 1, "enable": 1,
                  "reset": 1, "rate": 127, "depth": 127}
        if not 0 <= value <= limits[parameter]:
            raise ValueError(f"{parameter} value must be 0..{limits[parameter]}")
        virtual_index = LFO2_PARAMETERS[parameter] + lane
        with self.lock:
            steps = stock_call(self.cpu, WAVE_SHIM_BASE, [virtual_index, value])
        return {
            "lane": lane,
            "parameter": parameter,
            "value": value,
            "virtual_index": f"0x{virtual_index:04X}",
            "instructions": steps,
        }

    def lfo2_runtime_snapshot(self) -> dict[str, Any]:
        with self.lock:
            enable_mask = self.bus.read(LFO2_MASK_ADDRESS, 2)
            trigger_mask = self.bus.read(TRIGGER_MASK_ADDRESS, 2)
            lanes = []
            for lane in range(LANES):
                lbase = LFO2_STATE0 + lane * LFO2_STATE_STRIDE
                fbase = FILTER2_STATE0 + lane * FILTER2_STATE_STRIDE
                config = self.bus.read(fbase + CONFIG_OFFSET, 4)
                lanes.append({
                    "lane": lane,
                    "enabled": bool(enable_mask & (1 << lane)),
                    "trigger": bool(trigger_mask & (1 << lane)),
                    "waveform": config & 0x07,
                    "mode": (config >> 3) & 0x03,
                    "phase": f"0x{self.bus.read(lbase, 4):08X}",
                    "increment": f"0x{self.bus.read(lbase + 4, 4):08X}",
                    "depth": f"0x{self.bus.read(lbase + 8, 4):08X}",
                    "last_modulation": f"0x{self.bus.read(lbase + 12, 4):08X}",
                    "effective_target": f"0x{self.bus.read(fbase + 16, 4):08X}",
                    "random_index": self.bus.read(fbase + RANDOM_INDEX_OFFSET, 4),
                })
            return {
                "enable_mask": f"0x{enable_mask:04X}",
                "trigger_mask": f"0x{trigger_mask:04X}",
                "callback_count": self.callback_count,
                "lanes": lanes,
            }

    def trigger_note(self, note: int) -> dict[str, Any]:
        if not 0 <= note <= 127:
            raise ValueError("note must be 0..127")
        pitch_word = note << 16
        with self.lock:
            self._write_note_event(NOTE_ON, note)
            constructor_steps = stock_call(self.cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])
            if self.bus.read(TRIGGER_RECORD, 4) != NOTE_ON:
                raise RuntimeError("stock note-on constructor did not publish trigger state 1")
            pitch_reads: list[dict[str, Any]] = []
            original_read = self.bus.read

            def traced_read(address: int, size: int) -> int:
                value = original_read(address, size)
                if address == LIVE_PITCH and size == 4:
                    pitch_reads.append({
                        "pc": f"0x{self.cpu.pc:08X}",
                        "value": f"0x{value:08X}",
                    })
                return value

            self.bus.read = traced_read
            try:
                callback = run_complete_callback(self.cpu, self._callback_sp)
            finally:
                self.bus.read = original_read
            queue_count = self.bus.read(QUEUE + 4, 4)
            if queue_count == 0:
                raise RuntimeError("stock note trigger did not enqueue a command")
            ring_index = (queue_count - 1) & (QUEUE_CAPACITY - 1)
            command_pointer = self.bus.read(QUEUE_RING + ring_index * 4, 4)
            result = {
                "note": note,
                "encoded_pitch": f"0x{pitch_word:08X}",
                "live_pitch": f"0x{self.bus.read(LIVE_PITCH, 4):08X}",
                "constructor_instructions": constructor_steps,
                "callback_cases": callback["cases"],
                "pitch_consumer": {
                    "chromatic_mode": "synth",
                    "source_gate": f"0x{self.bus.read(TRACK_CHROMATIC_MODE_SOURCE, 1):02X}",
                    "live_gate": f"0x{self.bus.read(LIVE_CHROMATIC_MODE, 1):02X}",
                    "reads": pitch_reads,
                    "renderer_input_proven": (
                        bool(pitch_reads)
                        and {item["pc"] for item in pitch_reads} == {f"0x{LIVE_PITCH_READ_PC:08X}"}
                        and {item["value"] for item in pitch_reads} == {f"0x{pitch_word:08X}"}
                    ),
                },
                "one_shot_cleared": self.bus.read(EVENT_VALUE, 4) == 0,
                "queued_command": {
                    "pointer": f"0x{command_pointer:08X}",
                    "code": self.bus.read(command_pointer, 1),
                    "track_mask": f"0x{self.bus.read(command_pointer + 4, 4):08X}",
                    "queue_count": queue_count,
                },
            }
        if result["live_pitch"] != result["encoded_pitch"]:
            raise RuntimeError("stock note trigger did not publish live pitch")
        if not result["pitch_consumer"]["renderer_input_proven"]:
            raise RuntimeError("stock renderer did not consume live note pitch")
        if result["queued_command"]["code"] != 0x1F:
            raise RuntimeError("stock note trigger produced the wrong command")
        if result["queued_command"]["track_mask"] != "0x00000001":
            raise RuntimeError("stock note trigger produced the wrong track mask")
        return result

    def _write_note_event(self, event_type: int, note: int) -> None:
        for offset in range(0, 0x28, 4):
            self.bus.write(EVENT_INPUT + offset, 4, 0)
        self.bus.write(EVENT_INPUT + EVENT_TRACK, 4, 0)
        self.bus.write(EVENT_INPUT + EVENT_NOTE, 4, note)
        self.bus.write(EVENT_INPUT + EVENT_TYPE, 4, event_type)
        self.bus.write(EVENT_INPUT + NOTE_EVENT_FLAGS, 4, VOICE_RESET_FLAG if event_type == NOTE_ON else 0)
        self.bus.write(EVENT_INPUT + EVENT_SOURCE_MASK, 4, QWERTY_SOURCE_MASK)

    def release_note(self, note: int) -> dict[str, Any]:
        if not 0 <= note <= 127:
            raise ValueError("note must be 0..127")
        with self.lock:
            held_before = self.bus.read(HELD_SOURCE_MASK_BASE, 4)
            self._write_note_event(NOTE_OFF, note)
            constructor_steps = stock_call(self.cpu, NOTE_EVENT_CONSTRUCTOR, [EVENT_INPUT])
            accepted = self.bus.read(TRIGGER_RECORD, 4) == NOTE_OFF
            if not accepted:
                raise RuntimeError("stock note-off constructor rejected the held source")
            held_after_constructor = self.bus.read(HELD_SOURCE_MASK_BASE, 4)
            callback = run_complete_callback(self.cpu, self._callback_sp)
            result = {
                "note": note,
                "constructor_instructions": constructor_steps,
                "accepted": accepted,
                "held_source_mask_before": f"0x{held_before:08X}",
                "held_source_mask_after": f"0x{held_after_constructor:08X}",
                "callback_cases": callback["cases"],
                "one_shot_cleared": self.bus.read(EVENT_VALUE, 4) == 0,
                "queue_count": self.bus.read(QUEUE + 4, 4),
            }
        if result["held_source_mask_after"] != "0x00000000":
            raise RuntimeError("stock note-off constructor did not release source ownership")
        return result

    def close(self) -> None:
        self._temporary.close()


class ControllerState:
    def __init__(self, bridge: EmulatorBridge, initial_value: int = DEFAULT_CONTROL):
        self.bridge = bridge
        self.values = [clamp_control(initial_value)] * LANES
        self.selected_lane = 0
        self.lfo2 = {
            "waveform": [WAVEFORMS["triangle"]] * LANES,
            "mode": [MODES["loop"]] * LANES,
            "trigger": [0] * LANES,
            "enable": [0] * LANES,
            "rate": [DEFAULT_CONTROL] * LANES,
            "depth": [DEFAULT_CONTROL] * LANES,
        }
        self.held_notes: set[int] = set()
        self.audition_note = 60
        self.events: deque[dict[str, Any]] = deque(maxlen=32)
        self.lock = threading.RLock()
        self.started_at = time.time()
        self.event_sequence = 0
        self._run_stop = threading.Event()
        self._run_thread: threading.Thread | None = None
        self.callback_run: dict[str, Any] = {
            "status": "idle",
            "requested": 0,
            "completed": 0,
            "started_callback_count": 0,
            "last_callback": None,
            "error": None,
        }
        for lane, value in enumerate(self.values):
            self._publish_filter(lane, value, "initialization")
            for parameter in ("waveform", "mode", "trigger", "enable", "rate", "depth"):
                self.bridge.publish_lfo2(lane, parameter, self.lfo2[parameter][lane])
        self.selected_lane = 0
        self.bridge.capture_callback_baseline()

    def _record(self, event: dict[str, Any]) -> dict[str, Any]:
        self.event_sequence += 1
        event = {"sequence": self.event_sequence, "time": time.time(), **event}
        self.events.append(event)
        return event

    def _publish_filter(self, lane: int, value: int, source: str) -> dict[str, Any]:
        publication = self.bridge.publish(lane, value)
        self.values[lane] = publication["value"]
        self.selected_lane = lane
        return self._record({"type": "filter2", "source": source, **publication})

    def set_filter(self, lane: int, value: int, source: str = "api") -> dict[str, Any]:
        if not isinstance(lane, int) or isinstance(lane, bool) or not 0 <= lane < LANES:
            raise ValueError("lane must be an integer from 0 through 7")
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("value must be an integer from 0 through 127")
        if source not in {"mouse", "wheel", "keyboard", "api"}:
            source = "api"
        with self.lock:
            return self._publish_filter(lane, clamp_control(value), source)

    def set_lfo2(self, lane: int, parameter: str, value: Any, source: str = "api") -> dict[str, Any]:
        if not isinstance(lane, int) or isinstance(lane, bool) or not 0 <= lane < LANES:
            raise ValueError("lane must be an integer from 0 through 7")
        if parameter == "waveform" and isinstance(value, str):
            if value not in WAVEFORMS:
                raise ValueError("unsupported LFO2 waveform")
            value = WAVEFORMS[value]
        elif parameter == "mode" and isinstance(value, str):
            if value not in MODES:
                raise ValueError("unsupported LFO2 mode")
            value = MODES[value]
        elif parameter in {"trigger", "enable"} and isinstance(value, bool):
            value = int(value)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("LFO2 value must be an integer or supported name")
        if source not in {"mouse", "keyboard", "api"}:
            source = "api"
        with self.lock:
            publication = self.bridge.publish_lfo2(lane, parameter, value)
            self.selected_lane = lane
            if parameter != "reset":
                self.lfo2[parameter][lane] = value
            return self._record({"type": "lfo2", "source": source, **publication})

    def note(self, key: str, action: str, velocity: int = 100) -> dict[str, Any]:
        key = key.lower()
        if key not in NOTE_KEYS:
            raise ValueError("unsupported QWERTY note key")
        if action not in {"on", "off"}:
            raise ValueError("note action must be 'on' or 'off'")
        velocity = max(1, min(127, int(velocity)))
        note = NOTE_KEYS[key]
        with self.lock:
            if action == "on":
                self.held_notes.add(note)
                self.audition_note = note
                stock_trigger = self.bridge.trigger_note(note)
                transport = "emulated_stock_trigger"
            else:
                if note not in self.held_notes:
                    raise ValueError("note key is not held")
                self.held_notes.discard(note)
                stock_trigger = self.bridge.release_note(note)
                transport = "emulated_stock_release"
            event = {
                "type": "note",
                "source": "qwerty",
                "key": key,
                "action": action,
                "note": note,
                "velocity": velocity if action == "on" else 0,
                "firmware_transport": transport,
            }
            event["stock_trigger" if action == "on" else "stock_release"] = stock_trigger
            return self._record(event)

    def render_audio_preview(self, count: int = 32) -> tuple[bytes, dict[str, Any]]:
        with self.lock:
            if self.callback_run["status"] in {"running", "stopping"}:
                raise ValueError("audio preview is unavailable while a callback run is active")
            wav_bytes, metadata = self.bridge.render_audio_preview(
                self.audition_note, self.selected_lane, count,
            )
            self._record({"type": "audio_preview", "source": "api", **metadata})
            return wav_bytes, metadata

    def step_callbacks(self, count: int = 1) -> dict[str, Any]:
        with self.lock:
            if self.callback_run["status"] in {"running", "stopping"}:
                raise ValueError("manual callback stepping is unavailable while a run is active")
            result = self.bridge.step_callbacks(count)
            return self._record({"type": "callback_step", "source": "api", **result})

    def _callback_run_worker(self, requested: int) -> None:
        status = "completed"
        error = None
        try:
            for _ in range(requested):
                if self._run_stop.is_set():
                    status = "stopped"
                    break
                result = self.bridge.step_callbacks(1)
                with self.lock:
                    self.callback_run["completed"] += 1
                    self.callback_run["last_callback"] = result["callbacks"][0]
            if self._run_stop.is_set() and self.callback_run["completed"] < requested:
                status = "stopped"
        except Exception as caught:  # background errors must remain visible to the API
            status = "error"
            error = str(caught)
        with self.lock:
            self.callback_run["status"] = status
            self.callback_run["error"] = error
            self._record({
                "type": "callback_run_finished",
                "source": "runner",
                "status": status,
                "requested": requested,
                "completed": self.callback_run["completed"],
                "error": error,
            })

    def control_callback_run(self, action: str, max_callbacks: int = 16) -> dict[str, Any]:
        if action not in {"start", "stop"}:
            raise ValueError("run action must be 'start' or 'stop'")
        if action == "start":
            if (not isinstance(max_callbacks, int) or isinstance(max_callbacks, bool)
                    or not 1 <= max_callbacks <= 32):
                raise ValueError("max_callbacks must be an integer from 1 through 32")
            with self.lock:
                if self.callback_run["status"] in {"running", "stopping"}:
                    raise ValueError("a callback run is already active")
                self._run_stop.clear()
                self.callback_run = {
                    "status": "running",
                    "requested": max_callbacks,
                    "completed": 0,
                    "started_callback_count": self.bridge.callback_count,
                    "last_callback": None,
                    "error": None,
                }
                event = self._record({
                    "type": "callback_run_started",
                    "source": "api",
                    "requested": max_callbacks,
                })
                self._run_thread = threading.Thread(
                    target=self._callback_run_worker,
                    args=(max_callbacks,),
                    name="offline-callback-runner",
                    daemon=True,
                )
                self._run_thread.start()
                return event
        with self.lock:
            if self.callback_run["status"] not in {"running", "stopping"}:
                raise ValueError("no callback run is active")
            self._run_stop.set()
            self.callback_run["status"] = "stopping"
            return self._record({
                "type": "callback_run_stop_requested",
                "source": "api",
                "completed": self.callback_run["completed"],
            })

    def close(self) -> None:
        self._run_stop.set()
        thread = self._run_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=10)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "status": "ready",
                "filter2": {
                    "values": list(self.values),
                    "selected_lane": self.selected_lane,
                    "virtual_indices": [f"0x{VIRTUAL_INDEX_BASE + lane:04X}" for lane in range(LANES)],
                    "transport": "emulator publication shim",
                },
                "lfo2": {
                    **{name: list(values) for name, values in self.lfo2.items()},
                    "selected_lane": self.selected_lane,
                    "waveforms": WAVEFORMS,
                    "modes": MODES,
                    "virtual_indices": {
                        name: [f"0x{base + lane:04X}" for lane in range(LANES)]
                        for name, base in LFO2_PARAMETERS.items()
                    },
                    "transport": "emulator extended-wave publication shim",
                    "runtime": self.bridge.lfo2_runtime_snapshot(),
                },
                "notes": {
                    "keys": NOTE_KEYS,
                    "held": sorted(self.held_notes),
                    "audition_note": self.audition_note,
                    "transport": "key-down: stock constructor type 1; key-up: stock constructor type 2",
                },
                "emulator": {
                    "stock_sha256": self.bridge.stock_sha256,
                    "candidate_sha256": self.bridge.candidate_sha256,
                    "runtime_armed_only": True,
                    "flashable_image_created": False,
                },
                "callback_run": dict(self.callback_run),
                "events": list(self.events),
                "uptime_seconds": round(time.time() - self.started_at, 3),
            }


def json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def make_handler(state: ControllerState):
    class ControllerHandler(BaseHTTPRequestHandler):
        server_version = "Rytm2Lab/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: HTTPStatus, payload: Any) -> None:
            self._send(status, json_bytes(payload), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/state":
                self._json(HTTPStatus.OK, state.snapshot())
                return
            relative = "index.html" if path == "/" else path.lstrip("/")
            candidate = (STATIC / relative).resolve()
            if STATIC.resolve() not in candidate.parents or not candidate.is_file():
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            body = candidate.read_bytes()
            self._send(HTTPStatus.OK, body, MIME_TYPES.get(candidate.suffix, "application/octet-stream"))

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4096:
                    raise ValueError("invalid request length")
                payload = json.loads(self.rfile.read(length))
                path = urlparse(self.path).path
                if path == "/api/filter2":
                    event = state.set_filter(payload.get("lane"), payload.get("value"), payload.get("source", "api"))
                elif path == "/api/lfo2":
                    event = state.set_lfo2(
                        payload.get("lane"), payload.get("parameter", ""),
                        payload.get("value"), payload.get("source", "api"),
                    )
                elif path == "/api/note":
                    event = state.note(payload.get("key", ""), payload.get("action", ""), payload.get("velocity", 100))
                elif path == "/api/step":
                    event = state.step_callbacks(payload.get("count", 1))
                elif path == "/api/run":
                    event = state.control_callback_run(
                        payload.get("action", ""), payload.get("max_callbacks", 16),
                    )
                elif path == "/api/audio-preview":
                    wav_bytes, metadata = state.render_audio_preview(payload.get("count", 32))
                    public_metadata = {
                        key: metadata[key] for key in (
                            "note", "frequency_hz", "lane", "callbacks", "callback_count",
                            "rendered_frames", "playback_frames", "sample_rate_hz",
                            "mixer_guard_bits", "nonzero_samples", "minimum", "maximum",
                            "raw_pcm_sha256", "wav_sha256",
                        )
                    }
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(wav_bytes)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header(
                        "X-Rytm-Audio-Metadata",
                        json.dumps(public_metadata, separators=(",", ":")),
                    )
                    self.end_headers()
                    self.wfile.write(wav_bytes)
                    return
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                self._json(HTTPStatus.OK, {"ok": True, "event": event, "state": state.snapshot()})
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
            except Exception as error:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(error)})

    return ControllerHandler


def default_paths() -> tuple[Path, Path]:
    return (
        RESEARCH / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin",
        ROOT / "recovered_library" / "minicoldfire_audio.py",
    )


def main() -> None:
    stock_default, emulator_default = default_paths()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--stock-main", type=Path, default=stock_default)
    parser.add_argument("--emulator", type=Path, default=emulator_default)
    args = parser.parse_args()
    bridge = EmulatorBridge(args.stock_main.resolve(), args.emulator.resolve())
    state = ControllerState(bridge)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    print(f"Rytm II lab controller: http://{args.host}:{server.server_port}")
    print("Filter2, LFO2 and QWERTY note-on/note-off are emulator-backed.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        state.close()
        bridge.close()


if __name__ == "__main__":
    main()
