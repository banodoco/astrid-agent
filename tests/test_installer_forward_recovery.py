"""A broken prior service must not trap update retries in destructive rollback."""
from pathlib import Path

import pytest

from astrid.installer.core import Installer, Version, read_json, switch, write_json
from tests.test_installer_lifecycle import LifecycleBackend


def failed_update(tmp_path):
    from runtime_protocol import platform

    root, data = tmp_path / 'program', tmp_path / 'data'
    platform.private_directory(root)
    platform.private_directory(data)
    folder = root / 'versions' / 'old'
    platform.private_directory(folder)
    python = folder / 'python'
    python.write_text('retained interpreter')
    old = Version(folder, python, folder / 'profile.json', {})
    write_json(folder / 'version.json', old.document())
    switch(root, old)
    backend = LifecycleBackend(root, data)
    backend.old = old
    backend.has_workspace = lambda _data: True
    backend.upgrade = lambda *_args: (_ for _ in ()).throw(RuntimeError('candidate rejected legacy host'))
    backend.restore = lambda *_args: backend.calls.append(('restore',))
    backend.verify = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('old POSIX host census fails'))
    return Installer(root, data, backend, say=lambda _: None), backend


def test_failed_candidate_and_broken_old_stop_automatic_restore_loop(tmp_path):
    installer, backend = failed_update(tmp_path)
    with pytest.raises(RuntimeError, match='recovery also failed'):
        installer.run('update', {})
    transaction = read_json(installer.root / 'update-transaction.json')
    assert transaction['step'] == 'recovery_required'
    assert transaction['data_restored'] is True
    assert transaction['service_healthy'] is False
    assert 'old POSIX' in transaction['recovery_error']
    (installer.data / 'newer-user-work').write_text('after backup')
    before = list(backend.calls)
    with pytest.raises(ValueError, match='--recover prepare'):
        installer.run('update', {})
    assert backend.calls == before
    assert (installer.data / 'newer-user-work').read_text() == 'after backup'


def test_interrupted_rollback_failure_is_durable_and_not_retried(tmp_path):
    installer, backend = failed_update(tmp_path)
    old = backend.old
    write_json(installer.root / 'update-transaction.json', {
        'format': 'astrid-install-update/v1', 'operation_id': 'a' * 32,
        'data_root': str(installer.data), 'old': old.document(), 'candidate': None,
        'realm': backend.workspace(), 'backup': None, 'step': 'recovering',
    })
    with pytest.raises(RuntimeError, match='recovery'):
        installer._recover_update()
    assert read_json(installer.root / 'update-transaction.json')['step'] == 'recovery_required'
    before = list(backend.calls)
    with pytest.raises(ValueError, match='--recover prepare'):
        installer._recover_update()
    assert backend.calls == before


