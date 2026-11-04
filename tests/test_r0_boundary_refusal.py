"""The legacy update must stay inert while the offline producer is unavailable."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer import core, local_update, update_job


# Captured from action 41625801442, before-cleanup/unit-file-before-cleanup.txt.
CAPTURES = Path(__file__).parent / 'fixtures' / 'r0_boundary_captures'


def manifest(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def version(root, release=0, managed_by=None):
    root.mkdir(parents=True, exist_ok=True)
    python = root / 'python'
    python.write_text('interpreter')
    profile = root / 'profile.json'
    profile.write_text('{}')
    return core.Version(root, python, profile, {}, release=release, managed_by=managed_by)


@pytest.mark.parametrize('capture', [CAPTURES / name for name in ('linux.txt', 'macos.txt', 'windows.txt')],
                         ids=lambda p: p.stem)
def test_legacy_transaction_refuses_without_touching_captured_service_or_data(tmp_path, capture):
    service = tmp_path / 'service'
    service.write_bytes(capture.read_bytes())
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'owner.lock').write_bytes(b'opaque lock bytes')
    (data / 'realm.sqlite3').write_bytes(b'original user data')
    old = version(tmp_path / 'program' / 'versions' / 'r0')
    before = manifest(tmp_path)
    calls = []
    installer = core.Installer(tmp_path / 'program', data, SimpleNamespace())
    with pytest.raises(ValueError, match='adoption boundary'):
        installer._run_update_transaction(old, {}, prepare=lambda: calls.append('prepare'),
            stop=lambda *a: calls.append('stop'), switch_candidate=lambda *a: calls.append('switch'),
            switch_old=lambda *a: calls.append('rollback'), activate=lambda *a: calls.append('activate'),
            activate_old=lambda *a: calls.append('activate old'))
    assert calls == []
    assert manifest(tmp_path) == before


@pytest.mark.parametrize('step', ['staged', 'stopping', 'stopped', 'backed_up', 'migrated', 'switched', 'recovering'])
def test_interrupted_legacy_transaction_refuses_before_recovery_effects(tmp_path, step):
    root, data = tmp_path / 'program', tmp_path / 'data'
    data.mkdir()
    (data / 'owner.lock').write_bytes(b'opaque')
    (data / 'realm.sqlite3').write_bytes(b'R0')
    old = version(root / 'versions' / 'r0')
    candidate = version(root / 'versions' / 'r1', release=1)
    transaction = {'format': 'astrid-install-update/v1', 'operation_id': 'a' * 32,
                   'step': step, 'data_root': str(data), 'old': old.document(),
                   'candidate': candidate.document(), 'realm': {'realm_root': str(data)},
                   'transaction_kind': 'migration'}
    (root / 'update-transaction.json').write_text(json.dumps(transaction))
    before = manifest(tmp_path)
    calls = []
    with pytest.raises(ValueError, match='adoption boundary'):
        core.recover_transaction(root, data, SimpleNamespace(),
            stop=lambda *a: calls.append('stop'), switch_old=lambda *a: calls.append('switch'),
            activate_old=lambda *a: calls.append('activate'))
    assert calls == []
    assert manifest(tmp_path) == before


def test_unknown_existing_selection_refused_without_opening_data(tmp_path):
    root, data = tmp_path / 'program', tmp_path / 'data'
    root.mkdir()
    data.mkdir()
    (root / 'install.json').write_text('{}')
    (data / 'owner.lock').write_bytes(b'opaque')
    before = manifest(tmp_path)
    with pytest.raises(ValueError, match='cannot be identified'):
        core.require_known_update_selection(root, data, None)
    assert manifest(tmp_path) == before


def test_local_selection_refuses_before_source_discovery(tmp_path, monkeypatch):
    root, data = tmp_path / 'program', tmp_path / 'data'
    old = version(root / 'versions' / 'r0')
    marker = {'owner': local_update.LABEL, 'data_root': str(data)}
    (root / 'install.json').write_text(json.dumps(marker))
    record = {**marker, 'program_root': str(root)}
    monkeypatch.setattr(local_update, 'current', lambda _: old)
    backend = SimpleNamespace(service=SimpleNamespace(recorded=lambda: record),
                              source_checkout_ownership=lambda *a: pytest.fail('discovery invoked'))
    with pytest.raises(ValueError, match='adoption boundary'):
        local_update._selection(root, backend)


def test_job_request_refuses_before_live_identity(tmp_path, monkeypatch):
    root, data = tmp_path / 'program', tmp_path / 'data'
    old = version(root / 'versions' / 'r0')
    marker = {'owner': update_job.LABEL, 'data_root': str(data)}
    (root / 'install.json').write_text(json.dumps(marker))
    selected = tmp_path / 'selection.json'
    selected.write_text(json.dumps({**marker, 'program_root': str(root)}))
    monkeypatch.setattr(update_job, '_selection_path', lambda: selected)
    monkeypatch.setattr(core, 'current', lambda _: old)
    request = {'operation_id': 'a' * 32, 'root': str(root), 'data_root': str(data),
               'selection_path': str(selected)}
    with pytest.raises(ValueError, match='adoption boundary'):
        update_job._validate_request(request, root)


def test_nonlegacy_and_pinokio_versions_are_not_rejected(tmp_path):
    core.require_boundary_available(version(tmp_path / 'r1', release=1))
    core.require_boundary_available(version(tmp_path / 'pinokio', managed_by='pinokio'))


def test_maintenance_refuses_before_inventorying_r0(tmp_path, monkeypatch):
    from astrid.installer import maintenance
    root, data = tmp_path / 'program', tmp_path / 'data'
    data.mkdir()
    old = version(root / 'versions' / 'r0')
    monkeypatch.setattr(maintenance, '_validate_paths', lambda *a: None)
    monkeypatch.setattr(maintenance, '_load_owned_marker', lambda *a: {})
    monkeypatch.setattr(maintenance, '_selected_version', lambda *a: old)
    monkeypatch.setattr(maintenance, '_tree_manifest', lambda *a: pytest.fail('data inventoried'))
    with pytest.raises(maintenance.MaintenanceRefusal, match='data boundary'):
        maintenance.plan_update_recovery(root, data)


def test_login_transaction_rejects_pinokio_version_before_callbacks(tmp_path):
    old = version(tmp_path / 'program' / 'versions' / 'r0', managed_by='pinokio')
    calls = []
    installer = core.Installer(tmp_path / 'program', tmp_path / 'data', SimpleNamespace())
    with pytest.raises(ValueError, match='installation manager'):
        installer._run_update_transaction(old, {}, prepare=lambda: calls.append('prepare'),
            stop=lambda *a: calls.append('stop'), switch_candidate=lambda *a: calls.append('switch'),
            switch_old=lambda *a: calls.append('rollback'), activate=lambda *a: calls.append('activate'),
            activate_old=lambda *a: calls.append('activate old'))
    assert calls == []


def test_login_selection_refuses_mismatched_manager_before_discovery(tmp_path, monkeypatch):
    root, data = tmp_path / 'program', tmp_path / 'data'
    old = version(root / 'versions' / 'r0', managed_by='pinokio')
    marker = {'owner': local_update.LABEL, 'data_root': str(data)}
    (root / 'install.json').write_text(json.dumps(marker))
    record = {**marker, 'program_root': str(root)}
    monkeypatch.setattr(local_update, 'current', lambda _: old)
    backend = SimpleNamespace(service=SimpleNamespace(recorded=lambda: record),
                              source_checkout_ownership=lambda *a: pytest.fail('discovery invoked'))
    with pytest.raises(ValueError, match='installation manager'):
        local_update._selection(root, backend)


def test_job_request_refuses_mismatched_manager_before_live_identity(tmp_path, monkeypatch):
    root, data = tmp_path / 'program', tmp_path / 'data'
    old = version(root / 'versions' / 'r0', managed_by='pinokio')
    marker = {'owner': update_job.LABEL, 'data_root': str(data)}
    (root / 'install.json').write_text(json.dumps(marker))
    selected = tmp_path / 'selection.json'
    selected.write_text(json.dumps({**marker, 'program_root': str(root)}))
    monkeypatch.setattr(update_job, '_selection_path', lambda: selected)
    monkeypatch.setattr(core, 'current', lambda _: old)
    request = {'operation_id': 'a' * 32, 'root': str(root), 'data_root': str(data),
               'selection_path': str(selected)}
    with pytest.raises(ValueError, match='installation manager'):
        update_job._validate_request(request, root)


def test_dangling_selection_is_not_treated_as_fresh(tmp_path):
    root = tmp_path / 'program'
    root.mkdir()
    (root / 'current').symlink_to(root / 'missing')
    with pytest.raises(ValueError, match='cannot be identified'):
        core.require_known_update_selection(root, tmp_path / 'data', None)


def test_fallback_legacy_version_refused_before_repair(tmp_path, monkeypatch):
    root, data = tmp_path / 'program', tmp_path / 'data'
    old = version(tmp_path / 'other' / 'r0')
    backend = SimpleNamespace(existing=lambda _: old,
                              workspace=lambda *a: pytest.fail('workspace inspected'))
    monkeypatch.setattr(core.Installer, '_recover_update', lambda *_: None)
    installer = core.Installer(root, data, backend)
    with pytest.raises(ValueError, match='adoption boundary'):
        installer._run_locked('install', {})


def test_login_recovery_rejects_mismatched_journal_before_replay(tmp_path):
    old = version(tmp_path / 'r0', managed_by='pinokio')
    with pytest.raises(ValueError, match='installation manager'):
        core.require_recovery_boundary({'old': old.document()}, login_managed=True)
    # The Pinokio recovery path keeps its original manager handling.
    core.require_recovery_boundary({'old': old.document()})


@pytest.mark.parametrize('route', ['recover', 'rollback', 'installer', 'local'])
@pytest.mark.parametrize('marker', [None, {'owner': 'haus.astrid.runtime'},
                                   {'owner': 'haus.astrid.runtime', 'managed_by': 'pinokio',
                                    'data_root': '/different/workspace'}])
def test_journal_cannot_claim_pinokio_to_bypass_login_recovery(tmp_path, route, marker):
    root, data = tmp_path / 'program', tmp_path / 'data'
    data.mkdir()
    (data / 'owner.lock').write_bytes(b'opaque')
    (data / 'realm.sqlite3').write_bytes(b'R0')
    old = version(root / 'versions' / 'r0', managed_by='pinokio')
    transaction = {'format': 'astrid-install-update/v1', 'operation_id': 'a' * 32,
                   'step': 'stopping', 'data_root': str(data), 'old': old.document(),
                   'realm': {'realm_root': str(data)}, 'transaction_kind': 'migration',
                   'transaction_context': {'owner': 'local-update'} if route == 'local' else {}}
    (root / 'update-transaction.json').write_text(json.dumps(transaction))
    if marker is not None:
        (root / 'install.json').write_text(json.dumps(marker))
    before = manifest(tmp_path)
    calls = []
    callbacks = dict(stop=lambda *a: calls.append('stop'),
                     switch_old=lambda *a: calls.append('switch'),
                     activate_old=lambda *a: calls.append('activate'))
    with pytest.raises(ValueError, match='installation manager'):
        if route == 'recover':
            core.recover_transaction(root, data, SimpleNamespace(), **callbacks)
        elif route == 'rollback':
            core.rollback_transaction(root, data, SimpleNamespace(), transaction,
                                      save=lambda *a, **k: calls.append('save'), **callbacks)
        elif route == 'installer':
            core.Installer(root, data, SimpleNamespace())._recover_update()
        else:
            local_update._recover(root, SimpleNamespace(), SimpleNamespace(), say=print)
    assert calls == []
    assert manifest(tmp_path) == before


def test_pinokio_recovery_manager_requires_matching_workspace(tmp_path):
    root, data = tmp_path / 'program', tmp_path / 'data'
    old = version(root / 'versions' / 'r0', managed_by='pinokio')
    (root / 'install.json').write_text(json.dumps({'owner': 'haus.astrid.runtime',
        'managed_by': 'pinokio', 'data_root': str(data)}))
    core.require_recovery_manager(root, data, {'old': old.document()})
