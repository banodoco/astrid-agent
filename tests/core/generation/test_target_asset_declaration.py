"""CPU composition proof: selected target provisioning owns auxiliary custody."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation import engine_local_endpoint, entries, preflight
from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
from astrid.core.generation.engine_contracts import (
    DownloadResult, HealthEvidence, effective_models_root,
)
from astrid.packs.vibecomfy import local_server_engine
from vibecomfy.porting.object_info import get_class
from vibecomfy.registry.ready import workflow_from_ready
from vibecomfy.workflow_bundle import WorkflowBundleError, emit_bundle


def _target_capture(workflow):
    data = {}
    for node in workflow.runtime_nodes().values():
        definition = get_class(node.class_type)
        assert definition is not None
        data[node.class_type] = {
            'input': copy.deepcopy(definition['inputs']),
            'input_order': copy.deepcopy(definition['input_order']),
            'output': [out['type'] for out in definition['outputs']],
            'output_name': [out['name'] for out in definition['outputs']],
            'output_is_list': [out['is_list'] for out in definition['outputs']],
        }
    for cls, field, name in (
        ('UNETLoader', 'unet_name', 'z_image_turbo_bf16.safetensors'),
        ('CLIPLoader', 'clip_name', 'qwen_3_4b.safetensors'),
        ('VAELoader', 'vae_name', 'ae.safetensors'),
    ):
        if cls in data:
            data[cls]['input']['required'][field] = [[name]]
    return data


def _selected_managed_target(tmp_path, monkeypatch):
    # This original fixture attests synthetic bytes through its receipt alone.
    # It has no pinned entry declarations; the provisioning fixture below
    # supplies those separately through real canonical bundles.
    monkeypatch.setattr(entries, 'ready_entries', lambda: [])
    root = tmp_path / 'installed-target'
    (root / '.astrid-engine').mkdir(parents=True)
    (root / '.astrid-engine/install.json').write_text('{}')
    content = b'CPU auxiliary fixture; no production weights'
    models_root = Path(effective_models_root(str(root), None))
    path = models_root / 'hunyuan_foley/synchformer_state_dict.pth'
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    asset = {'name': path.name, 'subdir': 'hunyuan_foley', 'size_bytes': len(content),
             'sha256': hashlib.sha256(content).hexdigest(),
             'url': 'https://fixture.invalid/synchformer_state_dict.pth'}
    workflow = workflow_from_ready('image/z_image_turbo')
    models = []
    for loader in workflow.metadata['model_assets']:
        loader_path = models_root / loader['subdir'] / loader['name']
        loader_path.parent.mkdir(parents=True, exist_ok=True)
        data = ('CPU loader fixture: ' + loader['name']).encode()
        loader_path.write_bytes(data)
        loader.update(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
        models.append(DownloadResult(loader_path.name, str(loader_path), 'skipped',
                                     len(data), loader['sha256'], True, 0))
    models.append(DownloadResult(path.name, str(path), 'skipped', len(content), asset['sha256'], True, 0))
    workflow.metadata['model_assets'].append(asset)
    workflow.requirements.models = copy.deepcopy(workflow.metadata['model_assets'])
    receipt = SimpleNamespace(status='installed',
        health=HealthEvidence('healthy', '2026-11-02T17:06:00Z', 'a' * 64, True),
        models=tuple(models))
    actual_endpoint = engine_local_endpoint.local_engine_endpoint
    monkeypatch.setattr(engine_local_endpoint, 'recorded_engine_root', lambda engine: str(root))
    monkeypatch.setattr(engine_local_endpoint, 'local_engine_endpoint',
        lambda engine, **kwargs: actual_endpoint(engine, **kwargs, context=object(),
                                                 status=lambda *a, **k: receipt))
    client = preflight.select_generation_target('managed')
    assert isinstance(client, ComfyTargetClient)
    expected = [{'name': model.model,
                 'subdir': Path(model.path).parent.relative_to(models_root).as_posix(),
                 'size': model.bytes, 'sha256': 'sha256:' + model.sha256}
                for model in models]
    assert client.target.facts['model_root']['inventory'] == sorted(
        expected, key=lambda row: (row['subdir'], row['name']))
    data = _target_capture(workflow)
    monkeypatch.setattr(client, 'object_info', lambda: copy.deepcopy(data))
    return client, workflow, path


@pytest.mark.parametrize('change', ['match', 'missing', 'hash', 'size'])
def test_selected_managed_inventory_reaches_real_bundle_approval(tmp_path, monkeypatch, change):
    client, workflow, path = _selected_managed_target(tmp_path, monkeypatch)
    if change == 'missing':
        path.unlink()
    elif change == 'hash':
        path.write_bytes(b'x' * path.stat().st_size)
    elif change == 'size':
        path.write_bytes(path.read_bytes() + b'x')
    if change != 'match':
        with pytest.raises(ValueError, match='model-root'):
            entries.target_schema({'id': 'auxiliary-contract'}, workflow, client)
        return
    provider = entries.target_schema({'id': 'auxiliary-contract'}, workflow, client)
    bundle = emit_bundle(workflow, tmp_path / 'workflow.py', {'operation': 'authored'})
    assert bundle.compile(schema_provider=provider).api_digest
    declaration = next(row for row in provider._approval_diagnostics if row['code'] == 'target_model_asset')
    assert declaration['name'] == path.name
    assert declaration['size'] == path.stat().st_size
    assert declaration['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    # The actual local_server composition still requires local bytes, using
    # the explicitly selected target root carried in the canonical workflow.
    local_provider = local_server_engine._live_preflight(
        bundle, {}, client.http_origin, tmp_path, client=client)
    assert local_provider.auxiliary_model_custody == 'local'
    assert local_server_engine._approve_live(bundle, {}, local_provider).api_digest
    path.write_bytes(b'x' * path.stat().st_size)
    with pytest.raises(WorkflowBundleError, match='verification failed'):
        local_server_engine._approve_live(bundle, {}, local_provider)


def test_attached_target_cannot_adopt_managed_inventory(tmp_path, monkeypatch):
    client, workflow, _path = _selected_managed_target(tmp_path, monkeypatch)
    attached = SimpleNamespace(target=ComfyTarget(client.target.url, 'attached', client.target.facts),
                               object_info=client.object_info, http_origin=client.http_origin)
    provider = entries.target_schema({'id': 'auxiliary-contract'}, workflow, attached)
    assert provider.auxiliary_model_custody == 'local'
    assert 'models_root' not in workflow.metadata
    bundle = emit_bundle(workflow, tmp_path / 'workflow.py', {'operation': 'authored'})
    with pytest.raises(WorkflowBundleError, match='not present locally'):
        bundle.compile(schema_provider=provider, models_root=tmp_path / 'empty-host-registry')


def test_engine_model_locks_leave_foley_assets_in_the_workflow_declaration():
    import vibecomfy
    from astrid.core.generation import engine_recipe
    lock_path = Path(vibecomfy.__file__).resolve().parents[1] / (
        'ready_templates/sources/custom_nodes/hunyuan_foley/bobrandomnumber/assets.json')
    assets = json.loads(lock_path.read_text())['assets']
    assert len(assets) == 10
    # assets.json owns the facts; the generated workflow carries that declaration.
    declared = workflow_from_ready('audio/hunyuan_video_foley').metadata['model_assets']
    fields = ('name', 'subdir', 'url', 'hf_revision', 'sha256', 'size_bytes')
    assert [{key: asset[key] for key in fields} for asset in declared] == [
        {key: asset[key] for key in fields} for asset in assets]
    # Foley is part of the H3/RunPod engine closure; keep it out of unrelated locks.
    for path in (engine_recipe._ROOT / 'packs').glob('*/models.lock.json'):
        assert ('hunyuan_foley' in path.read_text()) == (path.parent.name == 'runpod'), path
    for engine in ('comfyui',):
        models = engine_recipe.load_recipe_model_lock(engine, 'linux-x86_64-cu130').models
        foley_models = [model for model in models
                        if model.destination.startswith('hunyuan_foley/')]
        if engine == 'comfyui':
            assert len(foley_models) == len(assets) == 10
            assert {
                (model.destination, model.repository, model.revision,
                 model.bytes, model.sha256)
                for model in foley_models
            } == {
                (f"{asset['subdir']}/{asset['name']}", asset['repository'],
                 asset['hf_revision'], asset['size_bytes'], asset['sha256'])
                for asset in assets
            }
        else:
            assert not foley_models


def test_target_source_and_manifest_pins_use_the_current_custody_provider():
    import vibecomfy
    import subprocess
    from astrid.core.generation import engine_recipe
    vc = Path(vibecomfy.__file__).resolve().parents[1]
    revision = subprocess.check_output(['git', '-C', str(vc), 'rev-parse', 'HEAD'], text=True).strip()
    for engine, platform in [('comfyui', 'linux-x86_64-cu130'),
                             ('comfyui', 'linux-x86_64-cu130'),
                             ('comfyui', 'windows-x86_64-cu130')]:
        recipe = engine_recipe.load_recipe(engine, platform)
        assert next(source.pin.revision for source in recipe.sources if source.name == 'vibecomfy') == revision
        for environment in recipe.environments:
            if environment.node_lock:
                assert environment.node_lock.sha256 == hashlib.sha256((vc / environment.node_lock.path).read_bytes()).hexdigest()
    root = Path(__file__).resolve().parents[3] / 'astrid/packs'
    windows = json.loads((root / 'vibecomfy/environment.windows-nvidia.json').read_text())
    assert windows['pins']['sha256'] == hashlib.sha256((root.parent / windows['pins']['path']).read_bytes()).hexdigest()
    linux = json.loads((root / 'runpod/environment.linux-cu130.json').read_text())
    assert linux['custom_nodes_lock_sha256'] == hashlib.sha256((vc / 'custom_nodes.lock').read_bytes()).hexdigest()


def _provisionable_managed_entry(tmp_path, monkeypatch):
    """Mirror _selected_managed_target/_target_capture and 079 REPORT's emitter mechanics.

    The ready graph retains its loader declarations. Each gets fixture bytes,
    and an auxiliary declaration is appended under its authored subdirectory.
    Only declaration discovery, engine status and the process/HTTP providers
    supply fixtures; alignment, receipt parsing, downloader, custody and approval
    remain real.
    """
    import io
    from dataclasses import asdict
    from astrid.core.generation import asset_alignment as alignment, engine_recipe, engine_runner
    from astrid.core.generation.engine_contracts import CommandResult, InstallReceipt, StepReceipt
    from vibecomfy.templates import ModelAsset, _model_asset_metadata

    workflow = workflow_from_ready('image/z_image_turbo')
    assets, contents = [], {}
    for asset in [*workflow.metadata['model_assets'],
                  {'name': 'cpu-auxiliary.bin', 'subdir': 'hunyuan_foley/auxiliary'}]:
        data = ('fixture bytes for ' + asset['name']).encode()
        url = 'https://fixture.invalid/' + asset['name']
        assets.append(_model_asset_metadata(ModelAsset(url=url, subdir=asset['subdir'],
            sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))))
        contents[url] = data
    workflow.metadata['model_assets'] = assets
    workflow.requirements.models = copy.deepcopy(assets)
    bundle = emit_bundle(workflow, tmp_path / 'pinned-declaration/workflow.py', {'operation': 'authored'})
    entry = {'id': 'image/cpu-declared-assets', 'source': 'ready-template', 'modes': ['t2i'],
             'bundle': str(bundle.python_path), 'ambiguity': None,
             'model_assets': bundle.workflow.metadata['model_assets']}
    original_find = entries._ready_entries_for
    ready_entry = copy.deepcopy(original_find('image/z_image_turbo')[0])
    ready_entry['model_assets'] = copy.deepcopy(assets[:3])

    def selected_entries(identifier=None):
        if identifier == entry['id']:
            return [entry]
        if identifier == ready_entry['id']:
            return [ready_entry]
        return original_find(identifier)

    monkeypatch.setattr(entries, '_ready_entries_for', selected_entries)
    monkeypatch.setattr(entries, 'ready_entries', lambda: [entry])

    root = tmp_path / 'installed-target'
    recipe = engine_recipe.load_recipe('comfyui', 'linux-x86_64-cu130')
    plan = engine_runner.plan_install(recipe, root=str(root), placement='local')
    models_root = Path(effective_models_root(str(root), None))
    engine_models = tuple(DownloadResult(model.name, str(models_root / model.destination),
        'skipped', model.bytes, model.sha256, True, 0)
        for model in engine_recipe.load_model_lock(recipe.models_lock).models)
    receipt = InstallReceipt('astrid.engine-install.v1', 'cpu-existing-install', plan.engine, plan.platform,
        plan.placement, plan.root, 'installed', False, plan.recipe_sha256, plan.models_lock_sha256,
        {env.name: 'b' * 64 for env in recipe.environments},
        {source.name: source.pin.revision for source in recipe.sources}, engine_models,
        tuple(StepReceipt(step.id, step.kind, 'executed', 'passed', {}) for step in plan.steps),
        HealthEvidence('healthy', '2026-11-03T00:00:01Z', 'c' * 64, True),
        '2026-11-03T00:00:00Z', '2026-11-03T00:00:01Z', None)
    receipt_path = root / '.astrid-engine/install.json'
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(json.dumps(asdict(receipt)))
    environment_digests = {}
    for source in recipe.sources:
        (root / source.relative_path / '.git').mkdir(parents=True, exist_ok=True)
    for environment in recipe.environments:
        folder = root / '.astrid-engine/environments' / environment.name
        folder.mkdir(parents=True)
        lock = folder / 'requirements.lock'
        lock.write_bytes(b'fixture existing environment lock')
        digest = hashlib.sha256(lock.read_bytes()).hexdigest()
        environment_digests[environment.name] = digest
        (folder / 'receipt.json').write_text(json.dumps({'recipe_sha256': plan.recipe_sha256,
            'environment': environment.name, 'requirements_lock_sha256': digest, 'validated': True}))
        if environment.node_lock:
            import vibecomfy
            ref = environment.node_lock
            source = next(s for s in recipe.sources if s.name == ref.source)
            path = root / source.relative_path / ref.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((Path(vibecomfy.__file__).resolve().parents[1] / ref.path).read_bytes())

    events = []
    class ProcessProvider:
        def __init__(self, **kwargs):
            pass

        def run(self, request):
            argv = request.argv
            events.append(('process', argv))
            if argv[0] == 'git':
                source = next(s for s in recipe.sources if str(root / s.relative_path) == argv[2])
                output = (source.pin.repository if argv[3:] == ('remote', 'get-url', 'origin') else
                          source.pin.revision if argv[3:] == ('rev-parse', 'HEAD') else '')
            elif len(argv) >= 2 and argv[-1].startswith('{') and argv[-2] in {
                    'identity', 'node-lock', 'environment-spec'}:
                operation, payload = argv[-2], json.loads(argv[-1])
                facts = ({'helper_sha256': payload['helper_sha256']} if operation == 'identity' else
                         {'expected': {}, 'requirements_lock_sha256': environment_digests[payload['environment']]}
                         if operation == 'environment-spec' else {'valid': True})
                output = json.dumps({'schema': payload['schema'], 'operation': operation, 'facts': facts})
            else:
                # Existing environment probes/pip check cross the process edge.
                assert '-c' in argv or 'check' in argv, argv
                output = ''
            return CommandResult(0, output, '', False, 0.001, None, False, False)

        def call(self, operation, payload, **kwargs):
            pytest.fail(f'fixture unexpectedly requested engine service {operation}')

    monkeypatch.setattr('astrid.core.generation.engine_custody.LinuxEngineServiceClient', ProcessProvider)
    base_path = models_root / 'base/engine.bin'
    base_path.parent.mkdir(parents=True)
    base_path.write_bytes(b'existing engine model fixture')
    status_receipt = SimpleNamespace(status='installed', health=receipt.health, models=(
        DownloadResult(base_path.name, str(base_path), 'skipped', base_path.stat().st_size,
                       hashlib.sha256(base_path.read_bytes()).hexdigest(), True, 0),))
    actual_endpoint = engine_local_endpoint.local_engine_endpoint
    monkeypatch.setattr(engine_local_endpoint, 'recorded_engine_root', lambda engine: str(root))
    monkeypatch.setattr(engine_local_endpoint, 'local_engine_endpoint',
        lambda engine, **kwargs: actual_endpoint(engine, **kwargs, context=object(),
                                                 status=lambda *a, **k: status_receipt))
    observed = _target_capture(workflow)
    def http(self, path, **kwargs):
        events.append(('http', path))
        assert path == '/object_info', path  # /prompt must never be reached.
        return copy.deepcopy(observed)
    monkeypatch.setattr(ComfyTargetClient, 'request_json', http)
    downloads = []
    class Response:
        def __init__(self, data, start):
            self.status_code, self.url, self._body = (206 if start else 200), None, data[start:]
            self.headers = {'Content-Range': f'bytes {start}-{len(data) - 1}/{len(data)}'} if start else {}

        def iter_bytes(self):
            yield self._body

    class Client:
        """The httpx-style client seam of VibeComfy fetch."""

        @contextlib.contextmanager
        def stream(self, method, url, headers=None, **kwargs):
            data = contents[url]
            start = int((headers or {}).get('Range', 'bytes=0-').removeprefix('bytes=').split('-')[0])
            downloads.append(url)
            yield Response(data, start)
    client = Client()
    return SimpleNamespace(root=root, models_root=models_root, entry=entry, workflow=workflow, client=client,
        downloads=downloads, events=events, receipt=receipt_path, alignment=alignment)


@pytest.mark.parametrize('change', ['match', 'hash', 'size', 'missing', 'undeclared'])
def test_declared_entry_alignment_reaches_real_managed_custody_and_approval(tmp_path, monkeypatch, change):
    fixture = _provisionable_managed_entry(tmp_path, monkeypatch)
    align = fixture.alignment.align
    entry_id = fixture.entry['id']
    original_receipt = fixture.receipt.read_bytes()
    with pytest.raises(fixture.alignment.AlignmentError, match='explicit consent'):
        align(entry_id=entry_id, apply=True, client=fixture.client)
    assert fixture.downloads == []
    result = align(entry_id=entry_id, apply=True, consent=['user_env'], client=fixture.client)
    assert result['status'] == 'ready'
    assert len(fixture.downloads) == len(fixture.workflow.metadata['model_assets'])
    client = preflight.select_generation_target('managed', entry_id=entry_id)
    binding = client.target.facts['model_root']
    assert binding['path'] == str(fixture.models_root)
    assert len(binding['inventory']) == len(fixture.workflow.metadata['model_assets']) + 1
    auxiliary = fixture.models_root / 'hunyuan_foley/auxiliary/cpu-auxiliary.bin'
    before_sidecars = set(fixture.models_root.rglob('*.json'))
    if change == 'hash':
        auxiliary.write_bytes(b'x' * auxiliary.stat().st_size)
    elif change == 'size':
        auxiliary.write_bytes(auxiliary.read_bytes() + b'x')
    elif change == 'missing':
        auxiliary.unlink()
    elif change == 'undeclared':
        (auxiliary.parent / 'undeclared.bin').write_bytes(b'not declared')
    if change != 'match':
        with pytest.raises(ValueError, match='model-root'):
            entries.target_schema(fixture.entry, fixture.workflow, client)
        if change in {'hash', 'size'}:
            with pytest.raises(fixture.alignment.IntegrityError, match='differs from its lock'):
                align(entry_id=entry_id, apply=True, consent=['user_env'],
                      client=SimpleNamespace(stream=lambda *a, **k: pytest.fail('download after corruption')))
        if change == 'missing':
            with pytest.raises(preflight.GenerationReadinessError) as caught:
                preflight.select_generation_target('managed', entry_id=entry_id)
            assert entry_id in caught.value.recovery_command
            assert 'cpu-auxiliary.bin' in str(caught.value)
        assert fixture.receipt.read_bytes() == original_receipt
        assert set(fixture.models_root.rglob('*.json')) == before_sidecars
        assert not (tmp_path / 'outputs').exists()
        return
    provider = entries.target_schema(fixture.entry, fixture.workflow, client)
    bundle = emit_bundle(fixture.workflow, tmp_path / 'approved/workflow.py', {'operation': 'authored'})
    assert bundle.compile(schema_provider=provider).api_digest
    local_provider = local_server_engine._live_preflight(bundle, {}, client.http_origin, tmp_path, client=client)
    assert local_server_engine._approve_live(bundle, {}, local_provider).api_digest
    # B1 regression: the pre-existing ready entry still approves with Foley
    # fixture files present. Its inventory comes from the same composition.
    other = workflow_from_ready('image/z_image_turbo')
    other.metadata['model_assets'] = copy.deepcopy(fixture.workflow.metadata['model_assets'][:3])
    other.requirements.models = copy.deepcopy(other.metadata['model_assets'])
    other_client = preflight.select_generation_target('managed', entry_id='image/z_image_turbo')
    schema = entries.target_schema({'id': 'image/z_image_turbo'}, other, other_client)
    other_bundle = emit_bundle(other, tmp_path / 'other/workflow.py', {'operation': 'authored'})
    assert other_bundle.compile(schema_provider=schema).api_digest
    assert fixture.receipt.read_bytes() == original_receipt


def _receipt_attested_managed_workflow(tmp_path, monkeypatch):
    """Use the real ready declarations with receipt-owned CPU fixture bytes.

    Mirror _selected_managed_target's status boundary and _target_capture;
    retain the template asset identities with declarations for the fixture bytes.
    """
    monkeypatch.setenv('VIBECOMFY_ENTRIES_DIR', str(tmp_path / 'user-entries'))
    workflow = workflow_from_ready('image/z_image_turbo')
    original_assets = copy.deepcopy(workflow.metadata['model_assets'])
    assert len(original_assets) == 3
    for asset in original_assets:
        data = ('receipt fixture for ' + asset['name']).encode()
        asset['sha256'] = hashlib.sha256(data).hexdigest()
        asset['size_bytes'] = len(data)
    workflow.metadata['model_assets'] = copy.deepcopy(original_assets)
    workflow.requirements.models = copy.deepcopy(original_assets)
    original_find = entries._ready_entries_for
    ready = original_find('image/z_image_turbo')
    for entry in ready:
        entry['model_assets'] = copy.deepcopy(original_assets)
    monkeypatch.setattr(entries, 'ready_entries', lambda: ready)
    monkeypatch.setattr(entries, '_ready_entries_for',
        lambda identifier=None: ready if identifier in (None, 'image/z_image_turbo') else original_find(identifier))
    root = tmp_path / 'installed-target'
    receipt_path = root / '.astrid-engine/install.json'
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text('{}')
    models_root = Path(effective_models_root(str(root), None))
    models = []
    for asset in [*original_assets,
                  {'name': 'cpu-auxiliary.bin', 'subdir': 'hunyuan_foley/auxiliary'}]:
        path = models_root / asset['subdir'] / asset['name']
        data = ('receipt fixture for ' + asset['name']).encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if 'sha256' in asset:
            assert hashlib.sha256(path.read_bytes()).hexdigest() == asset['sha256']
            assert path.stat().st_size == asset['size_bytes']
        models.append(DownloadResult(path.name, str(path), 'skipped', len(data),
                                     hashlib.sha256(data).hexdigest(), True, 0))
    auxiliary = models[-1]
    workflow.metadata['model_assets'].append({
        'name': auxiliary.model, 'subdir': 'hunyuan_foley/auxiliary',
        'url': 'https://fixture.invalid/' + auxiliary.model,
        'sha256': auxiliary.sha256, 'size_bytes': auxiliary.bytes})
    receipt = SimpleNamespace(status='installed',
        health=HealthEvidence('healthy', '2026-11-03T04:00:00Z', 'a' * 64, True), models=tuple(models))
    actual_endpoint = engine_local_endpoint.local_engine_endpoint
    monkeypatch.setattr(engine_local_endpoint, 'recorded_engine_root', lambda engine: str(root))
    monkeypatch.setattr(engine_local_endpoint, 'local_engine_endpoint',
        lambda engine, **kwargs: actual_endpoint(engine, **kwargs, context=object(),
                                                 status=lambda *a, **k: receipt))
    observed = _target_capture(workflow)
    requests = []
    def http(self, path, **kwargs):
        requests.append(path)
        assert path == '/object_info', path
        return copy.deepcopy(observed)
    monkeypatch.setattr(ComfyTargetClient, 'request_json', http)
    return SimpleNamespace(workflow=workflow, original_assets=original_assets, root=root,
        models_root=models_root, receipt=receipt, receipt_path=receipt_path, requests=requests)


def _assert_auxiliary_verification_sidecar(fixture, before_sidecars):
    auxiliary = fixture.receipt.models[-1]
    sidecar = (fixture.models_root / '.vibecomfy/model-verification' /
               (hashlib.sha256(auxiliary.path.encode()).hexdigest() + '.json'))
    assert set(fixture.models_root.rglob('*.json')) == before_sidecars | {sidecar}
    verified = json.loads(sidecar.read_text())
    assert verified['path'] == auxiliary.path
    assert verified['expected_sha256'] == verified['actual_sha256'] == auxiliary.sha256
    assert verified['expected_size_bytes'] == auxiliary.bytes


@pytest.mark.parametrize('change', ['match', 'hash', 'size', 'missing', 'unattested', 'declaration_backed'])
def test_explicit_ready_template_selection_uses_receipt_custody(tmp_path, monkeypatch, change):
    fixture = _receipt_attested_managed_workflow(tmp_path, monkeypatch)
    if change in {'unattested', 'declaration_backed'}:
        fixture.receipt.models = fixture.receipt.models[1:]
    if change == 'unattested':
        # Neither receipt custody nor a trusted declaration attests these present bytes.
        ready = entries.ready_entries()[0]
        ready['model_assets'] = ready['model_assets'][1:]
    client = preflight.select_generation_target('managed', entry_id='image/z_image_turbo')
    inventory = client.target.facts['model_root']['inventory']
    if change == 'declaration_backed':
        # A complete pinned declaration can supply the omitted receipt row.
        assert len(inventory) == len(fixture.workflow.metadata['model_assets'])
    else:
        assert len(inventory) == len(fixture.receipt.models)
    attested_assets = fixture.workflow.metadata['model_assets']
    if change == 'unattested':
        attested_assets = attested_assets[1:]
    assert {(row['subdir'], row['name']) for row in inventory} == {
        (asset['subdir'], asset['name']) for asset in attested_assets}
    assert fixture.workflow.metadata['model_assets'][:3] == fixture.original_assets
    path = fixture.models_root / fixture.original_assets[0]['subdir'] / fixture.original_assets[0]['name']
    if change == 'hash':
        path.write_bytes(b'x' * path.stat().st_size)
    elif change == 'size':
        path.write_bytes(path.read_bytes() + b'x')
    elif change == 'missing':
        path.unlink()
    before_receipt = fixture.receipt_path.read_bytes()
    before_sidecars = set(fixture.models_root.rglob('*.json'))
    if change not in {'match', 'declaration_backed'}:
        with pytest.raises(ValueError, match='model-root'):
            entries.target_schema({'id': 'image/z_image_turbo'}, fixture.workflow, client)
        assert not (tmp_path / 'approved').exists()
    else:
        provider = entries.target_schema({'id': 'image/z_image_turbo'}, fixture.workflow, client)
        bundle = emit_bundle(fixture.workflow, tmp_path / 'approved/workflow.py', {'operation': 'authored'})
        assert bundle.compile(schema_provider=provider).api_digest
        live = local_server_engine._live_preflight(bundle, {}, client.http_origin, tmp_path, client=client)
        assert local_server_engine._approve_live(bundle, {}, live).api_digest
        _assert_auxiliary_verification_sidecar(fixture, before_sidecars)
        assert entries.target_schema({'id': 'image/z_image_turbo'}, fixture.workflow, client).auxiliary_model_custody == 'target'
    assert fixture.receipt_path.read_bytes() == before_receipt
    if change not in {'match', 'declaration_backed'}:
        assert set(fixture.models_root.rglob('*.json')) == before_sidecars
    assert set(fixture.requests) == {'/object_info'}


def test_incomplete_declaration_cannot_replace_receipt_custody(tmp_path, monkeypatch):
    fixture = _receipt_attested_managed_workflow(tmp_path, monkeypatch)
    first = fixture.original_assets[0]
    fixture.receipt.models = fixture.receipt.models[1:]
    entries.ready_entries()[0]['model_assets'][0].pop('sha256')
    before_receipt = fixture.receipt_path.read_bytes()
    before_sidecars = {path: path.read_bytes() for path in fixture.models_root.rglob('*.json')}
    client = preflight.select_generation_target('managed', entry_id='image/z_image_turbo')
    assert not any((row['subdir'], row['name']) == (first['subdir'], first['name'])
                   for row in client.target.facts['model_root']['inventory'])
    with pytest.raises(ValueError, match='model-root'):
        entries.target_schema({'id': 'image/z_image_turbo'}, fixture.workflow, client)
    assert fixture.receipt_path.read_bytes() == before_receipt
    assert {path: path.read_bytes() for path in fixture.models_root.rglob('*.json')} == before_sidecars


@pytest.mark.parametrize('field', ['sha256', 'size_bytes'])
@pytest.mark.parametrize('index', [0, -1], ids=['loader', 'auxiliary'])
def test_declared_identity_mismatch_refuses_managed_approval(tmp_path, monkeypatch, field, index):
    fixture = _receipt_attested_managed_workflow(tmp_path, monkeypatch)
    client = preflight.select_generation_target('managed', entry_id='image/z_image_turbo')
    asset = fixture.workflow.metadata['model_assets'][index]
    asset[field] = '0' * 64 if field == 'sha256' else asset[field] + 1
    fixture.workflow.requirements.models = copy.deepcopy(fixture.workflow.metadata['model_assets'])
    with pytest.raises((ValueError, WorkflowBundleError), match='mismatch'):
        provider = entries.target_schema({'id': 'image/z_image_turbo'}, fixture.workflow, client)
        bundle = emit_bundle(fixture.workflow, tmp_path / 'approved/workflow.py', {'operation': 'authored'})
        bundle.compile(schema_provider=provider)


@pytest.mark.parametrize('saved', [True, False], ids=['saved', 'unsaved'])
@pytest.mark.parametrize('change', ['match', 'hash', 'size', 'missing', 'undeclared'])
def test_user_authored_managed_selection_uses_receipt_custody(tmp_path, monkeypatch, saved, change):
    from astrid.core.model_catalog.registry import ModelRegistry
    from vibecomfy.entries import draft_entry

    fixture = _receipt_attested_managed_workflow(tmp_path, monkeypatch)
    entry_id = 'user-receipt-workflow'
    with pytest.raises(KeyError):
        ModelRegistry.load_default().get(entry_id)
    assert entries.find_entry(entry_id) is None
    if change == 'undeclared':
        path = fixture.models_root / 'user-only/auxiliary.bin'
        path.parent.mkdir()
        path.write_bytes(b'arbitrary user declaration and matching bytes')
        fixture.workflow.metadata['model_assets'].append({
            'name': path.name, 'subdir': 'user-only', 'url': 'https://fixture.invalid/' + path.name,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'size_bytes': path.stat().st_size})
        fixture.workflow.requirements.models = copy.deepcopy(fixture.workflow.metadata['model_assets'])
    bundle = emit_bundle(fixture.workflow, tmp_path / 'authored/workflow.py', {'operation': 'authored'})
    entry = draft_entry(bundle.workflow, entry_id, bundle=str(bundle.python_path))
    if saved:
        manifest = tmp_path / 'user-entries' / entry_id / 'entry.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps(entry))
        entry = entries.find_entry(entry_id)
        assert entry['source'] == 'user-entry'
        workflow = entries._load_entry_workflow(entry)
    else:
        assert entries.find_entry(entry_id) is None
        workflow = bundle.workflow
    client = preflight.select_generation_target('managed', entry_id=entry_id)
    inventory = client.target.facts['model_root']['inventory']
    assert len(inventory) == len(fixture.receipt.models)
    assert not any(row['subdir'] == 'user-only' for row in inventory)
    auxiliary = Path(fixture.receipt.models[-1].path)
    if change == 'hash':
        auxiliary.write_bytes(b'x' * auxiliary.stat().st_size)
    elif change == 'size':
        auxiliary.write_bytes(auxiliary.read_bytes() + b'x')
    elif change == 'missing':
        auxiliary.unlink()
    before_receipt = fixture.receipt_path.read_bytes()
    before_sidecars = set(fixture.models_root.rglob('*.json'))
    if change != 'match':
        with pytest.raises(ValueError, match='model-root'):
            entries.target_schema(entry, workflow, client)
        assert not (tmp_path / 'outputs').exists()
    else:
        provider = entries.target_schema(entry, workflow, client)
        approved = emit_bundle(workflow, tmp_path / 'approved/workflow.py', {'operation': 'authored'})
        assert approved.compile(schema_provider=provider).api_digest
        live = local_server_engine._live_preflight(approved, {}, client.http_origin, tmp_path, client=client)
        assert local_server_engine._approve_live(approved, {}, live).api_digest
        _assert_auxiliary_verification_sidecar(fixture, before_sidecars)
        assert entries.target_schema(entry, workflow, client).auxiliary_model_custody == 'target'
    assert fixture.receipt_path.read_bytes() == before_receipt
    if change != 'match':
        assert set(fixture.models_root.rglob('*.json')) == before_sidecars
    assert set(fixture.requests) == {'/object_info'}


# --- target custody rows (declared rows for --align trust) ---------------------------------------

def _rows_for(workflow):
    from astrid.core.generation.target_inventory import declared_rows
    rows = declared_rows(workflow)
    assert rows, 'the fixture workflow declares model assets'
    return list(rows)


def _runpod_like_target(tmp_path, monkeypatch):
    """An attached stand-in pod: ordinary object_info, no model files on this disk."""
    client, workflow, path = _selected_managed_target(tmp_path, monkeypatch)
    pod = SimpleNamespace(target=ComfyTarget(client.target.url, 'attached'),
                          object_info=client.object_info, http_origin=client.http_origin)
    workflow.metadata.pop('models_root', None)
    path.unlink()  # absent locally: only the pod could hold it
    bundle = emit_bundle(workflow, tmp_path / 'workflow.py', {'operation': 'authored'})
    return pod, workflow, bundle


def test_runpod_bound_model_absent_locally_passes_as_declared_unverified(tmp_path, monkeypatch):
    pod, workflow, bundle = _runpod_like_target(tmp_path, monkeypatch)
    provider = local_server_engine._live_preflight(
        bundle, {}, pod.http_origin, tmp_path, client=pod, target_custody=_rows_for(workflow))
    assert provider.auxiliary_model_custody == 'target'
    record = local_server_engine._approve_live(bundle, {}, provider)
    assert record.api_digest
    declaration = next(row for row in provider._approval_diagnostics if row['code'] == 'target_model_asset')
    # These rows are explicitly trusted declarations, not observed target bytes.
    # Approval remains valid without turning that trust into provisioning proof.
    assert declaration['status'] == 'declared_unverified'
    assert declaration['model_bytes_status'] == 'declared_unverified'


def test_runpod_bound_model_missing_from_inventory_fails_approval(tmp_path, monkeypatch):
    pod, workflow, bundle = _runpod_like_target(tmp_path, monkeypatch)
    provider = local_server_engine._live_preflight(
        bundle, {}, pod.http_origin, tmp_path, client=pod, target_custody=[])
    with pytest.raises(local_server_engine.ProductionEngineError) as raised:
        local_server_engine._approve_live(bundle, {}, provider)
    message = str(raised.value)
    assert pod.http_origin in message and 'does not declare auxiliary model asset' in message


def test_local_comfy_url_without_inventory_keeps_the_local_disk_check(tmp_path, monkeypatch):
    pod, workflow, bundle = _runpod_like_target(tmp_path, monkeypatch)
    provider = local_server_engine._live_preflight(bundle, {}, pod.http_origin, tmp_path, client=pod)
    assert provider.auxiliary_model_custody == 'local'
    with pytest.raises(WorkflowBundleError, match='not present locally'):
        local_server_engine._approve_live(bundle, {}, provider)


def test_trust_flag_is_the_only_host_channel_and_is_consumed_once(monkeypatch):
    """Plan 32 T5: no machine inventory reaches a child; --align trust uses the declared rows."""
    from astrid.core.generation import target_inventory
    workflow = SimpleNamespace(metadata={'model_assets': [
        {'name': 'a.bin', 'subdir': 'x', 'sha256': 'sha256:' + 'a' * 64, 'size_bytes': 3}]})
    bundle = SimpleNamespace(workflow=workflow)
    monkeypatch.delenv(target_inventory.TRUST_ENV, raising=False)
    assert local_server_engine._trust_from_environment(bundle) is None
    for other in ('align', 'check', 'inventory', ''):
        monkeypatch.setenv(target_inventory.TRUST_ENV, other)
        assert local_server_engine._trust_from_environment(bundle) is None
        assert target_inventory.TRUST_ENV not in os.environ  # consumed, never inherited by a subprocess
    monkeypatch.setenv(target_inventory.TRUST_ENV, 'trust')
    assert local_server_engine._trust_from_environment(bundle) == (
        {'name': 'a.bin', 'subdir': 'x', 'size': 3, 'sha256': 'a' * 64},)
    assert not hasattr(target_inventory, 'ENV') and not hasattr(target_inventory, 'build_document')


