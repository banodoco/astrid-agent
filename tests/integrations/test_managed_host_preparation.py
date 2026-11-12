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
import json,os,sys,uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
from urllib.parse import urlsplit,parse_qs
from PIL import Image
port=int(sys.argv[1]); started=Path(sys.argv[3]); histories={}
with started.open('a') as stream:stream.write(str(os.getpid())+'\n')
(started.parent/'device-env').write_text(os.environ.get('CUDA_VISIBLE_DEVICES', '<unset>'))
schema={name:{} for name in ('MiniMaxH3ReferenceToVideo','UNETLoader','VAELoader','VHS_VideoCombine')}
schema.update({
 'EmptyImage':{'input':{'required':{'width':['INT',{'default':16}],'height':['INT',{'default':16}],'batch_size':['INT',{'default':1}],'color':['INT',{'default':0}]}},'output':['IMAGE'],'output_name':['IMAGE'],'output_node':False,'python_module':'nodes'},
 'SaveImage':{'input':{'required':{'images':['IMAGE'],'filename_prefix':['STRING',{'default':'astrid'}]}},'output':[],'output_node':True,'python_module':'nodes'}})
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def send(self,payload):
  data=json.dumps(payload).encode();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def do_GET(self):
  if self.path.startswith('/view?'):
   name=parse_qs(urlsplit(self.path).query)['filename'][0];data=(started.parent/name).read_bytes()
   self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
  if self.path=='/object_info':return self.send({name:value for name,value in schema.items() if name!='EmptyImage' or not (started.parent/'missing-node').exists()})
  if self.path.startswith('/history/'):
   key=self.path.rsplit('/',1)[1];return self.send({key:histories[key]} if key in histories else {})
  if self.path=='/queue':return self.send({'queue_running':[],'queue_pending':[]})
  return self.send({'system':{'comfyui_version':'0.36.0'}})
 def do_POST(self):
  request=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
  if self.path!='/prompt':return self.send({})
  key=uuid.uuid4().hex;filename='cpu-output.png';Image.new('RGB',(16,16),(10,20,30)).save(started.parent/filename)
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
    profile = type('Profile', (), {'param': 'managed_native',
                                   'comfy_source': getattr(request, 'comfy_source', None),
                                   'extra_launch_flags': getattr(request, 'extra_launch_flags', ()),
                                   'standin_distributions': getattr(request, 'standin_distributions', {})})()
    generator = cpu_h3_install.__wrapped__(session, monkeypatch, profile)
    fixture = next(generator)
    installed_python = root / "runtime/venv/bin/python"
    import vibecomfy
    expected_module = hashlib.sha256(Path(vibecomfy.__file__).read_bytes()).hexdigest()
    identity_program = "import hashlib,json,importlib.metadata,vibecomfy;from pathlib import Path;print(json.dumps({'version':importlib.metadata.version('vibecomfy'),'module_sha256':hashlib.sha256(Path(vibecomfy.__file__).read_bytes()).hexdigest()}))"
    identity = fixture[0].run(CommandRequest((str(installed_python), '-I', '-c', identity_program),
        str(root), {}, time.monotonic() + 15))
    assert identity.exit_code == 0
    observed = json.loads(identity.stdout)
    assert observed['module_sha256'] == expected_module
    (tmp_path / 'native-cli-identity.json').write_text(json.dumps(observed))
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


@pytest.mark.parametrize('failure', [None, 'align', 'start', 'cancel', 'schema', 'cancel-after-schema', 'trust', 'device-explicit', 'device-default', 'device-ambient', 'health', 'receipt-before', 'receipt-after'],
                         ids=['repairs-before-worker', 'align-fails-no-worker', 'start-fails-no-worker', 'cancel-no-worker', 'schema-fails-no-worker', 'cancel-after-schema-no-worker', 'trust-skips-absent-auxiliary', 'device-explicit', 'device-default', 'device-ambient', 'health', 'receipt-before', 'receipt-after'])
