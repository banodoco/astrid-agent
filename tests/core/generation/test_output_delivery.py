"""BUG-15: exact public argv must deliver Runtime bytes to the caller.

ComfyUI wire responses are the checked-in S-45 live captures. Inference and
Runtime settlement are CPU fakes; the CLI, SDK preparation, bundle compiler,
result reconstruction, CAS reads, verification and local export are real.
"""
from __future__ import annotations

import hashlib
import io
import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from PIL import Image

from astrid import console
from astrid.core.generation import cli, preflight
from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
from astrid.sdk.results import InvocationResult

FIXTURE = Path(__file__).parents[2] / 'fixtures/comfyui/live-423cbd20'
TASK_ID = '200fed415f794e54aa01274b7eee6599'
PROMPT = 'A small red fox beside a blue ceramic teacup, clean studio light'


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    def captured(name):
        return json.loads((FIXTURE / name).read_text())
    object_info = {**captured('used-nodes.json'), **captured('qwen-delivery-nodes.json')}
    history = captured('history.json')
    prompt_id, historical = next(iter(history.items()))
    dims = historical['outputs']['13']['images'][0]['metadata']
    image = io.BytesIO()
    Image.new('RGB', (dims['width'], dims['height']), (128, 32, 16)).save(image, format='PNG')
    state = SimpleNamespace(image=image.getvalue(), calls=[], objects={}, reads=[],
                            closed=False, admissions=0, rows=[], corrupt=None,
                            invocations=[], prompt_requests=[], retained_paths={}, import_paths={})

    class Response(io.BytesIO):
        status = 200

    def opener(request, timeout):
        path = urlsplit(request.full_url).path
        state.calls.append(path)
        if path == '/prompt':
            state.prompt_requests.append((request.get_method(), json.loads(request.data)))
        if path == '/view':
            return Response(state.image)
        payload = {'/object_info': object_info,
                   '/system_stats': captured('system-stats.json'),
                   '/history/' + prompt_id: history,
                   '/prompt': {'prompt_id': prompt_id, 'node_errors': {}}}[path]
        return Response(json.dumps(payload).encode())

    comfy = ComfyTargetClient(ComfyTarget('http://127.0.0.1:8188', 'managed'), opener=opener)
    monkeypatch.setattr(preflight, 'select_generation_target', lambda *args, **kwargs: comfy)
    # The CPU installation has no running ComfyUI schema registry. Supply the
    # captured server schema at its authoring-provider seam; validation stays on.
    import vibecomfy.schema

    from astrid.core.generation.backends.comfy_schema import TargetSchemaProvider
    schema = TargetSchemaProvider.from_object_info(object_info, server_url=comfy.http_origin)
    monkeypatch.setattr(vibecomfy.schema, 'get_authoring_schema_provider', lambda **kwargs: schema)
    import vibecomfy.schema.provider
    monkeypatch.setattr(vibecomfy.schema.provider, 'get_authoring_schema_provider', lambda **kwargs: schema)

    def store(data):
        digest = 'sha256:' + sha(data)
        state.objects[digest] = data
        return digest

    def import_file(*, project, path):
        assert project == 'gpu-shakedown'
        data = path.read_bytes()
        digest = store(data)
        retained = tmp_path / 'runtime' / sha(data) / path.name
        retained.parent.mkdir(parents=True, exist_ok=True)
        retained.write_bytes(data)
        state.retained_paths[digest] = retained
        state.import_paths[digest] = path
        return SimpleNamespace(ok=True, data={'object_id': digest, 'filename': path.name})

    def read_bytes(digest):
        assert not state.closed, 'delivery must finish before the Runtime connection closes'
        state.reads.append(digest)
        if state.corrupt == digest:
            return b'corrupted transport bytes'
        return state.objects[digest]

    def invoke_result(capability, **kwargs):
        from vibecomfy.security.provenance import Provenance
        from vibecomfy.workflow_bundle import load_bundle

        assert capability == 'vibecomfy.run'
        state.invocations.append((capability, deepcopy(kwargs['inputs'])))
        state.admissions += 1
        assert kwargs['project'] == 'gpu-shakedown'
        source = kwargs['inputs']['python']
        path = tmp_path / 'runtime' / source['sha256'] / source['filename']
        companion = kwargs['inputs']['companion']
        path.with_suffix('.vibe.json').write_bytes(state.objects[companion['digest']])
        workflow = load_bundle(path, trust=Provenance.USER_CONFIRMED, schema_provider=schema).workflow
        api = workflow.compile('api')
        nodes = {node['class_type']: node['inputs'] for node in api.values()}
        assert nodes['EmptyLatentImage']['width'] == 512
        assert nodes['EmptyLatentImage']['height'] == 512
        assert nodes['RandomNoise']['noise_seed'] == 19
        assert nodes['TextEncodeQwenImage21']['prompt'] == PROMPT
        comfy.submit_prompt({'prompt': api})
        output = comfy.history(prompt_id)[prompt_id]['outputs']['13']['images'][0]
        downloaded = tmp_path / 'fake-worker.png'
        comfy.download_output(output, downloaded)
        digest = store(downloaded.read_bytes())
        producer = {'producer_output_id': 'vibecomfy:image:0', 'output_port': 'image',
                    'ordinal': 0, 'path': 'outputs/Qwen_image_2.1.png',
                    'sha256': digest.removeprefix('sha256:'), 'bytes': len(state.image),
                    'media_type': 'image/png'}
        manifest_ref = store(json.dumps({'kind': 'managed-generation-result.v1',
                                         'schema_version': 1, 'outputs': [producer]}).encode())
        row = {'association_id': 'managed-output-image', 'project_id': 'gpu-shakedown',
               'run_id': 'run-1', 'task_id': TASK_ID, 'attempt_id': 'attempt-1',
               'generation_id': 'generation-fixture', 'output_port': 'vibecomfy_run',
               'selector': {'group_key': 'main', 'variant_key': 'original'},
               'ordinal': 0, 'role': 'result', 'filename': 'Qwen_image_2.1.png',
               'media_type': 'image/png', 'size': len(state.image), 'digest': digest,
               'object_id': digest, 'durability': 'durable', 'manifest_ref': manifest_ref}
        thumbnail = {**row, 'association_id': 'thumbnail', 'role': 'thumbnail'}
        state.rows = [row, thumbnail]
        state.manifest_ref = manifest_ref
        if state.corrupt == 'image':
            state.corrupt = digest
        elif state.corrupt == 'manifest':
            state.corrupt = manifest_ref
        # Worker spool is gone before the SDK sees the settled associations.
        downloaded.unlink()
        state.task = {'task_id': TASK_ID, 'state': 'COMPLETED',
                      'result': {'manifest_ref': manifest_ref}}
        return InvocationResult(capability, 'executor', 'command', True,
            kernel_task_id=TASK_ID, raw_result={'task': state.task, 'managed_outputs': state.rows})

    from astrid.sdk.remote import RemoteMedia
    transport = SimpleNamespace(get_object=lambda digest: {'data': read_bytes(digest)},
        list_project_objects=lambda project, **kwargs: ([
            {'object_id': digest, 'digest': digest, 'size': len(data),
             'filename': 'manifest.json' if data.startswith(b'{') else 'Qwen_image_2.1.png',
             'media_type': 'application/json' if data.startswith(b'{') else 'image/png'}
            for digest, data in state.objects.items()], None))
    media = RemoteMedia(transport)
    media.import_file = import_file
    state.client = SimpleNamespace(invoke_result=invoke_result, media=media)

    @contextmanager
    def connection():
        try:
            yield state.client
        finally:
            state.closed = True

    monkeypatch.setattr(cli, '_open_client', connection)
    from astrid.core.pack.discovery import pack_discovery_scope
    with pack_discovery_scope():
        yield state


