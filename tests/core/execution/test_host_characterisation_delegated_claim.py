"""HM1 R02: fresh parent authority precedes the exact delegated-child claim.

The RuntimeProtocolClient is real; its generated transport is a recording
double. This pins the host adapter, not Runtime's signature verification.
"""
from __future__ import annotations

import pytest

from astrid.core.execution._child_bridge import ChildBridgeError
from tests.core.execution.test_runtime_epoch_fence import _client


@pytest.mark.parametrize("foreign_field", [None, "parent_task_id", "parent_attempt_id", "authority"])
def test_r02_delegated_claim_uses_locked_parent_receipt_and_captures_child_epoch(monkeypatch, foreign_field):
    client = _client()
    parent_claim = client.claim_next(
        executor_id="parent-worker", capability_ids=["render.basic"], idempotency_key="parent-claim",
    )
    parent = {
        "task_id": parent_claim["task_id"], "attempt_id": parent_claim["attempt_id"],
        "lease_id": parent_claim["lease_id"], "fence": parent_claim["fence"],
        "runtime_epoch": parent_claim["runtime_epoch"],
    }
    receipt = {
        "parent_task_id": parent["task_id"], "parent_attempt_id": parent["attempt_id"],
        "authority": "fresh-parent-authority",
    }
    if foreign_field is not None:
        receipt[foreign_field] = "" if foreign_field == "authority" else "foreign-parent"
    child_claim = {
        "task_id": "child-task", "attempt_id": "child-attempt", "lease_id": "child-lease",
        "fence": 1, "runtime_epoch": 3,
    }
    calls = []

    def issue_authority(attempt_id, **kwargs):
        calls.append(("issue", attempt_id, kwargs, client._heartbeat_lock.locked()))
        return receipt

    def claim_child(task_id, **kwargs):
        calls.append(("claim", task_id, kwargs, client._heartbeat_lock.locked()))
        return child_claim

    def forbid_queue_claim(**kwargs):
        pytest.fail("delegated child must never select work from the queue")

    monkeypatch.setattr(client.generated, "issue_child_authority", issue_authority, raising=False)
    monkeypatch.setattr(client.generated, "claim_delegated_child", claim_child, raising=False)
    monkeypatch.setattr(client.generated, "claim_task", forbid_queue_claim)
    target = {"kind": "local", "executor_id": "child-worker"}
    request = {
        "executor_id": "child-worker", "capability_ids": ["render.child"],
        "target": target, "parent": parent, "idempotency_key": "exact-child-claim",
    }
    expected_issue = (
        "issue", "attempt-1", {"lease_id": "lease-1", "fence": 2, "runtime_epoch": 3}, True,
    )
    if foreign_field is not None:
        with pytest.raises(ChildBridgeError, match="^foreign delegated child claim authority$"):
            client.claim_delegated_child("child-task", **request)
        assert calls == [expected_issue]
        assert client._attempt_runtime_epochs == {"attempt-1": 3}
    else:
        assert client.claim_delegated_child("child-task", **request) is child_claim
        assert calls == [expected_issue, (
            "claim", "child-task", {
                "executor_id": "child-worker", "capability_ids": ["render.child"],
                "target": target, "runtime_epoch": 3, "authority": "fresh-parent-authority",
                "idempotency_key": "exact-child-claim",
            }, True,
        )]
        assert calls[1][2]["target"] is not target
        assert client._attempt_runtime_epochs == {"attempt-1": 3, "child-attempt": 3}
    assert client.generated.health_reads == 2
    assert client.generated.heartbeats == [], "issuing child authority must not renew the parent lease"
    assert not client._heartbeat_lock.locked()
