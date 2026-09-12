#!/usr/bin/env python3

import hashlib
import io
import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from ar172_extract import NRV2BDepacker, decode_sysex, parse_ele3
from audio_stream_trace import trace as trace_audio_stream
from audio_interface_trace import trace as trace_audio_interface
from audio_handoff_trace import trace as trace_audio_handoff
from audio_contract_compare import TraceError, compare, load_trace, validate_trace
from build_audio_shadow_fixture import (
    DATA as SHADOW_DATA,
    EXIT as SHADOW_EXIT,
    START as SHADOW_START,
    build as build_audio_shadow_fixture,
)
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
from ssi1_clock_input_trace import trace as trace_ssi1_clock_input


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


class QemuAudioEdmaTests(unittest.TestCase):
    def test_audio_contract_probe_and_comparator(self):
        report = json.loads(
            (HERE / "AR172_QEMU_AUDIO_CONTRACT_PROBE_GATE.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            report["status"], "PASS_NATIVE_AUDIO_KERNEL_CONTRACT_CAPTURED"
        )
        self.assertEqual(report["validated_capture"]["memory_events"], 1009)
        self.assertEqual(report["validated_capture"]["loads"], 791)
        self.assertEqual(report["validated_capture"]["stores"], 218)

        plugin = (ROOT / "qemu" / "plugins" / "ar_audio_contract.c").read_text(
            encoding="utf-8"
        )
        self.assertIn("QEMU_PLUGIN_CB_R_REGS", plugin)
        self.assertIn("qemu_plugin_read_register", plugin)
        self.assertIn("qemu_plugin_register_vcpu_mem_cb", plugin)
        self.assertIn("qemu_plugin_mem_get_value", plugin)
        self.assertIn('g_str_has_prefix(argv[i], "out=")', plugin)

        records = [
            {
                "kind": "header", "schema_version": 1,
                "start_pc": "0x401184c4", "end_pc": "0x401187ff",
                "exit_pc": "0x40117fc2", "defaults": True,
            },
            {
                "kind": "boundary", "phase": "entry", "pc": "0x401184c4",
                "registers": {"d0": "0x00000001", "pc": "0x401184c4"},
            },
            {
                "kind": "memory", "sequence": 0, "pc": "0x401184c8",
                "operation": "load", "address": "0x42000000", "size": 4,
                "value": "0x00000002",
            },
            {
                "kind": "boundary", "phase": "exit", "pc": "0x40117fc2",
                "registers": {"d0": "0x00000002", "pc": "0x40117fc2"},
            },
            {
                "kind": "footer", "complete": True, "memory_events": 1,
                "loads": 1, "stores": 0,
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.ndjson"
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in records),
                encoding="utf-8",
            )
            loaded = load_trace(path)
            self.assertTrue(compare(loaded, loaded, "strict")["match"])
            changed = json.loads(json.dumps(loaded))
            changed[2]["value"] = "0x00000003"
            self.assertFalse(compare(loaded, changed, "strict")["match"])
            self.assertTrue(compare(loaded, changed, "topology")["match"])
            incomplete = json.loads(json.dumps(loaded))
            incomplete[-1]["complete"] = False
            with self.assertRaises(TraceError):
                validate_trace(incomplete)

        shadow = json.loads(
            (HERE / "AR172_QEMU_AUDIO_SHADOW_GATE.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(shadow["status"], "PASS_SAME_PROCESS_SHADOW_CONTROL")
        self.assertEqual(
            shadow["non_proprietary_control"]["result"],
            "PASS_IDENTICAL_NATIVE_SHADOW",
        )
        self.assertEqual(shadow["stock_audio_target"]["status"],
                         "READY_PENDING_LOCAL_MAIN_RUN")
        shadow_plugin = (
            ROOT / "qemu" / "plugins" / "ar_audio_shadow.c"
        ).read_text(encoding="utf-8")
        self.assertIn("qemu_plugin_read_memory_vaddr", shadow_plugin)
        self.assertIn("qemu_plugin_write_memory_vaddr", shadow_plugin)
        self.assertIn("qemu_plugin_write_register", shadow_plugin)
        self.assertIn("qemu_plugin_set_pc(start_pc)", shadow_plugin)
        fixture = build_audio_shadow_fixture()
        self.assertEqual(
            fixture[SHADOW_START - 0x40000000 : SHADOW_START - 0x40000000 + 6],
            bytes.fromhex("203940000100"),
        )
        self.assertEqual(fixture[SHADOW_EXIT - 0x40000000 :][:2],
                         bytes.fromhex("60f8"))
        self.assertEqual(fixture[SHADOW_DATA - 0x40000000 :][:4], bytes(4))

    def test_ssi1_clock_gate_report(self):
        report_path = HERE / "AR172_SSI1_CLOCK_INPUT_TRACE.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(
            report["result"],
            "PASS_REQUIRED_SSI1_CLOCK_RECOVERED_BOOT_HANDOFF_OPEN",
        )
        self.assertEqual(report["derived_clocks"]["required_ssi_clock_hz"], 98_304_000)
        self.assertEqual(report["derived_clocks"]["renderer_period_ns"], 666_667)
        self.assertEqual(report["direct_cdrh_reference_count"], 0)
        self.assertEqual(
            report["ccm_constraint"]["pll_source_solution_if_fsys_is_245760000_hz"]["ssi1div"],
            5,
        )
        fixture_dir = HERE / "extracted_stock_nrv"
        main = fixture_dir / "section_3_id_3.decompressed.bin"
        if main.exists():
            sections = sorted(fixture_dir.glob("section_*_id_*.decompressed.bin"))
            live = trace_ssi1_clock_input(main, sections)
            self.assertEqual(live["derived_clocks"], report["derived_clocks"])
            self.assertEqual(live["direct_cdrh_reference_count"], 0)

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

    def test_desktop_audio_service_tracks_held_pad_and_release_tail(self):
        source = (
            ROOT / "qemu" / "hw" / "m68k" / "ar_mk2_intc_pit.c"
        ).read_text(encoding="utf-8")
        self.assertIn("AR_AUDIO_TRIGGER_BLOCKS 8u", source)
        self.assertIn("AR_AUDIO_TRIGGER_DELAY_TICKS 10u", source)
        self.assertIn("ar_panel_audio_observe", source)
        self.assertIn("group == 2 || group == 3", source)
        self.assertIn("c->audio_service_budget--", source)
        self.assertIn("c->audio_pad_held", source)
        self.assertIn("final pad release group=%u", source)
        self.assertIn("completed=%u; bounded %u-block", source)
        self.assertIn("AR_AUDIO_TRIGGER_BLOCKS - in_flight", source)
        self.assertIn("!c->audio_service_budget", source)
        self.assertIn("c->audio_service_delay--", source)
        self.assertIn("c->audio_service_pending", source)
        self.assertIn("c->audio_service_entered", source)
        self.assertIn("c->audio_service_completed", source)
        self.assertIn("AR_SSI1_CLOCK_HZ 98304000LL", source)
        self.assertIn("AR_SSI1_FRAME_BITS (16LL * 32LL)", source)
        self.assertIn("AR_AUDIO_BLOCK_PERIOD_NS", source)
        self.assertIn("ar_audio_service_tick(c, true)", source)
        self.assertIn("AR_EDMA_SSI1_TX_CHANNEL 54u", source)
        self.assertIn("ar_edma_pump_channel(&c->edma, AR_EDMA_SSI1_TX_CHANNEL)", source)
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
        machine = (
            ROOT / "qemu" / "hw" / "m68k" / "elektron_ar_mk2.c"
        ).read_text(encoding="utf-8")
        self.assertIn("AR_MK2_MOCK_PROJECT_SAMPLE_FRAMES", machine)
        self.assertIn("AR_SYNTH_SAMPLE_MAX_FRAMES 48000u", machine)
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


class DesktopPanelInputTests(unittest.TestCase):
    def test_desktop_input_gate(self):
        report = json.loads(
            (HERE / "AR172_QEMU_DESKTOP_INPUT_GATE.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            report["status"], "PASS_QWERTY_MOUSE_AND_FIVE_PAGE_READBACK"
        )
        self.assertTrue(report["runtime_proof"]["visible_firmware_change"])
        self.assertEqual(
            report["runtime_proof"]["validated_probe_frames"],
            ["30 81", "30 40"],
        )

    def test_qwerty_layout_and_knob_clamp(self):
        from qemu.desktop_panel import (
            PAGE_PARAMETER_OFFSETS,
            QWERTY_TRIGS,
            clamp_panel_value,
            decode_page_parameters,
            decode_track_level_state,
            decode_track_levels,
            decode_trig_parameters,
        )

        self.assertEqual(
            [QWERTY_TRIGS[key] for key in "qwertyuiasdfghjk"],
            list(range(1, 17)),
        )
        self.assertEqual(clamp_panel_value(-1), 0)
        self.assertEqual(clamp_panel_value(64), 64)
        self.assertEqual(clamp_panel_value(128), 127)
        state = bytearray(0x54)
        for value, offset in enumerate(PAGE_PARAMETER_OFFSETS["SYN"], start=10):
            state[offset:offset + 2] = (value << 8).to_bytes(2, "big")
        self.assertEqual(
            decode_page_parameters(bytes(state), "SYN"),
            tuple(range(10, 18)),
        )
        self.assertIsNone(decode_page_parameters(bytes(state), "TRIG"))
        self.assertEqual(
            decode_track_levels(bytes.fromhex("6400 6e00") + bytes(22))[:2],
            (100, 110),
        )
        level_state = bytes.fromhex("00000001 6400 6e00") + bytes(22)
        self.assertEqual(decode_track_level_state(level_state)[0], 1)
        self.assertEqual(decode_track_level_state(level_state)[1][:2], (100, 110))
        trig = bytes.fromhex("3c 40 0e 07 80 10 00 02 00 64")
        self.assertEqual(
            decode_trig_parameters(trig),
            (60, 64, 14, 100, 1, 1, 1, 1),
        )

    def test_knob_delta_uses_signed_encoder_frames(self):
        from qemu.panel_event_bridge import PanelLink

        class SocketStub:
            def __init__(self):
                self.frames = []

            def sendall(self, data):
                self.frames.append(data)

        sock = SocketStub()
        link = PanelLink(sock)
        with redirect_stdout(io.StringIO()):
            link.encoder("A", 2)
            link.encoder("I", -1)
        self.assertEqual(
            sock.frames,
            [
                bytes((0x30, 0x02)),
                bytes((0x38, 0xFF)),
            ],
        )
        self.assertFalse(hasattr(link, "set_encoder_value"))

    def test_encoder_delta_gate(self):
        report = json.loads(
            (HERE / "AR172_QEMU_ENCODER_DELTA_GATE.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            report["status"], "PASS_NATIVE_SIGNED_DELTA_SEMANTICS"
        )
        self.assertFalse(
            report["runtime_probe"]["authoritative_parameter_readback"]
        )

    def test_active_encoder_binding_gate(self):
        report = json.loads(
            (HERE / "AR172_QEMU_ACTIVE_ENCODER_BINDING_GATE.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            report["status"],
            "PASS_NATIVE_EVENT_DISPATCH_AND_Q8_READBACK",
        )
        self.assertEqual(
            report["native_consumer_trace"]["active_page_handler"],
            "dynamic adapter 0x400761A6 selects target 0x40038B58, entering shared handler body 0x4003882C",
        )
        self.assertEqual(report["desktop_status"]["native_type_1_delivery"], "PASS")
        self.assertIn(
            "parameter descriptor ID 100",
            report["native_consumer_trace"]["parameter_selection"],
        )
        self.assertIn(
            "acceleration/debounce integrator",
            report["native_consumer_trace"]["value_resolution"],
        )
        self.assertEqual(
            report["pad_pressure_alias_rejected"]["channel_table"],
            "twelve entries 0..11 at 0x4026D4D8",
        )
        self.assertTrue(report["desktop_status"]["authoritative_readback"])

    def test_encoder_binding_readback_gate(self):
        report = json.loads(
            (HERE / "AR172_QEMU_ENCODER_BINDING_READBACK_GATE.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(report["status"], "PASS_FIVE_PAGE_NATIVE_Q8_READBACK")
        self.assertEqual(report["setter_proof"]["live_word"], "0x8000E5C4")
        self.assertEqual(
            report["parameter_ids_by_encoder_A_through_H"]["SYN"],
            [100, 104, 105, 103, 106, 102, 107, 101],
        )
        source = (
            ROOT / "qemu" / "hw" / "m68k" / "elektron_ar_mk2.c"
        ).read_text(encoding="utf-8")
        self.assertIn("AR_PARAMETER_ADDR    0x8000E5B0u", source)
        self.assertIn("AR_MK2_PARAMETER_STATE_OUT", source)

    def test_trig_and_level_readback_gate(self):
        report = json.loads(
            (HERE / "AR172_QEMU_TRIG_LEVEL_READBACK_GATE.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            report["status"],
            "PASS_SIX_PAGE_LEVEL_AND_TRACK_SELECTION_READBACK",
        )
        self.assertEqual(report["level_data"]["live_word"], "0x4123C8E3")
        self.assertEqual(report["trig"]["state_window"], "0x407C4B93..0x407C4B9C")
        self.assertIn("word 2", report["track_selection"]["causal_mutation"])
        source = (
            ROOT / "qemu" / "hw" / "m68k" / "elektron_ar_mk2.c"
        ).read_text(encoding="utf-8")
        self.assertIn("AR_MK2_TRACK_LEVEL_STATE_OUT", source)
        self.assertIn("AR_MK2_TRIG_STATE_OUT", source)
        smoke_source = (ROOT / "qemu" / "headless_ui_smoke.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("--exercise-track-level", smoke_source)
        self.assertIn('changed != [2]', smoke_source)

    def test_turnkey_qwerty_audio_gate(self):
        report = json.loads(
            (HERE / "AR172_QEMU_TURNKEY_QWERTY_AUDIO_GATE.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            report["status"],
            "PASS_NATIVE_DEMO_ASSIGNMENT_AND_QWERTY_HOST_PCM",
        )
        self.assertEqual(
            report["native_assignment"]["frames"], ["33 08"] * 4
        )
        self.assertTrue(report["qwerty_audio"]["nonzero_host_audio"])
        app_default = json.loads(
            (HERE / "AR172_QEMU_APP_AUDIO_DEFAULT_GATE.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            app_default["status"], "PASS_DOUBLE_CLICK_AUDIO_DEFAULT"
        )
        self.assertTrue(app_default["launcher"]["default_audio"])
        self.assertEqual(
            app_default["launcher"]["disable_option"], "--no-audio"
        )
        launcher = (ROOT / "qemu" / "run_desktop_emulator.py").read_text(
            encoding="utf-8"
        )
        panel = (ROOT / "qemu" / "desktop_panel.py").read_text(
            encoding="utf-8"
        )
        smoke = (ROOT / "qemu" / "headless_ui_smoke.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('env["AR_MK2_MOCK_PROJECT_SAMPLE"] = "1"', launcher)
        self.assertIn("action=argparse.BooleanOptionalAction", launcher)
        self.assertIn("default=True", launcher)
        self.assertIn('env.pop("AR_MK2_MOCK_PROJECT_SAMPLE", None)', launcher)
        self.assertIn('text="LOAD TEST"', panel)
        self.assertIn('self.encoder("D", 8)', panel)
        self.assertIn("--exercise-demo-sample", smoke)
        self.assertIn("--exercise-held-audio", smoke)
        self.assertIn("--qemu-plugin", smoke)
        self.assertIn("AR_MK2_AUDIO_BLOCK_OUT", smoke)
        self.assertIn("first_audio_block", smoke)
        self.assertIn("--held-seconds", smoke)
        self.assertIn("--demo-sample-frames", smoke)
        self.assertIn("sample_frames = 4096", smoke)
        self.assertIn("audio service continued after the release tail", smoke)

        held = json.loads(
            (HERE / "AR172_QEMU_HELD_QWERTY_SERVICE_GATE.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            held["status"], "PASS_HELD_KEY_SERVICE_AND_BOUNDED_RELEASE"
        )
        profile = json.loads(
            (HERE / "AR172_QEMU_AUDIO_WINDOW_PROFILE_GATE.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            profile["status"],
            "PASS_NATIVE_AUDIO_ISR_AND_SAMPLE_ROUTINE_PROFILED",
        )
        self.assertEqual(
            profile["exact_vector_191_window"]["completed_services"], 100
        )
        self.assertGreater(
            profile["exact_vector_191_window"]["guest_instructions"],
            25_000_000,
        )
        self.assertLess(
            profile["nested_sample_routine_window"][
                "share_of_exact_isr_percent"
            ],
            1,
        )
        throughput = json.loads(
            (HERE / "AR172_QEMU_QUIET_AUDIO_THROUGHPUT_GATE.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            throughput["status"],
            "PASS_NATIVE_OUTPUT_PRESERVED_NO_MICRO_FUSION_GAIN",
        )
        self.assertGreater(
            throughput["quiet_runtime_acceptance"]["service_rate_hz"], 150
        )
        self.assertGreater(
            throughput["quiet_runtime_acceptance"]["realtime_shortfall_factor"],
            8,
        )
        plugin = (ROOT / "qemu" / "plugins" / "ar_audio_window.c").read_text(
            encoding="utf-8"
        )
        self.assertIn("qemu_plugin_tb_vaddr", plugin)
        self.assertNotIn("qemu_plugin_read_memory", plugin)


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