def argv(out=None, *, target='managed', negative_prompt=None, entry='qwen-image-2.1-turbo'):
    args = ['generate', 'image']
    if entry is not None:
        args += ['--entry', entry]
    args += ['--mode', 't2i',
            '--target', target, '--width', '512', '--height', '512', '--seed', '19',
            '--project', 'gpu-shakedown', '--consent', 'user_env', '--json', PROMPT]
    if negative_prompt is not None:
        args += ['--negative-prompt', negative_prompt]
    if out is not None:
        args += ['--out', str(out)]
    return args


@pytest.mark.parametrize('target', ['managed', 'auto', 'http://127.0.0.1:8188'])
@pytest.mark.parametrize('destination', ['file', 'directory', 'default'])
def test_exact_cli_delivers_verified_caller_local_image(runtime, tmp_path, monkeypatch, capsys,
                                                       target, destination):
    monkeypatch.chdir(tmp_path)
    out = {'file': tmp_path / 'out/R4-512.png', 'directory': tmp_path / 'export',
           'default': None}[destination]
    assert console.main(argv(out, target=target)) == 0
    payload = json.loads(capsys.readouterr().out)
    expected = (out if destination == 'file' else
                (out or tmp_path / 'outputs' / TASK_ID) / 'Qwen_image_2.1.png')
    assert payload['image_paths'] == [str(expected)]
    assert payload['run_dir'] == str(expected.parent)
    assert expected.is_file()
    assert sha(expected.read_bytes()) == runtime.rows[0]['digest'].removeprefix('sha256:')
    with Image.open(expected) as image:
        assert image.size == (512, 512)
    assert runtime.task['state'] == 'COMPLETED'
    assert runtime.rows[0]['generation_id']
    assert runtime.reads == [runtime.manifest_ref, runtime.rows[0]['object_id']]
    assert runtime.admissions == 1 and runtime.closed
    assert '/view' in runtime.calls


