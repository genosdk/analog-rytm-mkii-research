# AR MKII OS 1.72 — stock Bit Reduction hardware characterization

## Purpose

Measure the **hardware-side** Bit Reduction transfer function on an unmodified Analog Rytm MKII running stock OS 1.72.

Static/emulated work now proves the CPU-side control path:

`BR -> renderer 0x4010CBA8 -> packed per-voice command -> DSPI1/eDMA -> external audio hardware`

The ColdFire MAIN image does **not** show a proven PCM mask/shift/truncation operation for BR. The remaining question is how the external audio/FPGA path interprets the normalized BR command and changes sample resolution.

This procedure is therefore stock-firmware measurement, not a custom-firmware flash test.

## Proven control facts used by the harness

- Front-panel BR range: `0..127`.
- OS 1.72 MIDI Appendix C assigns **Sample Bit Reduction to CC 26** and NRPN `1:10`.
- Track-0 BR control-frame field: `0x8000F7BE`.
- Renderer: `0x4010CBA8`.
- CPU command ramp:

  `command(BR) = 0xB31407FF + ceil(BR * 0x40000 / 127)`

- Endpoints:
  - BR 0 -> `0xB31407FF`
  - BR 127 -> `0xB31807FF`

These command words are serialized into DSPI1 transport rather than applied directly to CPU-rendered PCM.

## Tool

`research/br_hardware_characterize.py`

The hardware sweep itself requires `mido` plus a MIDI backend such as `python-rtmidi`. Stimulus generation, manifest generation, WAV decoding and analysis use the Python standard library.

Run the built-in validation first:

```bash
python research/br_hardware_characterize.py self-test
```

Expected result: `PASS`, recovering a synthetic 6-bit truncate-to-zero quantizer exactly.

## Generate diagnostic samples

```bash
python research/br_hardware_characterize.py stimuli build/br_hw/stimuli
```

Generated samples:

- `br_ramp_full.wav` — primary measurement sample; monotonic bipolar 16-bit ramp, 48 kHz.
- `br_ramp_low.wav` — low-amplitude ramp for fine-resolution checks.
- `br_sine_997.wav` — 997 Hz sine for spectral/SNR verification.
- `br_impulse_train.wav` — transient/check sample.

The ramp starts immediately near negative full scale so each playback onset can be detected automatically in a continuous recording.

## Rytm setup

Use **stock OS 1.72**.

On one disposable track:

1. Load `br_ramp_full.wav`.
2. Set sample tune/fine tune to unity/zero.
3. START at the beginning; END at the end; LOOP off.
4. Disable sample-parameter modulation, LFO modulation and p-locks.
5. Set sample level to a fixed value and do not touch it during the sweep.
6. Remove avoidable downstream coloration: no overdrive, no compressor contribution, no sends; keep the analog filter as open/neutral as practical.
7. Trigger only the sample layer for the measurement track if the current sound setup permits it.

Prefer an **Overbridge/digital individual-track capture**. This is the best route for proving truncation vs rounding because analog output reconstruction, ADC noise and gain staging can obscure exact code levels. Analog capture is still useful for coarse effective-bit-depth trends.

## MIDI sweep

The harness needs the MIDI channel assigned to the measurement track and a note that triggers that track.

Example:

```bash
python research/br_hardware_characterize.py sweep-midi \
  --port "Elektron Analog Rytm MKII" \
  --channel 1 \
  --note 36 \
  --values all \
  --output build/br_hw/br_sweep_session.json
```

The tool sends CC 26 values `0..127`, waits briefly for the parameter update, triggers the same sample once, and spaces events on a fixed timeline suitable for one continuous recording.

For a quick first pass before recording all 128 values:

```bash
--values 0,1,8,16,32,48,64,80,96,112,120,127
```

Do not assume the example MIDI channel or note matches the current project; use the track's actual configured channel/note.

## Capture requirements

Record continuously before starting `sweep-midi` and stop after the final sample finishes.

Preferred format:

- WAV PCM
- 48 kHz if available
- 24- or 32-bit capture preferred, 16-bit acceptable
- no normalization
- no limiter/compressor/plugin processing
- fixed gain for the entire session

A few seconds of silence before the first trigger is fine; onset detection is automatic.

## Analyze the session

```bash
python research/br_hardware_characterize.py analyze-session \
  build/br_hw/capture.wav \
  build/br_hw/br_sweep_session.json \
  --output build/br_hw/AR172_BR_HARDWARE_CHARACTERIZATION.json
```

For each BR value the analyzer reports:

- reconstructed CPU command word,
- detected capture position,
- best-fitting uniform bit depth `1..16`,
- best operation among `round`, `truncate_zero`, and `floor`,
- affine gain/DC fit,
- fit MSE and runner-up margin,
- exact integer-code difference GCD when meaningful,
- unique capture-code count.

It also condenses adjacent BR settings into inferred bit-depth/mode plateaus.

## Acceptance criteria

A hardware quantization law may be called **proven** only when:

1. the same BR-to-resolution mapping repeats over at least two captures,
2. digital/Overbridge data strongly separates the winning rounding/truncation model from alternatives,
3. the low-amplitude ramp agrees with the full-range ramp,
4. the 997 Hz sine produces distortion/SNR behavior consistent with the inferred quantizer,
5. endpoint behavior at BR 0 and 127 is explicitly checked,
6. the measurement is performed on stock OS 1.72 before introducing any BR/SRR patch.

Analog-only data may establish coarse behavior but should be labeled estimated rather than bit-exact.

## What this unlocks

Once the measured mapping is known, compare:

`CPU normalized BR command -> measured quantizer step/effective bits`

That determines whether Photon OS should:

- preserve the stock hardware BR command and add SRR separately,
- remap the stock BR command for a more musical curve,
- or implement an independent software quantizer upstream while leaving hardware BR at its neutral value.

No custom BR firmware should be designed around the superseded `0x4011870E` interpretation.
