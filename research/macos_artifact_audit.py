#!/usr/bin/env python3
"""Audit a packaged AR MKII Emulator GitHub Actions artifact without macOS."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import plistlib
import stat
import zipfile
from pathlib import Path, PurePosixPath


APP_NAME = "AR MKII Emulator.app"
ARCH_CPU_TYPES = {"arm64": 0x0100000C, "x86_64": 0x01000007}
FORBIDDEN_SUFFIXES = (".syx", ".ele3", ".bin", ".rom", ".fw")
REQUIRED_APP_FILES = (
    "Contents/Info.plist",
    "Contents/MacOS/AR MKII Emulator",
    "Contents/MacOS/qemu-system-m68k",
    "Contents/Resources/qemu/assets/photon_panel_neutral.png",
    "Contents/Resources/qemu/assets/photon_panel_active.png",
    "Contents/_CodeSignature/CodeResources",
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_names(archive: zipfile.ZipFile) -> list[str]:
    names = archive.namelist()
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ValueError(f"unsafe ZIP member: {name!r}")
    return names


def is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def macho_cpu_type(data: bytes) -> int | None:
    if len(data) < 8:
        return None
    if data[:4] == b"\xcf\xfa\xed\xfe":
        return int.from_bytes(data[4:8], "little")
    if data[:4] == b"\xfe\xed\xfa\xcf":
        return int.from_bytes(data[4:8], "big")
    return None


def forbidden_member(name: str) -> bool:
    lower = name.lower()
    return (
        lower.endswith(FORBIDDEN_SUFFIXES)
        or "firmware" in PurePosixPath(lower).name
    )


def parse_checksum(text: str, expected_name: str) -> str:
    parts = text.strip().split()
    if len(parts) != 2 or parts[1].lstrip("*") != expected_name:
        raise ValueError("inner checksum manifest has an unexpected format")
    digest = parts[0].lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError("inner checksum manifest does not contain SHA-256")
    return digest


def audit(outer_path: Path, architecture: str, expected_outer_sha256: str | None) -> dict:
    expected_cpu = ARCH_CPU_TYPES[architecture]
    outer_bytes = outer_path.read_bytes()
    outer_digest = sha256(outer_bytes)
    if expected_outer_sha256 and outer_digest != expected_outer_sha256.lower():
        raise ValueError("GitHub artifact envelope SHA-256 mismatch")

    stem = f"AR-MKII-Emulator-{architecture}"
    inner_name = f"{stem}.zip"
    checksum_name = f"{inner_name}.sha256"
    architecture_name = f"{stem}.architecture.txt"
    audit_name = f"{stem}.audit.json"
    required_outer_members = {inner_name, checksum_name, architecture_name}

    with zipfile.ZipFile(io.BytesIO(outer_bytes)) as outer:
        outer_names = safe_names(outer)
        if not (
            set(outer_names) == required_outer_members
            or set(outer_names) == required_outer_members | {audit_name}
        ):
            raise ValueError(f"unexpected artifact members: {sorted(outer_names)!r}")
        inner_bytes = outer.read(inner_name)
        manifest_digest = parse_checksum(
            outer.read(checksum_name).decode("utf-8"), inner_name
        )
        if sha256(inner_bytes) != manifest_digest:
            raise ValueError("nested release ZIP SHA-256 mismatch")
        architecture_manifest = outer.read(architecture_name).decode("utf-8")
        if architecture_manifest.count(architecture) != 2:
            raise ValueError("architecture manifest does not name both executables")

    with zipfile.ZipFile(io.BytesIO(inner_bytes)) as inner:
        inner_names = safe_names(inner)
        prefix = f"{APP_NAME}/"
        if not inner_names or any(not name.startswith(prefix) for name in inner_names):
            raise ValueError("release ZIP must contain exactly one expected app bundle")
        for relative in REQUIRED_APP_FILES:
            member = prefix + relative
            if member not in inner_names or inner.getinfo(member).file_size == 0:
                raise ValueError(f"missing or empty app member: {relative}")

        suspicious = [name for name in inner_names if forbidden_member(name)]
        nested_suspicious: list[str] = []
        macho_files: list[str] = []
        wrong_architecture: list[dict] = []
        for info in inner.infolist():
            if info.is_dir() or is_symlink(info):
                continue
            data = inner.read(info)
            cpu_type = macho_cpu_type(data)
            if cpu_type is not None:
                relative = info.filename[len(prefix):]
                macho_files.append(relative)
                if cpu_type != expected_cpu:
                    wrong_architecture.append(
                        {"path": relative, "cpu_type": f"0x{cpu_type:08x}"}
                    )
            if info.filename.lower().endswith(".zip") and zipfile.is_zipfile(io.BytesIO(data)):
                with zipfile.ZipFile(io.BytesIO(data)) as nested:
                    for name in safe_names(nested):
                        if forbidden_member(name):
                            nested_suspicious.append(f"{info.filename}!/{name}")

        if suspicious or nested_suspicious:
            raise ValueError("firmware-like payload found in packaged app")
        if wrong_architecture:
            raise ValueError(f"foreign Mach-O payloads: {wrong_architecture!r}")
        if len(macho_files) < 2:
            raise ValueError("packaged app does not contain both native executables")

        plist = plistlib.loads(inner.read(prefix + "Contents/Info.plist"))
        if plist.get("CFBundleIdentifier") != "org.photonos.armk2emulator":
            raise ValueError("unexpected bundle identifier")
        if plist.get("CFBundleExecutable") != "AR MKII Emulator":
            raise ValueError("unexpected bundle executable")

        file_count = sum(not info.is_dir() for info in inner.infolist())
        uncompressed_bytes = sum(
            info.file_size for info in inner.infolist() if not info.is_dir()
        )

    return {
        "schema_version": 1,
        "status": "PASS_MACOS_ARTIFACT_INTEGRITY",
        "architecture": architecture,
        "outer_artifact": {
            "sha256": outer_digest,
            "size_in_bytes": len(outer_bytes),
            "member_count": len(outer_names),
        },
        "release_zip": {
            "sha256": manifest_digest,
            "size_in_bytes": len(inner_bytes),
            "app_file_count": file_count,
            "app_uncompressed_bytes": uncompressed_bytes,
        },
        "bundle": {
            "identifier": plist["CFBundleIdentifier"],
            "executable": plist["CFBundleExecutable"],
            "short_version": plist.get("CFBundleShortVersionString"),
            "macho_file_count": len(macho_files),
            "all_macho_files_match_architecture": True,
            "required_members_present": list(REQUIRED_APP_FILES),
            "firmware_like_payload_count": 0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--architecture", choices=sorted(ARCH_CPU_TYPES), required=True)
    parser.add_argument("--expected-outer-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.artifact, args.architecture, args.expected_outer_sha256)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
