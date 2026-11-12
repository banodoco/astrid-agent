"""CPU proof for offline inspection and consent-gated managed reconciliation."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli
from astrid.core.generation import asset_alignment as alignment
from astrid.core.generation.asset_gaps import require_workflow_assets
from astrid.core.generation.engine_contracts import DownloadError, IntegrityError, ModelSpec, UnsafePathError
from astrid.core.generation.preflight import GenerationReadinessError, generation_error

DATA = b'locked CPU fake model bytes'


def _locked_model_rows(monkeypatch, rows_by_lock):
    indexed = tuple(SimpleNamespace(engine=f'engine-{i}', platform='test')
                    for i in range(len(rows_by_lock)))
    monkeypatch.setattr(alignment, 'load_recipe_index',
                        lambda: SimpleNamespace(entries=indexed))
    monkeypatch.setattr(alignment, 'load_recipe',
                        lambda engine, _platform: SimpleNamespace(
                            models_lock=f'lock-{engine.removeprefix("engine-")}'))
    monkeypatch.setattr(alignment, 'load_model_lock',
                        lambda lock: SimpleNamespace(models=rows_by_lock[lock]))


def _artifact(name='qwen-a', **changes):
    facts = {
        'name': name,
        'repository': 'Comfy-Org/Qwen-Image-2.1',
        'revision': 'a' * 40,
        'path': 'diffusion_models/qwen.safetensors',
        'url': None,
        'destination': 'diffusion_models/qwen.safetensors',
        'bytes': 123,
        'sha256': 'b' * 64,
    }
    facts.update(changes)
    return ModelSpec(**facts)


def test_packaged_model_locks_deduplicate_same_artifact_with_different_names(monkeypatch):
    first = _artifact(name='qwen_image_2.1_turbo_int8_convrot')
    alias = _artifact(name='qwen-image-edit-diffusion-models-qwen-image-2-1-turbo-int8-convrot')
    _locked_model_rows(monkeypatch, {'lock-0': (first,), 'lock-1': (alias,)})

    assert alignment._locked_models()[first.destination] == alias


@pytest.mark.parametrize('field,value', [
    ('repository', 'other/repository'),
    ('revision', 'c' * 40),
    ('path', 'other/path.safetensors'),
    ('url', 'https://models.invalid/other'),
    ('bytes', 124),
    ('sha256', 'd' * 64),
])
def test_packaged_model_locks_still_reject_different_artifact_facts(monkeypatch, field, value):
    first = _artifact()
    conflicting = _artifact(name='alias', **{field: value})
    _locked_model_rows(monkeypatch, {'lock-0': (first,), 'lock-1': (conflicting,)})

    with pytest.raises(IntegrityError, match='conflicting packaged model locks'):
        alignment._locked_models()


class Workflow:
    def compile(self, _format):
        return {'1': {'class_type': 'UNETLoader', 'inputs': {'unet_name': 'fake.safetensors'}}}


def info():
    return {'UNETLoader': {'input': {'required': {'unet_name': [['fake.safetensors']]}}}}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    model = ModelSpec(name='fake', destination='diffusion_models/fake.safetensors',
                      repository=None, revision=None, path=None,
                      bytes=len(DATA), sha256=hashlib.sha256(DATA).hexdigest(),
                      url='https://models.invalid/immutable/fake')
    target = alignment.ManagedAssetTarget(tmp_path / 'engine', tmp_path / 'engine/models',
        tmp_path / 'engine/runtime/ComfyUI/custom_nodes', sys.executable,
        'http://127.0.0.1:8188', 'a' * 64, tmp_path / 'engine/cache.json')
    manifests = [{'id': 'fake', 'provider_only': False, 'modes': [
        {'mode': 't2i', 'workflow': Workflow(), 'models': [model], 'node_packs': [], 'unlocked': []}]}]
    observed, calls = info(), []
    monkeypatch.setattr(alignment.ManagedAssetTarget, 'cached_object_info', lambda self: observed)
    monkeypatch.setattr(alignment.ManagedAssetTarget, 'object_info', lambda self: calls.append('probe') or observed)
    monkeypatch.setattr(alignment.ManagedAssetTarget, 'verify_installation', lambda self, **kw: None)
    monkeypatch.setattr(alignment.ManagedAssetTarget, 'verify_readiness', lambda self, **kw: None)
    monkeypatch.setattr(alignment.ManagedAssetTarget, 'install_lock', lambda self, **kw: nullcontext())
    monkeypatch.setattr(alignment, 'selected_target', lambda: target)
    monkeypatch.setattr(alignment, 'catalog_manifests', lambda entry=None: manifests)
    return SimpleNamespace(target=target, model=model, manifests=manifests, observed=observed, calls=calls)


class FakeResponse:
    def __init__(self, content, start, chunk, fail_at=None):
        self.status_code = 206 if start else 200
        self.headers = {'Content-Range': f'bytes {start}-{len(DATA) - 1}/{len(DATA)}'} if start else {}
        self.url = 'https://models.invalid/immutable/fake'
        self._body, self._chunk, self._fail_at, self._start = content[start:], chunk, fail_at, start

    def iter_bytes(self):
        for offset in range(0, len(self._body), self._chunk):
            if self._fail_at is not None and self._start + offset >= self._fail_at:
                raise OSError('fake interrupted connection')
            yield self._body[offset:offset + self._chunk]


class FakeClient:
    """The httpx-style client seam of VibeComfy fetch; records (start, end) per request."""

    def __init__(self, calls, content=DATA, chunk=len(DATA), fail_at=None, honor_range=True):
        self.calls, self.content, self.chunk, self.fail_at, self.honor_range = calls, content, chunk, fail_at, honor_range

    @contextlib.contextmanager
    def stream(self, method, url, headers=None, **kwargs):
        value = (headers or {}).get('Range', 'bytes=0-').removeprefix('bytes=')
        start = int(value.split('-')[0]) if self.honor_range else 0
        self.calls.append((start, len(DATA) - 1))
        yield FakeResponse(self.content, start, self.chunk, self.fail_at)


def request_bytes(calls, content=DATA):
    return FakeClient(calls, content)


def no_download(message):
    class Forbidden:
        def stream(self, *args, **kwargs):
            pytest.fail(message)
    return Forbidden()


def apply(fake, **kwargs):
    return alignment.align(entry_id='fake', apply=True, consent=['user_env'], **kwargs)


def test_inspection_has_exact_assets_bytes_and_remedy_without_side_effects(fake, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('read-only alignment attempted a network/download/install operation')
    monkeypatch.setattr(alignment, '_fetch_model', forbidden)
    monkeypatch.setattr(alignment, '_install_pack', forbidden)
    monkeypatch.setattr(alignment.ManagedAssetTarget, 'object_info', forbidden)
    monkeypatch.setattr('urllib.request.urlopen', forbidden)
    monkeypatch.setattr('socket.socket.connect', forbidden)
    before = list(fake.target.root.parent.rglob('*'))
    report = alignment.align(entry_id='fake')
    assert list(fake.target.root.parent.rglob('*')) == before
    assert report['total_missing_bytes'] == len(DATA)
    row = report['entries'][0]['modes'][0]['models'][0]
    assert (row['sha256'], row['bytes'], row['source_url'], row['target_folder']) == (
        fake.model.sha256, len(DATA), fake.model.url, str(fake.target.models_root / 'diffusion_models'))
    assert report['align_command'] == 'astrid generate align --apply --consent user_env --entry fake'
    assert report['disk_check']['status'] == 'ok' and report['status'] == 'not_ready'
    assert fake.calls == []


def test_all_deduplicates_missing_bytes_and_lists_unlocked_assets(fake):
    second = {**fake.manifests[0], 'id': 'another'}
    second['modes'] = [{**fake.manifests[0]['modes'][0], 'unlocked': [
        {'kind': 'model', 'value': 'unlocked.safetensors', 'missing': ['sha256', 'bytes']}]}]
    fake.manifests.append(second)
    report = alignment.align(all_entries=True)
    assert [entry['id'] for entry in report['entries']] == ['fake', 'another']
    assert report['total_missing_bytes'] == len(DATA)
    assert report['unlocked_bytes'] == 'unknown'
    assert report['entries'][1]['modes'][0]['unlocked'][0]['value'] == 'unlocked.safetensors'
    assert report['align_command'].endswith('--all')


@pytest.mark.parametrize('consent', [(), ['paid'], 'user_env', ['user_env_required']])
def test_apply_requires_explicit_consent_before_selecting_target(fake, monkeypatch, consent):
    monkeypatch.setattr(alignment, 'selected_target', lambda: pytest.fail('selected before consent'))
    with pytest.raises(alignment.AlignmentError, match='--apply --consent user_env'):
        alignment.align(apply=True, consent=consent)
    assert fake.calls == [] and not fake.target.root.exists()


def test_consent_without_apply_stays_read_only(fake):
    report = alignment.align(consent=['user_env'])
    assert not report['applied'] and not fake.target.root.exists()


def test_apply_reprobes_same_target_and_is_idempotent(fake):
    calls = []
    report = apply(fake, client=request_bytes(calls))
    assert report['status'] == 'ready' and report['applied']
    assert calls == [(0, len(DATA) - 1)] and fake.calls == ['probe']
    assert (fake.target.models_root / fake.model.destination).read_bytes() == DATA
    second = apply(fake, client=no_download('second download'))
    assert second['status'] == 'ready' and second['actions'][0]['action'] == 'skipped'
    assert fake.calls == ['probe', 'probe']


def test_apply_fetches_subdir_name_into_models_root_with_comfyui_path_set(fake, monkeypatch, tmp_path):
    """The explicit models root remains authoritative when a ComfyUI checkout is configured."""
    monkeypatch.setenv('COMFYUI_PATH', str(tmp_path / 'separate-comfyui'))
    monkeypatch.delenv('VIBECOMFY_MODELS_ROOT', raising=False)
    report = apply(fake, client=request_bytes([]))
    expected = fake.target.models_root / fake.model.destination
    assert report['status'] == 'ready'
    assert expected.read_bytes() == DATA
    assert not (tmp_path / 'separate-comfyui' / fake.target.models_root.name / fake.model.destination).exists()


def test_hash_mismatch_refuses_publication(fake):
    calls = []
    with pytest.raises(alignment.AlignmentError, match='fake.safetensors.*SHA-256 mismatch'):
        apply(fake, client=request_bytes(calls, b'x' * len(DATA)))
    path = fake.target.models_root / fake.model.destination
    assert not path.exists() and not path.with_suffix('.safetensors.part').exists()
    assert fake.calls == []


def test_corrupt_existing_file_is_reported_and_never_overwritten(fake):
    path = fake.target.models_root / fake.model.destination
    path.parent.mkdir(parents=True)
    path.write_bytes(b'x' * len(DATA))
    assert alignment.align()['entries'][0]['modes'][0]['models'][0]['status'] == 'mismatch'
    with pytest.raises(IntegrityError, match='differs from its lock'):
        apply(fake, client=no_download('downloaded over corrupt file'))
    assert path.read_bytes() == b'x' * len(DATA)


def test_insufficient_disk_refuses_before_download(fake, monkeypatch):
    monkeypatch.setattr('astrid.core.generation.engine_runner._disk_anchor', lambda _: ('', 0, 0, 0))
    assert alignment.align()['disk_check']['status'] == 'blocked'
    with pytest.raises(alignment.AlignmentError, match='Insufficient disk.*need.*free 0'):
        apply(fake, client=no_download('download despite full disk'))
    assert not fake.target.root.exists()


def test_interrupted_download_resumes_verified_locked_bytes(fake):
    calls = []
    with pytest.raises(alignment.AlignmentError, match='prefix retained for resume'):
        apply(fake, client=FakeClient(calls, chunk=8, fail_at=8))
    path = fake.target.models_root / fake.model.destination
    assert not path.exists()
    assert path.with_name(path.name + '.part').read_bytes() == DATA[:8]
    calls.clear()
    report = apply(fake, client=request_bytes(calls))
    assert report['status'] == 'ready' and calls[0][0] == 8
    assert report['actions'][0]['resumed_from_bytes'] == 8 and path.read_bytes() == DATA


def test_server_ignoring_range_restarts_without_claiming_resume(fake):
    path = fake.target.models_root / fake.model.destination
    part = path.with_name(path.name + '.part')
    part.parent.mkdir(parents=True)
    part.write_bytes(DATA[:8])
    report = apply(fake, client=FakeClient([], honor_range=False))
    assert report['actions'][0]['resumed_from_bytes'] == 0 and path.read_bytes() == DATA and not part.exists()


def test_poisoned_prefix_is_refused_and_not_kept_for_resume(fake):
    path = fake.target.models_root / fake.model.destination
    part = path.with_name(path.name + '.part')
    part.parent.mkdir(parents=True)
    part.write_bytes(b'y' * 8)
    with pytest.raises(alignment.AlignmentError, match='SHA-256 mismatch'):
        apply(fake, client=request_bytes([]))
    assert not path.exists() and not part.exists()


def test_symlink_and_hardlink_model_paths_are_refused(fake, tmp_path):
    fake.target.models_root.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (fake.target.models_root / 'diffusion_models').symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        alignment.align()
    (fake.target.models_root / 'diffusion_models').unlink()
    (fake.target.models_root / 'diffusion_models').mkdir()
    original = outside / 'data'
    original.write_bytes(DATA)
    os.link(original, fake.target.models_root / fake.model.destination)
    with pytest.raises(UnsafePathError, match='hard-link'):
        apply(fake)


@pytest.mark.parametrize('observed,code', [({}, 'missing_node_pack'),
    ({'UNETLoader': {'input': {'required': {'unet_name': [[]]}}}}, 'missing_model')])
def test_compile_gaps_are_classified_and_actionable(observed, code):
    with pytest.raises(GenerationReadinessError) as caught:
        require_workflow_assets(Workflow(), observed, 'qwen-image-2.1-turbo-edit')
    error = caught.value
    assert error.code == code
    assert error.recovery_command == 'astrid generate align --apply --consent user_env --entry qwen-image-2.1-turbo-edit'
    assert generation_error(error).to_envelope()['issues'][0]['code'] == code


def test_role_resolution_reports_model_or_node_instead_of_enum(fake):
    from astrid.core.generation.backends.comfy_target import _capture_model_resolution, ComfyModelMissing
    from astrid.core.model_catalog.registry import ModelRegistry
    client = SimpleNamespace(object_info=lambda: {}, target=SimpleNamespace(url=fake.target.url, kind='managed'))
    with pytest.raises(GenerationReadinessError, match='Missing ComfyUI node') as caught:
        _capture_model_resolution(client, ModelRegistry.load_default(), 'qwen-image-2.1-turbo', 't2i')
    assert caught.value.code == 'missing_node_pack'
    error = ComfyModelMissing('qwen-image-2.1-turbo', 't2i', {'diffusion': []}, [])
    assert generation_error(error).code == 'missing_model'
    assert error.as_dict()['code'] == 'missing_model'


def test_offline_without_cached_schema_is_explicitly_unknown(fake, monkeypatch):
    monkeypatch.setattr(alignment.ManagedAssetTarget, 'cached_object_info', lambda _: None)
    report = alignment.align()
    assert report['object_info'] == 'unknown' and report['status'] == 'not_ready'


def test_apply_probe_failure_never_claims_ready(fake, monkeypatch):
    def unavailable(_):
        raise OSError('offline')
    monkeypatch.setattr(alignment.ManagedAssetTarget, 'object_info', unavailable)
    report = apply(fake, client=request_bytes([]))
    assert report['status'] == 'not_ready' and report['object_info'] == 'unknown'
    assert report['probe_error']


def test_node_pack_apply_uses_locked_sha_and_is_idempotent(fake, monkeypatch):
    pack = {'name': 'fake-pack', 'repository': 'https://github.com/example/fake.git',
            'revision': 'a' * 40, 'classes': ['FakeNode']}
    fake.manifests[0]['modes'][0]['node_packs'] = [pack]
    installed = []
    monkeypatch.setattr(alignment, '_pack_state', lambda *args: 'verified' if installed else 'missing')
    def installer(target, locked):
        assert target is fake.target and locked == pack
        installed.append(locked['revision'])
    report = apply(fake, client=request_bytes([]), installer=installer)
    assert report['status'] == 'ready' and installed == ['a' * 40]
    apply(fake, client=no_download('redownload'), installer=installer)
    assert installed == ['a' * 40]


def test_existing_node_checkout_verifies_repo_sha_and_cleanliness(fake):
    pack = {'name': 'node', 'repository': 'https://github.com/example/node.git', 'revision': '', 'classes': []}
    path = fake.target.custom_nodes_root / pack['name']
    path.mkdir(parents=True)
    def git(*args):
        return subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    git('config', 'user.email', 'cpu@example.invalid')
    git('config', 'user.name', 'CPU test')
    git('remote', 'add', 'origin', pack['repository'])
    (path / 'node.py').write_text('# CPU fixture\n')
    git('add', 'node.py')
    git('commit', '-qm', 'CPU fixture')
    pack['revision'] = git('rev-parse', 'HEAD')
    assert alignment._pack_state(fake.target, pack) == 'verified'
    (path / 'node.py').write_text('# dirty\n')
    assert alignment._pack_state(fake.target, pack) == 'mismatch'


def test_cli_routes_inspection_all_and_consent(fake, capsys):
    assert cli.main(['align', '--entry', 'fake', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['total_missing_bytes'] == len(DATA)
    assert cli.main(['align', '--all', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['align_command'].endswith('--all')
    assert cli.main(['align', '--apply', '--entry', 'fake', '--json']) == 1
    assert '--consent user_env' in json.loads(capsys.readouterr().err)['error']['message']


def test_align_selector_exclusivity_and_generate_help(fake, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(['--help'])
    assert caught.value.code == 0
    assert 'align' in capsys.readouterr().out
    with pytest.raises(SystemExit) as caught:
        cli.main(['align', '--help'])
    assert caught.value.code == 0
    help_text = capsys.readouterr().out
    assert 'astrid generate align' in help_text
    assert all(flag in help_text for flag in ('--entry', '--all', '--apply', '--consent', '--json'))
    assert cli.main(['align', '--entry', 'fake', '--all', '--json']) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'blocked'
    assert fake.calls == [] and not fake.target.root.exists()


def test_operational_engine_cli_keeps_its_existing_commands():
    import argparse
    from astrid.core import engine_cli
    parser = engine_cli._parser()
    choices = next(action.choices for action in parser._actions
                   if isinstance(action, argparse._SubParsersAction))
    assert set(choices) == {'install', 'status'}


def test_real_catalog_inventory_has_exact_unlocked_models():
    manifests = alignment.catalog_manifests()
    rows = {row['id']: row for row in manifests}
    assert len(rows) == 33
    assert not rows['hunyuan-foley-local']['provider_only']
    assert rows['hunyuan-foley-local']['modes']
    for entry in ('qwen-image-2.1-turbo', 'qwen-image-2.1-turbo-edit'):
        assert len(rows[entry]['modes'][0]['models']) == 3
        assert rows[entry]['modes'][0]['unlocked'] == []
    assert {gap['value'] for gap in rows['z-image']['modes'][0]['unlocked'] if gap['kind'] == 'model'} == {
        'z_image_turbo_bf16.safetensors', 'qwen_3_4b.safetensors', 'ae.safetensors'}
    for entry in ('qwen-image-2512', 'qwen-image-edit', 'wan-2.2', 'ltx-2.3'):
        assert all(mode['unlocked'] for mode in rows[entry]['modes'])
    assert rows['gpt-image-2']['provider_only']


@pytest.mark.installed_smoke
def test_noneditable_wheel_align_entrypoint_and_module(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    installed = tmp_path / 'installed'
    result = subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', '--no-build-isolation',
                             '--target', str(installed), str(repo)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    import runtime_protocol
    environment = {**os.environ, 'PYTHONPATH': os.pathsep.join([
        str(installed), str(Path(runtime_protocol.__file__).resolve().parent.parent),
        *[p for p in os.environ.get('PYTHONPATH', '').split(os.pathsep) if p and Path(p).resolve() != repo]])}
    for argv in ([sys.executable, '-m', 'astrid', 'generate', 'align', '--help'],
                 [sys.executable, str(installed / 'bin/astrid'), 'generate', 'align', '--help']):
        checked = subprocess.run(argv, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30)
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert 'astrid generate align' in checked.stdout
        assert '--apply' in checked.stdout and '--consent' in checked.stdout
    for family in ('generate', 'engines'):
        checked = subprocess.run([sys.executable, '-m', 'astrid', family, '--help'],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30)
        assert checked.returncode == 0, checked.stdout + checked.stderr
        if family == 'generate':
            assert 'align' in checked.stdout
        else:
            assert 'align' not in checked.stdout
    refused = subprocess.run([sys.executable, '-m', 'astrid', 'generate', 'align', '--apply', '--entry', 'z-image', '--json'],
                             cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30)
    assert refused.returncode == 1
    assert '--apply --consent user_env' in json.loads(refused.stderr)['error']['message']


def test_selected_managed_receipt_is_offline_and_validates_models_lock(tmp_path, monkeypatch):
    from dataclasses import asdict
    from astrid.core.generation.engine_contracts import (
        DownloadResult, HealthEvidence, InstallReceipt, StepReceipt, effective_models_root,
    )
    from astrid.core.generation.engine_recipe import load_recipe, load_model_lock
    from astrid.core.generation.engine_runner import plan_install
    root = tmp_path / 'managed'
    recipe = load_recipe('comfyui', 'linux-x86_64-cu130')
    plan = plan_install(recipe, root=str(root), placement='local')
    models_root = Path(effective_models_root(str(root), None))
    models = tuple(DownloadResult(model.name, str(models_root / model.destination), 'skipped',
        model.bytes, model.sha256, True, 0) for model in load_model_lock(recipe.models_lock).models)
    receipt = InstallReceipt('astrid.engine-install.v1', 'cpu-installed', plan.engine, plan.platform,
        plan.placement, plan.root, 'installed', False, plan.recipe_sha256, plan.models_lock_sha256,
        {env.name: 'b' * 64 for env in recipe.environments},
        {source.name: source.pin.revision for source in recipe.sources}, models,
        tuple(StepReceipt(step.id, step.kind, 'executed', 'passed', {}) for step in plan.steps),
        HealthEvidence('healthy', '2026-11-01T12:00:01Z', 'c' * 64, True),
        '2026-11-01T12:00:00Z', '2026-11-01T12:00:01Z', None)
    path = root / '.astrid-engine/install.json'
    path.parent.mkdir(parents=True)
    raw = asdict(receipt)
    path.write_text(json.dumps(raw))
    monkeypatch.setattr('astrid.core.generation.engine_local_endpoint.recorded_engine_root', lambda engine: str(root))
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **kw: pytest.fail('offline selection probed target'))
    target = alignment.selected_target()
    assert target.root == root and target.models_root == root / 'runtime/ComfyUI/models'
    assert target.models_lock_sha256 == plan.models_lock_sha256
    assert target.url == 'http://127.0.0.1:8188'
    assert not target.models_root.exists()
    raw['models_lock_sha256'] = 'e' * 64
    path.write_text(json.dumps(raw))
    with pytest.raises(IntegrityError, match='installed but stale'):
        alignment.selected_target()


def test_schema_cache_is_bound_to_target_receipt_and_refuses_tampering(tmp_path):
    from dataclasses import replace
    target = alignment.ManagedAssetTarget(tmp_path, tmp_path / 'models', tmp_path / 'nodes',
        sys.executable, 'http://127.0.0.1:8188', 'a' * 64, tmp_path / 'schema/cache.json',
        receipt_sha256='c' * 64)
    assert target.cached_object_info() is None
    target.cache_object_info(info())
    assert target.cached_object_info() == info()
    assert replace(target, receipt_sha256='d' * 64).cached_object_info() is None
    assert replace(target, url='http://127.0.0.1:8199').cached_object_info() is None
    data = json.loads(target.schema_cache.read_text())
    data['UNETLoader']['input']['required']['unet_name'] = [['tampered']]
    target.schema_cache.write_text(json.dumps(data))
    assert target.cached_object_info() is None


def test_apply_keeps_installed_source_integrity_gate(fake, monkeypatch):
    def invalid(self, **kwargs):
        raise IntegrityError('pinned source differs')
    monkeypatch.setattr(alignment.ManagedAssetTarget, 'verify_installation', invalid)
    with pytest.raises(IntegrityError, match='pinned source differs'):
        apply(fake, client=no_download('downloaded before source verification'))
    assert not fake.target.root.exists() and fake.calls == []


def test_apply_holds_existing_engine_lock_for_all_mutations(fake, monkeypatch):
    from contextlib import contextmanager
    locked = []
    @contextmanager
    def lock(self, **kwargs):
        locked.append(True)
        try:
            yield
        finally:
            locked.pop()
    monkeypatch.setattr(alignment.ManagedAssetTarget, 'install_lock', lock)
    inner = request_bytes([])
    class Checked:
        def stream(self, *args, **kwargs):
            assert locked
            return inner.stream(*args, **kwargs)
    assert apply(fake, client=Checked())['status'] == 'ready'
    assert not locked


def test_ready_assets_do_not_bypass_managed_admission(fake, monkeypatch):
    from astrid.core.generation.engine_local_endpoint import EngineNotInstalled
    def unready(self, **kwargs):
        raise EngineNotInstalled('comfyui', 'engine_unhealthy')
    monkeypatch.setattr(alignment.ManagedAssetTarget, 'verify_readiness', unready)
    report = apply(fake, client=request_bytes([]))
    assert report['entries'][0]['status'] == 'ready'
    assert report['status'] == 'not_ready'
    assert report['managed_readiness_error'] == 'engine_not_installed'
    assert 'astrid generate align' in report['next_action']


def test_managed_dry_run_missing_file_reports_entry_align_before_compile(fake, monkeypatch):
    from dataclasses import replace
    from astrid.core.generation import preflight
    from astrid.core.generation.engine_local_endpoint import EngineNotInstalled
    target = replace(fake.target, locked_models=(fake.model,))
    monkeypatch.setattr(alignment, 'selected_target', lambda: target)
    monkeypatch.setattr('astrid.core.generation.engine_local_endpoint.recorded_engine_root', lambda engine: str(target.root))
    def unready(*args, **kwargs):
        raise EngineNotInstalled('comfyui', 'install status failed, engine not healthy')
    monkeypatch.setattr('astrid.core.generation.engine_local_endpoint.local_engine_endpoint', unready)
    with pytest.raises(GenerationReadinessError) as caught:
        preflight.select_generation_target('managed', entry_id='qwen-image-2.1-turbo-edit')
    error = caught.value
    assert error.code == 'missing_model' and fake.model.destination in str(error)
    assert error.recovery_command == 'astrid generate align --apply --consent user_env --entry qwen-image-2.1-turbo-edit'
    assert not fake.target.root.exists() and fake.calls == []


def test_malformed_cache_metadata_is_unknown_instead_of_a_crash(tmp_path):
    target = alignment.ManagedAssetTarget(tmp_path, tmp_path / 'models', tmp_path / 'nodes',
        sys.executable, 'http://127.0.0.1:8188', 'a' * 64, tmp_path / 'cache.json', receipt_sha256='b' * 64)
    from vibecomfy.schema.cache import CACHE_METADATA_KEY
    target.schema_cache.write_text(json.dumps({CACHE_METADATA_KEY: [], **info()}))
    assert target.cached_object_info() is None


def test_real_node_installer_checks_out_locked_commit_on_cpu_without_network(fake, tmp_path, monkeypatch):
    """Exercise VibeComfy's installer in target Python, redirecting only clone bytes."""
    from dataclasses import replace
    import vibecomfy
    source = tmp_path / 'node-source'
    source.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(source), *args], check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    git('config', 'user.email', 'cpu@example.invalid')
    git('config', 'user.name', 'CPU fixture')
    (source / 'node.py').write_text('# locked revision\n')
    git('add', 'node.py')
    git('commit', '-qm', 'locked revision')
    locked_sha = git('rev-parse', 'HEAD')
    (source / 'node.py').write_text('# later revision\n')
    git('commit', '-qam', 'later revision')
    assert locked_sha != git('rev-parse', 'HEAD')
    pack = {'name': 'cpu-alignment-node', 'repository': 'https://github.com/example/cpu-alignment-node.git',
            'revision': locked_sha, 'classes': ['FakeNode']}
    fake.manifests[0]['modes'][0]['node_packs'] = [pack]
    selected_source = tmp_path / 'target-pythonpath'
    selected_source.mkdir()
    (selected_source / 'vibecomfy').symlink_to(Path(vibecomfy.__file__).parent, target_is_directory=True)
    # Keep the real install_pack, its capability fence, SHA verifier, sentinel,
    # and lock writer. The tiny local Git repo substitutes only network bytes.
    (selected_source / 'sitecustomize.py').write_text(
        'import subprocess, socket\n'
        'original = subprocess.run\n'
        f'url = {pack["repository"]!r}\nlocal = {str(source)!r}\n'
        'def run(argv, *args, **kwargs):\n'
        '    if argv[:3] == ["git", "clone", url]:\n'
        '        result = original(["git", "clone", local, argv[3]], *args, **kwargs)\n'
        '        original(["git", "-C", argv[3], "remote", "set-url", "origin", url], check=True, capture_output=True)\n'
        '        return result\n'
        '    if argv[0] == "git" and "fetch" in argv:\n'
        '        raise AssertionError("network fetch in CPU fixture")\n'
        '    return original(argv, *args, **kwargs)\n'
        'subprocess.run = run\n'
        'def denied(*args, **kwargs): raise AssertionError("network in CPU fixture")\n'
        'socket.socket.connect = denied\n')
    target = replace(fake.target, vibecomfy_root=selected_source)
    monkeypatch.setattr(alignment, 'selected_target', lambda: target)
    report = apply(fake, client=request_bytes([]))
    assert report['status'] == 'ready'
    assert alignment._pack_state(target, pack) == 'verified'
    assert (target.custom_nodes_root / pack['name'] / 'node.py').read_text() == '# locked revision\n'
    from vibecomfy.node_packs import read_lockfile
    recorded = read_lockfile(target.root / '.astrid-engine/aligned-custom-nodes.lock')
    assert [(entry.name, entry.url, entry.git_commit_sha) for entry in recorded] == [
        (pack['name'], pack['repository'], locked_sha)]
    second = apply(fake, client=no_download('idempotent model download'))
    assert second['status'] == 'ready'


