"""Shared installer transactions, including every update failure boundary."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from astrid.installer import core
from astrid.installer.core import Installer, Version, current, switch, write_json


def make_version(root: Path, name: str) -> Version:
    folder = root / 'versions' / name
    folder.mkdir(parents=True)
    value = Version(folder, folder / 'py/bin/python', folder / 'profile.json', {'PATH': '/tools/bin:/usr/bin:/bin'})
    value.python.parent.mkdir(parents=True)
    value.python.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
    write_json(folder / 'version.json', value.document())
    return value


class FakeBackend:
    def __init__(self, root, data, *, fail=None, existing=True):
        self.old = make_version(root, 'old') if existing else None
        if self.old:
            switch(root, self.old)
        self.candidate = make_version(root, 'new')
        self.data = data
        data.mkdir()
        self.sentinel = data / 'sentinel'
        self.sentinel.write_text('original')
        self.calls = []
        self.fail = fail
        self.failed = False
        self.legacy = None
        self.residue = []

    def step(self, name):
        self.calls.append(name)
        if self.fail == name and not self.failed:
            self.failed = True
            raise RuntimeError(name + ' failed')

    def existing(self, data):
        return self.legacy

    def has_workspace(self, data):
        return False

    def reinstallable(self, data):
        return False

    def workspace(self, old, data):
        self.step('workspace')
        return {'realm_id': 'same-realm', 'realm_root': str(data / 'realm')}

    def stop(self, version, data):
        self.step('stop')

    def backup(self, old, data, realm):
        self.step('backup')
        backup_root = old.root.parent.parent / 'backups' / 'test-backup'
        backup_root.mkdir(parents=True)
        return {'root': str(backup_root), 'kind': 'runtime', 'original': self.sentinel.read_text()}

    def stage(self, manifest, root, data):
        self.step('stage')
        return self.candidate

    def validate_candidate(self, version, data, realm):
        # The real backend returns None once the staged package is admitted.
        return None

    def upgrade(self, version, data):
        self.sentinel.write_text('migrated')
        self.step('upgrade')

    def create(self, version, data):
        self.step('create')
        return {'realm_id': 'same-realm'}

    def register(self, version, data):
        self.step('register-' + version.root.name)

    def start(self, version, data):
        self.step('start-' + version.root.name)

    def verify(self, version, data, realm_id, *, timeout=90):
        assert realm_id == 'same-realm'
        self.step('verify-' + version.root.name)

    def restart_required(self):
        self.step('restart-required')
        return False

    def finish(self, old, new, backup):
        self.step('finish')

    def restore(self, old, data, realm, backup):
        self.step('restore')
        self.sentinel.write_text(backup['original'])

    def open(self):
        self.step('open')

    def unregister(self):
        self.step('unregister')

    def preflight_remove(self, root, data, *, delete_data):
        self.step('preflight')

    def remove(self, root, data, *, delete_data):
        self.step('remove-data' if delete_data else 'remove-program')
        return self.residue


@pytest.fixture
def installation(tmp_path):
    root, data = tmp_path / 'program', tmp_path / 'data'
    backend = FakeBackend(root, data)
    lines = []
    return root, data, backend, Installer(root, data, backend, say=lines.append), lines


@pytest.mark.parametrize('failure', ['stop', 'backup', 'stage', 'upgrade', 'register-new', 'start-new', 'verify-new', 'finish'])
def test_update_rolls_back_each_failure_boundary(installation, failure):
    root, data, backend, installer, lines = installation
    backend.fail = failure
    with pytest.raises(RuntimeError, match=failure + ' failed'):
        installer.run('update', {})
    assert current(root) == backend.old
    assert backend.sentinel.read_text() == 'original'
    if failure == 'stage':
        # Stage and validate run while the old owner is still running, before
        # any stop: nothing was stopped, so nothing is restarted.
        assert backend.calls == ['workspace', 'stage']
        assert 'stop' not in backend.calls
        assert not {'register-old', 'start-old', 'verify-old'} & set(backend.calls)
    else:
        assert backend.calls[-3:] == ['register-old', 'start-old', 'verify-old']
    assert ('restore' in backend.calls) == (failure not in {'stop', 'backup', 'stage'})
    assert 'open' not in backend.calls
    assert not any(line.startswith('Open ') for line in lines)


def test_pointer_publish_failure_restores_data_before_restarting_old(installation, monkeypatch):
    root, data, backend, installer, lines = installation
    monkeypatch.setattr(core, 'switch', Mock(side_effect=OSError('pointer failed')))
    with pytest.raises(OSError, match='pointer failed'):
        installer.run('update', {})
    assert current(root) == backend.old
    assert backend.sentinel.read_text() == 'original'
    assert backend.calls[-4:] == ['restore', 'register-old', 'start-old', 'verify-old']


def test_success_keeps_old_version_and_opens_only_after_identity_verification(installation):
    root, data, backend, installer, lines = installation
    installer.run('update', {})
    assert current(root) == backend.candidate
    assert backend.old.root.is_dir()
    assert backend.calls == ['workspace', 'stage', 'stop', 'backup', 'upgrade', 'stop',
                             'register-new', 'start-new', 'verify-new', 'finish', 'open']
    assert lines[-1] == 'Open https://astrid.haus'


@pytest.mark.parametrize('mode', ['install', 'repair'])
def test_existing_install_is_idempotent_repair_without_data_or_tool_mutations(installation, mode):
    root, data, backend, installer, lines = installation
    before = backend.sentinel.stat().st_mtime_ns
    installer.run(mode, {})
    installer.run(mode, {})
    assert backend.calls == ['workspace', 'verify-old', 'restart-required', 'open'] * 2
    assert current(root) == backend.old
    assert backend.sentinel.read_text() == 'original'
    assert backend.sentinel.stat().st_mtime_ns == before


@pytest.mark.parametrize('reason', ['unhealthy', 'changed-settings'])
def test_repair_restarts_after_failed_verification_or_changed_service_settings(installation, reason):
    _, _, backend, installer, _ = installation

    def verify(version, data, realm_id, *, timeout=90):
        backend.calls.append('verify-' + version.root.name)
        if reason == 'unhealthy' and timeout == 3:
            raise core.VerificationError('browser door refused runtime_identity_mismatch')

    backend.verify = verify
    backend.restart_required = lambda: True

    installer.run('repair', {})

    assert backend.calls == [
        'workspace', 'verify-old', 'stop', 'register-old', 'start-old', 'verify-old', 'open'
    ]


@pytest.mark.parametrize('failure', [TypeError('bad call'), RuntimeError('bug'), KeyboardInterrupt()])
def test_repair_does_not_restart_after_unexpected_verification_failures(installation, failure):
    _, _, backend, installer, _ = installation
    backend.verify = Mock(side_effect=failure)

    with pytest.raises(type(failure)):
        installer.run('repair', {})

    assert backend.calls == ['workspace']


def test_first_install_creates_one_workspace_and_never_calls_upgrade(tmp_path):
    root, data = tmp_path / 'program', tmp_path / 'data'
    backend = FakeBackend(root, data, existing=False)
    Installer(root, data, backend, say=lambda line: None).run('install', {})
    assert current(root) == backend.candidate
    assert backend.calls == ['stage', 'create', 'register-new', 'start-new', 'verify-new', 'finish', 'open']


def test_unregistered_existing_data_is_never_overwritten(tmp_path):
    root, data = tmp_path / 'program', tmp_path / 'data'
    backend = FakeBackend(root, data, existing=False)
    backend.has_workspace = lambda path: True
    with pytest.raises(ValueError, match='existing workspace'):
        Installer(root, data, backend).run('install', {})
    assert backend.calls == []
    assert backend.sentinel.read_text() == 'original'


@pytest.mark.parametrize('delete_data', [False, True])
def test_uninstall_disables_and_unregisters_before_removal_and_reports_residue(installation, delete_data):
    root, data, backend, installer, lines = installation
    backend.residue = [root / 'locked.dll']
    installer.run('uninstall', {}, delete_data=delete_data)
    assert backend.calls == ['preflight', 'stop', 'unregister', 'remove-data' if delete_data else 'remove-program']
    assert f'Remaining: {root / "locked.dll"}' in lines
    assert (f'Workspace kept at {data}.' in lines) is (not delete_data)
    assert 'open' not in backend.calls


def test_failed_rollback_is_reported_and_does_not_claim_success(installation):
    root, data, backend, installer, lines = installation
    backend.fail = 'verify-new'
    backend.restore = Mock(side_effect=OSError('disk unavailable'))
    with pytest.raises(RuntimeError, match='recovery also failed.*disk unavailable'):
        installer.run('update', {})
    assert 'start-old' not in backend.calls
    assert 'open' not in backend.calls


def test_current_is_a_small_atomic_file_and_retains_previous_version(installation):
    root, data, backend, installer, lines = installation
    switch(root, backend.candidate)
    assert (root / 'current').read_text() == 'new\n'
    assert not (root / 'current').is_symlink()
    assert current(root) == backend.candidate
    assert backend.old.root.is_dir()
    (root / 'current').write_text('../outside')
    with pytest.raises(ValueError, match='identifier'):
        current(root)


def test_uninstall_preflight_failure_preserves_service_and_program(installation):
    root, data, backend, installer, lines = installation
    backend.fail = "preflight"
    with pytest.raises(RuntimeError, match="preflight failed"):
        installer.run("uninstall", {})
    assert backend.calls == ["preflight"]
    assert current(root) == backend.old
    assert backend.sentinel.read_text() == "original"
    assert lines == []
