from __future__ import annotations

import io
import json
import time

from astrid_runtime_client import generated

from astrid.sdk.command_timing import TimingCollector, timing_scope
from astrid.sdk.workspace_client import WORKSPACE_REQUEST_TIMEOUT_SECONDS, WorkspaceClient


class _HealthResponse(io.BytesIO):
    status = 200
    headers = {}


def test_workspace_client_uses_long_timeout_and_reports_delayed_http_wait(monkeypatch, capsys) -> None:
    observed = []

    def delayed_open(request, *, timeout):
        started = time.monotonic()
        time.sleep(2.2)
        observed.append((request.full_url, timeout, time.monotonic() - started))
        body = json.dumps({
            "status": "ok",
            "protocol": "workspace.v1",
            "schema_digest": "test",
            "runtime_epoch": 1,
            "runtime_session_id": "session",
            "runtime_instance_id": "instance",
            "identity": None,
        }).encode()
        return _HealthResponse(body)

    monkeypatch.setattr(generated, "_urlopen_loopback", delayed_open)
    client = WorkspaceClient("http://127.0.0.1:8123", "test-token")
    collector = TimingCollector()
    with timing_scope(collector):
        health = client.health()

    assert health.status == "ok"
    assert len(observed) == 1
    assert observed[0][0] == "http://127.0.0.1:8123/v1/health"
    assert observed[0][1] == 300.0
    assert observed[0][1] == WORKSPACE_REQUEST_TIMEOUT_SECONDS
    assert observed[0][2] > 2.0
    waiting = [line for line in capsys.readouterr().err.splitlines() if "waiting" in line.lower()]
    assert len(waiting) == 1
