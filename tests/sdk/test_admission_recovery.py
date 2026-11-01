"""Exact receipt fault injection against current local Runtime + generated HTTP."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers.runtime import executor_token, use_runtime

# Reuse the current Runtime; never bootstrap providers.
pytestmark = pytest.mark.integration  # needs the Runtime from a second checkout (real_runtime fixture)
_runtime = use_runtime()
import runtime_protocol.daemon as _runtime_daemon  # noqa: E402

assert Path(_runtime_daemon.__file__).resolve().parents[1] == _runtime

# Load the current Runtime client package explicitly, so its relative imports resolve.
# Other collected SDK tests may already have imported Astrid's bundled package under its normal name.
_client_dir = _runtime / "packages/python/astrid_runtime_client"
_generated_path = _client_dir / "generated.py"
_spec = importlib.util.spec_from_file_location(
    "_admission_recovery_runtime_client", _client_dir / "__init__.py",
    submodule_search_locations=[str(_client_dir)],
)
_client_package = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _client_package
_spec.loader.exec_module(_client_package)
_current_generated = importlib.import_module(f"{_spec.name}.generated")

from astrid.sdk import invocation
from astrid.sdk.recovery import AdmissionReceipt
from astrid.sdk.remote import RemoteTasks
from astrid.sdk.workspace_client import WorkspaceClient
from astrid.sdk.exceptions import CapabilityInvocationError, CapabilityValidationError

CAPABILITY = "fixture.recovery"
DIGEST = "sha256:" + "a" * 64
REQUEST = {"stage": "compile", "project": "demo", "input": ["first", "second"], "submission_nonce": "operation-1"}
METADATA = {"capability_id": CAPABILITY, "capability_type": "executor", "native_kind": "executor", "read_managed_outputs": False}


def _connect(daemon):
    transport = WorkspaceClient(daemon.endpoint, daemon.token)
    transport._generated = _current_generated.WorkspaceClient(daemon.endpoint, daemon.token)
    assert Path(sys.modules[type(transport._generated).__module__].__file__) == _generated_path
    transport.handshake("receipt-test", "1", ["projects:read", "projects:write", "tasks:read", "tasks:write"])
    return SimpleNamespace(tasks=RemoteTasks(transport), _transport=transport)


def _worker(daemon, executor_id):
    token = executor_token(daemon, executor_id)
    worker = WorkspaceClient(daemon.endpoint, token)
    worker._generated = _current_generated.WorkspaceClient(daemon.endpoint, token)
    return worker


@pytest.fixture
def runtime(tmp_path, real_runtime):
    root = tmp_path / "realm"
    daemon = real_runtime(root)
    try:
        daemon.service.register_capability({"capability_id": CAPABILITY, "definition_digest": DIGEST, "status": "ready"})
        client = _connect(daemon)
        project = client._transport.create_project("Demo", slug="demo", idempotency_key="project-demo")["data"]["project_id"]
        yield SimpleNamespace(daemon=daemon, client=client, project=project, root=root, start=real_runtime)
    finally:
        daemon.stop()


def _prepare(rt, *, key="frozen-admission", spec=None, execution_request=None):
    prepared = rt.client.tasks.prepare_admission(project_id=rt.project, capability=CAPABILITY,
        spec=spec or {"message": "hello"}, idempotency_key=key, execution_request=execution_request)
    assert prepared.ok
    return prepared.data


def _count(rt):
    return rt.daemon.service.store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def _receipt(rt, path, *, resume=False, request=REQUEST, client=None):
    return AdmissionReceipt(path, request=request, client=client or rt.client, resume=resume)


@pytest.mark.parametrize("failure", ["write", "file_fsync", "directory_fsync"])
def test_persistence_failure_dispatches_nothing(runtime, tmp_path, monkeypatch, failure):
    path = tmp_path / "stage.json"
    with _receipt(runtime, path) as receipt:
        # Restore the process-wide fsync before the Runtime fixture closes its realm.
        with monkeypatch.context() as patch:
            if failure == "write":
                patch.setattr(receipt, "_persist", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
            else:
                fsync = os.fsync
                calls = []
                def fail(fd):
                    calls.append(fd)
                    if failure == "file_fsync" or len(calls) == 2:
                        raise OSError("disk full")
                    fsync(fd)
                patch.setattr(os, "fsync", fail)
            with pytest.raises(OSError, match="disk full"):
                receipt.freeze(_prepare(runtime), metadata=METADATA)
                runtime.client.tasks.dispatch_admission(receipt.data["admission"])
    assert _count(runtime) == 0


def test_restart_before_send_preserves_exact_arguments(runtime, tmp_path, monkeypatch):
    path = tmp_path / "stage.json"
    frozen = _prepare(runtime, spec={"ordered": ["second", "first"], "unicode": "é"})
    with _receipt(runtime, path) as receipt:
        receipt.freeze(frozen, metadata=METADATA)
    assert path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(runtime.client.tasks, "prepare_admission", lambda **_: pytest.fail("must not rebuild"))
    with _receipt(runtime, path, resume=True) as receipt:
        assert receipt.data["admission"] == frozen
        result = receipt.recover_task()
        assert result.ok
        assert result.data["attempt_id"] is None
        assert receipt.data["locator"] == {"task_id": result.data["task_id"], "run_id": result.data["run_id"]}
    assert _count(runtime) == 1


@pytest.mark.parametrize("loss", ["http_response", "locator_write"])
def test_commit_then_response_or_locator_loss_recovers_same_task(runtime, tmp_path, monkeypatch, loss):
    path = tmp_path / "stage.json"
    frozen = _prepare(runtime)
    with _receipt(runtime, path) as receipt:
        receipt.freeze(frozen, metadata=METADATA)
        if loss == "http_response":
            request = runtime.client._transport._generated._request
            def dropped(method, url, **kwargs):
                response = request(method, url, **kwargs)
                if method == "POST" and url == "/v1/tasks":
                    raise ConnectionResetError("response lost after Runtime commit")
                return response
            with monkeypatch.context() as m:
                m.setattr(runtime.client._transport._generated, "_request", dropped)
                result = runtime.client.tasks.dispatch_admission(frozen)
                assert not result.ok and result.error.code == "transport_error"
        else:
            result = runtime.client.tasks.dispatch_admission(frozen)
            assert result.ok
            with monkeypatch.context() as m:
                m.setattr(receipt, "_persist", lambda *_: (_ for _ in ()).throw(OSError("locator lost")))
                with pytest.raises(OSError, match="locator lost"):
                    receipt.locate(result.data)
    first = runtime.daemon.service.store.conn.execute("SELECT id, run_id FROM tasks").fetchone()
    with _receipt(runtime, path, resume=True) as receipt:
        recovered = receipt.recover_task()
        assert recovered.ok
        assert recovered.data["task_id"] == first[0]
        assert recovered.data["run_id"] == first[1]
    assert _count(runtime) == 1
    assert runtime.daemon.service.store.conn.execute("SELECT COUNT(*) FROM command_idempotency WHERE command_kind='task.create'").fetchone()[0] == 1


def test_same_path_writer_excluded_without_overwrite(runtime, tmp_path):
    path = tmp_path / "stage.json"
    with _receipt(runtime, path) as receipt:
        receipt.freeze(_prepare(runtime), metadata=METADATA)
        original = path.read_bytes()
        with ThreadPoolExecutor() as executor:
            def concurrent():
                with _receipt(runtime, path, resume=True):
                    pytest.fail("second writer acquired lock")
            future = executor.submit(concurrent)
            with pytest.raises(CapabilityInvocationError, match="already in use"):
                future.result()
        assert path.read_bytes() == original
    with pytest.raises(CapabilityValidationError, match="already exists"):
        with _receipt(runtime, path):
            pass
    assert _count(runtime) == 0


@pytest.mark.parametrize("changed_field", ["spec", "settlement_effect"])
def test_real_http_idempotency_race_converges_and_conflicts(runtime, changed_field):
    frozen = _prepare(runtime)
    def dispatch(_):
        client = _connect(runtime.daemon)
        return client.tasks.dispatch_admission(frozen)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(dispatch, range(2)))
    assert all(result.ok for result in results)
    assert results[0].data == results[1].data
    assert _count(runtime) == 1
    changed = copy.deepcopy(frozen)
    if changed_field == "spec":
        changed["spec"]["message"] = "different"
    else:
        changed["settlement_effect"] = {"effect_type": "fixture.effect", "value": "different"}
    conflict = runtime.client.tasks.dispatch_admission(changed)
    assert not conflict.ok and conflict.error.code == "conflict"
    assert _count(runtime) == 1


@pytest.mark.parametrize("known_locator", [False, True])
def test_committed_recovery_ignores_catalog_changes_but_fresh_readiness_remains(runtime, tmp_path, monkeypatch, known_locator):
    path = tmp_path / "stage.json"
    frozen = _prepare(runtime)
    with _receipt(runtime, path) as receipt:
        receipt.freeze(frozen, metadata=METADATA)
        admitted = runtime.client.tasks.dispatch_admission(frozen)
        assert admitted.ok
        if known_locator:
            receipt.locate(admitted.data)
    runtime.daemon.service.store.conn.execute("UPDATE capabilities SET status='unavailable', definition_digest=? WHERE id=?", ("sha256:" + "b" * 64, CAPABILITY))
    runtime.daemon.service.store.conn.commit()
    monkeypatch.setattr(runtime.client.tasks, "prepare_admission", lambda **_: pytest.fail("must not prepare"))
    monkeypatch.setattr(runtime.client._transport, "list_capabilities", lambda **_: pytest.fail("must not select"))
    with _receipt(runtime, path, resume=True) as receipt:
        recovered = receipt.recover_task()
        assert recovered.ok and recovered.data["task_id"] == admitted.data["task_id"]
    fresh = copy.deepcopy(frozen)
    fresh["idempotency_key"] = "explicit-fresh"
    fresh["capability_digest"] = "sha256:" + "b" * 64
    rejected = runtime.client.tasks.dispatch_admission(fresh)
    assert not rejected.ok and rejected.error.code == "unavailable"
    assert _count(runtime) == 1


@pytest.mark.parametrize("mismatch", ["project", "input", "submission_nonce", "corrupt", "realm"])
def test_receipt_identity_mismatch_fails_without_admission(runtime, tmp_path, monkeypatch, mismatch):
    path = tmp_path / "stage.json"
    with _receipt(runtime, path) as receipt:
        receipt.freeze(_prepare(runtime), metadata=METADATA)
    changed = dict(REQUEST)
    if mismatch == "corrupt":
        content = json.loads(path.read_text())
        content["admission"]["spec"]["message"] = "tampered"
        path.write_text(json.dumps(content))
    elif mismatch == "realm":
        original = runtime.client._transport.handshake
        def other(*args):
            return {**original(*args), "realm_id": "another-realm"}
        monkeypatch.setattr(runtime.client._transport, "handshake", other)
    else:
        changed[mismatch] = "another"
    with pytest.raises(CapabilityValidationError, match="corrupt|identity mismatch"):
        with _receipt(runtime, path, resume=True, request=changed):
            pass
    assert _count(runtime) == 0


@pytest.mark.parametrize("known_locator", [False, True])
def test_revoked_authority_is_not_bypassed(runtime, tmp_path, known_locator):
    path = tmp_path / "stage.json"
    with _receipt(runtime, path) as receipt:
        receipt.freeze(_prepare(runtime), metadata=METADATA)
        admitted = runtime.client.tasks.dispatch_admission(receipt.data["admission"])
        assert admitted.ok
        if known_locator:
            receipt.locate(admitted.data)
    actor = runtime.client._transport._last_handshake["actor_id"]
    runtime.daemon.credentials.disable_actor(actor)
    with pytest.raises(Exception, match="disabled|credential|authorization|authorized"):
        with _receipt(runtime, path, resume=True) as receipt:
            receipt.recover_task()
    assert _count(runtime) == 1


@pytest.mark.parametrize("targeted", [False, True])
def test_ordinary_runtime_restart_keeps_realm_and_task_identity(runtime, tmp_path, targeted):
    path = tmp_path / "stage.json"
    execution_request = {"target": {"kind": "profile", "id": "chosen-profile", "profile_revision": "frozen-v1", "profile_digest": DIGEST}} if targeted else None
    with _receipt(runtime, path) as receipt:
        receipt.freeze(_prepare(runtime, execution_request=execution_request), metadata=METADATA)
        admitted = runtime.client.tasks.dispatch_admission(receipt.data["admission"])
        assert admitted.ok
    old_handshake = dict(runtime.client._transport._last_handshake)
    old_epoch = runtime.client._transport.health()["runtime_epoch"]
    runtime.daemon.stop()
    runtime.daemon = runtime.start(runtime.root)
    runtime.client = _connect(runtime.daemon)
    new_handshake = runtime.client._transport._last_handshake
    assert new_handshake["realm_id"] == old_handshake["realm_id"]
    assert new_handshake["session_id"] != old_handshake["session_id"]
    assert runtime.client._transport.health()["runtime_epoch"] > old_epoch
    with _receipt(runtime, path, resume=True) as receipt:
        recovered = receipt.recover_task()
        assert recovered.ok and recovered.data["task_id"] == admitted.data["task_id"]
        if targeted:
            assert recovered.data["execution_request"]["target"] == execution_request["target"]
    assert _count(runtime) == 1


def test_ordinary_default_explicit_and_deterministic_admission_compatibility(runtime):
    kwargs = dict(project_id=runtime.project, capability=CAPABILITY, spec={"message": "hello"})
    first, second = runtime.client.tasks.create(**kwargs), runtime.client.tasks.create(**kwargs)
    assert first.ok and second.ok and first.data["task_id"] != second.data["task_id"]
    explicit = runtime.client.tasks.create(**kwargs, idempotency_key="caller-key", deterministic_idempotency=True)
    replay = runtime.client.tasks.create(**kwargs, idempotency_key="caller-key")
    assert explicit.idempotency_key == replay.idempotency_key == "caller-key"
    assert explicit.data == replay.data
    deterministic = runtime.client.tasks.create(**kwargs, deterministic_idempotency=True)
    repeat = runtime.client.tasks.create(**kwargs, deterministic_idempotency=True)
    assert deterministic.data == repeat.data
    assert _count(runtime) == 4


def _public_fixture(monkeypatch, capability_id=CAPABILITY):
    capability = SimpleNamespace(id=capability_id, capability_type="executor", native_kind="executor",
                                 inputs=(), outputs=(), defaults={}, schema={}, definition={})
    sdk = SimpleNamespace(_load_registries=lambda **_: (SimpleNamespace(get=lambda _: {}), None, None),
                          get_capability=lambda *_args, **_: capability)
    monkeypatch.setattr(invocation, "_sdk_module", lambda: sdk)
    monkeypatch.setattr("astrid.core.foundation.hash.executor_definition_digest", lambda _: DIGEST)
    return capability


def test_new_sdk_render_invocation_after_failed_runtime_task_is_admitted(runtime, monkeypatch):
    """An earlier renderer failure cannot answer a later invocation's task.create."""
    render_id = "fixture.render"
    runtime.daemon.service.register_capability({
        "capability_id": render_id, "definition_digest": DIGEST, "status": "ready",
    })
    _public_fixture(monkeypatch, render_id)
    kwargs = dict(kind="executor", project="demo", inputs={}, outputs={}, client=runtime.client)
    first = invocation.invoke(render_id, **kwargs)
    assert first.ok and first.kernel_task_id

    runtime.client._transport.register_executor({
        "executor_id": "fixture-renderer", "protocol": "workspace.v1", "resource_keys": [],
        "capabilities": [{"capability_id": render_id, "definition_digest": DIGEST, "status": "ready"}],
    }, idempotency_key="register-fixture-renderer")
    worker = _worker(runtime.daemon, "fixture-renderer")
    attempt = worker.claim_task(
        executor_id="fixture-renderer", capability_ids=[render_id], idempotency_key="claim-first-render",
    )
    assert attempt["task_id"] == first.kernel_task_id
    failed = worker.fail_attempt(
        attempt["attempt_id"], lease_id=attempt["lease_id"], fence=attempt["fence"],
        runtime_epoch=attempt["runtime_epoch"], error={"code": "renderer_failed"},
        idempotency_key="fail-first-render",
    )
    assert failed["data"]["state"] == "failed"

    second = invocation.invoke(render_id, **kwargs)
    assert second.ok and second.kernel_task_id != first.kernel_task_id
    assert second.kernel_run_id != first.kernel_run_id
    assert _count(runtime) == 2

    explicit = invocation.invoke(render_id, **kwargs, idempotency_key="same-render-invocation")
    retry = invocation.invoke(render_id, **kwargs, idempotency_key="same-render-invocation")
    assert explicit.ok and retry.ok
    assert retry.kernel_task_id == explicit.kernel_task_id
    assert _count(runtime) == 3
    with pytest.raises(CapabilityValidationError, match="non-empty string"):
        invocation.invoke(render_id, **kwargs, idempotency_key="")
    assert _count(runtime) == 3


