"""Exercise failure evidence in the synthetic workflow, without a user manager."""
import json
import pwd
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest


WORKFLOW = Path('.github/workflows/m46-protected-boundary-probe.yml')


def lifecycle_program():
    workflow = WORKFLOW.read_text()
    step = workflow.split('name: Observe a synthetic candidate-owned user service', 1)[1]
    return textwrap.dedent(step.split("<<'PY'\n", 1)[1].split('\n          PY\n', 1)[0])


def test_failed_service_capture_retains_original_failure_and_command_exits(monkeypatch):
    written = {}
    calls = []
    monkeypatch.setattr(pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=1002, pw_gid=1002, pw_dir='/home/m46candidate'))
    monkeypatch.setattr(sys, 'argv', ['-', '/synthetic/lifecycle.json'])
    monkeypatch.setattr(Path, 'write_text', lambda self, data: written.update(json.loads(data)))

    def run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if len(calls) == 2:
            return SimpleNamespace(returncode=0, stdout='MainPID=0\nActiveState=failed\nInvocationID=abc\n', stderr='')
        if len(calls) == 3:
            kwargs['stdout'].write(b'Result=exit-code\nExecMainStatus=203\n')
            return SimpleNamespace(returncode=0)
        if len(calls) == 4:
            kwargs['stderr'].write(b'status returned failure')
            return SimpleNamespace(returncode=3)
        raise subprocess.TimeoutExpired(argv, 10)

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='candidate user-manager observation incomplete'):
        exec(compile(lifecycle_program(), str(WORKFLOW), 'exec'), {})
    assert [row['inner_exit'] for row in written['commands']] == [0, 0]
    assert written['error'] == 'RuntimeError: candidate user-manager observation incomplete'
    show, status, journal = written['failure_diagnostics']
    assert show['inner_exit'] == 0 and 'ExecMainStatus=203' in show['stdout']
    assert status['inner_exit'] == 3 and status['stderr'] == 'status returned failure'
    assert journal['capture_error'].startswith('TimeoutExpired:')
    assert calls[-1][-1] == '_UID=1002'
    assert written['admitted'] is False and written['native_product_observed'] is False


def test_capture_truncates_output_and_continues_after_failed_start(monkeypatch):
    written = {}
    monkeypatch.setattr(pwd, 'getpwnam', lambda _: SimpleNamespace(pw_uid=1002, pw_gid=1002, pw_dir='/home/m46candidate'))
    monkeypatch.setattr(sys, 'argv', ['-', '/synthetic/lifecycle.json'])
    monkeypatch.setattr(Path, 'write_text', lambda self, data: written.update(json.loads(data)))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            return SimpleNamespace(returncode=1, stdout='', stderr='start failed')
        kwargs['stdout'].write(b'x' * 40000)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='command failed'):
        exec(compile(lifecycle_program(), str(WORKFLOW), 'exec'), {})
    assert written['commands'][0]['inner_exit'] == 1
    assert len(written['failure_diagnostics']) == 3
    assert all(len(row['stdout']) == 32768 and row['stdout_truncated'] for row in written['failure_diagnostics'])
