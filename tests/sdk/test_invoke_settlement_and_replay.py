"""F-053: wait returns only when terminal AND outputs are listable.

F-054: idempotent replays are labelled, and a failed replay is re-run only on request.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from astrid.sdk import invocation
from astrid.sdk.exceptions import CapabilityInvocationError
from astrid.sdk.results import InvocationResult

TASK = "task-1"
RUN = "run-1"
OUTPUT_ROW = {"output_port": "result", "ordinal": 0, "role": "result", "handle": "h1", "digest": "sha256:" + "a" * 64}


def _fresh_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def _old_time() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()


class FakeTasks:
    """Scripted task status and managed-output listings for the wait loop."""

    def __init__(self, states, listings):
        self._states = list(states)
        self._listings = list(listings)
        self.show_calls = 0
        self.list_calls = 0

    def show(self, task_id):
        self.show_calls += 1
        state = self._states[min(self.show_calls, len(self._states)) - 1]
        return SimpleNamespace(ok=True, data={
            "task_id": task_id, "state": state, "attempt_id": "att-1",
            "result": {"outputs": []},
        })

    def list_managed_outputs(self, task_id):
        self.list_calls += 1
        rows = self._listings[min(self.list_calls, len(self._listings)) - 1]
        return SimpleNamespace(ok=True, data=(list(rows), None))


def _client(tasks):
    return SimpleNamespace(tasks=tasks)


def _short_settle(monkeypatch, sleeps=None):
    """Keep the bounded poll real but fast; record each back-off sleep."""
    original = invocation._settle_task_managed_outputs
    real_sleep = invocation.time.sleep

    def sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)
        real_sleep(0)

    monkeypatch.setattr(invocation.time, "sleep", sleep)
    monkeypatch.setattr(
        invocation,
        "_settle_task_managed_outputs",
        lambda client, task_id, **kw: original(
            client, task_id, expects_outputs=kw["expects_outputs"],
            timeout_seconds=0.05, poll_seconds=0.001,
        ),
    )


def _declared_capability():
    return SimpleNamespace(
        id="test.capability",
        capability_type="executor",
        native_kind="built_in",
        inputs=(),
        outputs=(SimpleNamespace(name="result", type="file"),),
    )


def _wait(client, **kwargs):
    return invocation._wait_for_kernel_task(
        client, task_id=TASK, run_id=RUN, timeout_seconds=5, poll_seconds=0.001,
        read_managed_outputs=True, expects_outputs=True, **kwargs,
    )


@pytest.mark.parametrize("placement,expected", [("runpod", [TASK]), ("local", [])])
def test_ctrl_c_cancels_only_the_known_runpod_generation_task(monkeypatch, placement, expected):
    cancelled = []
    task = {"state": "running", "spec": {"capability_id": "vibecomfy.run", "inputs": {
        "placement_policy": json.dumps({"kind": placement})}}}
    tasks = SimpleNamespace(show=lambda task_id: SimpleNamespace(ok=True, data=task),
                            cancel=lambda task_id, **kwargs: cancelled.append(task_id))
    monkeypatch.setattr(invocation.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        _wait(SimpleNamespace(tasks=tasks))
    assert cancelled == expected


def test_ctrl_c_without_a_runtime_cancel_api_preserves_the_interrupt(monkeypatch):
    task = {"state": "running", "spec": {"capability_id": "vibecomfy.run", "inputs": {
        "placement_policy": json.dumps({"kind": "runpod"})}}}
    tasks = SimpleNamespace(show=lambda task_id: SimpleNamespace(ok=True, data=task))
    monkeypatch.setattr(invocation.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        _wait(SimpleNamespace(tasks=tasks))


@pytest.mark.parametrize("placement,capacity_timeout,complete,limit", [
    ("runpod", None, True, None), ("runpod", 10, False, 3610), ("local", None, False, 3600),
])
def test_runpod_capacity_follow_preserves_other_task_wait_limits(monkeypatch, capsys,
                                                               placement, capacity_timeout, complete, limit):
    now = [0.0]
    policy = {"kind": placement, "wait_for_capacity": True, "capacity_timeout_seconds": capacity_timeout}

    def show(task_id):
        state = "succeeded" if complete and now[0] >= 3700 else "running"
        return SimpleNamespace(ok=True, data={"state": state, "result": {"outputs": []},
            "spec": {"capability_id": "vibecomfy.run", "inputs": {"placement_policy": json.dumps(policy)}}})

    monkeypatch.setattr(invocation.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(invocation.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    raw, ok, _ = invocation._wait_for_kernel_task(
        SimpleNamespace(tasks=SimpleNamespace(show=show)), task_id=TASK, run_id=RUN,
        timeout_seconds=3600, poll_seconds=60)
    if complete:
        assert ok and now[0] > 3600
    else:
        assert not ok and raw["error"]["code"] == "task_wait_timeout"
        assert raw["error"]["details"]["timeout_seconds"] == limit and now[0] == limit
    heartbeat = capsys.readouterr().err
    assert ("Still following RunPod" in heartbeat) is (placement == "runpod")


def test_host_capacity_timeout_is_reported_before_the_sdk_wait_limit(monkeypatch):
    now = [0.0]
    policy = {"kind": "runpod", "capacity_timeout_seconds": 10}

    def show(task_id):
        return SimpleNamespace(ok=True, data={"state": "failed" if now[0] >= 10 else "running",
            "result": {"error": "RunPod capacity timeout passed"},
            "spec": {"capability_id": "vibecomfy.run", "inputs": {"placement_policy": json.dumps(policy)}}})

    monkeypatch.setattr(invocation.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(invocation.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    raw, ok, _ = invocation._wait_for_kernel_task(SimpleNamespace(tasks=SimpleNamespace(show=show)),
        task_id=TASK, run_id=RUN, timeout_seconds=3600, poll_seconds=1)
    assert not ok and now[0] == 10 and raw["error"]["code"] == "task_failed"
    assert "capacity timeout" in raw["error"]["message"]


# ---- F-053: wait ------------------------------------------------------------

def test_wait_returns_only_at_terminal_with_outputs_present(monkeypatch):
    _short_settle(monkeypatch)
    tasks = FakeTasks(states=["queued", "running", "succeeded"], listings=[[OUTPUT_ROW]])
    raw, ok, attempt = _wait(_client(tasks))
    assert ok is True
    assert raw["state"] == "completed"
    assert raw["managed_outputs"] == [OUTPUT_ROW]
    assert tasks.show_calls == 3
    assert attempt == "att-1"


def test_outputs_that_settle_late_are_polled_then_succeed(monkeypatch):
    sleeps = []
    _short_settle(monkeypatch, sleeps)
    tasks = FakeTasks(states=["succeeded"], listings=[[], [], [OUTPUT_ROW]])
    raw, ok, _ = _wait(_client(tasks))
    assert ok is True
    assert raw["managed_outputs"] == [OUTPUT_ROW]
    assert tasks.list_calls == 3
    assert len(sleeps) == 2


def test_outputs_that_never_appear_raise_naming_the_task(monkeypatch):
    _short_settle(monkeypatch)
    tasks = FakeTasks(states=["succeeded"], listings=[[]])
    with pytest.raises(CapabilityInvocationError) as exc:
        _wait(_client(tasks))
    assert TASK in str(exc.value)
    assert exc.value.details["task_id"] == TASK
    assert "managed outputs" in str(exc.value)


def test_undeclared_outputs_may_legitimately_be_empty(monkeypatch):
    _short_settle(monkeypatch)
    tasks = FakeTasks(states=["succeeded"], listings=[[]])
    raw, ok, _ = invocation._wait_for_kernel_task(
        _client(tasks), task_id=TASK, run_id=RUN, timeout_seconds=5,
        poll_seconds=0.001, read_managed_outputs=True, expects_outputs=False,
    )
    assert ok is True
    assert raw["managed_outputs"] == []
    assert tasks.list_calls == 1


def test_invoke_wait_with_declared_outputs_raises_when_listing_stays_empty(monkeypatch):
    _short_settle(monkeypatch)
    capability = _declared_capability()
    _stub_action_preflight(monkeypatch)
    monkeypatch.setattr(invocation, "_sdk_module", lambda: _fake_sdk(capability))
    tasks = FakeTasks(states=["succeeded"], listings=[[]])
    monkeypatch.setattr(invocation, "_kernel_invoke", lambda cap, **kw: (RUN, TASK, "att-1", None, {"ok": True}, True, None))
    with pytest.raises(CapabilityInvocationError, match=TASK):
        invocation.invoke(
            capability.id, kind="executor", project="p", wait=True, client=_client(tasks),
        )


def _stub_action_preflight(monkeypatch):
    """Bypass definition-level input validation for the SimpleNamespace capability."""
    from astrid.sdk import actions

    monkeypatch.setattr(actions, "validate_action_inputs", lambda *_a, **_k: None)


def _fake_sdk(capability):
    definition = SimpleNamespace(to_dict=lambda: {"id": capability.id})
    return SimpleNamespace(
        _load_registries=lambda **_: ({capability.id: definition}, None, None),
        get_capability=lambda *_, **__: capability,
    )


# ---- F-054: replay labels and failed replays --------------------------------

def _admit_kernel(monkeypatch, responses):
    """Run the real admission path against scripted create() responses."""
    calls = []
    queue = list(responses)

    def create(**kwargs):
        calls.append(kwargs)
        return queue.pop(0)

    client = SimpleNamespace(tasks=SimpleNamespace(create=create))
    return client, calls


def _resp(state, created_at, task_id=TASK):
    return SimpleNamespace(ok=True, data={
        "task_id": task_id, "run_id": RUN, "attempt_id": "att-1",
        "state": state, "created_at": created_at,
    })


def _capability():
    return SimpleNamespace(id="test.capability", capability_type="executor", native_kind="built_in", inputs=())


def test_replayed_success_is_labelled_with_earlier_task():
    client, calls = _admit_kernel(None, [_resp("succeeded", _old_time())])
    _, task_id, _, _, raw, ok, _ = invocation._kernel_invoke(
        _capability(), kind="executor", project="p", inputs={}, outputs={}, _client=client,
    )
    assert ok is True
    assert task_id == TASK
    assert raw["replayed"]["task_id"] == TASK
    assert raw["replayed"]["created_at"]
    assert len(calls) == 1


def test_fresh_admission_is_not_labelled_as_replay():
    client, _ = _admit_kernel(None, [_resp("queued", _fresh_time())])
    _, _, _, _, raw, ok, _ = invocation._kernel_invoke(
        _capability(), kind="executor", project="p", inputs={}, outputs={}, _client=client,
    )
    assert ok is True
    assert "replayed" not in raw


def test_replayed_failed_task_is_not_silently_rerun():
    client, calls = _admit_kernel(None, [_resp("failed", _old_time())])
    _, task_id, _, _, raw, ok, _ = invocation._kernel_invoke(
        _capability(), kind="executor", project="p", inputs={}, outputs={}, _client=client,
    )
    assert ok is False
    assert task_id == TASK
    assert raw["error"]["code"] == "replayed_failed_task"
    assert "rerun=True" in raw["error"]["message"]
    assert raw["replayed"]["task_id"] == TASK
    assert len(calls) == 1


def test_replayed_failed_task_reruns_with_fresh_key_when_asked():
    fresh = _resp("queued", _fresh_time(), task_id="task-2")
    client, calls = _admit_kernel(None, [_resp("failed", _old_time()), fresh])
    _, task_id, _, _, raw, ok, _ = invocation._kernel_invoke(
        _capability(), kind="executor", project="p", inputs={}, outputs={}, _client=client,
        rerun=True,
    )
    assert ok is True
    assert task_id == "task-2"
    assert "replayed" not in raw
    assert len(calls) == 2
    assert calls[1]["deterministic_idempotency"] is False
    assert calls[1]["idempotency_key"] and calls[1]["idempotency_key"] != calls[0].get("idempotency_key")


def test_invoke_exposes_replay_label_on_the_result(monkeypatch):
    capability = _declared_capability()
    _stub_action_preflight(monkeypatch)
    monkeypatch.setattr(invocation, "_sdk_module", lambda: _fake_sdk(capability))
    replay = {"task_id": TASK, "run_id": RUN, "state": "succeeded", "created_at": "2020-01-01T00:00:00+00:00"}
    monkeypatch.setattr(
        invocation, "_kernel_invoke",
        lambda cap, **kw: (RUN, TASK, "att-1", None, {"ok": True, "replayed": replay}, True, None),
    )
    result = invocation.invoke(capability.id, kind="executor", project="p")
    assert isinstance(result, InvocationResult)
    assert result.replayed is True
    assert result.replayed_task_id == TASK
    assert result.replayed_from == "2020-01-01T00:00:00+00:00"
    assert "replayed task task-1 from 2020-01-01T00:00:00+00:00" in str(result)


def test_fresh_invocation_result_is_not_labelled(monkeypatch):
    capability = _declared_capability()
    _stub_action_preflight(monkeypatch)
    monkeypatch.setattr(invocation, "_sdk_module", lambda: _fake_sdk(capability))
    monkeypatch.setattr(
        invocation, "_kernel_invoke",
        lambda cap, **kw: (RUN, TASK, "att-1", None, {"ok": True}, True, None),
    )
    result = invocation.invoke(capability.id, kind="executor", project="p")
    assert result.replayed is False
    assert "replayed task" not in str(result)


# ---- replay detection on the recovery admission path -------------------------

class _FakeReceipt:
    def __init__(self):
        self.frozen = []
        self.located = []

    def freeze(self, admission, *, metadata, replace=False):
        assert replace or not self.frozen, "already frozen"
        self.frozen.append(dict(admission))

    def locate(self, task):
        self.located.append(dict(task))


def _recovery_client(responses):
    queue = list(responses)
    prepared = []

    def prepare(**kwargs):
        prepared.append(kwargs)
        return SimpleNamespace(ok=True, data={**kwargs, "idempotency_key": kwargs.get("idempotency_key") or "det-key"})

    tasks = SimpleNamespace(
        create=lambda **_: None, prepare_admission=prepare,
        dispatch_admission=lambda args: queue.pop(0),
    )
    return SimpleNamespace(tasks=tasks), prepared


def test_recovery_path_labels_replayed_success():
    client, _ = _recovery_client([_resp("succeeded", _old_time())])
    receipt = _FakeReceipt()
    _, _, _, _, raw, ok, _ = invocation._kernel_invoke(
        _capability(), kind="executor", project=None, inputs={}, outputs={}, _client=client,
        _recovery=receipt,
    )
    assert ok is True
    assert raw["replayed"]["task_id"] == TASK
    assert len(receipt.located) == 1


def test_recovery_path_refuses_replayed_failed_task_then_reruns_on_request():
    client, _ = _recovery_client([_resp("failed", _old_time())])
    _, _, _, _, raw, ok, _ = invocation._kernel_invoke(
        _capability(), kind="executor", project=None, inputs={}, outputs={}, _client=client,
        _recovery=_FakeReceipt(),
    )
    assert ok is False and raw["error"]["code"] == "replayed_failed_task"

    fresh = _resp("queued", _fresh_time(), task_id="task-2")
    client, prepared = _recovery_client([_resp("failed", _old_time()), fresh])
    receipt = _FakeReceipt()
    _, task_id, _, _, raw, ok, _ = invocation._kernel_invoke(
        _capability(), kind="executor", project=None, inputs={}, outputs={}, _client=client,
        _recovery=receipt, rerun=True,
    )
    assert ok is True and task_id == "task-2" and "replayed" not in raw
    assert len(receipt.frozen) == 2
    assert prepared[1]["deterministic_idempotency"] is False and prepared[1]["idempotency_key"]
