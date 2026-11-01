"""Cancellation must not turn local lifecycle observations into stop proof."""

import json

import pytest

from astrid.core.execution.generic_host import HostCancelled, HostError
from tests.test_generic_host_result_json import host_case


@pytest.mark.parametrize("phase", ["admission", "local_cleanup"])
def test_cancellation_without_independent_stop_keeps_uncertainty(host_case, monkeypatch, phase):
    case = host_case
    task = case.task["task"]
    task.update(status="running", runtime_epoch=7)
    calls = []
    cleaned = []
    captured = {}
    run_impl = case.host._run_task_impl

    def cancel(task_id, **kwargs):
        calls.append((task_id, kwargs))
        # Model the published Runtime's pending response for a fenced attempt
        # when the producer stop is not independently observed.
        task.update(status="cancel_requested", waiting_reason="provider_state_unknown")
        return {"task": dict(task)}

    def run(*args, _terminal, **kwargs):
        captured["terminal"] = _terminal
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
    if phase == "admission":
        assert case.host.run_task(case.task, lease_token="lease-result")["cancelled"]
    else:
        with pytest.raises(HostError, match="Runtime terminal evidence publication pending"):
            case.host.run_task(case.task, lease_token="lease-result")

    assert cleaned == (["local cleanup completed"] if phase == "local_cleanup" else [])
    assert len(calls) == 1
    task_id, options = calls[0]
    assert task_id == task["id"]
    assert (options["attempt_id"], options["lease_id"], options["fence"], options["runtime_epoch"]) == (
        "attempt-result", "lease-result", 3, 7
    )
    assert options["stop_observed"] is False
    assert task["status"] == "cancel_requested"
    assert task["waiting_reason"] == "provider_state_unknown"
    assert captured["terminal"]["completion_state"] == "unknown"
    assert not case.host._handoff_observation_known
    assert "Runtime completion unconfirmed; attempt retained" in case.host._unresolved_task_scopes
    receipt = json.loads((case.root / "result.json").read_text())
    assert receipt["publication"]["status"] == "unconfirmed"
    assert not case.runtime.failures