@pytest.fixture
def recovery_fixture(tmp_path, monkeypatch):
    """Real installer/disk evidence and realm; native OS boundaries are injected."""
    import shutil
    from types import SimpleNamespace

    from astrid_runtime.paths import RuntimePaths
    from runtime_protocol import platform
    from runtime_protocol.store import RealmStore
    from runtime_protocol.upgrade import inspect_canonical_schema

    from astrid.core.execution import boot_identity
    from astrid.installer import recovery
    from astrid.sdk import host_bootstrap

    installer, backend = failed_update(tmp_path)
    with pytest.raises(RuntimeError, match='recovery also failed'):
        installer.run('update', {})
    tx = read_json(installer.root / 'update-transaction.json')
    candidate, old = Version.load(tx['candidate']), backend.old
    # Use the real support-directory factory before realm publication: POSIX
    # chmod does not establish the protected DACL required on Windows.
    RuntimePaths.current(data_root=installer.data).ensure_support_dirs()
    realm = Path(tx['realm']['realm_root'])
    # Runtime publishes its staged canonical directory at an absent name.
    RealmStore.initialize(realm, realm_id=tx['realm']['realm_id']).close()
    (realm / 'new-user-data').write_text('newer than retained backup')
    support = installer.data / 'runtime'
    write_json(support / 'catalog.json', {'selected_realm_id': tx['realm']['realm_id'], 'realms': [
        {'realm_id': tx['realm']['realm_id'], 'data_root': str(realm)}]})
    write_json(support / 'generic-host.json', {'legacy': True})
    write_json(support / 'generic-host.ready.json', {'legacy': True})
    for name in recovery.MARKERS:
        platform.owner_only(support / name)
    write_json(installer.root / 'install.json', {'owner': 'haus.astrid.runtime', 'data_root': str(installer.data)})
    backup = Path(tx['backup']['root'])
    platform.private_directory(backup)
    write_json(backup / 'backup.json', tx['backup'])
    (backup / 'old-user-data').write_text('older backup')
    for version in (old, candidate):
        runtime = version.root / 'r'
        platform.private_directory(runtime)
        (runtime / 'bootstrap.py').write_text('retained runtime code')
        write_json(version.profile, {'runtime_checkout': str(runtime)})
        write_json(version.root / 'manifest.json', {'schema_version': 1,
            'agent': {'sha': 'a' * 40}, 'runtime': {'sha': 'b' * 40}, 'tools': {
                **{name: {'version': '1.2.3'} for name in ('uv', 'python', 'bun', 'ffmpeg')},
                'node': {'version': '20.19.4'}, 'omp': {'source': 'omp@1.2.3'}}})
    selection = tmp_path / 'selection.json'
    record = {'owner': 'haus.astrid.runtime', 'program_root': str(installer.root),
              'data_root': str(installer.data), **tx['realm']}
    write_json(selection, record)
    state = {'disabled': False, 'boot': {'version': 1, 'source': 'windows-kernel-boot-id-v1',
                                       'value': {'machine_guid': '11111111-1111-1111-1111-111111111111', 'boot_id': 5}}}
    backend.service = SimpleNamespace(selection=selection, recorded=lambda: read_json(selection),
        recovery_state=lambda *_: {'disabled': state['disabled'], 'running': False, 'sha256': 'owned-task'})
    backend.validate_recovery_candidate = lambda _version: {}
    backend.assert_recovery_stopped = lambda _data: None
    monkeypatch.setattr(recovery, 'inspect_schema', lambda _backend, _version, identity: {
        'identity': inspect_canonical_schema(identity['realm_root'], expected_realm_id=identity['realm_id']), 'target': 31})
    monkeypatch.setattr(boot_identity, 'capture_boot_identity', lambda: dict(state['boot']))
    proof = {'boot_identity': dict(state['boot'])}
    monkeypatch.setattr(host_bootstrap, 'capture_legacy_host_recovery', lambda _support: proof)

    def retire(support_root, _proof, **kwargs):
        target = kwargs['quarantine']
        platform.private_directory(target)
        for name in recovery.MARKERS:
            source = support_root / name
            if source.exists():
                shutil.copy2(source, target / name)
                source.unlink()
    monkeypatch.setattr(host_bootstrap, 'retire_preboot_host', retire)
    def stop_for_recovery(*_):
        state['disabled'] = True
        backend.calls.append(('recovery-stop',))
    backend.stop_for_recovery = stop_for_recovery
    def upgrade(*_):
        backend.calls.append(('candidate-upgrade',))
        write_json(support / 'upgrade-journal.json', {'version': 1, 'operation': 'astrid-runtime-upgrade/v1',
                                                    'operation_id': 'b' * 32, 'realm_id': tx['realm']['realm_id'], 'state': 'complete'})
    backend.upgrade = upgrade
    backend.verify = lambda version, *_: backend.calls.append(('candidate-verified', version))
    return installer, backend, state


def reboot(state):
    state['boot'] = {**state['boot'], 'value': {**state['boot']['value'], 'boot_id': state['boot']['value']['boot_id'] + 1}}


