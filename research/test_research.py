#!/usr/bin/env python3

import hashlib
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from ar172_extract import NRV2BDepacker, decode_sysex, parse_ele3
from audio_stream_trace import trace as trace_audio_stream
from audio_interface_trace import trace as trace_audio_interface
from audio_handoff_trace import trace as trace_audio_handoff
from br_bridge_trace import trace
from br_consumer_trace import trace as trace_br_consumer
from br_quantizer_runtime_trace import trace as trace_br_quantizer_runtime
from control_frame_trace import trace as trace_control_frame
from filter2_bypass_timing_trace import trace as trace_filter2_bypass_timing
from fpga_iob_mode_inventory import inventory as inventory_fpga_iobs
from lfo2_filter2_reference import run_tests
from runtime_descriptor_probe import probe as probe_runtime_descriptors
from sample_storage_trace import trace as trace_sample_storage
from sample_slot_trace import trace as trace_sample_slot


class FpgaIobGeometryTests(unittest.TestCase):
    def test_bond_and_iob_coordinate_tables(self):
        from fpga_iob_mode_inventory import BOND57, IOB_BITS, bel_for

        self.assertEqual(len(BOND57), 68)
        self.assertEqual(len({pin for pin, *_ in BOND57}), 68)
        self.assertEqual([len(IOB_BITS[x]) for x in "WESN"], [8, 8, 5, 5])
        self.assertEqual(bel_for("W", 29, 0), 0)
        self.assertEqual(bel_for("E", 29, 1), 6)
        self.assertEqual(bel_for("S", 11, 2), 2)
        self.assertEqual(bel_for("N", 13, 2), 2)

    def test_dspi_first_hop_rejects_locality_quartet(self):
        report = json.loads(
            (HERE / "AR172_FPGA_DSPI_FIRST_HOP_TRACE.json").read_text(encoding="utf-8")
        )
        self.assertEqual(report["result"], "PASS_P28_P31_QUARTET_REJECTED")
        self.assertEqual(report["p28_p31_hypothesis"]["status"], "REJECTED")
        routes = report["p28_p31_hypothesis"]["routes"]
        self.assertEqual(
            {pin: row["configured_first_hop_consumers"] for pin, row in routes.items()},
            {"28": [], "29": [], "30": [], "31": []},
        )
        self.assertEqual(
            report["p29_sin_output_path"]["ioi_mux_o"]["selected"], "NONE"
        )

    def test_dspi1_br_receive_is_drain_only(self):
        report = json.loads(
            (HERE / "AR172_DSPI1_TX_ONLY_TRACE.json").read_text(encoding="utf-8")
        )
        self.assertEqual(report["result"], "PASS_DSPI1_BR_TRANSPORT_TX_ONLY_IN_SOFTWARE")
        self.assertEqual(report["transmit_path"]["edma_channel"], 15)
        self.assertEqual(report["transmit_path"]["destination"], "0xFC03C034")
        self.assertEqual(report["receive_reads"]["literal_reference_count"], 8)
        self.assertEqual(
            [row["words"] for row in report["receive_reads"]["drain_idioms"]],
            [4, 3, 16],
        )

    def test_dspi1_wire_mode(self):
        wire = json.loads(
            (HERE / "AR172_DSPI1_WIRE_MODE_TRACE.json").read_text(encoding="utf-8")
        )
        self.assertEqual(wire["result"], "PASS_DSPI1_BR_WIRE_MODE_IDENTIFIED")
        self.assertEqual(
            wire["wire_mode"],
            {
                "word_bits": 16,
                "spi_mode": 3,
                "clock_idle": "high",
                "data_change_edge": "falling",
                "data_sample_edge": "rising",
                "bit_order": "MSB-first",
                "sck": "internal bus clock / 8",
            },
        )


