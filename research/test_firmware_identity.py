#!/usr/bin/env python3

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


class FirmwareIdentityTests(unittest.TestCase):
    @staticmethod
    def import_launcher():
        sys.path.insert(0, str(ROOT / "qemu"))
        try:
            import run_desktop_emulator
        finally:
            sys.path.pop(0)
        return run_desktop_emulator

    def test_verified_stock_and_fallback_labels(self):
        launcher = self.import_launcher()
        common = {
            "runtime": Path("runtime"),
            "main_image": Path("main.bin"),
            "filter2_controls": Path("controls.bin"),
        }
        verified = launcher.FirmwarePreparation(
            **common,
            metadata={},
            main_sha256=launcher.EXPECTED_MAIN_SHA256,
            filter2_enabled=True,
        )
        stock = launcher.FirmwarePreparation(
            **common,
            metadata={"version": "1.72"},
            main_sha256=launcher.EXPECTED_MAIN_SHA256,
            filter2_enabled=False,
        )
        fallback = launcher.FirmwarePreparation(
            **common,
            metadata={"version": "1.73"},
            main_sha256="0" * 64,
            filter2_enabled=False,
        )
        self.assertEqual(
            launcher.firmware_display_identity(verified),
            "OS 1.72  /  FILTER 2 + LFO2 VERIFIED",
        )
        self.assertEqual(
            launcher.firmware_display_identity(stock),
            "OS 1.72  /  STOCK MODE / EXTENSION OFF",
        )
        self.assertEqual(
            launcher.firmware_display_identity(fallback),
            "OS 1.73  /  UNCHANGED STOCK FALLBACK",
        )

    def test_native_tk_self_test_covers_identity_and_title(self):
        source = (ROOT / "qemu" / "desktop_panel.py").read_text(encoding="utf-8")
        self.assertIn("Tk did not preserve firmware identity", source)
        self.assertIn("Tk title omitted firmware compatibility mode", source)

    def test_finder_audio_choice_and_explicit_overrides(self):
        launcher = self.import_launcher()
        prompts = []

        def confirm():
            prompts.append("prompted")
            return True

        self.assertTrue(
            launcher.resolve_audio_mode(
                None,
                selected_interactively=True,
                frozen=True,
                confirm=confirm,
            )
        )
        self.assertEqual(prompts, ["prompted"])
        self.assertFalse(
            launcher.resolve_audio_mode(
                None,
                selected_interactively=False,
                frozen=True,
                confirm=confirm,
            )
        )
        self.assertFalse(
            launcher.resolve_audio_mode(
                None,
                selected_interactively=True,
                frozen=False,
                confirm=confirm,
            )
        )
        self.assertTrue(
            launcher.resolve_audio_mode(
                True,
                selected_interactively=True,
                frozen=True,
                confirm=confirm,
            )
        )
        self.assertFalse(
            launcher.resolve_audio_mode(
                False,
                selected_interactively=True,
                frozen=True,
                confirm=confirm,
            )
        )
        self.assertEqual(prompts, ["prompted"])

        source = (ROOT / "qemu" / "run_desktop_emulator.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"--no-audio"', source)
        self.assertIn("audio_enabled = resolve_audio_mode", source)

    def test_dual_arch_packaged_firmware_identity_gate(self):
        report = json.loads(
            (HERE / "AR172_DESKTOP_FIRMWARE_IDENTITY_GATE.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            report["status"], "PASS_DUAL_ARCH_PACKAGED_FIRMWARE_IDENTITY"
        )
        self.assertEqual(report["workflow_run"]["id"], 35057744335)
        self.assertEqual(report["workflow_run"]["number"], 25)
        self.assertEqual(report["workflow_run"]["conclusion"], "success")
        self.assertEqual(
            report["workflow_run"]["head_sha"],
            report["implementation"]["commit"],
        )
        self.assertEqual(
            {job["architecture"] for job in report["jobs"]},
            {"arm64", "x86_64"},
        )
        for job in report["jobs"]:
            self.assertEqual(job["conclusion"], "success")
            self.assertEqual(job["packaged_identity_smoke_test"], "success")
            self.assertEqual(job["enforced_artifact_audit"], "success")
            self.assertEqual(job["artifact_upload"], "success")
        for architecture, artifact in report["artifacts"].items():
            self.assertIn(architecture, artifact["name"])
            self.assertRegex(artifact["digest"], r"^sha256:[0-9a-f]{64}$")
            self.assertGreater(artifact["size_in_bytes"], 20_000_000)
        assertions = " ".join(report["packaged_identity_assertions"])
        self.assertIn("FILTER 2 + LFO2 VERIFIED", assertions)
        self.assertIn("STOCK MODE / EXTENSION OFF", assertions)
        self.assertIn("UNCHANGED STOCK FALLBACK", assertions)
        self.assertIn("window-title composition", assertions)


if __name__ == "__main__":
    unittest.main()
