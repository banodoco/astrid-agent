"""The installer delegates stop authority to Runtime before touching a manager."""
from __future__ import annotations

import importlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from astrid.installer.backend import Backend
from astrid.installer.core import Installer, RuntimeOperationError, VerificationError, Version, switch, write_json
from astrid.installer.service import LABEL
from astrid_runtime.paths import RuntimePaths


def realm_fixture(tmp_path, monkeypatch, *, system='linux', blocker='running'):
    root, data = tmp_path / 'program', tmp_path / 'data'
    value = Backend(root, tmp_path / 'install.log', tmp_path / 'uv')
    value.service.selection = tmp_path / 'selection.json'
    value.service.system = system
    value.service.path = tmp_path / 'service-definition'
    value.service.path.write_text('unchanged startup configuration')
    paths = RuntimePaths.current(data_root=data)
    paths.ensure_support_dirs()
    realm = paths.realms_dir / 'selected'
    realm.mkdir(parents=True)
    with sqlite3.connect(realm / 'realm.sqlite3') as db:
        db.executescript('''
            CREATE TABLE tasks(id TEXT, capability TEXT, status TEXT, created_at TEXT);
            CREATE TABLE attempts(id TEXT,task_id TEXT,lease_id TEXT,lease_expires_at TEXT,runtime_epoch INTEGER,settled INTEGER);
            CREATE TABLE execution_bindings(binding_id TEXT,task_id TEXT,status TEXT);
            CREATE TABLE reservations(task_id TEXT,resource_key TEXT,lease_token TEXT,released_at TEXT,executor_id TEXT,fence INTEGER DEFAULT 1,lease_expires_at TEXT,runtime_epoch INTEGER DEFAULT 1);
        ''')
        if blocker:
            status = 'running' if blocker == 'running' else 'cancel_requested' if blocker == 'cancel' else 'queued'
            db.execute("INSERT INTO tasks VALUES ('task-1','render',?,'2000-01-01T00:00:00Z')", (status,))
            if blocker == 'attempt':
                db.execute("INSERT INTO attempts VALUES ('attempt-1','task-1','lease-1','2000-01-01T00:00:00Z',1,0)")
            if blocker in ('claimed', 'stale'):
                db.execute("INSERT INTO execution_bindings VALUES ('binding-1','task-1',?)", (blocker,))
            if blocker == 'reservation':
                db.execute("INSERT INTO reservations(task_id,resource_key,lease_token,released_at) VALUES ('task-1','gpu:0','lease-1',NULL)")
    write_json(paths.catalog_path, {'version': 1, 'selected_realm_id': 'selected',
        'realms': [{'realm_id': 'selected', 'display_name': 'Selected', 'data_root': str(realm)}], 'source_profiles': {}})
    oldroot = root / 'versions' / 'old'
    oldroot.mkdir(parents=True)
    profile = oldroot / 'profile.json'
    write_json(profile, {'profile': 'astrid', 'runtime_checkout': str(tmp_path), 'source_checkout': str(tmp_path)})
    old = Version(oldroot, Path(sys.executable), profile, {})
    write_json(oldroot / 'version.json', old.document())
    switch(root, old)
    (root / 'run').write_bytes(b'unchanged existing launcher')
    write_json(root / 'install.json', {'owner': LABEL, 'data_root': str(data)})
    write_json(value.service.selection, {'owner': LABEL, 'program_root': str(root), 'data_root': str(data),
                                        'preferences': {'autostart': True}})
    calls = []
    for name in ('disable', 'stop', 'stop_foreground', 'unregister'):
        monkeypatch.setattr(value.service, name, lambda n=name: calls.append(n))
    monkeypatch.setattr(value.service, 'login_service_exists', lambda: True)
    monkeypatch.setattr(value.service, 'validate_stop_target', lambda *_args, **_kwargs: None)
    for name in ('register', 'start', 'restore', 'remove', 'backup', 'stage', 'upgrade', 'finish', 'open'):
        monkeypatch.setattr(value, name, Mock(side_effect=AssertionError(f'unexpected {name}')))
    monkeypatch.setattr(value, 'existing', lambda _data: old)
    monkeypatch.setattr(value, 'workspace', lambda *_: {'realm_id': 'selected', 'realm_root': str(realm)})
    monkeypatch.setattr(value, 'verify', Mock(side_effect=VerificationError('restart needed')))
    monkeypatch.setattr(value, 'preflight_remove', lambda *_, **kwargs: None)
    (data / 'workspace-sentinel').write_bytes(b'user work')
    return value, old, data, paths, realm, calls


