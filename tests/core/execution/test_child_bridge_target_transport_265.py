"""SDK/socket/host target contract, independent of the 263/264 implementation.

Selected-request admissions use an explicitly marked boundary double because
base RuntimeProtocolClient lacks task264's optional argument. No-controls file
and scalar admissions exercise the real client, authority, CAS and Runtime.
These tests do not establish synchronized Runtime placement/claim acceptance.
"""
from __future__ import annotations

import copy
import hashlib

import pytest

from astrid import CapabilityValidationError, invoke
from astrid.core.execution._child_bridge import ChildBridgeError, task_resource
from astrid.sdk import _child_bridge
from astrid.sdk.execution_request import normalize_execution_request
from tests.core.execution.test_generic_host_child_bridge_d18 import (
    CHILD, bridge, request, setup, world,  # noqa: F401 -- disposable Runtime fixture
)

INTEGRATION_FIXTURES = ("world",)
ALTERNATE = {"kind": "machine", "id": "alternate-machine"}


@pytest.fixture(autouse=True)
def service_health(world, monkeypatch):
    # In-process health lacks the HTTP decoder's identity field. Use the live
    # epoch, matching D18's established fixture; authority fences stay active.
    monkeypatch.setattr(world.client, "health", lambda **_kw: world.service.health())
    # Own the fixture roots, as in SDK's D18 tests. Unrelated built-in pack
    # discovery can outlive this disposable parent's short lease.
    from astrid.core.execution.executor import registry as executor_registry
    from astrid.core.execution.orchestrator import registry as orchestrator_registry
    from astrid.core.pack import discovery as pack_discovery
    for module in (executor_registry, orchestrator_registry, pack_discovery):
        monkeypatch.setattr(module, "discover_packs", lambda *_args, **_kwargs: ())


def install(world, monkeypatch, *, targeted=False, file_input=True):
    setup(world, targeted=targeted, derived=file_input)
    source = bridge(world)
    (source.output_root / "source.txt").write_text("managed child bytes")
    sdk = _child_bridge.ChildBridge(source.child_channel)
    monkeypatch.setattr(_child_bridge, "_bridge", sdk)
    return source, sdk


def call(world, *, file_input=True, **kwargs):
    return invoke(CHILD, kind="action", extra_pack_roots=(str(world.root),),
                  inputs={"value": ({"filename": "source.txt", "output_port": "source",
                                     "media_type": "text/plain"} if file_input else "scalar")},
                  wait=False, **kwargs)


