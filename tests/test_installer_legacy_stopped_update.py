"""Positive stopped admission requires independent, fail-closed observations."""
import errno
import json
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from astrid.installer.backend import Backend
from astrid.installer.core import current
from astrid.installer.service import Service
from tests.test_installer_legacy_live_update import legacy


def backend_for(root):
    return Backend(root, root / 'install.log', root / 'uv', process_control='strict')


def test_exact_stale_row_with_partial_observations_is_refused(legacy, monkeypatch):
    root, data, _, _, _ = legacy
    backend = backend_for(root)
    monkeypatch.setattr('runtime_protocol.platform.process_identity', lambda pid: None)
    monkeypatch.setattr(backend, '_legacy_doors_stopped', lambda discovery, version: True)
    monkeypatch.setattr(backend.service, 'legacy_manager_stopped', lambda data: True)
    from astrid.installer import legacy_identity
    assert legacy_identity.admitted(current(root), {}, legacy[3], program_root=root, stopped=True)
    with pytest.raises(RuntimeError, match='Cannot prove.*stopped'):
        backend.source_checkout_ownership(data, current(root))


@pytest.mark.parametrize('case', ['missing', 'live', 'unknown_process', 'door', 'manager', 'row', 'policy', 'changed'])
def test_stopped_admission_refuses_unproven_owner(legacy, monkeypatch, case):
    from astrid.installer import legacy_identity
    root, data, _, _, _ = legacy
    backend = backend_for(root)
    path = data / 'runtime/discovery.json'
    monkeypatch.setattr('runtime_protocol.platform.process_identity', lambda pid: None)
    monkeypatch.setattr(backend, '_legacy_doors_stopped', lambda discovery, version: True)
    monkeypatch.setattr(backend.service, 'legacy_manager_stopped', lambda data: True)
    if case == 'missing':
        path.unlink()
    elif case == 'live':
        monkeypatch.setattr('runtime_protocol.platform.process_identity', lambda pid: {'pid': pid, 'birth_id': 'birth'})
        monkeypatch.setattr(legacy_identity, '_policy', lambda: None)
    elif case == 'unknown_process':
        def unknown(pid):
            raise OSError('unavailable')
        monkeypatch.setattr('runtime_protocol.platform.process_identity', unknown)
    elif case == 'door':
        monkeypatch.setattr(backend, '_legacy_doors_stopped', lambda discovery, version: False)
    elif case == 'manager':
        monkeypatch.setattr(backend.service, 'legacy_manager_stopped', lambda data: False)
    elif case == 'row':
        with (current(root).root / 'manifest.json').open('a') as stream:
            stream.write(' ')
    elif case == 'policy':
        monkeypatch.setattr(legacy_identity, '_policy', lambda: None)
    elif case == 'changed':
        def change(discovery, version):
            path.write_text(json.dumps({**discovery, 'pid': 124}))
            return True
        monkeypatch.setattr(backend, '_legacy_doors_stopped', change)
    with pytest.raises((RuntimeError, ValueError)):
        backend.source_checkout_ownership(data, current(root))


def test_door_observation_requires_connection_refused(monkeypatch):
    class Socket:
        result = errno.ECONNREFUSED
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, timeout): pass
        def connect_ex(self, address): return self.result
    monkeypatch.setattr(socket, 'socket', Socket)
    version = type('Version', (), {'env': {'ASTRID_RUNTIME_DOOR_PORT': '4567'}})()
    discovery = {'endpoint': 'http://127.0.0.1:1234', 'door_endpoint': 'http://127.0.0.1:4567'}
    assert Backend._legacy_doors_stopped(discovery, version)
    Socket.result = 0
    assert not Backend._legacy_doors_stopped(discovery, version)
    Socket.result = errno.ETIMEDOUT
    assert not Backend._legacy_doors_stopped(discovery, version)
    assert not Backend._legacy_doors_stopped({}, version)
    assert not Backend._legacy_doors_stopped({'endpoint': 'http://example.com:1234'}, version)
    assert not Backend._legacy_doors_stopped({'endpoint': 'http://127.0.0.1:1234'}, version)
    version.env.clear()
    assert not Backend._legacy_doors_stopped(discovery, version)