def test_managed_host_prepares_before_sandboxed_worker(tmp_path, monkeypatch, real_runtime, managed_install, artifact_server, failure):
    from vibecomfy.workflow_bundle import emit_bundle_with_candidate
    from vibecomfy.security.provenance import Provenance
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
    from vibecomfy.templates import ReadyMetadata, new_workflow, OutputSpec
    from vibecomfy.nodes.core import EmptyImage, SaveImage
    asset = {'name': artifact.name, 'url': artifact_url, 'subdir': 'checkpoints',
             'sha256': hashlib.sha256(artifact_bytes).hexdigest(), 'size_bytes': len(artifact_bytes)}
    with new_workflow(ReadyMetadata.build(template_id='plan32-cpu', capability='image', model_assets=[asset])) as workflow:
        image = EmptyImage(width=16, height=16, batch_size=1, color=0)
        saved = SaveImage(images=image.out('IMAGE'), filename_prefix='astrid')
        workflow.finalize({}, outputs=[OutputSpec(node=saved)])
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    emit_bundle_with_candidate(workflow, bundle / 'workflow.py', Provenance.USER_CONFIRMED, None)
    diagnostic = client.run(CommandRequest((str(root / 'runtime/venv/bin/python'), '-I', '-c',
        'import sys;from vibecomfy.cli import main;sys.exit(main(sys.argv[1:]))',
        'doctor', str(bundle / 'workflow.py'), '--yes', '--non-interactive', '--json'),
        str(root / 'runtime/ComfyUI'), {'VIBECOMFY_MODELS_ROOT': str(root / 'models'), 'VIBECOMFY_HEADLESS': '1'},
        time.monotonic() + 15))
    assert diagnostic.exit_code == 1 and json.loads(diagnostic.stdout).get('missing_models'), diagnostic
    client.stop_owned(deadline=time.monotonic() + 15)
    original_receipt = (root / '.astrid-engine/install.json').read_bytes()
    started.unlink()
    assert not artifact.exists()
    events = []
    engine = entries.engine_table()['comfyui']
    original_align = type(engine).align
    def align(self, manifest, mode, **kwargs):
        events.append('align')
        assert mode == ('trust' if failure == 'trust' else 'align') and manifest['models_root'] == str(root / 'models')
        assert any(row['name'] == artifact.name for row in manifest['models'])
        if failure != 'device-ambient' or events.count('align') == 1:
            assert not started.exists(), 'server started before host alignment'
        if failure == 'align':
            raise ValueError('forced managed align failure')
        return original_align(self, manifest, mode, **kwargs)
    monkeypatch.setattr(type(engine), 'align', align)

    service_launches = []
    original_service_call = engine_custody.LinuxEngineServiceClient.call
    def record_service_call(self, operation, payload, **kwargs):
        if operation == 'probe':
            service_launches.append(payload['launch']['env'].get('CUDA_VISIBLE_DEVICES'))
        return original_service_call(self, operation, payload, **kwargs)
    monkeypatch.setattr(engine_custody.LinuxEngineServiceClient, 'call', record_service_call)

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
        if failure == 'trust': assert not artifact.exists()
        else: assert artifact.read_bytes() == artifact_bytes
        assert report['placement'] == 'managed' and report['health']['healthy'] is True
        if failure == 'schema': (root / 'missing-node').touch()
        return report
    monkeypatch.setattr(ManagedEnginePreparation, 'prepare', prepare)
    if failure in {'receipt-before', 'receipt-after'}:
        from astrid.core.generation.engine_execution import LocalLinuxExecutionContext
        original_write_json = LocalLinuxExecutionContext.write_json_atomic
        def fail_receipt_write(self, path, value, *, deadline):
            if path.endswith('/.astrid-engine/install.json'):
                if failure == 'receipt-after':
                    original_write_json(self, path, value, deadline=deadline)
                raise OSError('injected receipt write failure')
            return original_write_json(self, path, value, deadline=deadline)
        monkeypatch.setattr(LocalLinuxExecutionContext, 'write_json_atomic', fail_receipt_write)
    if failure == 'health':
        original_service = engine_custody.LinuxEngineServiceClient.call
        def fail_health(self, operation, payload, **kwargs):
            if operation == 'health':
                raise ValueError('forced managed health failure')
            return original_service(self, operation, payload, **kwargs)
        monkeypatch.setattr(engine_custody.LinuxEngineServiceClient, 'call', fail_health)
    if failure == 'start':
        original_start = engine_custody.LinuxEngineServiceClient.call
        def fail_start(self, operation, payload, **kwargs):
            if operation == 'start': raise ValueError('forced managed start failure')
            return original_start(self, operation, payload, **kwargs)
        monkeypatch.setattr(engine_custody.LinuxEngineServiceClient, 'call', fail_start)
    if failure == 'cancel-after-schema':
        from astrid.packs.vibecomfy import local_server_engine
        original_approve = local_server_engine._approve_live
        def cancel_after_schema(*args, **kwargs):
            original_approve(*args, **kwargs)
            from astrid.core.execution.generic_host import HostCancelled
            raise HostCancelled('forced cancellation after schema preflight')
        monkeypatch.setattr(local_server_engine, '_approve_live', cancel_after_schema)
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
    receipt_written = threading.Event()
    original_write = GenericPackHost._write_task_result
    def write_receipt(self, terminal):
        result = original_write(self, terminal)
        if terminal.get('status') in {'failed', 'cancelled', 'succeeded'} and (tmp_path / 'attempt/result.json').is_file():
            receipt_written.set()
        return result
    monkeypatch.setattr(GenericPackHost, '_write_task_result', write_receipt)
    worker = threading.Thread(target=host.run, kwargs={'poll_seconds': 0.02}, daemon=True)
    worker.start()
    try:
        from astrid.sdk.generation import _import_generation_member
        controls = {"client": sdk, "project": project_id}
        imported = {name: _import_generation_member(path, controls) for name, path in {
            "python": bundle / "workflow.py", "companion": bundle / "workflow.vibe.json", "source": source}.items()}
        if failure in {'device-explicit', 'device-default', 'device-ambient'}:
            if failure == 'device-explicit':
                installed = json.loads((root / '.astrid-engine/install.json').read_text())
                assert next(step for step in installed['steps'] if step['kind'] == 'start')['facts']['cuda_visible_devices'] is None
            from astrid.sdk.generation import GenerationFacade
            from astrid.core.generation import settings
            if failure == 'device-ambient':
                monkeypatch.setenv('CUDA_VISIBLE_DEVICES', '0')
                placement = 'managed'
            else:
                settings.update(lambda document: document['placement'].update({'kind': 'local', 'device': 1}), sdk)
                placement = {'device': 2} if failure == 'device-explicit' else 'auto'
            result = GenerationFacade().workflow(workflow=bundle / 'workflow.py', placement=placement,
                inputs={}, client=sdk, project=project_id, align='align', wait=True,
                timeout_seconds=120, poll_seconds=0.05)
        else:
            result = sdk.invoke_result('vibecomfy.run', kind='action', project=project_id,
            inputs={**imported, 'workflow_inputs': '{}',
                    'generation_intent': {'version': 1, 'modality': 'image', 'partial_success_policy': 'reject',
                        'groups': [{'group_key': 'main', 'selectors': [{'selector': 'main-0', 'ordinal': 0,
                            'variant_key': 'original', 'required': True}]}], 'metadata': {'model': 'workflow', 'align': 'trust' if failure == 'trust' else 'align'}}},
            wait=True, timeout_seconds=120, poll_seconds=0.05)
        assert receipt_written.wait(10), 'host terminal receipt did not complete after Runtime settlement'
        if failure and failure not in {'trust', 'device-explicit', 'device-default', 'device-ambient'}:
            assert not result.ok
            assert 'prepare' in events and 'align' in events, (events, result.to_dict())
            if failure != 'align':
                assert 'fetched' in events, (events, result.to_dict())
            assert 'worker' not in events, events
            if failure not in {'schema', 'cancel-after-schema'}: assert 'ready' not in events, events
            assert not (root / 'prompts').exists()
            receipt = json.loads((tmp_path / 'attempt/result.json').read_text())
            assert receipt['machine']['placement'] == 'managed'
            if failure == 'schema':
                assert receipt['readiness']['placement'] == 'managed'
                assert receipt['readiness']['target']['endpoint'] == endpoint
            if failure in {'cancel', 'cancel-after-schema'}: assert receipt['status'] == 'cancelled'
            installed_bytes = (root / '.astrid-engine/install.json').read_bytes()
            if failure in {'align', 'start', 'health', 'cancel', 'receipt-before'}:
                assert installed_bytes == original_receipt
            if failure in {'schema', 'cancel-after-schema', 'receipt-after'}:
                assert installed_bytes != original_receipt
                installed = json.loads(installed_bytes)
                assert installed['status'] == 'installed'
                assert next(step for step in installed['steps'] if step['kind'] == 'start')['facts']['owner_id']
            assert not getattr(host, '_uncertain_engine_preparations', []), receipt
        else:
            assert result.ok, str(result.to_dict())[:7000]
            if failure != 'trust': assert events.index('fetched') < events.index('ready') < events.index('worker')
            else: assert events.index('ready') < events.index('worker')
            assert 'supervisor-denied' in events
            assert (root / 'prompts').is_file()
            receipt = json.loads((tmp_path / 'attempt/result.json').read_text())
            assert receipt['machine']['placement'] == 'managed'
            if failure in {'device-explicit', 'device-default', 'device-ambient'}:
                expected = {'device-explicit': '2', 'device-default': '1', 'device-ambient': '0'}[failure]
                assert (root / 'device-env').read_text() == expected
                assert receipt['machine']['device'] == expected
                if failure == 'device-explicit':
                    from astrid.core.generation import engine_readiness
                    from astrid.core.generation.engine_platforms import LinuxPlatformAdapter
                    from astrid.packs.runpod.installed_session import LocalInstalledVibeComfySessionTransport
                    installed = engine_readiness.canonical_status(root=str(root), deadline=time.monotonic() + 90)
                    assert installed.status == 'installed', installed
                    start = next(step.facts for step in installed.steps if step.kind == 'start')
                    assert start['cuda_visible_devices'] == '2'
                    transport = LocalInstalledVibeComfySessionTransport()
                    observed = transport.read_installed(str(root / 'runtime'))
                    assert observed['comfy']['pid'] == int(started.read_text().strip())
                    health, launch = engine_readiness._owned_health(str(root), recipe,
                        LinuxPlatformAdapter(recipe.platform), start['owner_id'], time.monotonic() + 30,
                        models_root=installed.models_root, visibility=start['cuda_visible_devices'])
                    assert health['healthy'] and launch['env']['CUDA_VISIBLE_DEVICES'] == '2'
                if failure == 'device-ambient':
                    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', '1')
                    with pytest.raises(Exception, match='engine supervisor: endpoint_ownership'):
                        GenerationFacade().workflow(workflow=bundle / 'workflow.py', placement='managed',
                            inputs={}, client=sdk, project=project_id, align='align', wait=True,
                            timeout_seconds=120, poll_seconds=0.05)
                    assert service_launches[-2:] == ['0', '1']
                    assert (root / 'device-env').read_text() == '0'
                    assert len(started.read_text().splitlines()) == 1
            assert receipt['managed_engine_preparation']['placement'] == 'managed'
            if failure != 'trust':
                assert artifact_hits and 'native-doctor' in events and 'native-prepare' in events
                assert (root / 'runtime/.vibecomfy-managed').is_file()
            else:
                assert artifact_hits == [] and not artifact.exists()
                assert receipt['managed_engine_preparation']['alignment']['verified'] is False
                documents = [*(tmp_path / 'attempt').rglob('*.json'), *(tmp_path / 'attempt').rglob('*.jsonl')]
                rendered = '\n'.join(path.read_text() for path in documents)
                assert 'verified_target_provisioning' not in rendered
                assert 'declared_unverified' in rendered and artifact.name in rendered
    finally:
        host._shutdown.set()
        worker.join(timeout=35)
        host.shutdown()
        daemon.stop()


