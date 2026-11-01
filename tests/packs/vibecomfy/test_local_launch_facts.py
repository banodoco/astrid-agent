"""Native launch observations are additive to read-only HTTP discovery."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation import target_launch
from astrid.packs.vibecomfy import local


@pytest.fixture(autouse=True)
def no_real_process_or_http(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('discovery attempted a real process, observer or HTTP request')
    monkeypatch.setattr(local, '_get_json', forbidden)
    monkeypatch.setattr(target_launch, 'ProcLaunchObserver', forbidden)
    monkeypatch.setattr(target_launch.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(target_launch.ComfyTargetClient, 'request_json', forbidden)


@pytest.fixture
def observed(tmp_path, monkeypatch):
    root = tmp_path / 'selected ComfyUI'
    root.mkdir()
    (root / 'main.py').write_text('# never executed\n')
    python = tmp_path / 'venv' / 'bin' / 'python'
    python.parent.mkdir(parents=True)
    python.write_bytes(b'fake interpreter')
    python.chmod(0o700)
    process = target_launch.ProcessObservation(42, 'boot:123',
        (str(python), './main.py', '--port', '8199'), str(root), str(python))
    state = SimpleNamespace(process=process, listeners=(process,), calls=[])
    def listeners(endpoint, *, deadline):
        state.calls.append(('listeners', endpoint, deadline))
        return state.listeners
    def observe(pid, *, deadline):
        state.calls.append(('process', pid, deadline))
        return state.process
    observer = SimpleNamespace(listeners=listeners, process=observe)
    monkeypatch.setattr(target_launch, 'ProcLaunchObserver', lambda: observer)
    monkeypatch.setattr(local.time, 'monotonic', lambda: 1.0)
    server = {'url': 'http://127.0.0.1:8199', 'base_dir': str(root),
              'python': '/unverified/python', 'argv': ['untrusted HTTP argv']}
    return server, process, state, observer


def test_discovery_retains_exact_native_launch_and_birth(observed):
    server, process, state, observer = observed
    original = dict(server)
    result = local._observed_launch_facts(server, observer=observer)
    assert result == {'python': process.argv[0],
        'launch_app': str(Path(server['base_dir']) / 'main.py'),
        'launch_argv': list(process.argv),
        'launch_process': {'pid': process.pid, 'birth_id': process.birth_id}}
    assert server == original
    assert [call[0] for call in state.calls] == ['listeners', 'process', 'listeners']
    assert all(call[2] == 1.2 for call in state.calls)


@pytest.mark.parametrize('fault', [
    'ambiguous', 'no-listener', 'root', 'python', 'birth', 'relative-python',
    'wrapper', 'port', 'http-root', 'missing-app', 'changed-process',
])
def test_discovery_never_promotes_guesses_or_mismatched_process(observed, fault):
    server, process, state, observer = observed
    if fault == 'ambiguous':
        state.listeners = (process, process)
    elif fault == 'no-listener':
        state.listeners = ()
    elif fault == 'http-root':
        server['base_dir'] = '/unrelated/ComfyUI'
    elif fault == 'missing-app':
        (Path(server['base_dir']) / 'main.py').unlink()
    elif fault == 'changed-process':
        state.process = replace(process, birth_id='boot:reused')
    else:
        changes = {
            'root': {'root': '/unrelated/ComfyUI'},
            'python': {'executable': '/other/python'},
            'birth': {'birth_id': ''},
            'relative-python': {'argv': ('python', *process.argv[1:])},
            'wrapper': {'argv': (process.argv[0], '-m', 'comfy_launcher')},
            'port': {'argv': (*process.argv[:-1], '8200')},
        }
        state.process = replace(process, **changes[fault])
        state.listeners = (state.process,)
    assert local._observed_launch_facts(server, observer=observer) == {}


def test_discovery_rechecks_path_identity_before_retaining_facts(observed):
    server, _, state, observer = observed
    observe = observer.process
    def tamper(*args, **kwargs):
        (Path(server['base_dir']) / 'main.py').write_text('# replaced during observation\n')
        return observe(*args, **kwargs)
    observer.process = tamper
    assert local._observed_launch_facts(server, observer=observer) == {}


@pytest.mark.parametrize('expired', [False, True])
def test_discovery_respects_existing_probe_deadline(observed, expired):
    server, _, state, observer = observed
    result = local._observed_launch_facts(server, observer=observer,
                                         deadline=0.9 if expired else 1.1)
    if expired:
        assert result == {} and not state.calls
    else:
        assert result['launch_process']['pid'] == 42
        assert all(call[2] == 1.1 for call in state.calls)


def test_discovery_unknown_native_evidence_leaves_http_fields_alone(observed):
    server, _, _, observer = observed
    def unavailable(*args, **kwargs):
        raise PermissionError('native owner unavailable')
    observer.listeners = unavailable
    assert local._observed_launch_facts(server, observer=observer) == {}
    assert server['python'] == '/unverified/python'
    assert 'launch_argv' not in server


def test_probe_server_adds_only_verified_native_launch_facts(observed, monkeypatch):
    server, process, _, _ = observed
    requests = []
    payloads = {
        '/system_stats': {'system': {'argv': ['main.py', '--unsafe-http-argument']}, 'devices': []},
        '/internal/folder_paths': {'custom_nodes': [str(Path(server['base_dir']) / 'custom_nodes')]},
        '/userdata?dir=workflows': [],
    }
    def get_json(url, **kwargs):
        assert url.startswith(server['url'])
        path = url.removeprefix(server['url'])
        requests.append(path)
        return payloads[path]
    monkeypatch.setattr(local, '_get_json', get_json)
    result, errors = local._probe_server(8199, deadline=1.1)
    assert errors == []
    assert result['launch_argv'] == list(process.argv)
    assert result['python'] == process.argv[0]
    assert result['argv'] == ['main.py', '--unsafe-http-argument']
    assert requests == list(payloads)
