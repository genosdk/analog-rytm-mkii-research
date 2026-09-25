# Current gate

Updated: 2026-09-24 UTC

## Verified checkpoint

- Branch: `reconstruct-audio-contract`
- Last completed and remote-aligned commit at checkpoint creation: `0bc6bb5`
- Commit: `research: select final handoff composite [skip ci]`
- Railway: untouched
- GitHub Actions: intentionally not run (`[skip ci]` policy)
- Authoritative result: `research/AR172_QEMU_HANDOFF_POST_EMAC2X4_PROFILE_GATE.json`

## What the last experiment established

With all sixteen guarded TCG helpers enabled, 100 vector-191 services left exactly
18,100 guest instructions: 181 instructions per service across 181 unique PCs. Every
remaining PC executed exactly once per service. EMAC2x4 removed 13 distinct native PCs,
but the residual contains no repeated micro-kernel that can provide another
multiplicative reduction.

The selected remaining boundary is the full deterministic handoff composite:

- Entry: `0x40108C7C`
- Inclusive native window: `0x40108C7C..0x4010926A`
- Exit: `0x4010A06A`
- Frequency: once per vector-191 service
- Residual cost: 181 guest instructions per call

## Next gate

Build a verifier-only candidate for the complete residual composite. Reuse the sixteen
established helper models, reconstruct only the remaining single-pass scalar spine, and
require exact early/late complete-register and touched-memory equality before considering
a consolidation helper.

This is the final software-only consolidation boundary. If exact replay cannot be
established from the existing captured contract without introducing unverified runtime
values or proprietary bytes, stop and record the evidence gap rather than guessing.

## Startup checks

```bash
python3 research/verify_project_inputs.py
python3 -m unittest research.test_research
```

The official 1.72 firmware is a required private input. Its absence is a recoverable
dependency failure, not permission to substitute 1.73.
