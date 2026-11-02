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


def test_linux_target_model_locks_project_the_reviewed_foley_asset_lock():
    import vibecomfy
    from astrid.core.generation import engine_recipe
    lock_path = Path(vibecomfy.__file__).resolve().parents[1] / (
        'ready_templates/sources/custom_nodes/hunyuan_foley/bobrandomnumber/assets.json')
    assets = json.loads(lock_path.read_text())['assets']
    assert len(assets) == 10
    for engine in ('comfyui', 'vibecomfy-h3'):
        models = engine_recipe.load_recipe_model_lock(engine, 'linux-x86_64-cu130').models
        projected = {model.destination: model for model in models if model.destination.startswith('hunyuan_foley/')}
        assert len(projected) == len(assets)
        for asset in assets:
            model = projected[asset['subdir'] + '/' + asset['name']]
            assert (model.repository, model.revision, model.sha256, model.bytes) == (
                asset['repository'], asset['hf_revision'], asset['sha256'], asset['size_bytes'])
            assert model.path == asset['url'].split('/resolve/' + model.revision + '/')[1]


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
