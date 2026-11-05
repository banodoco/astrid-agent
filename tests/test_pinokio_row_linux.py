import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts import pinokio_row_linux as linux


@pytest.fixture
def config(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'install').write_text('#!/bin/sh\nexit 0\n')
    (source / 'manifest.json').write_text(json.dumps({'release': 1}))
    (source / 'standin.json').write_text(json.dumps({'release': 2, 'qualification_marker': 'test'}))
    return {'row': 'pinokio-linux', 'agent_root': str(tmp_path),
            'pinokio_root': os.environ.get('PINOKIO_CANDIDATE', str(tmp_path / 'candidate')),
            'install_root': str(tmp_path / 'pinokio/api/app/app/astrid'),
            'manifest': str(source / 'manifest.json'), 'standin_manifest': str(source / 'standin.json'),
            'task_command': ['true']}


def test_candidate_environment_and_installer_arguments(config, monkeypatch):
    if not Path(config['pinokio_root'], 'pinokio/environment.js').is_file():
        pytest.skip('set PINOKIO_CANDIDATE to the frozen candidate')
    monkeypatch.setenv('ASTRID_UNTRUSTED_INHERITED', 'must-not-pass')
    row = linux.LinuxRow(config)
    row.prepare()
    assert row.env['ASTRID_INSTALL_MANAGED_BY'] == 'pinokio'
    assert 'ASTRID_UNTRUSTED_INHERITED' not in row.env
    assert 'TMPDIR' not in row.env
    calls = []
    monkeypatch.setattr(row, 'command', lambda *args, **kw: calls.append(args))
    row.install()
    row.install(row.standin)
    row.install(uninstall=True)
    prefix = ('sh', row.installer, '--root', row.root, '--managed-by', 'pinokio')
    assert calls == [prefix + ('--manifest', row.input_manifest),
                     prefix + ('--manifest', row.input_manifest), prefix + ('--uninstall', '--manifest', row.input_manifest)]


def test_refusal_is_recorded_and_no_later_stage_runs(config, monkeypatch):
    events = []
    monkeypatch.setattr(linux.LinuxRow, 'prepare', lambda self: events.append('prepare'))
    monkeypatch.setattr(linux.LinuxRow, 'install_initial', lambda self: events.append('install'))
    monkeypatch.setattr(linux.LinuxRow, 'start', lambda self: events.append('start'))
    monkeypatch.setattr(linux.LinuxRow, 'task_once', lambda self: events.append('task'))
    def refuse(self):
        events.append('stop')
        raise linux.RowError('Astrid is busy (task 42)')
    monkeypatch.setattr(linux.LinuxRow, 'stop', refuse)
    result = linux.run(config)
    assert events == ['prepare', 'install', 'start', 'task', 'stop']
    assert [s['name'] for s in result] == ['install', 'start_health_task', 'stop_start']
    assert result[-1] == {'name': 'stop_start', 'status': 'fail', 'error': 'stop: RowError: Astrid is busy (task 42)'}


def test_missing_config_produces_failure_receipt(config):
    del config['install_root']
    receipt = linux.run(config)
    assert receipt[0]['name'] == 'install'
    assert receipt[0]['status'] == 'fail'
    assert 'install_root' in receipt[0]['error']


def test_stop_rejects_changed_identity_before_candidate_stop(config, monkeypatch):
    row = linux.LinuxRow(config)
    row.identity = {'runtime_instance_id': 'original', 'realm_root': '/workspace'}
    row.health = lambda: {'runtime_instance_id': 'other', 'realm_root': '/workspace'}
    row.command = lambda *args, **kwargs: pytest.fail('must not stop a different Runtime')
    with pytest.raises(linux.RowError, match='identity changed'):
        row.stop()


def test_busy_stop_requires_running_task(config, monkeypatch, tmp_path):
    row = linux.LinuxRow(config)
    row.identity = {'realm_root': str(tmp_path)}
    row.task_env = lambda: dict(os.environ)
    row.env = {'HOME': str(tmp_path)}
    row.work.mkdir()
    class Finished:
        returncode = 0
        def poll(self): return 0
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: Finished())
    monkeypatch.setattr(linux.time, 'sleep', lambda *_: None)
    row.stop = lambda: pytest.fail('must not claim busy stop')
    with pytest.raises(linux.RowError, match='no busy task observed'):
        row.busy_stop()


def test_reject_reused_home(config, monkeypatch):
    home = Path(config['standin_manifest']).parent / 'pinokio-linux-row'
    home.mkdir(parents=True)
    row = linux.LinuxRow(config)
    with pytest.raises(linux.RowError, match='refusing reuse'):
        row.prepare()


