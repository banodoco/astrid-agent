"""Exact H3 child dispatch through the real disposable Runtime HTTP transport.

Only the final CPU execution boundary is synthetic. No provider is activated;
fixture authorities and placement are generated inside the disposable realm.
"""
from __future__ import annotations

import hashlib
import json
import threading
from types import SimpleNamespace

import pytest
import yaml

from astrid.core.execution._child_bridge import ChildBridgeError, HostChildBridge, resource, task_resource
from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.sdk._child_bridge import ChildBridge, _BridgeRejected
from astrid_runtime_client import ApiError, WorkspaceClient

pytestmark = pytest.mark.integration
PARENT = "h3_av.transform"
CHILD = "h3_av.child"
DEFAULT = {"kind": "default"}
MACHINE = {"kind": "machine", "id": "claim135-fixture-machine"}


def digest(value):
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def claim_world(tmp_path, monkeypatch, real_runtime):
    # The canonical h3_av source pack has the same id as this tiny CPU fixture.
    # Retain the explicit fixture root without source-pack precedence hiding it.
    from astrid.core.execution.executor import registry as executor_registry
    from astrid.core.execution.orchestrator import registry as orchestrator_registry
    from astrid.core.pack import discovery as pack_discovery
    for module in (executor_registry, orchestrator_registry, pack_discovery):
        monkeypatch.setattr(module, "discover_packs", lambda *_args, **_kwargs: ())
    daemon = real_runtime()
    # A synthetic credential claim on the disposable HTTP fixture; no host
    # credential file or real-machine placement is consulted.
    from runtime_protocol.daemon import WORKER_SCOPES
    token, _ = daemon.credentials.provision("astrid-pack-host", list(WORKER_SCOPES), rotate=True,
        metadata={"execution_binding": {"actual": MACHINE, "executor_incarnation": "claim135/fixture",
            "verification": {"method": "credential_claim", "verified": True,
                             "evidence_digest": digest("placement")}}})
    owner = WorkspaceClient(daemon.endpoint, daemon.token)
    client = RuntimeProtocolClient(daemon.endpoint, token, timeout=3)
    client.executor_id = "astrid-pack-host"
    project = resource(owner.create_project("Exact claim", slug="claim135", idempotency_key="project"))["project_id"]
    pack = tmp_path / "cpu-pack"
    (pack / "actions").mkdir(parents=True)
    (pack / "actions/cpu.py").write_text("def run():\n    return {}\n")
    action = {"description": "CPU fixture", "invocation": {
        "kind": "python", "path": "actions/cpu.py", "function": "run"}, "inputs": [], "outputs": {}}
    (pack / "pack.yaml").write_text(yaml.safe_dump({"schema_version": 3, "id": "h3_av", "name": "Fixture",
        "version": "1.0.0", "actions": {"transform": action, "child": action}}))
    host = GenericPackHost(pack_roots=[pack], client=client, executor_id="astrid-pack-host", max_concurrency=4)
    host.discover()
    assert set(host.capabilities) == {PARENT, CHILD}
    for record in host.capabilities.values():
        owner.register_capability(record.id, record.capability_digest, idempotency_key="cap-" + record.id)
    client.generated.register_executor({"executor_id": host.executor_id, "capabilities": [PARENT, CHILD],
        "max_concurrency": 4, "runtime_epoch": daemon.service.health()["runtime_epoch"]}, idempotency_key="register")
    policy = {"capabilities": [{"capability_id": CHILD, "capability_digest": host.capabilities[CHILD].capability_digest}],
        "targets": [DEFAULT, MACHINE], "input_object_ids": [], "stages": [{"name": "prepare", "capability_id": CHILD,
        "capability_digest": host.capabilities[CHILD].capability_digest, "target": DEFAULT, "inputs": []}]}
    owner.admit_task(capability_id=PARENT, capability_digest=host.capabilities[PARENT].capability_digest,
        project_id=project, input_object_ids=[], spec={}, child_delegation=policy, idempotency_key="parent")
    claim = client.claim_next(executor_id=host.executor_id, capability_ids=[PARENT], idempotency_key="parent-claim")
    parent = task_resource(client.task(claim.task_id))
    parent.update(attempt_id=claim.attempt_id, lease_id=claim.lease_id, fence=claim.fence,
                  runtime_epoch=claim.runtime_epoch, executor_id=host.executor_id)
    context = {"task_id": claim.task_id, "run_id": claim.run_id, "project_id": project,
        "attempt_id": claim.attempt_id, "lease_id": claim.lease_id, "fence": claim.fence, "runtime_epoch": claim.runtime_epoch}
    # Stages are ordered and unique within a parent. Queue a delegated decoy
    # under a second live parent through normal admission, rather than forging
    # a same-stage sibling that Runtime intentionally forbids.
    owner.admit_task(capability_id=PARENT, capability_digest=host.capabilities[PARENT].capability_digest,
        project_id=project, input_object_ids=[], spec={}, child_delegation=policy, idempotency_key="foreign-parent")
    foreign_claim = client.claim_next(executor_id=host.executor_id, capability_ids=[PARENT], idempotency_key="foreign-parent-claim")
    foreign_context = {"task_id": foreign_claim.task_id, "run_id": foreign_claim.run_id, "project_id": project,
        "attempt_id": foreign_claim.attempt_id, "lease_id": foreign_claim.lease_id,
        "fence": foreign_claim.fence, "runtime_epoch": foreign_claim.runtime_epoch}
    foreign_child = task_resource(client.admit_child(child={"child_id": "foreign-child", "capability_id": CHILD,
        "capability_digest": host.capabilities[CHILD].capability_digest}, inputs={}, ordered_inputs=[],
        derived_inputs=[], stage="prepare", input_refs=[], **foreign_context))
    calls = []
    original_request = WorkspaceClient._request
    def observe(self, method, path, **kwargs):
        if path.endswith("/claim-delegated-child"):
            payload = json.loads(kwargs["body"])
            calls.append({"path": path, "target": payload["target"], "executor_id": payload["executor_id"],
                          "capability_ids": payload["capability_ids"]})
        elif path == "/v1/tasks/claim":
            calls.append({"path": path})
        return original_request(self, method, path, **kwargs)
    monkeypatch.setattr(WorkspaceClient, "_request", observe)
    output = tmp_path / "parent-attempt" / "outputs"
    output.parent.mkdir()
    source = HostChildBridge(host, parent, attempt_id=claim.attempt_id, lease_id=claim.lease_id,
        fence=claim.fence, runtime_epoch=claim.runtime_epoch, output_root=output, cancelled=lambda: False)
    sdk = ChildBridge(source.child_channel)
    world = SimpleNamespace(daemon=daemon, owner=owner, client=client, host=host, source=source, sdk=sdk,
        calls=calls, context=context, parent=parent, project=project, foreign_child=foreign_child)
    try:
        yield world
    finally:
        source.revoke()
        for bound in source._children.values():
            thread = bound.get("claim_thread")
            if thread is not None:
                thread.join(timeout=5)
                assert not thread.is_alive(), "exact child dispatch did not release its owned thread"


