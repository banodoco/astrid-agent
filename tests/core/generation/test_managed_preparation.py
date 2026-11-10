"""Host-origin preparation refuses drift and retains uncertain native custody."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from astrid.core.generation.engine_contracts import CommandResult, CustodyUnresolved, IntegrityError
from astrid.core.generation.managed_preparation import (
    ManagedEnginePreparation, PREPARATION_ENV, SCHEMA, digest, read_preparation,
)


@pytest.fixture
def preparation(tmp_path, monkeypatch):
    source = tmp_path / 'workflow.json'
    source.write_text('{}')
    manifest = {'engine': 'comfyui', 'base': None, 'models': [], 'extra': {'nodes': []},
                'models_root': str(tmp_path / 'models')}
    body = {'schema': SCHEMA, 'task_id': 'task', 'attempt_id': 'attempt', 'execution_identity': 'execution',
        'inputs': {'workflow': hashlib.sha256(source.read_bytes()).hexdigest()},
        'manifest': manifest, 'manifest_sha256': digest(manifest),
        'installation': {'root': str(tmp_path / 'install'), 'receipt_sha256': 'a' * 64,
                         'recipe_sha256': 'b' * 64, 'helper_sha256': 'c' * 64},
        'endpoint': 'http://127.0.0.1:8188', 'output_dir': str(tmp_path / 'output'), 'placement': 'managed',
        'mode': 'check', 'alignment': {'mode': 'check', 'missing': [], 'fetched': [], 'verified': True, 'status': 'ready'},
        'health': {'healthy': True, 'object_info_sha256': 'd' * 64, 'required_nodes_present': True}}
    monkeypatch.delenv('ASTRID_ALIGN', raising=False)
    return body, source


@pytest.mark.parametrize('change', ['task', 'mode', 'verified', 'manifest-extra', 'partial-inputs', 'relative-models', 'relative-output', 'source-drift'])
def test_preparation_refuses_other_attempt_or_changed_inputs(preparation, monkeypatch, change):
    body, source = preparation
    if change == 'task': body['task_id'] = 'another-task'
    if change == 'mode': body['mode'] = 'trust'
    if change == 'verified': body['alignment']['verified'] = False
    if change == 'manifest-extra':
        body['manifest']['lockfile'] = '/ambient/lock'
        body['manifest_sha256'] = digest(body['manifest'])
    if change == 'partial-inputs': body['inputs'] = {'python': 'a' * 64}
    if change == 'relative-models':
        body['manifest']['models_root'] = 'models'
        body['manifest_sha256'] = digest(body['manifest'])
    if change == 'relative-output': body['output_dir'] = 'output'
    if change == 'source-drift': source.write_text('{"changed":true}')
    monkeypatch.setenv(PREPARATION_ENV, json.dumps(body))
    with pytest.raises(ValueError):
        read_preparation(task='task', attempt='attempt', endpoint=body['endpoint'],
                         execution_identity='execution', inputs={'workflow': source})


def test_preparation_accepts_only_exact_host_attempt_bytes(preparation, monkeypatch):
    body, source = preparation
    monkeypatch.setenv(PREPARATION_ENV, json.dumps(body))
    assert read_preparation(task='task', attempt='attempt', endpoint=body['endpoint'],
                            execution_identity='execution', inputs={'workflow': source}) == body


def test_uncertain_preparation_keeps_lock_and_canonical_staging(tmp_path):
    from astrid.packs.vibecomfy.shared.bundle_inputs import staged_workflow_path
    originals = {name: tmp_path / name for name in ('python', 'companion', 'source')}
    for path in originals.values(): path.write_text(path.name)
    staged = None
    with pytest.raises(CustodyUnresolved):
        with staged_workflow_path(workflow=None, **originals, scratch=tmp_path / 'staged',
                                  preserve_on_error=(CustodyUnresolved,)) as (workflow, _):
            staged = workflow
            raise CustodyUnresolved('owned command still may read this workflow')
    assert staged is not None and staged.is_file()
    assert staged.with_name('workflow.vibe.json').is_file()
    prep = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    prep.uncertain = True
    prep.lock = SimpleNamespace(__exit__=lambda *args: pytest.fail('uncertain lock released'))
    with pytest.raises(CustodyUnresolved): prep.close()


def test_alignment_failure_keeps_native_stderr_out_of_public_error(tmp_path, caplog):
    from astrid.core.generation.engine_recipe import load_recipe
    from astrid.core.generation.engine_platforms import platform_adapter
    prep = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    prep.check = lambda: None
    prep.root = str(tmp_path)
    prep.task, prep.attempt = 'task', 'attempt'
    prep.recipe = load_recipe('comfyui', 'linux-x86_64-cu130')
    prep.adapter = platform_adapter(prep.recipe.platform)
    prep.deadline = time.monotonic() + 10
    secret = 'PRIVATE_STDERR_SENTINEL'
    prep.context = SimpleNamespace(run=lambda request: CommandResult(9, '', secret, False, 0.1, None, False, False))
    with pytest.raises(IntegrityError) as error:
        prep._native('fixture program', step='doctor')
    assert secret not in str(error.value) and secret not in caplog.text
    assert 'host log' in str(error.value)


def test_configured_models_root_is_read_only_and_refuses_unsafe_receipt(tmp_path):
    from astrid.core.generation.managed_preparation import configured_models_root
    root = tmp_path / 'absent'
    assert configured_models_root(root=str(root)) == str(root / 'models')
    assert not root.exists()
    receipt = root / '.astrid-engine/install.json'
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"invalid":true}')
    with pytest.raises((IntegrityError, ValueError)):
        configured_models_root(root=str(root))


@pytest.mark.parametrize('drained', [False, True])
def test_preparation_retains_lock_when_worker_or_prompt_drainage_is_unconfirmed(tmp_path, monkeypatch, drained):
    prep = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    prep.uncertain = False
    prep.report = {'endpoint': 'http://127.0.0.1:8188'}
    released = []
    prep.lock = SimpleNamespace(__exit__=lambda *args: released.append(True))
    def cleanup(*args, **kwargs):
        assert kwargs['confirm'] is True
        raise TimeoutError('owned prompt remains running')
    monkeypatch.setattr('astrid.core.generation.backends.vibecomfy_local_server.cleanup_prompts', cleanup)
    with pytest.raises((CustodyUnresolved, TimeoutError)):
        prep.close(execution_root=tmp_path, worker_drained=drained)
    assert prep.uncertain and prep.lock is not None and not released


def test_prompt_cleanup_confirms_owned_absence_and_never_cancels_foreign(tmp_path, monkeypatch):
    from astrid.core.generation.backends.vibecomfy_local_server import cleanup_prompts
    path = tmp_path / '.vibecomfy-runtime/out/runs/owned/attempt.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'queue_acceptance': {'status': 'accepted', 'prompt_id': 'owned'}}))
    calls, observations = [], iter([['owned', 'foreign'], ['foreign']])
    class Client:
        def __init__(self, *args): pass
        def witness_prompt(self, value): calls.append(('witness', value))
        def cancel_prompt(self, value): calls.append(('cancel', value))
        def request_json(self, path):
            return {'queue_running': [[0, value] for value in next(observations)], 'queue_pending': []}
    monkeypatch.setattr('astrid.core.generation.backends.vibecomfy_local_server.ComfyTargetClient', Client)
    cleanup_prompts('http://127.0.0.1:8188', tmp_path, confirm=True)
    assert calls == [('witness', 'owned'), ('cancel', 'owned')]


def test_prompt_cleanup_timeout_and_unknown_acceptance_refuse_release(tmp_path, monkeypatch):
    from astrid.core.generation.backends.vibecomfy_local_server import cleanup_prompts
    path = tmp_path / '.vibecomfy-runtime/out/runs/owned/attempt.json'
    path.parent.mkdir(parents=True)
    class Client:
        def __init__(self, *args): pass
        def witness_prompt(self, value): pass
        def cancel_prompt(self, value): pass
        def request_json(self, path): return {'queue_running': [[0, 'owned']], 'queue_pending': []}
    monkeypatch.setattr('astrid.core.generation.backends.vibecomfy_local_server.ComfyTargetClient', Client)
    path.write_text(json.dumps({'queue_acceptance': {'status': 'accepted', 'prompt_id': 'owned'}}))
    with pytest.raises(TimeoutError, match='not drained'):
        cleanup_prompts('http://127.0.0.1:8188', tmp_path, confirm=True, timeout=0)
    path.write_text(json.dumps({'queue_acceptance': {'status': 'not_attempted', 'prompt_id': None},
                               'terminal': {'phase': 'prepared'}}))
    with pytest.raises(ValueError, match='prepared prompt outcome remains unconfirmed'):
        cleanup_prompts('http://127.0.0.1:8188', tmp_path, confirm=True, timeout=0)
    path.write_text(json.dumps({'queue_acceptance': {'status': 'unknown', 'prompt_id': None}}))
    with pytest.raises(ValueError, match='acceptance remains unconfirmed'):
        cleanup_prompts('http://127.0.0.1:8188', tmp_path, confirm=True, timeout=0)



@pytest.mark.parametrize('cancel', [False, True])
def test_host_canonical_preparation_preserves_cancel_and_readiness(tmp_path, monkeypatch, cancel):
    from astrid.core.execution.generic_host import _prepare_vibecomfy_execution_identity, HostCancelled, HostError
    from astrid.packs.vibecomfy import production_engine
    source = tmp_path / 'source.json'; source.write_text('{}')
    monkeypatch.setattr(production_engine, 'load_workflow_path', lambda *args, **kwargs: object())
    monkeypatch.setattr(production_engine, 'loaded_workflow_execution_identity', lambda *args: 'identity')
    error = HostCancelled('cancelled after schema preflight') if cancel else production_engine.ProductionEngineError('missing node')
    if not cancel:
        error.code = 'missing_nodes'
        error.readiness = {'placement': 'managed', 'target': {'kind': 'managed', 'endpoint': 'http://127.0.0.1:8188'}}
    def prepare(*args): raise error
    with pytest.raises(HostCancelled if cancel else HostError) as observed:
        _prepare_vibecomfy_execution_identity({'workflow': str(source)}, tmp_path / 'scratch', None, prepare_engine=prepare)
    if not cancel:
        assert observed.value.code == 'missing_nodes'
        assert observed.value.readiness == error.readiness


def test_trust_diagnostics_translate_only_exact_declared_operational_rows():
    from astrid.core.generation.backends.comfy_schema import TargetSchemaProvider
    row = {'name': 'absent.bin', 'subdir': 'aux', 'sha256': 'a' * 64, 'size': 123}
    provider = TargetSchemaProvider.from_object_info({}, server_url='http://127.0.0.1:8188')
    provider.trust_model_assets([row])
    diagnostic = {**row, 'code': 'target_model_asset', 'model_bytes_status': 'verified_target_provisioning'}
    unrelated = {**diagnostic, 'sha256': 'b' * 64}
    provider._approval_diagnostics = [diagnostic, unrelated]
    assert provider._approval_diagnostics[0]['model_bytes_status'] == 'declared_unverified'
    assert provider._approval_diagnostics[1] == unrelated
    assert diagnostic['model_bytes_status'] == 'verified_target_provisioning'



def test_native_asset_capsule_authoring(tmp_path):
    from vibecomfy.templates import ReadyMetadata, new_workflow, OutputSpec
    from vibecomfy.nodes.core import EmptyImage, SaveImage
    from vibecomfy.workflow_bundle import emit_bundle_with_candidate, load_bundle
    from vibecomfy.security.provenance import Provenance
    # Native ReadyMetadata stores source-backed rows as string witnesses;
    # author in its normalized key order so publication round-trips exactly.
    asset = {'name': 'cpu-required.bin', 'url': 'http://127.0.0.1:1234/model', 'subdir': 'checkpoints',
             'sha256': 'a' * 64, 'size_bytes': 22}
    with new_workflow(ReadyMetadata.build(template_id='plan32-cpu', capability='image', model_assets=[asset])) as workflow:
        image = EmptyImage(width=16, height=16, batch_size=1, color=0)
        saved = SaveImage(images=image.out('IMAGE'), filename_prefix='astrid')
        workflow.finalize({}, outputs=[OutputSpec(node=saved)])
    emitted = emit_bundle_with_candidate(workflow, tmp_path / 'workflow.py', Provenance.USER_CONFIRMED, None)
    loaded = load_bundle(emitted.python_path, trust=Provenance.USER_CONFIRMED)
    assert loaded.workflow.metadata['model_assets'] == [asset]



def test_preparation_scope_is_attempt_bound_and_does_not_leak_across_threads(preparation, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from astrid.core.generation.managed_preparation import preparation_scope
    body, source = preparation
    other = {**body, 'task_id': 'another-task'}
    monkeypatch.setenv(PREPARATION_ENV, json.dumps(other))
    def read():
        return read_preparation(task='task', attempt='attempt', endpoint=body['endpoint'],
                                execution_identity='execution', inputs={'workflow': source})
    with preparation_scope(body):
        assert read() == body
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(ValueError): pool.submit(read).result()
    with pytest.raises(ValueError): read()



def test_native_pip_uses_real_python312_and_exact_recipe_selector(tmp_path):
    import importlib.util, subprocess
    from astrid.packs.vibecomfy.native_preparation import native_pip_program
    interpreter = Path('/usr/bin/python3.12')
    if not interpreter.is_file(): pytest.skip('real CPython 3.12 interpreter is unavailable')
    venv = tmp_path / 'python312'
    subprocess.run([str(interpreter), '-I', '-m', 'venv', '--without-pip', str(venv)],
                   check=True, capture_output=True, text=True, timeout=20)
    python = str(venv / 'bin/python')
    completed = subprocess.run([python, '-I', '-c', native_pip_program(), json.dumps({'python_executable': python})],
                               check=True, capture_output=True, text=True, timeout=30)
    report = json.loads(completed.stdout)
    assert report['installed']['version'] == report['bundled_version'] and len(report['wheel_sha256']) == 64
    # The shared machine has 3.12.3, not the recipe's 3.12.10 binary. Exercise
    # the exact tagged 3.12.10 selector source separately, without replacing the
    # runtime or pretending that its full binary was qualified here.
    path = Path(__file__).resolve().parents[2] / 'fixtures/plan32_cpython31210_ensurepip_selector.py'
    spec = importlib.util.spec_from_file_location('plan32_recipe_ensurepip', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module._WHEEL_PKG_DIR = None; module._PACKAGES = None
    package = module._get_packages()['pip']
    assert package.wheel_name == f'pip-{module.version()}-py3-none-any.whl'
    assert package.wheel_path is None
    wheels = tmp_path / 'selected-wheels'; wheels.mkdir()
    (wheels / 'pip-99.0-py3-none-any.whl').write_bytes(b'selector-only fixture')
    module._WHEEL_PKG_DIR = str(wheels); module._PACKAGES = None
    assert module._get_packages()['pip'].wheel_path == str(wheels / 'pip-99.0-py3-none-any.whl')



def test_native_pip_rejects_a_selected_wheel_symlink_escape(tmp_path):
    import subprocess, sys
    from astrid.packs.vibecomfy.native_preparation import native_pip_program
    trusted = tmp_path / 'interpreter'; trusted.mkdir()
    outside = tmp_path / 'outside.whl'; outside.write_bytes(b'outside wheel')
    selected = trusted / 'pip.whl'; selected.symlink_to(outside)
    program = "import ensurepip,sys;from contextlib import nullcontext;sys.base_prefix=" + repr(str(trusted)) + ";ensurepip._get_pip_whl_path_ctx=lambda:nullcontext(" + repr(str(selected)) + ")\n" + native_pip_program()
    completed = subprocess.run([sys.executable, '-I', '-c', program, json.dumps({'python_executable': sys.executable})],
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode != 0 and 'wheel escaped the verified interpreter' in completed.stderr


def test_native_alignment_diagnostics_never_forward_arbitrary_output(tmp_path, caplog):
    from astrid.core.generation.managed_preparation import native_failure_diagnostic
    from astrid.core.generation.engine_recipe import load_recipe
    from astrid.core.generation.engine_platforms import platform_adapter
    secret = 'PRIVATE_STDOUT_OR_TOKEN_SENTINEL'
    for step in ('runtime_inspect', 'nodes_restore', 'pip_bootstrap', 'fetch', 'prepare', 'doctor', secret):
        found, reason = native_failure_diagnostic(step, secret * 10000, secret * 10000)
        assert secret not in found + reason
    prep = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    prep.check = lambda: None
    prep.root, prep.task, prep.attempt = str(tmp_path), 'task', 'attempt'
    prep.recipe = load_recipe('comfyui', 'linux-x86_64-cu130')
    prep.adapter = platform_adapter(prep.recipe.platform)
    prep.deadline = time.monotonic() + 10
    prep.context = SimpleNamespace(run=lambda request: CommandResult(1, secret, secret, False, 0.1, None, False, False))
    with pytest.raises(IntegrityError, match='step=fetch, reason=model_fetch_failed') as error:
        prep._command(['fetch', '/private/' + secret])
    assert secret not in str(error.value) + caplog.text
    assert 'stdout_sha256=' in caplog.text and 'stderr_sha256=' in caplog.text


def test_real_vibecomfy_doctor_stdout_failure_and_success_are_distinguished(tmp_path, caplog):
    """Run the pinned native CLI; no models, server, provider or GPU involved."""
    import os
    import shutil
    import subprocess
    import sys
    import venv
    import vibecomfy
    from astrid.core.generation.engine_recipe import load_recipe
    from astrid.core.generation.engine_platforms import platform_adapter
    from vibecomfy.templates import ReadyMetadata, new_workflow, OutputSpec
    from vibecomfy.nodes.core import EmptyImage, SaveImage
    from vibecomfy.workflow_bundle import emit_bundle_with_candidate
    from vibecomfy.security.provenance import Provenance

    native = tmp_path / 'native'
    venv.EnvBuilder(with_pip=False, symlinks=False).create(native)
    python = str(native / 'bin/python')
    site = next((native / 'lib').glob('python*/site-packages'))
    dependencies = Path(sys.prefix).resolve() / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'
    (site / 'test-dependencies.pth').write_text(str(dependencies) + '\n')
    staged = tmp_path / 'pinned-vibecomfy'
    shutil.copytree(Path(vibecomfy.__file__).resolve().parent.parent, staged,
                    ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc', '.venv', 'node_modules', 'build', 'dist'))
    installed = subprocess.run([shutil.which('uv'), '--system-certs', 'pip', 'install', '--python', python,
                                '--no-deps', str(staged)],
                               capture_output=True, text=True, timeout=60)
    assert installed.returncode == 0, 'native VibeComfy fixture installation failed'

    prep = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    prep.check = lambda: None
    prep.root, prep.task, prep.attempt = str(tmp_path), 'task', 'attempt'
    prep.recipe = load_recipe('comfyui', 'linux-x86_64-cu130')
    adapter = platform_adapter(prep.recipe.platform)
    prep.adapter = SimpleNamespace(join=adapter.join, venv_python=lambda root: python)
    prep.deadline = time.monotonic() + 60
    (tmp_path / 'runtime/ComfyUI').mkdir(parents=True)
    observed = []
    def run(request):
        completed = subprocess.run(request.argv, cwd=request.cwd, env={**os.environ, **request.env},
                                   capture_output=True, text=True, timeout=30)
        observed.append(completed)
        return CommandResult(completed.returncode, completed.stdout, completed.stderr, False, 0.1, None, False, False)
    prep.context = SimpleNamespace(run=run)
    secret = 'PRIVATE_NATIVE_WORKFLOW_PATH_SENTINEL'
    with pytest.raises(IntegrityError, match='step=doctor, reason=workflow_load_failed') as error:
        prep._native('import sys;from vibecomfy.cli import main;sys.exit(main(sys.argv[1:]))',
                     'doctor', str(tmp_path / secret / 'absent.py'), '--non-interactive', '--yes', step='doctor')
    assert observed[-1].returncode == 1 and observed[-1].stderr == ''
    assert 'Layer: Python scratchpad import/build' in observed[-1].stdout
    assert secret not in str(error.value) + caplog.text

    with new_workflow(ReadyMetadata.build(template_id='native-doctor', capability='image')) as workflow:
        image = EmptyImage(width=16, height=16, batch_size=1, color=0)
        saved = SaveImage(images=image.out('IMAGE'), filename_prefix='astrid')
        workflow.finalize({}, outputs=[OutputSpec(node=saved)])
    emitted = emit_bundle_with_candidate(workflow, tmp_path / 'workflow.py', Provenance.USER_CONFIRMED, None)
    prep._native('import sys;from vibecomfy.cli import main;sys.exit(main(sys.argv[1:]))',
                 'doctor', str(emitted.python_path), '--non-interactive', '--yes', step='doctor')
    assert observed[-1].returncode == 0