def test_manager_observation_requires_owned_idle_unit(tmp_path, monkeypatch):
    root, data = tmp_path / 'program', tmp_path / 'data'
    fields = {'MainPID': '0', 'FragmentPath': '', 'LoadState': 'loaded', 'ActiveState': 'inactive',
              'SubState': 'dead', 'TasksCurrent': '0'}
    def run(*args, **kwargs):
        return '\n'.join(f'{key}={value}' for key, value in fields.items())
    service = Service(root, root / 'log', run)
    service.system = 'linux'
    fields['FragmentPath'] = str(service.path)
    monkeypatch.setattr(service, 'systemd', lambda: True)
    monkeypatch.setattr(service, 'recorded', lambda: {'owner': 'haus.astrid.runtime', 'program_root': str(root), 'data_root': str(data)})
    assert service.legacy_manager_stopped(data)
    for key, value in [('MainPID', '42'), ('TasksCurrent', '1'), ('ActiveState', 'active'), ('LoadState', 'not-found')]:
        original = fields[key]
        fields[key] = value
        assert not service.legacy_manager_stopped(data)
        fields[key] = original
    monkeypatch.setattr(service, 'systemd', lambda: False)
    assert not service.legacy_manager_stopped(data)


def test_stale_selected_row_with_native_observations_composed(legacy, monkeypatch):
    from astrid.installer.core import write_json
    root, data, _, discovery, _ = legacy
    old = current(root)
    record_path = old.root / 'version.json'
    record = json.loads(record_path.read_text())
    record['env']['ASTRID_RUNTIME_DOOR_PORT'] = '4567'
    write_json(record_path, record)
    discovery['door_endpoint'] = 'http://127.0.0.1:4567'
    write_json(data / 'runtime/discovery.json', discovery)
    backend = backend_for(root)
    backend.service.system = 'linux'
    monkeypatch.setattr('runtime_protocol.platform.process_identity', lambda pid: None)
    monkeypatch.setattr(backend.service, 'systemd', lambda: True)
    monkeypatch.setattr(backend.service, 'recorded', lambda: {
        'owner': 'haus.astrid.runtime', 'program_root': str(root), 'data_root': str(data)})
    monkeypatch.setattr(backend.service, 'run', lambda *args, **kwargs:
                        f'MainPID=0\nFragmentPath={backend.service.path}\nLoadState=loaded\n'
                        'ActiveState=inactive\nSubState=dead\nTasksCurrent=0\n')
    class RefusedSocket:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, timeout): pass
        def connect_ex(self, address): return errno.ECONNREFUSED
    monkeypatch.setattr(socket, 'socket', RefusedSocket)
    assert backend._legacy_doors_stopped(discovery, current(root))
    assert backend.service.legacy_manager_stopped(data)
    with pytest.raises(RuntimeError, match='Cannot prove.*stopped'):
        backend.source_checkout_ownership(data, current(root))


def test_stale_discovery_probes_do_not_exclude_an_unadvertised_healthy_door(legacy, monkeypatch):
    """A refused stale endpoint is not evidence about another live door."""
    from astrid.installer.core import write_json
    from astrid.installer.legacy_identity import admitted

    root, data, _, discovery, _ = legacy
    old = current(root)
    record = json.loads((old.root / 'version.json').read_text())

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, *args):
            pass

    with socket.socket() as old_private, socket.socket() as old_door, HTTPServer(('127.0.0.1', 0), Health) as live:
        old_private.bind(('127.0.0.1', 0))
        old_door.bind(('127.0.0.1', 0))
        record['env']['ASTRID_RUNTIME_DOOR_PORT'] = str(old_door.getsockname()[1])
        write_json(old.root / 'version.json', record)
        discovery['endpoint'] = f'http://127.0.0.1:{old_private.getsockname()[1]}'
        discovery['door_endpoint'] = f'http://127.0.0.1:{old_door.getsockname()[1]}'
        write_json(data / 'runtime/discovery.json', discovery)
        thread = threading.Thread(target=live.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{live.server_port}/api/runtime/v1/health', timeout=2) as response:
                assert json.load(response) == {'status': 'ok'}
            backend = backend_for(root)
            monkeypatch.setattr('runtime_protocol.platform.process_identity', lambda pid: None)
            monkeypatch.setattr(backend.service, 'legacy_manager_stopped', lambda data: True)
            assert admitted(current(root), {}, discovery, program_root=root, stopped=True)
            assert backend._legacy_doors_stopped(discovery, current(root))
            with pytest.raises(RuntimeError, match='Cannot prove.*stopped'):
                backend.source_checkout_ownership(data, current(root))
        finally:
            live.shutdown()
            thread.join()
