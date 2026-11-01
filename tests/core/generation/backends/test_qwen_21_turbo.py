"""CPU-only catalog and target compilation for the Qwen 2.1 Turbo routes."""
from __future__ import annotations

import copy
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
from astrid.core.generation.backends.vibecomfy import VibeComfyBackend
from astrid.core.model_catalog.registry import ModelRegistry
from vibecomfy.porting.object_info import get_class
from vibecomfy.registry.ready import workflow_from_ready
from vibecomfy.workflow_bundle import load_bundle

REVISION = 'aacf31a6d48e707f352ecba714cee0680da3479b'
ASSETS = {
    'diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors': (7256787829, '8144a0182964aa8403ad6dbc011db837304167d235161b9aabaa8532e967314c'),
    'text_encoders/qwen3vl_8b_int8_convrot.safetensors': (9350798360, '8bfd0f6e12abf2d2d697ecc888e5e90b0d6741d6708f05799f53afa560452e8f'),
    'vae/qwen_image_2.1_vae_bf16.safetensors': (675509688, 'bb21f7473051e1ac368515dd3f2e15cd44d7a11748ee8823e1ddca3e4876b7c9'),
}
ROUTES = [('qwen-image-2.1-turbo', 't2i', 'image/qwen_image_2_1_turbo'), ('qwen-image-2.1-turbo-edit', 'edit', 'edit/qwen_image_2_1_turbo_edit')]


def object_info(workflow):
    info = {}
    for node in workflow.runtime_nodes().values():
        entry = get_class(node.class_type)
        if entry is None:
            continue
        info[node.class_type] = {
            'input': copy.deepcopy(entry['inputs']), 'input_order': copy.deepcopy(entry['input_order']),
            'output': [item['type'] for item in entry['outputs']],
            'output_name': [item['name'] for item in entry['outputs']],
            'output_is_list': [item['is_list'] for item in entry['outputs']],
        }
    def schema(required, optional, outputs, names):
        return {'input': {'required': required, 'optional': optional},
                'input_order': {'required': list(required), 'optional': list(optional)},
                'output': outputs, 'output_name': names, 'output_is_list': [False] * len(outputs)}
    info['TextEncodeQwenImage21'] = schema(
        {'clip': ['CLIP'], 'prompt': ['STRING'], 'negative_prompt': ['STRING'], 'resolution': ['INT']},
        {'vae': ['VAE'], 'images.image_1': ['IMAGE']},
        ['CONDITIONING', 'CONDITIONING', 'LATENT'], ['positive', 'negative', 'latent'])
    info['QwenImage21Cache'] = schema({'model': ['MODEL'], 'device': [['auto', 'gpu', 'cpu', 'off']], 'dtype': [['default', 'int8', 'int4']]}, {}, ['MODEL'], ['MODEL'])
    for cls, field, names in (
        ('UNETLoader', 'unet_name', [Path(next(iter(ASSETS))).name]),
        ('CLIPLoader', 'clip_name', ['qwen3vl_8b_int8_convrot.safetensors']),
        ('VAELoader', 'vae_name', ['qwen_image_2.1_vae_bf16.safetensors']),
    ):
        info[cls]['input']['required'][field] = [names]
    return info


@pytest.mark.parametrize('model,mode,template', ROUTES)
def test_catalog_route_compiles_against_fake_comfy(model, mode, template, monkeypatch):
    entry = ModelRegistry.load_default().get(model)
    spec = entry.modes[mode].backends['local']
    assert spec.template == template
    workflow = workflow_from_ready(template)
    from astrid.core.generation.entries import _ready_entries_for
    projected = _ready_entries_for(template)
    assert len(projected) == 1
    assert (projected[0]['registry_id'], projected[0]['modes']) == (model, [mode])
    info = object_info(workflow)
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            payload = json.dumps({'system': {'comfyui_version': 'fake'}} if self.path == '/system_stats' else info).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(payload)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        client = ComfyTargetClient(ComfyTarget(f'http://127.0.0.1:{server.server_port}', 'attached'))
        params = {'prompt': 'A blue lighthouse', 'seed': 42}
        if mode == 'edit': params['image_ref'] = 'input.png'
        else: params['size'] = '512x384'
        prepared, plan, schema = VibeComfyBackend(target_client=client).prepare_catalog_workflow(entry, mode, params, resolve_target=True)
        projection = load_bundle(prepared).compile(schema_provider=schema).to_dict()['api_projection']
        assert set(plan['roles'].values()) == {Path(path).name for path in ASSETS}
        by_class = {node['class_type']: node['inputs'] for node in projection.values()}
        assert by_class['TextEncodeQwenImage21']['prompt'] == params['prompt']
        assert by_class['RandomNoise']['noise_seed'] == 42
        assert by_class['ManualSigmas']['sigmas'] == '1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0'
        if mode == 'edit':
            assert by_class['LoadImage']['image'] == 'input.png'
            image_edge = by_class['TextEncodeQwenImage21']['images.image_1']
            assert projection[image_edge[0]]['class_type'] == 'LoadImage'
            assert by_class['TextEncodeQwenImage21']['resolution'] == 0
        else: assert (by_class['EmptyLatentImage']['width'], by_class['EmptyLatentImage']['height']) == (512, 384)
        assert requests == ['/object_info']
        from astrid.core.generation.backends.comfy_target import ComfyModelMissing, resolve_comfy_models
        choices = info['UNETLoader']['input']['required']['unet_name']
        info['UNETLoader']['input']['required']['unet_name'] = [['qwen_image_2.1_turbo_copy.safetensors']]
        with pytest.raises(ComfyModelMissing):
            resolve_comfy_models(client, ModelRegistry.load_default(), model, mode)
        info['UNETLoader']['input']['required']['unet_name'] = choices
        from astrid.core.generation import entries
        from astrid.sdk.generation import GenerationFacade
        monkeypatch.setattr(entries, 'user_entries', lambda: [])
        monkeypatch.setattr(entries, 'ready_entries', lambda: [])
        selected = GenerationFacade()._generate_on_target(
            registry=ModelRegistry.load_default(), model=None, mode=mode, modality='image',
            target=client.target.url, execution='local', inputs=params, dry_run=True,
        )
        assert selected['entry'] == model
        assert selected['approval']['status'] == 'approved'
        assert requests == ['/object_info', '/object_info', '/system_stats', '/object_info']
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_public_lock_matches_workflow_assets():
    lock = json.loads((Path(__file__).parents[4] / 'astrid/packs/vibecomfy/models.lock.json').read_text())
    assert len(lock['models']) == len(ASSETS)
    for row in lock['models']:
        size, sha = ASSETS[row['path']]
        assert (row['repository'], row['revision'], row['bytes'], row['sha256']) == ('Comfy-Org/Qwen-Image-2.1', REVISION, size, sha)
        assert row['destination'] == row['path'] and row['url'] is None
    for _, _, template in ROUTES:
        workflow = workflow_from_ready(template)
        assets = workflow.metadata['model_assets']
        assert {f"{a['subdir']}/{a['name']}" for a in assets} == set(ASSETS)
        for asset in assets:
            size, sha = ASSETS[f"{asset['subdir']}/{asset['name']}"]
            assert (asset['hf_revision'], asset['size_bytes'], asset['sha256']) == (REVISION, size, sha)


