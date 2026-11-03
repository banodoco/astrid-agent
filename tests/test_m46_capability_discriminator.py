"""Fail-closed tests for the synthetic workflow; no real systemd is exercised."""
import json
import pwd
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

WORKFLOW = Path('.github/workflows/m46-protected-boundary-probe.yml')
ACCOUNT = SimpleNamespace(pw_uid=1002, pw_gid=1002, pw_dir='/home/m46candidate')
CAPS = ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')


def program(name):
    step = WORKFLOW.read_text().split('name: ' + name, 1)[1]
    return textwrap.dedent(step.split("<<'PY'\n", 1)[1].split('\n          PY\n', 1)[0])


def status(bad=None, service=False):
    values = dict(Uid='1002 1002 1002 1002', Gid='1002 1002 1002 1002', Groups='1002',
                  NoNewPrivs='1', **{name: '0000000000000000' for name in CAPS})
    if bad:
        values.update(bad)
    return ''.join(f'{k}:\t{v}\n' for k, v in values.items())


@pytest.mark.parametrize('bad', [None] + [{name: '1'} for name in CAPS] + [
    {'Uid': '0 1002 1002 1002'}, {'Gid': '0 1002 1002 1002'}, {'Groups': '1002 27'},
])
def test_manager_must_have_identity_and_empty_capability_sets(monkeypatch, bad):
    source = program('Verify disposable user manager capability boundary')
    written = {}
    monkeypatch.setattr(pwd, 'getpwnam', lambda _: ACCOUNT)
    monkeypatch.setattr(sys, 'argv', ['-', '/output'])
    monkeypatch.setattr(Path, 'write_text', lambda self, data: written.update(json.loads(data)))
    monkeypatch.setattr(Path, 'read_text', lambda self: status(bad))
    show = ('MainPID=123\nActiveState=active\nCapabilityBoundingSet=\n'
            'DropInPaths=/etc/systemd/system/user@1002.service.d/90-m46-capabilities.conf\n')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=show, stderr=''))
    if bad:
        with pytest.raises(RuntimeError, match='manager identity or capability boundary differs'):
            exec(compile(source, 'manager', 'exec'), {})
        assert 'manager_boundary_verified' not in written
    else:
        exec(compile(source, 'manager', 'exec'), {})
        assert written['manager_boundary_verified'] is True
    assert written['commands'][0]['inner_exit'] == 0
    assert written['admitted'] is False


@pytest.mark.parametrize('bad', [None] + [{name: '1'} for name in CAPS] + [
    {'Uid': '0 1002 1002 1002'}, {'Gid': '0 1002 1002 1002'},
    {'Groups': '1002 27'}, {'NoNewPrivs': '0'},
])
def test_service_observation_rejects_unrestricted_token(monkeypatch, bad):
    source = program('Observe a synthetic candidate-owned user service')
    written = {}
    calls = []
    monkeypatch.setattr(pwd, 'getpwnam', lambda _: ACCOUNT)
    monkeypatch.setattr(sys, 'argv', ['-', '/output'])
    monkeypatch.setattr(Path, 'write_text', lambda self, data: written.update(json.loads(data)))
    monkeypatch.setattr(Path, 'read_text', lambda self: 'birth-stat' if str(self).endswith('/stat') else status(bad, True))
    monkeypatch.setattr(Path, 'resolve', lambda self, strict=False: Path('/usr/bin/sleep'))
    monkeypatch.setattr(Path, 'exists', lambda self: False)
    def run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 2:
            return SimpleNamespace(returncode=0, stdout=('MainPID=123\nActiveState=active\nInvocationID=abc\n'
                'FragmentPath=/home/m46candidate/.config/systemd/user/m46-boundary.service\n'
                'NoNewPrivileges=yes\nCapabilityBoundingSet=reported-default\n'), stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(subprocess, 'run', run)
    if bad:
        with pytest.raises(RuntimeError, match='service token or executable differs'):
            exec(compile(source, 'service', 'exec'), {})
        assert 'synthetic_service_lifecycle' not in written
    else:
        exec(compile(source, 'service', 'exec'), {})
        assert written['synthetic_service_lifecycle'] is True
        assert [row['inner_exit'] for row in written['commands']] == [0, 0, 0, 0]
    assert written['admitted'] is False


def test_workflow_requires_verified_manager_and_does_not_request_user_drop():
    text = WORKFLOW.read_text()
    service = text.split('name: Observe a synthetic candidate-owned user service', 1)[1].split('name: Remove the exact', 1)[0]
    unit = service.split("<<'UNIT'", 1)[1].split('\n          UNIT', 1)[0]
    assert "steps.manager_boundary.outcome == 'success'" in service
    assert 'CapabilityBoundingSet=' not in unit
    assert 'NoNewPrivileges=yes' in unit
    assert 'CapabilityBoundingSet=\\n' in text.split('name: Provision', 1)[1].split('name: Attempt', 1)[0]
