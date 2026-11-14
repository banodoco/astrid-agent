"""A generation result's ``seed_used`` is the seed Astrid posted to ComfyUI.

Each case runs the public CLI on the attached path (``--placement <url>``) and on
the managed path (``--placement managed``). The Runtime settlement and the
inference are CPU fakes; the CLI, the SDK preparation and seed binding, the
bundle the executor receives, result reconstruction and local delivery are real.
The fake executor queues the received bundle and bindings on a fake ComfyUI, so
the seed each test compares against is the one in the posted ``/prompt``.
"""
from __future__ import annotations

import hashlib
import io
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from PIL import Image

from astrid import console
from astrid.core.generation import cli, preflight
from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
from astrid.sdk.results import InvocationResult
from tests.core.generation.test_user_workflow_attached import OBJECT_INFO_STOCK, _custom_inputs, _serve

FIXTURE = Path(__file__).parents[2] / 'fixtures/comfyui/live-423cbd20'
TASK_ID = '5eed05ed5eed05ed5eed05ed5eed05ed'
PROJECT = 'seed-receipt'
PROMPT = 'A small red fox beside a blue ceramic teacup, clean studio light'


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _png() -> bytes:
    image = io.BytesIO()
    Image.new('RGB', (8, 8), (128, 32, 16)).save(image, format='PNG')
    return image.getvalue()


def _use_schema(monkeypatch, object_info, comfy):
    # The CPU installation has no running ComfyUI schema registry; supply the
    # server schema at its authoring-provider seam, as test_output_delivery does.
    import vibecomfy.schema
    import vibecomfy.schema.provider

    from astrid.core.generation.backends.comfy_schema import TargetSchemaProvider
    schema = TargetSchemaProvider.from_object_info(object_info, server_url=comfy.http_origin)
    monkeypatch.setattr(vibecomfy.schema, 'get_authoring_schema_provider', lambda **kwargs: schema)
    monkeypatch.setattr(vibecomfy.schema.provider, 'get_authoring_schema_provider', lambda **kwargs: schema)
    monkeypatch.setattr(preflight, 'select_generation_target', lambda *args, **kwargs: comfy)
    return schema


def _runtime(monkeypatch, tmp_path, queue):
    """A Runtime that runs ``queue(workflow_path, workflow_inputs)`` and settles one image."""
    state = SimpleNamespace(objects={}, admissions=0)
    image = _png()

    def store(data):
        digest = 'sha256:' + _sha(data)
        state.objects[digest] = data
        return digest

    def import_file(*, project, path):
        assert project == PROJECT
        data = path.read_bytes()
        retained = tmp_path / 'runtime' / _sha(data) / path.name
        retained.parent.mkdir(parents=True, exist_ok=True)
        retained.write_bytes(data)
        return SimpleNamespace(ok=True, data={'object_id': store(data), 'filename': path.name})

    def invoke_result(capability, **kwargs):
        assert capability == 'vibecomfy.run'
        state.admissions += 1
        source = kwargs['inputs']['python']
        path = tmp_path / 'runtime' / source['sha256'] / source['filename']
        path.with_suffix('.vibe.json').write_bytes(state.objects[kwargs['inputs']['companion']['digest']])
        queue(path, json.loads(kwargs['inputs'].get('workflow_inputs') or '{}'))
        digest = store(image)
        producer = {'producer_output_id': 'vibecomfy:image:0', 'output_port': 'image', 'ordinal': 0,
                    'path': 'outputs/seeded.png', 'sha256': digest.removeprefix('sha256:'),
                    'bytes': len(image), 'media_type': 'image/png'}
        manifest_ref = store(json.dumps({'kind': 'managed-generation-result.v1',
                                         'schema_version': 1, 'outputs': [producer]}).encode())
        row = {'association_id': 'managed-output-image', 'project_id': PROJECT, 'run_id': 'run-1',
               'task_id': TASK_ID, 'attempt_id': 'attempt-1', 'generation_id': 'generation-seed',
               'output_port': 'vibecomfy_run', 'selector': {'group_key': 'main', 'variant_key': 'original'},
               'ordinal': 0, 'role': 'result', 'filename': 'seeded.png', 'media_type': 'image/png',
               'size': len(image), 'digest': digest, 'object_id': digest, 'durability': 'durable',
               'manifest_ref': manifest_ref}
        task = {'task_id': TASK_ID, 'state': 'COMPLETED', 'result': {'manifest_ref': manifest_ref}}
        return InvocationResult(capability, 'executor', 'command', True,
                                kernel_task_id=TASK_ID, raw_result={'task': task, 'managed_outputs': [row]})

    from astrid.sdk.remote import RemoteMedia
    transport = SimpleNamespace(get_object=lambda digest: {'data': state.objects[digest]},
        list_project_objects=lambda project, **kwargs: ([
            {'object_id': digest, 'digest': digest, 'size': len(data),
             'filename': 'manifest.json' if data.startswith(b'{') else 'seeded.png',
             'media_type': 'application/json' if data.startswith(b'{') else 'image/png'}
            for digest, data in state.objects.items()], None))
    media = RemoteMedia(transport)
    media.import_file = import_file
    client = SimpleNamespace(invoke_result=invoke_result, media=media)

    @contextmanager
    def connection():
        yield client

    monkeypatch.setattr(cli, '_open_client', connection)
    return state


def _result(capsys, argv) -> dict:
    from astrid.core.pack.discovery import pack_discovery_scope
    with pack_discovery_scope():
        code = console.main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    return payload


