from __future__ import annotations

import json
import time

from astrid.sdk.command_timing import TimingCollector, stage, timing_scope
from astrid.sdk.workspace_client import WorkspaceClient


def test_stage_reports_known_held_duration() -> None:
    collector = TimingCollector()
    start = time.monotonic()
    with timing_scope(collector):
        with stage("known_hold"):
            time.sleep(0.2)
    elapsed = time.monotonic() - start

    measured = next(item["seconds"] for item in collector.timings if item["stage"] == "known_hold")
    assert 0.9 * elapsed <= measured <= 1.1 * elapsed


def test_delayed_runtime_request_prints_one_waiting_line(capsys) -> None:
    client = WorkspaceClient("http://127.0.0.1:8123", "test-token")
    requests = []

    def transport(method, path, headers, body):
        requests.append((method, path))
        time.sleep(2.3)
        response = {
            "status": "ok", "protocol": "test", "schema_digest": "test",
            "runtime_session_id": "session", "runtime_instance_id": "instance", "identity": None,
        }
        return 200, {}, json.dumps(response).encode()

    client._generated._transport = transport
    collector = TimingCollector()
    with timing_scope(collector):
        result = client.health()

    assert result["status"] == "ok"
    assert requests == [("GET", "/v1/health")]
    waiting = [line for line in capsys.readouterr().err.splitlines() if "waiting" in line.lower()]
    assert len(waiting) == 1


def test_timeline_gateway_reports_timings_in_json_and_text(monkeypatch, capsys) -> None:
    from astrid.core.gateway import dispatch

    def run(args):
        assert "--timings" not in args
        with stage("timeline work"):
            time.sleep(0.02)
        print(json.dumps({"ok": True, "data": {}}))
        return 0

    monkeypatch.setattr(dispatch, "_dispatch_product_inner", run)
    assert dispatch._dispatch_product(["timelines", "show", "--json", "--timings"]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["ok"] is True
    assert result["timings"][0]["stage"] == "timeline work"
    assert "timeline work:" in captured.err