class QemuEmacPatchTests(unittest.TestCase):
    def test_load_operand_and_fractional_scale_patch(self):
        patch = (
            ROOT
            / "qemu"
            / "patches"
            / "0003-m68k-fix-coldfire-emac-load-operands.patch"
        ).read_text(encoding="utf-8")
        self.assertIn("DREG(ext, 12)", patch)
        self.assertIn("if (ext & 0x100)", patch)
        self.assertIn("(int64_t)(int32_t)op1 * (int32_t)op2", patch)
        self.assertIn("product >>= 23", patch)

    def test_audio_interpreter_emac_gate_records_mixer_and_fixture_correction(self):
        report = json.loads(
            (HERE / "AR172_MINICOLDFIRE_AUDIO_EMAC_GATE.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            report["status"],
            "PASS_AUDIO_INTERPRETER_EMAC_ALIGNED_STOCK_MIXER_ACTIVE",
        )
        proof = report["bounded_runtime_proof"]
        self.assertEqual(proof["unique_writes_per_callback"], 256)
        self.assertEqual(proof["nonzero_words_per_callback"], [31, 32])
        self.assertGreater(proof["maximum_s16"], 0)
        self.assertEqual(
            report["evidence_correction"]["status"],
            "RESOLVED_EXPLICIT_SAMPLE_LEVEL_PRECONDITION",
        )


class MacosPackagingTests(unittest.TestCase):
    def test_factory_storage_device_is_included_in_qemu_build(self):
        workflow = (
            ROOT / ".github" / "workflows" / "package-macos-app.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "cp research/qemu/hw/m68k/ar_mk2_esdhc.c qemu-src/hw/m68k/",
            workflow,
        )
        self.assertIn("'ar_mk2_esdhc.c'", workflow)

    def test_filter2_runtime_modules_are_on_pyinstaller_path(self):
        workflow = (
            ROOT / ".github" / "workflows" / "package-macos-app.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("working-directory: research", workflow)
        self.assertIn('--paths "$GITHUB_WORKSPACE/research/research"', workflow)
        self.assertIn("--hidden-import audio_callback_probe", workflow)
        self.assertIn("--hidden-import lfo2_extended_waveform_probe", workflow)


class QemuAudioEdmaTests(unittest.TestCase):
    def test_linked_audio_descriptor_semantics_are_modeled(self):
        source = (
            ROOT / "qemu" / "hw" / "m68k" / "ar_mk2_intc_pit.c"
        ).read_text(encoding="utf-8")
        self.assertIn("AR_EDMA_CSR_ESG", source)
        self.assertIn("ar_edma_iterations(citer_word)", source)
        self.assertIn("ar_edma_set_iterations(citer_word, citer)", source)
        self.assertIn("physical_memory_read(scatter_gather, tcd, AR_EDMA_TCD_SIZE)", source)
        self.assertIn("ar_edma_software_start", source)
        self.assertIn("ar_edma_pump_channel(&c->edma, 30)", source)
        self.assertIn("AR_EDMA_DSPI1_TX_CHANNEL 15u", source)
        self.assertIn("ar_edma_pump_dspi1_tx", source)
        self.assertIn(
            "ar_edma_pump_channel(s, AR_EDMA_DSPI1_TX_CHANNEL)", source
        )
        self.assertIn("ar_edma_pump_dspi1_tx(&c->edma)", source)

    def test_desktop_audio_service_is_bounded_by_pad_edges(self):
        source = (
            ROOT / "qemu" / "hw" / "m68k" / "ar_mk2_intc_pit.c"
        ).read_text(encoding="utf-8")
        self.assertIn("AR_AUDIO_TRIGGER_BLOCKS 8u", source)
        self.assertIn("AR_AUDIO_TRIGGER_DELAY_TICKS 10u", source)
        self.assertIn("ar_panel_audio_observe", source)
        self.assertIn("group == 2 || group == 3", source)
        self.assertIn("c->audio_service_budget--", source)
        self.assertIn("c->audio_service_delay--", source)
        self.assertIn("c->audio_service_pending", source)
        self.assertIn("c->audio_service_entered", source)
        self.assertIn("c->audio_service_completed", source)
        self.assertIn("c->mock_audio_service && !c->audio_service_pending", source)
        self.assertIn("c->audio_service_pending = true", source)
        self.assertIn("completed vector 191 service count=%u", source)
        self.assertIn("c->intc[1].ifr & (1ULL << 63)", source)
        self.assertIn("c->cpu->env.sr & SR_I", source)
        self.assertIn("fresh peripheral request activates a reloaded major loop", source)
        self.assertIn("csr & ~AR_EDMA_CSR_DONE", source)
        self.assertIn(
            "c->audio_service_delay = AR_AUDIO_TRIGGER_DELAY_TICKS", source
        )
        self.assertIn("AR_MK2_AUDIO_TRIGGER_SERVICE", source)
        launcher = (ROOT / "qemu" / "run_desktop_emulator.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('env["AR_MK2_AUDIO_TRIGGER_SERVICE"] = "1"', launcher)

    def test_desktop_audio_tap_is_stable_passive_and_stereo(self):
        source = (
            ROOT / "qemu" / "hw" / "m68k" / "elektron_ar_mk2.c"
        ).read_text(encoding="utf-8")
        self.assertIn("AR_RENDER_RING_COUNT   4u", source)
        self.assertIn("AR_RENDER_INDEX_ADDR   0x42F78044u", source)
        self.assertIn("AR_RENDER_FRAME_BYTES  0x40u", source)
        self.assertIn("AR_RENDER_LANES        8u", source)
        self.assertIn("ar_capture_renderer_block", source)
        self.assertIn("audio_candidate_valid", source)
        self.assertIn("audio_be_write", source)
        self.assertIn("AR_MK2_AUDIO_TAP", source)
        self.assertNotIn("ar_mk2_audio_service_request", source)

    def test_headless_gate_uses_desktop_events_and_bounded_audio(self):
        source = (ROOT / "qemu" / "headless_ui_smoke.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("follow_events", source)
        self.assertIn('env["AR_MK2_AUDIO_TRIGGER_SERVICE"] = "1"', source)
        self.assertIn("--exercise-active-retrigger", source)
        self.assertIn("native_retrigger_reset", source)
        self.assertIn("gdb_write_memory", source)
        self.assertIn('"phase": (0x402B4520, 0x12345678)', source)
        self.assertIn("--exercise-retrigger-matrix", source)
        self.assertIn("non_target_words_preserved", source)
        self.assertIn("--exercise-retrigger-negative-controls", source)
        self.assertIn("free_mode_note_on", source)
        self.assertIn("wait_lfo2_matrix_preserved", source)
        self.assertIn("--exercise-explicit-reset-matrix", source)
        self.assertIn("selective_explicit_reset", source)
        self.assertIn("unchanged_reset_generations", source)
        self.assertIn("--exercise-trigger-chord", source)
        self.assertIn("TRIG 1 + TRIG 2 CHORD", source)
        self.assertIn("wait_guest_words", source)
        self.assertIn('emit_event(events_file, "trig", "1", "press")', source)
        self.assertIn("wait_snapshot(controls_file, first.encode(), deadline)", source)
        self.assertIn("wait_hmp_value", source)
        self.assertIn('f"wav,path={audio_wav}"', source)
        self.assertIn('hmp_command(monitor_port, "quit")', source)
        self.assertIn('header[4:8] == bytes(4)', source)
        self.assertIn("requires at least 8 services per trigger", source)
        self.assertIn('audio_metrics["contains_nonzero_pcm"]', source)

    def test_headless_wav_metrics_closes_qemu_placeholder_header(self):
        from qemu.headless_ui_smoke import wav_metrics

        header = bytes.fromhex(
            "524946460000000057415645666d74201000000001000200"
            "44ac000010b10200040010006461746100000000"
        )
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "capture.wav"
            capture.write_bytes(header + bytes.fromhex("01000200fdff0400"))
            result = wav_metrics(capture)
            self.assertTrue(result["header_repaired"])
            self.assertEqual(result["frames"], 2)
            self.assertEqual(result["pcm_bytes"], 8)
            self.assertTrue(result["contains_nonzero_pcm"])


class DesktopPanelInputTests(unittest.TestCase):
    @staticmethod
    def make_headless_panel(event_file):
        from qemu.desktop_panel import PanelApp

        class RootStub:
            def __init__(self):
                self.callbacks = {}
                self.next_callback = 0

            def after(self, _delay, callback):
                self.next_callback += 1
                token = f"after-{self.next_callback}"
                self.callbacks[token] = callback
                return token

            def after_cancel(self, token):
                self.callbacks.pop(token, None)

            def after_idle(self, callback):
                callback()

            def focus_get(self):
                return None

            def run_pending(self):
                callbacks = list(self.callbacks.values())
                self.callbacks.clear()
                for callback in callbacks:
                    callback()

        class StatusStub:
            def set(self, value):
                self.value = value

        class TrigStub:
            def __init__(self):
                self.states = []

            def set_active(self, active):
                self.states.append(active)

        panel = PanelApp.__new__(PanelApp)
        panel.root = RootStub()
        panel.event_file = event_file
        panel.held_trigs = {}
        panel.pending_key_releases = {}
        panel.trig_widgets = {1: TrigStub()}
        panel.encoder_values = {}
        panel.status = StatusStub()
        return panel

    @staticmethod
    def panel_events(event_file):
        if not event_file.exists():
            return []
        return [json.loads(line) for line in event_file.read_text().splitlines()]

    def test_qwerty_layout_and_knob_clamp(self):
        from qemu.desktop_panel import (
            BUTTON_RECTS,
            KNOB_CENTERS,
            OLED_RECT,
            PAD_RECTS,
            PanelApp,
            PAGE_BUTTONS,
            PANEL_ASPECT,
            QWERTY_TRIGS,
            SKIN_H,
            SKIN_W,
            STATUS_H,
            TRIG_RECTS,
            active_crop_specs,
            clamp_panel_value,
            panel_window_size,
            skin_asset_path,
            validate_skin_geometry,
        )

        self.assertEqual(
            [QWERTY_TRIGS[key] for key in "qwertyuiasdfghjk"],
            list(range(1, 17)),
        )
        self.assertEqual(clamp_panel_value(-1), 0)
        self.assertEqual(clamp_panel_value(64), 64)
        self.assertEqual(clamp_panel_value(128), 127)
        self.assertEqual(PAGE_BUTTONS, ("TRIG", "SYN", "SMP", "FLTR", "AMP", "LFO"))
        window_w, window_h = panel_window_size(6)
        self.assertAlmostEqual(window_w / window_h, PANEL_ASPECT, places=2)
        self.assertEqual(panel_window_size(6), panel_window_size(6))
        self.assertEqual((window_w, window_h), (SKIN_W, SKIN_H + STATUS_H))
        self.assertEqual(set(KNOB_CENTERS), set("ABCDEFGHI"))
        self.assertEqual(set(BUTTON_RECTS), set(PAGE_BUTTONS + ("YES", "NO")))
        self.assertEqual(set(TRIG_RECTS), set(range(1, 17)))
        self.assertEqual(set(PAD_RECTS), set(range(1, 13)))
        self.assertGreater(OLED_RECT[2] - OLED_RECT[0], 200)
        self.assertGreater(OLED_RECT[3] - OLED_RECT[1], 100)
        self.assertTrue(skin_asset_path().is_file())
        self.assertTrue(skin_asset_path("photon_panel_active.png").is_file())
        validate_skin_geometry()
        specs = active_crop_specs()
        self.assertEqual(len(specs), 36)
        self.assertEqual(len({key for key, _rect, _margin in specs}), 36)
        for _key, rect, margin in specs:
            x1, y1, x2, y2 = PanelApp.expanded_rect(rect, margin)
            self.assertTrue(0 <= x1 < x2 <= SKIN_W)
            self.assertTrue(0 <= y1 < y2 <= SKIN_H)

    def test_qwerty_repeat_suppression_release_and_second_press(self):
        class Event:
            keysym = "q"

        with tempfile.TemporaryDirectory() as directory:
            event_file = Path(directory) / "panel-events.jsonl"
            panel = self.make_headless_panel(event_file)

            self.assertEqual(panel.key_press(Event()), "break")
            self.assertEqual(panel.key_press(Event()), "break")
            panel.key_release(Event())
            self.assertEqual(panel.key_press(Event()), "break")
            panel.root.run_pending()
            self.assertEqual(len(self.panel_events(event_file)), 1)

            panel.key_release(Event())
            panel.root.run_pending()
            panel.key_press(Event())
            panel.key_release(Event())
            panel.root.run_pending()

            events = self.panel_events(event_file)
            self.assertEqual(
                [(event["kind"], event["name"], event["value"])
                 for event in events],
                [
                    ("trig", "1", "press"),
                    ("trig", "1", "release"),
                    ("trig", "1", "press"),
                    ("trig", "1", "release"),
                ],
            )
            self.assertEqual(panel.trig_widgets[1].states,
                             [True, False, True, False])
            self.assertFalse(panel.held_trigs[1])
            self.assertFalse(panel.pending_key_releases)

    def test_photographic_hitbox_centers_dispatch_every_control(self):
        from qemu.desktop_panel import (
            BUTTON_RECTS,
            KNOB_CENTERS,
            PanelApp,
            TRIG_RECTS,
        )

        panel = PanelApp.__new__(PanelApp)
        for name, (x, y) in KNOB_CENTERS.items():
            self.assertEqual(panel.control_at(x, y), ("encoder", name))
        for name, (x1, y1, x2, y2) in BUTTON_RECTS.items():
            self.assertEqual(
                panel.control_at((x1 + x2) // 2, (y1 + y2) // 2),
                ("button", name),
            )
        for trig, (x1, y1, x2, y2) in TRIG_RECTS.items():
            self.assertEqual(
                panel.control_at((x1 + x2) // 2, (y1 + y2) // 2),
                ("trig", trig),
            )

    def test_mixed_source_ownership_and_focus_loss_release_once(self):
        class Event:
            keysym = "q"

        with tempfile.TemporaryDirectory() as directory:
            event_file = Path(directory) / "panel-events.jsonl"
            panel = self.make_headless_panel(event_file)

            panel.trig(1, True, "mouse")
            panel.key_press(Event())
            panel.key_release(Event())
            panel.root.run_pending()
            self.assertEqual(len(self.panel_events(event_file)), 1)
            self.assertEqual(panel.held_trigs[1], {"mouse"})

            panel.key_press(Event())
            panel.key_release(Event())
            panel.focus_lost()
            panel.root.run_pending()
            events = self.panel_events(event_file)
            self.assertEqual(
                [event["value"] for event in events], ["press", "release"]
            )
            self.assertEqual(panel.trig_widgets[1].states, [True, False])
            self.assertFalse(panel.held_trigs[1])
            self.assertFalse(panel.pending_key_releases)

    def test_qwerty_lifecycle_replays_as_exact_uart8_frames(self):
        from qemu.panel_event_bridge import PanelLink, follow_events

        class Event:
            keysym = "q"

        class SocketStub:
            def __init__(self):
                self.frames = []

            def sendall(self, data):
                self.frames.append(data)

        with tempfile.TemporaryDirectory() as directory:
            event_file = Path(directory) / "panel-events.jsonl"
            panel = self.make_headless_panel(event_file)
            for _ in range(2):
                panel.key_press(Event())
                panel.key_press(Event())
                panel.key_release(Event())
                panel.root.run_pending()

            sock = SocketStub()
            stop = threading.Event()
            follower = threading.Thread(
                target=follow_events,
                args=(event_file, PanelLink(sock, verbose=False), False,
                      None, stop),
            )
            follower.start()
            deadline = time.monotonic() + 1.0
            while len(sock.frames) < 4 and time.monotonic() < deadline:
                time.sleep(0.01)
            stop.set()
            follower.join(timeout=1.0)

            self.assertFalse(follower.is_alive())
            self.assertEqual(
                sock.frames,
                [b"\x23\x01", b"\x23\x00", b"\x23\x01", b"\x23\x00"],
            )

    def test_overlapping_qwerty_chord_preserves_group_masks(self):
        from qemu.panel_event_bridge import PanelLink, follow_events

        class Event:
            def __init__(self, keysym):
                self.keysym = keysym

        class SocketStub:
            def __init__(self):
                self.frames = []

            def sendall(self, data):
                self.frames.append(data)

        with tempfile.TemporaryDirectory() as directory:
            event_file = Path(directory) / "panel-events.jsonl"
            panel = self.make_headless_panel(event_file)
            panel.trig_widgets[2] = panel.trig_widgets[1].__class__()
            panel.key_press(Event("q"))
            panel.key_press(Event("w"))
            panel.key_press(Event("w"))
            panel.key_release(Event("q"))
            panel.root.run_pending()
            panel.key_release(Event("w"))
            panel.root.run_pending()

            sock = SocketStub()
            stop = threading.Event()
            follower = threading.Thread(
                target=follow_events,
                args=(event_file, PanelLink(sock, verbose=False), False,
                      None, stop),
            )
            follower.start()
            deadline = time.monotonic() + 1.0
            while len(sock.frames) < 4 and time.monotonic() < deadline:
                time.sleep(0.01)
            stop.set()
            follower.join(timeout=1.0)

            self.assertEqual(
                sock.frames,
                [b"\x23\x01", b"\x23\x03", b"\x23\x02", b"\x23\x00"],
            )
            self.assertFalse(panel.held_trigs[1])
            self.assertFalse(panel.held_trigs[2])

    def test_full_qwerty_matrix_replays_exact_uart8_frames(self):
        from qemu.panel_event_bridge import PanelLink, follow_events

        class Event:
            def __init__(self, keysym):
                self.keysym = keysym

        class SocketStub:
            def __init__(self):
                self.frames = []

            def sendall(self, data):
                self.frames.append(data)

        with tempfile.TemporaryDirectory() as directory:
            event_file = Path(directory) / "panel-events.jsonl"
            panel = self.make_headless_panel(event_file)
            trig_stub = panel.trig_widgets[1].__class__
            panel.trig_widgets = {trig: trig_stub() for trig in range(1, 17)}

            for key in "qwertyuiasdfghjk":
                panel.key_press(Event(key))
                panel.key_release(Event(key))
                panel.root.run_pending()

            sock = SocketStub()
            stop = threading.Event()
            follower = threading.Thread(
                target=follow_events,
                args=(event_file, PanelLink(sock, verbose=False), False,
                      None, stop),
            )
            follower.start()
            deadline = time.monotonic() + 1.0
            while len(sock.frames) < 32 and time.monotonic() < deadline:
                time.sleep(0.01)
            stop.set()
            follower.join(timeout=1.0)

            expected = []
            for group in (3, 2):
                for bit in range(8):
                    expected.extend(
                        [bytes((0x20 | group, 1 << bit)),
                         bytes((0x20 | group, 0))]
                    )
            self.assertFalse(follower.is_alive())
            self.assertEqual(sock.frames, expected)
            self.assertEqual(len(self.panel_events(event_file)), 32)
            self.assertTrue(all(not owners for owners in panel.held_trigs.values()))
            self.assertFalse(panel.pending_key_releases)

    def test_cross_group_qwerty_chord_keeps_masks_independent(self):
        from qemu.panel_event_bridge import PanelLink, follow_events

        class Event:
            def __init__(self, keysym):
                self.keysym = keysym

        class SocketStub:
            def __init__(self):
                self.frames = []

            def sendall(self, data):
                self.frames.append(data)

        with tempfile.TemporaryDirectory() as directory:
            event_file = Path(directory) / "panel-events.jsonl"
            panel = self.make_headless_panel(event_file)
            panel.trig_widgets[9] = panel.trig_widgets[1].__class__()
            panel.key_press(Event("q"))
            panel.key_press(Event("a"))
            panel.key_press(Event("a"))
            panel.key_release(Event("q"))
            panel.root.run_pending()
            panel.key_release(Event("a"))
            panel.root.run_pending()

            sock = SocketStub()
            stop = threading.Event()
            follower = threading.Thread(
                target=follow_events,
                args=(event_file, PanelLink(sock, verbose=False), False,
                      None, stop),
            )
            follower.start()
            deadline = time.monotonic() + 1.0
            while len(sock.frames) < 4 and time.monotonic() < deadline:
                time.sleep(0.01)
            stop.set()
            follower.join(timeout=1.0)

            self.assertFalse(follower.is_alive())
            self.assertEqual(
                sock.frames,
                [b"\x23\x01", b"\x22\x01", b"\x23\x00", b"\x22\x00"],
            )
            self.assertFalse(panel.held_trigs[1])
            self.assertFalse(panel.held_trigs[9])
            self.assertFalse(panel.pending_key_releases)

    def test_knob_delta_uses_validated_signed_encoder_frame(self):
        from qemu.panel_event_bridge import PanelLink

        class SocketStub:
            def __init__(self):
                self.frames = []

            def sendall(self, data):
                self.frames.append(data)

        sock = SocketStub()
        link = PanelLink(sock)
        link.encoder("A", 127)
        link.encoder("I", -127)
        self.assertEqual(sock.frames, [bytes((0x30, 0x7F)), bytes((0x38, 0x81))])

    def test_filter2_controls_publish_as_one_eight_byte_snapshot(self):
        from qemu.panel_event_bridge import publish_filter2_controls

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "filter2-controls.bin"
            controls = bytearray((0, 16, 32, 48, 64, 80, 96, 127))
            publish_filter2_controls(path, controls)
            self.assertEqual(path.read_bytes(), bytes(controls))
            self.assertFalse(path.with_suffix(".bin.tmp").exists())

    def test_runtime_controls_publish_complete_exact_lfo2_state(self):
        from qemu.panel_event_bridge import (
            RUNTIME_SNAPSHOT_SIZE,
            RuntimeControls,
            control_to_q31,
            publish_runtime_controls,
            rate_to_increment,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "filter2-controls.bin"
            controls = RuntimeControls()
            controls.filter2[2] = 127
            controls.waveform[2] = 6
            controls.mode[2] = 3
            controls.rate[2] = 127
            controls.depth[2] = 32
            controls.enable_mask = 1 << 2
            controls.trigger_mask = 1 << 2
            controls.reset_generation[2] = 1
            publish_runtime_controls(path, controls)
            snapshot = path.read_bytes()
            self.assertEqual(len(snapshot), RUNTIME_SNAPSHOT_SIZE)
            self.assertEqual(snapshot[:8], b"F2L2\x01\x00\x00\x00")
            self.assertEqual(snapshot[10], 127)
            self.assertEqual(snapshot[18], 6)
            self.assertEqual(snapshot[26], 3)
            self.assertEqual(int.from_bytes(snapshot[40:44], "big"), rate_to_increment(127))
            self.assertEqual(int.from_bytes(snapshot[72:76], "big"), control_to_q31(32))
            self.assertEqual(snapshot[96:100], b"\x00\x04\x00\x04")
            self.assertEqual(snapshot[102], 1)
            restored = RuntimeControls.decode(snapshot)
            self.assertEqual(restored.rate[2], 127)
            self.assertEqual(restored.depth[2], 32)
            self.assertEqual(restored.enable_mask, 1 << 2)
            self.assertFalse(path.with_suffix(".bin.tmp").exists())

    def test_qemu_machine_imports_live_filter2_targets(self):
        source = (
            ROOT / "qemu" / "hw" / "m68k" / "elektron_ar_mk2.c"
        ).read_text(encoding="utf-8")
        self.assertIn("AR_MK2_FILTER2_CONTROL_IN", source)
        self.assertIn("ar_import_filter2_controls", source)
        self.assertIn("AR_FILTER2_STATE0", source)
        self.assertIn("AR_LFO2_STATE0", source)
        self.assertIn("AR_RUNTIME_CONTROL_SIZE 108u", source)
        self.assertIn('memcmp(contents, "F2L2\\x01", 5)', source)
        self.assertIn("physical_memory_write(target, word, sizeof(word))", source)


@unittest.skipUnless(
    (HERE / "extracted_stock_nrv" / "section_2_id_1.decompressed.bin").exists(),
    "extracted proprietary FPGA image not present",
)
class FpgaIobInventoryTests(unittest.TestCase):
    def test_stock_iob_inventory_and_top_dspi_cluster(self):
        image = HERE / "extracted_stock_nrv" / "section_2_id_1.decompressed.bin"
        result = inventory_fpga_iobs(image)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["summary"]["bonded_user_pins"], 68)
        self.assertEqual(result["summary"]["directions"]["input"], 5)
        self.assertEqual(result["summary"]["directions"]["input-only"], 2)
        self.assertEqual(result["summary"]["directions"]["bidirectional"], 1)
        self.assertEqual(result["summary"]["directions"]["output"], 45)
        top = result["dspi_candidate_clusters"][0]
        self.assertEqual(top["package_pins"], [39, 43, 46])
        self.assertEqual(top["clock_capable_inputs"], ["GCLK0"])
        self.assertEqual(
            result["resolved_dspi_ingress"]["SOUT"], "P51 / IOB_S24_0"
        )


@unittest.skipUnless(
    (ROOT / "imported" / "Analog-Rytm_MKII_OS1.72.syx").exists(),
    "proprietary stock firmware not present",
)
class FirmwareExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stock_path = ROOT / "imported" / "Analog-Rytm_MKII_OS1.72.syx"

    def test_stock_transport_and_container(self):
        container, transport = decode_sysex(self.stock_path)
        self.assertEqual(transport["packet_count"], 13329)
        self.assertEqual(transport["bad_frames"], [])
        self.assertEqual(transport["container_size"], 1346176)
        self.assertEqual(
            transport["source_sha256"],
            "1ea60357abe8b876d8b9c52e6dcd988d833478a49d09e3cb22d42782ef822b2f",
        )
        self.assertEqual(container[:4], b"ELE3")

    def test_ele3_map_and_stream_sums(self):
        container, _ = decode_sysex(self.stock_path)
        header, sections = parse_ele3(container)
        self.assertEqual(header["hardware"], "0162")
        self.assertEqual(header["version"], "1.72")
        self.assertEqual([section["id"] for section in sections], [5, 2, 1, 3])
        self.assertEqual(sections[3]["load_address"], 0x40000400)
        for section in sections[1:]:
            data = section["data"]
            stream_length = int.from_bytes(data[:4], "big")
            stream_sum = int.from_bytes(data[4:8], "big")
            stream = data[8 : 8 + stream_length]
            self.assertEqual(len(stream), stream_length)
            self.assertEqual(sum(stream) & 0xFFFFFFFF, stream_sum)

    def test_nrv2b_section_images(self):
        container, _ = decode_sysex(self.stock_path)
        _, sections = parse_ele3(container)
        expected = {
            2: (
                42302,
                "9b7ed34c1ce6ff5c2842b581f00140a864b30b185a128c61ef784a23db301dbc",
            ),
            1: (
                149516,
                "7c8bff3cb411ed93434b3a4eab846738be6241d64aeb23b4061eb11a8aa29b2f",
            ),
            3: (
                2903032,
                "5d0b41eed77bb08b08be13ac63c6e8f0bb6a7334195436eb0ec6b5a5f26d6772",
            ),
        }
        for section in sections[1:]:
            data = section["data"]
            stream_length = int.from_bytes(data[:4], "big")
            unpacked = NRV2BDepacker(data[8 : 8 + stream_length]).depack()
            size, digest = expected[section["id"]]
            self.assertEqual(len(unpacked), size)
            self.assertEqual(hashlib.sha256(unpacked).hexdigest(), digest)


@unittest.skipUnless(
    (HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin").exists(),
    "extracted proprietary MAIN image not present",
)
class SampleStorageTraceTests(unittest.TestCase):
    def test_independent_manifest_and_verification_gates(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        result = trace_sample_storage(main_image)
        self.assertEqual(result["result"], "PASS")
        gates = result["storage_gates"]
        self.assertEqual(gates["factory_manifest"]["magic"], "MaGj")
        self.assertEqual(gates["factory_manifest"]["storage"], "+Drive/eSDHC")
        self.assertEqual(gates["sample_verification"]["magic"], "SM")
        self.assertEqual(gates["sample_verification"]["startup_expected_version"], 2)
        self.assertNotEqual(
            gates["factory_manifest"]["storage"],
            gates["sample_verification"]["storage"],
        )


@unittest.skipUnless(
    (HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin").exists(),
    "extracted proprietary MAIN image not present",
)
class SampleSlotTraceTests(unittest.TestCase):
    def test_project_sample_slot_boundary(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        result = trace_sample_slot(main_image)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["sample_slot_parameter"]["id"], "0x29")
        self.assertEqual(result["sample_slot_parameter"]["zero_meaning"], "OFF")
        self.assertEqual(result["picker"]["entry_count"], 128)
        self.assertEqual(result["picker"]["domain"], "OFF plus sample slots 1..127")
        self.assertEqual(result["sentinels"]["empty"], "---")
        self.assertEqual(result["runtime_tables"]["name_pointer_table"], "0x41928DCC")


class ReferenceModelTests(unittest.TestCase):
    def test_reference_vectors(self):
        result = run_tests()
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["tests"]["filter_stability"]["cases"], 192)
        self.assertEqual(
            result["tests"]["lfo_periodicity"]["vector_sha256"],
            "a6504c4685c5fb409e626e396bb29e9cea116432069dc5ec7afdf23cbb3ca7c0",
        )


@unittest.skipUnless(
    (HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin").exists(),
    "extracted proprietary MAIN image not present",
)
class BridgeTraceTests(unittest.TestCase):
    def test_br_bridge_signatures(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        result = trace(main_image)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["track_parameter_record"]["word_count"], 42)
        self.assertEqual(result["bit_reduction"]["sound_subview_byte_offset"], "0x3C")
        self.assertEqual(result["bit_reduction"]["packed_record_byte_offset"], "0x4E")
        self.assertEqual(result["bit_reduction"]["track_count"], 12)


@unittest.skipUnless(
    (HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin").exists(),
    "extracted proprietary MAIN image not present",
)
class ControlFrameTraceTests(unittest.TestCase):
    def test_control_frame_and_br_descriptor_geometry(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        result = trace_control_frame(main_image)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["control_frame"]["word_count"], 572)
        self.assertEqual(result["control_frame"]["first_record"], "0x8000F7A8")
        self.assertEqual(result["bit_reduction"]["packed_record_word_index"], 39)
        self.assertEqual(result["bit_reduction"]["frame_destinations_per_track"], 8)


@unittest.skipUnless(
    (HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin").exists(),
    "extracted proprietary MAIN image not present",
)
class AudioStreamTraceTests(unittest.TestCase):
    def test_audio_tick_and_stream_descriptor_geometry(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        result = trace_audio_stream(main_image)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["audio_tick"]["edma_channel"], 54)
        self.assertEqual(
            result["audio_tick"]["render_sequence"],
            ["0x40117F00", "0x4010A2E0", "0x40108944", "0x40105188"],
        )
        streams = result["generic_stream_descriptors"]
        self.assertEqual(streams["record_bytes"], 16)
        self.assertEqual(streams["record_count"], 131)
        self.assertEqual(streams["default_rate"], 48000)
        self.assertIn("unproven", streams["classification"])
        self.assertFalse(result["per_voice_control"]["bit_reduction_read_directly_here"])


@unittest.skipUnless(
    (HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin").exists(),
    "extracted proprietary MAIN image not present",
)
class BitReductionConsumerTests(unittest.TestCase):
    def test_control_smoother_and_emac_smoke_calls(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = ROOT / "recovered_library" / "minicoldfire.py"
        result = trace_br_consumer(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        smoother = result["control_smoother"]
        self.assertEqual(smoother["words_per_record"], 42)
        self.assertEqual(smoother["br_address_track_0"], "0x8000F7F6")
        self.assertEqual(smoother["br_pair_iteration_zero_based"], 19)
        self.assertEqual(
            result["downstream_smoke_calls"]["control_converter_0x40105188"]["emac_instructions"],
            48,
        )


@unittest.skipUnless(
    (HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin").exists(),
    "extracted proprietary MAIN image not present",
)
class AudioInterfaceTraceTests(unittest.TestCase):
    def test_ready_and_tcd30_poll_runtime(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = ROOT / "recovered_library" / "minicoldfire.py"
        result = trace_audio_interface(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["pre_render_runtime"]["result"], "PASS")
        self.assertEqual(result["tcd30_poll_runtime"]["busy_observations"], 1)
        self.assertEqual(result["tcd30_poll_runtime"]["final_polled_bit"], 0)


@unittest.skipUnless(
    (HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin").exists(),
    "extracted proprietary MAIN image not present",
)
class BitReductionQuantizerRuntimeTests(unittest.TestCase):
    def test_terminal_read_coefficients_and_32_sample_loop(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = ROOT / "recovered_library" / "minicoldfire.py"
        result = trace_br_quantizer_runtime(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["execution"]["br_levels"], 8)
        self.assertEqual(result["execution"]["loop_iterations"], 128)
        self.assertEqual(result["execution"]["quantizer_samples"], 256)
        self.assertEqual(result["execution"]["sample_matches"], 256)


@unittest.skipUnless(
    (HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin").exists(),
    "extracted proprietary MAIN image not present",
)
class AudioHandoffTraceTests(unittest.TestCase):
    def test_voice_transpose_staging_and_dma_direction(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = ROOT / "recovered_library" / "minicoldfire.py"
        result = trace_audio_handoff(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        pipeline = result["pipeline"]
        self.assertEqual(pipeline["renderer"]["tagged_input_words_matched"], 256)
        self.assertEqual(pipeline["renderer"]["address_permutation_words_matched"], 256)
        self.assertEqual(pipeline["handoff"]["source_reads"], 512)
        self.assertEqual(pipeline["handoff"]["unique_source_longwords"], 256)
        self.assertEqual(pipeline["outbound_dma_tcd42"]["channel"], 42)
        self.assertEqual(pipeline["outbound_dma_tcd42"]["major_bytes"], 256)
        self.assertEqual(result["input_dma_tcd30"]["channel"], 30)
        self.assertIn("input/capture", result["input_dma_tcd30"]["classification"])
        self.assertIn("not proven", result["filter2_insertion"]["cycle_status"])


@unittest.skipUnless(
    (HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin").exists(),
    "extracted proprietary MAIN image not present",
)
class Filter2BypassTimingTraceTests(unittest.TestCase):
    def test_in_memory_tail_detour_is_exact_through_renderer(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = ROOT / "recovered_library" / "minicoldfire.py"
        result = trace_filter2_bypass_timing(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        self.assertFalse(result["in_memory_candidate"]["artifact_emitted"])
        self.assertTrue(result["exact_bypass"]["renderer_return_state_identical"])
        self.assertTrue(result["exact_bypass"]["renderer_frame_slab_identical"])
        self.assertGreater(result["exact_bypass"]["nonzero_bytes"]["renderer_frame_slab"], 0)
        self.assertGreater(result["exact_bypass"]["nonzero_bytes"]["fixed_stage"], 0)
        self.assertGreater(result["exact_bypass"]["nonzero_bytes"]["outbound_dma_block"], 0)
        self.assertEqual(
            result["final_mix_initialization"]["physical_voice_record_order"],
            [0, 4, 1, 5, 8, 6, 10, 2],
        )
        self.assertTrue(
            result["stock_final_mix_input_reads"]["all_expected_fixture_reads_matched"]
        )
        self.assertEqual(
            result["instruction_measurement"]["added_semantic_instructions_per_32_frame_block"],
            1,
        )
        self.assertEqual(result["deadline_bound"]["frames_per_block"], 32)
        self.assertEqual(result["deadline_bound"]["blocks_per_second"], 1500.0)
        self.assertIn("OPEN", result["gate_status"]["cycle_safe"])


class MiniColdFirePeripheralTests(unittest.TestCase):
    @staticmethod
    def load_emulator():
        emulator_path = ROOT / "recovered_library" / "minicoldfire.py"
        spec = importlib.util.spec_from_file_location("test_minicoldfire", emulator_path)
        if spec is None or spec.loader is None:
            raise AssertionError("cannot load MiniColdFire")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_pit0_trigger_queues_installed_vector(self):
        module = self.load_emulator()
        bus = module.Bus()
        bus.pit0_pcsr = module.PIT_PCSR_EN | module.PIT_PCSR_PIE
        bus.pit0_pmr = 123
        self.assertTrue(bus.trigger_pit0())
        self.assertEqual(bus.pit0_pcntr, 123)
        self.assertEqual(bus.pending_irqs, [(module.PIT0_VECTOR, module.PIT0_LEVEL, "PIT0")])
        self.assertTrue(bus.trigger_pit0())
        self.assertEqual(len(bus.pending_irqs), 1)

    def test_audio_interface_ready_transition(self):
        module = self.load_emulator()
        bus = module.Bus()
        obj = 0x80007000
        bus.write(module.AUDIO_IFACE_PTRS[0], 4, obj)
        bus.write(obj + module.AUDIO_IFACE_STATUS_OFF, 2, 1)
        self.assertEqual(
            bus.read(obj + module.AUDIO_IFACE_STATUS_OFF, 2),
            1 | module.AUDIO_IFACE_READY,
        )
        self.assertEqual([event["kind"] for event in bus.audio_iface_events], ["COMMAND", "READY"])

    def test_tcd30_polled_bit_transition(self):
        module = self.load_emulator()
        bus = module.Bus()
        bus.write(module.AUDIO_DMA_TCD30_CSR, 2, module.AUDIO_DMA_POLLED_BIT)
        self.assertEqual(bus.read(module.AUDIO_DMA_TCD30_CSR, 2), module.AUDIO_DMA_POLLED_BIT)
        self.assertEqual(bus.read(module.AUDIO_DMA_TCD30_CSR, 2), 0)

    def test_signed_word_multiply(self):
        module = self.load_emulator()
        bus = module.Bus()
        cpu = module.CPU(bus)
        # MULS.W D0,D4 (0xC9C0): -2 * 3 = -6.
        bus.write(module.ENTRY, 2, 0xC9C0)
        cpu.d[0] = 3
        cpu.d[4] = 0x0000FFFE
        self.assertEqual(cpu.step(), "MULS.W")
        self.assertEqual(cpu.d[4], 0xFFFFFFFA)

    def test_emac_long_accumulate_and_extract(self):
        module = self.load_emulator()
        bus = module.Bus()
        cpu = module.CPU(bus)
        # MAC.L D4,D4,ACC0 followed by MOVE.L ACC0,D0.
        bus.write(module.ENTRY, 2, 0xA804)
        bus.write(module.ENTRY + 2, 2, 0x0800)
        bus.write(module.ENTRY + 4, 2, 0xA1C0)
        cpu.macsr = 0x40  # signed integer mode
        cpu.d[4] = 3
        self.assertEqual(cpu.step(), "EMAC")
        self.assertEqual(cpu.macc[0], 9)
        self.assertEqual(cpu.step(), "FROM_MAC ACC0")
        self.assertEqual(cpu.d[0], 9)

    def test_emac_signed_fractional_q31_extract(self):
        module = self.load_emulator()
        bus = module.Bus()
        cpu = module.CPU(bus)
        # MAC.L D0,D1,ACC0 followed by MOVE.L ACC0,D2.
        bus.write(module.ENTRY, 2, 0xA001)
        bus.write(module.ENTRY + 2, 2, 0x0800)
        bus.write(module.ENTRY + 4, 2, 0xA1C2)
        cpu.macsr = 0x20  # signed fractional, truncate
        cpu.d[0] = 0x40000000  # +0.5 in Q1.31
        cpu.d[1] = 0x40000000  # +0.5 in Q1.31
        self.assertEqual(cpu.step(), "EMAC")
        self.assertEqual(cpu.step(), "FROM_MAC ACC0")
        self.assertEqual(cpu.d[2], 0x20000000)  # +0.25 in Q1.31

        cpu.pc = module.ENTRY
        cpu.macc[0] = 0
        cpu.d[0] = 0xC0000000  # -0.5 in Q1.31
        self.assertEqual(cpu.step(), "EMAC")
        self.assertEqual(cpu.step(), "FROM_MAC ACC0")
        self.assertEqual(cpu.d[2], 0xE0000000)  # -0.25 in Q1.31

    def test_emac_with_load_is_not_dual_accumulate(self):
        module = self.load_emulator()
        bus = module.Bus()
        cpu = module.CPU(bus)
        # MAC.W D7.U,D0.L,4(A2),D0,ACC1. The low extension bits encode D7;
        # a load-form MAC is not one of EMAC_B's dual-accumulation opcodes.
        bus.write(module.ENTRY, 2, 0xA02A)
        bus.write(module.ENTRY + 2, 2, 0x0047)
        bus.write(module.ENTRY + 4, 2, 0x0004)
        cpu.macsr = 0x40  # signed integer mode
        cpu.d[0] = 2
        cpu.d[7] = 3 << 16
        cpu.a[2] = module.SRAM_BASE + 0x100
        bus.write(cpu.a[2] + 4, 4, 0x12345678)
        self.assertEqual(cpu.step(), "EMAC")
        self.assertEqual(cpu.macc[1], 6)
        self.assertEqual(cpu.d[0], 0x12345678)
        self.assertEqual(cpu.macc[0], 0)

    def test_audio_interpreter_mixer_load_form_is_not_dual_accumulate(self):
        emulator_path = ROOT / "recovered_library" / "minicoldfire_audio.py"
        spec = importlib.util.spec_from_file_location("test_minicoldfire_audio_emac", emulator_path)
        if spec is None or spec.loader is None:
            self.fail("cannot load audio MiniColdFire")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        bus = module.Bus()
        cpu = module.CPU(bus)
        # This exact load-form MSACL encoding occurs at stock mixer 0x4010A398.
        bus.write(module.ENTRY, 2, 0xAE9B)
        bus.write(module.ENTRY + 2, 2, 0x7901)
        cpu.macsr = 0xA0
        cpu.d[7] = 0x10000000
        cpu.d[1] = 0x80000000
        cpu.a[3] = module.SRAM_BASE + 0x100
        bus.write(cpu.a[3], 4, 0x12345678)
        self.assertEqual(cpu.step(), "EMAC")
        self.assertEqual(cpu.macc[0], 0x1000000000)
        self.assertEqual(cpu.d[7], 0x12345678)
        self.assertEqual(cpu.macc[1], 0)

    def test_emac_fractional_word_subtract_uses_extension_bit(self):
        module = self.load_emulator()
        bus = module.Bus()
        cpu = module.CPU(bus)
        # MSAC.W D3.U,D1.L,ACC0 followed by MOVE.L ACC0,D0.
        bus.write(module.ENTRY, 2, 0xA203)
        bus.write(module.ENTRY + 2, 2, 0x0140)
        bus.write(module.ENTRY + 4, 2, 0xA1C0)
        cpu.macsr = 0x20  # signed fractional, truncate
        cpu.d[3] = 0x80000000  # -1.0 in Q1.31
        cpu.d[1] = 0x00000200  # low-word lane
        self.assertEqual(cpu.step(), "EMAC")
        self.assertEqual(cpu.step(), "FROM_MAC ACC0")
        self.assertEqual(cpu.d[0], 0x02000000)


    def test_ext_register_encoding_overrides_movem(self):
        module = self.load_emulator()
        bus = module.Bus()
        cpu = module.CPU(bus)
        bus.write(module.ENTRY, 2, 0x48C0)  # EXT.L D0
        cpu.d[0] = 0x00008001
        self.assertEqual(cpu.step(), "EXT.L")
        self.assertEqual(cpu.d[0], 0xFFFF8001)

    @unittest.skipUnless(
        (HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin").exists(),
        "extracted proprietary MAIN image not present",
    )
    def test_synthetic_descriptor_runtime_proof(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = ROOT / "recovered_library" / "minicoldfire.py"
        result = probe_runtime_descriptors(main_image, emulator_path, 2_000_000)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["project_capture_status"], "NOT_LOADED_OPTIONAL")
        proof = result["synthetic_descriptor_proof"]
        self.assertEqual(proof["result"], "PASS")
        self.assertEqual(proof["instructions"], 154)
        self.assertEqual(
            [entry["frame_word_index"] for entry in proof["entries"]],
            list(range(39, 47)),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