def test_forward_recovery_keeps_new_live_data_and_retains_stale_snapshot_evidence(recovery_fixture):
    installer, backend, state = recovery_fixture
    before = len(backend.calls)
    installer.run('recover-prepare', {})
    with pytest.raises(ValueError, match='different trusted'):
        installer.run('recover-resume', {})
    reboot(state)
    installer.run('recover-resume', {})
    tx = read_json(installer.root / 'update-transaction.json')
    assert tx['step'] == 'complete' and tx['service_healthy'] is True
    assert tx['recovery']['phase'] == 'complete'
    assert (Path(tx['realm']['realm_root']) / 'new-user-data').read_text() == 'newer than retained backup'
    assert (Path(tx['backup']['root']) / 'old-user-data').read_text() == 'older backup'
    assert (Path(tx['recovery']['checkpoint']).parent / 'host-markers/generic-host.json').is_file()
    calls = backend.calls[before:]
    assert 'restore' not in [call[0] for call in calls]
    assert [call[0] for call in calls].index('candidate-upgrade') < [call[0] for call in calls].index('start')
    assert all(call[1] == backend.candidate for call in calls if call[0] in {'start', 'candidate-verified'})


@pytest.mark.parametrize('phase', ['prepared', 'retiring', 'reconciled', 'upgrading', 'upgraded',
                                  'stopped', 'selected', 'registered', 'started', 'verified'])
def test_recovery_resumes_after_every_durable_phase(recovery_fixture, monkeypatch, phase):
    installer, backend, state = recovery_fixture
    save = installer._save_update
    class Crash(BaseException):
        pass
    def crash(tx, step, **details):
        save(tx, step, **details)
        if tx.get('recovery', {}).get('phase') == phase:
            raise Crash()
    monkeypatch.setattr(installer, '_save_update', crash)
    if phase == 'prepared':
        with pytest.raises(Crash):
            installer.run('recover-prepare', {})
    else:
        installer.run('recover-prepare', {})
        reboot(state)
        with pytest.raises(Crash):
            installer.run('recover-resume', {})
    reboot(state)
    monkeypatch.setattr(installer, '_save_update', save)
    installer.run('recover-resume', {})
    assert read_json(installer.root / 'update-transaction.json')['step'] == 'complete'
    assert read_json(installer.root / 'update-transaction.json')['service_healthy'] is True


@pytest.mark.parametrize('mutation', ['checkpoint', 'realm', 'database', 'backup', 'runtime-code', 'ownership', 'service-env', 'selection', 'transaction'])
def test_recovery_rejects_changed_checkpoint_bound_state(recovery_fixture, mutation):
    installer, backend, state = recovery_fixture
    installer.run('recover-prepare', {})
    tx = read_json(installer.root / 'update-transaction.json')
    targets = {'checkpoint': Path(tx['recovery']['checkpoint']),
        'realm': Path(tx['realm']['realm_root']) / 'new-user-data',
        'backup': Path(tx['backup']['root']) / 'old-user-data',
        'runtime-code': backend.candidate.root / 'r/bootstrap.py',
        'ownership': installer.root / 'install.json', 'selection': backend.service.selection,
        'service-env': installer.root / 'service-env.json'}
    if mutation == 'database':
        import sqlite3
        with sqlite3.connect(Path(tx['realm']['realm_root']) / 'realm.sqlite3') as connection:
            connection.execute("INSERT INTO user_app_state VALUES ('owner','setting','{}',1,'now','now')")
    elif mutation == 'transaction':
        tx['operation_id'] = 'c' * 32
        write_json(installer.root / 'update-transaction.json', tx)
    else:
        targets[mutation].write_text('changed')
    reboot(state)
    before = list(backend.calls)
    with pytest.raises((ValueError, KeyError)):
        installer.run('recover-resume', {})
    assert backend.calls == before
    assert (installer.data / 'runtime/generic-host.json').exists()


def test_incomplete_runtime_journal_is_preserved_and_never_guessed_complete(recovery_fixture):
    installer, backend, state = recovery_fixture
    path = installer.data / 'runtime/upgrade-journal.json'
    write_json(path, {'operation': 'astrid-runtime-upgrade/v1', 'operation_id': 'd' * 32, 'state': 'activated'})
    before = path.read_bytes()
    with pytest.raises(ValueError, match='Runtime-supported'):
        installer.run('recover-prepare', {})
    assert path.read_bytes() == before
    assert not any(call[0] == 'recovery-stop' for call in backend.calls)


