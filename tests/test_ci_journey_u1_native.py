import contextlib
import io
import json
import unittest
from unittest import mock

from scripts import ci_journey_u1_native as probe


class HostObservationTests(unittest.TestCase):
    def test_identity_reuse_refuses(self):
        with mock.patch.object(probe.LiveProcessProbe, '_linux_identity', side_effect=[('1', probe.Path('/bin/sh')), ('2', probe.Path('/bin/sh'))]), \
             mock.patch.object(probe.Path, 'read_text', return_value='Uid:\t1000\t1000\t1000\t1000\n'), \
             mock.patch.object(probe.Path, 'read_bytes', return_value=b'executable'):
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                probe.observe(123)

    def test_root_product_refuses_without_starting_child(self):
        output = io.StringIO()
        with mock.patch.object(probe.os, 'geteuid', return_value=0), \
             mock.patch.object(probe.sys, 'argv', ['probe', '0', '0']), \
             mock.patch.object(probe.subprocess, 'Popen') as popen, \
             contextlib.redirect_stdout(output):
            self.assertEqual(probe.main(), 2)
        self.assertIn('nonzero', json.loads(output.getvalue())['error'])
        popen.assert_not_called()

    def test_unprivileged_observer_is_unknown(self):
        output = io.StringIO()
        with mock.patch.object(probe.os, 'geteuid', return_value=1000), \
             contextlib.redirect_stdout(output):
            self.assertEqual(probe.main(), 2)
        self.assertFalse(json.loads(output.getvalue())['authority'])


if __name__ == '__main__':
    unittest.main()