def child_request(world, key="selected"):
    return {"child_key": key, "child": {"capability_id": CHILD,
        "capability_digest": world.host.capabilities[CHILD].capability_digest}, "inputs": {},
        "input_descriptors": [], "stage": "prepare", "input_refs": [], "wait": False,
        "timeout_seconds": 5, "poll_seconds": 0.01}


def admit_child(world, key):
    return task_resource(world.client.admit_child(child={"child_id": key, "capability_id": CHILD,
        "capability_digest": world.host.capabilities[CHILD].capability_digest}, inputs={}, ordered_inputs=[],
        derived_inputs=[], stage="prepare", input_refs=[], **world.context))


def unrelated(world):
    return task_resource(world.owner.admit_task(capability_id=CHILD,
        capability_digest=world.host.capabilities[CHILD].capability_digest,
        input_object_ids=[], project_id=world.project, spec={}, idempotency_key="unrelated"))


def test_h3_bridge_claims_only_exact_child_and_keeps_status_socket_responsive(claim_world, monkeypatch):
    world = claim_world
    foreign = world.foreign_child
    other = unrelated(world)
    entered, release = threading.Event(), threading.Event()
    executed = []
    def execute(slot, task, **kwargs):
        executed.append((slot, task["task"]["id"], kwargs))
        entered.set()
        assert release.wait(timeout=5)
        slot.client.fail(task["task"]["id"], kwargs["lease_token"], "synthetic CPU boundary complete",
            retryable=False, attempt_id=kwargs["attempt_id"], fence=kwargs["fence"])
        return {"task_id": task["task"]["id"]}
    monkeypatch.setattr(GenericPackHost, "run_task", execute)
    try:
        selected = world.sdk.submit(child_request(world))
        assert entered.wait(timeout=5), "staged bridge did not execute its exact child"
        assert world.sdk.status("selected", selected["task_id"])["state"] == "running"
        assert world.calls == [{"path": f"/v1/tasks/{selected['task_id']}/claim-delegated-child",
            "target": DEFAULT, "executor_id": world.host.executor_id, "capability_ids": [CHILD]}]
        assert executed[0][0] is not world.host
        assert executed[0][0].client is not world.client
        assert executed[0][1] == selected["task_id"]
        assert world.host._handoff_active_attempts == 1
        # Only the two admissions renew the parent. Exact claim itself must
        # use its live authority without granting additional parent runtime.
        assert world.client._heartbeat_sequence == 2
        assert task_resource(world.client.task(foreign["task_id"]))["state"] == "queued"
        assert task_resource(world.client.task(other["task_id"]))["state"] == "queued"
    finally:
        release.set()
        bound = world.source._children.get("selected", {})
        if "claim_thread" in bound:
            bound["claim_thread"].join(timeout=5)
    assert world.host._handoff_active_attempts == 0