@pytest.fixture
def managed_install_with_ambient(monkeypatch, request):
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', '2')
    return request.getfixturevalue('managed_install')


def test_real_status_and_readiness_use_installed_visibility(managed_install_with_ambient, monkeypatch):
    from astrid.core.generation import engine_readiness
    from astrid.core.generation.engine_platforms import LinuxPlatformAdapter
    client, payload, recipe, root, started, bootstrap = managed_install_with_ambient
    assert payload['launch']['env']['CUDA_VISIBLE_DEVICES'] == '2'
    monkeypatch.delenv('CUDA_VISIBLE_DEVICES')
    deadline = time.monotonic() + 90
    receipt = engine_readiness.canonical_status(root=str(root), deadline=deadline)
    assert receipt.status == 'installed', receipt
    start = next(step.facts for step in receipt.steps if step.kind == 'start')
    assert start['cuda_visible_devices'] == '2'
    health, launch = engine_readiness._owned_health(str(root), recipe, LinuxPlatformAdapter(recipe.platform),
        start['owner_id'], deadline, models_root=receipt.models_root, visibility=start['cuda_visible_devices'])
    assert health['healthy'] and launch['env']['CUDA_VISIBLE_DEVICES'] == '2'
    assert len(started.read_text().splitlines()) == 1


