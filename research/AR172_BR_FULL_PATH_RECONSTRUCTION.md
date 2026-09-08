# Analog Rytm MKII OS 1.72 — Full BR render-path reconstruction

**Status:** static reconstruction passes; hardware output capture remains pending.

## Result

The stock Bit Reduction block is now reconstructed beyond the D3/D2 truncation core. The full 32-sample transform is:

```text
source sample
  -> D3 signed-fractional Q31 truncation
  -> << D2 binary scale restoration
  -> D4-compensated sample-level smoothing ramp
  -> output
```

With `MACSR=0x20`, define `truncQ31(a,b)` as the signed 32x32 fractional multiply with truncation and 32-bit destination wrap. For BR-dependent coefficients `(D2,D3,D4)` and incoming sample-level states `Lprev,Lnext`:

```text
q[i] = wrap32(truncQ31(sample[i], D3) << D2)
r0   = truncQ31(Lprev, D4)
r1   = truncQ31(Lnext, D4)
dr   = wrap32(r1-r0) >> 5
y[i] = truncQ31(q[i], wrap32(r0 + i*dr)), i=0..31
```

The stock implementation is two-lane/pipelined but is algebraically equivalent to that direct expression.

## Machine-code landmarks

- `0x40117EF0`: installs `MACSR=0x20`.
- `0x401186B2`: loads previous per-voice level state from `0x10(A3)` into D7.
- `0x401186CC`: loads the current signed level/control word from `0x0E(A1)` into D5.
- `0x401186E6`: squares the current level value and doubles it.
- `0x401186FA..0x40118700`: scales and persists the current level state back to `0x10(A3)`.
- `0x40118744..0x40118750`: obtains BR coefficients D4 and D3 from the exponential table at `0x40217950`.
- `0x40118754`: source block base `0x8000E1D0`.
- `0x4011875A/5E`: D4 is applied to previous/current level-ramp states.
- `0x4011876A/6C`: `(target-start) >> 5` becomes the 32-sample interpolation increment.
- `0x4011876E/72`: first source pair is pre-quantized with D3.
- `0x4011877A..0x40118780`: quantized pair is extracted and restored with D2.
- `0x40118782/88`: ACC2/ACC3 multiply the quantized pair by successive ramp values.
- `0x4011878E/92`: next source pair is pre-quantized.
- `0x40118796/9A`: ACC2/ACC3 are emitted with `MOVCLR`.
- `0x40118798/9C`: two 32-bit output samples are stored.
- `0x401187A2/A4`: prefetched ACC0/ACC1 are extracted/cleared at block exit.

Sixteen iterations emit exactly 32 samples (`0x80` bytes).

## D4 interpretation

D4 is not part of the amplitude-resolution truncation itself. It acts on the previous/current sample-level states before the 32-sample multiplicative ramp is formed. Its role is therefore BR-dependent amplitude compensation coupled to the D3/D2 quantizer.

This explains why multiple high BR settings can share the same coarse `(D2,D3)` quantizer while remaining behaviorally distinct: D4 continues to move even after D3 has collapsed to 1.

## Accumulator invariant

The normal block exit is self-cleaning:

- ACC2 and ACC3 are cleared by the `MOVCLR` output operations every emitted pair.
- ACC0 and ACC1 contain the next prefetched quantized pair after the final iteration, then are cleared by `0x401187A2/A4`.

Therefore all four EMAC accumulators are clear at normal block exit, which justifies the zero-accumulator Q31 formulation across consecutive blocks.

## Host-model validation

`research/br_full_path_reconstruct.py` implements both:

1. an instruction-order/pipelined model, and
2. an independent closed-form 32-sample model.

Validation against the known SRR research MAIN (whose arithmetic window remains stock):

- MAIN SHA-256: `41bfd182f5f933bbc70fe9033fd157fc938d3ce54a64229b45ec2e1709c8ee67`
- Q31 equivalence tests: **10,006 passed**
- randomized full-block equivalence cases: **10,000 passed**
- samples compared per block: **32**
- result: **PASS**

The Q31 helper is independently checked against the EMAC fractional-alignment formulation, including signed edge cases.

## Remaining unknowns

The arithmetic path itself is closed. Remaining provenance/semantic work is narrower:

1. Name the status flags at `0x28(A3)` and `0x29(A3)` and the gate involving `0x8000586C`.
2. Reconcile the exact control-frame/modulation bridge that ultimately supplies terminal BR at `6(A1)`.
3. Validate reconstructed outputs against physical-hardware capture once the AR MKII is available.

No firmware is modified or repacked by this work.