def test_timeline_render_cli_accepts_explicit_invocation_key():
    from astrid.packs.timeline.cli import build_parser

    parsed = build_parser(SimpleNamespace()).parse_args([
        "render", "--project", "demo", "--idempotency-key", "retry-render-1",
        "timeline-1", "--detach",
    ])
    assert parsed.idempotency_key == "retry-render-1"
    assert parsed.ref == "timeline-1"


def test_public_invocation_receipt_stores_locator_before_wait_and_resumes_without_preflight(runtime, tmp_path, monkeypatch):
    _public_fixture(monkeypatch)
    path = tmp_path / "invocation.json"
    kwargs = dict(kind="executor", project="demo", inputs={}, outputs={}, client=runtime.client, recovery_path=path)
    first = invocation.invoke(CAPABILITY, **kwargs)
    assert first.ok and first.kernel_task_id
    assert first.kernel_attempt_id == ""
    content = json.loads(path.read_text())
    assert content["admission"]["project_id"] == runtime.project
    assert content["locator"]["task_id"] == first.kernel_task_id
    monkeypatch.setattr(invocation, "_sdk_module", lambda: pytest.fail("resume must branch before live registry selection"))
    resumed = invocation.invoke(CAPABILITY, **kwargs, resume=True, wait=True, timeout_seconds=0.001, poll_seconds=0.001)
    assert not resumed.ok and resumed.error["code"] == "task_wait_timeout"
    assert resumed.kernel_task_id == first.kernel_task_id
    assert resumed.kernel_run_id == first.kernel_run_id
    assert _count(runtime) == 1


