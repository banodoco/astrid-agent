import base64
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import ci_mac_r18 as r18


def test_workflow_embeds_exact_script_and_is_manual():
    root = Path(__file__).parents[1]
    workflow = (root / '.github/workflows/journey.yml').read_text()
    match = re.search(r'base64\.b64decode\("([A-Za-z0-9+/=]+)", validate=True\)', workflow)
    assert match and base64.b64decode(match.group(1)) == (root / 'scripts/ci_mac_r18.py').read_bytes()
    assert 'workflow_dispatch:' in workflow and 'always()' in workflow
    assert 'actions/checkout' not in workflow


def test_candidate_pins_requested_tuple_and_artifacts(tmp_path):
    app = tmp_path / 'app/public'
    app.mkdir(parents=True)
    for name in ('install-manifest.json', 'install', 'install.sh', 'install.ps1'):
        (app / name).write_text(name)
    candidate = r18.make_candidate(tmp_path)
    assert candidate['agent'] == '80e19ea05d926172bbcf2588e46f6f9919776a81'
    assert candidate['app'] == 'ee66de5d100e0a9b8cf5198961556a68b24cad3e'
    assert candidate['runtime'] == '255ecda21ba904520eb867936ef173cc5f7d3ae4'
    assert candidate['pinokio'] == '3a6a98ae251ca126505245286e2d3e760f4348e9'
    assert set(candidate['artifacts']) == set(r18.ARTIFACTS)
    assert json.loads((tmp_path / 'candidate.json').read_text()) == candidate


def test_export_keeps_original_hash_and_redacts_runner_home(tmp_path):
    home = tmp_path / 'home'
    task = home / 'task'
    logs = task / 'logs'
    logs.mkdir(parents=True)
    raw = (str(home) + '/private\ntoken=abc123\nstop_identity=astrid-launchd-owner-predicate/v1\n').encode()
    (logs / 'journey.log').write_bytes(raw)
    result = {}
    upload = r18.export_evidence(home, task, tmp_path / "runner-temp/run", result)
    safe = (upload / 'logs/journey.log').read_bytes()
    assert str(home).encode() not in safe and b'abc123' not in safe
    assert b'stop_identity=astrid-launchd-owner-predicate/v1' in safe
    assert (logs / 'journey.log').read_bytes() == raw
    assert result['evidence'][0]['source_sha256'] != result['evidence'][0]['artifact_sha256']


@pytest.mark.parametrize('code,text,expected', [
    (113, 'Could not find service "haus.astrid.runtime" in domain for user gui: 503', True),
    (113, 'Bad request.\nCould not find service "haus.astrid.runtime" in domain for user gui: 503', True),
    (1, 'Could not find service "haus.astrid.runtime" in domain for user gui: 503', False),
    (113, 'Could not find service "another" in domain for user gui: 503', False),
])
def test_service_absent_requires_exact_identity(monkeypatch, code, text, expected):
    monkeypatch.setattr(r18.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=code, stderr=text))
    assert r18.service_absent() is expected


def test_export_preserves_before_cleanup_evidence(tmp_path):
    home = tmp_path / 'home'
    task = home / 'task'
    run = tmp_path / 'runner-temp/run'
    task.mkdir(parents=True)
    path = run / 'evidence-upload/before-cleanup/install.log'
    path.parent.mkdir(parents=True)
    path.write_text('stop_identity={"format":"astrid-launchd-owner-predicate/v1","root":"' + str(home) + '/test"}\n')
    result = {}
    upload = r18.export_evidence(home, task, run, result)
    assert '<RUNNER_HOME>/test' in (upload / 'retained/evidence-upload/before-cleanup/install.log').read_text()
    assert (task / 'retained/evidence-upload/before-cleanup/install.log').read_text() == path.read_text()
    assert result['stop_identity']['status'] == 'captured'
    assert '<RUNNER_HOME>/test' in (upload / 'stop-identity.json').read_text()
    assert result['evidence'][0]['source_sha256'] != result['evidence'][0]['artifact_sha256']


def test_disk_floor_is_decimal_10_gb(monkeypatch, tmp_path):
    monkeypatch.setattr(r18.shutil, 'disk_usage', lambda _: SimpleNamespace(free=9_999_999_999))
    with pytest.raises(RuntimeError, match='disk'):
        r18.disk(tmp_path)
    monkeypatch.setattr(r18.shutil, 'disk_usage', lambda _: SimpleNamespace(free=10_000_000_000))
    assert r18.disk(tmp_path) == 10_000_000_000


def test_environment_preserves_genuine_runner_temp(tmp_path):
    home = tmp_path / 'home'
    task = home / 'task'
    task.mkdir(parents=True)
    runner_temp = home / 'actions-runner/_work/_temp'
    env = r18.environment(home, task, home / 'python/bin', home / 'node/bin', runner_temp)
    assert env['RUNNER_TEMP'] == str(runner_temp)
    assert 'TMPDIR' not in env and 'GITHUB_TOKEN' not in env


def test_low_disk_interrupts_driver_process_group(monkeypatch, tmp_path):
    task = tmp_path / 'task'
    logs = task / 'logs'
    logs.mkdir(parents=True)
    states = iter([None, 9])
    process = SimpleNamespace(pid=765, returncode=130, poll=lambda: next(states))
    monkeypatch.setattr(r18.subprocess, 'Popen', lambda *a, **kw: process)
    monkeypatch.setattr(r18.shutil, 'disk_usage', lambda _: SimpleNamespace(free=2_999_999_999))
    calls = []
    monkeypatch.setattr(r18.os, 'killpg', lambda pid, sig: calls.append((pid, sig)))
    monkeypatch.setattr(r18.time, 'sleep', lambda _: None)
    result = r18.run_driver(['driver'], task, {}, task, logs)
    assert calls == [(765, r18.signal.SIGINT)]
    assert result['disk_interrupt_requested'] is True and result['exit'] == 130
