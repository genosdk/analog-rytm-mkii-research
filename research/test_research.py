#!/usr/bin/env python3

import hashlib
import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from ar172_extract import NRV2BDepacker, decode_sysex, parse_ele3
from audio_stream_trace import trace as trace_audio_stream
from audio_interface_trace import trace as trace_audio_interface
from br_bridge_trace import trace
from br_consumer_trace import trace as trace_br_consumer
from control_frame_trace import trace as trace_control_frame
from lfo2_filter2_reference import run_tests
from runtime_descriptor_probe import probe as probe_runtime_descriptors


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
