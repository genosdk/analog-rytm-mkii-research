#!/usr/bin/env python3
"""Verify private project inputs without copying proprietary bytes into Git."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = Path(__file__).resolve().with_name("ARTIFACT_MANIFEST.json")
LOADER = ROOT / "qemu" / "firmware_loader.py"


def load_manifest(path: Path = MANIFEST) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_firmware_loader():
    spec = importlib.util.spec_from_file_location("ar172_firmware_loader", LOADER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load firmware verifier from {LOADER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_firmware(path: Path, artifact: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "artifact_id": artifact["id"],
        "path": str(path),
        "status": "MISSING",
        "checks": {},
    }
    if not path.is_file():
        result["recovery"] = artifact["recovery"]
        return result

    expected_syx = artifact["sysex"]["sha256"]
    actual_syx = sha256_file(path)
    result["checks"]["sysex_sha256"] = {
        "expected": expected_syx,
        "actual": actual_syx,
        "match": actual_syx == expected_syx,
    }
    if actual_syx != expected_syx:
        result["status"] = "HASH_MISMATCH"
        return result

    loader = _load_firmware_loader()
    with tempfile.TemporaryDirectory(prefix="ar172-verify-") as directory:
        metadata = loader.extract_main(path, Path(directory) / "main.bin")

    expected_main = artifact["decompressed_main"]
    size_match = metadata["size"] == expected_main["size_bytes"]
    hash_match = metadata["sha256"] == expected_main["sha256"]
    result["checks"]["decompressed_main"] = {
        "expected_size_bytes": expected_main["size_bytes"],
        "actual_size_bytes": metadata["size"],
        "size_match": size_match,
        "expected_sha256": expected_main["sha256"],
        "actual_sha256": metadata["sha256"],
        "hash_match": hash_match,
        "load_address": f"0x{metadata['load_address']:08X}",
    }
    result["status"] = "VERIFIED" if size_match and hash_match else "MAIN_MISMATCH"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--firmware", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    artifact = manifest["artifacts"][0]
    firmware = args.firmware or ROOT / artifact["repo_local_path"]
    try:
        result = verify_firmware(firmware, artifact)
    except (OSError, ValueError, RuntimeError) as error:
        result = {
            "artifact_id": artifact["id"],
            "path": str(firmware),
            "status": "INVALID",
            "error": str(error),
        }

    if args.as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"{result['status']}: {result['path']}")
        if "recovery" in result:
            print(result["recovery"])
        if "error" in result:
            print(result["error"])
    return 0 if result["status"] == "VERIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
