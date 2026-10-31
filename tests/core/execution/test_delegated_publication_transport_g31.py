"""Offline transport checks; Runtime remains the managed-lineage authority."""
from __future__ import annotations

import copy
import threading
from types import SimpleNamespace

import pytest

from astrid.core.execution._child_bridge import ChildBridgeError
from astrid.core.execution.generic_host import HostError, RuntimeProtocolClient

OID = "sha256:" + "a" * 64
PIN = "sha256:" + "b" * 64


def admission_args():
    return {
        "child": {"child_id": "attempt-bound-child", "capability_id": "test.child", "capability_digest": PIN},
        "inputs": {"prompt": {"nested": ["producer data"]}},
        "ordered_inputs": [{"name": "media", "object_id": OID, "filename": "media.zip", "required": True}],
        "derived_inputs": [], "task_id": "parent-task", "attempt_id": "parent-attempt",
        "lease_id": "parent-lease", "fence": 7, "runtime_epoch": 11,
        "run_id": "parent-run", "project_id": "project", "stage": "compose",
        "input_refs": [{"name": "media", "root_object_id": OID}],
        "generation_intent": {"metadata": {"h3_av": {"producer": "opaque"}}},
    }


def transport(*, receipt_changes=None, issue_hook=None, admit_error=None, epoch=11, heartbeat_error=None):
    calls = []
    expected_effect = {"effect_type": "generation.publish_v1", "payload": {"runtime": "bound"}}

    def heartbeat(*args, **kwargs):
        calls.append(("heartbeat", args, copy.deepcopy(kwargs)))
        if heartbeat_error:
            raise heartbeat_error

    def issue(attempt_id, **kwargs):
        calls.append(("issue", attempt_id, copy.deepcopy(kwargs)))
        if issue_hook:
            issue_hook()
        receipt = {"authority": "private-runtime-authority", "parent_task_id": "parent-task",
                   "parent_attempt_id": "parent-attempt"}
        if "child" in kwargs:
            receipt.update(child=kwargs["child"], derived_inputs=kwargs["derived_inputs"])
        receipt.update(receipt_changes or {})
        return receipt

    def admit(**kwargs):
        calls.append(("admit", copy.deepcopy(kwargs)))
        if admit_error:
            raise admit_error
        return {"task": {"task_id": "child-task", "expected_effect": expected_effect}}

    client = RuntimeProtocolClient.__new__(RuntimeProtocolClient)
    client._heartbeat_lock = threading.RLock()
    client.heartbeat = heartbeat
    client._claimed_runtime_epoch = lambda attempt_id: epoch
    client.generated = SimpleNamespace(issue_child_authority=issue, admit_delegated_task=admit)
    return client, calls, expected_effect


def test_staged_transport_uses_parent_authority_and_runtime_owned_descriptors():
    client, calls, expected_effect = transport()
    args = admission_args()
    result = client.admit_child(**args)
    assert [row[0] for row in calls] == ["heartbeat", "issue", "admit"]
    assert calls[1] == ("issue", "parent-attempt", {
        "lease_id": "parent-lease", "fence": 7, "runtime_epoch": 11})
    wire = calls[2][1]
    assert wire["authority"] == "private-runtime-authority"
    assert wire["idempotency_key"] == args["child"]["child_id"]
    task = wire["task"]
    assert task["stage"] == "compose" and task["input_refs"] == args["input_refs"]
    assert task["generation_intent"] == args["generation_intent"]
    assert task["execution_request"] == {"schema_version": 1, "target": {"kind": "default"}}
    assert task["spec"]["inputs"]["media"] == {
        "object_id": OID, "filename": "media.zip", "required": True}
    assert not {"input_object_ids", "settlement_effect", "expected_effect"} & task.keys()
    assert not {"delegated_inputs", "delegated_parent", "verified_publication_source"} & task["spec"].keys()
    assert result["task"]["expected_effect"] is expected_effect


def test_producer_association_ref_is_forwarded_for_runtime_resolution():
    client, calls, _ = transport()
    args = admission_args()
    args["input_refs"] = [{"name": "media", "producer_task_id": "actual-child-task",
                           "association_id": "actual-managed-association", "output_port": "candidate"}]
    client.admit_child(**args)
    assert calls[-1][1]["task"]["input_refs"] == args["input_refs"]
    # No lookup, substitution or local producer filename is used as authority.
    assert set(calls[-1][1]["task"]["input_refs"][0]) == {
        "name", "producer_task_id", "association_id", "output_port"}