def test_catalog_without_model_roles_retains_original_fail_closed_gate(fake):
    from dataclasses import replace
    from astrid.core.generation.backends.comfy_target import _capture_model_resolution, ComfyModelMissing
    from astrid.core.model_catalog.registry import ModelRegistry
    original = ModelRegistry.load_default().get('z-image')
    mode = original.modes['t2i']
    backend = replace(mode.backends['local'], model_roles={})
    entry = replace(original, modes={'t2i': replace(mode, backends={'local': backend})})
    client = SimpleNamespace(object_info=info, target=SimpleNamespace(url=fake.target.url, kind='managed'))
    with pytest.raises(ComfyModelMissing) as caught:
        _capture_model_resolution(client, ModelRegistry([entry]), entry.id, 't2i')
    assert caught.value.failures == {'catalog_roles': []}
    assert caught.value.code == 'missing_model'
    assert caught.value.readiness['fixes'][0]['argv'][-2:] == ['--entry', 'z-image']
    assert not fake.target.root.exists()


def test_foley_alignment_projects_the_real_complete_declaration_without_downloads(tmp_path, monkeypatch):
    """Proven pattern: test_target_asset_declaration's canonical ready loader."""
    from vibecomfy.registry.ready import workflow_from_ready
    from vibecomfy.model_assets import _referenced_model_values
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: pytest.fail('offline declaration made a request'))
    monkeypatch.setattr('socket.socket.connect', lambda *a, **k: pytest.fail('offline declaration opened a socket'))
    entry_id = 'audio/hunyuan_video_foley'
    workflow = workflow_from_ready(entry_id)
    assets = workflow.metadata['model_assets']
    manifests = alignment.catalog_manifests(entry_id)
    models = manifests[0]['modes'][0]['models']
    assert [(model.name, model.destination, model.url, model.bytes, model.sha256) for model in models] == [
        (asset['name'], f"{asset['subdir']}/{asset['name']}", asset['url'], asset['size_bytes'], asset['sha256'])
        for asset in assets]
    picked = {row['value'] for row in _referenced_model_values(workflow)}
    assert any(model.name not in picked for model in models), 'must include non-picker auxiliary declarations'
    target = alignment.ManagedAssetTarget(tmp_path / 'engine', tmp_path / 'engine/models',
        tmp_path / 'engine/nodes', sys.executable, 'http://127.0.0.1:8188', 'a' * 64,
        tmp_path / 'engine/cache.json')
    report = alignment.align(entry_id=entry_id, target=target,
        client=no_download('declaration inspection downloaded weights'))
    assert report['entries'][0]['status'] == 'not_ready'
    assert report['total_missing_bytes'] == sum(asset['size_bytes'] for asset in assets)
    assert len(report['entries'][0]['modes'][0]['models']) == len(assets)
    assert not target.root.exists()


