"""Real managed host/supervisor custody with a tiny, verified CPU installation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.core.generation import entries
from astrid.core.generation.engine_contracts import CommandRequest
from astrid.core.generation.managed_preparation import ManagedEnginePreparation
from astrid.sdk import AstridClient
from astrid.sdk.remote import RemoteAstridClient
from astrid.sdk.workspace_client import WorkspaceClient
from tests.core.generation.test_engine_custody import session, cpu_h3_install  # noqa: F401
from tests.packs.vibecomfy.test_local_server_run import _workflow

pytestmark = pytest.mark.integration

_SERVER = r'''
import json,os,sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
from urllib.parse import urlsplit,parse_qs
from PIL import Image
port=int(sys.argv[1]); started=Path(sys.argv[3]); histories={}
with started.open('a') as stream:stream.write(str(os.getpid())+'\n')
schema={name:{} for name in ('MiniMaxH3ReferenceToVideo','UNETLoader','VAELoader','VHS_VideoCombine')}
schema.update({
 'SeedImage':{'input':{'required':{'seed':['INT',{'default':42}],'ckpt_name':[['installed.safetensors']]}},'output':['IMAGE'],'output_name':['IMAGE'],'output_node':False},
 'SaveImage':{'input':{'required':{'images':['IMAGE'],'filename_prefix':['STRING',{'default':'astrid'}]}},'output':[],'output_node':True}})
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def send(self,payload):
  data=json.dumps(payload).encode();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def do_GET(self):
  if self.path.startswith('/view?'):
   name=parse_qs(urlsplit(self.path).query)['filename'][0];data=(started.parent/name).read_bytes()
   self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
  if self.path=='/object_info':return self.send(schema)
  if self.path.startswith('/history/'):
   key=self.path.rsplit('/',1)[1];return self.send({key:histories[key]} if key in histories else {})
  if self.path=='/queue':return self.send({'queue_running':[],'queue_pending':[]})
  return self.send({'system':{'comfyui_version':'0.36.0'}})
 def do_POST(self):
  request=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
  if self.path!='/prompt':return self.send({})
  key=request['client_id'];filename='cpu-output.png';Image.new('RGB',(16,16),(10,20,30)).save(started.parent/filename)
  with (started.parent/'prompts').open('a') as stream:stream.write(key+'\n')
  outputs={node:{'images':[{'filename':filename,'subfolder':'','type':'output'}]} for node,value in request['prompt'].items() if value['class_type']=='SaveImage'}
  histories[key]={'outputs':outputs,'status':{'completed':True,'status_str':'success'}}
  self.send({'prompt_id':key,'number':1,'node_errors':{}})
HTTPServer(('127.0.0.1',port),Handler).serve_forever()
'''


@pytest.fixture
def managed_install(session, monkeypatch, request, tmp_path):
    """The established native install fixture checks real source pins, locks,
    Python environment, uv pip check, model bytes, receipt and owned HTTP.
    Only recipe inputs and the CPU HTTP implementation are substituted.
    """
    root = session[3]
    (root / 'tiny_http.py').write_text(_SERVER)
    generator = cpu_h3_install.__wrapped__(session, monkeypatch, request)
    fixture = next(generator)
    monkeypatch.setattr('astrid.core.generation.engine_local_endpoint._engines_dir', lambda: tmp_path / 'roots')
    monkeypatch.setenv('ASTRID_ENGINE_ROOT', str(root))
    try:
        yield fixture
    finally:
        try:
            next(generator)
        except StopIteration:
            pass


@pytest.mark.parametrize('failure', [None, 'align', 'start', 'cancel'],
                         ids=['repairs-before-worker', 'align-fails-no-worker', 'start-fails-no-worker', 'cancel-no-worker'])
def test_managed_host_prepares_before_sandboxed_worker(tmp_path, monkeypatch, real_runtime, managed_install, failure):
    from vibecomfy.workflow_bundle import emit_bundle
    from astrid.packs.vibecomfy.production_engine import load_workflow_path, _canonical_bundle_value
    from astrid.core.generation import engine_custody
    client, payload, recipe, root, started, bootstrap = managed_install
    endpoint = f'http://127.0.0.1:{recipe.start.port}'
    # Author once while the installed HTTP schema is available, then make the
    # installed service cold and deliberately omit this graph's required row.
    source = tmp_path / 'source.json'
    source.write_text(json.dumps(_workflow()))
    loaded = load_workflow_path(source, tmp_path / 'author', comfy_url=endpoint)
    workflow = _canonical_bundle_value(loaded.resolved).workflow
    artifact, artifact_bytes = root / 'models/checkpoints/cpu-required.bin', b'CPU required artifact\n'
    workflow.metadata['model_assets'] = [{'name': artifact.name, 'subdir': 'checkpoints',
        'url': 'https://example.invalid/cpu-required.bin', 'sha256': hashlib.sha256(artifact_bytes).hexdigest(),
        'size_bytes': len(artifact_bytes)}]
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    emit_bundle(workflow, bundle / 'workflow.py', {'operation': 'authored'})
    client.stop_owned(deadline=time.monotonic() + 15)
    started.unlink()
    assert not artifact.exists()
    events = []
    engine = entries.engine_table()['comfyui']
    original_align = type(engine).align
    def align(self, manifest, mode, **kwargs):
        events.append('align')
        assert mode == 'align' and manifest['models_root'] == str(root / 'models')
        assert any(row['name'] == artifact.name for row in manifest['models'])
        assert not started.exists(), 'server started before host alignment'
        if failure == 'align':
            raise ValueError('forced managed align failure')
        return original_align(self, manifest, mode, **kwargs)
    monkeypatch.setattr(type(engine), 'align', align)

    # The CPU manifest row is provisioned through a real finite owned command;
    # production alignment's orchestration is unchanged, downloads are a stand-in.
    class Commands:
        def doctor(self): pass
        def prepare_deps_sync(self): pass
        def nodes_restore(self, rows): pass
        def fetch(self, rows):
            code = f"from pathlib import Path;p=Path({str(artifact)!r});p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes({artifact_bytes!r})"
            result = client.run(CommandRequest((sys.executable, '-I', '-c', code), str(root), {}, time.monotonic() + 30))
            assert result.exit_code == 0
            events.append('fetched')
    monkeypatch.setattr(engine, 'commands', Commands())
    original_prepare = ManagedEnginePreparation.prepare
    def prepare(self, *args, **kwargs):
        events.append('prepare')
        report = original_prepare(self, *args, **kwargs)
        events.append('ready')
        assert artifact.read_bytes() == artifact_bytes
        assert report['placement'] == 'managed' and report['health']['healthy'] is True
        return report
    monkeypatch.setattr(ManagedEnginePreparation, 'prepare', prepare)
    if failure == 'start':
        original_start = engine_custody.LinuxEngineServiceClient.call
        def fail_start(self, operation, payload, **kwargs):
            if operation == 'start': raise ValueError('forced managed start failure')
            return original_start(self, operation, payload, **kwargs)
        monkeypatch.setattr(engine_custody.LinuxEngineServiceClient, 'call', fail_start)
    run_command = GenericPackHost._run_command_definition
    def launch(host, record, inputs, output_root, attempt, **kwargs):
        events.append('worker')
        assert events.index('ready') < events.index('worker')
        return run_command(host, record, inputs, output_root, attempt, **kwargs)
    monkeypatch.setattr(GenericPackHost, '_run_command_definition', launch)
    child_environment = GenericPackHost._child_environment
    def environment(host, record, attempt, **kwargs):
        result = child_environment(host, record, attempt, **kwargs)
        if record.id == 'vibecomfy.run' and kwargs.get('managed_preparation'):
            env, _ = result
            probe = "import socket,sys\ns=socket.socket(socket.AF_UNIX)\ntry:s.connect(sys.argv[1])\nexcept (PermissionError,RuntimeError):print('denied')\nelse:raise SystemExit('supervisor socket was accessible')"
            checked = subprocess.run([sys.executable, '-c', probe, str(root / '.astrid-engine/session/control.sock')],
                                     env=env, cwd=attempt, capture_output=True, text=True, timeout=15)
            assert checked.returncode == 0 and checked.stdout.strip() == 'denied', checked.stderr
            events.append('supervisor-denied')
        return result
    monkeypatch.setattr(GenericPackHost, '_child_environment', environment)

    daemon = real_runtime()
    workspace = WorkspaceClient(daemon.endpoint, daemon.token)
    sdk = AstridClient(remote=RemoteAstridClient(workspace))
    project = sdk.projects.create(name='Managed CPU', slug='managed-cpu', idempotency_key='project')
    assert project.ok
    project_id = project.data['project_id']
    workspace.select_project(project_id, idempotency_key='select')
    packs = Path(__file__).resolve().parents[2] / 'astrid/packs'
    host = GenericPackHost(pack_roots=[packs / 'vibecomfy'],
        client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token), attempt_base=tmp_path / 'attempts')
    host.discover(); host.preflight(); host.register()
    if failure == 'cancel':
        original_check = ManagedEnginePreparation.check
        def cancel_before_start(self):
            if 'fetched' in events:
                from concurrent.futures import CancelledError
                raise CancelledError('forced managed preparation cancellation')
            return original_check(self)
        monkeypatch.setattr(ManagedEnginePreparation, 'check', cancel_before_start)
    worker = threading.Thread(target=host.run, kwargs={'poll_seconds': 0.02}, daemon=True)
    worker.start()
    try:
        result = sdk.invoke_result('vibecomfy.run', kind='action', project=project_id,
            inputs={'python': str(bundle / 'workflow.py'), 'companion': str(bundle / 'workflow.vibe.json'),
                    'source': str(source), 'workflow_inputs': '{}',
                    'generation_intent': {'version': 1, 'modality': 'image', 'partial_success_policy': 'reject',
                        'groups': [{'group_key': 'main', 'selectors': [{'selector': 'main-0', 'ordinal': 0,
                            'variant_key': 'original', 'required': True}]}], 'metadata': {'model': 'workflow', 'align': 'align'}}},
            wait=True, timeout_seconds=120, poll_seconds=0.05)
        if failure:
            assert not result.ok
            assert 'worker' not in events and 'ready' not in events, events
            assert not (root / 'prompts').exists()
        else:
            assert result.ok, str(result.to_dict())[:7000]
            assert events.index('fetched') < events.index('ready') < events.index('worker')
            assert 'supervisor-denied' in events
            assert (root / 'prompts').is_file()
            receipts = list((tmp_path / 'attempts').rglob('task-result.json'))
            assert receipts
            receipt = json.loads(receipts[-1].read_text())
            assert receipt['machine']['placement'] == 'managed'
            assert receipt['managed_engine_preparation']['placement'] == 'managed'
    finally:
        host._shutdown.set()
        worker.join(timeout=35)
        host.shutdown()
        daemon.stop()
