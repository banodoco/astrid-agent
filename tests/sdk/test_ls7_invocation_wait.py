"""A negotiated result wait consumes the committed output snapshot directly."""
from types import SimpleNamespace

import pytest

from astrid.sdk import invocation
from astrid.sdk.exceptions import CapabilityInvocationError
from astrid.sdk.contracts import DomainResult, ErrorObject


def test_result_wait_has_no_status_or_output_poll_and_keeps_cursor(monkeypatch):
    calls = []
    task = {"task_id": "task", "run_id": "run", "attempt_id": "attempt", "state": "running"}
    output = {"association_id": "managed", "object_id": "sha256:" + "1" * 64}
    snapshots = iter([
        {"task": task, "cursor": "before", "managed_outputs": {"items": []}},
        {"task": {**task, "state": "succeeded", "result": {"outputs": [output]}},
         "cursor": "after", "managed_outputs": {"items": [output]}},
    ])
    def wait(task_id, **kwargs):
        calls.append((task_id, kwargs))
        return next(snapshots)
    def forbidden(*args, **kwargs):
        pytest.fail("negotiated success path polled or slept")
    client = SimpleNamespace(tasks=SimpleNamespace(show=forbidden, wait=wait,
        supports_wait=lambda: True, list_managed_outputs=forbidden))
    monkeypatch.setattr(invocation.time, "sleep", forbidden)
    result, ok, attempt = invocation._wait_for_kernel_task_loop(client, task_id="task", run_id="run",
        timeout_seconds=30, poll_seconds=10, read_managed_outputs=True, expects_outputs=True)
    assert ok and attempt == "attempt" and result["managed_outputs"] == [output]
    assert [item[1]["cursor"] for item in calls] == [None, "before"]
    assert len(calls) == 2


def test_terminal_snapshot_without_required_associations_is_refused_without_poll(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("missing committed association was hidden by a poll")
    client = SimpleNamespace(tasks=SimpleNamespace(show=forbidden, supports_wait=lambda: True,
        wait=lambda *args, **kwargs: {"task": {"task_id": "task", "state": "succeeded", "result": {}},
                                     "cursor": "terminal", "managed_outputs": {"items": []}},
        list_managed_outputs=forbidden))
    monkeypatch.setattr(invocation.time, "sleep", forbidden)
    with pytest.raises(CapabilityInvocationError, match="committed snapshot lists no managed outputs"):
        invocation._wait_for_kernel_task_loop(client, task_id="task", run_id="run", timeout_seconds=30,
            poll_seconds=1, read_managed_outputs=True, expects_outputs=True)


def test_disconnected_subscription_reauthenticates_and_resumes_same_task_and_cursor(monkeypatch):
    calls, authentications, delays = [], [], []
    task = {"task_id": "task", "run_id": "run", "attempt_id": "attempt", "state": "running"}
    output = {"association_id": "managed", "object_id": "sha256:" + "1" * 64}
    observations = iter([
        {"task": task, "cursor": "before", "managed_outputs": {"items": []}},
        DomainResult.failure(ErrorObject("transport_error", "lost response", {})),
        {"task": {**task, "state": "succeeded", "result": {"outputs": [output]}},
         "cursor": "after-reconnect", "managed_outputs": {"items": [output]}},
    ])
    def wait(task_id, **kwargs):
        calls.append((task_id, kwargs["cursor"]))
        return next(observations)
    def supported(*, refresh=False):
        authentications.append(refresh)
        return True
    def forbidden(*args, **kwargs):
        pytest.fail("reconnect created work or replaced event waiting with polling")
    tasks = SimpleNamespace(show=forbidden, create=forbidden, list_managed_outputs=forbidden,
                            supports_wait=supported, wait=wait)
    monkeypatch.setattr(invocation.time, "sleep", delays.append)
    result, ok, attempt = invocation._wait_for_kernel_task_loop(SimpleNamespace(tasks=tasks),
        task_id="task", run_id="run", timeout_seconds=30, poll_seconds=10,
        read_managed_outputs=True, expects_outputs=True)
    assert ok and attempt == "attempt" and result["managed_outputs"] == [output]
    assert calls == [("task", None), ("task", "before"), ("task", "before")]
    assert authentications == [False, True]
    assert delays == [.05]


@pytest.mark.parametrize("code", ["unauthorized", "wait_cancelled", "invalid_response"])
def test_subscription_refusal_is_not_retried_as_a_disconnect(monkeypatch, code):
    calls = []
    def wait(*args, **kwargs):
        calls.append((args, kwargs))
        return DomainResult.failure(ErrorObject(code, "refused", {}))
    def forbidden(*args, **kwargs):
        pytest.fail("a refusal was polled or retried")
    monkeypatch.setattr(invocation.time, "sleep", forbidden)
    tasks = SimpleNamespace(show=forbidden, wait=wait, supports_wait=lambda: True)
    result, ok, _ = invocation._wait_for_kernel_task_loop(SimpleNamespace(tasks=tasks),
        task_id="task", run_id="run", timeout_seconds=30, poll_seconds=1)
    assert not ok and result["error"]["details"]["code"] == code
    assert len(calls) == 1


@pytest.mark.parametrize("code", ["transport_error", "transport_timeout"])
def test_reconnect_attempts_are_bounded_and_do_not_redispatch(monkeypatch, code):
    calls, authentications, delays = [], [], []
    def wait(task_id, **kwargs):
        calls.append((task_id, kwargs["cursor"]))
        return DomainResult.failure(ErrorObject(code, "offline", {}))
    def supported(*, refresh=False):
        authentications.append(refresh)
        return True
    def forbidden(*args, **kwargs):
        pytest.fail("reconnect attempted another admission or status poll")
    tasks = SimpleNamespace(show=forbidden, create=forbidden, supports_wait=supported, wait=wait)
    monkeypatch.setattr(invocation.time, "sleep", delays.append)
    result, ok, _ = invocation._wait_for_kernel_task_loop(SimpleNamespace(tasks=tasks),
        task_id="task", run_id="run", timeout_seconds=30, poll_seconds=1)
    assert not ok and result["kernel_task_id"] == "task"
    assert calls == [("task", None)] * 4
    assert authentications == [False, True, True, True]
    assert delays == [.05, .1, .2]
