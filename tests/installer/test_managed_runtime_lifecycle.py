"""Managed Runtime requests retain the selected install's lifecycle owner."""
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer import backend, core, installation, service
from astrid.installer.runtime_lifecycle import managed_runtime_lifecycle
import astrid.services


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def managed(tmp_path, monkeypatch):
    root, data = tmp_path / 'program', tmp_path / 'data'
    version_dir = root / 'versions' / 'v13'
    source = version_dir / 'py' / 'lib' / 'python3.11' / 'site-packages'
    source.mkdir(parents=True)
    python, profile = version_dir / 'py' / 'bin' / 'python', version_dir / 'profile.json'
    python.parent.mkdir(parents=True)
    python.touch()
    write(profile, {'source_checkout': str(source), 'runtime_environment': str(version_dir / 'py')})
    selection = tmp_path / 'selection.json'
    write(selection, {'owner': service.LABEL, 'program_root': str(root), 'data_root': str(data)})
    write(root / 'install.json', {'owner': service.LABEL, 'data_root': str(data)})
    write(data / 'runtime' / 'catalog.json', {'selected_realm_id': 'realm', 'realms': [{'realm_id': 'realm', 'data_root': str(data / 'realm')}]})
    write(data / 'runtime' / 'discovery.json', {'runtime_instance_id': 'old', 'pid': 1})
    version = core.Version(version_dir, python, profile, {}, release=13)
    monkeypatch.setattr(core, 'current', lambda _: version)
    monkeypatch.setattr(service, 'install_selection', lambda *_: selection)
    monkeypatch.setattr(service, 'installer_lock_path', lambda *_: tmp_path / 'install.lock')
    monkeypatch.setattr(installation, 'adopt_if_handed_off', lambda *_: None)
    monkeypatch.setattr(astrid.services, 'prepare_pack_host_shutdown', lambda **_: lambda: None)
    events = []
    class FakeBackend:
        def __init__(self, *_a, **_kw):
            self.service = SimpleNamespace(settings=lambda: events.append('settings'), system='darwin',
                                           login_service_exists=lambda: True, autostart_enabled=lambda: True,
                                           validate_stop_target=lambda *_a, **_kw: None)
        def preflight_stop(self, _data): events.append('preflight')
        def stop(self, *_a, **_kw): events.append('stop')
        def register(self, *_a): events.append('register')
        def start(self, *_a):
            events.append('start')
            write(data / 'runtime' / 'discovery.json', {'runtime_instance_id': 'new', 'endpoint': 'local'})
        def verify(self, *_a, **_kw): events.append('verify')
    monkeypatch.setattr(backend, 'Backend', FakeBackend)
    return SimpleNamespace(root=root, data=data, source=source, selection=selection, profile=profile, events=events)


def test_restart_uses_owner_and_selected_profile(managed):
    result = managed_runtime_lifecycle(action='restart', source_checkout=managed.source, data_root=managed.data)
    assert result['status'] == 'restarted'
    assert result['runtime_instance_id'] == 'new'
    assert managed.events == ['settings', 'preflight', 'stop', 'register', 'start', 'verify']


def test_bad_selection_refuses_before_stop(managed):
    write(managed.selection, {'owner': service.LABEL, 'program_root': str(managed.root), 'data_root': '/other'})
    with pytest.raises(ValueError, match='selection'):
        managed_runtime_lifecycle(action='restart', source_checkout=managed.source, data_root=managed.data)
    assert 'stop' not in managed.events


def test_foreign_profile_refuses_before_stop(managed):
    write(managed.profile, {'source_checkout': '/different', 'runtime_environment': str(managed.root)})
    with pytest.raises(ValueError, match='profile'):
        managed_runtime_lifecycle(action='restart', source_checkout=managed.source, data_root=managed.data)
    assert 'stop' not in managed.events


def test_source_runtime_is_not_mistaken_for_managed(tmp_path, monkeypatch):
    monkeypatch.setattr(service, 'install_selection', lambda *_: tmp_path / 'missing.json')
    source = tmp_path / 'checkout'
    source.mkdir()
    assert managed_runtime_lifecycle(action='up', source_checkout=source, data_root=tmp_path / 'data') is None


def test_prior_registered_service_action_is_still_recognised(managed, tmp_path):
    import subprocess
    manager = service.Service(managed.root, tmp_path / 'log', lambda *_a, **_kw: '', home=tmp_path, system='darwin')
    version = core.current(managed.root)
    write(version.root / 'version.json', version.document())
    previous = service.command(version, managed.data, log=manager.log, env_file=manager.env_file,
                               _legacy_runtime_tail=True)
    assert manager._known_launch_action(previous, managed.data, windows=False, script=previous[-1])
    changed = previous.copy()
    changed[-1] += '\nprint("unexpected")'
    assert not manager._known_launch_action(changed, managed.data, windows=False, script=changed[-1])


def test_restart_respects_preflight_before_stop(managed, monkeypatch):
    def refuse(_self, _data):
        managed.events.append('preflight')
        raise ValueError('active task')
    monkeypatch.setattr(backend.Backend, 'preflight_stop', refuse)
    with pytest.raises(ValueError, match='active task'):
        managed_runtime_lifecycle(action='restart', source_checkout=managed.source, data_root=managed.data)
    assert managed.events == ['settings', 'preflight']


def test_up_live_owner_only_verifies(managed, monkeypatch):
    from runtime_protocol import platform
    write(managed.data / 'runtime' / 'discovery.json',
          {'runtime_instance_id': 'old', 'pid': 42, 'process_birth_id': 'birth'})
    monkeypatch.setattr(platform, 'process_identity', lambda _pid: {'birth_id': 'birth'})
    result = managed_runtime_lifecycle(action='up', source_checkout=managed.source, data_root=managed.data)
    assert result['status'] == 'reconnected'
    assert managed.events == ['settings', 'verify']


def test_up_stale_owner_uses_fenced_cleanup(managed, monkeypatch):
    from runtime_protocol import platform
    write(managed.data / 'runtime' / 'discovery.json',
          {'runtime_instance_id': 'old', 'pid': 42, 'process_birth_id': 'birth'})
    monkeypatch.setattr(platform, 'process_identity', lambda _pid: None)
    result = managed_runtime_lifecycle(action='up', source_checkout=managed.source, data_root=managed.data)
    assert result['status'] == 'started'
    assert managed.events == ['settings', 'preflight', 'stop', 'register', 'start', 'verify']


def test_up_unloaded_macos_service_re_registers(managed):
    (managed.data / 'runtime' / 'discovery.json').unlink()
    result = managed_runtime_lifecycle(action='up', source_checkout=managed.source, data_root=managed.data)
    assert result['status'] == 'started'
    assert managed.events == ['settings', 'register', 'start', 'verify']
