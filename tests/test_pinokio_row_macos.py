import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import pinokio_row_macos as row


def config(tmp_path):
    return {'row': 'pinokio-macos', 'agent_root': str(tmp_path / 'agent'),
            'pinokio_root': str(tmp_path / 'candidate'),
            'install_root': str(tmp_path / 'pinokio/api/astrid/app/astrid'),
            'manifest': str(tmp_path / 'public/install-manifest.json'),
            'standin_manifest': str(tmp_path / 'standin.json'),
            'task_command': ['test-task']}


def test_order_and_first_failure(monkeypatch, tmp_path):
    called = []
    class Fake:
        def __init__(self, config): pass
        def __getattr__(self, name):
            def operation(*args, **kwargs):
                called.append(name)
                if name == 'busy_stop':
                    raise RuntimeError('busy refusal')
            return operation
    monkeypatch.setattr(row, 'MacOSRow', Fake)
    result = row.run(config(tmp_path))
    assert [s['name'] for s in result] == ['install', 'start_health_task', 'stop_start', 'busy_stop']
    assert [s['status'] for s in result] == ['pass', 'pass', 'pass', 'fail']
    assert result[-1]['error'] == 'busy_stop: RuntimeError: busy refusal'
    assert called == ['prepare', 'install', 'start', 'task_once', 'stop', 'start', 'busy_stop']


def test_installer_arguments_and_existing_manifest_input(tmp_path):
    cfg = config(tmp_path)
    runner = row.MacOSRow(cfg)
    calls = []
    runner.command = lambda *args, **kw: calls.append(args)
    runner.record = lambda: (None, tmp_path, None)
    monkey = {'manifest': 1}
    (tmp_path / 'manifest.json').write_text(json.dumps(monkey))
    runner.manifest.parent.mkdir(parents=True)
    runner.manifest.write_text(json.dumps(monkey))
    runner.standin.write_text(json.dumps(monkey))
    runner.install()
    runner.install(runner.standin)
    runner.install(uninstall=True)
    assert calls == [
        ('sh', runner.installer, '--root', runner.root, '--managed-by', 'pinokio', '--manifest', runner.manifest),
        ('sh', runner.installer, '--root', runner.root, '--managed-by', 'pinokio', '--manifest', runner.standin),
        ('sh', runner.installer, '--root', runner.root, '--managed-by', 'pinokio', '--uninstall'),
    ]


def test_task_observations_require_identity_and_terminal_state():
    assert row.MacOSRow.task_events('noise\n{"data":{"task_id":"one","state":"running"}}\n{"data":{"task_id":"one","state":"succeeded"}}') == [('one', 'running'), ('one', 'succeeded')]
    assert row.MacOSRow.task_events('{"state":"succeeded"}\n{"task_id":"one"}') == []


def test_stop_uses_candidate_graceful_path_and_preserves_on_failure(monkeypatch, tmp_path):
    runner = row.MacOSRow(config(tmp_path))
    runner.data = tmp_path / 'data'
    runner.identity = {'runtime_instance_id': 'instance', 'realm_root': str(tmp_path / 'realm')}
    runner.session = SimpleNamespace(pid=123, wait=lambda **kw: pytest.fail('must not wait after failed stop'))
    runner.health = lambda: runner.identity
    runner.record = lambda: (runner.data, None, None)
    monkeypatch.setattr(row, 'other_runtimes', lambda pid: {})
    calls = []
    def fail(*args, **kwargs):
        calls.append(args)
        raise row.RowError('graceful refusal')
    runner.command = fail
    with pytest.raises(row.RowError, match='graceful refusal'):
        runner.stop()
    assert len(calls) == 1
    assert 'preflightStop' in calls[0][3]
    assert 'requestGracefulStop' in calls[0][3]
    assert 'waitForInstallerLock' in calls[0][3]


def test_other_runtime_change_is_detectable(monkeypatch, tmp_path):
    data = tmp_path / 'data'
    (data / 'runtime').mkdir(parents=True)
    discovery = data / 'runtime/discovery.json'
    discovery.write_text(json.dumps({'runtime_instance_id': 'one'}))
    ps = '  10 1 Thu Nov  5 07:00:00 2026 python -m runtime_protocol start\n  20 1 Thu Nov  5 07:00:00 2026 node session\n  21 20 Thu Nov  5 07:00:01 2026 python -m runtime_protocol start\n'
    def check_output(args, **kw):
        return ps if '-axo' in args else f'python ASTRID_RUNTIME_DATA_ROOT={data}'
    monkeypatch.setattr(row.subprocess, 'check_output', check_output)
    before = row.other_runtimes(20)
    assert list(before) == [10]
    discovery.write_text(json.dumps({'runtime_instance_id': 'two'}))
    assert row.other_runtimes(20) != before


def test_non_macos_reports_concrete_first_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(row.sys, 'platform', 'linux')
    assert row.run(config(tmp_path)) == [{'name': 'install', 'status': 'fail',
        'error': 'prepare: RowError: Expected the pinokio-macos row on macOS'}]


def test_stop_detects_another_runtime_changing(monkeypatch, tmp_path):
    runner = row.MacOSRow(config(tmp_path))
    runner.data = tmp_path / 'data'
    runner.identity = {'runtime_instance_id': 'instance', 'realm_root': str(tmp_path / 'realm')}
    runner.session = SimpleNamespace(pid=123, wait=lambda **kw: 0)
    runner.health = lambda: runner.identity
    runner.record = lambda: (runner.data, None, None)
    runner.command = lambda *args, **kw: ''
    snapshots = iter([{55: ('birth', 'one')}, {55: ('birth', 'two')}])
    monkeypatch.setattr(row, 'other_runtimes', lambda pid: next(snapshots))
    with pytest.raises(row.RowError, match='Another Runtime process changed'):
        runner.stop()


def test_real_candidate_environment_helper_is_used(monkeypatch, tmp_path):
    candidate = Path('/home/user/merge/ws/M-RT/repos/astrid-pinokio/repos/astrid-pinokio-candidate')
    if not (candidate / 'pinokio/environment.js').is_file():
        pytest.skip('candidate checkout unavailable')
    cfg = config(tmp_path)
    cfg['pinokio_root'] = str(candidate)
    Path(cfg['agent_root']).mkdir()
    Path(cfg['manifest']).parent.mkdir()
    (Path(cfg['manifest']).parent / 'install').write_text('# installer fixture')
    Path(cfg['manifest']).write_text('{"release":1}')
    Path(cfg['standin_manifest']).write_text('{"release":2,"qualification_marker":"test"}')
    # The actual helper runs on Linux here; the row's host check is mocked.
    monkeypatch.setattr(row.sys, 'platform', 'darwin')
    runner = row.MacOSRow(cfg)
    runner.prepare()
    assert runner.env['ASTRID_INSTALL_MANAGED_BY'] == 'pinokio'
    assert runner.env['HOME'] == str(runner.scratch / 'home')