def test_legacy_local_server_restart_refreshes_the_receipt(managed_install, monkeypatch):
    from astrid.core.generation import engine_readiness
    from astrid.core.generation.engine_local_server import ensure_local_engine_server
    from astrid.core.generation.engine_platforms import LinuxPlatformAdapter
    from astrid.packs.runpod.installed_session import LocalInstalledVibeComfySessionTransport
    client, payload, recipe, root, started, bootstrap = managed_install
    old = json.loads((root / '.astrid-engine/install.json').read_text())
    assert next(step for step in old['steps'] if step['kind'] == 'start')['facts']['cuda_visible_devices'] is None
    client.stop_owned(deadline=time.monotonic() + 15)
    started.unlink()
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', '2')
    assert ensure_local_engine_server(root=str(root), deadline_seconds=90) == 'started'
    monkeypatch.delenv('CUDA_VISIBLE_DEVICES')
    current = engine_readiness.canonical_status(root=str(root), deadline=time.monotonic() + 90)
    assert current.status == 'installed', current
    start = next(step.facts for step in current.steps if step.kind == 'start')
    assert start['cuda_visible_devices'] == '2'
    installed = LocalInstalledVibeComfySessionTransport().read_installed(str(root / 'runtime'))
    assert installed['comfy']['pid'] == int(started.read_text().strip())
    health, launch = engine_readiness._owned_health(str(root), recipe, LinuxPlatformAdapter(recipe.platform),
        start['owner_id'], time.monotonic() + 30, models_root=current.models_root,
        visibility=start['cuda_visible_devices'])
    assert health['healthy'] and launch['env']['CUDA_VISIBLE_DEVICES'] == '2'


