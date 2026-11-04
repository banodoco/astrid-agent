"""Agent payload boundaries only; Runtime admission needs the 263/265 pair."""
from __future__ import annotations

import copy
import json
import threading
from types import SimpleNamespace

import pytest

from astrid.core.execution._child_bridge import ChildBridgeError
from astrid.core.execution.generic_host import HostError, RuntimeProtocolClient, _execution_contract
from astrid.sdk.execution_request import (
    ExecutionRequestError,
    TARGETED_EXECUTION_BINDING_CAPABILITY,
    normalize_execution_request,
)
from astrid.sdk.remote import RemoteAstridClient, RemoteTasks
from astrid_runtime_client import WorkspaceClient as GeneratedClient
from tests.core.execution.test_generic_host_contract import _task


TARGETS = [
    pytest.param({"kind": "machine", "id": "explicit-child"}, id="explicit"),
    pytest.param({"kind": "default"}, id="explicit-default"),
    pytest.param({"kind": "runpod", "pod_id": "parent-pod", "provider_account_ref": "fixture-account",
                  "release_digest": "sha256:" + "c" * 64}, id="inherited"),
]
ORDERED = [
    {"name": name, "object_id": "sha256:" + digest * 64,
     "filename": name + ".txt", "required": required}
    for name, digest, required in (("zeta", "a", True), ("alpha", "b", False))
]
DESCRIPTORS = [{**item, "digest": item["object_id"]} for item in ORDERED]


@pytest.fixture
def boundary():
    # Keep actual host heartbeat/epoch checks and generated JSON serializers;
    # replace only HTTP with bounded deterministic responses. No Runtime runs.
    client = RuntimeProtocolClient.__new__(RuntimeProtocolClient)
    client.generated = GeneratedClient.__new__(GeneratedClient)
    client._heartbeat_lock = threading.RLock()
    client._heartbeat_sequence = 0
    client._heartbeat_session = "fixture-session"
    client._attempt_runtime_epochs = {"parent-attempt": 4}
    client.health = lambda: {"runtime_epoch": 4}
    context = {"task_id": "parent-task", "attempt_id": "parent-attempt", "lease_id": "parent-lease",
               "fence": 7, "runtime_epoch": 4, "run_id": "parent-run", "project_id": "project"}
    calls = []

    def request(method, path, *, body, headers, **_kwargs):
        payload = json.loads(body)
        calls.append((method, path, payload, headers))
        if path.endswith("/child-authority"):
            response = {"child": payload["child"], "parent_task_id": context["task_id"],
                        "parent_attempt_id": context["attempt_id"], "authority": "fixture-authority",
                        "derived_inputs": [{**ref, "parent_attempt_id": context["attempt_id"],
                                            "association_id": "association-" + ref["name"]}
                                           for ref in payload["derived_inputs"]]}
        else:
            assert path.endswith("/heartbeat") or path == "/v1/delegated-tasks"
            response = {"data": {"task_id": "child-task"}, "receipt": {}}
        return 200, {}, json.dumps(response).encode()

    client.generated._request = request

    def submit(execution_request=None, *, files=True):
        return client.admit_child(
            child={"child_id": "child-wire-id", "capability_id": "fixture.echo",
                   "capability_digest": "sha256:" + "d" * 64},
            inputs={"message": "scalar"}, ordered_inputs=copy.deepcopy(ORDERED if files else []),
            derived_inputs=copy.deepcopy(ORDERED[:1] if files else []),
            execution_request=execution_request, **context,
        )

    return SimpleNamespace(client=client, calls=calls, submit=submit)


def _outgoing(boundary):
    return next(row[2]["task"] for row in boundary.calls if row[1] == "/v1/delegated-tasks")


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("files", [True, False], ids=["managed-inputs", "scalar-only"])
def test_host_preserves_selected_child_request_through_generated_wire(boundary, target, files):
    # Task265 selects explicit vs validated inherited intent before this call.
    request = {"schema_version": 1, "target": target, "limits": {"max_runtime_seconds": 30}}
    original = copy.deepcopy(request)
    boundary.submit(request, files=files)
    outgoing = _outgoing(boundary)
    expected = normalize_execution_request({**request, "inputs": DESCRIPTORS if files else []})
    assert outgoing["execution_request"] == expected
    assert request == original
    expected_ids = [item["object_id"] for item in DESCRIPTORS] if files else []
    assert outgoing["input_object_ids"] == expected_ids
    assert outgoing["spec"]["inputs"]["message"] == "scalar"
    for item in DESCRIPTORS if files else []:
        assert outgoing["spec"]["inputs"][item["name"]] == {
            key: item[key] for key in ("object_id", "digest", "filename")}
    heartbeat, issuance, admission = boundary.calls
    assert heartbeat[2]["lease_id"] == issuance[2]["lease_id"] == "parent-lease"
    assert heartbeat[2]["fence"] == issuance[2]["fence"] == 7
    assert heartbeat[2]["runtime_epoch"] == issuance[2]["runtime_epoch"] == 4
    assert admission[3]["Idempotency-Key"] == issuance[2]["child"]["child_id"]


@pytest.mark.parametrize("files", [True, False], ids=["managed-inputs", "scalar-only"])
def test_no_controls_local_child_keeps_canonical_request_semantics(boundary, files):
    boundary.submit(files=files)
    outgoing = _outgoing(boundary)
    if files:
        assert outgoing["execution_request"] == {
            "schema_version": 1, "target": {"kind": "default"}, "inputs": DESCRIPTORS}
        assert outgoing["input_object_ids"] == [item["object_id"] for item in DESCRIPTORS]
    else:
        assert "execution_request" not in outgoing
        assert outgoing["input_object_ids"] == []


