"""CPU composition proof: selected target provisioning owns auxiliary custody."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation import engine_local_endpoint, entries, preflight
from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
from astrid.core.generation.engine_contracts import DownloadResult, HealthEvidence
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
    path = root / 'models/hunyuan_foley/synchformer_state_dict.pth'
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    asset = {'name': path.name, 'subdir': 'hunyuan_foley', 'size_bytes': len(content),
             'sha256': hashlib.sha256(content).hexdigest(),
             'url': 'https://fixture.invalid/synchformer_state_dict.pth'}
    receipt = SimpleNamespace(status='installed',
        health=HealthEvidence('healthy', '2026-11-02T17:06:00Z', 'a' * 64, True),
        models=(DownloadResult(path.name, str(path), 'skipped', len(content), asset['sha256'], True, 0),))
    actual_endpoint = engine_local_endpoint.local_engine_endpoint
    monkeypatch.setattr(engine_local_endpoint, 'recorded_engine_root', lambda engine: str(root))
    monkeypatch.setattr(engine_local_endpoint, 'local_engine_endpoint',
        lambda engine, **kwargs: actual_endpoint(engine, **kwargs, context=object(),
                                                 status=lambda *a, **k: receipt))
    client = preflight.select_generation_target('managed')
    assert isinstance(client, ComfyTargetClient)
    assert client.target.facts['model_root']['inventory'] == [{
        'name': path.name, 'subdir': 'hunyuan_foley', 'size': len(content),
        'sha256': 'sha256:' + asset['sha256'],
    }]
    workflow = workflow_from_ready('image/z_image_turbo')
    workflow.metadata['model_assets'].append(asset)
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
    # Engine locks are mandatory for every start, including unrelated workflows.
    for path in (engine_recipe._ROOT / 'packs').glob('*/models.lock.json'):
        assert 'hunyuan_foley' not in path.read_text(), path
    for engine in ('comfyui', 'vibecomfy-h3'):
        models = engine_recipe.load_recipe_model_lock(engine, 'linux-x86_64-cu130').models
        assert not any(model.destination.startswith('hunyuan_foley/') for model in models)


def test_target_source_and_manifest_pins_use_the_current_custody_provider():
    import vibecomfy
    import subprocess
    from astrid.core.generation import engine_recipe
    vc = Path(vibecomfy.__file__).resolve().parents[1]
    revision = subprocess.check_output(['git', '-C', str(vc), 'rev-parse', 'HEAD'], text=True).strip()
    for engine, platform in [('comfyui', 'linux-x86_64-cu130'),
                             ('vibecomfy-h3', 'linux-x86_64-cu130'),
                             ('vibecomfy', 'windows-x86_64-cu130')]:
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
             'bundle': str(bundle.python_path), 'ambiguity': None}
    original_find = entries._ready_entries_for
    monkeypatch.setattr(entries, '_ready_entries_for',
        lambda identifier=None: [entry] if identifier == entry['id'] else original_find(identifier))
    monkeypatch.setattr(entries, 'ready_entries', lambda: [entry])

    root = tmp_path / 'installed-target'
    recipe = engine_recipe.load_recipe('comfyui', 'linux-x86_64-cu130')
    plan = engine_runner.plan_install(recipe, root=str(root), placement='local')
    helper_sha = engine_runner._sha(engine_runner.helper_manifest(recipe))
    engine_models = tuple(DownloadResult(model.name, str(root / 'models' / model.destination),
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
            'helper_sha256': helper_sha, 'environment': environment.name, 'requirements_lock_sha256': digest}))
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
                    'identity', 'node-lock', 'environment-spec', 'models-config-check'}:
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
    base_path = root / 'models/base/engine.bin'
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
    def opener(request, timeout):
        data = contents[request.full_url]
        start, end = map(int, request.get_header('Range').removeprefix('bytes=').split('-'))
        downloads.append(request.full_url)
        response = io.BytesIO(data[start:end + 1])
        response.status = 206
        response.headers = {'Content-Range': f'bytes {start}-{end}/{len(data)}'}
        return response
    return SimpleNamespace(root=root, entry=entry, workflow=workflow, opener=opener,
        downloads=downloads, events=events, receipt=receipt_path, alignment=alignment)


@pytest.mark.parametrize('change', ['match', 'hash', 'size', 'missing', 'undeclared'])
def test_declared_entry_alignment_reaches_real_managed_custody_and_approval(tmp_path, monkeypatch, change):
    fixture = _provisionable_managed_entry(tmp_path, monkeypatch)
    align = fixture.alignment.align
    entry_id = fixture.entry['id']
    original_receipt = fixture.receipt.read_bytes()
    with pytest.raises(fixture.alignment.AlignmentError, match='explicit consent'):
        align(entry_id=entry_id, apply=True, opener=fixture.opener)
    assert fixture.downloads == []
    result = align(entry_id=entry_id, apply=True, consent=['user_env'], opener=fixture.opener)
    assert result['status'] == 'ready'
    assert len(fixture.downloads) == len(fixture.workflow.metadata['model_assets'])
    client = preflight.select_generation_target('managed', entry_id=entry_id)
    binding = client.target.facts['model_root']
    assert binding['path'] == str(fixture.root / 'models')
    assert len(binding['inventory']) == len(fixture.workflow.metadata['model_assets']) + 1
    auxiliary = fixture.root / 'models/hunyuan_foley/auxiliary/cpu-auxiliary.bin'
    before_sidecars = set((fixture.root / 'models').rglob('*.json'))
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
                      opener=lambda *a, **k: pytest.fail('download after corruption'))
        if change == 'missing':
            with pytest.raises(preflight.GenerationReadinessError) as caught:
                preflight.select_generation_target('managed', entry_id=entry_id)
            assert entry_id in caught.value.recovery_command
            assert 'cpu-auxiliary.bin' in str(caught.value)
        assert fixture.receipt.read_bytes() == original_receipt
        assert set((fixture.root / 'models').rglob('*.json')) == before_sidecars
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
    other_client = preflight.select_generation_target('managed')
    schema = entries.target_schema({'id': 'image/z_image_turbo'}, other, other_client)
    other_bundle = emit_bundle(other, tmp_path / 'other/workflow.py', {'operation': 'authored'})
    assert other_bundle.compile(schema_provider=schema).api_digest
    assert fixture.receipt.read_bytes() == original_receipt
