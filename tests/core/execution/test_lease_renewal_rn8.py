"""RN-8: use Runtime's lease policy and expose the accepted renewal's bounds."""
from __future__ import annotations

import inspect
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from astrid.core.execution import generic_host
from astrid_runtime_client import WorkspaceClient
from tests.core.execution.test_lease_pump_logging_rn7 import (
    _ack, _capture_lease_logs, _hold_executor_until, lease_pump_case,
)
from tests.test_generic_host_result_json import _success_boundary, host_case


@pytest.mark.parametrize("remaining", [None, 0.05, 30.0])
def test_host_heartbeat_uses_server_lease_and_bounded_transport(monkeypatch, remaining):
    calls = []

    class Generated:
        def __init__(self, *_args, timeout=30.0):
            self.timeout = timeout

        def health(self):
            return {"runtime_epoch": 7}

        def heartbeat_attempt(self, attempt_id, **payload):
            calls.append((attempt_id, payload, self.timeout))
            return payload

    monkeypatch.setattr("astrid_runtime_client.WorkspaceClient", Generated)
    client = generic_host.RuntimeProtocolClient("http://127.0.0.1:8765", "test-worker")
    client._attempt_runtime_epochs["attempt-rn8"] = 7
    original_timeout = client.generated.timeout
    deadline = None if remaining is None else time.monotonic() + remaining

    client.heartbeat("task-rn8", "lease-rn8", attempt_id="attempt-rn8", fence=3,
                     progress={"phase": "encoding"}, deadline=deadline)

    assert len(calls) == 1
    attempt_id, payload, timeout = calls[0]
    assert attempt_id == "attempt-rn8"
    assert "lease_seconds" not in payload
    assert payload["runtime_epoch"] == 7 and payload["fence"] == 3
    assert payload["progress"] == {"phase": "encoding"}
    assert 0 < timeout <= client.execution_policy.authority_window_seconds / 150
    if remaining is not None:
        assert timeout <= remaining
    assert client.generated.timeout == original_timeout


def test_generated_heartbeat_has_no_client_lease_duration(monkeypatch):
    assert "lease_seconds" not in inspect.signature(WorkspaceClient.heartbeat_attempt).parameters
    client = WorkspaceClient("http://127.0.0.1:8765", "test-worker")
    requests = []

    def request(method, path, **kwargs):
        requests.append((method, path, json.loads(kwargs["body"])))
        return 200, {}, b'{"data":{},"receipt":{}}'

    monkeypatch.setattr(client, "_request", request)
    client.heartbeat_attempt("attempt-rn8", lease_id="lease-rn8", fence=3,
                             runtime_epoch=7, idempotency_key="heartbeat-rn8")

    assert requests == [("POST", "/v1/attempts/attempt-rn8/heartbeat",
                         {"lease_id": "lease-rn8", "fence": 3, "runtime_epoch": 7})]


def test_ack_logs_effective_attempt_deadline_and_exact_lease_expiry(
    lease_pump_case, monkeypatch, capsys,
):
    case = lease_pump_case
    deadline = datetime.now(timezone.utc) + timedelta(minutes=10)
    case.task["task"]["deadline_at"] = deadline.isoformat()
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()

    def heartbeat(*args, **kwargs):
        result = _ack(*args, **kwargs)
        result.lease_expires_at = expiry
        return result

    monkeypatch.setattr(case.runtime, "heartbeat", heartbeat)
    rows, ack_seen = _capture_lease_logs(monkeypatch, "heartbeat_acknowledged")
    _success_boundary(case, monkeypatch)
    _hold_executor_until(case.host, ack_seen)
    result = case.host.run_task(case.task, lease_token="lease-result")

    assert result["task"]["status"] == "completed"
    acknowledgements = [row for row in rows if row["category"] == "heartbeat_acknowledged"]
    assert acknowledgements
    for row in acknowledgements:
        assert row["lease_expires_at"] == expiry
        assert abs((datetime.fromisoformat(row["deadline_at"]) - deadline).total_seconds()) < 0.1
        assert 0 < row["deadline_remaining_seconds"] <= 600
    stderr = capsys.readouterr().err
    serialized = [json.loads(line.removeprefix("generic host: lease "))
                  for line in stderr.splitlines() if line.startswith("generic host: lease ")]
    assert any(row.get("lease_expires_at") == expiry and row.get("deadline_at")
               for row in serialized if row["category"] == "heartbeat_acknowledged")
    assert "lease-result" not in stderr


def test_deadline_diagnostics_failure_cannot_stop_renewal(lease_pump_case, monkeypatch):
    case = lease_pump_case
    renewed = threading.Event()

    def heartbeat(*args, **kwargs):
        result = _ack(*args, **kwargs)
        renewed.set()
        return result

    def broken_log(**_kwargs):
        raise OSError("test diagnostic sink unavailable")

    monkeypatch.setattr(case.runtime, "heartbeat", heartbeat)
    monkeypatch.setattr(generic_host, "log_lease_diagnostic", broken_log)
    _success_boundary(case, monkeypatch)
    _hold_executor_until(case.host, renewed)

    result = case.host.run_task(case.task, lease_token="lease-result")

    assert renewed.is_set()
    assert result["task"]["status"] == "completed"
