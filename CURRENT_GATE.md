# Current gate

Updated: 2026-09-25 UTC

## Verified checkpoint

- Branch: `main`
- Last completed and remote-aligned commit before this gate: `3357b1c`
- Commit: `research: secure verified OS 1.72 recovery metadata [skip ci]`
- Railway: untouched
- GitHub Actions: intentionally not run (`[skip ci]` policy)
- Authoritative result: `research/AR172_QEMU_HANDOFF_COMPOSITE_GATE.json`

## What the last experiment established

The verifier-only `candidate=handoff-composite` reconstruction now covers the complete
181-instruction residual leaf. It composes the established helper semantics with the
remaining single-pass scalar spine and executes with zero candidate guest accesses and
no fallback.

Same-process replay is exact at both `stable=8` and `stable=40`: all 35 registers and
every touched byte match the native exit. The boundary remains:

- Entry: `0x40108C7C`
- Inclusive native window: `0x40108C7C..0x4010926A`
- Exit: `0x4010A06A`
- Frequency: once per vector-191 service
- Native cost: 181 guest instructions per call

## Next gate

Promote the verified composite to a seventeenth opt-in guarded direct-state TCG helper.
Require explicit-arm early/late oracle equality, then measure the exact bounded
instruction reduction and held-audio release behavior before treating it as established.

## Startup checks

```bash
python3 research/verify_project_inputs.py
python3 -m unittest research.test_research
```

The official 1.72 firmware is verified in private ChatGPT Library storage as
`libfile_cc783b7c3be08191a68898dd79193d45` (version 0). Its SysEx SHA-256 and
decompressed MAIN size/SHA-256 exactly match `research/ARTIFACT_MANIFEST.json`.
If the private input is ever unavailable locally, recover that Library record and rerun
the verifier; absence is not permission to substitute 1.73.