@pytest.mark.parametrize('damage', [
    {'name': 'windows\\model.bin'}, {'subdir': None}, {'size_bytes': True},
    {'sha256': 'g' * 64}, {'url': 'http://fixture.invalid/model.bin'},
])
def test_invalid_unselected_declarations_grant_no_inventory_authority(damage):
    from astrid.core.generation.asset_gaps import declared_model_specs
    from astrid.core.generation.engine_contracts import EngineError
    asset = {'name': 'model.bin', 'subdir': 'models', 'url': 'https://fixture.invalid/model.bin',
             'sha256': 'a' * 64, 'size_bytes': 20, **damage}
    assert declared_model_specs([asset], require_complete=False) == ()
    with pytest.raises(EngineError):
        declared_model_specs([asset])


def test_second_claim_on_a_warm_volume_fetches_nothing(fake, tmp_path, monkeypatch):
    """Plan 32 volume cache: a fresh pod (empty engine root) bound to the same saved volume fetches no rows."""
    volume_models = tmp_path / 'volume/astrid/engines/comfyui/models'
    first_engine = tmp_path / 'claim-1/engine'
    second_engine = tmp_path / 'claim-2/engine'

    def target_for(engine):
        return alignment.ManagedAssetTarget(engine, volume_models,
            engine / 'runtime/ComfyUI/custom_nodes', sys.executable,
            'http://127.0.0.1:8188', 'a' * 64, engine / 'cache.json')

    monkeypatch.setattr(alignment, 'selected_target', lambda: target_for(first_engine))
    first_calls = []
    report = apply(fake, client=request_bytes(first_calls))
    assert report['status'] == 'ready' and len(first_calls) == 1
    assert (volume_models / fake.model.destination).read_bytes() == DATA
    assert not (first_engine / 'models').exists()

    # The second claim's engine root is new and empty; only the saved volume's cache survives.
    monkeypatch.setattr(alignment, 'selected_target', lambda: target_for(second_engine))
    second_calls = []
    report = apply(fake, client=FakeClient(second_calls))
    assert second_calls == [], 'a warm volume must not re-fetch any manifest row'
    assert report['actions'][0]['action'] == 'skipped'
    assert not (second_engine / 'models').exists()