@pytest.mark.parametrize("inputs", [ORDERED, DESCRIPTORS], ids=["optional-digest", "digest-witness"])
def test_matching_request_inputs_keep_canonical_receipt_descriptors(boundary, inputs):
    boundary.submit({"target": {"kind": "machine", "id": "child"}, "inputs": inputs})
    assert _outgoing(boundary)["execution_request"]["inputs"] == DESCRIPTORS


@pytest.mark.parametrize("change", ["object_id", "filename", "required", "name", "order"])
def test_forged_request_inputs_cannot_replace_canonical_descriptors(boundary, change):
    inputs = copy.deepcopy(ORDERED)
    if change == "order":
        inputs.reverse()
    else:
        inputs[0][change] = {"object_id": "sha256:" + "f" * 64, "filename": "forged.txt",
                             "required": False, "name": "forged"}[change]
    with pytest.raises(ChildBridgeError, match="canonical descriptors"):
        boundary.submit({"target": {"kind": "machine", "id": "child"}, "inputs": inputs})
    assert all(row[1] != "/v1/delegated-tasks" for row in boundary.calls)


@pytest.mark.parametrize("child_request", [
    {"target": {"kind": "unverified", "id": "child"}},
    {"target": {"kind": "machine", "id": "child"}, "execution_binding": {"verified": True}},
    {"target": {"kind": "machine", "id": "child"},
     "execution": {"target": {"kind": "machine", "id": "different"}}},
])
def test_invalid_or_forged_target_request_rejected_before_transport(boundary, child_request):
    with pytest.raises(ExecutionRequestError):
        boundary.submit(child_request)
    assert boundary.calls == []


def test_stale_parent_epoch_still_fences_child_transport(boundary):
    boundary.client.health = lambda: {"runtime_epoch": 5}
    with pytest.raises(HostError, match="epoch changed"):
        boundary.submit({"target": {"kind": "machine", "id": "child"}})
    assert boundary.calls == []


@pytest.mark.parametrize("mutation", [None, "actual_target", "resolved_target", "fence", "verification"])
def test_observed_child_binding_is_required_and_mismatch_rejected(boundary, mutation):
    boundary.submit({"target": {"kind": "machine", "id": "local-1"}})
    outgoing = _outgoing(boundary)
    task = _task()["task"]
    task["execution_request"] = outgoing["execution_request"]
    task["input_object_ids"] = outgoing["input_object_ids"]
    task["spec"] = {"spec": outgoing["spec"], "execution_request": outgoing["execution_request"]}
    if mutation in ("actual_target", "resolved_target"):
        task["execution_binding"][mutation]["id"] = "forged-worker"
    elif mutation == "fence":
        task["execution_binding"]["fence"] += 1
    elif mutation == "verification":
        task["execution_binding"]["verification"]["verified"] = False
    if mutation is None:
        assert _execution_contract(task) == outgoing["execution_request"]
        assert task["execution_binding"]["binding_id"] == "binding-contract"
    else:
        with pytest.raises(HostError, match="credential|fence"):
            _execution_contract(task)


class _AdmissionTransport:
    def __init__(self):
        self.calls = []

    def handshake(self, *_args):
        return {"capabilities": [TARGETED_EXECUTION_BINDING_CAPABILITY]}

    def list_capabilities(self, **_kwargs):
        return [[{"capability_id": "fixture.echo", "definition_digest": "sha256:" + "d" * 64}], None]

    def admit_task(self, **kwargs):
        self.calls.append(kwargs)
        return {"task_id": "child", "run_id": "run", "state": "queued"}


def _sdk_submit(transport, route, request, spec):
    if route == "tasks":
        return RemoteTasks(transport).create(project_id="project", capability="fixture.echo", spec=spec,
            input_manifest=[item["object_id"] for item in ORDERED], execution_request=request,
            idempotency_key="sdk-boundary")
    return RemoteAstridClient(transport).invoke("fixture.echo", project_id="project", spec=spec,
        input_object_ids=[item["object_id"] for item in ORDERED], execution_request=request,
        idempotency_key="sdk-boundary")


@pytest.mark.parametrize("route", ["tasks", "client"])
@pytest.mark.parametrize("target", TARGETS + [pytest.param(None, id="no-controls-local")])
def test_sdk_admission_preserves_placement_and_managed_input_assembly(route, target):
    transport = _AdmissionTransport()
    spec = {"inputs": {item["name"]: {key: item[key] for key in ("object_id", "filename")}
                       for item in ORDERED}}
    request = {"target": target} if target is not None else None
    original = copy.deepcopy(spec)
    assert _sdk_submit(transport, route, request, spec).ok
    submitted = transport.calls[0]
    assert spec == original
    assert submitted["input_object_ids"] == [item["object_id"] for item in ORDERED]
    if target is None:
        assert "execution_request" not in submitted
        assert submitted["spec"] == spec
    else:
        assert submitted["execution_request"]["target"] == target
        assert submitted["execution_request"]["inputs"] == [dict(item, required=True) for item in ORDERED]
        assert submitted["spec"]["inputs"]["zeta"]["object_id"] == ORDERED[0]["object_id"]


@pytest.mark.parametrize("route", ["tasks", "client"])
def test_sdk_rejects_caller_binding_before_admission(route):
    transport = _AdmissionTransport()
    result = _sdk_submit(transport, route,
        {"target": {"kind": "machine", "id": "child"}, "execution_binding": {"verified": True}}, {})
    assert not result.ok and result.error.code == "validation_error"
    assert transport.calls == []