def test_legacy_local_server_refuses_modified_base_without_receipt_write(managed_install, monkeypatch):
    from astrid.core.generation.engine_contracts import IntegrityError
    from astrid.core.generation.engine_local_server import ensure_local_engine_server
    client, payload, recipe, root, started, bootstrap = managed_install
    original = (root / '.astrid-engine/install.json').read_bytes()
    client.stop_owned(deadline=time.monotonic() + 15)
    started.unlink()
    source = root / 'runtime/ComfyUI/tiny.py'
    source.write_text(source.read_text() + '\n# uncommitted change\n')
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', '2')
    with pytest.raises(IntegrityError, match='uncommitted'):
        ensure_local_engine_server(root=str(root), deadline_seconds=90)
    assert (root / '.astrid-engine/install.json').read_bytes() == original
    assert not started.exists()


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



def test_native_node_installer_bootstraps_bundled_pip_without_replacing_base(tmp_path, managed_install):
    from astrid.packs.vibecomfy.native_preparation import native_pip_program
    from astrid.core.generation.engine_runner import _Runner, plan_install
    client, payload, recipe, root, started, bootstrap = managed_install
    prep = ManagedEnginePreparation(task='pip-task', attempt='pip-attempt', mode='align',
        deadline=time.monotonic() + 60, cancelled=lambda: False, scratch=tmp_path / 'pip-preparation')
    lock = prep.adapter.local_install_lock(prep.root, deadline=prep.deadline, cancelled=prep.cancelled)
    lock.__enter__(); prep.lock = lock
    try:
        python = str(root / 'runtime/venv/bin/python')
        facts = json.loads(prep._native(native_pip_program(), json.dumps({'python_executable': python})).stdout)
        assert facts['existed'] is False
        assert len(facts['wheel_sha256']) == 64
        assert facts['installed']['version'] == facts['bundled_version']
        assert Path(facts['installed']['location']).is_relative_to(root / 'runtime/venv')
        repeated = json.loads(prep._native(native_pip_program(), json.dumps({'python_executable': python})).stdout)
        assert repeated['existed'] is True and repeated['installed'] == facts['installed']
        runner = _Runner(plan_install(recipe, root=str(root), placement='local'), prep.context, prep.adapter, prep.deadline)
        for step in runner.plan.steps:
            if step.kind in {'source', 'environment'}: runner.execute(step, readonly=True)
    finally:
        prep.close()



