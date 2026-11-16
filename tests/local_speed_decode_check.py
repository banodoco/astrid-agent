"""Standard-library unit checks of offline decode, never a product-speed proof."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

from tests import local_speed_native_capture as capture
from tests import _local_speed_native as native


class DecodeChecks(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        (self.root / "trace.etl").write_bytes(b"original ETL fixture")

    def test_default_conversion_is_separate_bounded_work(self):
        with patch.object(capture, "run_bounded", return_value=0) as run:
            result = capture.decode_saved_trace(self.root, tracerpt="fake-tracerpt")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["timeout"], 900)
        self.assertEqual(run.call_args.args[0][0], "fake-tracerpt")
        self.assertEqual(run.call_args.args[0][1], str(self.root / "trace.etl"))
        self.assertTrue(result["decode_completed"])
        self.assertEqual(result["decode_exit"], 0)
        self.assertEqual((self.root / "trace.etl").read_bytes(), b"original ETL fixture")
        self.assertFalse((self.root / "proof.log").exists())

    def test_timeout_is_not_a_successful_decode(self):
        with patch.object(capture, "run_bounded", return_value=124):
            result = capture.decode_saved_trace(self.root, tracerpt="fake-tracerpt")
        self.assertFalse(result["decode_completed"])
        self.assertEqual(result["decode_exit"], 124)
        self.assertEqual(json.loads((self.root / "decode.json").read_text())["decode_exit"], 124)

    def test_other_failure_stays_failure(self):
        with patch.object(capture, "run_bounded", return_value=1):
            result = capture.decode_saved_trace(self.root, tracerpt="fake-tracerpt", timeout=180)
        self.assertFalse(result["decode_completed"])
        self.assertEqual(result["decode_timeout_seconds"], 180)

    def test_invalid_bound_does_not_start_any_process(self):
        with patch.object(capture, "run_bounded") as run:
            for limit in (0, -1, 901, True, 1.5):
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    capture.decode_saved_trace(self.root, tracerpt="fake-tracerpt", timeout=limit)
        run.assert_not_called()

    def test_missing_input_does_not_start_any_process(self):
        (self.root / "trace.etl").unlink()
        with patch.object(capture, "run_bounded") as run, self.assertRaises(FileNotFoundError):
            capture.decode_saved_trace(self.root, tracerpt="fake-tracerpt")
        run.assert_not_called()

    def test_missing_decoder_does_not_start_any_process(self):
        with patch.object(capture.shutil, "which", return_value=None), patch.object(capture, "run_bounded") as run:
            with self.assertRaises(RuntimeError):
                capture.decode_saved_trace(self.root)
        run.assert_not_called()

    def test_command_failure_retains_incomplete_diagnostic(self):
        with patch.object(capture, "run_bounded", side_effect=OSError("decoder unavailable")):
            with self.assertRaises(OSError):
                capture.decode_saved_trace(self.root, tracerpt="fake-tracerpt")
        record = json.loads((self.root / "decode.json").read_text())
        self.assertFalse(record["decode_completed"])
        self.assertIn("decode_finished_epoch_ns", record)


class StreamedXmlChecks(unittest.TestCase):
    def test_completed_events_are_removed_from_their_real_parent(self):
        # Simulate a nested tracerpt document with many fully parsed siblings.
        # A stream retaining empty Event objects indefinitely fails this oracle.
        wrapper = ET.Element("Trace")
        events = ET.SubElement(wrapper, "Events")
        for _ in range(512):
            item = ET.SubElement(events, "Event")
            system = ET.SubElement(item, "System")
            ET.SubElement(system, "Provider", Guid="{68fdd900-4a3e-11d1-84f4-0000f80464e3}")
            ET.SubElement(system, "Opcode").text = "0"

        def parsed(element):
            yield "start", element
            for child in list(element):
                yield from parsed(child)
            yield "end", element

        with patch.object(native.ET, "iterparse", return_value=parsed(wrapper)):
            decoded = list(native.xml_events(Path("synthetic-only.xml")))
        self.assertEqual(len(decoded), 512)
        self.assertTrue(all(item["opcode"] == 0 for item in decoded))
        self.assertEqual(len(events), 0, "completed siblings were retained in memory")


if __name__ == "__main__":
    unittest.main(verbosity=2)
