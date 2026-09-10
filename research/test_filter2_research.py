#!/usr/bin/env python3

"""Executable regression coverage for the recovered Filter 2/controller chain."""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "controller"))

from audio_callback_probe import probe as probe_audio_callback
from br_hardware_sink_probe import probe as probe_br_hardware_sink
from dspi1_control_link_probe import probe as probe_dspi1_control_link
from filter2_bypass_canary_probe import probe as probe_filter2_bypass_canary
from filter2_coefficient_slew_probe import probe as probe_filter2_coefficient_slew
from filter2_control_publication_probe import probe as probe_filter2_control_publication
from filter2_dispatcher_probe import probe as probe_filter2_dispatcher
from filter2_eight_lane_probe import probe as probe_filter2_eight_lane
from filter2_half_kernel_probe import probe as probe_filter2_half_kernel
from filter2_host_control_ingress_probe import probe as probe_filter2_host_control_ingress
from filter2_lfo2_state_canary_probe import probe as probe_filter2_lfo2_state_canary
from filter2_numeric_contract_probe import probe as probe_filter2_numeric_contract
from filter2_publication_shim_probe import probe as probe_filter2_publication_shim
from filter2_q31_coefficient_probe import probe as probe_filter2_q31_coefficient
from filter2_unity_kernel_probe import probe as probe_filter2_unity_kernel
from machine_pitch_calibration_probe import probe as probe_machine_pitch_calibration
from note_event_constructor_probe import probe as probe_note_event_constructor
from note_pitch_consumer_boundary_probe import probe as probe_note_pitch_consumer_boundary
from note_pitch_publication_probe import probe as probe_note_pitch_publication
from post_voice_ingress_probe import probe as probe_post_voice_ingress
from render_mixer_probe import probe as probe_render_mixer
from renderer_control_ownership_probe import probe as probe_renderer_control_ownership
from whole_callback_control_ownership_probe import probe as probe_whole_callback_control_ownership
from control_setup_ownership_probe import probe as probe_control_setup_ownership
from control_candidate_absolute_reference_probe import (
    probe as probe_control_candidate_absolute_reference,
)
from control_frame_eight_lane_ownership_probe import (
    probe as probe_control_frame_eight_lane_ownership,
)
from control_frame_track_lane_probe import probe as probe_control_frame_track_lane
from control_frame_eight_lane_two_word_locality_probe import (
    probe as probe_control_frame_eight_lane_two_word_locality,
)
from control_frame_static_move_writer_probe import (
    probe as probe_control_frame_static_move_writer,
)
from control_frame_computed_record_writer_probe import (
    probe as probe_control_frame_computed_record_writer,
)
from filter2_lfo2_cutoff_binding_probe import (
    probe as probe_filter2_lfo2_cutoff_binding,
)
from lfo2_control_publication_probe import probe as probe_lfo2_control_publication
from lfo2_note_trigger_reset_probe import probe as probe_lfo2_note_trigger_reset
from lfo2_waveform_mode_probe import probe as probe_lfo2_waveform_mode
from lfo2_extended_waveform_probe import probe as probe_lfo2_extended_waveform
from lfo2_controller_sequence_probe import probe as probe_lfo2_controller_sequence
from sample_br_renderer_probe import probe as probe_sample_br_renderer
from sample_state_probe import probe as probe_sample_state
from synth_pitch_encoding_probe import probe as probe_synth_pitch_encoding
from trigger_queue_probe import probe as probe_trigger_queue
from validate_controller import validate as validate_filter2_controller

STOCK = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
EMULATOR = HERE.parent / "recovered_library" / "minicoldfire_audio.py"


@unittest.skipUnless(
    STOCK.is_file() and EMULATOR.is_file(),
    "proprietary stock MAIN test fixture not present",
)
class StockProbeTest(unittest.TestCase):
    pass