# --- a person's saved UI workflow (seed 7, control fixed) ---------------------------------------

@pytest.fixture
def saved(tmp_path):
    fake, path = _serve(tmp_path, 'fixed')
    try:
        yield fake, path
    finally:
        fake.stop()


def _saved_run(placement, saved, tmp_path, monkeypatch, capsys, inputs=None):
    fake, path = saved
    kind = 'managed' if placement == 'managed' else 'attached'
    comfy = ComfyTargetClient(ComfyTarget(fake.url, kind))
    _use_schema(monkeypatch, OBJECT_INFO_STOCK, comfy)

    def queue(workflow_path, bindings):
        from astrid.packs.vibecomfy import production_engine
        # The executor's own run: it binds what it was sent and posts /prompt.
        # The fake has no /history, so the run ends after the POST.
        try:
            production_engine.run_workflow_path(workflow_path, tmp_path / 'engine', task_identity=TASK_ID,
                                                comfy_url=fake.url, workflow_input_bindings=bindings)
        except Exception:  # noqa: BLE001 - only the queued prompt matters here
            pass

    _runtime(monkeypatch, tmp_path, queue)
    argv = ['generate', 'workflow', str(path), '--placement', fake.url if kind == 'attached' else 'managed',
            '--project', PROJECT, '--out', str(tmp_path / 'export'), '--consent', 'user_env', '--json']
    if inputs is not None:
        (tmp_path / 'inputs.json').write_text(json.dumps(inputs), encoding='utf-8')
        argv += ['--inputs-json', str(tmp_path / 'inputs.json')]
    payload = _result(capsys, argv)
    assert len(fake.prompts) == 1, fake.requests
    return payload, _custom_inputs(fake.prompts[0]['prompt'])['seed']


@pytest.mark.parametrize('placement', ['attached', 'managed'])
def test_saved_fixed_seed_is_the_seed_used(placement, saved, tmp_path, monkeypatch, capsys):
    payload, posted = _saved_run(placement, saved, tmp_path, monkeypatch, capsys)
    assert posted == 7
    assert payload['seed_used'] == 7


def test_explicit_seed_over_a_saved_fixed_seed_is_the_seed_used(saved, tmp_path, monkeypatch, capsys):
    # Attached only: without the attached server's schema, a managed run of this saved
    # workflow does not declare its custom node's seed as an input (the explicit seed on
    # the managed path is covered by the catalog case below).
    payload, posted = _saved_run('attached', saved, tmp_path, monkeypatch, capsys, inputs={'seed': 11})
    assert posted == 11
    assert payload['seed_used'] == 11


# --- a catalog entry: an explicit seed, and a fresh one -----------------------------------------

def _catalog_run(placement, tmp_path, monkeypatch, capsys, seed=None):
    def captured(name):
        return json.loads((FIXTURE / name).read_text())
    object_info = {**captured('used-nodes.json'), **captured('qwen-delivery-nodes.json')}
    history = captured('history.json')
    prompt_id = next(iter(history))
    posted = []

    class Response(io.BytesIO):
        status = 200

    def opener(request, timeout):
        path = urlsplit(request.full_url).path
        if path == '/prompt':
            posted.append(json.loads(request.data)['prompt'])
        payload = {'/object_info': object_info, '/system_stats': captured('system-stats.json'),
                   '/history/' + prompt_id: history,
                   '/prompt': {'prompt_id': prompt_id, 'node_errors': {}}}[path]
        return Response(json.dumps(payload).encode())

    kind = 'managed' if placement == 'managed' else 'attached'
    comfy = ComfyTargetClient(ComfyTarget('http://127.0.0.1:8188', kind), opener=opener)
    schema = _use_schema(monkeypatch, object_info, comfy)

    def queue(workflow_path, bindings):
        from vibecomfy.security.provenance import Provenance
        from vibecomfy.workflow_bundle import load_bundle
        workflow = load_bundle(workflow_path, trust=Provenance.USER_CONFIRMED, schema_provider=schema).workflow
        for name, value in bindings.items():
            workflow.set_input(name, value)
        comfy.submit_prompt({'prompt': workflow.compile('api')})

    _runtime(monkeypatch, tmp_path, queue)
    payload = _result(capsys, [
        'generate', 'image', '--entry', 'qwen-image-2.1-turbo', '--mode', 't2i', '--placement', placement,
        '--width', '512', '--height', '512', *(['--seed', str(seed)] if seed is not None else []),
        '--project', PROJECT, '--out', str(tmp_path / 'export'), '--consent', 'user_env', '--json', PROMPT])
    assert len(posted) == 1
    noise = [node['inputs']['noise_seed'] for node in posted[0].values() if node['class_type'] == 'RandomNoise']
    assert len(noise) == 1, noise
    return payload, noise[0]


@pytest.mark.parametrize('placement', ['http://127.0.0.1:8188', 'managed'])
def test_explicit_seed_is_the_seed_used(placement, tmp_path, monkeypatch, capsys):
    payload, posted = _catalog_run(placement, tmp_path, monkeypatch, capsys, seed=11)
    assert posted == 11
    assert payload['seed_used'] == 11


@pytest.mark.parametrize('placement', ['http://127.0.0.1:8188', 'managed'])
def test_catalog_fresh_seed_is_the_seed_used(placement, tmp_path, monkeypatch, capsys):
    payload, posted = _catalog_run(placement, tmp_path, monkeypatch, capsys)
    assert type(posted) is int and posted != 0
    assert payload['seed_used'] == posted
