"""Compare controller preparation with the real runner of a shipped CPU job."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.sdk.generation import GenerationFacade
from astrid.core.execution.machine_run import MachineJob
from astrid.core.generation.local_job import run_job
from astrid.packs.runpod import machine
from tests.core.generation.test_runpod_engine_selection import NoPodClient, live_machine  # noqa: F401
from tests.integrations.test_dry_run_equals_live_run import Recorder, _built, _graph
from tests.integrations.test_remote_machine_job_seam_cpu import FileTransport, LeaseOwner
from tests.integrations.test_managed_host_preparation import managed_install, session  # noqa: F401
from tests.integrations.runpod_slot_pod import receipt_files
from tests.integrations.test_runpod_slot_path_cpu import (
    H3_REQUEST, PROMPTS, SLOTS, SlotPod, _h3_built_classes, _slot_classes, _video_source,
)
from tests.integrations.test_dry_run_equals_live_run import H3_CLASSES

pytestmark = pytest.mark.integration


@pytest.fixture(scope='session')
def pinned_comfy_source(tmp_path_factory):
    """Use a supplied clean pin or obtain the public source once per test run."""
    pins = Path(__file__).resolve().parents[2] / 'astrid/packs/vibecomfy/pins.json'
    pin = json.loads(pins.read_text())['sources']['comfyui']['revision']
    supplied = os.environ.get('ASTRID_TEST_COMFY_SOURCE')
    if supplied:
        source = Path(supplied)
    else:
        source = tmp_path_factory.mktemp('pinned-comfy') / 'source'
        subprocess.run(['git', 'clone', '--quiet', '--no-checkout', '--filter=blob:none',
                        'https://github.com/Comfy-Org/ComfyUI.git', str(source)], check=True, timeout=180)
        subprocess.run(['git', '-C', str(source), 'checkout', '--quiet', '--detach', pin], check=True, timeout=180)
    assert subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() == pin
    assert not subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain'], text=True).strip()
    return source


@pytest.fixture
def pinned_managed_install(session, monkeypatch, request, tmp_path, pinned_comfy_source):
    source = pinned_comfy_source
    profile = SimpleNamespace(comfy_source=source,
        extra_launch_flags=('--use-ck-attention', '--disable-comfy-compiler'),
        standin_distributions={'torch': '2.10.0+cu130', 'comfy-kitchen': '0.2.35', 'comfy-aimdo': '0.5.5'})
    generator = managed_install.__wrapped__(session, monkeypatch, profile, tmp_path)
    fixture = next(generator)
    try:
        yield fixture
    finally:
        try:
            next(generator)
        except StopIteration:
            pass


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
# Slow on purpose: the video/h3 case measured about 165 s on hosted Lanes; the suite default is 120 s.
@pytest.mark.timeout(300)
def test_runpod_dry_live_shipped_t2i(cell, tmp_path, monkeypatch, live_machine, pinned_managed_install):
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
    args = dict(placement='runpod', align='trust', client=client, project='demo', **values)
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
    job = MachineJob(entry=inputs['generation_intent']['metadata']['model'],
                     mode=inputs['generation_intent']['metadata']['mode'], policy=json.loads(inputs['placement_policy']),
                     inputs=inputs, invocation_id='task:attempt', task_id='task', attempt_id='attempt',
                     scratch=scratch, progress_path=scratch / 'progress.json', align='trust')
    remote = machine.RunPodMachine(adapter).start(lease, job)
    job_path = Path(remote.remote_root) / 'job.json'
    assert job_path.read_bytes() == transport.uploads[str(job_path)]
    shipped = json.loads(job_path.read_bytes())
    dry_values = json.loads(dry_bindings)
    media_bindings = {}
    if 'managed_assets' in shipped['execution']:
        from astrid.packs.vibecomfy.asset_manifest import read_archive
        archive = read_archive(shipped['execution']['managed_assets'])
        media_bindings = archive.bindings
        for row in archive.manifest['assets']:
            dry_value = dry_values[row['binding']]
            if h3:
                assert Path(dry_value).name == dry_value
                assert archive.members[row['member']] == source.read_bytes()
            else:
                assert archive.members[row['member']] == Path(dry_value).read_bytes()
    assert shipped['bindings'] == {name: value for name, value in dry_values.items() if name not in media_bindings}

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
    from astrid.core.generation import entries
    from vibecomfy.model_assets import _referenced_model_values
    files = [path for engine in dict.fromkeys(('comfyui', *entries.runpod_engines()))
             for path in receipt_files(provider._receipt_for(engine))]
    files += [value for ref in _referenced_model_values(workflow)
              for value in (ref['value'], ref['subdir'] + '/' + ref['value'])]
    client_engine, payload, recipe, root, started, bootstrap = pinned_managed_install
    from astrid.core.generation import engine_recipe
    from astrid.core.generation.engine_local_endpoint import record_engine_root
    monkeypatch.setattr(engine_recipe, 'load_recipe', lambda engine, platform, **kwargs: recipe)
    record_engine_root('comfyui', str(root))
    records = tmp_path / 'peer-records'
    records.mkdir()
    program = f'''
import sys,json,os,threading
sys.path[:0] = {[str(Path(__file__).resolve().parents[2]), *sys.path]!r}
from pathlib import Path
from tests.integrations.runpod_slot_pod import PodComfy,pin_object_info
from tests.integrations.test_runpod_slot_path_cpu import _attached_comfy
class Recorded(list):
 def __init__(self,name): self.path=Path({str(records)!r})/name
 def append(self,value):
  with self.path.open('a') as out: out.write(json.dumps(value)+'\\n')
  super().append(value)
peer=PodComfy({provider.comfy.classes!r}, lambda: {files!r}, extra=pin_object_info())
peer.h3_output={h3!r}
peer.prompts=Recorded('prompts'); peer.views=Recorded('views'); peer.rejections=Recorded('rejections')
port=int(sys.argv[1]); Path(sys.argv[3]).write_text(str(os.getpid())+'\\n')
with _attached_comfy(peer,port): threading.Event().wait()
'''
    (root / 'tiny_http.py').write_text(program)
    client_engine.stop_owned(deadline=time.monotonic() + 15)
    try:
        result = run_job(job.entry, job_path, Path(remote.remote_root) / 'out', mode=job.mode)
    finally:
        client_engine.stop_owned(deadline=time.monotonic() + 15)
    provider.comfy.installed_files = lambda: files
    for name in ('prompts', 'views', 'rejections'):
        path = records / name
        setattr(provider.comfy, name, [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else [])
    assert (Path(remote.remote_root) / 'out' / result['managed_generation_result_path']).is_file()
    assert len(observed) == len(runtime) == len(namespaces) == 1
    assert observed[0][1] == 'trust'
    # Pin the managed layout independently of the product's path resolver.
    assert observed[0][0] == {**dry['entry']['requirements'],
                              'models_root': str(root / "runtime" / "ComfyUI" / "models")}
    from astrid.packs.vibecomfy.local_server_engine import run_scoped_overrides
    scoped = run_scoped_overrides(workflow, namespaces[0])
    for name, filename in media_bindings.items():
        assert str(runtime[0][name]).endswith(filename)
    for name, value in runtime[0].items():
        if str(getattr(scoped.inputs[name], 'media_semantics', '') or '').lower() not in {'image', 'mask', 'audio', 'video'}:
            assert shipped['bindings'].get(name, scoped.inputs[name].value) == value
    expected = json.loads(_graph(scoped, run_inputs=runtime[0]))['api']
    assert provider.comfy.prompts[-1]['prompt'] == expected
