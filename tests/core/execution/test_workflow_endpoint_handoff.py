"""Independent bridge data/order tests; installed HTTP output gate remains separate.

The preparation case uses 47's real retained native CPU engine and its local
installed-session verifier. The target byte boundary executes its actual
standalone helper in separate processes. It does not claim that observation-
only RunPod offers that command/write/live-verification handoff.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from astrid.core.generation.engine_contracts import AttestationError, EndpointOwnershipError, SecretRef
from astrid.core.generation.engine_endpoint import endpoint_to_dict
from astrid.core.generation.engine_transport import EngineEndpointTransport
from astrid.packs.runpod.worker_owner import RunPodRemoteWorkerPreparer, _run_sync
from astrid.packs.runpod.worker_preparation import write_target_record
from astrid.packs.runpod.worker_staging import RunPodPreparationTransport, WorkerPreparationError, _secret_free
from astrid.packs.vibecomfy.endpoint_checkout import (
    EXECUTION_REQUEST_ENV, checkout_from_profile, load_workflow_endpoint,
    read_host_workflow_execution_request, read_workflow_profile, workflow_endpoint_network_policy,
)
from astrid.sdk.execution_request import normalize_execution_request
from runpod_lifecycle.pod_agent import AgentServer
# Reuse the delivered session owner's real native CPU engine, not a fake adapter.
from tests.core.generation.test_engine_custody import installed_consumer, session, native_scratch

pytestmark = pytest.mark.integration


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def request():
    return normalize_execution_request({"target": {"kind": "runpod", "pod_id": "pod123",
        "provider_account_ref": "account-1", "storage": {"network_volume_id": "volume-1"}}})


class LocalTarget:
    """Test physical target for the existing ordinary command/file boundary."""
    id = "pod123"

    def __init__(self):
        self.commands = []

    async def exec_ssh(self, command, *, timeout):
        self.commands.append(command)
        result = await asyncio.create_subprocess_shell(command, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(result.communicate(), timeout)
        return result.returncode, stdout.decode(), stderr.decode()

    async def put_file(self, local, remote, *, reserved):
        assert reserved is True
        fd = os.open(remote, os.O_WRONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "wb") as stream:
            stream.write(Path(local).read_bytes())


@dataclass(frozen=True)
class Reference:
    effective_target: dict
    executable: object
    model_root: Path
    output_root: Path
    session_ref: str
    session_config_digest: str
    readiness_profile_path: Path
    readiness_profile_hash: str
    task_id: str = "task-original"
    run_id: str = "run-original"
    runtime_instance_id: str = "runtime"
    runtime_epoch: int = 1
    executor_id: str = "executor"
    credential_ref: str = "/private/executor.token"
    ready_file: Path | None = None
    support_root: Path = Path("/private/support")
    source_checkout: Path = Path("/selected/astrid")
    source_closure_digest: str = "sha256:" + "a" * 64

    def digest(self):
        return "sha256:" + hashlib.sha256(encoded({
            "profile": self.readiness_profile_hash, "target": self.effective_target,
            "config": self.session_config_digest,
        })).hexdigest()


@pytest.fixture
def prepared_endpoint(installed_consumer, session, tmp_path, monkeypatch):
    from astrid.core.generation import engine_endpoint

    owner, installed_transport, selected, cpu, registry = installed_consumer
    native, payload, recipe, root, mode, started = session
    events = []
    state = tmp_path / "agent-state"
    state.mkdir(mode=0o700)
    profile_path = state / "engine-readiness.json"
    base_profile = {"schema_version": "hc03-worker-readiness.v1", "status": "ready"}
    profile_path.write_bytes(encoded(base_profile))
    profile_path.chmod(0o600)
    reference = Reference(
        request()["target"], selected.executable, selected.model_root, selected.output_root,
        selected.session_ref, selected.session_config_digest, profile_path,
        "sha256:" + hashlib.sha256(profile_path.read_bytes()).hexdigest(),
    )
    token = "fixture-original-agent-bearer-" + "x" * 32
    controller_token = tmp_path / "controller-bearer"
    controller_token.write_text(token)
    controller_token.chmod(0o600)
    claim = {"state": "claimed", "operation_id": "original", "provider_account_ref": "account-1",
             "pod_id": "pod123", "network_volume_id": "volume-1", "network_volume_name": "volume",
             "network_volume_size_gb": 200, "network_volume_datacenter_id": "dc",
             "volume_mount_path": "/workspace", "acquisition_mode": "allocate", "transport": "https",
             "https_token_file": str(controller_token)}
    claim_path = tmp_path / "original-claim.json"
    write_target_record(claim_path, claim)
    pod = LocalTarget()
    transport = RunPodPreparationTransport(pod)
    staging = {"status": "cpu_ready", "gpu_qualified": False, "worker_qualified": False,
               "cpu_readiness": cpu, "plan_digest": "sha256:" + "d" * 64}
    preparer = RunPodRemoteWorkerPreparer(
        pod, provider_account_ref="account-1", runtime_session_id="runtime-session",
        claim_handle_path=claim_path, journal_dir=tmp_path / "journal", staging_inputs={},
        preparation_transport=transport, session_owner=owner, session_port=recipe.start.port,
    )
    monkeypatch.setattr(preparer, "_stage", lambda ref: events.append("stage") or staging)
    original_start = owner.start
    monkeypatch.setattr(owner, "start", lambda selection: events.append("attach") or original_start(selection))
    ensure = engine_endpoint.ensure_prepared_session_endpoint
    monkeypatch.setattr(engine_endpoint, "ensure_prepared_session_endpoint",
                        lambda *a, **kw: events.append("ensure") or ensure(*a, **kw))
    writer = transport.write_endpoint_profile
    async def write(*a, **kw):
        events.append("write-final")
        await writer(*a, **kw)
    monkeypatch.setattr(transport, "write_endpoint_profile", write)
    # Actual authenticated agent -> actual retained native tiny HTTP engine.
    server = AgentServer(("127.0.0.1", 0), token, state,
                         engine_proxy={"endpoint_id": "vibecomfy-h3", "port": recipe.start.port})
    cert, key = tmp_path / "tls.crt", tmp_path / "tls.key"
    host = "pod123-8000.proxy.runpod.net"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=" + host, "-addext", "subjectAltName=DNS:" + host,
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True, timeout=10)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    calls = []
    from runpod_lifecycle.pod_agent import Handler
    class RecordedHandler(Handler):
        def dispatch(self):
            calls.append((self.command, self.path, bool(self.headers.get("Upgrade")), bool(self.headers.get("Range"))))
            return super().dispatch()
    server.RequestHandlerClass = RecordedHandler
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def connection(self, timeout):
        # Physical dial mapping only. Original hostname, TLS verification,
        # origin check, safe bearer open and server authentication remain real.
        result = http.client.HTTPSConnection(self._origin.hostname, timeout=timeout,
                    context=ssl.create_default_context(cafile=str(cert)))
        import socket
        result._create_connection = lambda address, budget, *a: socket.create_connection(
            ("127.0.0.1", server.server_port), budget)
        return result
    monkeypatch.setattr(EngineEndpointTransport, "_connection", connection)
    def read(path, *, deadline):
        return _run_sync(transport.read_endpoint_file(path, deadline=deadline))
    kwargs = dict(engine="vibecomfy-h3", platform=recipe.platform, endpoint_id="vibecomfy-h3",
                  agent_port=8000, token_ref=SecretRef("file", str(controller_token)), read_file=read,
                  deadline=time.monotonic() + 45)
    try:
        yield SimpleNamespace(preparer=preparer, reference=reference, kwargs=kwargs, events=events,
            profile_path=profile_path, base=base_profile, native=native, started=started,
            claim_path=claim_path, claim=claim, calls=calls, pod=pod, staging=staging,
            registry=registry, owner=owner, recipe=recipe)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_prefreeze_ensure_uses_original_live_session_and_actual_file_readback_once(prepared_endpoint, monkeypatch):
    p = prepared_endpoint
    final = p.preparer.finalize_endpoint_readiness(p.reference, **p.kwargs)
    again = p.preparer.finalize_endpoint_readiness(p.reference, **p.kwargs)
    assert final == again
    assert p.events == ["stage", "attach", "ensure", "write-final"]
    assert p.calls == [("GET", "/engines/vibecomfy-h3/object_info", False, False)]
    final_profile = read_workflow_profile(final.readiness_profile_path, final.readiness_profile_hash)
    assert final.readiness_profile_hash != p.reference.readiness_profile_hash
    assert final_profile["workflow_endpoint"] == {"endpoint_id": "vibecomfy-h3", "token_ref": {
        "kind": "file", "path": str(p.profile_path.parent / "engine-bearer.token")}}
    assert p.base == {"schema_version": "hc03-worker-readiness.v1", "status": "ready"}
    _secret_free(p.base)  # CPU staging retains its original secret-free document.
    # No profile contents/launch token travelled in remote command argv.
    marker = json.loads((p.registry / "launch.json").read_bytes())
    assert all(marker["launch_token"] not in command for command in p.pod.commands)
    class ProcessOwner:
        async def launch_projected(self, launch):
            p.events.append("park-process")
            assert launch.reference.readiness_profile_hash == final.readiness_profile_hash
            return SimpleNamespace(as_dict=lambda: {"fixture": "original-host"})
    monkeypatch.setattr(p.preparer, "_owner", lambda ref: ProcessOwner())
    from runtime_protocol.remote_worker_deployment import ProjectedLaunch
    from runtime_protocol.remote_worker_activation import RemotePreparationLaunch
    launch = ProjectedLaunch((str(final.executable.path), "-m", "fixture-host"), (), final.digest(), final.source_checkout)
    handle = p.preparer.prepare(RemotePreparationLaunch(final, launch))
    assert p.events == ["stage", "attach", "ensure", "write-final", "park-process"]
    saved = json.loads(p.claim_path.read_bytes())
    assert saved["managed_worker"]["engine_endpoint"] == p.preparer._endpoint_preparation["endpoint"]
    assert saved["managed_worker"]["readiness_profile_hash"] == final.readiness_profile_hash
    assert saved["managed_worker"]["task_id"] == final.task_id
    p.preparer.record_local_generation({"executor_incarnation": "local-original", "evidence_digest": "sha256:" + "e" * 64,
                                       "profile_id": "profile", "workspace_uuid": "workspace"})
    assert json.loads(p.claim_path.read_bytes())["managed_worker"]["engine_endpoint"] == saved["managed_worker"]["engine_endpoint"]
    assert saved["operation_id"] == p.claim["operation_id"]
    assert len(p.started.read_text().splitlines()) == 1
    assert handle.session_receipt.daemon_pid == p.native._spawned.pid
    endpoint = load_workflow_endpoint(final_profile, profile_path=p.profile_path, execution_request=request())
    assert endpoint.lifecycle_owner.kind == "attached" and not endpoint.lifecycle_owner.release_on_finish
    assert endpoint.lifecycle_owner.handle_path is None
    assert workflow_endpoint_network_policy(endpoint) == {
        "allowed_protocols": ["dns", "tcp"], "allowed_destinations": ["127.0.0.1", "pod123-8000.proxy.runpod.net:443"],
        "allow_redirects": False}


@pytest.mark.parametrize("field", ["operation_id", "pod_id", "provider_account_ref", "network_volume_id"])
def test_changed_original_claim_cannot_park_or_ensure_twice(prepared_endpoint, field):
    p = prepared_endpoint
    p.preparer.finalize_endpoint_readiness(p.reference, **p.kwargs)
    claim = json.loads(p.claim_path.read_bytes())
    claim[field] = "changed"
    write_target_record(p.claim_path, claim)
    with pytest.raises(EndpointOwnershipError):
        p.preparer.finalize_endpoint_readiness(p.reference, **p.kwargs)
    assert p.events.count("ensure") == 1
    assert len(p.calls) == 1


@pytest.mark.parametrize("change", ["hash", "duplicate", "token-name", "token-parent", "extra", "pod", "account", "session-url"])
def test_hashed_profile_and_closed_slot_refuse_before_attachment(prepared_endpoint, change):
    p = prepared_endpoint
    final = p.preparer.finalize_endpoint_readiness(p.reference, **p.kwargs)
    profile = read_workflow_profile(p.profile_path, final.readiness_profile_hash)
    if change == "hash":
        p.profile_path.write_bytes(p.profile_path.read_bytes() + b" ")
        with pytest.raises(AttestationError):
            read_workflow_profile(p.profile_path, final.readiness_profile_hash)
        return
    if change == "duplicate":
        raw = b'{"workflow_endpoint":{},"workflow_endpoint":{}}'
        p.profile_path.write_bytes(raw)
        with pytest.raises(AttestationError):
            read_workflow_profile(p.profile_path, "sha256:" + hashlib.sha256(raw).hexdigest())
        return
    if change == "token-name": profile["workflow_endpoint"]["token_ref"]["path"] += ".other"
    if change == "token-parent": profile["workflow_endpoint"]["token_ref"]["path"] = "/controller/engine-bearer.token"
    if change == "extra": profile["workflow_endpoint"]["url"] = "https://other.invalid"
    if change == "pod": profile["endpoint_evidence"]["claim_attestation"]["pod_id"] = "other"
    if change == "account": profile["endpoint_evidence"]["claim_attestation"]["provider_account_ref"] = "other"
    if change == "session-url": profile["vibecomfy_session"]["server_url"] = "http://127.0.0.1:1"
    with pytest.raises(AttestationError):
        load_workflow_endpoint(profile, profile_path=p.profile_path, execution_request=request())
    assert len(p.calls) == 1


@pytest.mark.parametrize("value", ["", "null", "[]", "{", '{"target":{"kind":"default"},"target":{"kind":"default"}}'])
def test_reserved_request_rejects_invalid_or_duplicate_data(value):
    with pytest.raises(AttestationError):
        read_host_workflow_execution_request({EXECUTION_REQUEST_ENV: value})


def test_observation_denial_has_no_write_or_command_fallback(tmp_path):
    class Denied(LocalTarget):
        async def exec_ssh(self, command, *, timeout):
            raise PermissionError("observation policy")
        async def put_file(self, *a, **kw):
            pytest.fail("denied helper must not attempt an upload")
    transport = RunPodPreparationTransport(Denied())
    path = str(tmp_path / "engine-readiness.json")
    transport.select_endpoint_files(profile_path=path, runtime_root="/installed/runtime", profile_hash="sha256:" + "a" * 64)
    with pytest.raises(WorkerPreparationError, match="observation-only"):
        _run_sync(transport.write_endpoint_profile(path, {"status": "ready"}, deadline=time.monotonic() + 5))

@pytest.fixture(scope="session")
def installed_agent(tmp_path_factory):
    """Install this candidate into a private target; shared interpreter stays read-only."""
    build = tmp_path_factory.mktemp("bridge-installed")
    checkout = Path(__file__).resolve().parents[3]
    source = build / "source"
    # setuptools writes build/ inside its source. Snapshot exactly tracked
    # candidate inputs so concurrent fixture builds never share that directory
    # or accidentally package private pytest scratch from the checkout.
    tracked = subprocess.check_output(["git", "-C", str(checkout), "ls-files", "-z"], timeout=30)
    for name in tracked.decode().split("\0"):
        if not name:
            continue
        selected = checkout / name
        assert selected.resolve().is_relative_to(checkout), "tracked source escapes candidate checkout"
        destination = source / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(selected, destination)
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PIP_CACHE_DIR="/home/user/proof/.pip-cache")
    commands = [
        [sys.executable, "-m", "pip", "wheel", "--no-cache-dir", "--no-deps", "--no-build-isolation",
         "--wheel-dir", str(build / "wheels"), str(source)],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=build, env=environment, capture_output=True, text=True, timeout=90)
        (build / "build.log").write_text(result.stdout + result.stderr)
        assert result.returncode == 0, "candidate wheel build failed; see private build.log"
    wheel = next((build / "wheels").glob("astrid-*.whl"))
    target = build / "installed"
    result = subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "--no-deps",
                             "--no-compile", "--target", str(target), str(wheel)],
                            cwd=build, env=environment, capture_output=True, text=True, timeout=45)
    (build / "install.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, "candidate wheel installation failed; see private install.log"
    return target


_INSTALLED_DRIVER = r'''
import importlib.util,json,os,sys
from pathlib import Path
from astrid.core.execution.generic_host import GenericPackHost
from astrid.packs.vibecomfy.endpoint_checkout import EXECUTION_REQUEST_ENV
configuration=json.loads(Path(sys.argv[1]).read_text())
root=Path(configuration['root'])
profile=Path(configuration['profile'])
os.environ['ASTRID_HOST_READINESS_PROFILE_PATH']=str(profile)
os.environ['ASTRID_HOST_READINESS_PROFILE_HASH']=configuration['hash']
os.environ[EXECUTION_REQUEST_ENV]='ambient-untrusted-value'
host=GenericPackHost(pack_roots=[Path(__import__('astrid').__file__).parent/'packs/vibecomfy'])
host.discover()
record=host.capabilities['vibecomfy.run']
observed={'host_pid':os.getpid(),'host_origin':__import__('astrid.core.execution.generic_host',fromlist=['']).__file__}
validation_env,validation_secrets=host._child_environment(host.capabilities['vibecomfy.validate'],root/'validation-attempt',
 explicit_env={EXECUTION_REQUEST_ENV:'binding-untrusted-value'},workflow_execution_request=configuration['request'])
observed['reserved_absent_for_other_action']=EXECUTION_REQUEST_ENV not in validation_env
validation_env.clear(); validation_secrets.clear()
original=host._child_environment
def child_environment(*args,**kwargs):
 env,secrets=original(*args,**kwargs)
 observed['request_matches']=json.loads(env[EXECUTION_REQUEST_ENV])==configuration['request']
 observed['policy']=json.loads(env['ASTRID_NETWORK_POLICY'])
 observed['network_admission_has_request']='workflow_execution_request' in json.loads(env['ASTRID_NETWORK_ADMISSION'])
 if configuration['mode']=='missing-request': env.pop(EXECUTION_REQUEST_ENV)
 if configuration['mode']=='bad-hash':
  # A test-only mutation after the host check. Main must independently reread.
  profile.write_bytes(profile.read_bytes()+b' ')
 hook=Path(env['PYTHONPATH'].split(os.pathsep)[0])/'sitecustomize.py'
 # Observe the installed action main without changing argv or action dispatch.
 hook.write_text(hook.read_text()+"\nimport sys,json,importlib.util,os\nfrom pathlib import Path\n"
  +"_action_origin=importlib.util.find_spec('astrid.packs.vibecomfy.actions.run.run').origin\n"
  +"import socket\n_external_denied=False\n_probe=socket.socket()\ntry:\n _probe.connect(('203.0.113.1',443))\nexcept __import__('astrid.core.execution.network_policy',fromlist=['']).NetworkPolicyError:\n _external_denied=True\nfinally:\n _probe.close()\n"
  +"def _observe_main(frame,event,arg):\n"
  +" if event=='call' and frame.f_code.co_name=='main' and frame.f_code.co_filename==_action_origin:\n"
  +"  Path("+repr(configuration['child_observation'])+").write_text(json.dumps({'pid':os.getpid(),'action_origin':_action_origin,'agent_origin':__import__('astrid').__file__,'guard_active':bool(__import__('astrid.core.execution.network_policy',fromlist=[''])._POLICY),'external_denied':_external_denied}))\n"
  +"  sys.setprofile(None)\n"
  +"sys.setprofile(_observe_main)\n")
 return env,secrets
host._child_environment=child_environment
admission={'task_id':'task-installed','attempt_id':'attempt-installed','fence':1,
 'execution_identity':'execution-installed','source_digest':record.source_digest,'source_root':str(record.source_root),
 'source_roots':[str(p) for p in __import__('astrid.core.execution.generic_host',fromlist=[''])._admitted_source_roots(record.source_root,record.definition)],
 'workflow_execution_request':configuration['request']}
try:
 host._run_command_definition(record,{'workflow':str(root/'absent-workflow.py')},root/'outputs',root/'attempt',admission=admission)
 observed['outcome']='unexpected-success'
except Exception as error:
 observed['outcome']='refused'
 observed['error_type']=type(error).__name__
Path(configuration['host_observation']).write_text(json.dumps(observed))
'''


@pytest.mark.parametrize("mode", ["explicit-request", "missing-request", "bad-hash"])
def test_installed_host_argv_env_and_separate_action_main_refusal(installed_agent, tmp_path, tmp_path_factory, mode):
    """Installed data wiring/refusal evidence, deliberately not output acceptance."""
    from tests.core.generation.backends.test_checkout_server import _hc03_profile
    from astrid.core.generation.engine_contracts import HealthEvidence, RunPodAttestation
    from dataclasses import asdict

    tmp_path.chmod(0o700)
    profile = _hc03_profile(tmp_path)
    profile["vibecomfy_session"]["server_url"] = "http://127.0.0.1:8188"
    health = HealthEvidence("healthy", "2026-10-27T23:00:00Z", "a" * 64, True)
    profile["endpoint_evidence"] = {"schema": "astrid.runpod.endpoint-evidence.v1", "engine": "vibecomfy-h3",
        "platform": "linux-x86_64-cu130", "claim_operation_id": "original",
        "claim_attestation": asdict(RunPodAttestation("runpod-engine.v1", "account-1", "pod123",
            "/controller/inert-snapshot", "b" * 64, "/installed/.astrid-engine/install.json", "c" * 64,
            health, "a" * 64))}
    path = tmp_path / "engine-readiness.json"
    profile["workflow_endpoint"] = {"endpoint_id": "vibecomfy-h3", "token_ref": {
        "kind": "file", "path": str(path.parent / "engine-bearer.token")}}
    path.write_bytes(encoded(profile))
    path.chmod(0o600)
    configuration = {"root": str(tmp_path), "profile": str(path), "request": request(), "mode": mode,
        "hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        "child_observation": str(tmp_path / "child-observation.json"),
        "host_observation": str(tmp_path / "host-observation.json")}
    config_path = tmp_path / "case.json"
    config_path.write_bytes(encoded(configuration))
    driver = tmp_path / "installed-driver.py"
    driver.write_text(_INSTALLED_DRIVER)
    # Candidate checkout roots are excluded. Runtime/VibeComfy are the declared
    # read-only dependencies; lifecycle uses this candidate's own source.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("ASTRID_")}
    environment.update({"PYTHONPATH": os.pathsep.join((str(installed_agent),
        str(Path(__file__).resolve().parents[4] / "lifecycle/src"), "/home/user/proof/src/astrid-runtime")),
        "PYTHONDONTWRITEBYTECODE": "1", "ASTRID_RUNTIME_CHECKOUT": "/home/user/proof/src/astrid-runtime",
        "ASTRID_VIBECOMFY_CHECKOUT": "/home/user/proof/src/VibeComfy"})
    completed = subprocess.run([sys.executable, str(driver), str(config_path)], cwd=tmp_path,
                               env=environment, capture_output=True, text=True, timeout=90)
    (tmp_path / "installed-driver.log").write_text(completed.stdout + completed.stderr)
    assert completed.returncode == 0, "installed host driver failed; see private installed-driver.log"
    observed = json.loads(Path(configuration["host_observation"]).read_bytes())
    assert observed["outcome"] == "refused"
    assert observed["request_matches"] and not observed["network_admission_has_request"]
    assert observed["reserved_absent_for_other_action"]
    assert observed["policy"] == {"allowed_protocols": ["dns", "tcp"],
        "allowed_destinations": ["127.0.0.1", "pod123-8000.proxy.runpod.net:443"], "allow_redirects": False}
    child = json.loads(Path(configuration["child_observation"]).read_bytes())
    assert child["pid"] != observed["host_pid"] and child["guard_active"] and child["external_denied"]
    assert Path(observed["host_origin"]).is_relative_to(installed_agent)
    assert Path(child["action_origin"]).is_relative_to(installed_agent)
    assert Path(child["agent_origin"]).is_relative_to(installed_agent)
    retained = tmp_path_factory.getbasetemp()
    (retained / ("installed-observations-" + mode + ".json")).write_bytes(encoded({
        "mode": mode, "host": observed, "child": child,
    }))
    (retained / ("installed-driver-" + mode + ".log")).write_text(completed.stdout + completed.stderr)


def test_attached_checkout_constructor_failure_closes_only_transport(prepared_endpoint, monkeypatch):
    p = prepared_endpoint
    final = p.preparer.finalize_endpoint_readiness(p.reference, **p.kwargs)
    profile = read_workflow_profile(p.profile_path, final.readiness_profile_hash)
    checkout = checkout_from_profile(
        profile_path=p.profile_path, profile_hash=final.readiness_profile_hash, execution_request=request(),
        task_identity="task", attempt_identity="attempt", execution_identity="execution",
    )
    closed = []
    original = EngineEndpointTransport.close
    def close(attachment):
        closed.append(attachment)
        return original(attachment)
    monkeypatch.setattr(EngineEndpointTransport, "close", close)
    # The real adapter refuses the deliberately incomplete ordinary ready facts.
    # Its constructor failure must close the attachment without any provider or
    # installed-session stop call. No successful output is asserted here.
    with pytest.raises(ValueError, match="verified facts"):
        with checkout(hc03_profile=profile, model_id="model", template_id="template",
                      invocation_identity="task", expected_comfyui_version=None):
            pytest.fail("invalid adapter construction succeeded")
    assert len(closed) == 1 and closed[0]._server is None
    assert json.loads(p.claim_path.read_bytes())["operation_id"] == "original"
    assert len(p.calls) == 1 and len(p.started.read_text().splitlines()) == 1


def test_claim_change_before_finalization_never_attaches_or_probes(prepared_endpoint):
    p = prepared_endpoint
    claim = json.loads(p.claim_path.read_bytes())
    claim["operation_id"] = "changed"
    write_target_record(p.claim_path, claim)
    with pytest.raises(EndpointOwnershipError):
        p.preparer.finalize_endpoint_readiness(p.reference, **p.kwargs)
    assert p.events == [] and p.calls == []


def test_existing_endpoint_evidence_and_snapshot_are_retained(prepared_endpoint):
    p = prepared_endpoint
    root = p.registry.parents[3]
    receipt_path = root / ".astrid-engine/install.json"
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    snapshot = p.claim_path.with_name("provided-claim-snapshot.json")
    write_target_record(snapshot, p.claim)
    evidence = {
        "schema": "astrid.runpod.endpoint-evidence.v1", "engine": "vibecomfy-h3", "platform": p.recipe.platform,
        "claim_operation_id": "original", "claim_attestation": {
            "kind": "runpod-engine.v1", "provider_account_ref": "account-1", "pod_id": "pod123",
            "claim_handle_path": str(snapshot), "claim_handle_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            "install_receipt_path": str(receipt_path), "install_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "health": receipt["health"], "object_info_sha256": receipt["health"]["object_info_sha256"],
        },
    }
    profile = {**p.base, "endpoint_evidence": evidence}
    p.profile_path.write_bytes(encoded(profile))
    reference = replace(p.reference, readiness_profile_hash="sha256:" + hashlib.sha256(encoded(profile)).hexdigest())
    final = p.preparer.finalize_endpoint_readiness(reference, **p.kwargs)
    assert read_workflow_profile(p.profile_path, final.readiness_profile_hash)["endpoint_evidence"] == evidence
    assert not list(p.preparer.journal_dir.glob("endpoint-claim-*"))
    assert len(p.calls) == 1 and p.events == ["stage", "attach", "ensure", "write-final"]


def test_new_generation_cannot_select_legacy_session_start(tmp_path):
    from tests.packs.runpod.test_prepared_task import _production_runtime
    from astrid.packs.runpod.prepared_task import PreparedWorkerError

    runtime, events = _production_runtime(tmp_path, {"state": "stopped"})
    components = runtime._components({})
    components[0].session_ref = "legacy-session"
    runtime._components = lambda task: components
    with pytest.raises(PreparedWorkerError, match="original installed"):
        runtime.ensure_for_task({"task_id": "task-1", "state": "queued"})
    assert "park" not in events and "activate" not in events