def test_shipped_windows_recipe_accepts_updated_lock():
    from astrid.core.generation.engine_recipe import load_recipe_model_lock
    lock = load_recipe_model_lock('vibecomfy', 'windows-x86_64-cu130')
    assert {model.path for model in lock.models} == set(ASSETS)


@pytest.mark.parametrize('model,mode,template', ROUTES)
def test_shipped_graph_assets_match_indexed_recipe_lock(model, mode, template):
    """Follow the shipped graph through the verified recipe to its model lock."""
    from astrid.core.generation.engine_recipe import load_recipe, load_recipe_model_lock

    recipe = load_recipe('vibecomfy', 'windows-x86_64-cu130')
    lock = load_recipe_model_lock(recipe.engine, recipe.platform)
    rows = {row.destination: row for row in lock.models}
    workflow = workflow_from_ready(template)
    assets = {f"{a['subdir']}/{a['name']}": a for a in workflow.metadata['model_assets']}
    assert set(rows) == set(assets)
    loader_fields = {'UNETLoader': ('diffusion_models', 'unet_name'),
                     'CLIPLoader': ('text_encoders', 'clip_name'),
                     'VAELoader': ('vae', 'vae_name')}
    references = {f"{loader_fields[n.class_type][0]}/{n.inputs[loader_fields[n.class_type][1]]}"
                  for n in workflow.runtime_nodes().values() if n.class_type in loader_fields}
    assert references == set(rows)
    for path, asset in assets.items():
        row = rows[path]
        assert (row.path, row.revision, row.bytes, row.sha256) == (
            path, asset['hf_revision'], asset['size_bytes'], asset['sha256'])
        assert asset['url'] == f'https://huggingface.co/{row.repository}/resolve/{row.revision}/{row.path}'


def test_generate_pin_and_h3_pin_are_independent():
    from astrid.core.generation.engine_recipe import load_recipe

    generate = load_recipe('vibecomfy', 'windows-x86_64-cu130')
    h3 = load_recipe('vibecomfy-h3', 'linux-x86_64-cu130')
    generate_sources = {s.name: (s.pin.repository, s.pin.revision) for s in generate.sources}
    h3_sources = {s.name: (s.pin.repository, s.pin.revision) for s in h3.sources}
    assert generate_sources['comfyui'][1] == '423cbd20511becb1f16122ab518e92e58bfa03d9'  # v0.43.2
    assert h3_sources['comfyui'][1] == 'ee71d5c4993f29086b27fde1629a945ae48425bf'
    assert {k: v for k, v in generate_sources.items() if k != 'comfyui'} == {
        k: v for k, v in h3_sources.items() if k != 'comfyui'}

    root = Path(__file__).parents[4] / 'astrid/packs/vibecomfy'
    env = json.loads((root / 'environment.windows-nvidia.json').read_text())
    assert env['pins']['sha256'] == hashlib.sha256((root / 'pins.json').read_bytes()).hexdigest()
    requirements = (root / 'requirements.windows-nvidia.txt').read_text().splitlines()
    constraints = generate.environments[0].requirements.constraints
    for requirement in ('comfy-kitchen==0.2.40', 'comfy-aimdo==0.6.0'):
        assert requirement in requirements and requirement in constraints
    assert env['exclude_newer'] == generate.environments[0].requirements.exclude_newer == '2026-10-31T00:00:00Z'
