import copy
from datetime import datetime, timezone
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import check_journey_evidence as gate
from scripts import windows_update_journey as update
from scripts import ci_journey


def test_update_scheduler_capture_filters_scope_and_projects_fields(tmp_path, monkeypatch):
    seen = []
    row = {"id": 201, "time": "2026-11-05T03:10:01.1234567Z", "result": "2147942401",
           "instance": "{6b3040e8-a34e-43d9-b42b-96e4be464612}",
           "action": "private path", "message": "private message"}
    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "observed", "events": [row] * 70}))
    monkeypatch.setattr(update.subprocess, "run", run)
    update.capture_update_scheduler_events(tmp_path, datetime(2026, 11, 5, 3, 10, tzinfo=timezone.utc))
    record = json.loads((tmp_path / update.SCHEDULER_EVENTS).read_text())
    assert record["status"] == "observed" and len(record["events"]) == 64
    assert record["started_at_utc"] == "2026-11-05T03:10:00.000000Z" and record["event_limit_reached"]
    assert record["events"][0] == {"id": 201, "time": row["time"], "result_code": 2147942401,
                                   "instance_id": "6b3040e8-a34e-43d9-b42b-96e4be464612"}
    assert "private" not in json.dumps(record)
    command = seen[0][0][-1]
    assert "2026-11-05T03:10:00.000000Z" in command
    assert "Data[@Name='TaskName']='\\Astrid Runtime'" in command
    assert "-MaxEvents 64" in command and seen[0][1]["timeout"] == 30
    assert update.SCHEDULER_EVENTS in (Path(__file__).resolve().parents[1] / ".github/workflows/journey.yml").read_text()


@pytest.mark.parametrize("value,expected", [
    ({"status": "disabled", "events": []}, "disabled"),
    ({"status": "unavailable", "events": []}, "unavailable"),
    ({"status": "observed", "events": []}, "observed"),
    ({"status": "observed", "events": [{"id": 201, "time": "private"}]}, "observed"),
])
def test_update_scheduler_capture_reports_absence_without_exposing_details(tmp_path, monkeypatch, value, expected):
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(value)))
    update.capture_update_scheduler_events(tmp_path, datetime.now(timezone.utc))
    record = json.loads((tmp_path / update.SCHEDULER_EVENTS).read_text())
    assert record["status"] == expected and record["events"] == []
    assert "absence is inconclusive" in record["limitation"]