@pytest.fixture
def addon_index():
    import base64, csv, io, zipfile
    members = {'plan32_addon/__init__.py': b"__version__ = '1.0'\n",
               'plan32_addon-1.0.dist-info/METADATA': b'Metadata-Version: 2.3\nName: plan32-addon\nVersion: 1.0\n',
               'plan32_addon-1.0.dist-info/WHEEL': b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n'}
    records = [(name, 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('='), str(len(data)))
               for name, data in members.items()]
    records.append(('plan32_addon-1.0.dist-info/RECORD', '', ''))
    record = io.StringIO(); csv.writer(record).writerows(records)
    members['plan32_addon-1.0.dist-info/RECORD'] = record.getvalue().encode()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as wheel:
        for name, data in members.items(): wheel.writestr(name, data)
    state = {'available': True, 'requests': []}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            state['requests'].append(self.path)
            if self.path == '/packages/plan32_addon-1.0-py3-none-any.whl' and state['available']:
                data = archive.getvalue(); content_type = 'application/octet-stream'
            elif self.path.rstrip('/') == '/simple/plan32-addon':
                data = (b'<a href="../../packages/plan32_addon-1.0-py3-none-any.whl">plan32-addon</a>'
                        if state['available'] else b'')
                content_type = 'text/html'
            else:
                self.send_error(404); return
            self.send_response(200); self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Type', content_type); self.end_headers(); self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}/simple', state
    finally: server.shutdown(); server.server_close(); thread.join()


@pytest.mark.parametrize('outcome', ['installed', 'missing', 'changed-capsule', 'partial'])
def test_native_preparation_installs_only_allowed_addon_or_refuses_before_execution(tmp_path, managed_install, addon_index, outcome):
    from vibecomfy.templates import ReadyMetadata, new_workflow, OutputSpec
    from vibecomfy.nodes.core import EmptyImage, SaveImage
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import emit_bundle_with_candidate
    from astrid.packs.vibecomfy.native_preparation import native_preparation_program
    from astrid.core.generation.engine_contracts import IntegrityError
    from astrid.core.generation.engine_runner import _Runner, plan_install
    client, payload, recipe, root, started, bootstrap = managed_install
    index_url, state = addon_index
    state['available'] = outcome != 'missing'
    requirements = {'packages': {'plan32-addon': '==1.0'}, 'package_indexes': [index_url]}
    with new_workflow(ReadyMetadata.build(template_id='addon-cpu', capability='image', requirements={'runtime': requirements})) as workflow:
        image = EmptyImage(width=16, height=16, batch_size=1, color=0)
        saved = SaveImage(images=image.out('IMAGE'), filename_prefix='astrid')
        workflow.finalize({}, outputs=[OutputSpec(node=saved)])
    capsule = tmp_path / 'capsule/workflow.py'
    emitted = emit_bundle_with_candidate(workflow, capsule, Provenance.USER_CONFIRMED, None, source_format='scratchpad')
    hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in capsule.parent.iterdir() if path.is_file()}
    if outcome == 'changed-capsule': capsule.write_text(capsule.read_text() + '\n# changed after admission\n')
    prep = ManagedEnginePreparation(task='addon-task', attempt='addon-attempt', mode='align',
        deadline=time.monotonic() + 60, cancelled=lambda: False, scratch=tmp_path / 'addon-preparation')
    lock = prep.adapter.local_install_lock(prep.root, deadline=prep.deadline, cancelled=prep.cancelled)
    lock.__enter__(); prep.lock = lock
    try:
        request = {'workflow_path': str(capsule), 'runtime_root': str(root / 'runtime'),
                   'python_executable': str(root / 'runtime/venv/bin/python'),
                   'uv_executable': str(root / 'runtime/bootstrap/bin/uv'), 'launch_flags': [],
                   'capsule_sha256': hashes, 'expected_requirements': requirements, 'sync_packages': [] if outcome == 'partial' else ['plan32-addon']}
        def invoke(): return json.loads(prep._native(native_preparation_program(), json.dumps(request)).stdout)
        if outcome == 'installed':
            report = invoke()
            assert report['synchronization']['synced'] is True and report['synchronization']['actions']
            assert report['observation']['packages']['plan32-addon'] == '1.0'
            assert report['compliance']['status'] == 'matching'
            assert report['preparation']['plan']['workflow'] == str(capsule)
            unchanged = invoke()
            assert unchanged['synchronization']['synced'] is False
            assert unchanged['synchronization']['actions'] == []
            assert unchanged['preparation']['plan']['workflow'] == str(capsule)
            assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in hashes.items())
            assert any(path.startswith('/packages/') for path in state['requests'])
        else:
            with pytest.raises(IntegrityError): invoke()
            if outcome in {'changed-capsule', 'partial'}: assert state['requests'] == []
            assert not list((root / 'runtime/out/preparations').glob('*.json'))
        runner = _Runner(plan_install(recipe, root=str(root), placement='local'), prep.context, prep.adapter, prep.deadline)
        for step in runner.plan.steps:
            if step.kind in {'source', 'environment'}: runner.execute(step, readonly=True)
    finally:
        prep.close()