def test_task_events_require_id_and_state():
    output = 'noise\n{"data":{"id":"t1","state":"running"}}\n{"data":{"id":"t1","state":"succeeded"}}\n'
    assert linux.LinuxRow.task_events(output) == [('t1', 'running'), ('t1', 'succeeded')]
    assert linux.LinuxRow.task_events('{"ok":true}\n') == []


def test_task_without_terminal_receipt_fails(config, monkeypatch, tmp_path):
    row = linux.LinuxRow(config)
    row.env = {'HOME': str(tmp_path)}
    row.task_env = lambda: {}
    monkeypatch.setattr(linux, 'run_process', lambda *args, **kwargs: '{"ok":true}')
    with pytest.raises(linux.RowError, match='successful terminal task'):
        row.task_once()


def test_stop_uses_preflight_controller_and_lock_and_checks_other_runtimes(config, monkeypatch, tmp_path):
    row = linux.LinuxRow(config)
    row.data = tmp_path / 'data'
    row.identity = {'runtime_instance_id': 'instance-1', 'realm_root': str(row.data / 'realm')}
    row.health = lambda: dict(row.identity)
    row.record = lambda: (row.data, None, None)
    class Session:
        pid = 12345
        def wait(self, timeout): return 0
    row.session = Session()
    calls = []
    row.command = lambda *args, **kwargs: calls.append(args)
    monkeypatch.setattr(linux, 'other_runtimes', lambda pid: {99: ('birth', 'other')})
    row.stop()
    assert len(calls) == 1
    script = calls[0][3]
    assert script.index('preflightStop') < script.index('requestGracefulStop') < script.index('waitForInstallerLock')


def test_stop_rejects_other_runtime_change(config, monkeypatch, tmp_path):
    row = linux.LinuxRow(config)
    row.data = tmp_path / 'data'
    row.identity = {'runtime_instance_id': 'instance-1', 'realm_root': str(row.data / 'realm')}
    row.health = lambda: dict(row.identity)
    row.record = lambda: (row.data, None, None)
    class Session:
        pid = 12345
        def wait(self, timeout): return 0
    row.session = Session()
    row.command = lambda *args, **kwargs: None
    snapshots = iter([{99: ('birth', 'other')}, {}])
    monkeypatch.setattr(linux, 'other_runtimes', lambda pid: next(snapshots))
    with pytest.raises(linux.RowError, match='Another Runtime process changed'):
        row.stop()


def test_all_seven_stages_require_their_operations(config, monkeypatch):
    calls = []
    for method in ('prepare', 'install_initial', 'start', 'task_once', 'stop', 'busy_stop', 'verify_marker', 'update', 'release2', 'uninstall', 'cleanup_check'):
        def observe(self, *args, _method=method, **kwargs):
            calls.append(_method)
        monkeypatch.setattr(linux.LinuxRow, method, observe)
    result = linux.run(config)
    assert [step['name'] for step in result] == [
        'install', 'start_health_task', 'stop_start', 'busy_stop', 'update',
        'release2_health_task', 'uninstall_cleanup']
    assert all(step['status'] == 'pass' for step in result)
    assert calls == ['prepare', 'install_initial', 'start', 'task_once', 'stop', 'start',
                     'busy_stop', 'start', 'verify_marker', 'stop', 'update',
                     'start', 'release2', 'stop', 'uninstall', 'cleanup_check']


def test_uninstall_uses_launcher_preflight_before_installer_and_finish(config):
    row = linux.LinuxRow(config)
    calls = []
    def launcher(body, *args):
        calls.append((body, args))
        return '["/safe/data"]' if 'console.log' in body else ''
    row.launcher_call = launcher
    row.install = lambda **kwargs: calls.append(('install', kwargs))
    row.uninstall()
    assert 'assertSafeRemoval' in calls[0][0]
    assert 'stopForUninstall' in calls[1][0]
    assert 'assertSafeRemoval' in calls[2][0] and 'ensureDoorPortFree' in calls[2][0]
    assert calls[3] == ('install', {'uninstall': True})
    assert 'finishUninstall' in calls[4][0]


def test_uninstall_refusal_preserves_installer_and_cleanup(config):
    row = linux.LinuxRow(config)
    calls = []
    def launcher(body, *args):
        calls.append(body)
        if 'stopForUninstall' in body:
            raise linux.RowError('stop refused')
        return '[]'
    row.launcher_call = launcher
    row.install = lambda **kwargs: pytest.fail('installer must not run after refusal')
    with pytest.raises(linux.RowError, match='stop refused'):
        row.uninstall()
    assert len(calls) == 2


def test_install_rejects_symlinked_program_ancestor(config, tmp_path):
    alias = tmp_path / 'alias'
    alias.symlink_to(tmp_path / 'real')
    config['install_root'] = str(alias / 'api/app/app/astrid')
    with pytest.raises(linux.RowError, match='canonical'):
        linux.LinuxRow(config).prepare()
