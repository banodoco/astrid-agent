import json
import sys

import pytest

from astrid.core._shared import run_logs
from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, HostCancelled, HostError
from tests.test_generic_host import _write_manifest


def _run(tmp_path, monkeypatch, code, cancelled=None):
    manifest_path = _write_manifest(tmp_path / 'pack', capability_id='local.capture')
    manifest = json.loads(manifest_path.read_text())
    manifest['command']['argv'] = [sys.executable, '-c', code]
    manifest_path.write_text(json.dumps(manifest))
    host = GenericPackHost(pack_roots=[tmp_path])
    record = host.discover()[0]
    attempt = tmp_path / 'attempt'
    output = attempt / 'outputs'
    output.mkdir(parents=True)
    monkeypatch.setattr(generic_host, '_network_sandbox_argv', lambda argv, *_a: argv)
    try:
        with pytest.raises((HostError, HostCancelled)):
            host._run_command_definition(record, {}, output, attempt, cancelled=cancelled and (lambda: cancelled(output)))
    finally:
        host.shutdown()
    return output


def _assert_logs(output, stdout, stderr):
    declared = {decl.name: (decl.role, path.read_bytes()) for decl, path in run_logs.read_declared(output)}
    assert declared['job.stdout'] == ('stdout', stdout)
    assert declared['job.stderr'] == ('stderr', stderr)


def test_failed_job_preserves_complete_streams(tmp_path, monkeypatch):
    traceback = b'Traceback (most recent call last):\n  error\nValueError: private-value\n'
    code = "import os; os.write(1, b'x' * (3 * 1024 * 1024)); os.write(2, " + repr(traceback) + "); raise SystemExit(1)"
    output = _run(tmp_path, monkeypatch, code)
    _assert_logs(output, b'x' * (3 * 1024 * 1024), traceback)


def test_cancelled_job_preserves_streams_written_before_kill(tmp_path, monkeypatch):
    code = "import os, time; os.write(1, b'before kill\\n'); os.write(2, b'error before kill\\n'); time.sleep(30)"
    def ready(output):
        path = output.parent / 'task-stderr-tail.log'
        return path.exists() and path.stat().st_size >= len(b'error before kill\n')
    output = _run(tmp_path, monkeypatch, code, ready)
    _assert_logs(output, b'before kill\n', b'error before kill\n')