def test_public_invocation_persistence_failure_sends_nothing(runtime, tmp_path, monkeypatch):
    _public_fixture(monkeypatch)
    monkeypatch.setattr(AdmissionReceipt, "_persist", lambda *_: (_ for _ in ()).throw(OSError("receipt persistence failed")))
    with pytest.raises(CapabilityInvocationError, match="persistence failed"):
        invocation.invoke(CAPABILITY, kind="executor", project="demo", inputs={}, outputs={},
                          client=runtime.client, recovery_path=tmp_path / "invocation.json")
    assert _count(runtime) == 0


def test_response_loss_then_fake_execution_and_resume_runs_once(runtime, tmp_path, monkeypatch):
    _public_fixture(monkeypatch)
    path = tmp_path / "invocation.json"
    transport = runtime.client._transport
    transport.register_executor({"executor_id": "fake-worker", "protocol": "workspace.v1", "resource_keys": [],
        "capabilities": [{"capability_id": CAPABILITY, "definition_digest": DIGEST, "status": "ready"}]},
        idempotency_key="register-fake-worker")
    request = transport._generated._request
    def dropped(method, url, **kwargs):
        response = request(method, url, **kwargs)
        if method == "POST" and url == "/v1/tasks":
            raise ConnectionResetError("response dropped after commit")
        return response
    kwargs = dict(kind="executor", project="demo", inputs={}, outputs={}, client=runtime.client, recovery_path=path)
    with monkeypatch.context() as m:
        m.setattr(transport._generated, "_request", dropped)
        first = invocation.invoke(CAPABILITY, **kwargs)
        assert not first.ok
    assert json.loads(path.read_text())["locator"] is None
    worker = _worker(runtime.daemon, "fake-worker")
    attempt = worker.claim_task(executor_id="fake-worker", capability_ids=[CAPABILITY], idempotency_key="fake-claim")
    assert attempt and attempt["task_id"]
    executions = []
    executions.append(attempt["task_id"])
    settled = worker.settle_attempt(attempt["attempt_id"], {
        "lease_id": attempt["lease_id"], "fence": attempt["fence"], "runtime_epoch": attempt["runtime_epoch"],
        "outputs": [], "effect": None,
    }, idempotency_key="fake-settle")
    assert settled["data"]["state"] == "succeeded"
    monkeypatch.setattr(invocation, "_sdk_module", lambda: pytest.fail("resume resolved manifest"))
    resumed = invocation.invoke(CAPABILITY, **kwargs, resume=True, wait=True)
    assert resumed.ok and resumed.kernel_task_id == attempt["task_id"]
    again = invocation.invoke(CAPABILITY, **kwargs, resume=True, wait=True)
    assert again.ok and again.kernel_task_id == resumed.kernel_task_id
    assert len(executions) == 1
    assert _count(runtime) == 1
    assert runtime.daemon.service.store.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
    assert worker.claim_task(executor_id="fake-worker", capability_ids=[CAPABILITY], idempotency_key="claim-again") is None


