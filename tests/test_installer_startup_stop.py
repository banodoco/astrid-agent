"""Unpublished owners require a realm fence and exact retained launch custody."""
from __future__ import annotations

import subprocess
import sys
from unittest.mock import Mock

import pytest
from runtime_protocol import platform

from astrid.installer.core import RuntimeOperationError, write_json
from tests.test_installer_busy_fence import Boundary, publish, realm_fixture


def record_child(backend, data, realm, child, *, retained):
    identity = platform.process_identity(child.pid)
    record = backend.service.recorded()
    record['foreground'] = {'pid': child.pid, 'birth_id': identity['birth_id'],
                            'realm_id': 'selected', 'realm_root': str(realm)}
    write_json(backend.service.selection, record)
    if retained:
        backend.service._foreground_process = child


@pytest.mark.parametrize('stale_discovery', [False, True])
def test_unpublished_stop_requires_and_uses_actual_retained_child(tmp_path, monkeypatch, stale_discovery):
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    if stale_discovery:
        publish(paths, realm)
        record = __import__('json').loads(paths.discovery_path.read_text())
        record['pid'] = 2**31 - 1
        record['process_birth_id'] = 'absent'
        write_json(paths.discovery_path, record)
        write_json(paths.instance_lock_path, record)
    child = platform.spawn_tree([sys.executable, '-I', '-c', 'import time;time.sleep(60)'],
                                detached=True, stdin=subprocess.DEVNULL)
    try:
        record_child(backend, data, realm, child, retained=True)
        backend.stop(old, data)
        assert child.wait(timeout=5) != 0
        assert calls == ['disable', 'stop']
        assert not paths.discovery_path.exists()
    finally:
        if child.poll() is None:
            platform.stop_tree(child, grace=1)
        child.wait(timeout=5)


@pytest.mark.parametrize('stale_discovery', [False, True])
def test_external_child_without_custody_is_never_signaled(tmp_path, monkeypatch, stale_discovery):
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    if stale_discovery:
        publish(paths, realm)
        record = __import__('json').loads(paths.discovery_path.read_text())
        record['pid'] = 2**31 - 1
        record['process_birth_id'] = 'absent'
        write_json(paths.discovery_path, record)
        write_json(paths.instance_lock_path, record)
    child = subprocess.Popen([sys.executable, '-I', '-c', 'import time;time.sleep(60)'])
    try:
        record_child(backend, data, realm, child, retained=False)
        with pytest.raises(RuntimeOperationError, match='retained child'):
            backend.stop(old, data)
        assert child.poll() is None
        assert calls == []
    finally:
        child.terminate()  # Test owns this child; product code must leave it live.
        child.wait(timeout=5)


def test_busy_unpublished_child_stays_live_and_preserves_manager(tmp_path, monkeypatch):
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker='attempt')
    child = platform.spawn_tree([sys.executable, '-I', '-c', 'import time;time.sleep(60)'],
                                detached=True, stdin=subprocess.DEVNULL)
    try:
        record_child(backend, data, realm, child, retained=True)
        with pytest.raises(RuntimeOperationError) as raised:
            backend.stop(old, data)
        assert raised.value.details['task_count'] == 1
        assert child.poll() is None
        assert calls == []
        assert not paths.bootstrap_lock_path.exists()
    finally:
        if child.poll() is None:
            platform.stop_tree(child, grace=1)
        child.wait(timeout=5)


@pytest.mark.parametrize('publication', ['missing', 'malformed', 'changed-birth', 'live-lock'])
def test_uncertain_publication_never_authorizes_manager_or_fallback(tmp_path, monkeypatch, publication):
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    if publication != 'missing':
        publish(paths, realm)
    if publication == 'malformed':
        paths.discovery_path.write_text('{invalid')
    elif publication == 'changed-birth':
        boundary = Boundary(paths, calls)
        boundary.validate_owner = lambda **_: False
        monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    elif publication == 'live-lock':
        paths.discovery_path.unlink()
        boundary = Boundary(paths, calls)
        monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    with pytest.raises(RuntimeOperationError):
        backend.stop(old, data)
    assert not any(call in calls for call in ('disable', 'stop', 'stop_foreground'))


