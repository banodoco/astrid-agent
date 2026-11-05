import json
from pathlib import Path

import pytest

from scripts import pinokio_row_windows as row


def config(tmp_path):
    app = tmp_path / 'home' / 'api' / 'launcher'
    return {'row': 'pinokio-windows', 'agent_root': str(tmp_path / 'agent'),
            'pinokio_root': str(tmp_path / 'candidate'), 'install_root': str(app / 'app' / 'astrid'),
            'manifest': str(tmp_path / 'public' / 'install-manifest.json'),
            'standin_manifest': str(tmp_path / 'standin.json'), 'task_command': ['task-client']}


def test_order_and_first_failure(monkeypatch, tmp_path):
    calls = []
    class Fake:
        def __init__(self, cfg):
            assert cfg == config(tmp_path)
        def __getattr__(self, name):
            def stage():
                calls.append(name)
                if name == 'busy_stop':
                    raise RuntimeError('Runtime busy')
            return stage
    monkeypatch.setattr(row, 'WindowsRow', Fake)
    steps = row.run(config(tmp_path))
    assert calls == list(row.STEPS[:4])
    assert [s['name'] for s in steps] == calls
    assert [s['status'] for s in steps] == ['pass', 'pass', 'pass', 'fail']
    assert steps[-1]['error'] == 'RuntimeError: Runtime busy'


def test_install_and_uninstall_use_launcher_arguments(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    manifest = Path(cfg['manifest'])
    manifest.parent.mkdir()
    (manifest.parent / 'install.ps1').write_text('')
    runner = row.WindowsRow(cfg)
    calls = []
    monkeypatch.setattr(row, 'run_command', lambda argv, **kwargs: calls.append(argv))
    runner.installer(manifest)
    runner.installer(Path(cfg['standin_manifest']))
    runner.installer(None, uninstall=True)
    prefix = ['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(manifest.parent / 'install.ps1'), '--root', str(runner.root), '--managed-by', 'pinokio']
    assert calls == [prefix + ['--manifest', str(manifest)],
                     prefix + ['--manifest', cfg['standin_manifest']], prefix + ['--uninstall']]


def test_environment_uses_launcher_policy(tmp_path, monkeypatch):
    calls = []
    def invoke(argv, **kwargs):
        calls.append(argv)
        return json.dumps({'PATH': 'clean', 'ASTRID_INSTALL_MANAGED_BY': 'pinokio'})
    monkeypatch.setattr(row, 'run_command', invoke)
    env = row.launcher_env(tmp_path / 'candidate', tmp_path / 'home' / 'api' / 'launcher')
    assert env['PATH'] == 'clean'
    assert Path(calls[0][-2]) == tmp_path / 'candidate' / 'pinokio' / 'environment.js'


def test_busy_stop_attempts_launcher_stop_only_after_running_task(tmp_path, monkeypatch):
    runner = row.WindowsRow(config(tmp_path))
    events = []
    reports = iter([{'safe': True}, {'safe': True}, {'safe': False, 'details': {'active_tasks': [{'task_id': 't', 'status': 'running'}]}}])
    def preflight():
        events.append('preflight')
        return next(reports)
    class Client:
        def poll(self): return None
    monkeypatch.setattr(runner, 'preflight', preflight)
    monkeypatch.setattr(row.subprocess, 'Popen', lambda *a, **kw: Client())
    monkeypatch.setattr(row.time, 'sleep', lambda _: None)
    def stop():
        events.append('stop')
        raise row.RowError('launcher refused busy task')
    monkeypatch.setattr(runner, 'stop', stop)
    with pytest.raises(row.RowError, match='launcher refused busy task'):
        runner.busy_stop()
    assert events == ['preflight', 'preflight', 'preflight', 'stop']


def test_running_client_does_not_prove_busy_task(tmp_path, monkeypatch):
    runner = row.WindowsRow(config(tmp_path))
    reports = iter([{'safe': True}, {'safe': False, 'message': 'unknown'}])
    monkeypatch.setattr(runner, 'preflight', lambda: next(reports))
    class Client:
        def poll(self): return None
    monkeypatch.setattr(row.subprocess, 'Popen', lambda *a, **kw: Client())
    monkeypatch.setattr(runner, 'stop', lambda: pytest.fail('must not stop'))
    with pytest.raises(row.RowError, match='without a running task'):
        runner.busy_stop()


def test_start_uses_pinned_node_and_does_not_refuse_force_capable_source(tmp_path, monkeypatch):
    runner = row.WindowsRow(config(tmp_path))
    pin = tmp_path / 'candidate' / 'pinokio'
    pin.mkdir(parents=True)
    (pin / 'session.mjs').write_text('taskkill.exe')
    calls = []
    class Child:
        pid = 57
        returncode = None
        def poll(self): return None
    monkeypatch.setattr(runner, 'version', lambda: {'env': {'ASTRID_NODE_EXECUTABLE': 'pinned-node'}})
    monkeypatch.setattr(row.subprocess, 'Popen', lambda argv, **kw: calls.append(argv) or Child())
    monkeypatch.setattr(runner, 'health', lambda: {})
    monkeypatch.setattr(row, 'read_json', lambda _: {'pid': 57, 'token': 'x', 'port': 1})
    runner.start()
    assert calls[0] == ['pinned-node', str(pin / 'session.mjs'), 'serve', str(runner.root), str(runner.app / 'app' / 'editor'), 'backend']


def test_shared_driver_records_actual_first_failure(tmp_path, monkeypatch):
    import importlib.util
    from types import SimpleNamespace
    import sys

    driver_path = Path('/home/user/merge/ws/M-RT/queue/running/X-PINOKIO-ROW-DRIVER/worktree/scripts/pinokio_row_driver.py')
    if not driver_path.is_file():
        pytest.skip('shared driver checkout is unavailable')
    spec = importlib.util.spec_from_file_location('scripts._shared_driver_for_windows_test', driver_path)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    cfg = config(tmp_path)
    manifest = Path(cfg['manifest'])
    manifest.parent.mkdir()
    manifest.write_text('{"release": 1}')
    output = tmp_path / 'receipt.json'
    monkeypatch.setattr(row, 'sys', SimpleNamespace(platform='linux'))
    assert driver.run(cfg, output) is False
    receipt = json.loads(output.read_text())
    assert receipt['row'] == 'pinokio-windows'
    assert receipt['steps'] == [{'name': 'install', 'status': 'fail',
                                 'error': 'RowError: Windows row requires Windows'}]
    assert json.loads(Path(cfg['standin_manifest']).read_text())['release'] == 2