def snapshot(paths):
    return {str(p): p.read_bytes() for p in paths if p.is_file()}


@pytest.mark.parametrize('system', ['linux', 'darwin', 'win32'])
@pytest.mark.parametrize('operation', ['repair', 'update', 'uninstall', 'delete-data', 'recovery', 'candidate'])
@pytest.mark.parametrize('blocker', ['running', 'cancel', 'attempt', 'claimed', 'stale', 'reservation'])
def test_busy_stop_path_matrix_has_no_manager_signal_or_cleanup(tmp_path, monkeypatch, system, operation, blocker):
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, system=system, blocker=blocker)
    watched = [*data.rglob('*'), backend.root / 'current', backend.root / 'install.json', backend.root / 'run',
               backend.service.selection, backend.service.path]
    before = snapshot(watched)
    before_names = sorted(str(p) for p in data.rglob('*'))
    installer = Installer(backend.root, data, backend, say=lambda _: None)
    if operation == 'recovery':
        write_json(backend.root / 'update-transaction.json', {'format': 'astrid-install-update/v1',
            'operation_id': 'a' * 32, 'data_root': str(data), 'old': old.document(), 'candidate': None,
            'realm': {'realm_id': 'selected', 'realm_root': str(realm)}, 'backup': None, 'step': 'stopped'})
    with pytest.raises(RuntimeOperationError) as raised:
        if operation == 'candidate':
            backend.stop(old, data)
        else:
            mode = 'install' if operation == 'repair' else 'uninstall' if operation in ('uninstall', 'delete-data') else 'update'
            installer.run(mode, {}, delete_data=operation == 'delete-data')
    assert raised.value.stop_refused is True
    assert raised.value.details['task_count'] == 1
    assert str(raised.value).splitlines()[0] == 'Astrid is busy (1 tasks running). Try again when they finish.'
    assert raised.value.details['blockers'][0]['task_id'] == 'task-1'
    assert snapshot(watched) == before
    assert sorted(str(p) for p in data.rglob('*')) == before_names
    assert calls == []


class Boundary:
    def __init__(self, paths, calls, *, failure=None):
        self.paths, self.calls, self.failure = paths, calls, failure
    def prepare_restart(self, **kwargs):
        self.calls.append('prepare')
    def is_pid_alive(self, pid):
        return True
    def process_birth_identity(self, pid):
        return 'birth'
    def validate_owner(self, **kwargs):
        return True
    def endpoint_metadata(self, **kwargs):
        return {'runtime_instance_id': 'instance', 'realm_id': 'selected'}
    def stop_owner(self, **kwargs):
        self.calls.append('runtime-stop')
        if self.failure:
            raise self.failure
        return {'status': 'stopped'}


def publish(paths, realm):
    record = {'pid': 123, 'process_birth_id': 'birth', 'runtime_instance_id': 'instance',
              'active_realm': 'selected', 'realm_id': 'selected', 'realm_root': str(realm),
              'endpoint': 'http://127.0.0.1:12345'}
    write_json(paths.discovery_path, record)
    write_json(paths.instance_lock_path, record)


