# Project context — Analog Rytm MKII OS 1.72

This file is the durable entry point for resuming the project. Read it together with
`CURRENT_GATE.md` and `research/ARTIFACT_MANIFEST.json` before doing new work.

## Objective

Research and emulate the Analog Rytm MKII OS 1.72 sample/audio path, preserving exact
behavior while progressively replacing bounded ColdFire/EMAC regions with independently
verified QEMU TCG helpers. Hardware-facing work remains behind the staged safety gates in
`docs/AR172_FIRST_HARDWARE_TEST_PROTOCOL.md`.

## Source-of-truth order

1. Private ChatGPT Library artifacts, identified by exact filename, version, and hash.
2. This Git repository for permissible code, manifests, gates, and reproducible tooling.
3. The active conversation only as transient working context.

The official firmware and extracted proprietary sections must never be committed. Git
stores their identities and verification procedure, not their bytes.

## Required baseline

- Firmware: `Analog-Rytm_MKII_OS1.72.syx`
- Official source: Elektron Analog Rytm MKII support/download page
- SysEx SHA-256: `1ea60357abe8b876d8b9c52e6dcd988d833478a49d09e3cb22d42782ef822b2f`
- Decompressed MAIN size: `2,903,032` bytes
- Decompressed MAIN SHA-256: `5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772`
- MAIN load address: `0x40000400`

The machine-readable authority is `research/ARTIFACT_MANIFEST.json`. A filename match is
not sufficient: run `python3 research/verify_project_inputs.py` before research that
depends on firmware bytes.

## Binding boundaries

- Do not commit or distribute stock or modified `.syx` images, decompressed firmware,
  project data, sample names, PCM, runtime traces, or instruction bytes.
- QEMU/research changes are not hardware firmware changes.
- Do not touch Railway unless the user explicitly requests it.
- Validate locally and use `[skip ci]` for research-only Git checkpoints unless the user
  explicitly changes that policy.
- Preserve exact test results, commit IDs, hashes, recovery identifiers, and explicit
  unknowns. Never infer that an artifact is stored when it has not been verified.

## Resume protocol

```bash
git status --short --branch
git log -1 --oneline
python3 research/verify_project_inputs.py
python3 -m unittest research.test_research
```

If the firmware verifier reports `MISSING`, recover the exact 1.72 file from its private
Library record first. If that record is unavailable, resolve the historical 1.72 asset
through Elektron's official support/archive surface and accept it only after both the
SysEx and extracted MAIN hashes match the manifest. The live product download may expose
a newer release; never substitute it.