@pytest.mark.parametrize('entry', ['qwen-image-2.1-turbo', None],
                         ids=['explicit-entry', 'no-entry-builtin-default'])
def test_qwen_cli_records_selected_id_and_submitted_inputs(runtime, tmp_path, capsys, entry):
    from vibecomfy.registry.ready import repo_ready_template_discovery, resolve_ready_template

    out = tmp_path / 'qwen.png'
    assert console.main(argv(out, entry=entry, negative_prompt='D0 no lettering')) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(runtime.invocations) == 1
    capability, submitted = runtime.invocations[0]
    assert capability == 'vibecomfy.run'
    assert submitted['generation_intent']['modality'] == 'image'
    assert submitted['generation_intent']['metadata'] == {
        'model': 'qwen-image-2.1-turbo', 'mode': 't2i'}
    assert submitted['comfy_url'] == 'http://127.0.0.1:8188'
    assert json.loads(submitted['workflow_inputs']) == {
        'prompt': PROMPT, 'negative_prompt': 'D0 no lettering',
        'seed': 19, 'width': 512, 'height': 512}
    assert 'managed_assets' not in submitted

    # Observe the HTTP request body, after production selection and preparation.
    assert len(runtime.prompt_requests) == 1
    method, body = runtime.prompt_requests[0]
    assert method == 'POST'
    api = body['prompt']
    assert api['5']['class_type'] == 'TextEncodeQwenImage21'
    assert api['5']['inputs']['prompt'] == PROMPT
    assert api['5']['inputs']['negative_prompt'] == 'D0 no lettering'
    assert api['8']['class_type'] == 'RandomNoise'
    assert api['8']['inputs']['noise_seed'] == 19
    assert api['4']['class_type'] == 'EmptyLatentImage'
    assert (api['4']['inputs']['width'], api['4']['inputs']['height']) == (512, 512)
    assert any(node['class_type'] == 'SaveImage' for node in api.values())

    # All three descriptors retain exact CAS bytes after producer scratch cleanup.
    for member in ('source', 'python', 'companion'):
        descriptor = submitted[member]
        retained = runtime.retained_paths[descriptor['digest']]
        data = retained.read_bytes()
        assert retained.name == descriptor['filename']
        assert descriptor['digest'] == 'sha256:' + sha(data)
        assert descriptor['sha256'] == sha(data)
        assert descriptor['bytes'] == len(data)
        assert runtime.objects[descriptor['digest']] == data
        assert not runtime.import_paths[descriptor['digest']].exists()
    original = resolve_ready_template('image/qwen_image_2_1_turbo',
                                      repo_ready_template_discovery()).path
    source = submitted['source']
    assert source['filename'] == original.name
    assert runtime.retained_paths[source['digest']].read_bytes() == original.read_bytes()

    assert payload['image_paths'] == [str(out)]
    assert payload['run_dir'] == str(out.parent)
    assert sha(out.read_bytes()) == runtime.rows[0]['digest'].removeprefix('sha256:')
    with Image.open(out) as image:
        assert image.size == (512, 512)
    assert runtime.task['state'] == 'COMPLETED'
    assert runtime.rows[0]['generation_id']
    assert runtime.reads == [runtime.manifest_ref, runtime.rows[0]['object_id']]
    assert runtime.admissions == 1 and runtime.closed
    assert '/view' in runtime.calls


@pytest.mark.parametrize('corrupt', ['image', 'manifest'])
def test_corruption_fails_cli_without_replacing_requested_file(runtime, tmp_path, capsys, corrupt):
    runtime.corrupt = corrupt
    out = tmp_path / 'R4-512.png'
    out.write_bytes(b'previous caller file')
    assert console.main(argv(out)) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload['issues'][0]['code'] == 'output_delivery_failed'
    assert payload['task_id'] == TASK_ID
    assert 'identity' in payload['issues'][0]['message']
    assert out.read_bytes() == b'previous caller file'
    assert runtime.closed


