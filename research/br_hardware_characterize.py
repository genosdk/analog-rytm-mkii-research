#!/usr/bin/env python3
"""Analog Rytm MKII OS 1.72 stock Bit Reduction hardware characterization.

This tool deliberately does not patch firmware. It generates deterministic WAV
stimuli, drives the stock BR parameter (MIDI CC 26) through a chosen track MIDI
channel, and analyzes captured output to estimate the hardware-side quantizer.

Primary workflow:
  1. stimuli OUTDIR
  2. load br_ramp_full.wav into one AR MKII track on stock OS 1.72
  3. record that track (prefer Overbridge/digital) while running sweep-midi
  4. analyze-session capture.wav session.json --output result.json

The script uses only the Python standard library except sweep-midi, which needs
'mido' plus an installed backend (for example python-rtmidi).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import struct
import time
import wave
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 48_000
BR_CC = 26
BR_NRPN_MSB = 1
BR_NRPN_LSB = 10
RAMP_SAMPLES = 65_536
RAMP_PEAK = 0.95
LOW_RAMP_PEAK = 0.125
DEFAULT_SEGMENT_SECONDS = 1.75
DEFAULT_SETTLE_SECONDS = 0.05
DEFAULT_NOTE_SECONDS = 0.05


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def pcm16(value: float) -> int:
    return int(round(clamp(value, -1.0, 1.0) * 32767.0))


def write_mono16(path: Path, values: list[float], rate: int = SAMPLE_RATE) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"".join(struct.pack("<h", pcm16(v)) for v in values)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(payload)
    return {
        "file": path.name,
        "sample_rate": rate,
        "channels": 1,
        "sample_width_bits": 16,
        "frames": len(values),
        "duration_seconds": len(values) / rate,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def generate_stimuli(outdir: Path) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    ramp = [
        -RAMP_PEAK + (2.0 * RAMP_PEAK * i / (RAMP_SAMPLES - 1))
        for i in range(RAMP_SAMPLES)
    ]
    low_ramp = [
        -LOW_RAMP_PEAK + (2.0 * LOW_RAMP_PEAK * i / (RAMP_SAMPLES - 1))
        for i in range(RAMP_SAMPLES)
    ]
    sine_frames = SAMPLE_RATE * 2
    sine = [0.90 * math.sin(2.0 * math.pi * 997.0 * i / SAMPLE_RATE) for i in range(sine_frames)]
    impulse_frames = SAMPLE_RATE * 2
    impulse = [0.0] * impulse_frames
    for i in range(0, impulse_frames, 2400):
        impulse[i] = 0.95
        if i + 1 < impulse_frames:
            impulse[i + 1] = -0.95

    entries = [
        write_mono16(outdir / "br_ramp_full.wav", ramp),
        write_mono16(outdir / "br_ramp_low.wav", low_ramp),
        write_mono16(outdir / "br_sine_997.wav", sine),
        write_mono16(outdir / "br_impulse_train.wav", impulse),
    ]
    manifest = {
        "format": "AR172_BR_HARDWARE_STIMULI_V1",
        "purpose": "stock Bit Reduction hardware-side transfer-function characterization",
        "recommended_primary": "br_ramp_full.wav",
        "stimuli": entries,
        "notes": [
            "Use stock OS 1.72 for characterization.",
            "Prefer a digital/Overbridge track capture; analog capture is supported but model confidence will be lower.",
            "Keep sample tuning at unity and disable modulation/overdrive/compression for the measurement track.",
        ],
    }
    (outdir / "stimuli_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def br_command_word(br: int) -> int:
    if not 0 <= br <= 127:
        raise ValueError("BR must be 0..127")
    delta = 0 if br == 0 else (br * 0x40000 + 126) // 127
    return (0xB31407FF + delta) & 0xFFFFFFFF


def build_sweep_manifest(values: list[int], segment_seconds: float) -> dict:
    rows = []
    for i, br in enumerate(values):
        command = br_command_word(br)
        rows.append({
            "index": i,
            "br": br,
            "midi_cc": BR_CC,
            "midi_value": br,
            "nrpn": [BR_NRPN_MSB, BR_NRPN_LSB],
            "cpu_command_word": f"0x{command:08X}",
            "dspi_halfwords": [
                "0x4000", "0x0080", f"0x{command >> 16:04X}", f"0x{command & 0xFFFF:04X}"
            ],
            "nominal_start_seconds": i * segment_seconds,
        })
    return {
        "format": "AR172_BR_HARDWARE_SWEEP_V1",
        "os": "1.72",
        "parameter": "Sample Bit Reduction",
        "midi_cc": BR_CC,
        "nrpn": [BR_NRPN_MSB, BR_NRPN_LSB],
        "segment_seconds": segment_seconds,
        "values": rows,
    }


def parse_values(text: str) -> list[int]:
    if text.strip().lower() in ("all", "0-127"):
        return list(range(128))
    out: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = [int(x) for x in part.split("-", 1)]
            step = 1 if b >= a else -1
            out.extend(range(a, b + step, step))
        else:
            out.append(int(part))
    if not out or any(v < 0 or v > 127 for v in out):
        raise ValueError("values must be in 0..127")
    return out


def sweep_midi(args: argparse.Namespace) -> dict:
    try:
        import mido  # type: ignore
    except ImportError as exc:
        raise SystemExit("sweep-midi requires 'mido' and a MIDI backend such as python-rtmidi") from exc

    channel = args.channel - 1
    values = parse_values(args.values)
    manifest = build_sweep_manifest(values, args.segment_seconds)
    manifest.update({
        "midi_port": args.port,
        "midi_channel_1_based": args.channel,
        "trigger_note": args.note,
        "velocity": args.velocity,
        "settle_seconds": args.settle_seconds,
        "note_seconds": args.note_seconds,
        "started_unix": time.time(),
    })
    events = []
    t0 = time.monotonic()
    with mido.open_output(args.port) as port:
        for index, br in enumerate(values):
            target = t0 + index * args.segment_seconds
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            t_cc = time.monotonic() - t0
            port.send(mido.Message("control_change", channel=channel, control=BR_CC, value=br))
            time.sleep(args.settle_seconds)
            t_on = time.monotonic() - t0
            port.send(mido.Message("note_on", channel=channel, note=args.note, velocity=args.velocity))
            time.sleep(args.note_seconds)
            port.send(mido.Message("note_off", channel=channel, note=args.note, velocity=0))
            events.append({
                "index": index,
                "br": br,
                "cc_time_seconds": t_cc,
                "note_on_time_seconds": t_on,
                "cpu_command_word": f"0x{br_command_word(br):08X}",
            })
    manifest["events"] = events
    manifest["finished_unix"] = time.time()
    if args.output:
        Path(args.output).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


@dataclass
class WavData:
    rate: int
    channels: int
    width: int
    samples: list[float]
    raw_integers: list[int]


def read_wav(path: Path, channel: int = 0) -> WavData:
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        frames = w.getnframes()
        data = w.readframes(frames)
    if not 0 <= channel < channels:
        raise ValueError(f"channel {channel} outside 0..{channels-1}")
    ints: list[int] = []
    floats: list[float] = []
    frame_width = width * channels
    scale = float(1 << (8 * width - 1))
    for off in range(0, len(data), frame_width):
        b = data[off + channel * width: off + (channel + 1) * width]
        if width == 1:
            iv = b[0] - 128
        elif width == 2:
            iv = int.from_bytes(b, "little", signed=True)
        elif width == 3:
            iv = int.from_bytes(b + (b"\xFF" if b[2] & 0x80 else b"\x00"), "little", signed=True)
        elif width == 4:
            iv = int.from_bytes(b, "little", signed=True)
        else:
            raise ValueError(f"unsupported PCM sample width {width}")
        ints.append(iv)
        floats.append(iv / scale)
    return WavData(rate, channels, width, floats, ints)


def find_onset(samples: list[float], threshold: float = 0.08, run: int = 64) -> int:
    count = 0
    for i, x in enumerate(samples):
        if abs(x) >= threshold:
            count += 1
            if count >= run:
                return i - run + 1
        else:
            count = 0
    raise ValueError("could not detect stimulus onset")


def resample_linear(values: list[float], count: int) -> list[float]:
    if len(values) == count:
        return values[:]
    if len(values) < 2 or count < 2:
        raise ValueError("not enough samples to resample")
    scale = (len(values) - 1) / (count - 1)
    out = []
    for i in range(count):
        pos = i * scale
        j = int(pos)
        frac = pos - j
        if j >= len(values) - 1:
            out.append(values[-1])
        else:
            out.append(values[j] * (1.0 - frac) + values[j + 1] * frac)
    return out


def ideal_ramp(count: int = RAMP_SAMPLES, peak: float = RAMP_PEAK) -> list[float]:
    return [-peak + 2.0 * peak * i / (count - 1) for i in range(count)]


def quantize_model(x: float, bits: int, mode: str) -> float:
    levels = 1 << bits
    step = 2.0 / levels
    z = x / step
    if mode == "round":
        q = math.floor(z + 0.5) if z >= 0 else math.ceil(z - 0.5)
    elif mode == "truncate_zero":
        q = math.trunc(z)
    elif mode == "floor":
        q = math.floor(z)
    else:
        raise ValueError(mode)
    return clamp(q * step, -1.0, 1.0 - step)


def affine_fit(x: list[float], y: list[float]) -> tuple[float, float, float]:
    if len(x) != len(y) or not x:
        raise ValueError("fit vectors differ")
    mx = statistics.fmean(x)
    my = statistics.fmean(y)
    sxx = sum((v - mx) ** 2 for v in x)
    if sxx == 0:
        return 0.0, my, float("inf")
    a = sum((u - mx) * (v - my) for u, v in zip(x, y)) / sxx
    b = my - a * mx
    mse = sum((a * u + b - v) ** 2 for u, v in zip(x, y)) / len(x)
    return a, b, mse


def gcd_nonzero_differences(values: list[int], stride: int = 1) -> int:
    g = 0
    prev = values[0] if values else 0
    for i in range(stride, len(values), stride):
        cur = values[i]
        d = abs(cur - prev)
        prev = cur
        if d:
            g = math.gcd(g, d)
            if g == 1:
                break
    return g


def analyze_segment(segment: list[float], raw_segment: list[int], capture_rate: int, width: int) -> dict:
    n = len(segment)
    trim = max(1, int(n * 0.08))
    core = segment[trim:n - trim]
    raw_core = raw_segment[trim:n - trim]
    source = ideal_ramp(len(segment))[trim:n - trim]
    max_fit = 8192
    step = max(1, len(core) // max_fit)
    y = core[::step]
    src = source[::step]

    candidates = []
    for bits in range(1, 17):
        for mode in ("round", "truncate_zero", "floor"):
            q = [quantize_model(x, bits, mode) for x in src]
            gain, dc, mse = affine_fit(q, y)
            candidates.append({"bits": bits, "mode": mode, "gain": gain, "dc": dc, "mse": mse})
    candidates.sort(key=lambda r: r["mse"])
    best = candidates[0]
    second = candidates[1]
    gcd_step = gcd_nonzero_differences(raw_core, stride=max(1, len(raw_core) // 32768)) if raw_core else 0
    unique_exact = len(set(raw_core)) if len(raw_core) <= 200_000 else None
    return {
        "best_uniform_model": best,
        "runner_up": second,
        "model_margin_ratio": (second["mse"] / best["mse"]) if best["mse"] > 0 else None,
        "integer_difference_gcd": gcd_step,
        "unique_capture_codes": unique_exact,
        "capture_sample_width_bits": width * 8,
        "analysis_samples": len(core),
        "fit_samples": len(y),
    }


def extract_segment(capture: WavData, expected_start: int, stimulus_frames: int, search_radius: int) -> tuple[int, list[float], list[int]]:
    lo = max(0, expected_start - search_radius)
    hi = min(len(capture.samples), expected_start + search_radius + stimulus_frames)
    window = capture.samples[lo:hi]
    try:
        local = find_onset(window)
        start = lo + local
    except ValueError:
        start = max(0, expected_start)
    end = start + stimulus_frames
    if end > len(capture.samples):
        raise ValueError("capture ends before expected stimulus segment")
    return start, capture.samples[start:end], capture.raw_integers[start:end]


def analyze_session(capture_path: Path, session_path: Path, output: Path | None, channel: int, start_offset: float | None) -> dict:
    capture = read_wav(capture_path, channel)
    session = json.loads(session_path.read_text(encoding="utf-8"))
    events = session.get("events") or session.get("values")
    if not events:
        raise ValueError("session has no events/values")
    stimulus_seconds = RAMP_SAMPLES / SAMPLE_RATE
    stimulus_frames = round(stimulus_seconds * capture.rate)
    segment_seconds = float(session.get("segment_seconds", DEFAULT_SEGMENT_SECONDS))
    segment_frames = round(segment_seconds * capture.rate)
    search_radius = round(0.12 * capture.rate)

    first = find_onset(capture.samples) if start_offset is None else round(start_offset * capture.rate)
    rows = []
    for index, event in enumerate(events):
        br = int(event["br"])
        expected = first + index * segment_frames
        start, seg, raw = extract_segment(capture, expected, stimulus_frames, search_radius)
        if capture.rate != SAMPLE_RATE:
            seg = resample_linear(seg, RAMP_SAMPLES)
        result = analyze_segment(seg, raw, capture.rate, capture.width)
        result.update({
            "index": index,
            "br": br,
            "cpu_command_word": event.get("cpu_command_word", f"0x{br_command_word(br):08X}"),
            "capture_start_frame": start,
            "capture_start_seconds": start / capture.rate,
        })
        rows.append(result)

    plateaus = []
    if rows:
        s = 0
        for i in range(1, len(rows) + 1):
            changed = i == len(rows) or (
                rows[i]["best_uniform_model"]["bits"] != rows[s]["best_uniform_model"]["bits"]
                or rows[i]["best_uniform_model"]["mode"] != rows[s]["best_uniform_model"]["mode"]
            )
            if changed:
                plateaus.append({
                    "start_br": rows[s]["br"],
                    "end_br": rows[i - 1]["br"],
                    "bits": rows[s]["best_uniform_model"]["bits"],
                    "mode": rows[s]["best_uniform_model"]["mode"],
                })
                s = i

    report = {
        "format": "AR172_BR_HARDWARE_CHARACTERIZATION_V1",
        "capture": {
            "file": str(capture_path),
            "sha256": hashlib.sha256(capture_path.read_bytes()).hexdigest(),
            "sample_rate": capture.rate,
            "channels": capture.channels,
            "analyzed_channel": channel,
            "sample_width_bits": capture.width * 8,
        },
        "session": str(session_path),
        "stock_control_path": {
            "midi_cc": BR_CC,
            "nrpn": [BR_NRPN_MSB, BR_NRPN_LSB],
            "cpu_command_equation": "0xB31407FF + ceil(BR*0x40000/127)",
            "hardware_quantizer_equation": "UNPROVEN_UNTIL_CAPTURE_ANALYSIS",
        },
        "results": rows,
        "inferred_plateaus": plateaus,
        "interpretation_rule": (
            "Digital/Overbridge captures with a strong model margin are suitable for reconstructing the hardware law. "
            "Analog captures can identify coarse trends but should not by themselves prove truncation versus rounding."
        ),
    }
    if output:
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def self_test(tmp: Path) -> dict:
    manifest = generate_stimuli(tmp / "stimuli")
    src = ideal_ramp()
    q = [quantize_model(x, 6, "truncate_zero") for x in src]
    captured = [0.8 * x + 0.01 for x in q]
    analysis = analyze_segment(captured, [pcm16(x) for x in captured], SAMPLE_RATE, 2)
    if analysis["best_uniform_model"]["bits"] != 6 or analysis["best_uniform_model"]["mode"] != "truncate_zero":
        raise AssertionError(f"synthetic quantizer recovery failed: {analysis['best_uniform_model']}")
    if br_command_word(0) != 0xB31407FF or br_command_word(127) != 0xB31807FF:
        raise AssertionError("BR command endpoint mismatch")
    return {
        "result": "PASS",
        "stimulus_count": len(manifest["stimuli"]),
        "synthetic_model": analysis["best_uniform_model"],
        "br_command_endpoints": [f"0x{br_command_word(0):08X}", f"0x{br_command_word(127):08X}"],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stimuli", help="generate deterministic BR test WAVs")
    s.add_argument("output_dir", type=Path)

    m = sub.add_parser("manifest", help="write a 0..127 sweep manifest without sending MIDI")
    m.add_argument("output", type=Path)
    m.add_argument("--values", default="all")
    m.add_argument("--segment-seconds", type=float, default=DEFAULT_SEGMENT_SECONDS)

    sw = sub.add_parser("sweep-midi", help="send stock BR CC26 + trigger notes for one continuous recording")
    sw.add_argument("--port", required=True)
    sw.add_argument("--channel", type=int, required=True, choices=range(1, 17))
    sw.add_argument("--note", type=int, required=True)
    sw.add_argument("--velocity", type=int, default=100)
    sw.add_argument("--values", default="all")
    sw.add_argument("--segment-seconds", type=float, default=DEFAULT_SEGMENT_SECONDS)
    sw.add_argument("--settle-seconds", type=float, default=DEFAULT_SETTLE_SECONDS)
    sw.add_argument("--note-seconds", type=float, default=DEFAULT_NOTE_SECONDS)
    sw.add_argument("--output", type=Path, default=Path("br_sweep_session.json"))

    a = sub.add_parser("analyze-session", help="analyze continuous WAV capture from sweep-midi")
    a.add_argument("capture", type=Path)
    a.add_argument("session", type=Path)
    a.add_argument("--channel", type=int, default=0)
    a.add_argument("--start-offset", type=float)
    a.add_argument("--output", type=Path, default=Path("br_hardware_characterization.json"))

    t = sub.add_parser("self-test")
    t.add_argument("--tmp", type=Path, default=Path("/tmp/ar172_br_hw_selftest"))

    args = ap.parse_args()
    if args.cmd == "stimuli":
        result = generate_stimuli(args.output_dir)
    elif args.cmd == "manifest":
        result = build_sweep_manifest(parse_values(args.values), args.segment_seconds)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    elif args.cmd == "sweep-midi":
        result = sweep_midi(args)
    elif args.cmd == "analyze-session":
        result = analyze_session(args.capture, args.session, args.output, args.channel, args.start_offset)
    else:
        result = self_test(args.tmp)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