def test_update_scheduler_capture_timeout_is_unavailable_and_staged(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("private command", 30, output="private output")
    monkeypatch.setattr(update.subprocess, "run", timeout)
    evidence = tmp_path / "run" / "evidence" / "fresh-install"
    evidence.mkdir(parents=True)
    update.capture_update_scheduler_events(evidence, datetime.now(timezone.utc))
    ci_journey.retain_named_evidence(evidence, "before-cleanup")
    staged = tmp_path / "run" / "evidence-upload" / "before-cleanup" / update.SCHEDULER_EVENTS
    record = json.loads(staged.read_text())
    assert record["status"] == "unavailable" and record["events"] == []
    assert "private" not in staged.read_text()


def receipt():
    return {"outcome": "updated", "from_release": 1, "to_release": 2,
            "marker": update.MARKER, "manifest_sha256": "a" * 64,
            "old_version": "old", "current_version": "new", "exit_code": 0, "refusal": None, "task_id": "task-1",
            "task_labels": [{"name": "Astrid Runtime", "path": "\\"}], "service_names": [],
            "checks": {key: "pass" for key in ("running_before", "owned_task", "healthy", "same_labels",
                       "no_r0_handoff", "workspace", "sentinel", "prior_version", "verify_update", "task_settled")}}


def test_standin_preserves_pins_and_canonical_hash():
    source = {"release": 1, "agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40},
              "tools": {"python": {"version": "3.12"}}, "vibecomfy": {"sha": "c" * 40}}
    prior = copy.deepcopy(source)
    standin = update.make_standin_release(source)
    assert source == prior
    assert {k: standin[k] for k in ("agent", "runtime", "tools", "vibecomfy")} == {k: source[k] for k in ("agent", "runtime", "tools", "vibecomfy")}
    assert standin["release"] == 2 and standin["qualification_marker"] == update.MARKER
    raw = update.canonical_manifest_bytes(standin)
    assert raw == (json.dumps(standin, indent=2, sort_keys=True) + "\n").encode()
    assert len(hashlib.sha256(raw).hexdigest()) == 64


def test_gate_requires_successful_update():
    assert gate._windows_fresh_update_errors(None, "direct-windows")
    good = receipt()
    assert not gate._windows_fresh_update_errors(good, "direct-windows")
    refused = copy.deepcopy(good)
    refused.update(outcome="refused", exit_code=1, current_version="old", refusal="File is locked.")
    refused["checks"].pop("verify_update")
    refused["checks"].pop("task_settled")
    assert gate._windows_fresh_update_errors(refused, "direct-windows")
    assert gate._windows_fresh_update_errors(dict(good, outcome="refused"), "direct-windows")
    for field in ("verify_update", "task_settled", "same_labels", "no_r0_handoff", "workspace", "sentinel"):
        bad = copy.deepcopy(good)
        bad["checks"].pop(field)
        assert gate._windows_fresh_update_errors(bad, "direct-windows")


def test_update_success_and_failure_exercise_assertions(tmp_path, monkeypatch):
    root = tmp_path / "program"
    data = tmp_path / "data"
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    old = root / "versions" / "old" / "version.json"
    old.parent.mkdir(parents=True)
    old.write_text("{}")
    new = root / "versions" / "new" / "version.json"
    marker = tmp_path / "marker"
    candidate = {"release": 1, "agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40}, "tools": {}}
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(update, "task_labels", lambda: [("\\", "Astrid Runtime")])
    monkeypatch.setattr(update, "service_names", lambda: [])
    monkeypatch.setattr(update, "assert_running_task", lambda: None)
    monkeypatch.setattr(update, "assert_no_boundary", lambda *args: None)
    monkeypatch.setattr(update.windows, "wait_for", lambda *a, **kw: None)
    calls = []
    monkeypatch.setattr(update.process_capture, "host_pid", lambda data: 42)
    monkeypatch.setattr(update.process_capture, "observe", lambda: (calls.append("observe-processes") or {}))
    def process_failure(*args):
        calls.append("capture-processes")
        raise OSError("private diagnostic failure")
    monkeypatch.setattr(update.process_capture, "capture_failure", process_failure)
    def capture(*args):
        calls.append("capture")
        raise OSError("capture failure")
    monkeypatch.setattr(update, "capture_update_scheduler_events", capture)
    monkeypatch.setattr(update.windows, "task", lambda *a, **kw: (calls.append("task") or "task-1"))
    current = [old]
    def verify(mode, *args, **kwargs):
        calls.append("verify-" + mode)
        path = evidence / f"verify-{mode}.json"
        path.write_text('{"passed": true}')
        return path
    posts = []
    journey = SimpleNamespace(HEALTH_URL="http://127.0.0.1:17340/api/runtime/v1/health",
        bootstrap=lambda *a: pytest.fail("the journey must not run the bootstrap itself"),
        installation=lambda *a: (current[0], data, {"root": str(current[0].parent), "python": "C:/installed/python.exe",
        "identity": {"release": 1 if current[0] == old else 2, "managed_by": None}}),
        healthy=lambda *a: (calls.append("healthy") or {"runtime_instance_id": "runtime-old" if current[0] == old else "runtime-new",
            "identity": {"release": 1 if current[0] == old else 2}}),
        assert_direct_service_ownership=lambda *a, **kw: calls.append("owner"),
        verify=verify, wait_ready=lambda *a, **kw: calls.append("ready"), assert_content=lambda *a: calls.append("content"),
        sentinel_observation=lambda *a: {"alive": True}, read_json=lambda p: json.loads(p.read_text()))
    def hosted_update(**kwargs):
        posts.append(kwargs)
        new.parent.mkdir()
        new.write_text("{}")
        (new.parent / "manifest.json").write_bytes(kwargs["manifest"])
        current[0] = new
        return {"route": "hosted-button"}
    monkeypatch.setattr(update.hosted, "run_hosted_update", hosted_update)
    args = SimpleNamespace(shell="powershell.exe")
    result = update.run_update(journey, args, tmp_path, candidate, root, data, evidence, None, "birth", "project", marker, "digest")
    assert result["outcome"] == "updated" and not gate._windows_fresh_update_errors(result, "direct-windows")
    assert {"verify-observe", "verify-update", "task", "content", "ready"} <= set(calls)
    assert result["hosted"] == {"route": "hosted-button"}
    assert len(posts) == 1
    assert "capture" not in calls and "capture-processes" not in calls
    post = posts[0]
    assert post["app"] == tmp_path and post["python"] == "C:/installed/python.exe"
    assert post["door_base"] == journey.HEALTH_URL and (post["root"], post["evidence"]) == (root, evidence)
    assert (evidence / "windows-update-manifest.json").read_bytes() == post["manifest"]
    assert post["manifest"] == update.canonical_manifest_bytes(update.make_standin_release(candidate))
    current[0] = old
    calls.clear()
    for output in ("File is locked.", "Access denied."):
        calls.clear()
        monkeypatch.setattr(update.hosted, "run_hosted_update",
                            lambda *, _output=output, **kw: (_ for _ in ()).throw(RuntimeError(_output)))
        with pytest.raises(RuntimeError, match=output):
            update.run_update(journey, args, tmp_path, candidate, root, data, evidence, None, "birth", "project", marker, "digest")
        assert "verify-update" not in calls and "task" not in calls
        assert calls.count("observe-processes") == 2
        assert calls.index("capture-processes") < calls.index("capture")


def test_update_follows_the_candidate_release_and_waits_for_the_worker_task_to_go(monkeypatch):
    with pytest.raises(RuntimeError, match="integer release"):
        update.run_update(SimpleNamespace(), None, None, {"release": True}, None, None, None, None, None, None, None, None)
    with pytest.raises(RuntimeError, match="not direct release 7"):
        journey = SimpleNamespace(installation=lambda *a: (Path("/v/old/version.json"), None,
                                                          {"identity": {"release": 1, "managed_by": None}}))
        update.run_update(journey, None, None, {"release": 7}, None, None, None, None, None, None, None, None)
    # The worker's transient task is listed until its own cleanup finishes.
    seen = iter([[("\\", "Astrid Runtime"), ("\\", "astrid-update-1")], [("\\", "Astrid Runtime")]])
    monkeypatch.setattr(update, "task_labels", lambda: next(seen))
    monkeypatch.setattr(update, "service_names", lambda: [])
    monkeypatch.setattr(update.time, "sleep", lambda *_: None)
    update.wait_for_labels([("\\", "Astrid Runtime")], [])
    clock = iter([0, 1, 1000])
    monkeypatch.setattr(update.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(update, "task_labels", lambda: [("\\", "Astrid Runtime"), ("\\", "other")])
    with pytest.raises(RuntimeError, match="changed Astrid task or service labels"):
        update.wait_for_labels([("\\", "Astrid Runtime")], [])
