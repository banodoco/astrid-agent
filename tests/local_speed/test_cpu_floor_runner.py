"""Focused checks for the CPU floor result validator, not performance evidence."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('cpu_floor_runner', Path(__file__).with_name('cpu_floor_runner.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'result.json'
        self.data = {'records': [
            {'sample': i, 'caller_wall_ms': wall, 'execution_observed_ms': 10,
             'bytes': 1024, 'digest': 'sha256:' + 'a' * 64}
            for i, wall in enumerate((40, 60))], 'summary_ms': {}}
    def tearDown(self): self.tmp.cleanup()
    def summarize(self, data=None, integrated=True):
        self.path.write_text(json.dumps(self.data if data is None else data))
        return runner.summarize(self.path, integrated=integrated, samples=2)
    def test_actual_wall_minus_execution_no_queue_subtraction(self):
        result = self.summarize()
        self.assertEqual(result['overhead_observed_ms'], {'median': 40, 'max': 50})
        self.assertFalse(result['queue_wait_subtracted'])
        self.assertFalse(result['product_meter'])
        self.assertFalse(result['gpu_qualification'])
    def test_duplicate_sample_rejected(self):
        self.data['records'][1]['sample'] = 0
        with self.assertRaises(ValueError): self.summarize()
    def test_missing_sample_rejected(self):
        self.data['records'].pop()
        with self.assertRaises(ValueError): self.summarize()
    def test_invalid_durations_rejected(self):
        for value in (-1, float('nan'), float('inf'), '40'):
            with self.subTest(value=value):
                self.data['records'][0]['caller_wall_ms'] = value
                with self.assertRaises(ValueError): self.summarize()
    def test_execution_larger_than_wall_rejected(self):
        self.data['records'][0]['execution_observed_ms'] = 50
        with self.assertRaises(ValueError): self.summarize()
    def test_missing_output_identity_rejected(self):
        self.data['records'][0]['digest'] = ''
        with self.assertRaises(ValueError): self.summarize()
    def test_empty_output_rejected(self):
        self.data['records'][0]['bytes'] = 0
        with self.assertRaises(ValueError): self.summarize()
    def test_transport_result(self):
        data = {'records': [{'sample': i, 'wall_ms': 15, 'execution_ms': 5,
                            'outputs': [{'bytes': 99, 'sha256': 'a' * 64}]}
                           for i in range(2)], 'summary_ms': {}}
        self.assertEqual(self.summarize(data, False)['overhead_observed_ms']['median'], 10)
    def test_transport_requires_outputs(self):
        data = {'records': [{'sample': i, 'wall_ms': 15, 'execution_ms': 5, 'outputs': []}
                           for i in range(2)], 'summary_ms': {}}
        with self.assertRaises(ValueError): self.summarize(data, False)

if __name__ == '__main__': unittest.main()
