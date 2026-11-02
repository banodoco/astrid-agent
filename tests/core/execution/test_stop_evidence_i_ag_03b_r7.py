"""Cancellation must not turn local lifecycle observations into stop proof."""

import pytest

from astrid.core.execution.generic_host import HostCancelled
from tests.test_generic_host_result_json import host_case


@pytest.mark.parametrize("phase", ["admission", "local_cleanup"])
def test_cancellation_without_independent_stop_keeps_uncertainty(host_case, monkeypatch, phase):
    case = host_case
    task = case.task["task"]
    task.update(status="running", runtime_epoch=7)
    calls = []
    cleaned = []
    run_impl = case.host._run_task_impl

    def cancel(task_id, **kwargs):
        calls.append((task_id, kwargs))
        task.update(status="cancel_requested", waiting_reason="provider_state_unknown")
        return {"task": dict(task)}

    def run(*args, _terminal, **kwargs):
        if phase == "admission":
            assert _terminal["stage"] == "admission"
            _terminal["force_abort"]("cancelled")
            return {"cancelled": True}
        return run_impl(*args, _terminal=_terminal, **kwargs)

    def execute(*args, **kwargs):
        try:
            raise HostCancelled("cancel this invocation")
        finally:
            cleaned.append("local cleanup completed")

    monkeypatch.setattr(case.runtime, "cancel", cancel, raising=False)
    monkeypatch.setattr(case.host, "_run_task_impl", run)
    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    assert case.host.run_task(case.task, lease_token="lease-result")["cancelled"]

    assert cleaned == (["local cleanup completed"] if phase == "local_cleanup" else [])
    assert len(calls) == 1
    task_id, options = calls[0]
    assert task_id == task["id"]
    assert (options["attempt_id"], options["lease_id"], options["fence"], options["runtime_epoch"]) == (
        "attempt-result", "lease-result", 3, 7
    )
    assert options["stop_observed"] is False
    assert options["result"]["status"] == "cancelled"
    assert options["result"]["error"]["code"] == "cancelled"
    assert isinstance(options["outputs"], list)
    assert task["status"] == "cancel_requested"
    assert task["waiting_reason"] == "provider_state_unknown"
    assert not case.runtime.failures