def test_different_authenticated_realm_fails_before_replay(runtime, tmp_path):
    path = tmp_path / "stage.json"
    with _receipt(runtime, path) as receipt:
        receipt.freeze(_prepare(runtime), metadata=METADATA)
    other = runtime.start(tmp_path / "other-realm")
    try:
        other_client = _connect(other)
        with pytest.raises(CapabilityValidationError, match="realm identity mismatch"):
            with _receipt(runtime, path, resume=True, client=other_client):
                pass
        assert other.service.store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert _count(runtime) == 0
    finally:
        other.stop()


@pytest.mark.parametrize("known_locator", [False, True])
def test_write_revocation_does_not_bypass_replay_or_block_authorized_read(runtime, tmp_path, known_locator):
    path = tmp_path / "stage.json"
    with _receipt(runtime, path) as receipt:
        receipt.freeze(_prepare(runtime), metadata=METADATA)
        admitted = runtime.client.tasks.dispatch_admission(receipt.data["admission"])
        assert admitted.ok
        if known_locator:
            receipt.locate(admitted.data)
    token, _ = runtime.daemon.credentials.provision("reader", ["handshake", "tasks:read", "projects:read"])
    transport = WorkspaceClient(runtime.daemon.endpoint, token)
    transport._generated = _current_generated.WorkspaceClient(runtime.daemon.endpoint, token)
    reader = SimpleNamespace(tasks=RemoteTasks(transport), _transport=transport)
    with _receipt(runtime, path, resume=True, client=reader) as receipt:
        observed = receipt.recover_task()
        if known_locator:
            assert observed.ok and observed.data["task_id"] == admitted.data["task_id"]
        else:
            assert not observed.ok and observed.error.code == "unauthorized"
            assert receipt.data["locator"] is None
    assert _count(runtime) == 1


