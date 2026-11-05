"""C: real Runtime offline adoption + persistent OS-manager simulation.

No native daemon is launched. Actual owner locks, subprocess Runtime CLI,
full source manifests and SIGKILL cuts exercise the serialized handoff.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from runtime_protocol import platform
from runtime_protocol.store import RealmStore
from astrid_runtime import workspace_copy
from astrid_runtime.bootstrap import BootstrapConfig, SourceProfile
from astrid_runtime.paths import RuntimePaths
from astrid_runtime.workspace_adopt import WorkspaceAdoptError, adopt_workspace

from astrid.installer import adoption_receipt, boundary_handoff as handoff
from astrid.installer import boundary_registration as registration, cli, r0_stop
from astrid.installer.backend import Backend
from astrid.installer.core import Version, current, read_json, write_json
from astrid.installer.service import installer_lock_path
from tests.test_installer_r0_entry import public_case, state

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='persistent Linux manager simulation and POSIX kill cuts')


def snapshot(root):
    result = {}
    for path in [root, *root.rglob('*')]:
        info = path.lstat()
        result[str(path.relative_to(root))] = (info.st_dev, info.st_ino, info.st_uid, info.st_gid,
            info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
    return result


@pytest.fixture
def case(tmp_path, monkeypatch):
    root, data, realm, service, manager = public_case(tmp_path, monkeypatch)
    shutil.rmtree(realm)
    RealmStore.initialize(realm, realm_id='r0').close()
    catalog = read_json(data / 'runtime/catalog.json')
    catalog['realms'][0]['display_name'] = 'R0 workspace'
    write_json(data / 'runtime/catalog.json', catalog)
    (data / 'credentials').mkdir()
    (data / 'credentials/retained.json').write_text('{"private":"source credential"}')
    (data / 'empty').mkdir()
    (data / 'runtime/discovery.json').write_text('OPAQUE legacy state; do not parse')
    media = realm / 'cas/sha256/ab/media'
    media.parent.mkdir(parents=True)
    media.write_bytes(b'immutable media')
    shim = tmp_path / 'commands/systemctl'
    body = shim.read_text().replace('print("FragmentPath=" + state["unit"])',
        'print("FragmentPath=" + state["unit"])\n'
        '    print("ActiveState=" + ("active" if state["running"] else "inactive"))\n'
        '    print("UnitFileState=" + ("enabled" if state["enabled"] else "disabled"))\n'
        '    print("LoadState=loaded\\nDropInPaths=" + state.get("r0_dropin", ""))')
    body = body.replace('print("LoadState=loaded\\nActiveState=" + state["r1"])',
        'print("LoadState=loaded\\nActiveState=" + state["r1"])\n'
        f'    print("FragmentPath=" + state.get("r1_fragment", {str(service.path.with_name(registration.R1_LINUX))!r}))\n'
        '    print("DropInPaths=" + state.get("dropins", ""))\n'
        '    print("UnitFileState=" + ("enabled" if state.get("r1_enabled") else "disabled"))')
    body = body.replace('else:\n    raise SystemExit("unexpected manager mutation: " + repr(args))', f'''
elif args == ["--user", "disable", "astrid.service"]:
    state["enabled"] = False
    path.write_text(json.dumps(state))
elif args == ["--user", "daemon-reload"]:
    exists = Path({str(service.path.with_name(registration.R1_LINUX))!r}).exists()
    if exists and state["r1"] == "absent": state["r1"] = "inactive"
    if not exists: state["r1"] = "absent"
    path.write_text(json.dumps(state))
elif args == ["--user", "disable", "--now", "astrid-r1.service"]:
    if state.get("fail_r1_stop"): raise SystemExit("injected stop failure")
    state.update(r1="inactive", r1_enabled=False)
    path.write_text(json.dumps(state))
elif args == ["--user", "enable", "--now", "astrid-r1.service"]:
    assert not state["enabled"] and not state["running"], "two service owners"
    state.update(r1="active", r1_enabled=True)
    path.write_text(json.dumps(state))
else:
    raise SystemExit("unexpected manager mutation: " + repr(args))
''')
    shim.write_text(body)
    backend = Backend(root, service.home / 'install.log', Path(sys.executable))
    backend.console_diagnostics = True
    destination = Path(str(data) + '-r1')
    runtime_root = Path(workspace_copy.__file__).parent.parent
    version_root = root / 'versions/release1'

    def stage(manifest, selected_root, target, *, copy_from):
        assert target == destination and selected_root == root and copy_from == data
        version_root.mkdir()
        profile = SourceProfile('astrid', str(runtime_root), str(Path(__file__).parent.parent))
        write_json(version_root / 'profile.json', profile.as_dict())
        version = Version(version_root, Path(sys.executable), version_root / 'profile.json',
                          {'ASTRID_RUNTIME_DATA_ROOT': str(target)}, release=1)
        write_json(version_root / 'version.json', version.document())
        write_json(version_root / 'manifest.json', manifest)
        registration.stage_runtime_launcher(version)
        return version

    run = backend.run
    runtime_calls = []
    def runtime_cli(argv, **options):
        if 'astrid_runtime' in argv:
            runtime_calls.append(list(map(str, argv)))
            assert list(argv[6:])[:2] == ['workspace', 'adopt']
            # Shared interpreter; expose the exact checked-out Runtime without
            # creating/installing a venv. Preserve the real command arguments.
            script = f'import sys; sys.path.insert(0, {str(runtime_root)!r}); from astrid_runtime.cli import main; raise SystemExit(main())'
            return run([sys.executable, '-c', script, *argv[6:]], **options)
        return run(argv, **options)

    verified = []
    def verify(version, target, realm_id, **options):
        assert target == destination and realm_id == 'r0'
        assert state(manager)['r1'] == 'active' and not state(manager)['running']
        assert current(root) == version
        selected = read_json(service.selection)
        assert selected['realm_root'] == str(realm)
        assert selected['r1_realm_root'] == str(destination / 'runtime/realms/r0')
        # R0's existing lock remains held throughout selection and activation.
        with (realm / 'owner.lock').open('rb') as lock:
            with pytest.raises(BlockingIOError):
                platform.lock_file(lock.fileno(), blocking=False)
        verified.append(target)

    monkeypatch.setattr(backend, 'stage', stage)
    monkeypatch.setattr(backend, 'run', runtime_cli)
    monkeypatch.setattr(backend, 'verify', verify)
    return SimpleNamespace(root=root, data=data, realm=realm, service=backend.service, manager=manager,
        backend=backend, destination=destination, runtime_calls=runtime_calls, verified=verified,
        tx=handoff.BoundaryHandoff(backend), manifest={'release': 1})


def upgrade(c):
    return c.tx.run(c.data, lambda: c.manifest, say=lambda _: None)


def r1_work(c):
    database = c.destination / 'runtime/realms/r0/realm.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE accepted_r1_work (value TEXT)')
        db.execute('INSERT INTO accepted_r1_work VALUES (?)', ('never lose this work',))


def mark_completed_handoff(c):
    """Build the public uninstall evidence without invoking the external Runtime."""
    c.destination.mkdir()
    (c.destination / 'r1-data').write_text('retained R1 workspace')
    owner = c.destination / 'runtime/realms/r0/owner.lock'
    owner.parent.mkdir(parents=True)
    owner.write_bytes(b'retained R1 owner lock')
    r1_service = c.service.path.with_name(
        registration.R1_LINUX if c.service.system == 'linux' else registration.R1_MAC,
    )
    r1_service.write_text('retained R1 service definition')
    marker = read_json(c.root / 'install.json')
    write_json(c.root / 'install.json', {**marker, 'r1_data_root': str(c.destination)})
    selection = read_json(c.service.selection)
    write_json(c.service.selection, {**selection, 'r1_data_root': str(c.destination),
        'service_label': r1_service.name})
    write_json(c.tx.journal, {'version': 1, 'phase': 'complete', 'system': c.service.system,
        'program_root': str(c.root), 'r0_data_root': str(c.data),
        'r1_data_root': str(c.destination), 'realm_id': 'r0'})


def assert_r0(c):
    manager = state(c.manager)
    assert manager['enabled'] and manager['running']
    assert manager['r1'] == 'absent' and not manager.get('r1_enabled')
    assert current(c.root).release == 0
    assert 'r1_data_root' not in read_json(c.service.selection)


def test_real_cli_copy_receipt_activation_rerun_and_rollback_after_work(case, monkeypatch):
    c = case
    for path in [*c.data.rglob('*'), c.data]:
        path.chmod(0o555 if path.is_dir() else 0o444)
    before = snapshot(c.data), c.service.path.read_bytes()
    upgrade(c)
    assert (snapshot(c.data), c.service.path.read_bytes()) == before
    assert c.runtime_calls and c.verified
    call = c.runtime_calls[0]
    assert call[6:8] == ['workspace', 'adopt']
    assert all(flag in call for flag in ('--read-only-source', '--no-start', '--json', '--source-manifest'))
    assert call[call.index('--data-root') + 1] == str(c.destination)
    value = read_json(handoff.journal_path(c.service))
    receipt = read_json(c.destination / adoption_receipt.RECEIPT)
    assert value['phase'] == 'complete' and value['receipt'] == receipt
    assert receipt['source_inventory_sha256'] == adoption_receipt.observe(c.data)['inventory']
    assert receipt['source_manifest_sha256'] == hashlib.sha256(Path(value['candidate']['profile']).read_bytes()).hexdigest()
    assert receipt['started'] is False
    assert read_json(c.root / 'install.json')['r1_data_root'] == str(c.destination)
    assert read_json(c.service.selection)['version'] == value['candidate']
    assert str(c.destination) in c.service.path.with_name(registration.R1_LINUX).read_text()
    # A completed rerun selects only R1 and does not invoke the old preflight.
    monkeypatch.setattr(c.backend, 'verify', lambda version, data, realm_id: c.verified.append(data))
    assert handoff.handle(c.root, c.data, c.backend)
    assert c.verified[-1] == c.destination and len(c.runtime_calls) == 1
    r1_work(c)
    retained = snapshot(c.destination)
    c.tx.recover(rollback=True)
    assert_r0(c)
    assert snapshot(c.destination) == retained
    assert (snapshot(c.data), c.service.path.read_bytes()) == before
    with sqlite3.connect(c.destination / 'runtime/realms/r0/realm.sqlite3') as db:
        assert db.execute('SELECT value FROM accepted_r1_work').fetchall() == [('never lose this work',)]
    assert handoff.handle(c.root, c.data, c.backend, rollback=True)
    with pytest.raises(ValueError, match='retained'):
        handoff.handle(c.root, c.data, c.backend, manifest_loader=lambda: c.manifest)


def in_process_adopt(c, monkeypatch):
    def runtime(version, target, *args, profile=False):
        assert args == ('workspace', 'adopt', '--from', str(c.data), '--realm-id', 'r0', '--read-only-source', '--no-start')
        assert profile and target == c.destination
        return adopt_workspace(RuntimePaths.current(data_root=target), object(),
            BootstrapConfig(source_manifest=version.profile), source=c.data,
            realm_id='r0', read_only_source=True, no_start=True)
    monkeypatch.setattr(c.backend, 'runtime', runtime)
    return runtime


def test_injected_writer_mid_copy_discards_candidate_and_restores_r0(case, monkeypatch):
    c = case
    in_process_adopt(c, monkeypatch)
    copy = workspace_copy._copy_tree
    original = (c.data / 'credentials/retained.json').read_bytes()
    injected = []
    def writer(*args):
        copy(*args)
        if not injected:
            injected.append(True)
            (c.data / 'credentials/retained.json').write_bytes(original + b' external writer')
            injected.append(snapshot(c.data))
    monkeypatch.setattr(workspace_copy, '_copy_tree', writer)
    with pytest.raises(WorkspaceAdoptError, match='changed while updating'):
        upgrade(c)
    assert snapshot(c.data) == injected[-1]  # only the external writer changed R0
    assert not c.destination.exists()
    assert not list(c.data.parent.glob('.data-r1.adopt-*'))
    assert_r0(c)


@pytest.mark.parametrize('bad', ['response', 'receipt', 'profile', 'identity', 'inventory', 'boolean', 'catalog'])
def test_receipt_gate_refuses_before_selection_and_keeps_both_roots(case, monkeypatch, bad):
    c = case
    runtime = in_process_adopt(c, monkeypatch)
    before = snapshot(c.data)
    def tamper(*args, **options):
        result = runtime(*args, **options)
        result = dict(result)
        receipt = c.destination / adoption_receipt.RECEIPT
        if bad == 'profile':
            Path(args[0].profile).write_text('{}')
        elif bad == 'catalog':
            write_json(c.destination / 'runtime/catalog.json', {})
        else:
            field, value = {'response': ('source', '/foreign'), 'receipt': ('operation_id', 'bad'),
                'identity': ('source_root_inode', 0), 'inventory': ('post_inventory_sha256', '0' * 64),
                'boolean': ('started', 0)}[bad]
            result[field] = value
            if bad != 'response':
                write_json(receipt, result)
        return result
    monkeypatch.setattr(c.backend, 'runtime', tamper)
    with pytest.raises((ValueError, KeyError)):
        upgrade(c)
    assert snapshot(c.data) == before and c.destination.exists()
    assert_r0(c)
    assert not c.verified


def test_busy_r0_refused_and_reenabled_without_copy(case, monkeypatch):
    c = case
    before = snapshot(c.data), snapshot(c.root), c.service.path.read_bytes()
    wait = r0_stop._wait_for_owner_lock
    monkeypatch.setattr(r0_stop, '_wait_for_owner_lock', lambda path, *_: wait(path, .02, .005))
    with (c.realm / 'owner.lock').open('rb') as lock:
        platform.lock_file(lock.fileno(), blocking=False)
        with pytest.raises(ValueError, match='Finish or cancel'):
            upgrade(c)
    assert_r0(c)
    assert not c.destination.exists() and not c.runtime_calls
    assert (snapshot(c.data), snapshot(c.root), c.service.path.read_bytes()) == before


def kill_child(action):
    child = os.fork()
    if child == 0:
        try:
            action()
        finally:
            os._exit(99)
    _, result = os.waitpid(child, 0)
    assert os.WIFSIGNALED(result) and os.WTERMSIG(result) == signal.SIGKILL


def die():
    os.kill(os.getpid(), signal.SIGKILL)


@pytest.mark.parametrize('cut', ['handoff-journal-link', 'stop', 'staging', 'adopting', 'runtime-published', 'receipt-returned',
    'adopted', 'registering', 'registration-journal', 'launcher', 'definition', 'switching',
    'marker', 'selection', 'pointer', 'selected-mirror', 'starting', 'enabled', 'verified', 'complete'])
def test_sigkill_each_adoption_and_switch_step_recovers_one_owner(case, monkeypatch, cut):
    c = case
    before = snapshot(c.data), c.service.path.read_bytes()
    runtime = in_process_adopt(c, monkeypatch)
    save = c.tx._save
    publish = registration._publish
    run = c.backend.run
    verify = c.backend.verify
    targets = {'registration-journal': c.tx.registration.journal, 'launcher': c.tx.registration.launcher,
        'definition': c.tx.registration.r1_path, 'marker': c.root / 'install.json',
        'selection': c.service.selection, 'pointer': c.root / 'current',
        'selected-mirror': c.destination / 'update-journal.r1.json'}
    def child_action():
        link = registration.os.link
        def linked(source, target, **options):
            link(source, target, **options)
            if cut == 'handoff-journal-link' and target == c.tx.journal: die()
        registration.os.link = linked
        def saved(value, **options):
            save(value, **options)
            if value['phase'] == cut: die()
        c.tx._save = saved
        def published(path, content, **options):
            publish(path, content, **options)
            if path == targets.get(cut): die()
        registration._publish = published
        def manager_run(argv, **options):
            result = run(argv, **options)
            if cut == 'stop' and list(argv) == ['systemctl', '--user', 'disable', '--now', 'astrid.service']: die()
            if cut == 'enabled' and list(argv) == ['systemctl', '--user', 'enable', '--now', 'astrid-r1.service']: die()
            return result
        c.backend.run = manager_run
        c.backend.service.run = manager_run
        def returned(*args, **options):
            result = runtime(*args, **options)
            if cut == 'receipt-returned': die()
            return result
        c.backend.runtime = returned
        actual_publish = workspace_copy._publish
        def runtime_published(*args):
            actual_publish(*args)
            if cut == 'runtime-published': die()
        workspace_copy._publish = runtime_published
        def verified(*args, **options):
            verify(*args, **options)
            if cut == 'verified': die()
        c.backend.verify = verified
        upgrade(c)
    kill_child(child_action)
    assert (snapshot(c.data), c.service.path.read_bytes()) == before
    # The only committed completion is safe to keep. Every other cut switches
    # back, even when a plausible published receipt exists.
    if cut == 'complete':
        monkeypatch.setattr(c.backend, 'verify', lambda *a: None)
        assert handoff.handle(c.root, c.data, c.backend)
        assert state(c.manager)['r1'] == 'active' and not state(c.manager)['running']
        c.tx.recover(rollback=True)
    else:
        assert handoff.handle(c.root, c.data, c.backend)
    assert_r0(c)
    assert (snapshot(c.data), c.service.path.read_bytes()) == before
    if c.destination.exists():
        retained = snapshot(c.destination)
        with pytest.raises(ValueError, match='retained'):
            handoff.handle(c.root, c.data, c.backend)
        assert snapshot(c.destination) == retained


@pytest.mark.parametrize('cut', ['intent', 'stopped', 'marker', 'selection', 'pointer', 'r1-definition-removed',
                               'r0-enabled', 'registration-removed', 'rolled_back'])
def test_sigkill_during_rollback_replays_after_r1_work(case, monkeypatch, cut):
    c = case
    upgrade(c)
    r1_work(c)
    before = snapshot(c.data), snapshot(c.destination), c.service.path.read_bytes()
    save, publish, unlink, run = c.tx._save, registration._publish, Path.unlink, c.backend.run
    def action():
        def saved(value, **options):
            save(value, **options)
            if (cut == 'intent' and value['phase'] == 'rolling_back') or value['phase'] == cut: die()
        c.tx._save = saved
        def published(path, content, **options):
            publish(path, content, **options)
            targets = {'marker': c.root / 'install.json', 'selection': c.service.selection, 'pointer': c.root / 'current'}
            if path == targets.get(cut): die()
        registration._publish = published
        def removed(path, *args, **options):
            unlink(path, *args, **options)
            if ((cut == 'r1-definition-removed' and path == c.tx.registration.r1_path)
                    or (cut == 'registration-removed' and path == c.tx.registration.journal)): die()
        Path.unlink = removed
        def manager_run(argv, **options):
            result = run(argv, **options)
            if cut == 'stopped' and list(argv) == ['systemctl', '--user', 'disable', '--now', 'astrid-r1.service']: die()
            if cut == 'r0-enabled' and list(argv) == ['systemctl', '--user', 'enable', '--now', 'astrid.service']: die()
            return result
        c.backend.service.run = manager_run
        c.tx.recover(rollback=True)
    kill_child(action)
    assert handoff.handle(c.root, c.data, c.backend, rollback=True)
    assert_r0(c)
    assert (snapshot(c.data), snapshot(c.destination), c.service.path.read_bytes()) == before


@pytest.mark.parametrize('problem', ['busy', 'changed-unit', 'foreign-manager', 'changed-selection', 'failed-stop'])
def test_rollback_refuses_uncertain_r1_without_restarting_r0(case, monkeypatch, problem):
    c = case
    upgrade(c)
    r1_work(c)
    if problem == 'changed-unit':
        c.tx.registration.r1_path.write_text('foreign service')
    elif problem == 'foreign-manager':
        state(c.manager, r1_fragment='/foreign/service')
    elif problem == 'changed-selection':
        selected = read_json(c.service.selection)
        write_json(c.service.selection, {**selected, 'r1_data_root': '/foreign/data'})
    elif problem == 'failed-stop':
        state(c.manager, fail_r1_stop=True)
    before = snapshot(c.data), snapshot(c.destination)
    wait = r0_stop._wait_for_owner_lock
    monkeypatch.setattr(r0_stop, '_wait_for_owner_lock', lambda path, *_: wait(path, .02, .005))
    lock = (c.destination / 'runtime/realms/r0/owner.lock').open('rb')
    try:
        if problem == 'busy': platform.lock_file(lock.fileno(), blocking=False)
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            c.tx.recover(rollback=True)
    finally:
        lock.close()
    assert not state(c.manager)['enabled']
    assert handoff.journal_path(c.service).exists()
    assert (snapshot(c.data), snapshot(c.destination)) == before


def test_cli_serializes_boundary_and_never_calls_legacy_paths(case, monkeypatch):
    c = case
    # A local test manifest avoids network. Staging is the only replaced
    # product operation; Runtime adoption and service calls remain real paths.
    manifest = c.root.parent / 'manifest.json'
    write_json(manifest, c.manifest)
    monkeypatch.setattr(cli, 'Backend', lambda *a, **kw: c.backend)
    monkeypatch.setattr(cli, 'validate_manifest', lambda value: value)
    monkeypatch.setattr(c.backend, 'preflight_stop', lambda *a: pytest.fail('legacy preflight'))
    monkeypatch.setattr(c.backend, 'source_checkout_ownership', lambda *a: pytest.fail('legacy discovery'))
    from astrid.installer.service import installer_lock_path
    stage = c.backend.stage
    def under_lock(*args, **options):
        with installer_lock_path(c.service.home, 'linux').open('rb') as lock:
            with pytest.raises(BlockingIOError): platform.lock_file(lock.fileno(), blocking=False)
        return stage(*args, **options)
    monkeypatch.setattr(c.backend, 'stage', under_lock)
    assert cli.main(['--root', str(c.root), '--manifest', str(manifest), '--uv', sys.executable]) == 0
    monkeypatch.setattr(c.backend, 'verify', lambda *a: None)
    assert cli.main(['--root', str(c.root)]) == 0
    r1_work(c)
    before = snapshot(c.data), snapshot(c.destination)
    assert cli.main(['--root', str(c.root), '--rollback']) == 0
    assert_r0(c)
    assert (snapshot(c.data), snapshot(c.destination)) == before


def test_rollback_retains_r1_lock_until_r0_restart(case, monkeypatch):
    c = case
    upgrade(c)
    run = c.service.run
    observed = []
    def manager(argv, **options):
        if list(argv) == ['systemctl', '--user', 'enable', '--now', 'astrid.service']:
            with (c.destination / 'runtime/realms/r0/owner.lock').open('rb') as lock:
                with pytest.raises(BlockingIOError): platform.lock_file(lock.fileno(), blocking=False)
            observed.append(True)
        return run(argv, **options)
    monkeypatch.setattr(c.service, 'run', manager)
    c.tx.recover(rollback=True)
    assert observed
    assert_r0(c)


@pytest.mark.parametrize('alias', [False, True])
def test_real_service_declarations_relocate_retained_and_inherited_r0_write_paths(case, monkeypatch, alias):
    c = case
    old = current(c.root)
    source = c.data
    if alias:
        source = c.root.parent / 'source-alias'
        source.symlink_to(c.data, target_is_directory=True)
    write_json(old.profile, {'service_declaration': str(old.root / 'services.json')})
    write_json(old.root / 'services.json', {'services': [{'name': 'chat', 'env': {
        'ASTRID_ACP_CWD': str(source / 'chat'),
        'ASTRID_ACP_SESSION_DIR': str(source / 'chat/sessions'),
        'ASTRID_ACP_SYSTEM_PROMPT_FILE': str(source / 'prompt.txt'),
    }}]})
    monkeypatch.setenv('ASTRID_ACP_PROFILE', str(source / 'profile.json'))
    monkeypatch.setenv('ASTRID_ACP_SESSION_DIR', str(source / 'wrong-sessions'))
    folder = c.root / 'declaration-test'
    folder.mkdir()
    run = Backend.run.__get__(c.backend)
    def declaration(argv, **options):
        # Run the actual declaration API with this checkout and shared Python.
        script = f'import sys; sys.path.insert(0, {str(Path(__file__).parent.parent)!r}); ' + argv[5]
        return run([sys.executable, '-c', script, *argv[6:]], **options)
    monkeypatch.setattr(c.backend, 'run', declaration)
    before = snapshot(c.data)
    path = c.backend._declare_services(folder=folder, agent=Path(__file__).parent.parent,
        data=c.destination, python=Path(sys.executable), bun='/pinned/bun', env={}, old=old, copy_from=c.data)
    services = read_json(path)['services']
    for service in services:
        assert service['env']['ASTRID_RUNTIME_DATA_ROOT'] == str(c.destination)
        assert service['env']['ASTRID_SUPPORT_ROOT'] == str(c.destination)
    chat = next(row['env'] for row in services if row['name'] == 'chat')
    assert chat['ASTRID_ACP_CWD'] == str(c.destination / 'chat')
    assert chat['ASTRID_ACP_SESSION_DIR'] == str(c.destination / 'chat/sessions')
    assert chat['ASTRID_ACP_SYSTEM_PROMPT_FILE'] == str(c.destination / 'prompt.txt')
    assert chat['ASTRID_ACP_PROFILE'] == str(c.destination / 'profile.json')
    assert snapshot(c.data) == before


def test_real_r0_v31_fixture_migrates_copies_legacy_edit_and_rolls_back_after_work(case, monkeypatch):
    c = case
    fixture = Path(__file__).parent / 'fixtures/upgrade/r0-legacy-timeline'
    expected = read_json(fixture / 'expected.json')
    assert hashlib.sha256((fixture / 'realm.sqlite3').read_bytes()).hexdigest() == expected['database_sha256']
    for suffix in ('-wal', '-shm', '-journal'):
        (c.realm / ('realm.sqlite3' + suffix)).unlink(missing_ok=True)
    shutil.copyfile(fixture / 'realm.sqlite3', c.realm / 'realm.sqlite3')
    # Preserve the real R0 identity rather than manufacturing a current-schema
    # source. Catalog and owner.lock are fixture setup, before the observation.
    with sqlite3.connect(c.realm / 'realm.sqlite3') as db:
        realm_id = db.execute('SELECT id FROM realm').fetchone()[0]
    catalog = read_json(c.data / 'runtime/catalog.json')
    catalog['selected_realm_id'] = catalog['realms'][0]['realm_id'] = realm_id
    write_json(c.data / 'runtime/catalog.json', catalog)
    for path in [*c.data.rglob('*'), c.data]:
        path.chmod(0o555 if path.is_dir() else 0o444)
    before = snapshot(c.data)
    verified = []
    def verify(version, data, selected):
        assert selected == realm_id and data == c.destination
        assert RealmStore.inspect_realm(data / 'runtime/realms' / realm_id, deep=True)['ok']
        verified.append(selected)
    monkeypatch.setattr(c.backend, 'verify', verify)
    upgrade(c)
    receipt = read_json(c.destination / adoption_receipt.RECEIPT)
    assert receipt['source_version'] == 31 and receipt['target_version'] == 33
    assert verified == [realm_id] and snapshot(c.data) == before
    database = c.destination / 'runtime/realms' / realm_id / 'realm.sqlite3'
    with sqlite3.connect(database) as db:
        row = db.execute('SELECT id,project_id,kind,content_json,version FROM project_documents WHERE id=?',
                         (expected['document']['id'],)).fetchone()
        assert dict(zip(('id', 'project_id', 'kind', 'content_json', 'version'), row)) == expected['document']
        db.execute('CREATE TABLE accepted_r1_work (value TEXT)')
        db.execute('INSERT INTO accepted_r1_work VALUES (?)', ('keep both histories',))
    retained = snapshot(c.destination)
    c.tx.recover(rollback=True)
    assert_r0(c)
    assert snapshot(c.data) == before and snapshot(c.destination) == retained


def prepare_uninstall(c, monkeypatch):
    mark_completed_handoff(c)
    monkeypatch.setattr(cli, 'Backend', lambda *a, **kw: c.backend)
    monkeypatch.setattr(c.backend, 'preflight_stop', lambda data: None)
    monkeypatch.setattr(c.backend, 'preflight_remove', lambda root, data, **kw: c.realm)


@pytest.mark.parametrize('delete_data', [False, True])
@pytest.mark.parametrize('malformed', [False, True])
@pytest.mark.parametrize('existing_state', [False, True])
def test_cli_uninstall_checks_selected_custom_marker_before_installer_state(
    case, monkeypatch, capsys, delete_data, malformed, existing_state,
):
    c = case
    mark_completed_handoff(c)
    c.tx.journal.unlink()
    c.tx.registration.r1_path.unlink()
    selected = read_json(c.service.selection)
    selected.pop('r1_data_root')
    write_json(c.service.selection, selected)
    if malformed:
        (c.root / 'install.json').write_text('{ malformed marker')
    lock = installer_lock_path(Path.home(), 'linux')
    log = platform.log_dir() / 'install.log'
    if existing_state:
        lock.write_text('existing lock')
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('existing installer log')

    def forbidden(*args, **kwargs):
        pytest.fail('uninstall reached an operational or process action')

    for method in ('run', 'stop', 'preflight_stop', 'preflight_remove', 'remove', 'unregister'):
        monkeypatch.setattr(Backend, method, forbidden)
    monkeypatch.setattr(os, 'kill', forbidden)
    monkeypatch.setattr(os, 'killpg', forbidden)
    before = snapshot(c.root.parent)
    args = ['--uninstall'] + (['--delete-data'] if delete_data else [])
    assert cli.main(args) == 1
    assert capsys.readouterr().out.splitlines()[-1] == handoff.UPGRADE_UNINSTALL_REFUSAL
    assert snapshot(c.root.parent) == before


@pytest.mark.parametrize('delete_data', [False, True])
@pytest.mark.parametrize('evidence', ['complete', 'malformed', 'missing-journal'])
def test_cli_uninstall_completed_handoff_refuses_without_mutation(
    case, monkeypatch, capsys, delete_data, evidence,
):
    c = case
    prepare_uninstall(c, monkeypatch)
    if evidence == 'malformed':
        c.tx.journal.write_text('{ malformed handoff evidence\n')
    elif evidence == 'missing-journal':
        c.tx.journal.unlink()
    from astrid.installer.boundary_handoff import UPGRADE_UNINSTALL_REFUSAL

    before = (snapshot(c.root), snapshot(c.data), snapshot(c.destination),
              snapshot(c.service.path.parent), snapshot(c.manager))
    lock = installer_lock_path(Path.home(), 'linux')
    assert not lock.exists()
    args = ['--root', str(c.root), '--uninstall'] + (['--delete-data'] if delete_data else [])
    assert cli.main(args) == 1
    output = capsys.readouterr().out
    assert output.splitlines()[-1] == UPGRADE_UNINSTALL_REFUSAL
    assert (snapshot(c.root), snapshot(c.data), snapshot(c.destination),
            snapshot(c.service.path.parent), snapshot(c.manager)) == before
    assert not lock.exists()


@pytest.mark.parametrize('problem', ['incomplete', 'changed-selection', 'changed-unit', 'changed-r0-unit',
                                      'foreign-manager', 'active-r0', 'changed-r0-realm',
                                      'failed-r1-stop', 'failed-r0-stop', 'busy-r1'])
def test_cli_uninstall_refuses_any_handoff_evidence_before_mutation(case, monkeypatch, problem):
    c = case
    prepare_uninstall(c, monkeypatch)
    if problem == 'incomplete':
        value = read_json(c.tx.journal)
        value['phase'] = 'starting'
        c.tx._save(value)
    elif problem == 'changed-selection':
        value = read_json(c.service.selection)
        write_json(c.service.selection, {**value, 'r1_data_root': '/foreign'})
    elif problem == 'changed-unit':
        c.tx.registration.r1_path.write_text('foreign service')
    elif problem == 'changed-r0-unit':
        c.service.path.write_text('foreign service')
    elif problem == 'foreign-manager':
        state(c.manager, r1_fragment='/foreign/service')
    elif problem == 'active-r0':
        state(c.manager, enabled=True, running=True)
    elif problem == 'changed-r0-realm':
        value = read_json(c.data / 'runtime/catalog.json')
        value['realms'][0]['data_root'] = str(c.root.parent / 'unrelated')
        write_json(c.data / 'runtime/catalog.json', value)
    elif problem == 'failed-r1-stop':
        state(c.manager, fail_r1_stop=True)
    elif problem == 'failed-r0-stop':
        run = c.service.run
        def failing(argv, **options):
            if list(argv) == ['systemctl', '--user', 'disable', registration.R0_LINUX]:
                raise subprocess.CalledProcessError(1, argv)
            return run(argv, **options)
        monkeypatch.setattr(c.service, 'run', failing)
    wait = r0_stop._wait_for_owner_lock
    monkeypatch.setattr(r0_stop, '_wait_for_owner_lock', lambda path, *_: wait(path, .02, .005))
    before = snapshot(c.data), snapshot(c.destination)
    lock = (c.destination / 'runtime/realms/r0/owner.lock').open('rb')
    try:
        if problem == 'busy-r1':
            platform.lock_file(lock.fileno(), blocking=False)
        assert cli.main(['--root', str(c.root), '--uninstall', '--delete-data']) == 1
    finally:
        lock.close()
    assert (snapshot(c.data), snapshot(c.destination)) == before
    assert c.root.exists() and c.service.path.exists()
    assert c.tx.registration.r1_path.exists()
    assert c.tx.journal.exists() and not c.tx.registration.journal.exists()
    assert state(c.manager)['calls'] == []


def test_uninstall_uses_real_remove_preflight_and_r1_stop_preflight(case, monkeypatch):
    c = case
    prepare_uninstall(c, monkeypatch)
    monkeypatch.setattr(cli, 'Backend', lambda *a, **kw: c.backend)
    monkeypatch.setattr(c.backend, 'workspace', lambda *args: pytest.fail('R0 Runtime inspection'))
    assert cli.main(['--root', str(c.root), '--uninstall']) == 1
    assert c.data.exists() and c.destination.exists()


def test_uninstall_rechecks_realm_at_file_removal(case, monkeypatch):
    c = case
    prepare_uninstall(c, monkeypatch)
    foreign = c.root.parent / 'unrelated'
    foreign.mkdir()
    (foreign / 'keep').write_text('user data')
    calls = []
    def preflight(*args, **kwargs):
        calls.append(True)
        return c.realm if len(calls) == 1 else foreign
    monkeypatch.setattr(c.backend, 'preflight_remove', preflight)
    before = snapshot(c.root), snapshot(c.data), snapshot(c.destination)
    assert cli.main(['--root', str(c.root), '--uninstall', '--delete-data']) == 1
    assert (foreign / 'keep').read_text() == 'user data'
    assert (snapshot(c.root), snapshot(c.data), snapshot(c.destination)) == before


@pytest.mark.parametrize("appearance", ["before-disable", "after-disable", "drop-in"])
def test_uninstall_refuses_new_r0_owner_without_signalling_process(case, monkeypatch, appearance):
    c = case
    prepare_uninstall(c, monkeypatch)
    before = (snapshot(c.root), snapshot(c.data), snapshot(c.destination),
              snapshot(c.service.path.parent), snapshot(c.manager))
    assert cli.main(['--root', str(c.root), '--uninstall', '--delete-data']) == 1
    assert state(c.manager)['calls'] == []
    assert (snapshot(c.root), snapshot(c.data), snapshot(c.destination),
            snapshot(c.service.path.parent), snapshot(c.manager)) == before


def mac_ready(c, monkeypatch):
    import plistlib
    from astrid.installer.service import Service
    from tests.test_installer_r0_stop import FIXTURES
    old_service = c.service
    svc = Service(c.root, c.backend.log, None, home=old_service.home, system='darwin')
    write_json(svc.selection, read_json(old_service.selection))
    svc.path.parent.mkdir(parents=True)
    plist = plistlib.loads((FIXTURES / 'macos.txt').read_bytes())
    plist['ProgramArguments'] = [x.replace('/Users/runner/work/_temp/astrid-r1-direct-macos/program', str(c.root)) for x in plist['ProgramArguments']]
    svc.path.write_bytes(plistlib.dumps(plist))
    manager = {'r0_loaded': True, 'r0_enabled': True, 'r1_loaded': False, 'r1_enabled': False, 'separator': '=>', 'disabled_value': 'true', 'calls': []}
    def run(argv, **kw):
        argv = list(map(str, argv))
        manager['calls'].append(argv)
        action, target = argv[1:3]
        is_r1 = target.endswith(registration.R1_MAC)
        which = 'r1' if is_r1 else 'r0'
        if action == 'print':
            if not manager[which + '_loaded']:
                raise subprocess.CalledProcessError(113, argv, output='Could not find service "' + target.rsplit('/', 1)[-1] + '"')
            path = svc.path.with_name(registration.R1_MAC + '.plist') if is_r1 else svc.path
            args = plistlib.loads(path.read_bytes())['ProgramArguments']
            body = ''.join('\t\t' + arg + '\n' for arg in args)
            return f'{target} = {{\n\tpath = {path}\n\targuments = {{\n{body}\t}}\n}}\n'
        if action == 'print-disabled':
            return 'disabled services = {\n' + ''.join(f'\t"{label}" {manager["separator"]} {(manager["disabled_value"] if not manager[key + "_enabled"] else "false")}\n' for key, label in [('r0', registration.R0_MAC), ('r1', registration.R1_MAC)]) + '}\n'
        if action in ('enable', 'disable'):
            manager[which + '_enabled'] = action == 'enable'
        elif action == 'bootout':
            manager[which + '_loaded'] = False
        elif action == 'bootstrap':
            manager['r1_loaded'] = True
        else:
            raise AssertionError(argv)
        return ''
    svc.run = run
    c.service = c.backend.service = svc
    c.tx = handoff.BoundaryHandoff(c.backend)
    monkeypatch.setattr(c.backend, 'verify', lambda *a, **kw: None)
    mark_completed_handoff(c)
    monkeypatch.setattr(c.backend, 'preflight_stop', lambda *a, **kw: None)
    c.backend.managed_by = None
    manager['calls'].clear()
    return manager


def test_mac_uninstall_helper_refuses_without_mutation(case, monkeypatch):
    c = case
    manager = mac_ready(c, monkeypatch)
    from astrid.installer.boundary_handoff import UPGRADE_UNINSTALL_REFUSAL

    before = (snapshot(c.root), snapshot(c.data), snapshot(c.destination),
              snapshot(c.service.path.parent), dict(manager))
    with pytest.raises(ValueError) as error:
        handoff.uninstall_complete(c.backend, c.data, delete_data=True)
    assert str(error.value) == UPGRADE_UNINSTALL_REFUSAL
    assert (snapshot(c.root), snapshot(c.data), snapshot(c.destination),
            snapshot(c.service.path.parent), dict(manager)) == before


def test_cli_handled_handoff_refuses_policy_change_and_reports_saved_policy(case, monkeypatch, capsys):
    # The handled R0->R1 route returns before the installer applies choices, so a
    # requested change must be refused (never silently dropped) and saved policy
    # must be carried over and summarised. R0 is an existing install: no prompt.
    c = case
    manifest = c.root.parent / 'manifest.json'
    write_json(manifest, c.manifest)
    saved = {'autostart': False, 'command': True, 'skills': True, 'skill_copies': False}
    write_json(c.service.selection, {**read_json(c.service.selection), 'choices': saved})
    before = c.service.selection.read_bytes(), snapshot(c.data)
    monkeypatch.setattr(cli, 'Backend', lambda *a, **kw: c.backend)
    monkeypatch.setattr(cli, 'validate_manifest', lambda value: value)
    monkeypatch.setattr(cli, '_prompt_choices', lambda: pytest.fail('R0 is an existing install; no first-install prompt'))
    args = ['--root', str(c.root), '--manifest', str(manifest), '--uv', sys.executable]

    # Explicit change: refused before any handoff work, nothing changed.
    assert cli.main([*args, '--autostart', 'on']) == 1
    out = capsys.readouterr().out
    assert 'cannot apply setup choice changes (autostart)' in out
    assert not handoff.journal_path(c.service).exists() and not c.runtime_calls
    assert (c.service.selection.read_bytes(), snapshot(c.data)) == before
    assert_r0(c)

    # Explicit value equal to the saved policy is no change: handoff completes with a summary.
    assert cli.main([*args, '--autostart', 'off']) == 0
    out = capsys.readouterr().out
    assert 'Astrid setup summary:' in out and 'R0 to R1 handoff handled' in out
    assert 'Astrid will not start at sign-in' in out  # saved policy, not defaults
    assert read_json(handoff.journal_path(c.service))['phase'] == 'complete'
    assert read_json(c.service.selection)['choices'] == saved

    # Completed-handoff fast path: a requested change is refused, no change is silent.
    monkeypatch.setattr(c.backend, 'verify', lambda *a: None)
    assert cli.main(['--root', str(c.root), '--command', 'off']) == 1
    assert 'cannot apply setup choice changes (command)' in capsys.readouterr().out
    assert read_json(handoff.journal_path(c.service))['phase'] == 'complete'
    assert cli.main(['--root', str(c.root)]) == 0
    out = capsys.readouterr().out
    assert 'Astrid setup summary:' in out and 'Astrid will not start at sign-in' in out
    assert read_json(c.service.selection)['choices'] == saved