def test_legacy_service_foreground_entry_cannot_signal(tmp_path, monkeypatch):
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    # Invoke the class method because the matrix replaces manager methods.
    signal = Mock()
    monkeypatch.setattr(platform, 'signal_process', signal)
    from astrid.installer.service import Service
    with pytest.raises(RuntimeError, match='validated installer stop handoff'):
        Service.stop_foreground(backend.service)
    signal.assert_not_called()


@pytest.mark.parametrize('system', ['linux', 'darwin', 'win32'])
@pytest.mark.parametrize('related', [True, False])
def test_manager_target_readback_matches_selected_owner(tmp_path, monkeypatch, system, related):
    import json
    from astrid.installer.service import Service, command
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, system=system, blocker=None)
    publish(paths, realm)
    queries = []
    monkeypatch.setattr(backend.service, 'systemd', lambda: True)
    def run(argv):
        queries.append([str(arg) for arg in argv])
        if system == 'linux':
            return 'MainPID=' + ('123' if related else '456') + '\nFragmentPath=' + str(backend.service.path) + '\nLoadState=loaded'
        if system == 'darwin':
            return 'service = {\n pid = ' + ('123' if related else '456') + '\n}'
        argv = command(old, data, windows=True, log=backend.log, env_file=backend.service.env_file)
        return json.dumps([{'Execute': argv[0] if related else 'unrelated.exe',
                            'Arguments': subprocess.list2cmdline(argv[1:])}])
    monkeypatch.setattr(backend.service, 'run', run)
    if related:
        Service.validate_stop_target(backend.service, old, data, owner_pid=123)
    else:
        with pytest.raises(ValueError, match='does not match'):
            Service.validate_stop_target(backend.service, old, data, owner_pid=123)
    assert queries
    assert calls == []
    assert not any('bootout' in arg or arg in ('stop', 'disable') or 'Stop-ScheduledTask' in arg
                   for query in queries for arg in query)


def test_unrelated_manager_refuses_before_runtime_signal(tmp_path, monkeypatch):
    from astrid.installer.service import Service
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    publish(paths, realm)
    boundary = Boundary(paths, calls)
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    monkeypatch.setattr(backend.service, 'systemd', lambda: True)
    monkeypatch.setattr(backend.service, 'run', lambda *_: 'MainPID=456\nFragmentPath=/unrelated\nLoadState=loaded')
    monkeypatch.setattr(backend.service, 'validate_stop_target',
                        lambda *args, **kwargs: Service.validate_stop_target(backend.service, *args, **kwargs))
    with pytest.raises(RuntimeOperationError, match='does not match'):
        backend.stop(old, data)
    assert calls == []
    assert paths.discovery_path.exists()


def test_unavailable_systemd_is_not_idle_proof_for_registered_manager(tmp_path, monkeypatch):
    from astrid.installer.service import Service
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    monkeypatch.setattr(backend.service, 'systemd', lambda: False)
    with pytest.raises(ValueError, match='Cannot validate'):
        Service.validate_stop_target(backend.service, old, data, owner_pid=123)
    assert calls == []


@pytest.mark.parametrize('operation', ['disable', 'stop'])
def test_validated_systemd_unavailability_fails_instead_of_skipping_stop(tmp_path, monkeypatch, operation):
    from astrid.installer.service import Service
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    backend.service._validated_systemd_stop = True
    monkeypatch.setattr(backend.service, 'systemd', lambda: False)
    with pytest.raises(RuntimeError, match='no longer available'):
        getattr(Service, operation)(backend.service)
    assert calls == []


