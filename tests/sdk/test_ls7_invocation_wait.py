"""A negotiated result wait consumes the committed output snapshot directly."""
from types import SimpleNamespace

import pytest

from astrid.sdk import invocation
from astrid.sdk.exceptions import CapabilityInvocationError


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
