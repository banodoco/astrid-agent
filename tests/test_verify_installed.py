"""Focused verifier policy and live bridge negative control."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION
from scripts.verify_installed import bridge_observation, evaluate
from tests.lanes import unavailable


def _local_health_fixture(tmp_path):
    discovery = {'endpoint': 'http://127.0.0.1:31415', 'runtime_instance_id': 'instance-1',
                 'active_realm': 'realm-1', 'identity': {'release': 1, 'managed_by': 'pinokio'}}
    path = tmp_path / 'discovery.json'
    path.write_text(json.dumps(discovery))
    config = {'schemaVersion': 1, 'endpoint': discovery['endpoint'], 'actorId': 'astrid-app',
              'realmId': 'realm-1', 'runtimeInstanceId': 'instance-1'}
    health = {'status': 'ok', 'protocol': 'workspace.v1', 'schema_digest': 'sha256:example',
              'runtime_instance_id': 'instance-1', 'identity': discovery['identity'].copy(),
              'services': [{'name': 'chat', 'state': 'running'}]}
    return path, discovery, config, health


def test_local_editor_health_binds_selected_discovery_and_raw_owner(tmp_path, monkeypatch):
    from scripts import verify_installed as verifier
    path, discovery, config, health = _local_health_fixture(tmp_path)
    calls = []
    def fake_request(url, headers=None):
        calls.append((url, headers))
        if url.endswith('/runtime-config/v1/workspace.json'):
            return config
        return health.copy()
    monkeypatch.setattr(verifier, 'request', fake_request)
    assert verifier.local_health(path, discovery) == health
    assert [url for url, _ in calls] == [
        'http://127.0.0.1:5180/runtime-config/v1/workspace.json',
        'http://127.0.0.1:5180/api/runtime/v1/health',
        'http://127.0.0.1:31415/v1/health',
        'http://127.0.0.1:5180/runtime-config/v1/workspace.json',
    ]
    assert calls[1][1] == calls[2][1] == {'X-Astrid-Owner-Identity': '1'}


@pytest.mark.parametrize('mutation', [
    'foreign-endpoint', 'foreign-instance', 'foreign-realm', 'missing-actor',
    'foreign-owner-identity', 'foreign-owner-instance', 'foreign-owner-services',
    'missing-owner-digest', 'foreign-editor-identity', 'changed-config', 'changed-discovery', 'unsafe-endpoint',
])
def test_local_editor_health_fails_closed(tmp_path, monkeypatch, mutation):
    from scripts import verify_installed as verifier
    path, discovery, config, health = _local_health_fixture(tmp_path)
    owner = {**health, 'identity': health['identity'].copy(), 'services': list(health['services'])}
    if mutation == 'foreign-endpoint': config['endpoint'] = 'http://127.0.0.1:9'
    if mutation == 'foreign-instance': config['runtimeInstanceId'] = 'foreign'
    if mutation == 'foreign-realm': config['realmId'] = 'foreign'
    if mutation == 'missing-actor': config['actorId'] = ''
    if mutation == 'foreign-owner-identity': owner['identity']['managed_by'] = None
    if mutation == 'foreign-owner-instance': owner['runtime_instance_id'] = 'foreign'
    if mutation == 'foreign-owner-services': owner['services'] = []
    if mutation == 'missing-owner-digest': owner.pop('schema_digest')
    if mutation == 'foreign-editor-identity': health['identity'] = {'release': 2, 'managed_by': 'pinokio'}
    if mutation == 'unsafe-endpoint':
        discovery['endpoint'] = 'http://127.0.0.1:31415@evil.example:80'
        path.write_text(json.dumps(discovery))
    config_reads = 0
    def fake_request(url, headers=None):
        nonlocal config_reads
        if url.endswith('/runtime-config/v1/workspace.json'):
            config_reads += 1
            return {**config, 'realmId': 'changed'} if mutation == 'changed-config' and config_reads == 2 else config.copy()
        if url.startswith('http://127.0.0.1:5180'):
            return health
        if mutation == 'changed-discovery': path.write_text(json.dumps({**discovery, 'pid': 7}))
        return owner
    monkeypatch.setattr(verifier, 'request', fake_request)
    with pytest.raises(ValueError):
        verifier.local_health(path, discovery)


@pytest.mark.parametrize('source', ['hosted', 'local-editor'])
def test_observe_keeps_service_and_capability_failures_for_each_health_source(tmp_path, monkeypatch, source):
    from scripts import verify_installed as verifier
    root = tmp_path / 'version'
    root.mkdir()
    data = tmp_path / 'data'
    runtime = data / 'runtime'
    runtime.mkdir(parents=True)
    def write(path, value):
        path.write_text(json.dumps(value))
    write(root / 'manifest.json', {'tools': {'python': {'version': '3.13.0'}}})
    declaration = root / 'services.json'
    write(declaration, {'services': [{'name': 'chat', 'argv': [str(root / 'python')]}]})
    profile = root / 'profile.json'
    write(profile, {'service_declaration': str(declaration)})
    matrix = root / 'matrix.json'
    write(matrix, {'capabilities': [{'id': 'required', 'disposition': 'required'}]})
    version = root / 'version.json'
    write(version, {'root': str(root), 'python': str(root / 'python'), 'profile': str(profile),
                    'env': {'ASTRID_CAPABILITY_MATRIX_PATH': str(matrix)}})
    write(runtime / 'discovery.json', {'runtime_instance_id': 'instance'})
    write(runtime / 'generic-host.ready.json', {'status': 'ready', 'runtime_instance_id': 'instance',
          'registration': {'capabilities': [{'id': 'required', 'ready': False}]},
          'unready_capabilities': ['required']})
    write(runtime / 'chat.json', {})
    health = {'runtime_instance_id': 'instance', 'services': [{'name': 'chat', 'state': 'stopped'}]}
    routes = []
    monkeypatch.setattr(verifier, 'request', lambda url, *_: routes.append(url) or health)
    monkeypatch.setattr(verifier, 'local_health', lambda *_: routes.append('local') or health)
    monkeypatch.setattr(verifier, 'installed_runtime_contract', lambda *_: {})
    monkeypatch.setattr(verifier, 'legacy_identity_observation', lambda *_: (False, {'problem': 'identity'}))
    monkeypatch.setattr(verifier, 'bridge_observation', lambda *_: (True, {}))
    result = verifier.observe(version, data, health_source=source)
    assert routes == (['local'] if source == 'local-editor' else ['http://127.0.0.1:17340/api/runtime/v1/health'])
    assert result['observations']['runtime.identity'] is False
    assert result['observations']['service.chat'] is False
    assert result['observations']['origin.imports'] is False
    assert result['observations']['capabilities.required'] is False


def test_update_only_fails_regressions_and_reports_preexisting():
    before = {'observations': {'old': False, 'healthy': True}}
    result = evaluate({'observations': {'old': False, 'healthy': False}}, 'update', before)
    assert result['failures'] == ['healthy']
    assert result['preexisting_unhealthy'] == ['old']
    assert not result['passed']
    assert evaluate({'observations': {'old': False, 'healthy': True}}, 'update', before)['passed']
    assert not evaluate({'observations': {'old': False}}, 'install')['passed']


@pytest.mark.omp
def test_broken_real_bridge_model_command_fails_verify(tmp_path):
    bun = os.environ.get('ASTRID_TEST_BUN')
    if not bun or not Path(bun).is_file():
        unavailable('set ASTRID_TEST_BUN to run the real bridge negative control')
    env = {k: v for k, v in os.environ.items() if not k.endswith('_API_KEY') and not k.endswith('_TOKEN')}
    env.update(ASTRID_WORKSPACE_DISCOVERY=str(tmp_path / 'discovery.json'),
               ASTRID_ACP_CWD=str(tmp_path), ASTRID_ACP_BRIDGE_PORT='0',
               ASTRID_ACP_COMMAND=str(tmp_path / 'intentionally-missing-command'))
    proc = subprocess.Popen([bun, str(Path(__file__).resolve().parents[1] / 'astrid/chat/service.mjs')],
                            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        registration = tmp_path / 'chat.json'
        deadline = time.monotonic() + 15
        while not registration.exists() and time.monotonic() < deadline and proc.poll() is None:
            time.sleep(.05)
        assert registration.exists(), 'real bridge did not start'
        chat = json.loads(registration.read_text())
        try:
            ok, _ = bridge_observation(chat, 'models')
        except Exception:
            ok = False
        receipt = evaluate({'observations': {'chat.models': ok}}, 'install')
        assert receipt['failures'] == ['chat.models']
        assert not receipt['passed']
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)


def test_model_catalog_requires_real_well_formed_providers(monkeypatch):
    from scripts import verify_installed as verifier

    signed_out = {'currentModel': None, 'providers': [{'id': 'openrouter', 'name': 'OpenRouter',
        'signedIn': False, 'usable': False, 'reason': 'needs_sign_in', 'models': []}]}
    chat = {'endpoint': 'http://127.0.0.1:1', 'token': 'local'}
    for catalog, expected in [
        (signed_out, True),
        ({'currentModel': None, 'providers': []}, False),
        ({'currentModel': None, 'providers': [{}]}, False),
        ({'currentModel': None, 'providers': [{**signed_out['providers'][0], 'models': [{}]}]}, False),
        ({'currentModel': None, 'providers': [{**signed_out['providers'][0], 'id': ''}]}, False),
    ]:
        monkeypatch.setattr(verifier, 'request', lambda *_args, catalog=catalog: catalog)
        observed, _ = verifier.bridge_observation(chat, 'models')
        receipt = verifier.evaluate({'observations': {'chat.models': observed}}, 'install')
        assert receipt['passed'] is expected


def test_chat_rejects_non_loopback_before_sending_token(monkeypatch):
    from scripts import verify_installed as verifier
    monkeypatch.setattr(verifier, 'request', lambda *_: pytest.fail('token sent'))
    for endpoint in ('http://example.com:1234', 'http://192.168.1.1:1234',
                     'https://127.0.0.1:1234', 'http://127.0.0.1:1234@evil.example:80'):
        with pytest.raises(ValueError):
            verifier.bridge_observation({'endpoint': endpoint, 'token': 'secret'}, 'models')


def test_bridge_does_not_follow_redirect(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from scripts import verify_installed as verifier

    requests = []
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(302)
            self.send_header('Location', 'http://example.com/stolen')
            self.end_headers()
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(Exception):
            verifier.bridge_observation({'endpoint': f'http://127.0.0.1:{server.server_port}', 'token': 'secret'}, 'models')
        assert requests == ['/models']
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.posix_native
def test_service_executable_symlink_escape_is_rejected(tmp_path):
    from scripts.verify_installed import installed_executable
    root = tmp_path / 'install'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.write_text('outside')
    link = root / 'executable'
    try:
        link.symlink_to(outside)
    except OSError:
        unavailable('symlinks unavailable')
    assert not installed_executable(link, root)
    inside = root / 'real'
    inside.write_text('inside')
    link.unlink()
    link.symlink_to(inside)
    assert installed_executable(link, root)


@pytest.mark.posix_native
def test_pinned_bootstrap_python_link_allowed_but_external_or_wrong_pin_rejected(tmp_path):
    from scripts.verify_installed import installed_python
    version = tmp_path / 'versions' / 'candidate'
    executable = version / 'py' / 'bin' / 'python'
    executable.parent.mkdir(parents=True)
    bootstrap = tmp_path / 'astrid' / 'bootstrap' / 'python'
    pinned = bootstrap / 'cpython-3.11.13-linux-x86_64-gnu' / 'bin' / 'python3.11'
    pinned.parent.mkdir(parents=True)
    pinned.write_text('python')
    try:
        executable.symlink_to(pinned)
    except OSError:
        unavailable('symlinks unavailable')
    assert installed_python(executable, version, bootstrap, '3.11.13')
    assert not installed_python(executable, version, bootstrap, '3.11.12')
    assert not installed_python(pinned, version, bootstrap, '3.11.13')
    external = tmp_path / 'runner-python'
    external.write_text('python')
    executable.unlink()
    executable.symlink_to(external)
    assert not installed_python(executable, version, bootstrap, '3.11.13')
    # A bootstrap-looking directory cannot redirect the real executable outside.
    pinned.unlink()
    pinned.symlink_to(external)
    executable.unlink()
    executable.symlink_to(pinned)
    assert not installed_python(executable, version, bootstrap, '3.11.13')


def _legacy_identity_fixture(tmp_path):
    import base64
    fixture = json.loads((Path(__file__).parent / 'fixtures/upgrade/legacy_identityless_v31.json').read_text())
    program = tmp_path / 'program'
    root = program / 'versions' / '3204bb00-6d53a190-legacy01'
    root.mkdir(parents=True)
    (root / 'r').mkdir()
    (root / 'a').mkdir()
    (root / 'v').mkdir()
    (root / 'py').mkdir(exist_ok=True)
    (program / 'current').write_text(root.name)
    (program / 'install.json').write_text(json.dumps({'managed_by': None}))
    profile = root / 'profile.json'
    source_checkout = root / 'a' / 'source'
    source_checkout.mkdir(parents=True)
    declaration = root / 'a' / 'service.json'
    declaration.write_text('{}')
    profile.write_text(json.dumps({'runtime_checkout': str(root / 'r'), 'source_checkout': str(source_checkout),
                                   'runtime_environment': str(root / 'py'), 'source_digest': 'a' * 64,
                                   'service_declaration': str(declaration)}))
    python = root / 'py' / 'bin' / 'python'
    python.parent.mkdir(parents=True)
    python.write_text('fixture python')
    installed_manifest = dict(fixture['manifest'])
    installed_manifest['vibecomfy'] = {'sha': VIBECOMFY_ENGINE_REVISION}
    (root / 'manifest.json').write_text(json.dumps(installed_manifest, indent=2, sort_keys=True) + '\n')
    version_path = root / 'version.json'
    fixture['version'] = {'root': str(root), 'python': str(python), 'profile': str(profile), 'env': {}}
    runtime = {'protocol': 'workspace.v1', 'schema_digest': 'sha256:9061d4982c786e04d99e45470b8ea4f3d9c9ff9855c4a2f83d751a93408c205b', 'workspace_schema_version': 31}
    return fixture, version_path, runtime


def _identity_aware_fixture(tmp_path):
    import base64
    source = json.loads((Path(__file__).parent / 'fixtures/upgrade/identity_aware_version_v1.json').read_text())
    program = tmp_path / 'program'
    root = program / 'versions' / 'e8d04151-d8704b7a-identityaware'
    root.mkdir(parents=True)
    (root / 'r').mkdir()
    (root / 'a').mkdir()
    (root / 'v').mkdir()
    (root / 'py').mkdir()
    (program / 'current').write_text(root.name)
    (program / 'install.json').write_text(json.dumps({'managed_by': None}))
    profile = root / 'profile.json'
    source_checkout = root / 'a' / 'source'
    source_checkout.mkdir(parents=True)
    declaration = root / 'a' / 'service.json'
    declaration.write_text('{}')
    profile.write_text(json.dumps({'runtime_checkout': str(root / 'r'), 'source_checkout': str(source_checkout),
                                   'runtime_environment': str(root / 'py'), 'source_digest': 'b' * 64,
                                   'service_declaration': str(declaration)}))
    python = root / 'py' / 'bin' / 'python'
    python.parent.mkdir(parents=True)
    python.write_text('fixture python')
    raw = base64.b64decode(source['manifest_base64'])
    installed_manifest = dict(source['manifest'])
    installed_manifest['vibecomfy'] = {'sha': VIBECOMFY_ENGINE_REVISION}
    raw = (json.dumps(installed_manifest, indent=2, sort_keys=True) + '\n').encode()
    (root / 'manifest.json').write_bytes(raw)
    version_path = root / 'version.json'
    identity = {'release': source['manifest']['release'], 'managed_by': None}
    fixture = {
        'manifest': source['manifest'],
        'health': {**source['health_shape'], 'identity': identity},
        'discovery': {**source['discovery_shape'], 'identity': identity},
        'version': {'root': str(root), 'python': str(python), 'profile': str(profile), 'env': {}, 'identity': identity},
    }
    runtime = {'protocol': source['health_shape']['protocol'], 'schema_digest': source['manifest']['runtime']['schema_digest'],
               'workspace_schema_version': 31}
    return fixture, version_path, runtime, raw


def test_supported_legacy_schema_accepts_matching_instance_and_exact_provenance(tmp_path):
    from scripts.verify_installed import legacy_identity_observation
    fixture, version_path, runtime = _legacy_identity_fixture(tmp_path)
    raw = (Path(fixture['version']['root']) / 'manifest.json').read_bytes()
    ok, details = legacy_identity_observation(fixture['health'], fixture['discovery'], fixture['version'],
                                               fixture['manifest'], raw, version_path, runtime)
    assert ok
    assert details['mode'] == 'supported-legacy-schema'
    assert details['health_runtime_instance_id'] == 'synthetic-instance-7'
    assert details['health_protocol'] == 'workspace.v1'
    assert details['health_schema_digest'] == 'sha256:9061d4982c786e04d99e45470b8ea4f3d9c9ff9855c4a2f83d751a93408c205b'
    assert details['health_identity'] is None
    assert details['discovery_identity'] is None
    assert details['version_identity'] is None
    assert details['version_record_fields'] == ['env', 'profile', 'python', 'root']
    assert json.loads(raw)['vibecomfy']['sha'] == VIBECOMFY_ENGINE_REVISION


def test_source_backed_identity_aware_schema_accepts_synthetic_matching_identity_shape(tmp_path):
    """Synthetic identity values exercise policy; exact Runtime sources prove the fields exist."""
    from scripts.verify_installed import legacy_identity_observation
    fixture, version_path, runtime, raw = _identity_aware_fixture(tmp_path)
    assert fixture['health']['identity'] == fixture['discovery']['identity'] == fixture['version']['identity']
    ok, details = legacy_identity_observation(fixture['health'], fixture['discovery'], fixture['version'],
                                               fixture['manifest'], raw, version_path, runtime)
    assert ok
    assert details['mode'] == 'identity-aware'


@pytest.mark.parametrize('mutation', ['unknown-key', 'changed-pin'])
def test_installed_manifest_mutation_fails_reviewed_schema_hash(tmp_path, mutation):
    from scripts.verify_installed import legacy_identity_observation
    fixture, version_path, runtime = _legacy_identity_fixture(tmp_path)
    root = Path(fixture['version']['root'])
    installed = json.loads((root / 'manifest.json').read_text())
    if mutation == 'unknown-key':
        installed['unreviewed_field'] = {'value': True}
    else:
        installed['agent']['sha'] = 'f' * 40
    raw = (json.dumps(installed, indent=2, sort_keys=True) + '\n').encode()
    (root / 'manifest.json').write_bytes(raw)
    fixture['manifest'] = installed
    ok, details = legacy_identity_observation(fixture['health'], fixture['discovery'], fixture['version'],
                                               installed, raw, version_path, runtime)
    assert not ok
    assert any('installed manifest' in problem or 'reviewed schema' in problem for problem in details['problems'])


def test_installed_manifest_noncanonical_writer_representation_fails(tmp_path):
    from scripts.verify_installed import legacy_identity_observation
    fixture, version_path, runtime = _legacy_identity_fixture(tmp_path)
    root = Path(fixture['version']['root'])
    raw = (root / 'manifest.json').read_bytes()
    (root / 'manifest.json').write_bytes(raw.replace(b'  "agent"', b'    "agent"', 1))
    # The parsed content remains the reviewed content, but real installers
    # emit sorted, two-space indented JSON with one trailing newline.
    ok, details = legacy_identity_observation(fixture['health'], fixture['discovery'], fixture['version'],
                                               fixture['manifest'], (root / 'manifest.json').read_bytes(),
                                               version_path, runtime)
    assert not ok
    assert 'installed manifest representation does not match reviewed installer writer' in details['problems']


@pytest.mark.parametrize('unknown_schema', [
    {'workspace_schema_version': 32},
    {'schema_digest': 'sha256:' + 'f' * 64},
])
def test_identity_aware_unknown_or_newer_schema_fails_closed_with_matching_identity(tmp_path, unknown_schema):
    from scripts.verify_installed import legacy_identity_observation
    fixture, version_path, runtime, raw = _identity_aware_fixture(tmp_path)
    runtime.update(unknown_schema)
    fixture['health']['schema_digest'] = runtime['schema_digest']
    ok, details = legacy_identity_observation(fixture['health'], fixture['discovery'], fixture['version'],
                                               fixture['manifest'], raw, version_path, runtime)
    assert not ok
    assert fixture['health']['identity'] == fixture['discovery']['identity'] == fixture['version']['identity']
    assert any('reviewed schema' in problem for problem in details['problems'])


def test_identity_aware_bad_version_directory_prefix_fails_closed(tmp_path):
    from scripts.verify_installed import legacy_identity_observation
    fixture, version_path, runtime, raw = _identity_aware_fixture(tmp_path)
    fixture['version']['root'] = str(Path(fixture['version']['root']).parent / 'unreviewed-prefix')
    assert fixture['health']['identity'] == fixture['discovery']['identity'] == fixture['version']['identity']
    ok, details = legacy_identity_observation(fixture['health'], fixture['discovery'], fixture['version'],
                                               fixture['manifest'], raw, version_path, runtime)
    assert not ok
    assert 'version directory provenance does not match reviewed schema' in details['problems']


def test_identity_aware_release_missing_health_and_discovery_identity_fails_closed(tmp_path):
    from scripts.verify_installed import legacy_identity_observation
    fixture, version_path, runtime, raw = _identity_aware_fixture(tmp_path)
    fixture['health'].pop('identity')
    fixture['discovery'].pop('identity')
    ok, details = legacy_identity_observation(fixture['health'], fixture['discovery'], fixture['version'],
                                               fixture['manifest'], raw, version_path, runtime)
    assert not ok
    assert fixture['manifest']['release'] == 1
    assert 'health/discovery/version identities are not exactly equal' in details['problems']


def test_unknown_modern_schema_without_health_identity_fails_closed(tmp_path):
    from scripts.verify_installed import legacy_identity_observation
    fixture, version_path, runtime = _legacy_identity_fixture(tmp_path)
    fixture['manifest']['runtime']['sha'] = 'a' * 40
    raw = (Path(fixture['version']['root']) / 'manifest.json').read_bytes()
    ok, details = legacy_identity_observation(fixture['health'], fixture['discovery'], fixture['version'],
                                               fixture['manifest'], raw, version_path, runtime)
    assert not ok
    assert 'runtime_sha mismatch' in details['problems']


def test_newer_workspace_schema_without_identity_fails_closed(tmp_path):
    from scripts.verify_installed import legacy_identity_observation
    fixture, version_path, runtime = _legacy_identity_fixture(tmp_path)
    runtime['workspace_schema_version'] = 32
    raw = (Path(fixture['version']['root']) / 'manifest.json').read_bytes()
    ok, details = legacy_identity_observation(fixture['health'], fixture['discovery'], fixture['version'],
                                               fixture['manifest'], raw, version_path, runtime)
    assert not ok
    assert 'workspace_schema_version mismatch' in details['problems']


def test_legacy_manifest_provenance_mismatch_fails_closed(tmp_path):
    from scripts.verify_installed import legacy_identity_observation
    fixture, version_path, runtime = _legacy_identity_fixture(tmp_path)
    original_root = Path(fixture['version']['root'])
    fixture['version']['root'] = str(Path(fixture['version']['root']).parent / 'wrong-version')
    raw = (original_root / 'manifest.json').read_bytes()
    ok, details = legacy_identity_observation(fixture['health'], fixture['discovery'], fixture['version'],
                                               fixture['manifest'], raw, version_path, runtime)
    assert not ok
    assert 'version root does not match selected version record' in details['problems']


def test_legacy_profile_runtime_checkout_provenance_mismatch_fails_closed(tmp_path):
    from scripts.verify_installed import legacy_identity_observation
    fixture, version_path, runtime = _legacy_identity_fixture(tmp_path)
    profile = Path(fixture['version']['profile'])
    profile.write_text(json.dumps({'runtime_checkout': '/outside/runtime'}))
    raw = (Path(fixture['version']['root']) / 'manifest.json').read_bytes()
    ok, details = legacy_identity_observation(fixture['health'], fixture['discovery'], fixture['version'],
                                               fixture['manifest'], raw, version_path, runtime)
    assert not ok
    assert 'installed profile provenance is missing or malformed' in details['problems'] or any(
        'profile Runtime checkout provenance' in problem for problem in details['problems'])


def test_legacy_profile_missing_source_checkout_fails_closed(tmp_path):
    from scripts.verify_installed import legacy_identity_observation
    fixture, version_path, runtime = _legacy_identity_fixture(tmp_path)
    root = Path(fixture['version']['root'])
    profile = Path(fixture['version']['profile'])
    value = json.loads(profile.read_text())
    Path(value['source_checkout']).rmdir()
    profile.write_text(json.dumps(value))
    raw = (root / 'manifest.json').read_bytes()
    ok, details = legacy_identity_observation(fixture['health'], fixture['discovery'], fixture['version'],
                                               fixture['manifest'], raw, version_path, runtime)
    assert not ok
    assert any('profile source_checkout provenance' in problem for problem in details['problems'])


@pytest.mark.posix_native
def test_legacy_profile_runtime_checkout_symlink_escape_fails_closed(tmp_path):
    from scripts.verify_installed import legacy_identity_observation
    fixture, version_path, runtime = _legacy_identity_fixture(tmp_path)
    root = Path(fixture['version']['root'])
    runtime_dir = root / 'r'
    runtime_dir.rmdir()
    outside = tmp_path / 'outside-runtime'
    outside.mkdir()
    try:
        runtime_dir.symlink_to(outside, target_is_directory=True)
    except OSError:
        unavailable('symlinks unavailable')
    raw = (root / 'manifest.json').read_bytes()
    ok, details = legacy_identity_observation(fixture['health'], fixture['discovery'], fixture['version'],
                                               fixture['manifest'], raw, version_path, runtime)
    assert not ok
    assert 'profile Runtime checkout provenance does not match installed version' in details['problems']


@pytest.mark.posix_native
def test_legacy_profile_runtime_environment_symlink_escape_fails_closed(tmp_path):
    from scripts.verify_installed import legacy_identity_observation
    fixture, version_path, runtime = _legacy_identity_fixture(tmp_path)
    root = Path(fixture['version']['root'])
    python = Path(fixture['version']['python'])
    python.unlink()
    python.parent.rmdir()
    environment = root / 'py'
    environment.rmdir()
    outside = tmp_path / 'outside-python'
    outside.mkdir()
    try:
        environment.symlink_to(outside, target_is_directory=True)
    except OSError:
        unavailable('symlinks unavailable')
    raw = (root / 'manifest.json').read_bytes()
    ok, details = legacy_identity_observation(fixture['health'], fixture['discovery'], fixture['version'],
                                               fixture['manifest'], raw, version_path, runtime)
    assert not ok
    assert 'profile Runtime environment provenance does not match installed version' in details['problems']


def test_identity_aware_identity_mismatch_fails_closed(tmp_path):
    from scripts.verify_installed import legacy_identity_observation
    fixture, version_path, runtime, raw = _identity_aware_fixture(tmp_path)
    fixture['health']['identity'] = {'release': 2, 'managed_by': None}
    ok, details = legacy_identity_observation(fixture['health'], fixture['discovery'], fixture['version'],
                                               fixture['manifest'], raw, version_path, runtime)
    assert not ok
    assert 'health/discovery/version identities are not exactly equal' in details['problems']