@pytest.mark.parametrize('prior,selected', [(None, 'sandbox'), ('strict', 'sandbox'), ('sandbox', 'strict')])
@pytest.mark.parametrize('busy', [False, True])
def test_stop_preserves_explicit_process_policy_and_defers_setting_write(tmp_path, monkeypatch, prior, selected, busy):
    from astrid.installer.backend import Backend
    from runtime_protocol import process_control
    from tests.test_installer_busy_fence import snapshot

    backend, old, data, paths, realm, calls = realm_fixture(
        tmp_path, monkeypatch, blocker='attempt' if busy else None)
    publish(paths, realm)
    if prior is not None:
        process_control.write_setting(paths.runtime_support, prior)
    monkeypatch.setattr(platform, '_process_control', None)
    chosen = Backend(backend.root, backend.log, backend.uv, process_control=selected)
    chosen.service = backend.service
    boundary = Boundary(paths, calls)
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    before = snapshot(data.rglob('*'))
    names_before = sorted(str(p) for p in data.rglob('*'))

    def manager_disable():
        # A mode migration has not been persisted before the validated stop.
        assert process_control.read_setting(paths.runtime_support) == (prior or 'strict')
        assert platform.process_control() == selected
        assert calls[-1] == 'runtime-stop'
        calls.append('disable')

    monkeypatch.setattr(chosen.service, 'disable', manager_disable)
    if busy:
        with pytest.raises(RuntimeOperationError) as raised:
            chosen.stop(old, data)
        assert raised.value.stop_refused
        assert raised.value.details['safe'] is False
        assert snapshot(data.rglob('*')) == before
        assert sorted(str(p) for p in data.rglob('*')) == names_before
        assert calls == []
    else:
        chosen.stop(old, data)
        assert calls == ['prepare', 'runtime-stop', 'disable', 'stop']
        assert process_control.read_setting(paths.runtime_support) == selected
    assert platform.process_control() == selected


@pytest.mark.parametrize('invalid', ['malformed', 'unknown', 'symlink'])
def test_explicit_policy_still_refuses_invalid_stored_metadata(tmp_path, monkeypatch, invalid):
    from astrid.installer.backend import Backend
    from runtime_protocol import process_control
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    publish(paths, realm)
    setting = process_control.setting_path(paths.runtime_support)
    if invalid == 'symlink':
        target = tmp_path / 'unrelated-setting'
        target.write_text('{"format_version": 1, "process_control": "strict"}')
        setting.symlink_to(target)
    else:
        setting.write_text('{' if invalid == 'malformed' else '{"format_version": 1, "process_control": "unknown"}')
        setting.chmod(0o600)
    before = setting.read_bytes()
    monkeypatch.setattr(platform, '_process_control', None)
    chosen = Backend(backend.root, backend.log, backend.uv, process_control='sandbox')
    chosen.service = backend.service
    with pytest.raises(RuntimeOperationError, match='process-control.json') as raised:
        chosen.stop(old, data)
    assert raised.value.stop_refused
    assert not raised.value.stop_completed
    assert calls == []
    assert setting.read_bytes() == before
    assert setting.is_symlink() == (invalid == 'symlink')


def test_completed_stop_retires_only_matching_foreground_record_before_candidate(tmp_path, monkeypatch):
    import json
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    publish(paths, realm)
    record = backend.service.recorded()
    record['foreground'] = {'pid': 123, 'birth_id': 'birth', 'realm_id': 'selected', 'realm_root': str(realm)}
    write_json(backend.service.selection, record)
    boundary = Boundary(paths, calls)
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    backend.stop(old, data)
    assert backend.service.recorded() == {key: value for key, value in record.items() if key != 'foreground'}
    assert backend.service._foreground_process is None
    boundary.process_birth_identity = lambda pid: None
    backend.stop(old, data)  # Same-controller retry needs no retained Popen.
    publish(paths, realm)
    for path in (paths.discovery_path, paths.instance_lock_path):
        value = json.loads(path.read_text())
        value.update(pid=456, process_birth_id='candidate-birth')
        write_json(path, value)
    boundary.process_birth_identity = lambda pid: None if pid == 123 else 'candidate-birth'
    backend.stop(old, data)  # The new publication receives its own validated stop.
    assert calls.count('runtime-stop') == 2


