"""Custody against the pinned disposable Runtime, with exact HTTP mutations."""
import copy
import base64
import hashlib
from pathlib import Path
import json
import threading

import pytest

from astrid.core.execution.generic_host import GenericPackHost, HostError
from astrid.core.execution._child_bridge import ChildBridgeError, task_resource
from astrid.sdk._child_bridge import _BridgeRejected
from tests.integrations.test_h3_delegated_child_claim_135 import (
    claim_world, child_request, unrelated, CHILD,
)

pytestmark = pytest.mark.integration


def test_lost_completion_reply_reconciles_without_opposite_terminal(claim_world, monkeypatch):
    world = claim_world
    calls = []
    settle = world.client.generated.settle_attempt
    def lost(attempt_id, settlement, **kwargs):
        result = settle(attempt_id, settlement, **kwargs)
        calls.append((attempt_id, copy.deepcopy(settlement), kwargs, result))
        raise OSError("committed completion reply lost")
    monkeypatch.setattr(world.client.generated, "settle_attempt", lost)
    failures = []
    monkeypatch.setattr(world.client, "fail", lambda *a, **kw: failures.append((a, kw)))
    terminal = {}
    def execute(*args, _terminal, **kwargs):
        terminal.update(value=_terminal)
        return _terminal["completion_call"](world.client.settle, world.context["task_id"],
            world.context["lease_id"], result={"cpu_fixture": True}, outputs=[], effect=None,
            attempt_id=world.context["attempt_id"], fence=world.context["fence"])
    monkeypatch.setattr(world.host, "_run_task_impl", execute)
    with pytest.raises(OSError, match="reply lost"):
        world.host.run_task({**world.parent, "id": world.context["task_id"]}, lease_token=world.context["lease_id"],
                            attempt_id=world.context["attempt_id"], fence=world.context["fence"], keep_attempt=True)
    assert len(calls) == 1 and failures == []
    current = task_resource(world.client.task(world.context["task_id"]))
    assert current["state"] == "succeeded"
    # Current task state is real, but it cannot prove this completion's request identity.
    assert terminal["value"]["completion_state"] == "unknown"
    root = terminal["value"]["root"]
    receipt = json.loads((root / "result.json").read_text())
    assert not receipt["ok"]
    assert receipt["publication"]["status"] == "unconfirmed"
    assert receipt["completion_diagnostic"] == {"status": "unconfirmed"}
    with pytest.raises(HostError, match="gate is closed or unresolved"):
        world.host._handoff_begin_direct_attempt()


def test_lease_loss_cannot_complete_replacement_attempt(claim_world, monkeypatch):
    from astrid.core.execution import generic_host as gh
    world = claim_world
    old = copy.deepcopy(world.context)
    world.client.fail(old["task_id"], old["lease_id"], "retry fixture", retryable=True,
                      attempt_id=old["attempt_id"], fence=old["fence"])
    world.owner.retry_task(old["task_id"], idempotency_key="custody-retry")
    replacement = world.client.claim_next(executor_id=world.host.executor_id,
        capability_ids=["h3_av.transform"], idempotency_key="custody-replacement")
    assert replacement.task_id == old["task_id"]
    assert replacement.attempt_id != old["attempt_id"]
    world.calls.clear()
    monkeypatch.setattr(gh, "_LEASE_POLL_SECONDS", 0.0)
    terminal = {}
    def execute(*args, _terminal, **kwargs):
        terminal.update(value=_terminal)
        assert _terminal["cancel_signal"].wait(5), "observer missed replacement attempt"
        return _terminal["completion_call"](world.client.settle, old["task_id"], old["lease_id"],
            result={}, outputs=[], effect=None, attempt_id=old["attempt_id"], fence=old["fence"])
    monkeypatch.setattr(world.host, "_run_task_impl", execute)
    with pytest.raises(HostError):
        claimed = {key: value for key, value in world.parent.items() if key != "lease_expires_at"}
        world.host.run_task({**claimed, "id": old["task_id"]}, lease_token=old["lease_id"],
                            attempt_id=old["attempt_id"], fence=old["fence"], keep_attempt=True)
    current = task_resource(world.client.task(old["task_id"]))
    assert current["state"] == "running"
    assert current["attempt_id"] == replacement.attempt_id
    assert terminal["value"]["forced"] == "lease_lost"
    assert not terminal["value"]["pump_thread"].is_alive()
    assert world.calls == []