class AudioCallbackProbeTests(StockProbeTest):
    def test_complete_callback_and_br_builder_mapping(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_audio_callback(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        callback = result["callback_execution"]
        self.assertEqual(callback["callback_instructions"], 40512)
        self.assertEqual(callback["audio_dma_initializer_instructions"], 1457)
        self.assertEqual(callback["sample_br_accesses"][0]["address"], "0x8000F7BC")
        self.assertEqual(callback["channel_30_audio_transfer"]["transferred_bytes"], 272)
        self.assertEqual(callback["channel_30_audio_transfer"]["source"], "0x4B7FFFF0")
        self.assertEqual(callback["channel_30_audio_transfer"]["source_modulo_bytes"], 4 * 1024 * 1024)
        self.assertEqual(callback["channels_31_32_external_ingress"]["31"]["bytes_per_major_loop"], 144)
        builder = result["control_frame_builder"]
        self.assertEqual(builder["target_longword_address"], "0x8000E5C4")
        self.assertEqual(builder["sample_br_address"], "0x8000F7BE")


class RenderMixerProbeTests(StockProbeTest):
    def test_three_plane_32_frame_8_lane_geometry(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_render_mixer(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        mixer = result["combiner"]
        self.assertEqual(mixer["instructions_in_inactive_callback"], 3227)
        self.assertEqual(mixer["frames"], 32)
        self.assertEqual(mixer["lanes_written_per_frame"], 8)
        self.assertEqual(mixer["frame_stride_bytes"], 64)


class SampleBrRendererProbeTests(StockProbeTest):
    def test_stock_renderer_encodes_br_in_bounded_case_three(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_sample_br_renderer(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["machine_dispatch"]["machine_0_renderer"], "0x4010CBA8")
        self.assertEqual(result["bounded_gate"]["selected_case"], 3)
        self.assertEqual(result["br_control_path"]["frame_address"], "0x8000F7BE")
        self.assertEqual([v["br_frame_word"] for v in result["vectors"]], ["0x0000", "0x7F00"])
        self.assertNotEqual(
            result["vectors"][0]["packed_voice_control"],
            result["vectors"][1]["packed_voice_control"],
        )
        followup = result["case_one_followup"]["vectors"]
        self.assertEqual([v["final_value"] for v in followup], ["0xF0100000", "0xF0100000"])
        self.assertEqual(followup[0]["region_sha256"], followup[1]["region_sha256"])


class TriggerQueueProbeTests(StockProbeTest):
    def test_authentic_trigger_queue_and_complete_callback_progression(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_trigger_queue(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS_QUEUE_PROGRESS_BR_PATH_RETRACTED")
        contract = result["recovered_runtime_contract"]
        self.assertEqual(contract["runtime_queue"], "0x4192A9D0")
        self.assertEqual(contract["queue_ring"], "0x419531F8")
        self.assertEqual(contract["full_interrupt_stop"], "0x4011CF12")
        expected = [[0], [1], [2], [2], [2], [2], [2], [2], [2], [2], [3], [4]]
        for vector in result["vectors"]:
            self.assertEqual(vector["queued_command"]["code"], 0x1F)
            self.assertEqual(vector["queued_command"]["track_mask"], "0x00000001")
            self.assertEqual([c["cases"] for c in vector["callbacks"]], expected)
            self.assertEqual(vector["callbacks"][0]["event_value_after"], 0)
        high_frames = [
            c["br_frame_at_renderer"][0] for c in result["vectors"][1]["callbacks"]
        ]
        self.assertEqual(high_frames, [0] * 12)
        self.assertEqual(
            result["vectors"][0]["final_packed_control"],
            result["vectors"][1]["final_packed_control"],
        )


class InterpolationStateProbeTests(StockProbeTest):
    def test_flag_bit_five_publishes_interpolation_state(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_sample_state(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        contract = result["trigger_interpolation_contract"]
        self.assertEqual(contract["interpolation_publish_flag"], "0x20")
        self.assertEqual(contract["tested_combined_flags"], "0xA0")
        self.assertEqual(
            contract["interpolation_publish_instructions"], ["0x4011C616", "0x4011C61E"]
        )
        reset_only, interpolation = result["vectors"]
        self.assertEqual(reset_only["watched_reads"], [])
        self.assertEqual(len(interpolation["watched_reads"]), 2)
        self.assertEqual(interpolation["callbacks"][0]["interpolation_phase"], "0x00007080")
        self.assertEqual(interpolation["callbacks"][-1]["interpolation_phase"], "0x00054600")
        self.assertEqual(interpolation["callbacks"][5]["interpolation_coefficient"], "0x000E9044")
        self.assertEqual(interpolation["callbacks"][-1]["interpolation_coefficient"], "0x001D2088")
        self.assertEqual(interpolation["callbacks"][0]["duration_countdown"], "0x000FC7C0")
        self.assertEqual(interpolation["nonzero_source_plane_writes"], [])


class BrHardwareSinkProbeTests(StockProbeTest):
    def test_natural_br_reaches_channel_15_peripheral_fifo(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_br_hardware_sink(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS_DMA_SINK_BR_PATH_RETRACTED")
        zero, high = result["vectors"]
        self.assertEqual(zero["case_3_br"], "0x0000")
        self.assertEqual(high["case_3_br"], "0x0000")
        self.assertEqual(zero["packed_control"], "0x00180FFF")
        self.assertEqual(high["packed_control"], "0x00180FFF")
        self.assertEqual(high["packet"]["wire_high"], "0x8001B018")
        self.assertEqual(high["packet"]["wire_low"], "0x80010FFF")
        self.assertEqual(high["dma"]["channel"], 15)
        self.assertEqual(high["dma"]["destination"], "0xFC03C034")
        self.assertEqual(high["dma"]["transferred_bytes"], 2040)
        self.assertEqual(result["payload_difference_offsets"], ["0x0BA", "0x0BB", "0x0BE", "0x0BF"])


class Dspi1ControlLinkProbeTests(StockProbeTest):
    def test_stock_control_transport_is_framed_dspi1_pcs0(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_dspi1_control_link(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["hardware_identity"]["peripheral"], "MCF5441x DSPI1")
        self.assertEqual(result["stock_configuration"]["ctar"][0]["frame_bits"], 16)
        self.assertEqual(result["stock_configuration"]["ctar"][0]["sck_divider"], 8)
        for vector in result["vectors"]:
            self.assertEqual(vector["dma"]["channel"], 15)
            self.assertEqual(vector["dma"]["destination"], "0xFC03C034")
            self.assertEqual(vector["packet"]["pushr_words"], 510)
            special = vector["packet"]["special_words"]
            self.assertEqual(special[0]["raw"], "0x8000AAAA")
            self.assertEqual(special[2]["raw"], "0x08005555")


class PostVoiceIngressProbeTests(StockProbeTest):
    def test_external_audio_ingress_uses_immutable_stock_tables(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_post_voice_ingress(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        classification = result["corrected_classification"]
        self.assertEqual(classification["external_source"], "0x4F9372E0")
        self.assertEqual(classification["ingress_dma_channels"], [31, 32])
        startup = result["stock_startup_provenance"]
        self.assertEqual([entry["longword_writes"] for entry in startup["tables"]], [256] * 3)
        level = result["explicit_level_precondition"]
        self.assertEqual(level["load_site"], "0x401186CC")
        self.assertEqual(level["physical_voice_records"], [1, 5, 2, 6, 9, 7, 11, 3])
        self.assertEqual(level["seeded_level"], "0x4000")
        zero, active = result["vectors"]
        self.assertEqual(zero["output_nonzero_words"], 0)
        self.assertEqual(active["output_nonzero_words"], 248)
        self.assertEqual(active["output_zero_indices"], list(range(0, 256, 32)))
        for vector in (zero, active):
            self.assertEqual(vector["table_reads"], [520, 512, 512])
            self.assertEqual(vector["table_writes"], [0, 0, 0])
            self.assertEqual(vector["dma_major_loops"], {"31": 8, "32": 8})


class Filter2BypassCanaryProbeTests(StockProbeTest):
    def test_inert_detour_and_next_stock_combiner_are_bit_identical(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_filter2_bypass_canary(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["candidate"]["cave"], "0x402B4340")
        self.assertEqual(result["candidate"]["cave_body"], "4eb940117f004e75")
        for comparison in result["comparisons"]:
            self.assertTrue(all(comparison["bit_identical"].values()))
            self.assertEqual(comparison["stock"]["mixer_instructions"], 3227)
            self.assertEqual(comparison["candidate"]["mixer_output_writes"], 256)


class Filter2NumericContractProbeTests(StockProbeTest):
    def test_fractional_wrap_contract_and_detour_overhead(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_filter2_numeric_contract(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        loop = result["stock_output_loop"]["loop"]
        self.assertEqual(loop["sequential_longword_writes"], 256)
        self.assertFalse(loop["saturation_enable_bit_seen"])
        self.assertEqual(
            [case["stored_word"] for case in result["controlled_boundary_cases"]],
            ["0x7FFFFFFF", "0x80000000", "0x80000000", "0x7FFFFFFF"],
        )
        for row in result["inert_detour_overhead"]["measurement"]:
            self.assertEqual(row["added_semantic_instructions"], 2)


class Filter2Lfo2StateCanaryProbeTests(StockProbeTest):
    def test_disabled_state_reservation_is_inert(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_filter2_lfo2_state_canary(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["candidate"]["state_bytes_reserved"], 496)
        self.assertEqual(result["layout"]["flags"], 0)
        self.assertEqual(result["stock_boot_liveness"]["reads"], 0)
        self.assertEqual(result["stock_boot_liveness"]["writes"], 0)
        for comparison in result["stock_equivalence"]:
            self.assertTrue(all(comparison["bit_identical"].values()))


class Filter2DispatcherProbeTests(StockProbeTest):
    def test_disabled_and_armed_placeholder_dispatch(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_filter2_dispatcher(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        for trace in result["dispatch_traces"]["disabled"]:
            self.assertFalse(trace["enabled_placeholder_executed"])
            self.assertEqual(trace["instructions_to_mixer"], 35366)
        for trace in result["dispatch_traces"]["armed_probe"]:
            self.assertTrue(trace["enabled_placeholder_executed"])
            self.assertEqual(trace["instructions_to_mixer"], 35369)
        for comparison in result["stock_equivalence"]:
            self.assertTrue(all(comparison["disabled_equals_stock"].values()))
            self.assertTrue(all(comparison["armed_placeholder_equals_stock"].values()))


class Filter2UnityKernelProbeTests(StockProbeTest):
    def test_two_stage_unity_kernel_is_bit_identical(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_filter2_unity_kernel(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        for trace in result["execution_traces"]["armed_unity"]:
            self.assertEqual(trace["loop_iterations"], 32)
            self.assertEqual(trace["stage1_sats_executions"], 32)
            self.assertEqual(trace["stage2_sats_executions"], 32)
            self.assertTrue(trace["lane0_bit_identical"])
        for comparison in result["stock_equivalence"]:
            self.assertTrue(all(comparison["disabled_equals_stock"].values()))
            self.assertTrue(all(comparison["armed_unity_equals_stock"].values()))


class Filter2HalfKernelProbeTests(StockProbeTest):
    def test_nonunity_half_step_kernel_matches_oracle(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_filter2_half_kernel(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        self.assertTrue(all(vector["oracle_match"] for vector in result["direct_oracle_vectors"].values()))
        active = result["callback_traces"]["armed_half"][1]
        self.assertEqual(active["loop_iterations"], 32)
        self.assertEqual(active["sats_executions"], 128)
        self.assertTrue(active["oracle_match"])
        self.assertTrue(active["output_differs_from_input"])
        for comparison in result["stock_comparisons"]:
            self.assertTrue(all(comparison["disabled_equals_stock"].values()))


class Filter2Q31CoefficientProbeTests(StockProbeTest):
    def test_state_loaded_coefficient_and_callback_continuity(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_filter2_q31_coefficient(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        for vector in result["coefficient_vectors"]:
            self.assertEqual(vector["multiply_calls"], 64)
            self.assertTrue(vector["oracle_match"])
        continuity = result["consecutive_callback_entries_through_mixer"]
        self.assertEqual([entry["input"] for entry in continuity], ["active", "zero", "active"])
        self.assertTrue(all(entry["oracle_match"] for entry in continuity))
        self.assertEqual(continuity[1]["starting_state"], continuity[0]["ending_state"])
        self.assertEqual(continuity[2]["starting_state"], continuity[1]["ending_state"])
        for comparison in result["stock_comparisons"]:
            self.assertTrue(all(comparison["disabled_equals_stock"].values()))


class Filter2PublicationShimProbeTests(StockProbeTest):
    def test_stock_passthrough_and_virtual_q31_publication(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_filter2_publication_shim(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        for vector in result["ordinary_stock_passthrough"]:
            self.assertTrue(vector["stock_write_identical"])
            self.assertTrue(vector["disabled_return_state_identical"])
        publications = result["virtual_filter2_publications"]
        self.assertEqual([entry["lane"] for entry in publications], list(range(8)))
        self.assertEqual([entry["write_size_bytes"] for entry in publications], [4] * 8)
        self.assertTrue(all(entry["single_aligned_store"] for entry in publications))
        integration = result["publication_to_filter_integration"]
        self.assertEqual(integration["multiply_calls"], 512)
        self.assertTrue(integration["all_oracles_match"])
        for comparison in result["disabled_callback_stock_equivalence"]:
            self.assertTrue(all(comparison["bit_identical"].values()))


class Filter2CoefficientSlewProbeTests(StockProbeTest):
    def test_control_mapping_and_per_sample_slew(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_filter2_coefficient_slew(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        for ramp in result["direct_ramps"]:
            self.assertEqual(ramp["multiply_calls"], 64)
            self.assertTrue(ramp["monotonic"])
            self.assertTrue(ramp["full_target_jump_avoided_at_first_sample"])
            self.assertTrue(ramp["oracle_match"])
        callbacks = result["consecutive_callback_ramps"]
        self.assertEqual([entry["control_target"] for entry in callbacks], [16, 112, 40, 96, 0])
        self.assertTrue(all(entry["oracle_match"] for entry in callbacks))
        for comparison in result["disabled_stock_equivalence"]:
            self.assertTrue(all(comparison["bit_identical"].values()))


class Filter2ControlPublicationProbeTests(StockProbeTest):
    def test_foreground_setter_is_unique_and_absent_from_audio_callback(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_filter2_control_publication(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        boundary = result["publication_boundary"]
        self.assertEqual(boundary["selected_interception_callsite"], "0x400B5936")
        self.assertEqual(boundary["absolute_setter_callsites"], ["0x400B5936"])
        for transaction in result["synthetic_setter_transactions"]:
            self.assertEqual(transaction["write_size_bytes"], 2)
            self.assertEqual(transaction["setter_instructions"], 5)
            self.assertEqual(transaction["getter_instructions"], 4)
            self.assertTrue(transaction["round_trip_match"])
        ownership = result["audio_callback_ownership"]
        self.assertEqual(ownership["target_array_reads"], 299)
        self.assertEqual(ownership["target_array_writes"], 0)
        self.assertEqual(ownership["word_setter_visits"], 0)
        self.assertEqual(ownership["foreground_callsite_visits"], 0)


class Filter2EightLaneProbeTests(StockProbeTest):
    def test_all_lanes_isolation_and_modeled_cost(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_filter2_eight_lane(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(len(result["one_hot_isolation"]), 8)
        for lane, vector in enumerate(result["one_hot_isolation"]):
            self.assertEqual(vector["selected_lanes"], [lane])
            self.assertEqual(vector["multiply_calls"], 64)
            self.assertEqual(vector["plane_writes"], 32)
            self.assertEqual(vector["state_writes"], 65)
            self.assertTrue(vector["oracle_match"])
            for observed in vector["lanes"]:
                self.assertEqual(observed["audio_changed"], observed["lane"] == lane)
                self.assertEqual(observed["state_changed"], observed["lane"] == lane)
        full = result["full_mask"]
        self.assertEqual(full["selected_lanes"], list(range(8)))
        self.assertEqual(full["multiply_calls"], 512)
        self.assertEqual(full["plane_writes"], 256)
        self.assertEqual(full["state_writes"], 520)
        self.assertTrue(full["oracle_match"])
        self.assertEqual(result["modeled_cost"]["semantic_instruction_delta"], 8605)
        for comparison in result["disabled_stock_equivalence"]:
            self.assertTrue(all(comparison["bit_identical"].values()))


class Filter2HostControlIngressProbeTests(StockProbeTest):
    def test_mouse_and_qwerty_share_virtual_setter_abi(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_filter2_host_control_ingress(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(
            result["storage_free_uart_audit"]["result"],
            "NOT_ACTIVE_IN_STORAGE_FREE_BOOT",
        )
        bridge = result["host_bridge_execution"]
        self.assertEqual(bridge["commands_executed"], 11)
        self.assertEqual(bridge["final_controls"], [1, 16, 32, 40, 64, 80, 96, 127])
        self.assertTrue(bridge["all_commands_single_aligned_store"])
        self.assertEqual(bridge["result"], "PASS")


class Filter2ControllerBuildTests(StockProbeTest):
    def test_eight_knob_service_and_distinct_note_path(self):
        result = validate_filter2_controller()
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(len(result["publications"]), 8)
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(result["note_events"][0]["firmware_transport"], "emulated_stock_trigger")
        self.assertEqual(result["note_events"][1]["firmware_transport"], "emulated_stock_release")


class NoteEventConstructorProbeTests(StockProbeTest):
    def test_stock_note_on_off_constructor_and_release_progression(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_note_event_constructor(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["note_on"]["constructed"]["record_state"], 1)
        self.assertEqual(result["note_off"]["constructed"]["record_state"], 2)
        self.assertEqual(
            [item["cases"] for item in result["note_off"]["release_callbacks"]],
            [[1]] + [[2]] * 8 + [[3], [4], [4]],
        )
        self.assertEqual(result["repeated_release"]["record_state"], 0)


class NotePitchPublicationProbeTests(StockProbeTest):
    def test_note_shift_and_stock_track_zero_trigger(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_note_pitch_publication(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual([vector["note"] for vector in result["vectors"]], [48, 60, 72])
        self.assertEqual(
            [vector["encoded_pitch"] for vector in result["vectors"]],
            ["0x00300000", "0x003C0000", "0x00480000"],
        )
        for vector in result["vectors"]:
            self.assertEqual(vector["encoded_pitch"], vector["live_pitch_after"])
            self.assertEqual(vector["callback_cases"], [0])
            self.assertTrue(vector["one_shot_cleared"])
            self.assertEqual(vector["queued_command"]["code"], 0x1F)
            self.assertEqual(vector["queued_command"]["track_mask"], "0x00000001")


class NotePitchConsumerBoundaryProbeTests(StockProbeTest):
    def test_gated_pitch_reaches_renderer_and_dspi1_packet(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_note_pitch_consumer_boundary(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["pitch_select_path"]["track_source_byte"], "0x412FACA1")
        self.assertEqual(result["pitch_select_path"]["live_gate"], "0x8000EA18")
        self.assertEqual(result["pitch_select_path"]["read_pc"], "0x4011CA7E")
        self.assertEqual(result["semantic_identity"]["field"], "sound chromatic mode")
        self.assertEqual(
            [item["synth_uses_live_note"] for item in result["chromatic_mode_matrix"]],
            [False, True, False, True],
        )
        self.assertEqual(result["dspi1_serialization"]["renderer_control_halfwords"], ["0x8000641A", "0x8000641C"])
        self.assertEqual(result["dspi1_serialization"]["dspi1_word_indices"], [46, 47])
        self.assertEqual([vector["note"] for vector in result["live_note_vectors"]], [48, 60, 72])
        self.assertEqual(
            [set(vector["renderer_pitch_arguments"]) for vector in result["live_note_vectors"]],
            [{"0x00300000"}, {"0x003C0000"}, {"0x00480000"}],
        )
        self.assertEqual(
            [item["differing_dspi1_word_indices"] for item in result["comparisons"]],
            [[46, 47], [46, 47]],
        )


class SynthPitchEncodingProbeTests(StockProbeTest):
    def test_full_note_sweep_and_octave_law(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_synth_pitch_encoding(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        self.assertTrue(result["stock_math"]["monotonic_notes_0_127"])
        self.assertTrue(result["calibration"]["all_128_vectors_match"])
        self.assertEqual(result["calibration"]["channel_scales"], ["0x00062000", "0x0004168F"])
        self.assertEqual(result["serialization"]["dspi1_word_indices"], [46, 47])
        self.assertEqual(
            [item["exp2_output"] for item in result["anchors"]],
            ["0x0001965F", "0x00032CBF", "0x0006597F"],
        )
        self.assertEqual(len(result["note_vectors"]), 128)


class MachinePitchCalibrationProbeTests(StockProbeTest):
    def test_renderer_pair_scope_and_shared_packet_topology(self):
        main_image = HERE / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
        emulator_path = EMULATOR
        result = probe_machine_pitch_calibration(main_image, emulator_path)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["dispatch"]["entries"], 53)
        self.assertEqual(result["machine_metadata"]["public_machine_count"], 34)
        self.assertEqual(result["machine_metadata"]["internal_or_reserved_id_range"], [34, 52])
        self.assertEqual(result["machine_metadata"]["names"][33]["machine_name"], "hh lab")
        self.assertFalse(result["identity"]["universal_pair"])
        self.assertEqual(result["static_classification"]["exact_two_scale_machine_ids"], [0, 1])
        self.assertEqual(result["static_classification"]["scale_machine_ids"]["0x00062000"], [0, 1])
        self.assertEqual(
            result["static_classification"]["scale_machine_ids"]["0x0004168F"],
            [0, 1, 13, 21, 22, 26, 30, 35, 36, 41],
        )
        anchors = {row["machine_id"]: row["vectors"] for row in result["runtime_anchors"]}
        self.assertEqual(anchors[0], anchors[1])
        self.assertNotEqual(anchors[0], anchors[2])
        self.assertEqual(anchors[33][0]["word_46"], "0x0000")


class RendererControlOwnershipProbeTests(StockProbeTest):
    def test_all_public_machine_and_state_renderer_owned_dspi1_fields(self):
        result = probe_renderer_control_ownership(STOCK, EMULATOR)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["coverage"]["public_machines"], 34)
        self.assertEqual(result["coverage"]["contexts"], 170)
        self.assertEqual(result["packet_source"]["halfwords"], 492)
        summary = result["ownership_summary"]
        self.assertEqual(summary["observed_owned_field_count"], 117)
        self.assertEqual(summary["universal_field_count"], 0)
        self.assertEqual(summary["unobserved_field_count"], 375)
        ownership = {row["dspi1_word_index"]: row for row in summary["fields"]}
        self.assertEqual(ownership[46]["context_count"], 93)
        self.assertEqual(ownership[47]["context_count"], 93)
        self.assertEqual(ownership[58]["machine_ids"], [5])


class WholeCallbackControlOwnershipProbeTests(StockProbeTest):
    def test_all_public_machine_and_state_callback_writers_and_packetizer_reads(self):
        result = probe_whole_callback_control_ownership(STOCK, EMULATOR)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["coverage"]["contexts"], 170)
        summary = result["summary"]
        self.assertEqual(summary["renderer_written_field_count"], 117)
        self.assertEqual(summary["non_renderer_written_field_count"], 233)
        self.assertEqual(summary["all_callback_written_field_count"], 327)
        self.assertEqual(summary["packetizer_read_field_count"], 492)
        self.assertEqual(summary["read_but_never_callback_written_field_count"], 165)
        consumer = result["packetizer_consumer"]
        self.assertEqual(consumer["entry"], "0x40077D14")
        self.assertEqual(consumer["consumed_field_count"], 492)
        self.assertEqual(
            consumer["read_pcs"],
            ["0x40077D62", "0x40077D64", "0x40077D66", "0x40077D68"],
        )


class ControlSetupOwnershipProbeTests(StockProbeTest):
    def test_initializers_and_note_constructors_do_not_write_packet_source(self):
        report = HERE / "AR172_WHOLE_CALLBACK_CONTROL_OWNERSHIP_PROBE.json"
        result = probe_control_setup_ownership(STOCK, EMULATOR, report)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["setup"]["write_event_count"], 0)
        self.assertEqual(result["setup"]["written_field_count"], 0)
        self.assertEqual(
            [phase["phase"] for phase in result["setup"]["phases"]],
            [
                "machine_preparation",
                "control_dma_initializer",
                "queue_initializer",
                "queue_installer",
                "note_on_constructor",
                "note_off_constructor",
            ],
        )
        rejection = result["candidate_rejection"]
        self.assertEqual(rejection["callback_unwritten_field_count"], 165)
        self.assertEqual(rejection["setup_written_callback_candidate_count"], 0)
        self.assertEqual(rejection["remaining_unobserved_field_count"], 165)


class ControlCandidateAbsoluteReferenceProbeTests(StockProbeTest):
    def test_callback_unwritten_field_literal_inventory(self):
        report = HERE / "AR172_WHOLE_CALLBACK_CONTROL_OWNERSHIP_PROBE.json"
        result = probe_control_candidate_absolute_reference(STOCK, report)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["source_candidates"], 165)
        summary = result["absolute_reference_summary"]
        self.assertEqual(summary["referenced_field_count"], 77)
        self.assertEqual(summary["unreferenced_field_count"], 88)
        self.assertEqual(summary["literal_reference_count"], 255)
        self.assertEqual(summary["cluster_count"], 24)


class ControlFrameTrackLaneProbeTests(StockProbeTest):
    def test_track_zero_candidate_is_owned_by_logical_track_one(self):
        result = probe_control_frame_track_lane(STOCK, EMULATOR)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["candidate"]["words"], [197, 198])
        self.assertTrue(result["candidate"]["rejected_for_universal_filter2_transport"])
        self.assertEqual(result["coverage"]["logical_tracks"], [0, 1, 2])


class ControlFrameEightLaneOwnershipProbeTests(StockProbeTest):
    def test_all_physical_voice_logical_track_mappings(self):
        result = probe_control_frame_eight_lane_ownership(STOCK, EMULATOR, jobs=4)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["coverage"]["contexts"], 1360)
        self.assertEqual(
            result["coverage"]["physical_voice_to_logical_track"],
            [0, 4, 1, 5, 8, 6, 10, 2],
        )
        ownership = result["ownership"]
        self.assertEqual(ownership["renderer_field_count"], 164)
        self.assertEqual(ownership["nonrenderer_field_count"], 233)
        self.assertEqual(ownership["any_callback_writer_field_count"], 368)
        self.assertEqual(ownership["writer_free_field_count"], 124)
        self.assertEqual(result["ranking"]["adjacent_writer_free_pairs"], 58)
        self.assertEqual(result["ranking"]["fully_read_isolated_pairs"], 58)
        self.assertEqual(result["ranking"]["winner"]["words"], [67, 68])


class ControlFrameEightLaneTwoWordLocalityProbeTests(StockProbeTest):
    def test_words_67_68_have_exact_packet_locality_across_all_lanes(self):
        result = probe_control_frame_eight_lane_two_word_locality(
            STOCK, EMULATOR, jobs=4,
        )
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["coverage"]["contexts"], 1360)
        self.assertEqual(result["coverage"]["baseline_callbacks"], 1360)
        self.assertEqual(result["coverage"]["seeded_callbacks"], 1360)
        self.assertEqual(result["candidate"]["words"], [67, 68])
        self.assertTrue(result["candidate"]["exact_packet_locality_in_every_context"])


class ControlFrameStaticMoveWriterProbeTests(StockProbeTest):
    def test_direct_absolute_word_writers_reject_locality_winner(self):
        report = HERE / "AR172_CONTROL_FRAME_EIGHT_LANE_OWNERSHIP_PROBE.json"
        result = probe_control_frame_static_move_writer(STOCK, report)
        self.assertEqual(result["result"], "PASS")
        writers = result["direct_absolute_word_writers"]
        self.assertEqual(writers["rejected_field_count"], 48)
        self.assertEqual(writers["writer_instruction_count"], 123)
        rejected = {row["word"] for row in writers["fields"]}
        self.assertTrue({67, 68}.issubset(rejected))
        self.assertEqual(result["remaining"]["field_count"], 76)
        self.assertEqual(result["remaining"]["adjacent_pair_count"], 13)
        self.assertEqual(result["remaining"]["ranked_winner"]["words"], [281, 282])


class ControlFrameComputedRecordWriterProbeTests(StockProbeTest):
    def test_computed_record_array_exhausts_adjacent_pairs(self):
        report = HERE / "AR172_CONTROL_FRAME_STATIC_MOVE_WRITER_PROBE.json"
        result = probe_control_frame_computed_record_writer(STOCK, report)
        self.assertEqual(result["result"], "PASS")
        writer = result["computed_record_writer"]
        self.assertEqual(writer["record_count"], 56)
        self.assertEqual(writer["candidate_intersecting_record_count"], 15)
        self.assertEqual(writer["candidate_fields_quarantined"], 37)
        quarantined = {
            word
            for row in writer["candidate_ranges_quarantined"]
            for word in range(row["first_word"], row["last_word"] + 1)
        }
        self.assertTrue({281, 282}.issubset(quarantined))
        slots = result["paired_slot_initializer"]
        self.assertEqual(slots["slot_count"], 80)
        self.assertEqual(slots["candidate_companion_fields_quarantined"], 39)
        self.assertEqual(result["remaining"]["field_count"], 0)
        self.assertEqual(result["remaining"]["adjacent_pair_count"], 0)


class Lfo2CpuIntegrationProbeTests(StockProbeTest):
    def test_cutoff_binding_all_eight_lanes(self):
        result = probe_filter2_lfo2_cutoff_binding(STOCK, EMULATOR)
        self.assertEqual(result["result"], "PASS")
        integration = result["lfo2_filter2_integration"]
        self.assertEqual(len(integration["controls"]), 8)
        self.assertTrue(integration["base_targets_immutable"])
        self.assertTrue(integration["all_oracles_match"])

    def test_foreground_publication_and_random_reset(self):
        result = probe_lfo2_control_publication(STOCK, EMULATOR)
        self.assertEqual(result["result"], "PASS")
        vectors = result["publication_vectors"]
        self.assertEqual(vectors["reset_lane5_random_index"], 0)
        self.assertTrue(result["published_control_to_audio"]["oracle_match"])

    def test_note_trigger_reset_and_four_base_waveforms(self):
        note = probe_lfo2_note_trigger_reset(STOCK, EMULATOR)
        self.assertEqual(note["result"], "PASS")
        self.assertEqual(len(note["note_on_mode_matrix"]), 8)
        for row in note["note_on_mode_matrix"]:
            expected = 0 if row["mode"] == "trigger" else 0x60 + row["track"]
            self.assertEqual(row["random_index_after"], expected)
        wave = probe_lfo2_waveform_mode(STOCK, EMULATOR)
        self.assertEqual(wave["result"], "PASS")
        self.assertTrue(wave["waveform_mode_matrix"]["all_oracles_match"])

    def test_all_seven_waveforms_and_disabled_identity(self):
        result = probe_lfo2_extended_waveform(STOCK, EMULATOR)
        self.assertEqual(result["result"], "PASS")
        matrix = result["waveform_matrix"]
        self.assertEqual(len(matrix["lanes"]), 7)
        self.assertTrue(matrix["all_seven_waveforms_match"])
        reset = result["nonlinear_reset_matrix"]
        self.assertEqual(len(reset["uninterrupted_callbacks"]), 6)
        self.assertEqual(len(reset["explicit_reset_prefix"]), 4)
        self.assertTrue(reset["random_wraps_exercised"])
        self.assertTrue(reset["explicit_and_note_prefixes_identical"])
        self.assertTrue(all(
            all(row["bit_identical"].values())
            for row in result["disabled_callback_stock_equivalence"]
        ))

    def test_controller_sequence_and_terminal_modes(self):
        result = probe_lfo2_controller_sequence(STOCK, EMULATOR)
        self.assertEqual(result["result"], "PASS")
        terminal = result["terminal_behavior"]
        self.assertEqual(terminal["one_shot_first_terminal_callback"], 14)
        self.assertEqual(terminal["half_shot_first_terminal_callback"], 9)
        self.assertEqual(terminal["hold_callbacks"], [4, 5])
        self.assertTrue(terminal["phase_modulation_and_target_stable_after_terminal"])
        self.assertTrue(terminal["every_audio_block_matches_filter_oracle"])
        transitions = result["active_transitions"]
        self.assertEqual(transitions["disabled_callbacks"], [3, 4, 5])
        self.assertTrue(transitions["disabled_phase_frozen"])
        self.assertTrue(transitions["reset_clears_phase_modulation_and_random_index"])
        self.assertTrue(transitions["all_transition_audio_blocks_match_oracle"])




if __name__ == "__main__":
    unittest.main()