def test_manager_failure_after_owner_removes_advertisements_allows_installer_recovery(tmp_path, monkeypatch):
    from astrid.installer.core import Installer, read_json
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    publish(paths, realm)
    boundary = Boundary(paths, calls)

    def stopped_owner(**_kwargs):
        calls.append('runtime-stop')
        paths.discovery_path.unlink()
        paths.instance_lock_path.unlink()
        boundary.is_pid_alive = lambda _pid: False
        boundary.process_birth_identity = lambda _pid: None
        return {'status': 'stopped'}

    boundary.stop_owner = stopped_owner
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    attempts = []

    def manager_disable():
        attempts.append('disable')
        if len(attempts) == 1:
            raise RuntimeError('manager unavailable')
        calls.append('disable')

    monkeypatch.setattr(backend.service, 'disable', manager_disable)
    for operation in ('register', 'start', 'verify'):
        monkeypatch.setattr(backend, operation, lambda *_, name=operation, **__: calls.append(name))
    with pytest.raises(RuntimeOperationError, match='manager unavailable') as raised:
        Installer(backend.root, data, backend, say=lambda _: None).run('update', {})
    assert raised.value.stop_completed
    assert not raised.value.stop_refused
    assert calls == ['prepare', 'runtime-stop', 'disable', 'stop', 'register', 'start', 'verify']
    assert attempts == ['disable', 'disable']
    assert read_json(backend.root / 'update-transaction.json')['step'] == 'rolled_back'


def test_completed_stop_preserves_replaced_foreground_selection(tmp_path, monkeypatch):
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    publish(paths, realm)
    record = backend.service.recorded()
    record['foreground'] = {'pid': 123, 'birth_id': 'birth', 'realm_id': 'selected', 'realm_root': str(realm)}
    write_json(backend.service.selection, record)
    replacement = {**record, 'foreground': {**record['foreground'], 'pid': 456, 'birth_id': 'replacement'}}
    boundary = Boundary(paths, calls)
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    monkeypatch.setattr(backend.service, 'stop', lambda: write_json(backend.service.selection, replacement))
    backend.stop(old, data)
    assert backend.service.recorded() == replacement


@pytest.mark.parametrize('replacement', ['busy', 'corrupt'])
def test_early_stop_refusal_cannot_revive_receipt_after_replacement_disappears(tmp_path, monkeypatch, replacement):
    import sqlite3
    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    publish(paths, realm)
    boundary = Boundary(paths, calls)
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)
    backend.stop(old, data)
    calls.clear()
    if replacement == 'busy':
        publish(paths, realm)
        record = __import__('json').loads(paths.discovery_path.read_text())
        record.update(pid=456, process_birth_id='candidate-birth')
        write_json(paths.discovery_path, record)
        write_json(paths.instance_lock_path, record)
        with sqlite3.connect(realm / 'realm.sqlite3') as db:
            db.execute("INSERT INTO tasks VALUES ('new-task','render','running',NULL)")
    else:
        paths.discovery_path.write_text('{invalid')
    with pytest.raises(RuntimeOperationError):
        backend.stop(old, data)
    assert calls == []
    with sqlite3.connect(realm / 'realm.sqlite3') as db:
        db.execute('DELETE FROM tasks')
    paths.discovery_path.unlink(missing_ok=True)
    paths.instance_lock_path.unlink(missing_ok=True)
    boundary.is_pid_alive = lambda _pid: False
    boundary.process_birth_identity = lambda _pid: None
    with pytest.raises(RuntimeOperationError, match='no owner identity'):
        backend.stop(old, data)
    assert calls == []


