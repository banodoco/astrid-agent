"""Focused checks for redacted diagnostic output."""
import base64
import importlib.util
import json
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('capture_systemd', Path(__file__).with_name('capture-systemd.py'))
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class CaptureTest(unittest.TestCase):
    def test_missing_keys_and_exact_safe_fragments(self):
        raw = b'Id=astrid.service\r\nEnvironment=PASSWORD=hunter2\nExecStart=command\n secret=hide\n'
        result = capture.describe_stdout(raw, ('Id', 'Environment', 'ExecStart', 'Job'))
        self.assertEqual(result['missing_keys'], ['Job'])
        self.assertEqual(result['execstart_continuation_lines'], 1)
        self.assertEqual(base64.b64decode(result['recognized_line_fragments'][0]['prefix_base64']), b'Id=')
        self.assertEqual(base64.b64decode(result['recognized_line_fragments'][0]['line_ending_base64']), b'\r\n')
        self.assertNotIn('hunter2', json.dumps(result))
        self.assertNotIn('hide', json.dumps(result))

    def test_duplicate_unparseable_and_invalid_utf8(self):
        result = capture.describe_stdout(b'Id=a\nId=b\nunknown=secret\n', ('Id', 'Job'))
        self.assertEqual(result['duplicate_keys'], ['Id'])
        self.assertEqual(result['unparseable_lines'], 1)
        self.assertNotIn('secret', json.dumps(result))
        with self.assertRaises(UnicodeDecodeError):
            capture.describe_stdout(b'Id=\xff\n', ('Id',))


if __name__ == '__main__':
    unittest.main()
