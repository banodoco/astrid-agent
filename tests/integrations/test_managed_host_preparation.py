"""Real managed host/supervisor custody with a tiny, verified CPU installation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.core.generation import entries
from astrid.core.generation.engine_contracts import CommandRequest
from astrid.core.generation.managed_preparation import ManagedEnginePreparation
from astrid.sdk import AstridClient
from astrid.sdk.remote import RemoteAstridClient
from astrid.sdk.workspace_client import WorkspaceClient
from tests.core.generation.test_engine_custody import session, cpu_h3_install  # noqa: F401
from tests.packs.vibecomfy.test_local_server_run import _workflow as _seed_workflow

def _workflow():
    workflow = _seed_workflow()
    workflow['nodes'][0].update(type='EmptyImage', widgets_values=[16, 16, 1, 0],
        properties={'Node name for S&R': 'EmptyImage'})
    return workflow

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
 'EmptyImage':{'input':{'required':{'width':['INT',{'default':16}],'height':['INT',{'default':16}],'batch_size':['INT',{'default':1}],'color':['INT',{'default':0}]}},'output':['IMAGE'],'output_name':['IMAGE'],'output_node':False},
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
    generator = cpu_h3_install.__wrapped__(session, monkeypatch, type('Profile', (), {'param': 'managed_native'})())
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


@pytest.fixture
def artifact_server():
    data, hits = b'CPU required artifact\n', []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/cpu-required.bin', data, hits
    finally:
        server.shutdown(); server.server_close(); thread.join()


@pytest.mark.parametrize('failure', [None, 'align', 'start', 'cancel'],
                         ids=['repairs-before-worker', 'align-fails-no-worker', 'start-fails-no-worker', 'cancel-no-worker'])
def test_managed_host_prepares_before_sandboxed_worker(tmp_path, monkeypatch, real_runtime, managed_install, artifact_server, failure):
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
    artifact_url, artifact_bytes, artifact_hits = artifact_server
    artifact = root / 'models/checkpoints/cpu-required.bin'
    workflow.metadata['model_assets'] = [{'name': artifact.name, 'subdir': 'checkpoints',
        'url': artifact_url, 'sha256': hashlib.sha256(artifact_bytes).hexdigest(),
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

    original_command = ManagedEnginePreparation._command
    def command(self, argv):
        events.append('native-' + argv[0])
        original_command(self, argv)
        if argv[0] == 'fetch': events.append('fetched')
    monkeypatch.setattr(ManagedEnginePreparation, '_command', command)
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
        client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token), attempt_root=tmp_path / 'attempt')
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
        from astrid.sdk.generation import _import_generation_member
        controls = {"client": sdk, "project": project_id}
        imported = {name: _import_generation_member(path, controls) for name, path in {
            "python": bundle / "workflow.py", "companion": bundle / "workflow.vibe.json", "source": source}.items()}
        result = sdk.invoke_result('vibecomfy.run', kind='action', project=project_id,
            inputs={**imported, 'workflow_inputs': '{}',
                    'generation_intent': {'version': 1, 'modality': 'image', 'partial_success_policy': 'reject',
                        'groups': [{'group_key': 'main', 'selectors': [{'selector': 'main-0', 'ordinal': 0,
                            'variant_key': 'original', 'required': True}]}], 'metadata': {'model': 'workflow', 'align': 'align'}}},
            wait=True, timeout_seconds=120, poll_seconds=0.05)
        if failure:
            assert not result.ok
            assert 'prepare' in events and 'align' in events, (events, result.to_dict())
            if failure != 'align':
                assert 'fetched' in events, (events, result.to_dict())
            assert 'worker' not in events and 'ready' not in events, events
            assert not (root / 'prompts').exists()
            receipt = json.loads((tmp_path / 'attempt/result.json').read_text())
            assert receipt['machine']['placement'] == 'managed'
            assert not getattr(host, '_uncertain_engine_preparations', []), receipt
        else:
            assert result.ok, str(result.to_dict())[:7000]
            assert events.index('fetched') < events.index('ready') < events.index('worker')
            assert 'supervisor-denied' in events
            assert (root / 'prompts').is_file()
            receipt = json.loads((tmp_path / 'attempt/result.json').read_text())
            assert receipt['machine']['placement'] == 'managed'
            assert receipt['managed_engine_preparation']['placement'] == 'managed'
            assert artifact_hits and 'native-doctor' in events and 'native-prepare' in events
            assert (root / 'runtime/.vibecomfy-managed').is_file()
    finally:
        host._shutdown.set()
        worker.join(timeout=35)
        host.shutdown()
        daemon.stop()


def test_plain_workflow_preparation_does_not_write_beside_admitted_input(tmp_path, monkeypatch, managed_install):
    from astrid.packs.vibecomfy.production_engine import load_workflow_path, _canonical_bundle_value
    client, payload, recipe, root, started, bootstrap = managed_install
    inputs = tmp_path / 'admitted'
    inputs.mkdir()
    source = inputs / 'workflow.json'
    source.write_text(json.dumps(_workflow()))
    loaded = load_workflow_path(source, tmp_path / 'author', comfy_url=f'http://127.0.0.1:{recipe.start.port}')
    workflow = _canonical_bundle_value(loaded.resolved).workflow
    workflow.metadata['model_assets'] = []
    client.stop_owned(deadline=time.monotonic() + 15)
    before = {path.name: path.read_bytes() for path in inputs.iterdir()}
    preparation = ManagedEnginePreparation(task='raw-task', attempt='raw-attempt', mode='align',
        deadline=time.monotonic() + 60, cancelled=lambda: False, scratch=tmp_path / 'private-preparation')
    try:
        report = preparation.prepare(source, execution_identity='raw-execution', inputs={'workflow': str(source)},
            canonical_workflow=workflow, input_digests={'workflow': hashlib.sha256(source.read_bytes()).hexdigest()})
        assert report['placement'] == 'managed' and report['health']['healthy'] is True
        assert {path.name: path.read_bytes() for path in inputs.iterdir()} == before
        assert (tmp_path / 'private-preparation/managed-custom-nodes.lock').is_file()
    finally:
        preparation.close()
