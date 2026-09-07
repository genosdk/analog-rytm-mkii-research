#!/usr/bin/env python3
"""Host reference models for AR MKII LFO2 and sample-side Filter 2.

These models define behavior and generate deterministic test vectors. They are
not target firmware and they do not modify or emit SysEx images.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import struct
from dataclasses import dataclass, field


WAVEFORMS = ("triangle", "sine", "square", "saw", "ramp", "exponential", "random")
LFO_MODES = ("free", "trig", "hold", "one", "half")
FILTER_MODES = ("lowpass", "highpass", "bandpass", "notch")


@dataclass
class LFO2:
    sample_rate: float
    frequency: float
    waveform: str = "sine"
    mode: str = "free"
    start_phase: float = 0.0
    phase: float = 0.0
    stopped: bool = False
    held_value: float = 0.0
    rng: random.Random = field(default_factory=lambda: random.Random(0x172))
    random_value: float = 0.0

    def __post_init__(self) -> None:
        if self.waveform not in WAVEFORMS:
            raise ValueError(self.waveform)
        if self.mode not in LFO_MODES:
            raise ValueError(self.mode)
        self.phase = self.start_phase % 1.0

    def trigger(self) -> None:
        if self.mode in ("trig", "one", "half"):
            self.phase = self.start_phase % 1.0
            self.stopped = False
        elif self.mode == "hold":
            self.held_value = self._shape(self.phase)

    def _shape(self, phase: float) -> float:
        if self.waveform == "triangle":
            return 1.0 - 4.0 * abs(phase - 0.5)
        if self.waveform == "sine":
            return math.sin(phase * math.tau)
        if self.waveform == "square":
            return 1.0 if phase < 0.5 else -1.0
        if self.waveform == "saw":
            return 2.0 * phase - 1.0
        if self.waveform == "ramp":
            return 1.0 - 2.0 * phase
        if self.waveform == "exponential":
            return 2.0 * phase * phase - 1.0
        return self.random_value

    def tick(self) -> float:
        if self.mode == "hold":
            output = self.held_value
        else:
            output = self._shape(self.phase)
        if self.stopped:
            return output

        previous = self.phase
        self.phase += self.frequency / self.sample_rate
        boundary = 0.5 if self.mode == "half" else 1.0
        if self.mode in ("one", "half") and self.phase >= boundary:
            self.phase = math.nextafter(boundary, 0.0)
            self.stopped = True
        else:
            self.phase %= 1.0
        if self.waveform == "random" and self.phase < previous:
            self.random_value = self.rng.uniform(-1.0, 1.0)
        return output


@dataclass
class SVFSection:
    sample_rate: float
    cutoff: float
    resonance: float
    ic1eq: float = 0.0
    ic2eq: float = 0.0

    def coefficients(self) -> tuple[float, float, float, float]:
        cutoff = min(max(self.cutoff, 5.0), self.sample_rate * 0.45)
        resonance = min(max(self.resonance, 0.0), 1.0)
        g = math.tan(math.pi * cutoff / self.sample_rate)
        k = 2.0 - 1.98 * resonance
        a1 = 1.0 / (1.0 + g * (g + k))
        a2 = g * a1
        a3 = g * a2
        return k, a1, a2, a3

    def process(self, value: float, mode: str) -> float:
        k, a1, a2, a3 = self.coefficients()
        v3 = value - self.ic2eq
        v1 = a1 * self.ic1eq + a2 * v3
        v2 = self.ic2eq + a2 * self.ic1eq + a3 * v3
        self.ic1eq = 2.0 * v1 - self.ic1eq
        self.ic2eq = 2.0 * v2 - self.ic2eq
        low = v2
        band = v1
        high = value - k * band - low
        return {
            "lowpass": low,
            "highpass": high,
            "bandpass": band,
            "notch": high + low,
        }[mode]


@dataclass
class Filter2:
    sample_rate: float = 48_000.0
    cutoff: float = 8_000.0
    resonance: float = 0.0
    mode: str = "lowpass"
    poles: int = 2
    drive: float = 0.0

    def __post_init__(self) -> None:
        if self.mode not in FILTER_MODES:
            raise ValueError(self.mode)
        if self.poles not in (2, 4):
            raise ValueError("poles must be 2 or 4")
        self.sections = [
            SVFSection(self.sample_rate, self.cutoff, self.resonance)
            for _ in range(self.poles // 2)
        ]

    def process(self, value: float) -> float:
        drive = min(max(self.drive, 0.0), 1.0)
        if drive > 0.0:
            gain = 1.0 + 7.0 * drive
            value = math.tanh(value * gain) / math.tanh(gain)
        for section in self.sections:
            section.cutoff = self.cutoff
            section.resonance = self.resonance
            value = section.process(value, self.mode)
        return value


def digest(values: list[float]) -> str:
    encoded = b"".join(struct.pack(">f", value) for value in values)
    return hashlib.sha256(encoded).hexdigest()


def run_tests() -> dict:
    results: dict[str, object] = {"result": "PASS", "tests": {}}

    lfo = LFO2(sample_rate=1_000.0, frequency=10.0, waveform="sine")
    cycle_a = [lfo.tick() for _ in range(100)]
    cycle_b = [lfo.tick() for _ in range(100)]
    periodic_error = max(abs(a - b) for a, b in zip(cycle_a, cycle_b))
    assert periodic_error < 1e-12
    results["tests"]["lfo_periodicity"] = {
        "maximum_error": periodic_error,
        "vector_sha256": digest(cycle_a),
    }

    one = LFO2(sample_rate=1_000.0, frequency=10.0, waveform="ramp", mode="one")
    one.trigger()
    one_values = [one.tick() for _ in range(150)]
    assert one.stopped and abs(one_values[-1] + 1.0) < 1e-12
    results["tests"]["lfo_one_shot"] = {"stopped": one.stopped, "final": one_values[-1]}

    max_abs = 0.0
    cases = 0
    for poles in (2, 4):
        for mode in FILTER_MODES:
            for cutoff in (20.0, 100.0, 1_000.0, 8_000.0, 18_000.0, 21_000.0):
                for resonance in (0.0, 0.5, 0.9, 1.0):
                    filt = Filter2(cutoff=cutoff, resonance=resonance, mode=mode, poles=poles)
                    output = [filt.process(1.0 if i == 0 else 0.0) for i in range(8192)]
                    assert all(math.isfinite(value) for value in output)
                    max_abs = max(max_abs, max(abs(value) for value in output))
                    cases += 1
    results["tests"]["filter_stability"] = {
        "cases": cases,
        "samples_per_case": 8192,
        "maximum_absolute_output": max_abs,
    }

    lp = Filter2(cutoff=1_000.0, mode="lowpass", poles=2)
    hp = Filter2(cutoff=1_000.0, mode="highpass", poles=2)
    lp_dc = [lp.process(0.25) for _ in range(8192)]
    hp_dc = [hp.process(0.25) for _ in range(8192)]
    assert abs(lp_dc[-1] - 0.25) < 1e-6
    assert abs(hp_dc[-1]) < 1e-6
    results["tests"]["filter_dc"] = {
        "lowpass_final": lp_dc[-1],
        "highpass_final": hp_dc[-1],
    }
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run_tests()
    print(json.dumps(result, indent=2) if args.json else result["result"])


if __name__ == "__main__":
    main()
