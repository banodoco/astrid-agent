"""Compare controller preparation with the real runner of a shipped CPU job."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.sdk.generation import GenerationFacade
from astrid.core.execution.machine_run import MachineJob
from astrid.core.generation.local_job import run_job
from astrid.packs.runpod import machine
from tests.core.generation.test_runpod_engine_selection import LIVE, NoPodClient, preferences  # noqa: F401
from tests.integrations.test_dry_run_equals_live_run import Recorder, _built, _graph
from tests.integrations.test_remote_machine_job_seam_cpu import FileTransport, LeaseOwner, _owned_cpu_engine
from tests.integrations.test_runpod_slot_path_cpu import (
    H3_REQUEST, PROMPTS, SLOTS, SlotPod, _h3_built_classes, _slot_classes, _video_source,
)
from tests.integrations.test_dry_run_equals_live_run import H3_CLASSES

pytestmark = pytest.mark.integration


class CaptureClient(NoPodClient):
    def __init__(self):
        super().__init__()
        self.members = {}

    @property
    def media(self):
        def import_file(*, project, path):
            data = path.read_bytes()
            digest = 'sha256:' + hashlib.sha256(data).hexdigest()
            self.members[digest] = (path.name, data)
            return SimpleNamespace(ok=True, data={'object_id': digest, 'filename': path.name})
        return SimpleNamespace(import_file=import_file)


@pytest.mark.parametrize('cell', [*SLOTS, ('video', 'h3')], ids=lambda cell: '/'.join(cell))
def test_runpod_dry_live_shipped_t2i(cell, tmp_path, monkeypatch, preferences):
    recorder = Recorder(monkeypatch)
    client = CaptureClient()
    facade = GenerationFacade()
    modality, mode = cell
    h3 = cell == ('video', 'h3')
    extra_classes = set()
    values = dict(PROMPTS.get(cell, {}))
    if cell == ('image', 'edit'):
        from PIL import Image
        source = tmp_path / 'source.png'
        Image.new('RGB', (64, 64), (200, 30, 30)).save(source)
        values['image'] = str(source)
    if cell == ('audio', 'sfx'):
        values['video_ref'] = str(_video_source(tmp_path / 'source.mp4', '256x144'))
    if h3:
        source = tmp_path / 'source.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
                        'color=c=blue:s=1024x576:r=24', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
                        '-frames:v', '57', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest',
                        str(source)], check=True, timeout=120)
        request = json.loads(H3_REQUEST.read_text())
        assets = tmp_path / 'assets.json'
        assets.write_text(json.dumps({request['media'][0]['asset']['id']: str(source), 'source': str(source)}))
        values = dict(entry='h3', request=str(H3_REQUEST), asset_map=str(assets))
        extra_classes = set(json.loads(H3_CLASSES.read_text())) | _h3_built_classes(
            H3_REQUEST, json.loads(assets.read_text()), tmp_path)
    else:
        values['mode'] = mode
    method = {'image': facade.image, 'audio': facade.audio, 'video': facade.video}[modality]
    args = dict(placement='runpod', align='trust', client=client, project='demo', **LIVE, **values)
    dry = method(**args, dry_run=True)
    dry_graph, dry_bindings, workflow = _built(recorder.take())
    seed = dry['entry']['bindings'].get('seed')
    if not h3 and seed is not None:
        args['seed'] = seed
    method(**args, wait=False)
    live_graph, live_bindings, _ = _built(recorder.take())
    assert dry_graph == live_graph and dry_bindings == live_bindings
    assert dry['status'] == 'ready'
    assert dry['alignment'] == {'mode': 'trust', 'verified': False}
    assert len(client.calls) == 1
    capability, call = client.calls[0]
    assert capability == 'vibecomfy.run'
    inputs = dict(call['inputs'])
    for name, value in list(inputs.items()):
        if isinstance(value, dict) and value.get('digest') in client.members:
            filename, data = client.members[value['digest']]
            path = tmp_path / 'materialized' / name / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            inputs[name] = str(path)
    transport, owner = FileTransport(), LeaseOwner()
    def job_start(command, *, job_id):
        return {'id': job_id, 'dir': str(tmp_path / 'pod-agent' / job_id)}
    adapter = SimpleNamespace(placement=transport, lease_owner=owner)
    lease = machine.MachineLease(object(), SimpleNamespace(ops=SimpleNamespace(job_start=job_start)),
                                 SimpleNamespace(note=lambda *args, **kwargs: None), {}, False)
    monkeypatch.setattr(machine, 'RUNS_ROOT', str(tmp_path / 'pod' / 'runs'))
    scratch = tmp_path / 'controller'
    scratch.mkdir()
    job = MachineJob(entry=dry['entry']['id'], mode=mode, policy=json.loads(inputs['placement_policy']),
                     inputs=inputs, invocation_id='task:attempt', task_id='task', attempt_id='attempt',
                     scratch=scratch, progress_path=scratch / 'progress.json', align='trust')
    remote = machine.RunPodMachine(adapter).start(lease, job)
    job_path = Path(remote.remote_root) / 'job.json'
    assert job_path.read_bytes() == transport.uploads[str(job_path)]
    shipped = json.loads(job_path.read_bytes())
    assert shipped['bindings'] == json.loads(dry_bindings)

    from astrid.packs.vibecomfy.actions.run import run as action
    from astrid.packs.vibecomfy import local_server_engine
    observed, runtime, namespaces = [], [], []
    original_engine = action._engine_for
    def engine_for(manifest):
        engine = original_engine(manifest)
        cls = type(engine)
        if not getattr(cls, '_runpod_seam_spy', False):
            align = cls.align
            def spy(self, manifest, mode, **kwargs):
                observed.append((dict(manifest), mode, kwargs.get('workflow')))
                return align(self, manifest, mode, **kwargs)
            monkeypatch.setattr(cls, 'align', spy)
            monkeypatch.setattr(cls, '_runpod_seam_spy', True, raising=False)
        return engine
    monkeypatch.setattr(action, '_engine_for', engine_for)
    approve, bundle = local_server_engine._approve_live, local_server_engine.local_server_bundle
    def spy_approve(bundle, bindings, provider):
        runtime.append(dict(bindings))
        return approve(bundle, bindings, provider)
    def spy_bundle(*args, **kwargs):
        if kwargs.get('output_identity') is not None:
            namespaces.append(kwargs['output_identity'])
        return bundle(*args, **kwargs)
    monkeypatch.setattr(local_server_engine, '_approve_live', spy_approve)
    monkeypatch.setattr(local_server_engine, 'local_server_bundle', spy_bundle)
    provider = SlotPod(tmp_path / 'claim.json', tmp_path / 'attempts', _slot_classes(cell) | extra_classes)
    with _owned_cpu_engine(provider, tmp_path / 'pod-engine', monkeypatch):
        result = run_job(job.entry, job_path, Path(remote.remote_root) / 'out', mode=job.mode)
    assert (Path(remote.remote_root) / 'out' / result['managed_generation_result_path']).is_file()
    assert len(observed) == len(runtime) == len(namespaces) == 1
    assert observed[0][1] == 'trust'
    assert observed[0][0] == {**dry['entry']['requirements'],
                              'models_root': str(tmp_path / 'pod-engine' / 'managed' / 'engines' / 'comfyui' / 'models')}
    from astrid.packs.vibecomfy.local_server_engine import run_scoped_overrides
    scoped = run_scoped_overrides(workflow, namespaces[0])
    for name, value in runtime[0].items():
        if str(getattr(scoped.inputs[name], 'media_semantics', '') or '').lower() not in {'image', 'mask', 'audio', 'video'}:
            assert shipped['bindings'].get(name, scoped.inputs[name].value) == value
    expected = json.loads(_graph(scoped, run_inputs=runtime[0]))['api']
    assert provider.comfy.prompts[-1]['prompt'] == expected