@pytest.mark.parametrize("mutation", ["missing", "unrelated", "wrong_target", "revoked"])
def test_exact_client_rejections_never_claim_foreign_child_or_queue_item(claim_world, mutation):
    world = claim_world
    selected, foreign, other = admit_child(world, "selected"), world.foreign_child, unrelated(world)
    task_id, target = selected["task_id"], DEFAULT
    if mutation == "missing":
        task_id = "absent-child"
    elif mutation == "unrelated":
        task_id = other["task_id"]
    elif mutation == "wrong_target":
        target = MACHINE
    else:
        world.owner.cancel_task(world.context["task_id"], idempotency_key="revoke-parent")
    heartbeat_sequence = world.client._heartbeat_sequence
    with pytest.raises(ApiError):
        world.client.claim_delegated_child(task_id, executor_id=world.host.executor_id,
            capability_ids=[CHILD], target=target, parent=world.context, idempotency_key="exact-rejection")
    assert world.client._heartbeat_sequence == heartbeat_sequence
    assert not any(call["path"] == "/v1/tasks/claim" for call in world.calls)
    foreign_task = task_resource(world.client.task(foreign["task_id"]))
    assert foreign_task.get("attempt_id") is None
    assert foreign_task["state"] == "queued"
    assert task_resource(world.client.task(other["task_id"]))["state"] == "queued"


def test_non_h3_staged_bridge_retains_external_serving_lifecycle(claim_world):
    world = claim_world
    world.source.task["capability_id"] = "some.other_parent"
    selected = world.sdk.submit(child_request(world))
    assert not world.calls
    assert "claim_thread" not in world.source._children["selected"]
    assert world.sdk.status("selected", selected["task_id"])["state"] == "queued"


def test_revocation_before_dispatch_prevents_child_execution(claim_world, monkeypatch):
    world = claim_world
    selected = admit_child(world, "selected")
    world.source.revoke()
    monkeypatch.setattr(world.host, "_new_claim_slot", lambda: pytest.fail("revoked bridge launched a child"))
    with pytest.raises(ChildBridgeError, match="parent authority ended"):
        world.source._dispatch_local_stage({}, selected)
    assert not world.calls


def test_h3_bridge_exact_claim_rejection_never_falls_back(claim_world, monkeypatch):
    world = claim_world
    other = unrelated(world)
    original = WorkspaceClient.claim_delegated_child
    def wrong_selector(self, task_id, **kwargs):
        return original(self, task_id, **{**kwargs, "target": MACHINE})
    monkeypatch.setattr(WorkspaceClient, "claim_delegated_child", wrong_selector)
    monkeypatch.setattr(GenericPackHost, "run_task", lambda *_a, **_k: pytest.fail("rejected child executed"))
    selected = world.sdk.submit(child_request(world))
    bound = world.source._children["selected"]
    bound["claim_thread"].join(timeout=5)
    assert not bound["claim_thread"].is_alive() and bound["claim_failed"]
    with pytest.raises(_BridgeRejected):
        world.sdk.status("selected", selected["task_id"])
    assert world.calls == [{"path": f"/v1/tasks/{selected['task_id']}/claim-delegated-child",
        "target": MACHINE, "executor_id": world.host.executor_id, "capability_ids": [CHILD]}]
    for row in (selected, world.foreign_child, other):
        assert task_resource(world.client.task(row["task_id"]))["state"] == "queued"
    assert world.host._handoff_active_attempts == 0
