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


def test_preparation_engine_copies_table_entry_and_refuses_missing(monkeypatch):
    from astrid.core.contracts.errors import AstridValueError
    from astrid.core.generation import entries
    from astrid.core.generation.managed_preparation import _preparation_engine

    shared = SimpleNamespace(commands=None)
    monkeypatch.setattr(entries, 'engine_table', lambda: {'comfyui': shared})
    assert _preparation_engine('comfyui') is not shared
    assert _preparation_engine(None) is not shared
    for name, expected in [('unknown', 'unknown'), (None, 'comfyui')]:
        monkeypatch.setattr(entries, 'engine_table', lambda: {})
        with pytest.raises(AstridValueError) as caught:
            _preparation_engine(name)
        assert str(caught.value) == f"no generation.engine contribution for {expected!r}"


def test_preparation_steps_time_success_and_preserve_refusals(monkeypatch):
    import astrid.core.generation.managed_preparation as module

    prep = object.__new__(ManagedEnginePreparation)
    prep.phase_seconds = {}
    ticks = iter([10.0, 12.0, 20.0, 23.0])
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    with prep._phase("installation_verification"):
        pass
    with pytest.raises(IntegrityError, match="unchanged refusal"):
        with prep._phase("runtime_requirements"):
            raise IntegrityError("unchanged refusal")
    assert prep.phase_seconds == {"installation_verification": 2.0, "runtime_requirements": 3.0}