@pytest.mark.parametrize('system', ['linux', 'darwin', 'win32'])
def test_idle_owner_stops_once_before_manager_with_fence_held(tmp_path, monkeypatch, system):
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, system=system, blocker=None)
    publish(paths, realm)
    boundary = Boundary(paths, calls)
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    def manager_disable():
        calls.append('disable')
        with sqlite3.connect(realm / 'realm.sqlite3', timeout=0) as db:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                db.execute("INSERT INTO tasks VALUES ('raced','render','running',NULL)")
    monkeypatch.setattr(backend.service, 'disable', manager_disable)
    backend.stop(old, data)
    assert calls == ['prepare', 'runtime-stop', 'disable', 'stop']
    assert not paths.discovery_path.exists()
    assert not paths.instance_lock_path.exists()


@pytest.mark.parametrize('failure', [TimeoutError('timeout'), RuntimeError('identity changed')])
def test_unknown_owner_stop_failure_never_falls_back_to_manager(tmp_path, monkeypatch, failure):
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    publish(paths, realm)
    boundary = Boundary(paths, calls, failure=failure)
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    before = snapshot([paths.discovery_path, paths.instance_lock_path])
    with pytest.raises(RuntimeOperationError) as raised:
        backend.stop(old, data)
    assert not raised.value.stop_completed
    assert calls == ['prepare', 'runtime-stop']
    assert snapshot([paths.discovery_path, paths.instance_lock_path]) == before


@pytest.mark.parametrize('output', [
    {'ok': False, 'error': {'message': 'busy headline', 'details': {'safe': False, 'blockers': [{'task_id': 't'}], 'task_count': 1}}},
    {'ok': False, 'error': {'message': 'identity failure', 'details': {'expected': {'instance_id': 'old'}}}},
    {'ok': False, 'error': 'corrupt owner'},
])
def test_actual_subprocess_result_mapping_preserves_refusals(tmp_path, monkeypatch, output):
    backend = Backend(tmp_path, tmp_path / 'log', Path(sys.executable))
    def run(argv, **kwargs):
        # Use Backend.run and its actual nonzero subprocess mapping.
        return Backend.run(backend, [sys.executable, '-I', '-c',
            'import json,sys;print(json.dumps(json.loads(sys.argv[1])));sys.exit(1)', json.dumps(output)])
    monkeypatch.setattr(backend, 'run', run)
    with pytest.raises(RuntimeOperationError) as raised:
        backend.runtime(None, tmp_path / 'data', 'down')
    error = output['error']
    assert str(raised.value) == (error['message'] if isinstance(error, dict) else error)
    assert raised.value.details == (error.get('details') if isinstance(error, dict) else None)
    assert raised.value.stop_refused


def test_cli_busy_preflight_precedes_marker_and_selection_writes(tmp_path, monkeypatch, capsys):
    from astrid.installer import cli
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, 'Backend', lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(cli, 'installer_lock_path', lambda *_: tmp_path / 'installer.lock')
    monkeypatch.setattr('runtime_protocol.platform.log_dir', lambda: tmp_path / 'logs')
    before = snapshot([backend.root / 'install.json', backend.service.selection, backend.root / 'current', realm / 'realm.sqlite3'])
    assert cli.main(['--root', str(backend.root), '--update']) == 1
    assert 'Astrid is busy (1 tasks running). Try again when they finish.' in capsys.readouterr().out
    assert snapshot([Path(p) for p in before]) == before
    assert calls == []


@pytest.mark.parametrize('system', ['linux', 'darwin', 'win32'])
@pytest.mark.parametrize('busy', [False, True])
def test_real_manager_methods_only_run_after_fenced_stop(tmp_path, monkeypatch, system, busy):
    from astrid.installer.service import Service
    backend, old, data, paths, realm, calls = realm_fixture(
        tmp_path, monkeypatch, system=system, blocker='running' if busy else None)
    publish(paths, realm)
    boundary = Boundary(paths, calls)
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    monkeypatch.setattr(backend.service, 'disable', lambda: Service.disable(backend.service))
    monkeypatch.setattr(backend.service, 'stop', lambda: Service.stop(backend.service))
    monkeypatch.setattr(backend.service, 'systemd', lambda: True)
    native = []

    def run(argv):
        # This is the sole OS seam: no development-host service is touched.
        native.append([str(part) for part in argv])
        assert 'runtime-stop' in calls
        with sqlite3.connect(realm / 'realm.sqlite3', timeout=0) as db:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                db.execute("INSERT INTO tasks VALUES ('race','render','running',NULL)")
        return 'Ready' if system == 'win32' else 'loaded'

    monkeypatch.setattr(backend.service, 'run', run)
    if busy:
        with pytest.raises(RuntimeOperationError):
            backend.stop(old, data)
        assert native == []
        assert calls == []
    else:
        backend.stop(old, data)
        assert calls.count('runtime-stop') == 1
        commands = ' '.join(' '.join(argv) for argv in native)
        assert {'linux': 'systemctl --user stop', 'darwin': 'launchctl bootout',
                'win32': 'Stop-ScheduledTask'}[system] in commands


