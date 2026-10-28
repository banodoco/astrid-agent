"""The installer uses Runtime's C1 fence before touching a manager."""
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
    write_json(paths.source_profiles_dir / 'astrid.json', json.loads(profile.read_text(encoding='utf-8')))
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
    monkeypatch.setattr(value.service, 'publish_launch_admission', lambda *_args: None)
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
    watched = [*data.rglob('*'), backend.root / 'current', backend.root / 'install.json',
               backend.service.selection, backend.service.path]
    before = snapshot(watched)
    before_names = sorted(str(p) for p in data.rglob('*'))
    installer = Installer(backend.root, data, backend, say=lambda _: None)
    if operation == 'update':
        # C2 stages online. Only lifecycle signals and workspace changes are
        # forbidden on a busy refusal; preparing isolated program files is safe.
        candidate = Version(backend.root / 'versions' / 'candidate', old.python,
                            backend.root / 'versions' / 'candidate' / 'profile.json', {})
        monkeypatch.setattr(backend, 'stage', Mock(return_value=candidate))
        monkeypatch.setattr(backend, 'validate_candidate', Mock())
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
    from astrid.installer.core import is_lifecycle_refusal
    assert is_lifecycle_refusal(raised.value)
    assert raised.value.details is not None, (str(raised.value), repr(raised.value.__cause__))
    assert raised.value.details['task_count'] == 1
    assert str(raised.value).splitlines()[0] == 'Astrid is busy (1 tasks running). Try again when they finish.'
    assert raised.value.details['blockers'][0]['task_id'] == 'task-1'
    # The transaction may publish the shared launcher before C1 admission;
    # selected version, install identity, service definition, and workspace
    # stay unchanged on a busy refusal.
    assert snapshot(watched) == before
    assert sorted(str(p) for p in data.rglob('*')) == before_names
    assert calls == []
    if operation == 'update':
        backend.stage.assert_called_once()
        backend.validate_candidate.assert_called_once_with(candidate, data, {
            'realm_id': 'selected', 'realm_root': str(realm),
        })


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
    # Backend.stop is the real installer caller. Observe the Runtime shared
    # interruption primitive as well as its effects so this test fails if the
    # compatibility adapter grows a second fenced stop implementation.
    import importlib
    bootstrap = importlib.import_module('astrid_runtime.bootstrap')
    shared_calls = []
    fenced_stop_and_manager = bootstrap._fenced_stop_and_manager

    def observe_shared_stop(*args, **kwargs):
        shared_calls.append((args, kwargs))
        return fenced_stop_and_manager(*args, **kwargs)

    monkeypatch.setattr(bootstrap, '_fenced_stop_and_manager', observe_shared_stop)
    def manager_disable():
        calls.append('disable')
        with sqlite3.connect(realm / 'realm.sqlite3', timeout=0) as db:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                db.execute("INSERT INTO tasks VALUES ('raced','render','running',NULL)")
    monkeypatch.setattr(backend.service, 'disable', manager_disable)
    backend.stop(old, data)
    assert calls == ['prepare', 'runtime-stop', 'disable', 'stop']
    assert len(shared_calls) == 1
    assert any(callable(value) for value in shared_calls[0][1].values())
    assert not paths.discovery_path.exists()
    assert not paths.instance_lock_path.exists()


def assert_stopping_intent(paths, realm):
    # Runtime's C1 writes the tagged `stopping` form of instance.lock inside the
    # final fence, before the first signal, and leaves it there when the stop
    # fails. Assert that exact owner intent rather than the original bytes.
    lock = json.loads(paths.instance_lock_path.read_text(encoding='utf-8'))
    assert lock['state'] == 'stopping'
    assert set(lock['stopping']) == {'version', 'binding'}
    assert lock['pid'] == 123
    assert lock['process_birth_id'] == 'birth'
    assert lock['runtime_instance_id'] == 'instance'
    assert lock['realm_id'] == 'selected'
    assert lock['realm_root'] == str(realm)