@pytest.mark.parametrize('outcome', ['capsule', 'base', 'cancel', 'report-cancel', 'receipt-failure', 'stop-uncertain', 'success'])
def test_owned_doctor_retains_custody_through_preparation_completion(tmp_path, monkeypatch, managed_install, outcome):
    """The real doctor launches a real owned CPU server before the injected fault."""
    import os
    import socket
    from concurrent.futures import CancelledError
    from astrid.core.generation.engine_contracts import CustodyUnresolved, IntegrityError
    from astrid.packs.vibecomfy.production_engine import load_workflow_path, _canonical_bundle_value
    client, payload, recipe, root, started, bootstrap = managed_install
    source = tmp_path / 'workflow.json'
    source.write_text(json.dumps(_workflow()))
    loaded = load_workflow_path(source, tmp_path / 'author', comfy_url=f'http://127.0.0.1:{recipe.start.port}')
    workflow = _canonical_bundle_value(loaded.resolved).workflow
    workflow.metadata['model_assets'] = []
    client.stop_owned(deadline=time.monotonic() + 15)
    started.unlink()
    cancelled, events = [], []
    prep = ManagedEnginePreparation(task='completion-task', attempt='completion-attempt', mode='align',
        deadline=time.monotonic() + 90, cancelled=lambda: bool(cancelled), scratch=tmp_path / 'completion')
    original_lock = prep.adapter.local_install_lock
    class Lock:
        def __init__(self, lock): self.lock = lock
        def __enter__(self): return self.lock.__enter__()
        def __exit__(self, *args):
            events.append('unlock')
            try:
                if outcome != 'success':
                    assert events[-2:] == ['stopped-and-drained', 'unlock']
                    assert not (root / '.astrid-engine/session/session.json').exists()
                    for pid in started.read_text().splitlines():
                        with pytest.raises(ProcessLookupError): os.kill(int(pid), 0)
                    with socket.socket() as probe:
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        probe.bind(('127.0.0.1', recipe.start.port))
            finally:
                self.lock.__exit__(*args)
    monkeypatch.setattr(prep.adapter, 'local_install_lock', lambda *args, **kw: Lock(original_lock(*args, **kw)))
    original_stop = prep.context.service_client.stop_owned
    def stop(*, deadline):
        events.append('stop-exact')
        if outcome == 'stop-uncertain': raise TimeoutError('PRIVATE_STOP_DETAIL')
        original_stop(deadline=deadline)
        events.append('stopped-and-drained')
    monkeypatch.setattr(prep.context.service_client, 'stop_owned', stop)
    original_command = prep._command
    def command(argv):
        result = original_command(argv)
        if argv[0] == 'doctor':
            assert started.is_file()
            assert len(started.read_text().splitlines()) == 1
            events.append('doctor-completed')
            if outcome in {'capsule', 'stop-uncertain'}:
                (prep.scratch / 'canonical/workflow.py').write_text('changed after doctor\n')
            elif outcome == 'base':
                path = root / 'runtime/ComfyUI/tiny.py'
                path.write_text(path.read_text() + '\n# changed after doctor\n')
            elif outcome == 'cancel': cancelled.append(True)
        return result
    monkeypatch.setattr(prep, '_command', command)
    if outcome == 'report-cancel':
        original_prepare = prep._prepare
        def cancel_completed_report(*args, **kwargs):
            report = original_prepare(*args, **kwargs)
            cancelled.append(True)
            return report
        monkeypatch.setattr(prep, '_prepare', cancel_completed_report)
    if outcome == 'receipt-failure':
        from astrid.core.generation import engine_runner
        original_refresh = engine_runner.refresh_installed_launch
        def fail_after_refresh(*args, **kwargs):
            original_refresh(*args, **kwargs)
            raise IntegrityError('injected failure after receipt refresh')
        monkeypatch.setattr(engine_runner, 'refresh_installed_launch', fail_after_refresh)
    try:
        def prepare():
            return prep.prepare(source, execution_identity='execution', inputs={'workflow': str(source)},
                canonical_workflow=workflow, input_digests={'workflow': hashlib.sha256(source.read_bytes()).hexdigest()})
        if outcome == 'success':
            assert prepare()['health']['healthy'] is True
            prep.close()
            assert events == ['doctor-completed', 'unlock']
            assert (root / '.astrid-engine/session/session.json').exists()
        elif outcome == 'stop-uncertain':
            with pytest.raises(CustodyUnresolved, match='stop remains unconfirmed') as error: prepare()
            assert 'PRIVATE_STOP_DETAIL' not in str(error.value)
            with pytest.raises(CustodyUnresolved): prep.close()
            assert prep.uncertain and prep.lock is not None
            assert events == ['doctor-completed', 'stop-exact']
            # The OS lock remains held, not just a Python flag.
            contender = subprocess.run(['flock', '-n', str(root / '.astrid-engine/install.lock'), 'true'])
            assert contender.returncode != 0
        else:
            with pytest.raises(CancelledError if outcome in {'cancel', 'report-cancel'} else IntegrityError): prepare()
            prep.close()
            assert events == ['doctor-completed', 'stop-exact', 'stopped-and-drained', 'unlock']
    finally:
        # Fixture-only recovery after the retained-fence assertion.
        original_stop(deadline=time.monotonic() + 15)
        if prep.lock is not None:
            prep.lock.lock.__exit__(None, None, None)
            prep.lock = None