def test_staged_producer_data_and_idempotency_are_snapshotted_before_authority_io():
    args = admission_args()

    def mutate():
        args["inputs"]["prompt"]["nested"].append("changed")
        args["input_refs"][0]["root_object_id"] = "sha256:" + "c" * 64
        args["generation_intent"]["metadata"]["h3_av"]["producer"] = "changed"
        args["child"]["child_id"] = "changed-key"

    client, calls, _ = transport(issue_hook=mutate)
    client.admit_child(**args)
    wire = calls[-1][1]
    assert wire["idempotency_key"] == "attempt-bound-child"
    assert wire["task"]["input_refs"][0]["root_object_id"] == OID
    assert wire["task"]["spec"]["inputs"]["prompt"]["nested"] == ["producer data"]
    assert wire["task"]["generation_intent"]["metadata"]["h3_av"]["producer"] == "opaque"


@pytest.mark.parametrize("change", [
    {"stage": None}, {"input_refs": None}, {"stage": "../compose"},
    {"derived_inputs": [{"filename": "untrusted.zip"}]},
    {"input_refs": [{"name": "media", "filename": "nested/file.zip"}]},
    {"input_refs": [{"name": "media", "root_object_id": "nested/file.zip"}]},
    {"input_refs": [{"name": "media", "root_object_id": OID, "authority": "fake"}]},
    {"input_refs": [{"name": "media", "producer_task_id": "task", "association_id": "assoc", "output_port": "out", "object_id": OID}]},
    {"ordered_inputs": [{"name": "wrong", "object_id": OID, "filename": "media.zip", "required": True}]},
    {"ordered_inputs": [{"name": "media", "object_id": OID, "filename": "media.zip", "required": 1}]},
    {"ordered_inputs": [{"name": "media", "object_id": "nested/media.zip", "filename": "media.zip", "required": True}]},
    {"ordered_inputs": [{"name": "media", "object_id": OID, "filename": "nested/media.zip", "required": True}]},
    {"ordered_inputs": [{"name": "media", "object_id": OID, "digest": "sha256:" + "c" * 64, "filename": "media.zip", "required": True}]},
    {"generation_intent": ["not an object"]},
    {"child": {"child_id": "child", "capability_id": "test.child", "capability_digest": PIN, "settlement_effect": {}}},
])
def test_mixed_or_unmanaged_stage_requests_fail_before_runtime_mutation(change):
    client, calls, _ = transport()
    with pytest.raises((ChildBridgeError, ValueError)):
        client.admit_child(**(admission_args() | change))
    assert calls == []


@pytest.mark.parametrize("changes", [
    {"parent_task_id": "foreign"}, {"parent_attempt_id": "foreign"}, {"authority": ""},
    {"child": {}}, {"derived_inputs": []},
])
def test_staged_transport_rejects_foreign_or_derived_authority_receipt(changes):
    client, calls, _ = transport(receipt_changes=changes)
    with pytest.raises(ChildBridgeError, match="authority|grants"):
        client.admit_child(**admission_args())
    assert [row[0] for row in calls] == ["heartbeat", "issue"]


def test_unknown_admission_outcome_is_propagated_without_fallback_or_retry():
    failure = TimeoutError("unknown admission outcome")
    client, calls, _ = transport(admit_error=failure)
    with pytest.raises(TimeoutError) as raised:
        client.admit_child(**admission_args())
    assert raised.value is failure
    assert [row[0] for row in calls] == ["heartbeat", "issue", "admit"]


@pytest.mark.parametrize("mode", ["epoch", "heartbeat"])
def test_lost_parent_authority_prevents_child_authority_or_admission(mode):
    client, calls, _ = transport(epoch=12 if mode == "epoch" else 11,
                                heartbeat_error=HostError("lease lost") if mode == "heartbeat" else None)
    with pytest.raises(HostError):
        client.admit_child(**admission_args())
    assert [row[0] for row in calls] == ["heartbeat"]


def test_unstaged_transport_retains_exact_child_grant_and_opaque_generation_intent():
    client, calls, expected_effect = transport()
    args = admission_args()
    args.pop("stage")
    args.pop("input_refs")
    # A producer metadata field does not become a task settlement authority.
    args["generation_intent"]["expected_effect"] = {"fake": "producer metadata"}
    result = client.admit_child(**args)
    assert calls[1][2]["child"] == args["child"]
    assert calls[1][2]["derived_inputs"] == []
    task = calls[-1][1]["task"]
    assert task["input_object_ids"] == [OID]
    assert task["execution_request"]["inputs"][0]["object_id"] == OID
    assert task["generation_intent"] == args["generation_intent"]
    assert "expected_effect" not in task and "settlement_effect" not in task
    assert result["task"]["expected_effect"] is expected_effect


def test_stage_replay_keeps_explicit_attempt_scoped_key_and_refs():
    client, calls, _ = transport()
    args = admission_args()
    client.admit_child(**args)
    client.admit_child(**args)
    admissions = [row[1] for row in calls if row[0] == "admit"]
    assert len(admissions) == 2
    assert admissions[0] == admissions[1]
    # Runtime, rather than another Agent ledger, owns duplicate suppression.
    assert admissions[0]["idempotency_key"] == "attempt-bound-child"