@pytest.mark.parametrize('failure', [TimeoutError('timeout'), RuntimeError('identity changed')])
def test_unknown_owner_stop_failure_never_falls_back_to_manager(tmp_path, monkeypatch, failure):
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    publish(paths, realm)
    boundary = Boundary(paths, calls, failure=failure)
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    discovery_before = paths.discovery_path.read_bytes()
    with pytest.raises(RuntimeOperationError) as raised:
        backend.stop(old, data)
    assert raised.value.details is None
    assert calls == ['prepare', 'runtime-stop']
    assert paths.discovery_path.read_bytes() == discovery_before
    assert_stopping_intent(paths, realm)


def test_unknown_owner_stop_retry_succeeds_once_failure_clears(tmp_path, monkeypatch):
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    publish(paths, realm)
    boundary = Boundary(paths, calls, failure=TimeoutError('timeout'))
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    discovery_before = paths.discovery_path.read_bytes()
    with pytest.raises(RuntimeOperationError):
        backend.stop(old, data)
    assert calls == ['prepare', 'runtime-stop']
    assert paths.discovery_path.read_bytes() == discovery_before
    assert_stopping_intent(paths, realm)
    boundary.failure = None
    calls.clear()
    backend.stop(old, data)
    assert calls == ['prepare', 'runtime-stop', 'disable', 'stop']
    assert not paths.discovery_path.exists()
    assert not paths.instance_lock_path.exists()


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
    from astrid.installer.core import is_lifecycle_refusal
    assert is_lifecycle_refusal(raised.value) is (
        isinstance(error, dict) and isinstance(error.get('details'), dict)
        and error['details'].get('safe') is False
    )