def admission_boundary(world, monkeypatch, *, wrong_target=False):
    """Capture the agreed new API and synthesize its queued admission readback."""
    calls = []

    def admit(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        selected = kwargs["execution_request"]
        descriptors = [{**{key: ref[key] for key in ("name", "object_id", "filename", "required")},
                        "digest": ref["object_id"]} for ref in kwargs["ordered_inputs"]]
        child = kwargs["child"]
        lineage = {"parent_task_id": kwargs["task_id"], "parent_attempt_id": kwargs["attempt_id"],
                   "parent_lease_id": kwargs["lease_id"], "parent_fence": kwargs["fence"],
                   "runtime_epoch": kwargs["runtime_epoch"], "project_id": kwargs["project_id"],
                   "executor_id": world.host.executor_id}
        return {"task_id": "boundary-child", "run_id": "boundary-run", "state": "queued",
                "capability_id": child["capability_id"], "capability_digest": child["capability_digest"],
                "idempotency_key": child["child_id"], "input_object_ids": [d["object_id"] for d in descriptors],
                "execution_request": normalize_execution_request({
                    **selected, "inputs": descriptors,
                    **({"target": {"kind": "default"}} if wrong_target else {}),
                }), "spec": {"delegated_parent": lineage}}

    monkeypatch.setattr(world.client, "admit_child", admit)
    return calls


def test_explicit_default_precedes_targeted_parent_through_public_socket(world, monkeypatch):
    source, sdk = install(world, monkeypatch, targeted=True)
    calls = admission_boundary(world, monkeypatch)
    try:
        result = call(world, execution_request={"target": {"kind": "default"},
                                               "limits": {"max_runtime_seconds": 17}})
        assert result.ok, result.error
        assert calls[0]["execution_request"] == normalize_execution_request({
            "target": {"kind": "default"}, "limits": {"max_runtime_seconds": 17}})
        assert calls[0]["ordered_inputs"][0]["required"] is True
        assert len(source.admitted_children_snapshot().children) == 1
    finally:
        sdk.close()


def test_explicit_alternate_target_and_controls_survive_bridge(world, monkeypatch):
    source, sdk = install(world, monkeypatch, targeted=True)
    source.policy["targets"].append(ALTERNATE)
    calls = admission_boundary(world, monkeypatch)
    try:
        result = call(world, execution_request={"execution": {"target": ALTERNATE},
                                               "checks": {"preflight": ["files"]}})
        assert result.ok, result.error
        assert calls[0]["execution_request"] == normalize_execution_request({
            "execution": {"target": ALTERNATE}, "checks": {"preflight": ["files"]}})
        # Descriptors remain host-derived, outside caller controls.
        assert calls[0]["inputs"]["value"]["filename"] == "source.txt"
        assert calls[0]["execution_request"].get("inputs", []) == []
    finally:
        sdk.close()


def test_inherits_only_validated_parent_target_with_managed_inputs(world, monkeypatch):
    source, sdk = install(world, monkeypatch, targeted=True)
    calls = admission_boundary(world, monkeypatch)
    try:
        result = call(world)
        assert result.ok, result.error
        assert calls[0]["execution_request"] == normalize_execution_request({
            "target": world.identity["execution_binding"]["actual"]})
        expected = "sha256:" + hashlib.sha256(b"managed child bytes").hexdigest()
        assert calls[0]["ordered_inputs"] == [{
            "name": "value", "filename": "source.txt", "output_port": "source",
            "media_type": "text/plain", "object_id": expected, "size": 19, "required": True}]
        assert calls[0]["derived_inputs"][0]["object_id"] == expected
    finally:
        sdk.close()


@pytest.mark.parametrize("file_input", [True, False])
def test_local_no_controls_keeps_real_default_request_and_custody(world, monkeypatch, file_input):
    source, sdk = install(world, monkeypatch, file_input=file_input)
    admissions = []
    real_admit = world.client.admit_child

    def capture(**kwargs):
        admissions.append(copy.deepcopy(kwargs))
        return real_admit(**kwargs)

    monkeypatch.setattr(world.client, "admit_child", capture)
    try:
        result = call(world, file_input=file_input)
        assert result.ok, result.error
        assert "execution_request" not in admissions[0]
        outgoing = next(body["task"] for _, path, body in world.calls if path == "/v1/delegated-tasks")
        if file_input:
            descriptor = outgoing["execution_request"]["inputs"][0]
            expected = "sha256:" + hashlib.sha256(b"managed child bytes").hexdigest()
            assert outgoing["execution_request"] == normalize_execution_request(outgoing["execution_request"])
            assert outgoing["execution_request"]["target"] == {"kind": "default"}
            assert descriptor == {"name": "value", "object_id": expected, "digest": expected,
                                  "filename": "source.txt", "required": True}
            assert outgoing["input_object_ids"] == [expected]
            assert outgoing["spec"]["inputs"]["value"] == {
                "object_id": expected, "digest": expected, "filename": "source.txt"}
        else:
            assert "execution_request" not in outgoing
            assert outgoing["input_object_ids"] == []
        assert len(source.admitted_children_snapshot().children) == 1
    finally:
        sdk.close()


@pytest.mark.parametrize("mutation", ["actual", "unverified", "fence", "effective", "changed"])
def test_forged_or_changed_parent_binding_rejected_before_upload(world, monkeypatch, mutation):
    source, sdk = install(world, monkeypatch, targeted=True)
    calls = admission_boundary(world, monkeypatch)
    real_task = world.client.task

    def forged(task_id):
        value = copy.deepcopy(task_resource(real_task(task_id)))
        binding = value["execution_binding"]
        if mutation == "actual":
            binding["actual_target"] = ALTERNATE
        elif mutation == "unverified":
            binding["verification"]["verified"] = False
        elif mutation == "fence":
            binding["fence"] += 1
        elif mutation == "effective":
            binding["effective_target"] = ALTERNATE
        else:
            binding["binding_id"] += "-changed"
        return value

    monkeypatch.setattr(world.client, "task", forged)
    try:
        result = call(world)
        assert not result.ok
        assert calls == []
        assert not any(path == "/v1/objects" for _, path, _ in world.calls)
        assert source.admitted_children_snapshot().children == ()
    finally:
        sdk.close()


def test_target_outside_policy_is_rejected_before_upload(world, monkeypatch):
    source, sdk = install(world, monkeypatch)
    calls = admission_boundary(world, monkeypatch)
    try:
        result = call(world, execution_request={"target": ALTERNATE})
        assert not result.ok
        assert calls == []
        assert not any(path == "/v1/objects" for _, path, _ in world.calls)
    finally:
        sdk.close()


def test_bridge_independently_rejects_caller_binding(world, monkeypatch):
    source, sdk = install(world, monkeypatch)
    calls = admission_boundary(world, monkeypatch)
    frame = request(world)
    frame["execution_request"] = {"target": {"kind": "default"}, "execution_binding": {"verified": True}}
    with pytest.raises(ChildBridgeError, match="unsupported fields"):
        source.dispatch(frame)
    assert calls == []
    sdk.close()


def test_target_readback_mismatch_does_not_publish_admission(world, monkeypatch):
    source, sdk = install(world, monkeypatch, targeted=True)
    calls = admission_boundary(world, monkeypatch, wrong_target=True)
    try:
        result = call(world)
        assert not result.ok
        assert len(calls) == 1
        assert source.admitted_children_snapshot().children == ()
    finally:
        sdk.close()


def test_target_is_part_of_automatic_key_and_fixed_key_replay(world, monkeypatch):
    source, sdk = install(world, monkeypatch, targeted=True)
    source.policy["targets"].append(ALTERNATE)
    calls = admission_boundary(world, monkeypatch)
    try:
        assert call(world, execution_request={"target": {"kind": "default"}}).ok
        assert call(world, execution_request={"target": ALTERNATE}).ok
        assert calls[0]["child"]["child_id"] != calls[1]["child"]["child_id"]
        assert call(world, child_key="fixed", execution_request={"target": {"kind": "default"}}).ok
        assert call(world, child_key="fixed", execution_request={"target": {"kind": "default"}}).ok
        result = call(world, child_key="fixed", execution_request={"target": ALTERNATE})
        assert not result.ok
        assert len(calls) == 3
    finally:
        sdk.close()


@pytest.mark.parametrize("controls", [
    {"execution_request": {"target": {"kind": "default"}, "execution_binding": {}}},
    {"execution_request": {"target": ALTERNATE, "execution": {"target": {"kind": "default"}}}},
    {"project": "forged"}, {"idempotency_key": "forged"}, {"outputs": {}},
])
def test_public_child_rejects_invalid_or_forbidden_controls(world, monkeypatch, controls):
    source, sdk = install(world, monkeypatch)
    calls = admission_boundary(world, monkeypatch)
    try:
        with pytest.raises(CapabilityValidationError):
            call(world, **controls)
        assert calls == []
        assert source.admitted_children_snapshot().children == ()
    finally:
        sdk.close()



@pytest.mark.parametrize("configured, trusted", [(False, True), (True, True), (True, False)])
def test_staged_child_actual_host_claim_selector(world, monkeypatch, configured, trusted):
    """Observe the real host wire against Runtime's staged-child filtering.

    This is an offline staged transport fixture, not a replay of session10827.
    """
    import json

    from astrid.core.execution.generic_host import GenericPackHost
    from tests.core.execution.test_generic_host_child_bridge_d18 import PARENT, digest

    monkeypatch.delenv("ASTRID_EXECUTION_TARGET_JSON", raising=False)
    setup(world, derived=True)
    service = world.service
    host = world.host
    # setup has one parent already running. Allow a second, staged parent and
    # retain exactly one spare slot for its child.
    service.register_executor({"executor_id": "worker", "capabilities": [PARENT, CHILD],
                               "max_concurrency": 3, "runtime_epoch": service.health()["runtime_epoch"]}, idempotency_key="stage-register")
    oid = service.ingest(world.project, b"staged frame", original_name="frame.bin",
                         idempotency_key="stage-frame")["data"]["digest"]
    refs = [{"name": "value", "root_object_id": oid}]
    child_digest = host.capabilities[CHILD].capability_digest
    policy = {"capabilities": [{"capability_id": CHILD, "capability_digest": child_digest}],
              "targets": [{"kind": "default"}], "input_object_ids": [oid],
              "stages": [{"name": "generate", "capability_id": CHILD,
                          "capability_digest": child_digest, "target": {"kind": "default"},
                          "inputs": refs}]}
    service.create_task({"project": world.project, "capability_id": PARENT,
                         "capability_digest": host.capabilities[PARENT].capability_digest,
                         "input_object_ids": [oid], "spec": {}, "child_delegation": policy,
                         "idempotency_key": "stage-parent"}, enforce_readiness=True)
    parent = service.claim_next({"executor_id": "worker", "capability_ids": [PARENT],
                                 "runtime_epoch": 1}, idempotency_key="stage-parent-claim",
                                identity=world.identity)
    world.client._attempt_runtime_epochs[parent["attempt_id"]] = parent["runtime_epoch"]
    admitted = world.client.admit_child(
        child={"child_id": "stage-child", "capability_id": CHILD, "capability_digest": child_digest},
        inputs={}, ordered_inputs=[{"name": "value", "object_id": oid, "filename": "frame.bin", "required": True}],
        derived_inputs=[], stage="generate", input_refs=refs, task_id=parent["task_id"],
        attempt_id=parent["attempt_id"], lease_id=parent["lease_id"], fence=parent["fence"],
        runtime_epoch=parent["runtime_epoch"], run_id=parent["run_id"], project_id=world.project)
    child_id = task_resource(admitted)["task_id"]
    wire = next(body["task"] for _, path, body in world.calls if path == "/v1/delegated-tasks")
    assert wire["stage"] == "generate" and wire["input_refs"] == refs
    assert wire["execution_request"]["target"] == {"kind": "default"}
    assert service.store.execution_binding(child_id)["resolved_target"] == {"kind": "default"}
    assert service._local_claim_actor != "worker"
    assert service._trusted_execution_placement(world.identity)["actual"]["kind"] == "machine"

    if configured:
        monkeypatch.setenv("ASTRID_EXECUTION_TARGET_JSON", '{"kind":"default"}')
    if not trusted:
        world.identity.pop("execution_binding")
    calls = []
    store_calls = []
    original_store_claim = service.store._claim_task
    def observe_store(*args, **kwargs):
        store_calls.append(args[0])
        return original_store_claim(*args, **kwargs)
    monkeypatch.setattr(service.store, "_claim_task", observe_store)
    original_transport = world.client.generated._transport
    def claim_transport(method, path, headers, body):
        if path != "/v1/tasks/claim":
            return original_transport(method, path, headers, body)
        payload = json.loads(body)
        calls.append(payload)
        value = service.claim_next(payload, idempotency_key=headers["Idempotency-Key"], identity=world.identity)
        return (204, {}, b"") if value is None else (200, {}, json.dumps(value).encode())
    monkeypatch.setattr(world.client.generated, "_transport", claim_transport)
    # Keep the real selection, wire, Runtime claim, heartbeat and task read;
    # provider execution is outside this diagnostic.
    monkeypatch.setattr(GenericPackHost, "run_task", lambda self, task, **kw: {"task_id": child_id})
    result = host.claim_once()
    assert len(calls) == (2 if configured and not trusted else 1)
    assert all(call.get("target") == ({"kind": "default"} if configured else None) for call in calls)
    assert all(("target" in call) is configured for call in calls)
    if configured and trusted:
        assert result == {"task_id": child_id}
        assert store_calls == [child_id]
        assert service.task(child_id)["task"]["status"] == "running"
    else:
        assert result is None and store_calls == []
        assert service.task(child_id)["task"]["status"] == "queued"
        if configured:
            assert service.task(child_id)["task"]["waiting_reason"] == "execution_binding_missing"


def test_ordinary_untargeted_child_claim_remains_queue_wide(world, monkeypatch):
    """The unconfigured host's actual request still consumes untargeted work."""
    import json
    from astrid.core.execution.generic_host import GenericPackHost

    monkeypatch.delenv("ASTRID_EXECUTION_TARGET_JSON", raising=False)
    _, sdk = install(world, monkeypatch, file_input=False)
    try:
        admitted = call(world, file_input=False)
        assert admitted.ok, admitted.error
        child_id = admitted.kernel_task_id
        wire_child = next(body["task"] for _, path, body in world.calls if path == "/v1/delegated-tasks")
        assert "execution_request" not in wire_child
        calls = []
        original_transport = world.client.generated._transport
        def claim_transport(method, path, headers, body):
            if path != "/v1/tasks/claim":
                return original_transport(method, path, headers, body)
            payload = json.loads(body)
            calls.append(payload)
            value = world.service.claim_next(payload, idempotency_key=headers["Idempotency-Key"], identity=world.identity)
            return (204, {}, b"") if value is None else (200, {}, json.dumps(value).encode())
        monkeypatch.setattr(world.client.generated, "_transport", claim_transport)
        monkeypatch.setattr(GenericPackHost, "run_task", lambda self, task, **kw: {"task_id": child_id})
        assert world.host.claim_once() == {"task_id": child_id}
        assert len(calls) == 1 and "target" not in calls[0]
        assert world.service.task(child_id)["task"]["status"] == "running"
    finally:
        sdk.close()
