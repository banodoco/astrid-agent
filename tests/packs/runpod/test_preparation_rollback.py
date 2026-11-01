"""Published preparation rollback with real journal, HTTPS agent and CPU processes.

Only provider HTTP is simulated. The tiny ComfyUI main.py is the GPU boundary;
the canonical recipe, source pin, native supervisor and stop path execute as-is.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import requests
from runpod_lifecycle.pod_agent import AgentServer

from astrid.core.generation import engine_custody
from astrid.core.generation.engine_platforms import LinuxPlatformAdapter
from astrid.core.generation.engine_recipe import load_recipe
from astrid.packs.runpod.preparation_custody import JournalPreparationOps
from astrid.packs.runpod.prepared_task import (
    PreparedRunPodTaskClient,
    PreparedWorkerError,
    release_preparation,
)
from astrid.packs.runpod.remote_placement import _HttpsOps
from astrid.packs.runpod.worker_preparation import (
    AcquisitionUnresolvedError,
    TargetObservationError,
    _validate_release_quiescence,
    target_owner,
    unactivated_target_quiescence,
    write_target_record,
)

pytestmark = pytest.mark.linux_native


def _read(path):
    return json.loads(path.read_text())


def _wait(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "CPU preparation fixture did not settle"
        time.sleep(.02)


def _live(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] not in {"Z", "X"}
    except FileNotFoundError:
        return False


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    mount = tmp_path / "volume"
    mount.mkdir()
    sentinel = mount / "preserved"
    sentinel.write_text("persistent volume data")
    operation = uuid.uuid4().hex
    name = "astrid-product-" + operation
    path = tmp_path / "claim.json"
    claim = {
        "schema_version": "astrid.runpod.claim.v1", "state": "claimed",
        "operation_id": operation, "pod_id": "podexact", "name": name, "request_name": name,
        "provider_account_ref": "test-account", "acquisition_mode": "allocate",
        "target_identity_verified": True, "release_authorized": True,
        "gpu_type": "test-gpu", "worker_image": "worker@sha256:" + "a" * 64,
        "network_volume_id": "volume-exact", "network_volume_name": "persistent",
        "network_volume_size_gb": 100, "volume_mount_path": str(mount),
        "ordinary_preparation": {
            "schema": "astrid.runpod.ordinary-preparation.v1", "state": "preparing",
            "claim_operation_id": operation, "pod_id": "podexact", "provider_account_ref": "test-account",
        },
    }
    write_target_record(path, claim)  # R02 has published its exact, verified claim.
    server = AgentServer(("127.0.0.1", 0), "test-token-" + "x" * 40, tmp_path / "agent")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    ops = _HttpsOps("podexact", server.token, base_url=base_url, job_root=str(mount / "jobs"))
    with target_owner(path):
        journal = JournalPreparationOps.begin(path, ops, engine_root=str(mount / "engine"), python=sys.executable)
    state = SimpleNamespace(path=path, ops=ops, journal=journal, mount=mount, sentinel=sentinel,
                            present=True, failure=None, requests=[], deletes=0, base_url=base_url,
                            config=SimpleNamespace(api_key="dummy-provider-http-only"))
    original_send = httpx.Client.send

    def provider_send(client, request, **kwargs):
        if request.url.host in {"127.0.0.1", "localhost"}:
            if request.method == "POST" and request.url.path == "/jobs":
                command = json.loads(request.content)["command"]
                if "base64 -d" in command:  # detached launch, including a lost launch ACK
                    saved = _read(path)
                    assert saved["preparation_custody"]["jobs"]
                    assert saved["preparation_custody"]["jobs"][-1]["job_id"] in command
                    assert saved["preparation_custody"]["supervisor"]["owner_id"]
            return original_send(client, request, **kwargs)
        assert request.url.host in {"rest.runpod.io", "api.runpod.io"}, str(request.url)
        state.requests.append((request.method, request.url.path))
        saved = _read(path)
        assert saved["state"] in {"release_pending", "cleanup_pending"}
        with pytest.raises(AcquisitionUnresolvedError):
            with target_owner(path):
                pytest.fail("release lost P1's owner lock")
        if request.method == "DELETE":
            assert request.url.path == "/v1/pods/podexact"
            _validate_release_quiescence(saved["release_quiescence"], saved)
            state.deletes += 1
            state.present = False
            if state.failure == "delete_reply":
                return httpx.Response(503, request=request)
            return httpx.Response(204, request=request)
        if request.url.path == "/v1/networkvolumes":
            size = 101 if not state.present and state.failure == "volume" else 100
            value = [{"id": "volume-exact", "name": "persistent", "size": size}]
        elif request.url.path == "/v1/pods":
            if not state.present and state.failure == "list":
                return httpx.Response(503, request=request)
            value = [{"id": "podexact", "name": name, "desiredStatus": "RUNNING",
                      "machineType": "test-gpu", "imageName": claim["worker_image"],
                      "networkVolumeId": "volume-exact"}] if state.present else []
        elif request.url.path == "/v1/pods/podexact":
            if state.failure == "get":
                return httpx.Response(503, request=request)
            if state.failure == "null":
                return httpx.Response(200, json=None, request=request)
            return httpx.Response(404 if not state.present else 200,
                                  json=None if not state.present else {"id": "podexact"}, request=request)
        else:
            pytest.fail("unexpected provider HTTP request " + str(request.url))
        return httpx.Response(200, json=value, request=request)

    def sdk_http(session, request, **kwargs):
        assert request.url.startswith("https://api.runpod.io/")
        # Exercise lifecycle's actual SDK-error -> independent REST fallback.
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"errors":[{"message":"HTTP fixture: use REST inventory"}]}'
        return response

    monkeypatch.setattr(httpx.Client, "send", provider_send)
    monkeypatch.setattr(requests.Session, "send", sdk_http)
    try:
        yield state
    finally:
        custody = _read(path)["preparation_custody"]
        engine_custody.LinuxEngineServiceClient(**custody["supervisor"]["identity"],
            owner_id=custody["supervisor"]["owner_id"]).stop_owned(deadline=time.monotonic() + 10)
        server.shutdown()
        server.server_close()
        thread.join(2)


def _job(p, command):
    with target_owner(p.path):
        started = p.journal.job_start(command)
        _wait(lambda: p.ops.job_poll(started["id"])["state"] == "exited")
        result = p.ops.job_poll(started["id"])
        stderr = Path(started["dir"], "stderr").read_text()
        return result["exit_code"], stderr


def _reserve(p):
    code, stderr = _job(p, p.journal.supervisor_command("reserve"))
    assert code == 0, stderr


def _failed(p):
    current = _read(p.path)
    write_target_record(p.path, {**current, "state": "cleanup_pending", "reconciliation_required": True,
        "ordinary_preparation": {**current["ordinary_preparation"], "state": "unresolved"}})


def _release(p, **kwargs):
    return release_preparation(p.config, p.path, ops=p.ops, resume=True, verify_timeout_seconds=0, **kwargs)


def _assert_proof(p, result):
    assert result["state"] == "cleanup_complete"
    assert result["cleanup_status"] == "complete"
    assert result["reconciliation_required"] is False
    assert result["cleanup"]["absence"] == {
        "provider_account_ref": "test-account", "pod_id": "podexact", "exact_get_status": 404,
        "account_list_succeeded": True, "account_list_absent": True,
    }
    assert result["cleanup"]["backup_volume_preserved"] is True
    assert p.sentinel.read_text() == "persistent volume data"
    assert result["release_quiescence"]["schema"] == "astrid.runpod.preparation-quiescence.v1"
    assert "managed_worker" not in result and "activation_id" not in result
    assert p.deletes == 1
    _validate_release_quiescence(result["release_quiescence"], result)


def test_failed_install_after_claim_publication_has_exact_release_proof(prepared):
    p = prepared
    _reserve(p)
    assert _job(p, shlex.join([sys.executable, "-c", "raise SystemExit(3)"]))[0] == 3
    _failed(p)
    result = _release(p)
    _assert_proof(p, result)
    assert len(result["release_quiescence"]["jobs"]) == 2
    # Completed recovery is provider/transport free and returns the same receipt.
    p.requests.clear()
    assert _release(p) == result
    assert p.requests == []


def _start_cpu_engine(p):
    """Real canonical launch against a tiny CPU ComfyUI source fixture."""
    custody = _read(p.path)["preparation_custody"]
    identity = custody["supervisor"]["identity"]
    root = Path(identity["root"])
    comfy = root / "runtime/ComfyUI"
    comfy.mkdir(parents=True)
    (comfy / "main.py").write_text('''
import json, os, sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
Path('cpu-engine.pid').write_text(str(os.getpid()))
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  value={'system': {'comfyui_version': 'cpu-fixture'}} if self.path=='/system_stats' else {'MiniMaxH3ReferenceToVideo': {}, 'VHS_VideoCombine': {}}
  body=json.dumps(value).encode()
  self.send_response(200); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
HTTPServer(('127.0.0.1',int(sys.argv[sys.argv.index('--port')+1])),Handler).serve_forever()
''')
    python = root / "runtime/venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    # Keep the recipe's real b2123edf source revision and effective-source hash.
    source = Path(os.environ["ASTRID_VIBECOMFY_CHECKOUT"])
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip() == "b2123edfbe7b950848b5c98d575fd9554302fc73"
    (root / "runtime/vibecomfy").symlink_to(source, target_is_directory=True)
    recipe = load_recipe(identity["engine"], identity["platform"])
    launch = LinuxPlatformAdapter(identity["platform"]).start_request(recipe, str(root), deadline=time.monotonic() + 5)
    payload = {**identity, "schema": "astrid.engine-service.v1", "owner_id": None,
               "launch": {"argv": list(launch.argv), "cwd": launch.cwd, "env": dict(launch.env)},
               "timeout_seconds": recipe.health.timeout_seconds,
               "poll_interval_seconds": recipe.health.poll_interval_seconds}
    program = f"import time; from astrid.core.generation.engine_custody import LinuxEngineServiceClient; LinuxEngineServiceClient(**{identity!r}).call('start', {payload!r}, deadline=time.monotonic()+25); raise SystemExit(7)"
    code, stderr = _job(p, shlex.join([sys.executable, "-c", program]))
    assert code == 7, stderr
    marker = _read(root / "runtime/out/sessions/vibecomfy-h3/launch.json")
    assert _live(marker["pid"]) and _live(marker["comfy_pid"])
    return marker


def test_failure_after_engine_start_stops_original_supervisor_before_delete(prepared):
    p = prepared
    _reserve(p)
    marker = _start_cpu_engine(p)
    _failed(p)
    result = _release(p)
    assert not _live(marker["pid"]) and not _live(marker["comfy_pid"])
    _assert_proof(p, result)
    assert result["release_quiescence"]["supervisor_stop"]["owner_id"] == _read(p.path)["preparation_custody"]["supervisor"]["owner_id"]


def test_restart_mid_rollback_reuses_same_jobs_and_original_supervisor(prepared, tmp_path):
    p = prepared
    _reserve(p)
    marker = _start_cpu_engine(p)
    _failed(p)
    paused = tmp_path / "rollback-before-supervisor-stop"
    # Block only at the HTTP send boundary, after job-stop checkpoints. Kill
    # the actual controller, then recover with freshly constructed owners.
    program = f'''
import json,time,httpx
from pathlib import Path
from types import SimpleNamespace
from astrid.packs.runpod.prepared_task import release_preparation
from astrid.packs.runpod.remote_placement import _HttpsOps
original=httpx.Client.send
def send(self,request,**kwargs):
 if request.url.host!='127.0.0.1': raise AssertionError('unexpected provider before controller restart')
 if request.method=='POST' and request.url.path=='/jobs':
  command=json.loads(request.content)['command']
  if 'control_preparation_supervisor' in command and "stop" in command:
   Path({str(paused)!r}).write_text('paused')
   while True: time.sleep(.05)
 return original(self,request,**kwargs)
httpx.Client.send=send
ops=_HttpsOps('podexact',{'test-token-' + 'x' * 40!r},base_url={p.base_url!r},job_root={p.ops.job_root!r})
release_preparation(SimpleNamespace(api_key='dummy'),{str(p.path)!r},ops=ops,resume=True,verify_timeout_seconds=0)
'''
    child = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _wait(paused.exists)
        checkpoint = _read(p.path)
        assert checkpoint["release_progress"]["preparation"]["jobs"]
        assert "release_quiescence" not in checkpoint
        assert _live(marker["comfy_pid"])
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
        _, stderr = child.communicate(timeout=5)
    assert child.returncode == -9, stderr.decode()
    p.ops = _HttpsOps("podexact", "test-token-" + "x" * 40, base_url=p.base_url, job_root=p.ops.job_root)
    result = _release(p)
    _assert_proof(p, result)
    assert not _live(marker["pid"]) and not _live(marker["comfy_pid"])


@pytest.mark.parametrize("failure", ["list", "get", "null", "volume"])
def test_failed_independent_absence_or_volume_observation_never_claims_release(prepared, failure):
    p = prepared
    _reserve(p)
    _failed(p)
    p.failure = failure
    with pytest.raises(Exception):
        _release(p)
    current = _read(p.path)
    assert current["state"] == "cleanup_pending"
    assert current["reconciliation_required"] is True
    assert current.get("cleanup_status") != "complete"
    assert current["release_quiescence"]["supervisor_stop"]["stopped"] is True
    assert p.sentinel.read_text() == "persistent volume data"
    p.failure = None
    _assert_proof(p, _release(p))


@pytest.mark.parametrize("change", [
    {"preparation_custody": None}, {"managed_worker": {"state": "parked"}},
    {"managed_session": {}}, {"task_id": "task"}, {"activation_id": "activation"},
    {"worker_activation": "started"}, {"acquisition_mode": "attach"},
    {"target_identity_verified": False}, {"operation_id": "foreign"},
])
def test_published_claims_without_exclusive_preparation_custody_are_refused(prepared, change):
    p = prepared
    original = _read(p.path)
    current = {**original, **change}
    write_target_record(p.path, current)
    with pytest.raises(TargetObservationError):
        release_preparation(p.config, p.path, ops=p.ops)
    assert p.requests == []
    # Restore only for the real supervisor fixture teardown.
    write_target_record(p.path, original)


def test_admission_checkpoint_excludes_preparation_rollback_even_if_ack_is_lost(prepared):
    p = prepared
    current = _read(p.path)
    current["preparation_custody"]["state"] = "prepared"
    write_target_record(p.path, current)
    class Client:
        def invoke_result(self, *args, **kwargs):
            with pytest.raises(AcquisitionUnresolvedError):
                with target_owner(p.path):
                    pytest.fail("admission lost the claim lock")
            assert _read(p.path)["preparation_custody"]["admission"] == {"state": "requested"}
            raise RuntimeError("lost Runtime admission acknowledgement")
    bound = PreparedRunPodTaskClient(Client(), object(), claim_handle_path=p.path)
    with pytest.raises(RuntimeError, match="lost Runtime"):
        bound.invoke_result("vibecomfy.run", kind="executor", recovery_path="receipt",
                            execution_request={"remote_activation_required": True})
    with pytest.raises(TargetObservationError, match="never-admitted"):
        release_preparation(p.config, p.path, ops=p.ops)
    assert p.requests == []


def test_release_fences_admission_and_job_launch(prepared):
    p = prepared
    _failed(p)
    bound = PreparedRunPodTaskClient(object(), object(), claim_handle_path=p.path)
    with pytest.raises(PreparedWorkerError, match="excluded"):
        bound.invoke_result("vibecomfy.run", kind="executor", recovery_path="receipt",
                            execution_request={"remote_activation_required": True})
    with target_owner(p.path), pytest.raises(TargetObservationError, match="excluded"):
        p.journal.job_start("echo should-never-run")
    assert p.requests == []


def test_lost_launch_ack_still_cancels_the_write_ahead_job(prepared, monkeypatch):
    p = prepared
    _reserve(p)
    original_send = httpx.Client.send

    def lose_ack(client, request, **kwargs):
        response = original_send(client, request, **kwargs)
        if request.method == "POST" and request.url.path == "/jobs":
            command = json.loads(request.content)["command"]
            if "base64 -d" in command:
                raise RuntimeError("controller lost the detached launch acknowledgement")
        return response

    with monkeypatch.context() as fault:
        fault.setattr(httpx.Client, "send", lose_ack)
        with target_owner(p.path), pytest.raises(RuntimeError, match="lost the detached"):
            p.journal.job_start("sleep 60")
    job_id = _read(p.path)["preparation_custody"]["jobs"][-1]["job_id"]
    _wait(lambda: Path(p.ops.job_root, job_id, "pid").exists())
    _failed(p)
    result = _release(p)
    _assert_proof(p, result)
    assert result["release_quiescence"]["jobs"][-1]["job_id"] == job_id


def test_cancel_ack_without_positive_group_stop_keeps_cleanup_pending(prepared, monkeypatch):
    p = prepared
    _reserve(p)
    with target_owner(p.path):
        started = p.journal.job_start("sleep 60")
    _wait(lambda: Path(started["dir"], "pid").exists())
    _failed(p)
    original_send = httpx.Client.send

    def false_cancel(client, request, **kwargs):
        if request.method == "POST" and request.url.path == "/jobs":
            body = json.loads(request.content)
            if "kill -TERM --" in body["command"]:
                headers = {key: value for key, value in request.headers.items() if key != "content-length"}
                request = httpx.Request(request.method, request.url, headers=headers,
                                        json={**body, "command": "true"})
        return original_send(client, request, **kwargs)

    try:
        with monkeypatch.context() as fault:
            fault.setattr(httpx.Client, "send", false_cancel)
            with pytest.raises(TargetObservationError, match="job stop remains uncertain"):
                _release(p)
        current = _read(p.path)
        assert current["state"] == "cleanup_pending"
        assert current["reconciliation_required"] is True
        assert "release_quiescence" not in current
        assert p.requests == []
        _assert_proof(p, _release(p))
    finally:
        p.ops.job_cancel(started["id"])


def test_missing_job_observation_is_not_an_unactivated_proof(prepared):
    p = prepared
    # A launch intent whose outcome was lost cannot be called unstarted.
    current = _read(p.path)
    current["preparation_custody"]["jobs"].append({
        "job_id": uuid.uuid4().hex, "command_sha256": "sha256:" + "b" * 64,
    })
    write_target_record(p.path, current)
    _failed(p)
    with pytest.raises(TargetObservationError, match="job stop remains uncertain"):
        _release(p)
    assert _read(p.path)["state"] == "cleanup_pending"
    assert p.requests == []


def test_lost_delete_reply_recovers_from_durable_proof_without_remote_transport(prepared):
    p = prepared
    _reserve(p)
    _failed(p)
    p.failure = "delete_reply"
    with pytest.raises(RuntimeError, match="termination failed"):
        _release(p)
    checkpoint = _read(p.path)
    assert checkpoint["state"] == "cleanup_pending"
    assert checkpoint["release_quiescence"]["supervisor_stop"]["stopped"] is True
    p.failure = None
    # No token reference or remote ops: durable proof is the sole recovery input.
    result = release_preparation(p.config, p.path, resume=True, verify_timeout_seconds=0)
    _assert_proof(p, result)


def test_rollback_never_stops_a_different_supervisor_owner(prepared):
    p = prepared
    _reserve(p)
    original = _read(p.path)["preparation_custody"]["supervisor"]
    marker = Path(original["identity"]["root"], ".astrid-engine/session/session.json")
    assert _read(marker)["owner_id"] == original["owner_id"]
    current = _read(p.path)
    current["preparation_custody"]["supervisor"] = {**original, "owner_id": uuid.uuid4().hex}
    write_target_record(p.path, current)
    _failed(p)
    try:
        with pytest.raises(TargetObservationError, match="supervisor stop remains unresolved"):
            _release(p)
        assert _read(marker)["owner_id"] == original["owner_id"]
        assert p.requests == []
        assert "release_quiescence" not in _read(p.path)
    finally:
        current = _read(p.path)
        current["preparation_custody"]["supervisor"] = original
        write_target_record(p.path, current)
    _assert_proof(p, _release(p))


@pytest.mark.parametrize("field", ["jobs", "supervisor", "identity", "operation", "progress", "boolean"])
def test_completed_preparation_proof_cannot_be_rebound_or_weakened(prepared, field):
    p = prepared
    _reserve(p)
    _failed(p)
    result = _release(p)
    proof = result["release_quiescence"]
    if field == "jobs":
        proof["jobs"] = []
        result["release_progress"]["preparation"]["jobs"] = []
    elif field == "supervisor":
        proof["supervisor_stop"]["owner_id"] = uuid.uuid4().hex
    elif field == "identity":
        proof["pod_id"] = "foreign-pod"
    elif field == "operation":
        result["ordinary_preparation"]["claim_operation_id"] = "foreign"
    elif field == "progress":
        result["release_progress"] = {}
    else:
        proof["supervisor_stop"]["stopped"] = 1
    with pytest.raises(TargetObservationError):
        _validate_release_quiescence(proof, result)


def test_old_unactivated_proof_still_refuses_every_published_claim(prepared):
    with pytest.raises(TargetObservationError, match="unactivated allocation custody"):
        unactivated_target_quiescence(_read(prepared.path))
