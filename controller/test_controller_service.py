#!/usr/bin/env python3

import json
import sys
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parent
STOCK = ROOT / "research" / "extracted_stock_nrv" / "section_3_id_3.decompressed.bin"
EMULATOR = ROOT / "recovered_library" / "minicoldfire_audio.py"

from filter2_controller_service import (  # noqa: E402
    ControllerState,
    EmulatorBridge,
    make_handler,
)


@unittest.skipUnless(STOCK.is_file(), "proprietary stock MAIN test fixture not present")
class RealBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = EmulatorBridge(STOCK, EMULATOR)

    @classmethod
    def tearDownClass(cls):
        cls.bridge.close()

    def test_all_eight_knobs_publish_one_aligned_store(self):
        for lane, value in enumerate((0, 16, 32, 48, 64, 80, 96, 127)):
            result = self.bridge.publish(lane, value)
            self.assertEqual(result["lane"], lane)
            self.assertEqual(result["value"], value)
            self.assertEqual(result["virtual_index"], f"0x{0x7FF8 + lane:04X}")
            self.assertTrue(result["single_aligned_store"])

    def test_lfo2_waveform_and_mode_publish_for_all_lanes(self):
        for lane in range(8):
            wave = self.bridge.publish_lfo2(lane, "waveform", lane % 7)
            mode = self.bridge.publish_lfo2(lane, "mode", lane % 4)
            self.assertEqual(wave["virtual_index"], f"0x{0x7FC0 + lane:04X}")
            self.assertEqual(mode["virtual_index"], f"0x{0x7FC8 + lane:04X}")


@unittest.skipUnless(STOCK.is_file(), "proprietary stock MAIN test fixture not present")
class ServiceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = EmulatorBridge(STOCK, EMULATOR)
        cls.state = ControllerState(cls.bridge)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.state))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.bridge.close()

    def request(self, method, path, payload=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        body = json.dumps(payload) if payload is not None else None
        headers = {"Content-Type": "application/json"} if body else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, response.getheader("Content-Type"), data

    def test_static_controller_and_state(self):
        status, content_type, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("text/html"))
        self.assertIn(b"RYTM II CONTROLLER", body)
        status, _, body = self.request("GET", "/api/state")
        snapshot = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["filter2"]["values"], self.state.values)
        self.assertEqual(len(snapshot["filter2"]["values"]), 8)
        self.assertEqual(len(snapshot["lfo2"]["waveform"]), 8)
        self.assertEqual(len(snapshot["lfo2"]["mode"]), 8)
        self.assertTrue(all(0 <= value <= 6 for value in snapshot["lfo2"]["waveform"]))
        self.assertTrue(all(0 <= value <= 3 for value in snapshot["lfo2"]["mode"]))
        self.assertTrue(snapshot["emulator"]["runtime_armed_only"])
        self.assertFalse(snapshot["emulator"]["flashable_image_created"])

    def test_filter_and_note_paths_remain_distinct(self):
        status, _, body = self.request("POST", "/api/filter2", {"lane": 3, "value": 91, "source": "mouse"})
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result["event"]["type"], "filter2")
        self.assertEqual(result["event"]["q31"], "0x5BB76EDD")
        self.assertEqual(result["state"]["filter2"]["values"][3], 91)

        status, _, body = self.request(
            "POST", "/api/lfo2",
            {"lane": 3, "parameter": "waveform", "value": "sine", "source": "mouse"},
        )
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result["event"]["type"], "lfo2")
        self.assertEqual(result["event"]["value"], 4)
        self.assertEqual(result["event"]["virtual_index"], "0x7FC3")
        self.assertEqual(result["state"]["lfo2"]["waveform"][3], 4)

        status, _, body = self.request(
            "POST", "/api/lfo2", {"lane": 3, "parameter": "mode", "value": "half-shot"},
        )
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result["event"]["value"], 2)
        self.assertEqual(result["event"]["virtual_index"], "0x7FCB")

        status, _, body = self.request("POST", "/api/note", {"key": "a", "action": "on", "velocity": 100})
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result["event"]["type"], "note")
        self.assertEqual(result["event"]["note"], 48)
        self.assertEqual(result["event"]["firmware_transport"], "emulated_stock_trigger")
        self.assertEqual(result["event"]["stock_trigger"]["encoded_pitch"], "0x00300000")
        self.assertEqual(result["event"]["stock_trigger"]["live_pitch"], "0x00300000")
        self.assertEqual(result["event"]["stock_trigger"]["pitch_consumer"]["source_gate"], "0x01")
        self.assertEqual(result["event"]["stock_trigger"]["pitch_consumer"]["chromatic_mode"], "synth")
        self.assertEqual(result["event"]["stock_trigger"]["pitch_consumer"]["live_gate"], "0x01")
        self.assertTrue(result["event"]["stock_trigger"]["pitch_consumer"]["renderer_input_proven"])
        self.assertEqual(
            result["event"]["stock_trigger"]["pitch_consumer"]["reads"],
            [{"pc": "0x4011CA7E", "value": "0x00300000"}],
        )
        self.assertEqual(result["event"]["stock_trigger"]["queued_command"]["code"], 0x1F)
        self.assertIn(48, result["state"]["notes"]["held"])
        self.assertEqual(result["state"]["filter2"]["values"][3], 91)

        status, _, body = self.request("POST", "/api/note", {"key": "a", "action": "off"})
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result["event"]["firmware_transport"], "emulated_stock_release")
        self.assertTrue(result["event"]["stock_release"]["accepted"])
        self.assertEqual(result["event"]["stock_release"]["constructor_instructions"], 45)
        self.assertEqual(result["event"]["stock_release"]["held_source_mask_after"], "0x00000000")
        self.assertEqual(result["event"]["stock_release"]["callback_cases"], [1])
        self.assertNotIn(48, result["state"]["notes"]["held"])

    def test_input_validation(self):
        status, _, body = self.request("POST", "/api/filter2", {"lane": 8, "value": 64})
        self.assertEqual(status, 400)
        self.assertIn("lane", json.loads(body)["error"])
        status, _, body = self.request(
            "POST", "/api/lfo2", {"lane": 0, "parameter": "waveform", "value": "noise"},
        )
        self.assertEqual(status, 400)
        self.assertIn("waveform", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