@pytest.mark.parametrize('publication', ['discovery_path', 'instance_lock_path'])
def test_mutex_time_malformed_publication_revokes_completed_handoff_receipt(
        tmp_path, monkeypatch, publication):
    import importlib
    from contextlib import contextmanager
    from tests.test_installer_busy_fence import snapshot

    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    publish(paths, realm)
    boundary = Boundary(paths, calls)
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)

    # Establish stop authority through the complete Backend -> Runtime handoff.
    backend.stop(old, data)
    assert calls == ['prepare', 'runtime-stop', 'disable', 'stop']

    # The former owner is now positively absent. A retained receipt may support
    # teardown only while all publication observations remain trustworthy.
    boundary.is_pid_alive = lambda _pid: False
    boundary.process_birth_identity = lambda _pid: None
    calls.clear()
    watched = [*data.rglob('*'), backend.root / 'current', backend.root / 'install.json',
               backend.root / 'run', backend.service.selection, backend.service.path,
               paths.catalog_path, paths.discovery_path, paths.instance_lock_path]
    before = snapshot(watched)
    before_names = sorted(str(path) for path in data.rglob('*'))

    bootstrap = importlib.import_module('astrid_runtime.bootstrap')
    original_mutex = bootstrap._bootstrap_mutex
    advertisement = getattr(paths, publication)

    @contextmanager
    def malformed_at_mutex_entry(runtime_paths):
        # Backend preflight has already run; the malformed publication appears
        # immediately before the real bootstrap mutex is acquired.
        advertisement.write_text('{incomplete replacement')
        with original_mutex(runtime_paths):
            yield

    with monkeypatch.context() as race:
        race.setattr(bootstrap, '_bootstrap_mutex', malformed_at_mutex_entry)
        with pytest.raises(RuntimeOperationError, match='malformed') as first:
            backend.stop(old, data)
    assert first.value.stop_refused
    assert getattr(boundary, '_installer_stop_receipt_invalidated', False)
    assert calls == []

    advertisement.unlink()
    with pytest.raises(RuntimeOperationError, match='no owner identity') as retry:
        backend.stop(old, data)
    assert retry.value.stop_refused
    assert calls == []
    assert not any(call in calls for call in
                   ('runtime-stop', 'disable', 'stop', 'stop_foreground', 'unregister',
                    'register', 'start', 'restore', 'remove', 'backup', 'stage',
                    'upgrade', 'finish', 'open'))
    assert snapshot(watched) == before
    assert sorted(str(path) for path in data.rglob('*')) == before_names


@pytest.mark.parametrize(('lane', 'publication'), [
    ('stop', 'discovery_path'),
    ('preflight_stop', 'catalog_path'),
])
def test_agent_pre_handoff_validation_revokes_completed_receipt(
        tmp_path, monkeypatch, lane, publication):
    """A failed Agent observation after the early check must retire receipt authority."""
    import importlib
    from tests.test_installer_busy_fence import snapshot

    backend, old, data, paths, realm, calls = realm_fixture(tmp_path, monkeypatch, blocker=None)
    publish(paths, realm)
    boundary = Boundary(paths, calls)
    monkeypatch.setattr('astrid_runtime.runtime_boundary.LocalRuntimeBoundary', lambda: boundary)

    backend.stop(old, data)
    assert calls == ['prepare', 'runtime-stop', 'disable', 'stop']

    # Retained receipt reuse is only relevant once the stopped incarnation is
    # positively absent. The injected malformed file appears immediately after
    # the real early invalidation check and before the next Agent read.
    boundary.is_pid_alive = lambda _pid: False
    boundary.process_birth_identity = lambda _pid: None
    calls.clear()
    path = getattr(paths, publication)
    prior_bytes = path.read_bytes() if path.exists() else None
    watched = [*data.rglob('*'), backend.root / 'current', backend.root / 'install.json',
               backend.root / 'run', backend.service.selection, backend.service.path,
               paths.catalog_path, paths.discovery_path, paths.instance_lock_path]
    before = snapshot(watched)
    before_names = sorted(str(entry) for entry in data.rglob('*'))

    bootstrap = importlib.import_module('astrid_runtime.bootstrap')
    invalidate = bootstrap._invalidate_stale_stop_receipt

    def malformed_after_real_early_check(runtime_paths, retained_boundary):
        invalidate(runtime_paths, retained_boundary)
        path.write_text('{unknown replacement')

    with monkeypatch.context() as race:
        race.setattr(bootstrap, '_invalidate_stale_stop_receipt', malformed_after_real_early_check)
        with pytest.raises(RuntimeOperationError, match='malformed') as refusal:
            if lane == 'stop':
                backend.stop(old, data)
            else:
                backend.preflight_stop(data)

    assert refusal.value.stop_refused
    assert getattr(boundary, '_installer_stop_receipt_invalidated', False)
    assert calls == []

    # Remove the transient malformed publication and retry. The old receipt
    # must stay revoked; a retry cannot turn the earlier uncertain observation
    # into manager work, a signal, cleanup, rollback, or activation.
    if prior_bytes is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(prior_bytes)
    with pytest.raises(RuntimeOperationError) as retry:
        backend.stop(old, data)
    assert retry.value.stop_refused
    assert calls == []
    assert snapshot(watched) == before
    assert sorted(str(entry) for entry in data.rglob('*')) == before_names