def test_cli_busy_preflight_precedes_marker_and_selection_writes(tmp_path, monkeypatch, capsys):
    from astrid.installer import cli
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch)
    preflight = Mock(wraps=backend.preflight_stop)
    monkeypatch.setattr(backend, 'preflight_stop', preflight)
    monkeypatch.setattr(cli, 'Backend', lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(cli, 'installer_lock_path', lambda *_: tmp_path / 'installer.lock')
    monkeypatch.setattr('runtime_protocol.platform.log_dir', lambda: tmp_path / 'logs')
    before = snapshot([backend.root / 'install.json', backend.service.selection, backend.root / 'current', realm / 'realm.sqlite3'])
    assert cli.main(['--root', str(backend.root), '--update']) == 1
    assert 'Astrid is busy (1 tasks running). Try again when they finish.' in capsys.readouterr().out
    assert snapshot([Path(p) for p in before]) == before
    preflight.assert_called_once_with(data)
    assert calls == []


@pytest.mark.parametrize('claim_at', ['none', 'staging', 'final-fence'])
def test_cli_preflights_once_and_actual_stop_fences_staged_work(tmp_path, monkeypatch, capsys, claim_at):
    from astrid.installer import cli
    from tests.test_installer_cli_recovery import _valid_cli_manifest

    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    publish(paths, realm)
    boundary = Boundary(paths, calls)
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    monkeypatch.setattr(cli, 'Backend', lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(cli, 'installer_lock_path', lambda *_: tmp_path / 'installer.lock')
    monkeypatch.setattr('runtime_protocol.platform.log_dir', lambda: tmp_path / 'logs')
    monkeypatch.setattr(backend, 'source_checkout_ownership', lambda *_: False)
    monkeypatch.setattr(backend, 'has_workspace', lambda *_: True)
    monkeypatch.setattr('astrid.installer.core.install_launcher', lambda *_: None)
    marker_before = (backend.root / 'install.json').read_bytes()
    pointer_before = (backend.root / 'current').read_bytes()
    preflight = Mock(wraps=backend.preflight_stop)

    def entry_preflight(selected_data):
        assert calls == []
        assert (backend.root / 'install.json').read_bytes() == marker_before
        assert (backend.root / 'current').read_bytes() == pointer_before
        preflight(selected_data)
        calls.append('preflight')

    monkeypatch.setattr(backend, 'preflight_stop', entry_preflight)
    def claim():
        with sqlite3.connect(realm / 'realm.sqlite3') as db:
            db.execute("INSERT INTO tasks VALUES ('late-task','render','running',NULL)")

    if claim_at == 'final-fence':
        import runtime_protocol.lifecycle as lifecycle
        original_assert_safe = lifecycle.assert_interruption_safe

        def claim_after_last_read_only_check(selected_realm):
            report = original_assert_safe(selected_realm)
            claim()
            return report

        monkeypatch.setattr(lifecycle, 'assert_interruption_safe', claim_after_last_read_only_check)
    candidate_root = backend.root / 'versions' / 'candidate'
    candidate_root.mkdir()
    candidate = Version(candidate_root, old.python, candidate_root / 'profile.json', {})

    def stage(*_args):
        assert calls == ['preflight']
        calls.append('stage')
        if claim_at == 'staging':
            claim()
        return candidate

    monkeypatch.setattr(backend, 'stage', stage)
    monkeypatch.setattr(backend, 'validate_candidate', lambda *_: calls.append('validate'))
    # End the idle case after the real old-owner stop. The sentinel backup
    # failure exercises rollback's additional C1 stop without another preflight.
    def fail_backup(*_args):
        calls.append('backup')
        raise RuntimeError('backup sentinel')
    monkeypatch.setattr(backend, 'backup', fail_backup)
    monkeypatch.setattr(backend, 'start', lambda *_: calls.append('restart-old'))
    monkeypatch.setattr(backend, 'register', lambda *_: None)
    monkeypatch.setattr(backend, 'verify', lambda *_args, **_kwargs: None)
    manifest = tmp_path / 'manifest.json'
    write_json(manifest, _valid_cli_manifest(1))

    assert cli.main(['--root', str(backend.root), '--update', '--manifest', str(manifest)]) == 1
    preflight.assert_called_once_with(data)
    assert calls[:3] == ['preflight', 'stage', 'validate']
    assert (backend.root / 'current').read_bytes() == pointer_before
    if claim_at != 'none':
        assert calls == ['preflight', 'stage', 'validate'] + (['prepare'] if claim_at == 'final-fence' else [])
        assert 'Astrid is busy (1 tasks running)' in capsys.readouterr().out
        assert paths.discovery_path.exists()
        assert paths.instance_lock_path.exists()
    else:
        assert calls.count('runtime-stop') == 1
        assert calls.index('runtime-stop') < calls.index('disable') < calls.index('backup')
        # Rollback must go through C1 again, even though entry was already safe.
        assert calls.count('disable') == 2
        assert calls.count('stop') == 2


@pytest.mark.parametrize('operation', ['repair', 'uninstall'])
def test_cli_repair_and_uninstall_preflight_once_before_stop_admission(tmp_path, monkeypatch, operation):
    from astrid.installer import cli
    from tests.test_installer_cli_recovery import _valid_cli_manifest

    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    monkeypatch.setattr(cli, 'Backend', lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(cli, 'installer_lock_path', lambda *_: tmp_path / 'installer.lock')
    monkeypatch.setattr('runtime_protocol.platform.log_dir', lambda: tmp_path / 'logs')
    monkeypatch.setattr(backend, 'source_checkout_ownership', lambda *_: False)
    monkeypatch.setattr('astrid.installer.core.install_launcher', lambda *_: None)
    preflight = Mock(wraps=backend.preflight_stop)
    monkeypatch.setattr(backend, 'preflight_stop', preflight)
    admission = Mock(side_effect=lambda *_: calls.append('publish-admission'))
    monkeypatch.setattr(backend, 'publish_launch_admission', admission)
    # A real C1 stop occurs before this intentional end-of-operation failure.
    monkeypatch.setattr(backend, 'register', Mock(side_effect=RuntimeError('after-stop sentinel')))
    monkeypatch.setattr(backend, 'unregister', Mock(side_effect=RuntimeError('after-stop sentinel')))
    args = ['--root', str(backend.root)]
    if operation == 'uninstall':
        args.append('--uninstall')
    else:
        manifest = tmp_path / 'manifest.json'
        write_json(manifest, _valid_cli_manifest(0))
        args.extend(['--manifest', str(manifest)])

    assert cli.main(args) == 1
    preflight.assert_called_once_with(data)
    admission.assert_called_once_with(old, data)
    assert calls == ['publish-admission', 'disable', 'stop']


@pytest.mark.parametrize('system', ['linux', 'darwin', 'win32'])
@pytest.mark.parametrize('busy', [False, True])
def test_real_manager_methods_only_run_after_fenced_stop(tmp_path, monkeypatch, system, busy):
    from astrid.installer.service import LABEL, Service, command, platform, render
    backend, old, data, paths, realm, calls = realm_fixture(
        tmp_path, monkeypatch, system=system, blocker='running' if busy else None)
    publish(paths, realm)
    boundary = Boundary(paths, calls)
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    monkeypatch.setattr(backend.service, 'disable', lambda: Service.disable(backend.service))
    monkeypatch.setattr(backend.service, 'stop', lambda: Service.stop(backend.service))
    monkeypatch.setattr(backend.service, 'systemd', lambda: True)
    if system == 'darwin':
        backend.service.path.write_text(render('darwin', old, data, backend.log), encoding='utf-8')
        monkeypatch.setattr(backend.service, 'validate_stop_target',
                            lambda *args, **kwargs: Service.validate_stop_target(backend.service, *args, **kwargs))
    native = []

    def run(argv):
        # This is the sole OS seam: no development-host service is touched.
        native.append([str(part) for part in argv])
        if system == 'darwin' and argv[:2] == ['launchctl', 'print']:
            action = command(old, data)
            running = 'runtime-stop' not in calls
            return (f'gui/{platform.current_user()}/{LABEL} = {{\n'
                    f'\tpath = {backend.service.path}\n'
                    f'\tstate = {"running" if running else "not running"}\n'
                    f'\tprogram = {action[0]}\n\targuments = {{\n'
                    + '\n'.join(action) + '\n\t}\n'
                    + ('\tpid = 123\n' if running else '') + '}')
        assert 'runtime-stop' in calls
        with sqlite3.connect(realm / 'realm.sqlite3', timeout=0) as db:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                db.execute("INSERT INTO tasks VALUES ('race','render','running',NULL)")
        return 'Ready' if system == 'win32' else 'loaded'

    monkeypatch.setattr(backend.service, 'run', run)
    if busy:
        with pytest.raises(RuntimeOperationError):
            backend.stop(old, data)
        assert all(argv[:2] == ['launchctl', 'print'] for argv in native)
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
    from astrid.installer.core import is_lifecycle_refusal
    assert not is_lifecycle_refusal(raised.value)
    assert calls == ['prepare', 'runtime-stop']
    assert json.loads(paths.discovery_path.read_text())['runtime_instance_id'] == 'replacement'


def test_fresh_backend_no_publication_runs_c1_before_manager_callback(tmp_path, monkeypatch):
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    boundary = Boundary(paths, calls)
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)

    # A newly constructed controller has no process-local stop receipt or
    # knowledge of a prior owner. C1 still fences the selected readable realm.
    fresh = Backend(backend.root, backend.log, backend.uv)
    fresh.service.selection = backend.service.selection
    fresh.service.system = 'linux'
    fresh.service.disable = lambda: calls.append('disable')
    fresh.service.stop = lambda: calls.append('stop')
    fresh.service.login_service_exists = lambda: True
    fresh.service.validate_stop_target = lambda *_args, **_kwargs: None
    fresh.stop(old, data)

    assert calls == ['disable', 'stop']
    assert 'runtime-stop' not in calls
    assert not paths.discovery_path.exists()
    assert not paths.instance_lock_path.exists()


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


@pytest.mark.parametrize('outcome', ['busy', 'late_busy', 'replacement', 'unknown', 'timeout', 'malformed', 'success'])
def test_pinokio_real_c1_reports_stop_to_bound_controller_only_after_success(tmp_path, monkeypatch, outcome):
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from astrid.installer.app_update import PinokioLifecycle

    backend, old, data, paths, realm, calls = realm_fixture(
        tmp_path, monkeypatch, blocker='running' if outcome == 'busy' else None)
    backend.managed_by = 'pinokio'
    publish(paths, realm)
    boundary = Boundary(paths, calls, failure=(TimeoutError('stop timeout') if outcome == 'timeout'
                                              else RuntimeError('unknown stop') if outcome == 'unknown' else None))
    if outcome == 'late_busy':
        bootstrap = importlib.import_module('astrid_runtime.bootstrap')
        original_fenced = bootstrap._fenced_stop_and_manager
        def inject_busy(*args, **kwargs):
            with sqlite3.connect(realm / 'realm.sqlite3') as db:
                db.execute("INSERT INTO tasks VALUES ('late','render','running',NULL)")
            return original_fenced(*args, **kwargs)
        monkeypatch.setattr(bootstrap, '_fenced_stop_and_manager', inject_busy)
    if outcome == 'replacement':
        def stop_owner(**_kwargs):
            calls.append('runtime-stop')
            record = json.loads(paths.discovery_path.read_text())
            record['runtime_instance_id'] = 'replacement'
            write_json(paths.discovery_path, record)
            return {'status': 'stopped'}
        boundary.stop_owner = stop_owner
    if outcome == 'malformed':
        paths.discovery_path.write_text('{invalid')
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    control = backend.root / 'pinokio-control.json'
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
            assert body == b'{"c1":"stopped"}'  # byte-exact data, never a command
            requests.append((self.path, json.loads(body)))
            # C1 completed and released its fence before the report arrives.
            assert calls == ['prepare', 'runtime-stop']
            control.unlink()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    write_json(control, {'schema': 1, 'pid': os.getpid(), 'port': server.server_port, 'token': 'a' * 64})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    lifecycle = object.__new__(PinokioLifecycle)
    lifecycle.root, lifecycle.backend = backend.root, backend
    try:
        if outcome == 'success':
            lifecycle.stop(old, data)
            assert requests == [('/stop', {'c1': 'stopped'})]
            assert calls == ['prepare', 'runtime-stop']
            assert not control.exists()
        else:
            with pytest.raises(RuntimeOperationError):
                lifecycle.stop(old, data)
            assert requests == []
            assert control.exists()
            if outcome == 'busy':
                with sqlite3.connect(realm / 'realm.sqlite3') as db:
                    db.execute("DELETE FROM tasks")
                lifecycle.stop(old, data)
                assert requests == [('/stop', {'c1': 'stopped'})]
                assert not control.exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("failure", ["drain", "timeout"])
def test_pinokio_controller_failure_retries_ownerless_through_fresh_c1(tmp_path, monkeypatch, failure):
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from astrid.installer.app_update import PinokioLifecycle

    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    backend.managed_by = 'pinokio'
    publish(paths, realm)
    boundary = Boundary(paths, calls)
    def stop_owner(**_kwargs):
        calls.append('runtime-stop')
        paths.discovery_path.unlink()
        paths.instance_lock_path.unlink()
        return {'status': 'stopped'}
    boundary.stop_owner = stop_owner
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    control = backend.root / 'pinokio-control.json'
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
            requests.append(json.loads(body))
            if failure == 'drain' and len(requests) == 1:
                self.send_response(500); self.end_headers(); self.wfile.write(b'{"ok":false,"errors":["drain failed"]}'); return
            if failure == 'timeout' and len(requests) == 1:
                import time
                time.sleep(0.4)
                return
            control.unlink()
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    write_json(control, {'schema': 1, 'pid': os.getpid(), 'port': server.server_port, 'token': 'a' * 64})
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    if failure == 'timeout':
        from astrid.installer import app_update
        connection_type = app_update.http.client.HTTPConnection
        timed_out = False
        class Connection:
            def __init__(self, *args, **kwargs):
                self.inner = connection_type(*args, **kwargs)
            def request(self, method, *args, **kwargs):
                nonlocal timed_out
                if method == 'POST' and not timed_out:
                    timed_out = True
                    self.inner.timeout = 0.1
                self.inner.request(method, *args, **kwargs)
            def getresponse(self): return self.inner.getresponse()
            def close(self): self.inner.close()
        monkeypatch.setattr(app_update.http.client, 'HTTPConnection', Connection)
    def lifecycle(value):
        result = object.__new__(PinokioLifecycle)
        result.root, result.backend = value.root, value
        return result
    try:
        with pytest.raises(RuntimeOperationError, match='drain failed|timed out'):
            lifecycle(backend).stop(old, data)
        assert control.exists() and not paths.discovery_path.exists()
        # A retry never replays the earlier report: it needs a fresh C1 first.
        fresh = Backend(backend.root, tmp_path / 'fresh.log', tmp_path / 'uv', managed_by='pinokio')
        fresh.service.selection = backend.service.selection
        lifecycle(fresh).stop(old, data)
        assert requests == [{'c1': 'stopped'}, {'c1': 'stopped'}]
        assert calls == ['prepare', 'runtime-stop']
        assert not control.exists()
    finally:
        server.shutdown(); server.server_close(); thread.join()