def test_delegated_child_exact_replay_and_parent_output_custody(claim_world, monkeypatch):
    world = claim_world
    decoy = unrelated(world)
    entered, release = threading.Event(), threading.Event()
    executed = []
    contents = b"verified child output"
    output = {"name": "answer", "filename": "answer.txt", "kind": "object", "media_type": "text/plain",
              "digest": "sha256:" + hashlib.sha256(contents).hexdigest(), "size": len(contents),
              "data_base64": base64.b64encode(contents).decode(), "ordinal": 0, "role": "result"}
    def execute(slot, task, **kwargs):
        executed.append(task["task"]["id"])
        entered.set()
        assert release.wait(5)
        return slot.client.settle(task["task"]["id"], kwargs["lease_token"],
            result={"cpu_fixture": True}, outputs=[output], effect=None,
            attempt_id=kwargs["attempt_id"], fence=kwargs["fence"])
    monkeypatch.setattr(GenericPackHost, "run_task", execute)
    request = child_request(world)
    try:
        first = world.sdk.submit(request)
        assert entered.wait(5)
        assert world.sdk.submit(copy.deepcopy(request)) == first
        assert executed == [first["task_id"]]
        changed = copy.deepcopy(request)
        changed["inputs"] = {"foreign": True}
        with pytest.raises(_BridgeRejected):
            world.sdk.submit(changed)
        assert world.sdk.status("selected", first["task_id"])["state"] == "running"
        # A parent cannot consume an unrelated child's output page.
        with pytest.raises(_BridgeRejected):
            world.sdk.status("selected", decoy["task_id"])
        assert all(call["path"] != "/v1/tasks/claim" for call in world.calls)
        assert task_resource(world.client.task(decoy["task_id"]))["state"] == "queued"
    finally:
        release.set()
        bound = world.source._children.get("selected", {})
        if "claim_thread" in bound:
            bound["claim_thread"].join(5)
    assert world.sdk.status("selected", first["task_id"])["state"] == "succeeded"
    rows = world.sdk.outputs("selected", first["task_id"])
    assert len(rows) == 1
    assert rows[0]["digest"] == output["digest"]
    assert rows[0]["task_id"] == first["task_id"]
    materialized = world.sdk.materialize_output(child_key="selected", task_id=first["task_id"], output=rows[0])
    assert (world.source.output_root / materialized.filename).read_bytes() == contents
    assert world.sdk.materialize_output(child_key="selected", task_id=first["task_id"], output=rows[0]) == materialized
    assert len(world.calls) == 1 and len(executed) == 1


@pytest.mark.parametrize("loss", ["before_dispatch", "revoke_inflight", "parent_failure"])
def test_parent_authority_loss_revokes_children_without_fallback(claim_world, monkeypatch, loss):
    world = claim_world
    if loss == "before_dispatch":
        selected = task_resource(world.client.admit_child(child={"child_id": "selected", "capability_id": CHILD,
            "capability_digest": world.host.capabilities[CHILD].capability_digest}, inputs={}, ordered_inputs=[],
            derived_inputs=[], stage="prepare", input_refs=[], **world.context))
        world.source.revoke()
        monkeypatch.setattr(world.host, "_new_claim_slot", lambda: pytest.fail("revoked parent spawned child"))
        with pytest.raises(ChildBridgeError, match="parent authority ended"):
            world.source._dispatch_local_stage({}, selected)
        assert world.calls == []
        assert task_resource(world.client.task(selected["task_id"]))["state"] == "queued"
        return
    entered = threading.Event()
    slots = []
    def execute(slot, task, **kwargs):
        slots.append(slot)
        entered.set()
        assert slot._shutdown.wait(5), "revocation did not contain delegated execution"
        if loss == "revoke_inflight":
            return slot.client.fail(task["task"]["id"], kwargs["lease_token"], "revoked CPU fixture",
                retryable=False, attempt_id=kwargs["attempt_id"], fence=kwargs["fence"])
        return {"cancelled": True}
    monkeypatch.setattr(GenericPackHost, "run_task", execute)
    selected = world.sdk.submit(child_request(world))
    assert entered.wait(5)
    try:
        if loss == "parent_failure":
            c = world.context
            world.client.fail(c["task_id"], c["lease_id"], "parent authority ended", retryable=False,
                              attempt_id=c["attempt_id"], fence=c["fence"])
            with pytest.raises(ChildBridgeError):
                world.source._live()
        else:
            world.source.revoke()
    finally:
        world.source.revoke()
        world.source._children["selected"]["claim_thread"].join(5)
    assert slots[0]._shutdown.is_set()
    assert not world.source._children["selected"]["claim_thread"].is_alive()
    assert world.host._handoff_active_attempts == 0
    with pytest.raises(ChildBridgeError, match="parent authority ended"):
        world.source._dispatch_local_stage({}, task_resource(world.client.task(selected["task_id"])))
    assert len(world.calls) == 1 and world.calls[0]["path"].endswith("/claim-delegated-child")
    child = task_resource(world.client.task(selected["task_id"]))
    assert child["state"] == ("cancel_requested" if loss == "parent_failure" else "failed")


def test_external_cancellation_acknowledgement_is_not_terminal_proof(claim_world, monkeypatch):
    world = claim_world
    calls, terminals = [], []
    def execute(host, task, *args, _terminal, **kwargs):
        terminals.append((host, _terminal))
        def cancel(task_id, **options):
            calls.append(task_id)
            # Existing owner RPC supplies an acknowledged external request;
            # product code gains no owner credential or fallback.
            current = world.owner.get_task(task_id)
            return world.owner.cancel_task(task_id, expected_version=current.version,
                                           idempotency_key="custody-external-cancel")
        monkeypatch.setattr(host.client, "cancel", cancel)
        monkeypatch.setattr(host.client, "fail", lambda *a, **kw: pytest.fail("opposite failure mutation"))
        _terminal["force_abort"]("cancelled")
        return {"cancelled": True}
    monkeypatch.setattr(GenericPackHost, "_run_task_impl", execute)
    selected = world.sdk.submit(child_request(world))
    world.source._children["selected"]["claim_thread"].join(5)
    assert calls == [selected["task_id"]]
    assert task_resource(world.client.task(selected["task_id"]))["state"] == "cancel_requested"
    host, terminal = terminals[0]
    assert terminal["completion_state"] == "unknown"
    receipt = json.loads((terminal["root"] / "result.json").read_text())
    assert receipt["publication"]["status"] == "unconfirmed"
    assert receipt["completion_diagnostic"] == {"status": "unconfirmed"}
    assert not host._handoff_observation_known