def test_no_wait_returns_admission_without_local_delivery(runtime, tmp_path, capsys):
    out = tmp_path / 'R4-512.png'
    assert console.main([*argv(out), '--no-wait']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['kernel_task_id'] == TASK_ID
    assert not runtime.reads and not out.exists()


def settled_media(*, filenames=('one.png', 'two.png')):
    """A settled Runtime whose public media facade and integrity checks are real."""
    from astrid.sdk.remote import RemoteMedia

    objects, metadata, outputs, rows = {}, [], [], []
    for index, filename in enumerate(filenames):
        data = f'output {index}'.encode()
        digest = 'sha256:' + sha(data)
        objects[digest] = data
        row = {'object_id': digest, 'digest': digest, 'size': len(data),
               'filename': filename, 'media_type': 'image/png', 'role': 'result',
               'project_id': 'project-1'}
        rows.append(row)
        metadata.append(row)
        outputs.append({'sha256': sha(data), 'bytes': len(data), 'path': filename})
    manifest = json.dumps({'outputs': outputs}).encode()
    reference = 'sha256:' + sha(manifest)
    objects[reference] = manifest
    metadata.append({'object_id': reference, 'size': len(manifest),
                     'filename': 'manifest.json', 'media_type': 'application/json'})
    transport = SimpleNamespace(get_object=lambda digest: {'data': objects[digest]},
        list_project_objects=lambda project, **kwargs: (metadata, None))
    payload = {'task_id': 'task-1', 'manifest_ref': reference,
               'manifest': {'outputs': rows}, 'image_paths': [], 'run_dir': None}
    return SimpleNamespace(payload=payload, rows=rows, objects=objects,
                           client=SimpleNamespace(media=RemoteMedia(transport)))


@pytest.mark.parametrize('file_output', [True, False])
def test_each_output_is_delivered_with_collision_safe_names(tmp_path, file_output):
    from astrid.core.generation.delivery import deliver_generation

    runtime = settled_media(filenames=('../../same.png', '../../same.png'))
    out = tmp_path / ('result.png' if file_output else 'export')
    payload = deliver_generation(runtime.payload, client=runtime.client, out=out)
    paths = [Path(path) for path in payload['image_paths']]
    assert paths == ([out, tmp_path / 'result-2.png'] if file_output else
                     [out / 'same.png', out / '2-same.png'])
    assert [path.read_bytes() for path in paths] == [b'output 0', b'output 1']
    assert not list(Path(payload['run_dir']).glob('.astrid-delivery-*'))


@pytest.mark.parametrize('failure', ['corrupt-second', 'missing-second', 'unsealed-output', 'wrong-size'])
def test_partial_delivery_never_reports_success_or_replaces_prior_file(tmp_path, failure):
    from astrid.core.generation.delivery import deliver_generation

    runtime = settled_media()
    second = runtime.rows[1]['object_id']
    if failure == 'corrupt-second':
        runtime.objects[second] = b'corrupted'
    elif failure == 'missing-second':
        del runtime.objects[second]
    elif failure == 'unsealed-output':
        runtime.rows[1]['digest'] = 'sha256:' + 'a' * 64
    else:
        runtime.rows[1]['size'] += 1
    out = tmp_path / 'result.png'
    out.write_bytes(b'previous caller file')
    with pytest.raises(ValueError, match='local output delivery failed') as caught:
        deliver_generation(runtime.payload, client=runtime.client, out=out)
    assert caught.value.code == 'output_delivery_failed'
    assert caught.value.task_id == 'task-1'
    assert out.read_bytes() == b'previous caller file'
    assert not (tmp_path / 'result-2.png').exists()
    assert not list(tmp_path.glob('.astrid-delivery-*'))


def test_manifest_only_completion_resolves_cas_without_worker_paths(tmp_path):
    from astrid.core.generation.delivery import deliver_generation

    runtime = settled_media(filenames=('one.png',))
    runtime.payload['manifest']['outputs'] = []
    payload = deliver_generation(runtime.payload, client=runtime.client,
                                 out=tmp_path / 'result.png', project='project-1')
    assert Path(payload['image_paths'][0]).read_bytes() == b'output 0'


def test_legacy_provider_uses_the_shared_local_export_path(tmp_path):
    from astrid.core.generation.backends.base import GenerationResult
    from astrid.core.generation.delivery import deliver_generation

    source = tmp_path / 'provider.png'
    source.write_bytes(b'provider output')
    out = tmp_path / 'export/image.png'
    result = GenerationResult(image_paths=[source])
    payload = deliver_generation(result, client=None, out=out)
    assert payload['image_paths'] == [str(out)]
    assert out.read_bytes() == source.read_bytes()


def test_failed_generation_is_preserved_for_the_cli_failure_handler(tmp_path):
    from astrid.core.generation.delivery import deliver_generation

    result = SimpleNamespace(ok=False)
    assert deliver_generation(result, client=None, out=tmp_path / 'image.png') is result