def test_legacy_preflight_retains_candidate_for_explicit_recovery_without_old_activation(tmp_path):
    from astrid.sdk.host_bootstrap import PackHostBootstrapError
    installer, backend = failed_update(tmp_path)
    def preflight(_data):
        raise PackHostBootstrapError('missing legacy proof', code='legacy_windows_recovery_required')
    backend.preflight_host_update = preflight
    with pytest.raises(ValueError, match='--recover prepare'):
        installer.run('update', {})
    tx = read_json(installer.root / 'update-transaction.json')
    assert tx['step'] == 'recovery_required'
    assert tx['candidate'] and tx['backup']
    assert not tx['data_restored']
    assert not any(call[0] in {'restore', 'start', 'register'} for call in backend.calls)


@pytest.mark.parametrize('mutation', ['none', 'command', 'principal', 'context', 'state'])
def test_recovery_validates_actual_task_action_and_principal_before_stop(tmp_path, mutation):
    from astrid.installer.service import Service, render
    root = tmp_path / 'program'
    version = Version(root / 'versions/old', root / 'versions/old/python.exe',
                      root / 'versions/old/profile.json', {})
    manager = Service(root, tmp_path / 'install.log', lambda _: '', home=tmp_path, system='win32')
    xml = render('win32', version, tmp_path / 'data', manager.log, user='S-1-5-21-test', env_file=manager.env_file)
    xml = xml.replace('<Enabled>true</Enabled>', '<Enabled>false</Enabled>')
    if mutation == 'command':
        xml = xml.replace('pythonw.exe', 'foreign.exe')
    elif mutation == 'principal':
        xml = xml.replace('<RunLevel>LeastPrivilege</RunLevel>', '<RunLevel>HighestAvailable</RunLevel>')
    elif mutation == 'context':
        xml = xml.replace('Context="Owner"', 'Context="Foreign"')
    def powershell(script):
        if 'Export-ScheduledTask' in script:
            return xml
        if 'WindowsIdentity' in script:
            return 'S-1-5-21-test'
        return 'Unknown' if mutation == 'state' else 'Disabled'
    manager.powershell = powershell
    if mutation == 'none':
        assert manager.recovery_state((version,), tmp_path / 'data')['disabled'] is True
    else:
        with pytest.raises(ValueError, match='recovery'):
            manager.recovery_state((version,), tmp_path / 'data')


def test_preparation_intent_blocks_automatic_old_start_and_retries_same_boot(recovery_fixture, monkeypatch):
    installer, backend, state = recovery_fixture
    stop = backend.stop_for_recovery
    def interrupted_stop(*_):
        raise OSError('power loss boundary before checkpoint')
    monkeypatch.setattr(backend, 'stop_for_recovery', interrupted_stop)
    with pytest.raises(OSError):
        installer.run('recover-prepare', {})
    tx = read_json(installer.root / 'update-transaction.json')
    assert tx['recovery']['phase'] == 'preparing'
    before = list(backend.calls)
    with pytest.raises(ValueError, match='--recover prepare'):
        installer.run('update', {})
    assert backend.calls == before
    monkeypatch.setattr(backend, 'stop_for_recovery', stop)
    installer.run('recover-prepare', {})
    assert read_json(installer.root / 'update-transaction.json')['recovery']['phase'] == 'prepared'


def test_missing_native_boot_proof_is_actionable_in_status_without_stopping(recovery_fixture, monkeypatch):
    from astrid.core.execution import boot_identity
    installer, backend, _state = recovery_fixture
    def unavailable():
        raise boot_identity.BootIdentityUnavailable('native boot-counter access denied')
    monkeypatch.setattr(boot_identity, 'capture_boot_identity', unavailable)
    before = list(backend.calls)
    with pytest.raises(ValueError, match='Trusted operating-system boot proof is unavailable'):
        installer.run('recover-prepare', {})
    assert backend.calls == before
    tx = read_json(installer.root / 'update-transaction.json')
    assert 'access denied' in tx['recovery_error']
    messages = []
    installer.say = messages.append
    installer.run('recover-status', {})
    assert 'access denied' in messages[0]
    assert (installer.data / 'runtime/generic-host.json').exists()