@pytest.mark.parametrize('missing_field', ['sha256', 'size'])
def test_judge_declarations_reports_every_unpinned_ready_model(tmp_path, monkeypatch, missing_field):
    import importlib.util
    from astrid.core.generation.managed_preparation import judge_declarations
    from vibecomfy.registry.ready import workflow_from_ready

    engine_path = Path(__file__).resolve().parents[3] / (
        'astrid/packs/vibecomfy/extensions/generation.engine/comfyui_engine.py')
    spec = importlib.util.spec_from_file_location('managed_preparation_test_engine', engine_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Render the shipped ready template, then fill a real public input before
    # emitting it through judge_declarations' canonical ready-template path.
    workflow = workflow_from_ready('image/z_image_turbo')
    workflow.set_input('prompt', 'a small red lighthouse beside a quiet sea')
    assets = workflow.metadata['model_assets']
    assert len(assets) > 1
    model_names = [asset['name'] for asset in assets]
    engine = module.ComfyUIEngine()
    requirements = engine.workflow_requirements

    def incomplete_requirements(workflows):
        manifest = requirements(workflows)
        for row in manifest['models']:
            row[missing_field] = None
        return manifest

    # Keep the rendered workflow byte-for-byte semantically genuine. Inject
    # incomplete declaration rows at the engine's manifest boundary instead
    # of corrupting canonical workflow metadata before it is serialized.
    monkeypatch.setattr(engine, 'workflow_requirements', incomplete_requirements)

    with pytest.raises(IntegrityError, match='no declared digest') as caught:
        judge_declarations(workflow, tmp_path / 'scratch', engine)

    assert str(caught.value).endswith(', '.join(model_names))
    assert (tmp_path / 'scratch/canonical/workflow.py').is_file()


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


@pytest.mark.parametrize("receipt_bytes", [None, "{"])
def test_preparation_keeps_installation_guidance_for_unreadable_receipt(tmp_path, monkeypatch, receipt_bytes):
    from astrid.core.generation import engine_local_endpoint
    from astrid.core.generation.engine_local_endpoint import EngineNotInstalled

    root = tmp_path / "engine"
    monkeypatch.setattr(engine_local_endpoint, "default_engine_root", lambda _engine: str(root))
    if receipt_bytes is not None:
        receipt = root / ".astrid-engine" / "install.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(receipt_bytes)
    workflow = tmp_path / "workflow.json"
    workflow.write_text("{}")
    preparation = ManagedEnginePreparation(task="task", attempt="attempt", mode="check",
        deadline=time.monotonic() + 5, cancelled=lambda: False, scratch=tmp_path / "scratch")
    try:
        with pytest.raises(EngineNotInstalled, match="astrid engines install comfyui") as error:
            preparation.prepare(workflow, execution_identity="execution", inputs={},
                                canonical_workflow=None, input_digests={})
        assert error.value.next_action == "astrid engines install comfyui"
    finally:
        preparation.close()


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


def test_preparation_failure_remains_primary_when_owned_stop_is_unconfirmed(tmp_path):
    preparation = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    original = ValueError("alignment failed")
    preparation.uncertain = False
    preparation._prepare = lambda *args, **kwargs: (_ for _ in ()).throw(original)
    def fail_stop():
        preparation.uncertain = True
        raise CustodyUnresolved("owned engine stop is unconfirmed")
    preparation._stop_preparing_engine = fail_stop
    try:
        with pytest.raises(ValueError) as caught:
            preparation.prepare(tmp_path / "workflow", execution_identity="execution", inputs={},
                                canonical_workflow=None, input_digests={})
        assert caught.value is original
        assert any("owned engine stop is unconfirmed" in note for note in caught.value.__notes__)
        from astrid.core.generation import managed_preparation
        assert managed_preparation._UNRESOLVED_CUSTODY[id(preparation)] is preparation
    finally:
        from astrid.core.generation import managed_preparation
        managed_preparation._UNRESOLVED_CUSTODY.pop(id(preparation), None)


def test_owned_engine_stop_failure_is_logged_and_reported_as_unconfirmed_custody(monkeypatch, caplog, capsys):
    import logging

    from astrid.core.generation import managed_preparation

    stop_failure = RuntimeError("injected owned stop failure")
    prep = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    prep._preparing_engine = True
    prep.uncertain = False
    service = SimpleNamespace(stop_owned=lambda **_kwargs: None)
    monkeypatch.setattr(service, 'stop_owned', lambda **_kwargs: (_ for _ in ()).throw(stop_failure))
    prep.context = SimpleNamespace(service_client=service)

    with caplog.at_level(logging.DEBUG, logger=managed_preparation.__name__):
        with pytest.raises(CustodyUnresolved) as caught:
            prep._stop_preparing_engine()

    assert prep.uncertain is True
    assert prep._preparing_engine is True
    assert caught.value.code == "custody_unresolved"
    assert str(caught.value) == "managed preparation failed; exact owned engine stop remains unconfirmed"
    assert caught.value.__notes__ == ["owned engine stop failed: RuntimeError"]
    assert "managed preparation owned-engine stop failed" in caplog.text
    assert "injected owned stop failure" in caplog.text
    assert not capsys.readouterr().err


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
    assert configured_models_root(root=str(root)) == str(root / 'runtime/ComfyUI/models')
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
    import importlib.util, os, shutil, subprocess
    from astrid.packs.vibecomfy.native_preparation import native_pip_program
    interpreter = shutil.which('python3.12')
    venv = tmp_path / 'python312'
    if interpreter is None:
        # Hosted images need not carry 3.12 in /usr/bin. As the engine does,
        # provision the recipe's managed Python with uv, within this test root.
        uv = shutil.which('uv')
        assert uv is not None, 'uv is required to provision the recipe Python'
        subprocess.run([uv, '--system-certs', '--no-config', 'venv', '--python', '3.12.10',
                        '--managed-python', str(venv)],
                       env={**os.environ, 'UV_PYTHON_INSTALL_DIR': str(tmp_path / 'interpreters'),
                            'UV_CACHE_DIR': str(tmp_path / 'uv-cache')},
                       check=True, capture_output=True, text=True, timeout=120)
    else:
        subprocess.run([interpreter, '-I', '-m', 'venv', '--without-pip', str(venv)],
                       check=True, capture_output=True, text=True, timeout=20)
    python = str(venv / 'bin/python')
    version = subprocess.run([python, '-I', '-c', 'import sys;print(sys.version_info[:2])'],
                             check=True, capture_output=True, text=True, timeout=10)
    assert version.stdout.strip() == '(3, 12)'
    completed = subprocess.run([python, '-I', '-c', native_pip_program(), json.dumps({'python_executable': python})],
                               check=True, capture_output=True, text=True, timeout=30)
    report = json.loads(completed.stdout)
    assert report['installed']['version'] == report['bundled_version'] and len(report['wheel_sha256']) == 64
    # A PATH interpreter can differ from the recipe's 3.12.10 binary. Exercise
    # the exact tagged selector source separately from the native pip bootstrap.
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


def test_managed_node_lock_declares_real_recipe_patches(tmp_path):
    from astrid.core.generation.engine_recipe import load_recipe
    from astrid.core.generation.managed_preparation import _write_managed_nodes_lock
    from vibecomfy.node_packs import read_lockfile

    recipe = load_recipe('comfyui', 'linux-x86_64-cu130')
    patches = [p for p in recipe.source_patches if p.source == 'comfyui-qwentts']
    assert patches
    source = next(s for s in recipe.sources if s.name == patches[0].source)
    nodes = [{'repo': source.pin.repository, 'commit': source.pin.revision}]
    lock = tmp_path / 'custom_nodes.lock'
    _write_managed_nodes_lock(lock, nodes, recipe)
    entry = read_lockfile(lock)[0]
    assert entry.name == 'ComfyUI-QwenTTS'
    assert [(p.path, p.sha256) for p in entry.patches] == [
        (p.path, p.sha256_after) for p in patches
    ]


def test_managed_node_lock_without_patches_preserves_original_bytes(tmp_path):
    from dataclasses import replace
    from astrid.core.generation.engine_recipe import load_recipe
    from astrid.core.generation.managed_preparation import _write_managed_nodes_lock

    recipe = replace(load_recipe('comfyui', 'linux-x86_64-cu130'), source_patches=())
    lock = tmp_path / 'custom_nodes.lock'
    _write_managed_nodes_lock(lock, [{'repo': 'https://example.test/pack.git', 'commit': 'abc'}], recipe)
    assert lock.read_text() == ('[nodepacks."pack"]\nurl = "https://example.test/pack.git"\n'
                                'commit = "abc"\n')


def test_asset_alignment_accepts_only_verified_recipe_patch_at_recipe_pin(tmp_path):
    import subprocess
    from dataclasses import replace
    from astrid.core.generation.asset_alignment import ManagedAssetTarget, _pack_state
    from astrid.core.generation.engine_recipe import load_recipe

    root = tmp_path / 'nodes'
    pack_path = root / 'ComfyUI-QwenTTS'
    pack_path.mkdir(parents=True)
    def git(*args):
        return subprocess.run(['git', '-C', str(pack_path), *args], check=True, capture_output=True, text=True).stdout.strip()
    git('init')
    git('config', 'user.name', 'Fixture')
    git('config', 'user.email', 'fixture@example.test')
    (pack_path / 'patched.py').write_text('before')
    git('add', 'patched.py')
    git('commit', '-m', 'fixture')
    revision = git('rev-parse', 'HEAD')
    repo = 'https://example.test/ComfyUI-QwenTTS.git'
    git('remote', 'add', 'origin', repo)
    (pack_path / 'patched.py').write_text('after')
    recipe = load_recipe('comfyui', 'linux-x86_64-cu130')
    old_patch = next(p for p in recipe.source_patches if p.source == 'comfyui-qwentts')
    patch = replace(old_patch, path='patched.py', sha256_after=hashlib.sha256(b'after').hexdigest())
    sources = tuple(replace(s, pin=replace(s.pin, repository=repo, revision=revision))
                    if s.name == patch.source else s for s in recipe.sources)
    recipe = replace(recipe, sources=sources, source_patches=(patch,))
    target = ManagedAssetTarget(tmp_path, tmp_path / 'models', root, 'python', 'http://localhost', '',
                                tmp_path / 'cache', recipe=recipe)
    pack = {'name': pack_path.name, 'repository': repo, 'revision': revision}
    assert _pack_state(target, pack) == 'verified'
    (pack_path / 'untracked').write_text('other')
    assert _pack_state(target, pack) == 'mismatch'


def test_managed_node_lock_refuses_a_recipe_patch_at_another_commit(tmp_path):
    import pytest
    from astrid.core.generation.engine_contracts import IntegrityError
    from astrid.core.generation.engine_recipe import load_recipe
    from astrid.core.generation.managed_preparation import _write_managed_nodes_lock

    recipe = load_recipe('comfyui', 'linux-x86_64-cu130')
    patch = next(p for p in recipe.source_patches if p.source == 'comfyui-qwentts')
    source = next(s for s in recipe.sources if s.name == patch.source)
    with pytest.raises(IntegrityError, match='ComfyUI-QwenTTS'):
        _write_managed_nodes_lock(tmp_path / 'custom_nodes.lock',
                                  [{'repo': source.pin.repository, 'commit': '0' * 40}], recipe)


@pytest.fixture
def fetch_volume(tmp_path, monkeypatch):
    """Real preparation + engine alignment on a fake volume; no native download."""
    import os
    import subprocess

    mount = tmp_path / 'volume'
    models = mount / 'engine/models'
    (models / 'checkpoints').mkdir(parents=True)
    have = models / 'checkpoints/have.bin'
    have.write_bytes(b'have')
    rows = [
        {'name': 'have.bin', 'subdir': 'checkpoints', 'size': 4,
         'sha256': hashlib.sha256(b'have').hexdigest()},
        {'name': 'need.bin', 'subdir': 'checkpoints', 'size': 9,
         'sha256': hashlib.sha256(b'need-more').hexdigest()},
    ]
    manifest = {'models_root': str(models), 'models': rows}
    prep = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    prep.mode = 'align'
    prep.deadline = time.monotonic() + 20
    prep.cancelled = lambda: False
    native, quota_calls = [], []
    prep._native = lambda *args, **kwargs: native.append(kwargs['step'])
    original_read = Path.read_text
    device = mount.stat().st_dev
    mountinfo = f'40 1 {os.major(device)}:{os.minor(device)} / {mount} rw - fuse.mfs mfs#host:9421 rw\n'
    monkeypatch.setattr(Path, 'read_text', lambda path, *a, **kw:
        state.mountinfo if str(path) == '/proc/self/mountinfo' else original_read(path, *a, **kw))
    state = SimpleNamespace(free=10, fs_free=10**12, unavailable=False,
                            fs_unavailable=False, query_failure=False, mountinfo=mountinfo,
                            transform=lambda text, _path: text)

    def filesystem_space(_path):
        if state.fs_unavailable:
            raise OSError('PRIVATE_PROVIDER_BODY')
        return SimpleNamespace(f_bavail=state.fs_free, f_frsize=1, f_blocks=10**15)

    monkeypatch.setattr(os, 'statvfs', filesystem_space)

    def quota(argv, **kwargs):
        quota_calls.append(argv)
        if state.unavailable:
            raise FileNotFoundError('PRIVATE_PROVIDER_BODY')
        if state.query_failure:
            return SimpleNamespace(returncode=1, stdout='PRIVATE_PROVIDER_BODY', stderr='PRIVATE_PROVIDER_BODY')
        text = ('quota:\nsoft quota grace period: default\n'
                '          | current value | soft quota | percent | hard quota | percent |\n'
                ' inodes   | 10 | - | - | - | - |\n'
                f' length   | 190 | - | - | {190 + state.free} | 95.00 |\n'
                ' size     | 200 | - | - | - | - |\n'
                ' realsize | 400 | - | - | - | - |\n')
        return SimpleNamespace(returncode=0, stdout=state.transform(text, argv[-1]),
                               stderr='PRIVATE_PROVIDER_BODY')

    monkeypatch.setattr(subprocess, 'run', quota)

    from contextlib import nullcontext
    from astrid.core.generation import engine_runner, entries, managed_preparation
    from astrid.core.generation.engine_recipe import load_recipe
    from tests.core.generation.test_run_local import mod

    # Installation custody is already proven by the existing installation tests.
    # Keep the actual _prepare -> engine.align -> managed _command fetch path.
    prep.root = str(tmp_path / 'install')
    prep.task = 'task-storage'
    prep.logs_root = tmp_path / 'out'
    prep.logs_root.mkdir()
    prep.scratch = tmp_path / 'scratch'
    prep.device = prep.visibility = None
    prep.recipe = load_recipe('comfyui', 'linux-x86_64-cu130')
    prep.adapter = SimpleNamespace(local_install_lock=lambda *a, **kw: nullcontext())
    prep.context = SimpleNamespace(read_json=lambda *a, **kw: {})
    prep._guard_runtime = lambda *a, **kw: None
    receipt = SimpleNamespace(status='installed', requirements_locks={}, source_commits={})
    runner = SimpleNamespace(helper_sha256='a' * 64, locks={}, commits={},
                             helper=lambda _op: {'helper_sha256': 'a' * 64})
    monkeypatch.setattr(engine_runner, '_parse_receipt', lambda *a: receipt)
    monkeypatch.setattr(engine_runner, 'plan_install', lambda *a, **kw: None)
    monkeypatch.setattr(engine_runner, 'plan_from_receipt', lambda *a, **kw: SimpleNamespace(steps=[]))
    monkeypatch.setattr(engine_runner, 'require_current_receipt', lambda *a: None)
    monkeypatch.setattr(engine_runner, '_Runner', lambda *a, **kw: runner)
    monkeypatch.setattr(managed_preparation, 'configured_models_root', lambda **kw: str(models))
    canonical = prep.scratch / 'canonical/workflow.py'
    canonical.parent.mkdir(parents=True)
    canonical.write_text('# canonical CPU fixture')
    monkeypatch.setattr(managed_preparation, 'judge_declarations', lambda *a: (manifest, 'scratchpad'))
    commands = SimpleNamespace(nodes_restore=lambda _nodes: None,
        fetch=lambda _rows: prep._command(['fetch', str(canonical)]),
        prepare_deps_sync=lambda: None, doctor=lambda: None)
    monkeypatch.setattr(entries, 'engine_table', lambda: {'comfyui': mod.ComfyUIEngine(commands)})
    source = tmp_path / 'workflow.json'
    source.write_text('{}')

    class AlignmentComplete(Exception):
        pass

    def alignment_complete(*_args):
        raise AlignmentComplete

    prep._verify_alignment = alignment_complete

    def fetch():
        try:
            prep._prepare(source, execution_identity='execution', inputs={},
                          canonical_workflow=None, input_digests={})
        except AlignmentComplete:
            pass  # Never start an engine in a CPU storage test.

    return SimpleNamespace(prep=prep, fetch=fetch, models=models, have=have, rows=rows,
                           state=state, native=native, quota_calls=quota_calls)


def test_fetch_storage_preflight_refuses_before_any_download(fetch_volume):
    c = fetch_volume
    c.have.unlink()
    with pytest.raises(IntegrityError) as caught:
        c.fetch()
    assert caught.value.code == 'insufficient_storage'
    assert caught.value.details == {'needed_bytes': 13, 'free_bytes': 10}
    assert str(caught.value) == ('Model download refused (reason=insufficient_storage): '
        '13 bytes needed, 10 bytes free; use a larger volume; '
        'see `astrid tasks logs task-storage` (storage.inventory).')
    assert c.native == []


def test_fetch_storage_preflight_enough_space_proceeds(fetch_volume):
    c = fetch_volume
    c.have.unlink()
    c.state.free = 13
    c.fetch()
    assert c.quota_calls, 'fetch bypassed the quota check'
    assert all(argv[:2] == ['mfsgetquota', '-n'] for argv in c.quota_calls)
    assert c.native == ['fetch']


def test_fetch_storage_preflight_skips_already_verified_models(fetch_volume):
    c = fetch_volume
    c.state.free = 9  # fits only the missing model, not the whole 13-byte lock
    c.fetch()
    assert c.quota_calls, 'fetch bypassed verified-present accounting'
    assert c.native == ['fetch']
    assert c.have.read_bytes() == b'have'


def test_fetch_storage_preflight_counts_same_size_bad_digest(fetch_volume):
    c = fetch_volume
    c.have.write_bytes(b'fake')
    c.state.free = 9
    with pytest.raises(IntegrityError) as caught:
        c.fetch()
    assert caught.value.details == {'needed_bytes': 13, 'free_bytes': 9}
    assert c.native == []


def test_fetch_storage_preflight_does_not_credit_unverified_partials(fetch_volume):
    c = fetch_volume
    (c.models / 'checkpoints/need.bin.part').write_bytes(b'need-mor')
    c.state.free = 8
    with pytest.raises(IntegrityError) as caught:
        c.fetch()
    assert caught.value.details == {'needed_bytes': 9, 'free_bytes': 8}
    assert c.native == []


@pytest.mark.parametrize('free,refused', [(8, True), (9, False)], ids=['below-full-size', 'fits-full-size'])
def test_fetch_storage_preflight_stale_part_requires_full_size(fetch_volume, free, refused):
    c = fetch_volume
    part = c.models / 'checkpoints/need.bin.part'
    part.write_bytes(b'need-mor')  # Eight of nine bytes, ignored by native VibeComfy fetch.
    c.state.free = free
    if refused:
        with pytest.raises(IntegrityError) as caught:
            c.fetch()
        assert caught.value.code == 'insufficient_storage'
        assert caught.value.details == {'needed_bytes': 9, 'free_bytes': free}
        assert c.native == []
    else:
        c.fetch()
        assert c.native == ['fetch']
    assert c.quota_calls, 'fetch bypassed the measured capacity check'
    assert part.read_bytes() == b'need-mor'
    assert c.have.read_bytes() == b'have'


def test_fetch_storage_preflight_also_respects_filesystem_free_space(fetch_volume):
    c = fetch_volume
    c.state.fs_free = 8
    with pytest.raises(IntegrityError) as caught:
        c.fetch()
    assert caught.value.details == {'needed_bytes': 9, 'free_bytes': 8}
    assert c.native == []


def _assert_storage_warning(c):
    from astrid.core._shared import run_logs

    collected = run_logs.collect(c.prep.logs_root)
    warning = next(row for row in collected if row['name'] == 'preparation.storage.stderr')
    assert warning['role'] == 'stderr'
    text = (c.prep.logs_root / 'logs' / warning['file']).read_text()
    assert text == 'storage capacity could not be measured; proceeding\n'
    assert 'PRIVATE_PROVIDER_BODY' not in text


def test_fetch_storage_preflight_falls_back_after_failed_quota_query(fetch_volume):
    c = fetch_volume
    c.state.query_failure = True
    c.fetch()
    assert c.native == ['fetch']
    _assert_storage_warning(c)


@pytest.mark.parametrize('kind,current', [('inodes', 10), ('size', 200), ('realsize', 400)])
def test_fetch_storage_preflight_falls_back_for_unaccounted_quota_dimensions(fetch_volume, kind, current):
    c = fetch_volume
    c.state.transform = lambda text, _path: text.replace(
        f'{kind:<8} | {current} | - | - | - | - |',
        f'{kind:<8} | {current} | - | - | 1000 | 1.00 |')
    c.fetch()
    assert c.native == ['fetch']
    _assert_storage_warning(c)


def test_fetch_storage_preflight_respects_soft_limit_without_grace(fetch_volume):
    c = fetch_volume
    c.state.free = 810  # usage190, hard1000; only the soft limit has eight bytes left.
    c.state.transform = lambda text, _path: text.replace(
        'soft quota grace period: default', 'soft quota grace period: 0').replace(
        'length   | 190 | - | - |', 'length   | 190 | 198 | 95.96 |')
    c.fetch()
    assert c.native == ['fetch']


@pytest.mark.parametrize('hard,refused', [(1000, False), (198, True)])
def test_fetch_storage_preflight_only_hard_quota_can_refuse(fetch_volume, hard, refused):
    c = fetch_volume
    c.state.free = hard - 190
    c.state.transform = lambda text, _path: text.replace(
        'soft quota grace period: default', 'soft quota grace period: 24h').replace(
        'length   | 190 | - | - |', 'length   | 190 | 198 | 95.96 |')
    if refused:
        with pytest.raises(IntegrityError) as caught:
            c.fetch()
        assert caught.value.details == {'needed_bytes': 9, 'free_bytes': 8}
        assert c.native == []
    else:
        c.fetch()
        assert c.native == ['fetch']


@pytest.mark.parametrize('first_size,refused', [(4, False), (5, True)])
def test_fetch_storage_preflight_keeps_destination_quota_scopes(fetch_volume, first_size, refused):
    c = fetch_volume
    c.have.unlink()
    first, second = c.models / 'first', c.models / 'second'
    first.mkdir()
    second.mkdir()
    c.rows[0].update(subdir='first', size=first_size,
                     sha256=hashlib.sha256(b'x' * first_size).hexdigest())
    c.rows[1]['subdir'] = 'second'
    c.state.free = 100
    headroom = {str(first): 4, str(second): 9}
    c.state.transform = lambda text, path: text.replace('| 290 |', f'| {190 + headroom.get(path, 100)} |')
    if refused:
        with pytest.raises(IntegrityError) as caught:
            c.fetch()
        assert caught.value.details == {'needed_bytes': 5, 'free_bytes': 4}
        assert '5 bytes needed, 4 bytes free' in str(caught.value)
        assert c.native == []
    else:
        c.fetch()
        assert c.native == ['fetch']
    assert str(first) in {argv[-1] for argv in c.quota_calls}
    if not refused:
        assert str(second) in {argv[-1] for argv in c.quota_calls}


@pytest.mark.parametrize('first_free,second_free', [(5, 10), (4, 9), (12, 12), (4, None)])
def test_fetch_storage_preflight_checks_each_directory_independently(fetch_volume, monkeypatch, first_free, second_free):
    import os

    c = fetch_volume
    c.have.unlink()
    first, second = c.models / 'first', c.models / 'second'
    first.mkdir()
    second.mkdir()
    assert first.stat().st_dev == second.stat().st_dev  # XFS projects may share a device.
    c.rows[0]['subdir'], c.rows[1]['subdir'] = 'first', 'second'
    c.state.mountinfo = c.state.mountinfo.replace('fuse.mfs mfs#host:9421', 'ext4 /dev/fake')
    def space(path):
        value = {first: first_free, second: second_free}.get(path, 1000)
        if value is None:
            raise OSError('PRIVATE_PROVIDER_BODY')
        return SimpleNamespace(f_bavail=value, f_frsize=1, f_blocks=1000)

    monkeypatch.setattr(os, 'statvfs', space)
    c.fetch()
    assert c.native == ['fetch']
    if second_free is None:
        _assert_storage_warning(c)


def test_fetch_storage_preflight_does_not_sum_different_directories_under_shared_quota(fetch_volume):
    c = fetch_volume
    c.have.unlink()
    first, second = c.models / 'first', c.models / 'second'
    first.mkdir()
    second.mkdir()
    c.rows[0]['subdir'], c.rows[1]['subdir'] = 'first', 'second'
    c.state.free = 100
    c.state.transform = lambda text, path: text.replace('| 290 |', '| 202 |') if path == str(c.models) else text
    c.fetch()
    assert c.native == ['fetch']


def test_fetch_storage_preflight_sums_only_one_destination_directory(fetch_volume):
    c = fetch_volume
    c.have.unlink()
    c.state.free = 12
    with pytest.raises(IntegrityError) as caught:
        c.fetch()
    assert caught.value.details == {'needed_bytes': 13, 'free_bytes': 12}
    assert c.native == []


@pytest.mark.parametrize('same_directory,refused', [(False, False), (True, True)])
def test_fetch_storage_preflight_keeps_missing_intended_directories_distinct(fetch_volume, same_directory, refused):
    c = fetch_volume
    c.have.unlink()
    c.rows[0]['subdir'] = 'missing/first'
    c.rows[1]['subdir'] = 'missing/first' if same_directory else 'missing/second'
    c.state.free = 12
    if refused:
        with pytest.raises(IntegrityError) as caught:
            c.fetch()
        assert caught.value.details == {'needed_bytes': 13, 'free_bytes': 12}
        assert c.native == []
    else:
        c.fetch()
        assert c.native == ['fetch']
    assert not (c.models / 'missing').exists()


def test_fetch_storage_preflight_checks_destination_ancestor_quota(fetch_volume):
    c = fetch_volume
    c.state.free = 100
    c.state.transform = lambda text, path: text.replace('| 290 |', '| 198 |') \
        if path == str(c.models / 'checkpoints') else text
    with pytest.raises(IntegrityError) as caught:
        c.fetch()
    assert caught.value.details == {'needed_bytes': 9, 'free_bytes': 8}
    assert c.native == []


def test_fetch_storage_preflight_falls_back_after_malformed_quota_output(fetch_volume):
    c = fetch_volume
    c.state.transform = lambda _text, _path: 'PRIVATE_PROVIDER_BODY'
    c.fetch()
    assert c.native == ['fetch']
    _assert_storage_warning(c)


def test_fetch_storage_preflight_no_quota_tool_with_ample_statvfs_proceeds_and_logs(fetch_volume):
    c = fetch_volume
    c.state.unavailable = True
    c.fetch()
    assert c.native == ['fetch']
    _assert_storage_warning(c)


def test_fetch_storage_preflight_no_quota_tool_with_low_statvfs_refuses(fetch_volume):
    c = fetch_volume
    c.state.unavailable = True
    c.state.fs_free = 8
    with pytest.raises(IntegrityError) as caught:
        c.fetch()
    assert caught.value.code == 'insufficient_storage'
    assert caught.value.details == {'needed_bytes': 9, 'free_bytes': 8}
    assert c.native == []
    names = {row['name'] for row in json.loads((c.prep.logs_root / 'logs.json').read_text())['logs']}
    assert 'storage.inventory' in names
    assert 'preparation.storage.stderr' not in names


def test_fetch_storage_preflight_both_measurements_unavailable_proceeds_and_logs(fetch_volume):
    c = fetch_volume
    c.state.unavailable = c.state.fs_unavailable = True
    c.fetch()
    assert c.native == ['fetch']
    _assert_storage_warning(c)


def test_fetch_storage_preflight_other_network_filesystem_uses_statvfs(fetch_volume):
    c = fetch_volume
    c.state.mountinfo = c.state.mountinfo.replace('fuse.mfs mfs#host:9421', 'nfs server:/volume')
    c.fetch()
    assert c.native == ['fetch'] and c.quota_calls == []
    _assert_storage_warning(c)


@pytest.mark.parametrize('choice', [None, True, False])
def test_fetch_storage_preflight_valid_quota_survives_unavailable_statvfs(
    fetch_volume, generation_settings_store, choice,
):
    c = fetch_volume
    if choice is not None:
        generation_settings_store.documents['generation'] = ({'models': {'trust_unchanged': choice}}, 1)
    c.state.fs_unavailable = True
    c.fetch()
    assert c.native == ['fetch'] and c.quota_calls
    declared = {row['name']: row for row in json.loads((c.prep.logs_root / 'logs.json').read_text())['logs']}
    assert set(declared) == {'preparation.model_verification.stdout', 'preparation.model_verification.stderr'}
    assert json.loads(Path(declared['preparation.model_verification.stdout']['path']).read_text()) == {
        'trust_unchanged': choice is not False, 'hashed': 1, 'skipped': 0,
        'files': [{'path': 'checkpoints/have.bin',
                   'outcome': 'hashed_opt_in' if choice is False else 'hashed_new'}]}
    assert Path(declared['preparation.model_verification.stderr']['path']).read_text() == ''


def test_fetch_preflight_accepts_real_vibecomfy_receipt(fetch_volume, generation_settings_store):
    from vibecomfy.fetch import verify

    c = fetch_volume
    generation_settings_store.documents['generation'] = ({}, 1)
    # Local fixture bytes only: use the pinned writer to prove format compatibility.
    verify({'name': c.have.name, 'sha256': c.rows[0]['sha256'], 'size_bytes': c.have.stat().st_size},
           c.have, root=c.models, force=True)
    c.fetch()
    assert c.native == ['fetch']
    value = json.loads((c.prep.logs_root / '.preparation-logs/model_verification.stdout.log').read_text())
    assert value == {'trust_unchanged': True, 'hashed': 0, 'skipped': 1,
                     'files': [{'path': 'checkpoints/have.bin', 'outcome': 'trusted_vibecomfy_receipt'}]}
    assert len(list((c.models / '.astrid/model-verification').glob('*.json'))) == 1


@pytest.fixture
def inventory_pod(fetch_volume, monkeypatch):
    import os

    c = fetch_volume
    c.state.fs_free = 8
    c.listings, c.listing_failed = [], False
    original = os.scandir

    def listing(path):
        c.listings.append(path)
        if c.listing_failed and not isinstance(path, int) and (
            Path(path) == c.models or c.models in Path(path).parents
        ):
            raise OSError('PRIVATE_PROVIDER_BODY')
        return original(path)

    monkeypatch.setattr(os, 'scandir', listing)
    return c


def _storage_inventory(c):
    from astrid.core._shared import run_logs

    row = next(row for row in run_logs.collect(c.prep.logs_root) if row['name'] == 'storage.inventory')
    return (c.prep.logs_root / 'logs' / row['file']).read_text()


def test_storage_refusal_declares_inventory_with_bounded_sorted_listing(inventory_pod):
    c = inventory_pod
    for i in range(1, 101):
        with (c.models / f'model-{i:03}.bin').open('wb') as stream:
            stream.truncate(2**30 + i)
    with pytest.raises(IntegrityError) as caught:
        c.fetch()
    assert caught.value.details == {'needed_bytes': 9, 'free_bytes': 8}
    text = _storage_inventory(c)
    assert 'fuse.mfs mfs#host:9421' in text and 'statvfs: free_bytes=8' in text
    assert '/model-100.bin' in text and '/model-040.bin\n' not in text
    assert text.index('/model-100.bin') < text.index('/model-099.bin')
    assert len(text.encode()) < 128 * 1024
    assert 'see `astrid tasks logs task-storage` (storage.inventory)' in str(caught.value)
    assert c.native == []


def test_storage_refusal_failed_inventory_preserves_numbers_and_note(inventory_pod):
    c = inventory_pod
    c.listing_failed = True
    with pytest.raises(IntegrityError) as caught:
        c.fetch()
    assert caught.value.code == 'insufficient_storage'
    assert caught.value.details == {'needed_bytes': 9, 'free_bytes': 8}
    assert 'incomplete' in _storage_inventory(c)
    assert 'PRIVATE_PROVIDER_BODY' not in _storage_inventory(c)
    assert c.native == []


def test_storage_inventory_never_deletes_volume_content(inventory_pod):
    c = inventory_pod
    (c.models / 'retained.bin').write_bytes(b'retained version')
    (c.models / 'checkpoints/need.bin.part').write_bytes(b'partial')
    # Include the now volume-local witness in the unchanged-content snapshot.
    c.prep._model_fact(str(c.have), str(c.models), c.rows[0]['sha256'])
    before = {str(p): p.read_bytes() for p in c.models.rglob('*') if p.is_file()}
    with pytest.raises(IntegrityError):
        c.fetch()
    assert {str(p): p.read_bytes() for p in c.models.rglob('*') if p.is_file()} == before
    assert c.models in c.listings
    assert 'storage.inventory' in (c.prep.logs_root / 'logs.json').read_text()


@pytest.fixture
def warm_case(tmp_path, monkeypatch):
    """Real preparation orchestration and disk witness; CPU-only verification edges."""
    from contextlib import nullcontext
    from dataclasses import replace
    import subprocess
    from astrid.core.generation import engine_runner, entries, managed_preparation, settings, warm_attestation
    from astrid.core.generation.engine_recipe import load_recipe, recipe_digest
    from tests.core.generation.test_run_local import mod
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource

    root = tmp_path / 'install'
    state_dir = root / '.astrid-engine'
    state_dir.mkdir(parents=True)
    recipe = load_recipe('comfyui', 'linux-x86_64-cu130')
    recipe = replace(recipe, sources=recipe.sources[:1], environments=recipe.environments[:1])
    checkout = root / recipe.sources[0].relative_path
    checkout.mkdir(parents=True)
    for args in [('init',), ('config', 'user.email', 'cpu@example.invalid'),
                 ('config', 'user.name', 'CPU'), ('add', '.'), ('commit', '--allow-empty', '-m', 'fixture')]:
        subprocess.run(['git', '-C', str(checkout), *args], check=True, capture_output=True)
    (checkout / 'tracked.py').write_text('verified = True\n')
    node_lock = checkout / 'custom_nodes.lock'
    node_lock.write_text('verified node pins\n')
    subprocess.run(['git', '-C', str(checkout), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(checkout), 'commit', '-m', 'source'], check=True, capture_output=True)
    head = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip()
    recipe = replace(recipe, sources=(replace(recipe.sources[0], pin=replace(recipe.sources[0].pin, revision=head)),))
    from astrid.core.generation.engine_contracts import SourceFileRef
    recipe = replace(recipe, environments=(replace(recipe.environments[0], node_lock=SourceFileRef(
        recipe.sources[0].name, 'custom_nodes.lock', hashlib.sha256(node_lock.read_bytes()).hexdigest())),))
    locks = {recipe.environments[0].name: 'b' * 64}
    lockfile = state_dir / 'environments' / recipe.environments[0].name / 'requirements.lock'
    lockfile.parent.mkdir(parents=True)
    lockfile.write_text('verified lock')
    raw = {'receipt_id': 'receipt', 'models_root': None, 'fixture': True}
    (state_dir / 'install.json').write_text(json.dumps(raw))
    receipt = SimpleNamespace(status='installed', requirements_locks=locks,
                              source_commits={recipe.sources[0].name: head}, receipt_id='receipt', models_root=None)
    calls, logs = [], []
    state = SimpleNamespace(claim='claim-1', birth={'pid': 123, 'start_ticks': '456', 'uid': 1000},
                            nodes=[], models=[], fail_doctor=False, recipe=recipe, helper_sha='a' * 64)
    monkeypatch.setattr(warm_attestation, 'supervisor_incarnation', lambda *a: state.birth)
    (root / 'runtime/ComfyUI/models').mkdir(parents=True, exist_ok=True)
    (root / recipe.environments[0].relative_path / 'lib/python3.12/site-packages').mkdir(parents=True)
    plan = SimpleNamespace(steps=[SimpleNamespace(kind=k) for k in ('source', 'environment')])

    class Runner:
        def __init__(self, *args, **kwargs):
            self.plan, self.recipe = plan, state.recipe
            self.helper_sha256 = state.helper_sha
            self.context = SimpleNamespace(service_client=None)
            self.commits, self.locks = {}, {}
            self.health = SimpleNamespace(object_info_sha256='d' * 64, required_nodes_present=True)
        def helper(self, op, **kwargs):
            calls.append(op)
            return {'helper_sha256': self.helper_sha256}
        def execute(self, step, **kwargs):
            calls.append(step.kind)
            self.commits.update(receipt.source_commits)
            self.locks.update(receipt.requirements_locks)
        def run(self, argv, **kwargs):
            calls.append('git-inventory')
            result = subprocess.run(argv, capture_output=True, text=True, check=kwargs.get('check', True))
            return SimpleNamespace(stdout=result.stdout, stdout_truncated=False, exit_code=result.returncode)
        def check_health(self):
            calls.append('health')
            return 'skipped', {}
        def launch_spec(self):
            return {'argv': []}

    monkeypatch.setattr(engine_runner, '_parse_receipt', lambda *a: receipt)
    monkeypatch.setattr(engine_runner, 'plan_install', lambda *a, **kw: plan)
    monkeypatch.setattr(engine_runner, 'plan_from_receipt', lambda *a, **kw: plan)
    monkeypatch.setattr(engine_runner, 'require_current_receipt', lambda *a: None)
    monkeypatch.setattr(engine_runner, '_Runner', Runner)
    monkeypatch.setattr(engine_runner, 'refresh_installed_launch', lambda *a, **kw: 'c' * 64)
    read_trust = settings.model_trust_unchanged
    monkeypatch.setattr(settings, 'model_trust_unchanged', lambda: state.trust)
    state.trust = True
    monkeypatch.setattr(managed_preparation, 'configured_models_root', lambda **kw: str(root / 'runtime/ComfyUI/models'))
    workflow = VibeWorkflow('warm', WorkflowSource('warm'))
    source = tmp_path / 'workflow.json'
    source.write_text('{}')

    def judge(*args):
        canonical = args[1] / 'canonical/workflow.py'
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text('# CPU canonical workflow')
        return {'engine': 'comfyui', 'base': None, 'models': state.models, 'extra': {'nodes': state.nodes}}, 'scratchpad'

    monkeypatch.setattr(managed_preparation, 'judge_declarations', judge)

    def run():
        prep = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
        prep.root, prep.recipe, prep.claim_id = str(root), state.recipe, state.claim
        prep.task, prep.attempt, prep.mode = 'task', 'attempt', 'align'
        prep.deadline, prep.cancelled = time.monotonic() + 30, lambda: False
        prep.device = prep.visibility = None
        prep.scratch = tmp_path / 'scratch'
        prep.context = SimpleNamespace(read_json=lambda *a, **kw: raw)
        prep.adapter = SimpleNamespace(local_install_lock=lambda *a, **kw: nullcontext())
        prep._start_engine = lambda runner: ('skipped', {})
        prep._keep_step_output = lambda step, result: logs.append((step, result.stdout))
        prep._guard_runtime = lambda *a, **kw: calls.append('runtime')
        def verify_alignment(*args):
            calls.append('alignment')
            return ManagedEnginePreparation._verify_alignment(prep, *args)
        prep._verify_alignment = verify_alignment
        def doctor():
            prep._verify_alignment(*prep._alignment_verification)
            calls.append('doctor')
            if state.fail_doctor:
                raise IntegrityError('doctor failed')
        commands = SimpleNamespace(nodes_restore=lambda rows: calls.append('nodes'),
            prepare_deps_sync=lambda: calls.append('prepare'), doctor=doctor)
        engine = mod.ComfyUIEngine(commands)
        original_align = engine.align
        def align(*args, **kwargs):
            calls.append('engine-align')
            return original_align(*args, **kwargs)
        engine.align = align
        monkeypatch.setattr(entries, 'engine_table', lambda: {'comfyui': engine})
        try:
            return prep._prepare(source, execution_identity='execution', inputs={},
                                 canonical_workflow=workflow, input_digests={})
        finally:
            if getattr(prep, 'lock', None):
                prep.lock.__exit__(None, None, None)

    return SimpleNamespace(run=run, state=state, calls=calls, logs=logs, root=root, checkout=checkout,
        lockfile=lockfile, attestation=state_dir / 'warm-attestation.json', recipe_digest=recipe_digest,
        runner=Runner, raw=raw, plan=plan, read_trust=read_trust)


def test_warm_attestation_second_job_skips_expensive_steps(warm_case):
    c = warm_case
    c.run()
    c.calls.clear()
    c.run()
    assert not set(c.calls) & {'source', 'environment', 'runtime', 'nodes', 'prepare', 'doctor', 'alignment', 'git-inventory'}
    log = json.loads([body for step, body in c.logs if step == 'warm_attestation'][-1])
    assert log['warm_attestation']['used'] is True
    assert 'doctor' in log['warm_attestation']['skipped']


def test_local_managed_warm_second_job_skips_audits(warm_case):
    c = warm_case
    c.state.claim = None
    c.run()
    c.calls.clear()
    c.run()
    assert not set(c.calls) & {'source', 'environment', 'runtime', 'nodes', 'prepare', 'doctor',
                               'alignment', 'engine-align', 'git-inventory'}
    saved = json.loads(c.attestation.read_text())
    assert 'claim_id' not in saved['identity']
    assert saved['identity']['supervisor'] == c.state.birth
    log = json.loads([body for step, body in c.logs if step == 'warm_attestation'][-1])
    assert log['warm_attestation']['used'] is True


def test_local_managed_supervisor_restart_reverifies_fully(warm_case):
    c = warm_case
    c.state.claim = None
    c.run()
    assert c.attestation.exists()
    c.state.birth = {**c.state.birth, 'start_ticks': '999'}
    c.calls.clear()
    c.run()
    assert {'source', 'environment', 'runtime', 'nodes', 'prepare', 'doctor', 'alignment'} <= set(c.calls)
    assert json.loads(c.attestation.read_text())['identity']['supervisor'] == c.state.birth


def test_local_managed_requirement_set_survives_t2i_edit_t2i(warm_case):
    warm_case.state.claim = None
    test_warm_attestation_requirement_set_survives_t2i_edit_t2i(warm_case)


@pytest.mark.parametrize('damage', ['absent', 'corrupt', 'schema', 'false', 'invalid-settings', 'unreadable-settings'])
def test_local_managed_attestation_fails_closed(warm_case, monkeypatch, damage):
    from astrid.core.generation import settings
    c = warm_case
    c.state.claim = None
    c.run()
    assert c.attestation.exists()
    if damage == 'absent':
        c.attestation.unlink()
    elif damage == 'corrupt':
        c.attestation.write_text('{')
    elif damage == 'schema':
        saved = json.loads(c.attestation.read_text())
        saved['schema'] = 'wrong-schema'
        c.attestation.write_text(json.dumps(saved))
    elif damage == 'false':
        c.state.trust = False
    else:
        monkeypatch.setattr(settings, 'model_trust_unchanged', c.read_trust)
        def read(*args):
            raise OSError('unreadable settings') if damage == 'unreadable-settings' else ValueError('invalid settings')
        monkeypatch.setattr(settings, 'read', read)
    c.calls.clear()
    c.run()
    assert {'source', 'environment', 'runtime', 'nodes', 'prepare', 'doctor', 'alignment'} <= set(c.calls)


@pytest.mark.parametrize('first,second', [('claim-1', 'claim-2'), ('claim-1', None), (None, 'claim-1')])
def test_warm_attestation_claim_presence_and_identity_must_match(warm_case, first, second):
    c = warm_case
    c.state.claim = first
    c.run()
    assert c.attestation.exists()
    c.state.claim = second
    c.calls.clear()
    c.run()
    assert {'source', 'environment', 'runtime', 'nodes', 'prepare', 'doctor', 'alignment'} <= set(c.calls)


@pytest.mark.parametrize('change', ['node-lock', 'node-pin', 'source-pin'])
def test_warm_attestation_binds_engine_and_node_pins(warm_case, change):
    from dataclasses import replace
    c = warm_case
    c.run()
    saved = json.loads(c.attestation.read_text())
    environment = c.state.recipe.environments[0]
    assert saved['identity']['node_locks'] == {environment.name: environment.node_lock.sha256}
    if change == 'node-lock':
        (c.checkout / 'custom_nodes.lock').write_text('changed node pins\n')
    elif change == 'node-pin':
        c.state.recipe = replace(c.state.recipe, environments=(replace(environment,
            node_lock=replace(environment.node_lock, sha256='f' * 64)),))
    else:
        source = c.state.recipe.sources[0]
        c.state.recipe = replace(c.state.recipe, sources=(replace(source,
            pin=replace(source.pin, revision='f' * 40)),))
    c.calls.clear()
    c.run()
    assert {'source', 'environment', 'runtime', 'nodes', 'prepare', 'doctor', 'alignment'} <= set(c.calls)


def test_warm_attestation_retains_storage_and_model_verification(warm_case, monkeypatch):
    c = warm_case
    c.run()
    c.calls.clear()
    original = ManagedEnginePreparation._verify_models
    def verify(self, manifest):
        c.calls.append('models')
        return original(self, manifest)
    monkeypatch.setattr(ManagedEnginePreparation, '_verify_models', verify)
    monkeypatch.setattr(ManagedEnginePreparation, '_preflight_model_storage',
                        lambda *args: c.calls.append('storage'))
    c.run()
    assert {'storage', 'models'} <= set(c.calls)
    assert not {'source', 'environment', 'doctor', 'alignment'} & set(c.calls)


def test_warm_attestation_requirement_set_survives_t2i_edit_t2i(warm_case):
    c = warm_case
    c.run()
    c.calls.clear()
    c.state.nodes = [{'repo': 'https://example.invalid/edit.git', 'commit': 'e' * 40}]
    c.run()
    assert not set(c.calls) & {'source', 'environment', 'runtime'}
    assert {'nodes', 'prepare', 'doctor', 'alignment'} <= set(c.calls)
    c.state.nodes = []
    c.calls.clear()
    c.run()
    assert not set(c.calls) & {'source', 'environment', 'runtime', 'nodes', 'prepare', 'doctor', 'alignment'}
    assert len(json.loads(c.attestation.read_text())['requirements']) == 2


@pytest.mark.parametrize('change', ['recipe', 'head', 'dirty', 'lock', 'claim', 'supervisor',
    'corrupt', 'absent', 'schema', 'types', 'false', 'dependency', 'receipt', 'helper'])
def test_warm_attestation_drift_fails_closed(warm_case, change):
    from dataclasses import replace
    import subprocess
    c = warm_case
    c.run()
    assert c.attestation.exists()
    if change == 'recipe':
        c.state.recipe = replace(c.state.recipe, start=replace(c.state.recipe.start, port=8199))
    elif change == 'head':
        subprocess.run(['git', '-C', str(c.checkout), 'commit', '--allow-empty', '-m', 'changed'],
                       check=True, capture_output=True)
    elif change == 'dirty':
        (c.checkout / 'tracked.py').write_text('dirty = True\n')
    elif change == 'lock':
        c.lockfile.write_text('changed lock')
    elif change == 'claim':
        c.state.claim = 'claim-2'
    elif change == 'supervisor':
        c.state.birth = {**c.state.birth, 'start_ticks': '789'}
    elif change == 'corrupt':
        c.attestation.write_text('{')
    elif change == 'absent':
        c.attestation.unlink()
    elif change in {'schema', 'types'}:
        saved = json.loads(c.attestation.read_text())
        saved['schema' if change == 'schema' else 'requirements'] = 1
        c.attestation.write_text(json.dumps(saved))
    elif change == 'false':
        c.state.trust = False
    elif change == 'dependency':
        site = next(c.root.glob('**/site-packages'))
        (site / 'injected.dist-info').mkdir()
    elif change == 'receipt':
        path = c.root / '.astrid-engine/install.json'
        raw = json.loads(path.read_text())
        raw['receipt_id'] = 'reinstalled'
        path.write_text(json.dumps(raw))
    elif change == 'helper':
        c.state.helper_sha = 'f' * 64
    c.calls.clear()
    c.run()
    assert {'source', 'environment', 'runtime', 'nodes', 'prepare', 'doctor', 'alignment'} <= set(c.calls)
    log = json.loads([body for step, body in c.logs if step == 'warm_attestation'][-1])
    assert log['warm_attestation'] == {'used': False, 'skipped': []}


def test_warm_attestation_failing_doctor_writes_nothing(warm_case):
    c = warm_case
    c.state.fail_doctor = True
    with pytest.raises(IntegrityError, match='doctor failed'):
        c.run()
    assert not c.attestation.exists()


def test_warm_attestation_new_requirement_failure_does_not_extend(warm_case):
    c = warm_case
    c.run()
    before = c.attestation.read_bytes()
    c.state.nodes = [{'repo': 'https://example.invalid/edit.git', 'commit': 'e' * 40}]
    c.state.fail_doctor = True
    with pytest.raises(IntegrityError, match='doctor failed'):
        c.run()
    assert c.attestation.read_bytes() == before


def test_warm_attestation_requirement_digest_ignores_graph_parameters():
    from astrid.core.generation.warm_attestation import requirement_digest
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    workflow = VibeWorkflow('warm', WorkflowSource('warm'))
    workflow.add_node('KSampler', _id='1', uid='sampler', seed=1)
    manifest = {'models': [], 'extra': {'nodes': []}}
    before = requirement_digest(workflow, manifest)
    other = VibeWorkflow('warm', WorkflowSource('warm'))
    other.add_node('KSampler', _id='1', uid='sampler', seed=999)
    assert requirement_digest(other, manifest) == before
    assert requirement_digest(other, {'models': [], 'extra': {'nodes': [
        {'repo': 'https://example.invalid/edit.git', 'commit': 'e' * 40}]}}) != before


@pytest.mark.parametrize('change', ['class', 'custom-nodes', 'custom-refs', 'pip'])
def test_warm_attestation_requirement_digest_binds_canonical_requirements(change):
    from astrid.core.generation.warm_attestation import requirement_digest
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    workflow = VibeWorkflow('warm', WorkflowSource('warm'))
    manifest = {'models': [], 'extra': {'nodes': []}}
    before = requirement_digest(workflow, manifest)
    if change == 'class':
        workflow.add_node('NewRequiredClass', _id='1', uid='new')
    elif change == 'custom-nodes':
        workflow.requirements.custom_nodes = ['new-pack']
    elif change == 'custom-refs':
        workflow.requirements.custom_node_refs = [{'slug': 'new-pack', 'source': 'git',
            'url': 'https://example.invalid/new.git', 'commit': 'f' * 40}]
    else:
        workflow.requirements.runtime = {'packages': {'new-package': '==1.0'}}
    assert requirement_digest(workflow, manifest) != before


@pytest.mark.parametrize('change', ['outputs', 'tracked-output'])
def test_warm_attestation_ignores_generated_outputs_but_watches_tracked_members(warm_case, change):
    import subprocess
    c = warm_case
    (c.checkout / 'output').mkdir()
    (c.checkout / 'output/placeholder').write_text('tracked')
    subprocess.run(['git', '-C', str(c.checkout), 'add', 'output/placeholder'], check=True)
    (c.checkout / '.gitignore').write_text('output/\n')
    subprocess.run(['git', '-C', str(c.checkout), 'add', '.gitignore'], check=True)
    subprocess.run(['git', '-C', str(c.checkout), 'commit', '-m', 'generated output rule'],
                   check=True, capture_output=True)
    c.run()
    assert c.attestation.exists()
    if change == 'outputs':
        (c.checkout / 'output/new.png').write_bytes(b'new result')
    else:
        (c.checkout / 'output/placeholder').write_text('changed tracked member')
    c.calls.clear()
    c.run()
    assert ('source' in c.calls) is (change == 'tracked-output')


@pytest.mark.parametrize('change', ['none', 'claim', 'supervisor', 'corrupt', 'false'])
def test_warm_attestation_collection_receipt_requires_same_live_claim(warm_case, monkeypatch, change):
    from contextlib import nullcontext
    from astrid.core.generation import engine_execution, engine_recipe, warm_attestation
    c = warm_case
    c.run()
    monkeypatch.setattr(engine_recipe, 'load_recipe', lambda *args: c.state.recipe)
    context = SimpleNamespace(read_json=lambda *a, **kw: c.raw)
    adapter = SimpleNamespace(local_install_lock=lambda *a, **kw: nullcontext())
    monkeypatch.setattr(engine_execution, 'create_local_backend', lambda **kwargs: (context, adapter))
    if change == 'claim':
        c.state.claim = 'other-claim'
    elif change == 'supervisor':
        c.state.birth = {**c.state.birth, 'pid': 999}
    elif change == 'corrupt':
        c.attestation.write_text('{')
    result = warm_attestation.collection_receipt(root=str(c.root), engine='comfyui', platform=c.state.recipe.platform,
        claim_id=c.state.claim, deadline=time.monotonic() + 20, trust_unchanged=change != 'false')
    assert result == (c.raw if change == 'none' else None)


@pytest.mark.parametrize('error', [OSError('unreadable'), ValueError('parse'), TypeError('type')])
def test_warm_attestation_observation_errors_fall_back(warm_case, monkeypatch, error):
    from astrid.core.generation import warm_attestation
    c = warm_case
    c.run()
    def fail(*args):
        raise error
    monkeypatch.setattr(warm_attestation, 'supervisor_incarnation', fail)
    c.calls.clear()
    c.run()
    assert {'source', 'environment', 'runtime', 'doctor'} <= set(c.calls)


def test_warm_attestation_tracks_model_rows_and_metadata(warm_case):
    c = warm_case
    model = c.root / 'runtime/ComfyUI/models/weights.bin'
    model.write_bytes(b'good')
    c.state.models = [{'name': 'weights.bin', 'subdir': '', 'url': None, 'size': 4,
                       'sha256': hashlib.sha256(b'good').hexdigest()}]
    c.run()
    saved = json.loads(c.attestation.read_text())
    assert saved['model_rows'] == c.state.models
    c.calls.clear()
    c.run()
    assert 'doctor' not in c.calls
    model.write_bytes(b'evil')
    c.calls.clear()
    with pytest.raises(IntegrityError, match='model bytes differ'):
        c.run()
    assert {'source', 'environment', 'alignment'} <= set(c.calls)


def test_warm_attestation_receipt_health_refresh_preserves_claim(warm_case):
    c = warm_case
    c.raw['health'] = {'checked_at': 'first'}
    receipt = c.root / '.astrid-engine/install.json'
    receipt.write_text(json.dumps(c.raw))
    c.run()
    c.raw['health'] = {'checked_at': 'second'}
    receipt.write_text(json.dumps(c.raw))
    c.calls.clear()
    c.run()
    assert 'doctor' not in c.calls


@pytest.mark.parametrize('field,value', [('size', True), ('sha256', 123), ('url', []), ('name', None)])
def test_warm_attestation_refuses_wrong_model_row_types(warm_case, field, value):
    from astrid.core.generation.warm_attestation import WarmAttestation
    c = warm_case
    c.run()
    saved = json.loads(c.attestation.read_text())
    row = {'name': 'weights.bin', 'subdir': '', 'url': None, 'size': 4, 'sha256': 'a' * 64}
    row[field] = value
    saved['model_rows'] = [row]
    with pytest.raises(ValueError, match='model declaration'):
        WarmAttestation._validate(saved)


def test_warm_attestation_observes_live_supervisor_peer(tmp_path):
    import os
    import socket
    from astrid.core.generation.engine_custody import _vibe_birth
    from astrid.core.generation.warm_attestation import supervisor_incarnation
    control = tmp_path / 'session'
    control.mkdir(mode=0o700)
    identity = {'engine': 'cpu-test'}
    channel, peer = socket.socketpair()
    observed = []
    def connect(fd, deadline, *, start):
        observed.append(start)
        return channel
    client = SimpleNamespace(control=str(control), identity=identity,
        _marker=lambda fd: {'identity': identity, 'process': {'pid': os.getpid()}}, _connect=connect)
    try:
        assert supervisor_incarnation(client, time.monotonic() + 10) == {
            'pid': os.getpid(), 'uid': os.getuid(), 'start_ticks': _vibe_birth(os.getpid())}
        assert observed == [False]
    finally:
        peer.close()
        channel.close()


@pytest.mark.parametrize('field,value', [('engine_port', 9191), ('object_info_sha256', 'f' * 64),
    ('model_rows', [{'name': 'later-host.bin', 'subdir': '', 'url': None, 'sha256': 'f' * 64, 'size': 4}])])
def test_warm_attestation_host_route_fields_are_not_reuse_predicates(warm_case, field, value):
    c = warm_case
    c.run()
    saved = json.loads(c.attestation.read_text())
    saved[field] = value
    c.attestation.write_text(json.dumps(saved))
    c.calls.clear()
    c.run()
    assert not {'source', 'environment', 'runtime', 'doctor', 'alignment'} & set(c.calls)