def test_frozen_uncommitted_receipt_enforces_current_readiness_without_retarget(runtime, tmp_path):
    path = tmp_path / "stage.json"
    frozen = _prepare(runtime)
    with _receipt(runtime, path) as receipt:
        receipt.freeze(frozen, metadata=METADATA)
    runtime.daemon.service.store.conn.execute("UPDATE capabilities SET status='unavailable' WHERE id=?", (CAPABILITY,))
    runtime.daemon.service.store.conn.commit()
    with _receipt(runtime, path, resume=True) as receipt:
        result = receipt.recover_task()
        assert not result.ok and result.error.code == "unavailable"
        assert receipt.data["admission"] == frozen
        assert receipt.data["locator"] is None
    assert _count(runtime) == 0


@pytest.mark.parametrize("receipt_state", ["stale", "absent", "partial_current", "partial_stale"])
@pytest.mark.parametrize("changed_field", ["settlement_effect", "capability_digest"])
def test_real_http_conflict_preserves_persisted_identity_and_receipt(runtime, receipt_state, changed_field):
    frozen = _prepare(runtime)
    first = runtime.client.tasks.dispatch_admission(frozen)
    assert first.ok
    store = runtime.daemon.service.store
    if receipt_state in {"partial_current", "partial_stale"}:
        store.conn.execute("UPDATE command_idempotency SET txn_id=NULL, primary_stream_id=NULL, resulting_stream_seq=NULL, first_project_seq=NULL, last_project_seq=NULL, event_ids_json=NULL WHERE command_kind='task.create'")
    if receipt_state in {"stale", "partial_stale"}:
        store.conn.execute("UPDATE command_idempotency SET request_hash='legacy-stale-hash' WHERE command_kind='task.create'")
    elif receipt_state == "absent":
        store.conn.execute("DELETE FROM command_idempotency WHERE command_kind='task.create'")
    store.conn.commit()
    def snapshot():
        return {table: [dict(row) for row in store.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("tasks", "runs", "events", "command_idempotency", "project_sequences")}
    before = snapshot()
    changed = copy.deepcopy(frozen)
    changed[changed_field] = ({"effect_type": "fixture.effect", "value": "different"}
                              if changed_field == "settlement_effect" else "sha256:" + "b" * 64)
    conflict = runtime.client.tasks.dispatch_admission(changed)
    assert not conflict.ok and conflict.error.code == "conflict"
    assert snapshot() == before
    replay = runtime.client.tasks.dispatch_admission(frozen)
    assert replay.ok and replay.data["task_id"] == first.data["task_id"]
    assert replay.receipt is not None
    after = snapshot()
    assert after["tasks"] == before["tasks"] and after["runs"] == before["runs"]
    assert after["events"] == before["events"]
    assert sum(row["command_kind"] == "task.create" for row in after["command_idempotency"]) == 1
    if receipt_state != "stale":
        assert replay.receipt.event_ids == ()
    again = runtime.client.tasks.dispatch_admission(frozen)
    assert again.ok and again.receipt == replay.receipt
    assert snapshot() == after
