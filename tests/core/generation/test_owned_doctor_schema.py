"""Owned doctor schema: replay genuine pinned CPU captures; preserve every gate."""
from __future__ import annotations

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import venv

import pytest

from astrid.core.generation.engine_contracts import CustodyUnresolved, IntegrityError
from astrid.core.generation.engine_runner import _object_info_sha256
from astrid.core.generation.managed_preparation import (
    ManagedEnginePreparation, native_failure_classes, native_failure_diagnostic,
)
from astrid.packs.vibecomfy.native_preparation import native_doctor_program

CAPTURE = Path(__file__).resolve().parents[2] / 'fixtures/comfyui_protocol/owned_doctor_qwen_cpu.json'


@pytest.fixture
def captured_schema():
    captured = json.loads(CAPTURE.read_text())
    assert captured['source_revision'] == '423cbd20511becb1f16122ab518e92e58bfa03d9'
    assert captured['torch'] == '2.10.0+cpu'
    return captured['object_info']


@pytest.fixture
def native_python(tmp_path):
    import vibecomfy
    environment = tmp_path / 'native'
    venv.EnvBuilder(with_pip=False, symlinks=False).create(environment)
    python = str(environment / 'bin/python')
    site = next((environment / 'lib').glob('python*/site-packages'))
    dependencies = Path(sys.prefix).resolve() / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'
    (site / 'test-dependencies.pth').write_text(str(dependencies) + '\n')
    source = tmp_path / 'vibecomfy'
    shutil.copytree(Path(vibecomfy.__file__).resolve().parent.parent, source,
                    ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc', '.venv', 'node_modules', 'build', 'dist'))
    result = subprocess.run([shutil.which('uv'), '--system-certs', 'pip', 'install', '--python', python, '--no-deps', str(source)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, 'native pinned VibeComfy fixture installation failed'
    return python


@pytest.fixture
def schema_server(captured_schema):
    state = SimpleNamespace(info=captured_schema, status=200, requests=[])
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state.requests.append(self.path)
            body = json.dumps(state.info).encode()
            self.send_response(state.status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Location', 'http://127.0.0.1:1/private')
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.endpoint = f'http://127.0.0.1:{server.server_port}'
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def latent_workflow(tmp_path):
    from vibecomfy.templates import ReadyMetadata, new_workflow, OutputSpec
    from vibecomfy.nodes.core import EmptyLatentImage, SaveLatent
    from vibecomfy.workflow_bundle import emit_bundle_with_candidate
    from vibecomfy.security.provenance import Provenance
    with new_workflow(ReadyMetadata.build(template_id='owned-doctor-cpu', capability='image')) as workflow:
        latent = EmptyLatentImage(width=16, height=16, batch_size=1)
        saved = SaveLatent(samples=latent.out('LATENT'), filename_prefix='astrid')
        workflow.finalize({}, outputs=[OutputSpec(node=saved)])
    return emit_bundle_with_candidate(workflow, tmp_path / 'workflow.py', Provenance.USER_CONFIRMED, None).python_path


def _native(native_python, schema_server, workflow, tmp_path, *, expected=None):
    argv = ['doctor', str(workflow), '--non-interactive', '--yes']
    request = {'argv': argv, 'endpoint': schema_server.endpoint,
               'expected_schema_sha256': expected or _object_info_sha256(schema_server.info), 'request_timeout': 5}
    env = {**os.environ, 'COMFYUI_PATH': str(tmp_path), 'VIBECOMFY_MODELS_ROOT': str(tmp_path / 'models'),
           'VIBECOMFY_HEADLESS': '1'}
    return subprocess.run([native_python, '-I', '-c', native_doctor_program(), json.dumps(request)],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)


def test_original_doctor_uses_owned_cpu_schema_without_offline_fallback(native_python, schema_server, latent_workflow, tmp_path):
    old = subprocess.run([native_python, '-I', '-c', 'import sys;from vibecomfy.cli import main;sys.exit(main(sys.argv[1:]))',
                          'doctor', str(latent_workflow), '--non-interactive', '--yes'],
                         cwd=tmp_path, env={**os.environ, 'VIBECOMFY_MODELS_ROOT': str(tmp_path / 'models')},
                         capture_output=True, text=True, timeout=30)
    assert old.returncode == 1 and 'Layer: Schema reconciliation' in old.stdout
    result = _native(native_python, schema_server, latent_workflow, tmp_path)
    assert result.returncode == 0, 'owned-schema doctor failed'
    assert schema_server.requests == ['/object_info']


def test_qwen_canonical_schema_uses_genuine_cpu_classes_and_preserves_missing_models(captured_schema, native_python, schema_server, tmp_path, monkeypatch):
    from astrid.core.generation.backends.comfy_schema import TargetSchemaProvider
    from vibecomfy.schema import get_schema_provider
    from vibecomfy.workflow_bundle import load_bundle, WorkflowReconciliationError, WorkflowBundleError
    from vibecomfy.security.provenance import Provenance
    import vibecomfy
    # The checkout the test imports VibeComfy from (CI, Lanes and local runs alike).
    source = Path(vibecomfy.__file__).resolve().parents[1] / 'ready_templates/image/qwen_image_2_1_turbo.py'
    bundle = load_bundle(source, trust=Provenance.USER_CONFIRMED)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('VIBECOMFY_MODELS_ROOT', str(tmp_path / 'absent-models'))
    with pytest.raises(WorkflowReconciliationError) as old:
        bundle.compile(schema_provider=get_schema_provider('local'))
    assert set(old.value.missing_classes) == {'EmptyLatentImage', 'QwenImage21Cache', 'TextEncodeQwenImage21'}
    provider = TargetSchemaProvider.from_object_info(captured_schema, server_url='http://127.0.0.1:8188')
    assert all(provider.get_schema(name) is not None for name in old.value.missing_classes)
    # No model bytes are fabricated: target-backed reconciliation proceeds to
    # the original hard model gate, which must still refuse this cold fixture.
    with pytest.raises(WorkflowBundleError) as error:
        bundle.compile(schema_provider=provider)
    assert not isinstance(error.value, WorkflowReconciliationError)
    assert 'model' in str(error.value).lower()
    doctor = _native(native_python, schema_server, source, tmp_path)
    assert doctor.returncode == 1
    assert native_failure_diagnostic('doctor', doctor.stdout, doctor.stderr)[1] == 'models_missing'


@pytest.mark.parametrize('failure', ['missing_class', 'changed_schema', 'redirect'])
def test_owned_doctor_refuses_missing_changed_or_redirected_schema(native_python, schema_server, latent_workflow, tmp_path, failure):
    expected = _object_info_sha256(schema_server.info)
    if failure == 'missing_class':
        schema_server.info = {name: value for name, value in schema_server.info.items() if name != 'EmptyLatentImage'}
        expected = _object_info_sha256(schema_server.info)
    elif failure == 'changed_schema':
        schema_server.info = copy.deepcopy(schema_server.info)
        schema_server.info['EmptyLatentImage']['description'] += ' changed'
    else:
        schema_server.status = 302
    result = _native(native_python, schema_server, latent_workflow, tmp_path, expected=expected)
    assert result.returncode != 0
    step, reason = native_failure_diagnostic('doctor', result.stdout, result.stderr)
    assert step == 'doctor'
    assert reason == {'missing_class': 'schema_reconciliation_failed', 'changed_schema': 'owned_schema_changed',
                      'redirect': 'owned_schema_unavailable'}[failure]
    if failure == 'missing_class':
        assert native_failure_classes(result.stdout, reason) == ('EmptyLatentImage',)
    assert schema_server.requests == ['/object_info']


def test_missing_classes_only_expose_public_pinned_identifiers():
    secret = 'PRIVATE_MODEL_NAME_AND_TOKEN'
    output = 'Layer: Schema reconciliation\n- workflow contains unresolved class types: EmptyLatentImage, QwenImage21Cache, ' + secret
    assert native_failure_classes(output, 'schema_reconciliation_failed') == ('EmptyLatentImage', 'QwenImage21Cache')
    assert native_failure_classes(output, 'workflow_diagnosis_failed') == ()


@pytest.mark.parametrize('change', ['file_picker', 'model_picker'])
def test_owned_capture_uses_exact_health_digest_projection(native_python, schema_server, latent_workflow, tmp_path, change):
    expected = _object_info_sha256(schema_server.info)
    schema_server.info = copy.deepcopy(schema_server.info)
    if change == 'file_picker':
        schema_server.info['LoadImage']['input']['required']['image'][0] = ['new-input.png']
    else:
        schema_server.info['UNETLoader']['input']['required']['unet_name'][0] = ['new-model.safetensors']
    # cause d6b5e6aef; ruling: machines 08:05 Sat
    assert _object_info_sha256(schema_server.info) == expected
    result = _native(native_python, schema_server, latent_workflow, tmp_path, expected=expected)
    assert result.returncode == 0


@pytest.mark.parametrize('failure', ['command', 'post_health', 'stop_uncertain'])
def test_doctor_failure_stops_exact_owned_engine_and_retains_uncertain_lock(tmp_path, failure):
    prep = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    prep.check = lambda: None
    prep.deadline = time.monotonic() + 30
    prep.recipe = SimpleNamespace(start=SimpleNamespace(port=8188))
    prep.uncertain = False
    prep._alignment_verification = ()
    prep._verify_alignment = lambda: None
    events = []
    health = SimpleNamespace(object_info_sha256='a' * 64)
    def check_health():
        events.append('health')
        if failure == 'post_health' and events.count('health') >= 2:
            health.object_info_sha256 = 'b' * 64
    prep._alignment_runner = SimpleNamespace(health=health, start=lambda **kw: events.append('start'), check_health=check_health)
    def native(*args, **kwargs):
        events.append('doctor')
        assert kwargs['step'] == 'doctor'
        if failure != 'post_health':
            raise IntegrityError('managed alignment command failed (step=doctor, reason=schema_reconciliation_failed)')
    prep._native = native
    def stop(*, deadline):
        events.append('stop_exact')
        assert deadline > time.monotonic()
        if failure == 'stop_uncertain':
            raise TimeoutError('PRIVATE_STOP_ERROR')
    prep.context = SimpleNamespace(service_client=SimpleNamespace(stop_owned=stop))
    prep.lock = SimpleNamespace(__exit__=lambda *args: events.append('unlock'))
    prep.report = None
    if failure == 'stop_uncertain':
        with pytest.raises(CustodyUnresolved, match='stop remains unconfirmed') as error:
            prep._command(['doctor', '/workflow.py'])
        assert 'PRIVATE_STOP_ERROR' not in str(error.value)
        with pytest.raises(CustodyUnresolved):
            prep.close()
        assert prep.uncertain and 'unlock' not in events
    else:
        with pytest.raises(IntegrityError):
            prep._command(['doctor', '/workflow.py'])
        prep.close()
        assert events[-2:] == ['stop_exact', 'unlock']
    assert events[0:3] == ['start', 'health', 'doctor']
    assert events.count('stop_exact') == 1


def test_doctor_does_not_stop_a_refused_engine_owner():
    from astrid.core.generation.engine_contracts import EndpointOwnershipError
    prep = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    prep.check = lambda: None
    prep._alignment_verification = ()
    prep._verify_alignment = lambda: None
    prep.uncertain = False
    prep.report = None
    events = []
    def refuse(**kwargs): raise EndpointOwnershipError('service incarnation or launch binding differs')
    prep._alignment_runner = SimpleNamespace(start=refuse)
    prep.context = SimpleNamespace(service_client=SimpleNamespace(
        stop_owned=lambda **kwargs: pytest.fail('refused owner was stopped')))
    prep.lock = SimpleNamespace(__exit__=lambda *args: events.append('unlock'))
    with pytest.raises(EndpointOwnershipError): prep._doctor(['doctor', '/workflow.py'])
    prep.close()
    assert events == ['unlock']