def test_final_stop_holds_admission_until_first_signal(tmp_path, monkeypatch):
    import threading
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    publish(paths, realm)
    boundary = Boundary(paths, calls)
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    first_stop = threading.Event()
    claim_finished = threading.Event()
    claim_result = []

    def claim():
        assert first_stop.wait(5)
        try:
            with sqlite3.connect(realm / 'realm.sqlite3', timeout=0.05) as db:
                db.execute("INSERT INTO tasks VALUES ('concurrent','render','running',NULL)")
        except sqlite3.OperationalError as exc:
            claim_result.append(str(exc))
        finally:
            claim_finished.set()

    def stop_owner(**kwargs):
        first_stop.set()
        assert claim_finished.wait(5)
        calls.append('runtime-stop')
        assert claim_result == ['database is locked']
        return {'status': 'stopped'}

    boundary.stop_owner = stop_owner
    worker = threading.Thread(target=claim)
    worker.start()
    try:
        backend.stop(old, data)
    finally:
        worker.join(6)
    assert not worker.is_alive()
    assert calls == ['prepare', 'runtime-stop', 'disable', 'stop']
    with sqlite3.connect(realm / 'realm.sqlite3') as db:
        db.execute("INSERT INTO tasks VALUES ('after-stop','render','running',NULL)")


def test_replacement_before_manager_loses_stop_authority(tmp_path, monkeypatch):
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    publish(paths, realm)
    boundary = Boundary(paths, calls)
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    def stop_owner(**kwargs):
        calls.append('runtime-stop')
        replacement = json.loads(paths.discovery_path.read_text())
        replacement['runtime_instance_id'] = 'replacement'
        write_json(paths.discovery_path, replacement)
        return {'status': 'stopped'}
    boundary.stop_owner = stop_owner
    with pytest.raises(RuntimeOperationError) as raised:
        backend.stop(old, data)
    assert raised.value.stop_refused
    assert not raised.value.stop_completed
    assert raised.value.details == {'stop_authority_lost': True}
    assert calls == ['prepare', 'runtime-stop']
    assert json.loads(paths.discovery_path.read_text())['runtime_instance_id'] == 'replacement'


@pytest.mark.parametrize('key,value', [('owner','other'), ('program_root','/elsewhere'), ('data_root','/elsewhere')])
def test_service_record_must_belong_to_selected_install(tmp_path, monkeypatch, key, value):
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    publish(paths, realm)
    record = backend.service.recorded()
    write_json(backend.service.selection, {**record, key: value})
    with pytest.raises(RuntimeOperationError, match='does not match|belongs to another'):
        backend.stop(old, data)
    assert calls == []
    assert paths.discovery_path.exists()


def test_same_backend_keeps_runtime_stop_evidence_for_teardown_retry(tmp_path, monkeypatch):
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    publish(paths, realm)
    boundary = Boundary(paths, calls)
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    backend.stop(old, data)
    assert not paths.discovery_path.exists()
    boundary.process_birth_identity = lambda _pid: None
    backend.stop(old, data)
    assert calls.count('runtime-stop') == 1
    assert calls == ['prepare', 'runtime-stop', 'disable', 'stop', 'disable', 'stop']