@pytest.mark.parametrize('mutation', ['capsule', 'base', 'environment'])
def test_native_mutation_is_refused_before_owned_doctor_launch(tmp_path, monkeypatch, managed_install, mutation):
    from astrid.core.generation.engine_contracts import IntegrityError
    from astrid.packs.vibecomfy.production_engine import load_workflow_path, _canonical_bundle_value
    client, payload, recipe, root, started, bootstrap = managed_install
    source = tmp_path / 'workflow.json'
    source.write_text(json.dumps(_workflow()))
    loaded = load_workflow_path(source, tmp_path / 'author', comfy_url=f'http://127.0.0.1:{recipe.start.port}')
    workflow = _canonical_bundle_value(loaded.resolved).workflow
    workflow.metadata['model_assets'] = []
    client.stop_owned(deadline=time.monotonic() + 15)
    started.unlink()
    prep = ManagedEnginePreparation(task='mutation-task', attempt='mutation-attempt', mode='align',
        deadline=time.monotonic() + 90, cancelled=lambda: False, scratch=tmp_path / 'mutation')
    original_command = prep._command
    def command(argv):
        result = original_command(argv)
        if argv[0] == 'prepare':
            path = {'capsule': prep.scratch / 'canonical/workflow.py',
                    'base': root / 'runtime/ComfyUI/tiny.py',
                    'environment': root / '.astrid-engine/environments' / recipe.primary_environment / 'requirements.lock'}[mutation]
            path.write_text(path.read_text() + '\n# mutated by preparation\n')
        return result
    monkeypatch.setattr(prep, '_command', command)
    try:
        with pytest.raises(IntegrityError):
            prep.prepare(source, execution_identity='execution', inputs={'workflow': str(source)},
                canonical_workflow=workflow, input_digests={'workflow': hashlib.sha256(source.read_bytes()).hexdigest()})
        assert not started.exists(), 'mutated installation executed before base rejection'
    finally:
        prep.close()
