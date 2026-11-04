"""Focused controls for the disposable full-view keep-going driver."""
import json
from types import SimpleNamespace

import pytest

from scripts import ci_journey as journey
from scripts import ci_journey_keepgoing as keepgoing
from scripts import journey_version_jumps as jumps


@pytest.mark.parametrize("wrong_selection,change_at", [
    (None, 0),
    *((kind, stage) for kind in ("path", "data", "release", "version") for stage in (1, 2)),
])
def test_release0_restart_refreshes_owner_and_guards_selection(tmp_path, monkeypatch, wrong_selection, change_at):
    evidence = tmp_path / "run" / "evidence" / "release0-to-release1"
    app = tmp_path / "app"
    app.mkdir()
    candidate = tmp_path / "candidate.json"
    candidate.write_text("{}")
    manifest0 = tmp_path / "manifest0.json"
    manifest0.write_text("{}")
    args = SimpleNamespace(job_id="direct-linux", mode="direct", leg="release0-to-release1",
                           shell="sh", app_checkout=app, candidate=candidate)
    calls = []
    state = {"release": 0, "pid": 1, "snapshot": False, "checks": 0}
    manifest = {"release": 1}
    monkeypatch.setattr(jumps, "release0_admission", lambda _: (tmp_path, manifest0, {}))
    monkeypatch.setattr(journey, "selected_manifest", lambda *a, **k: (candidate, manifest))
    monkeypatch.setattr(keepgoing.gate, "validate_tuple", lambda *a: manifest)
    monkeypatch.setattr(journey, "sentinel_observation", lambda *a: {"alive": True})
    monkeypatch.setattr(jumps, "bootstrap_release0", lambda *a: calls.append("bootstrap0"))
    monkeypatch.setattr(jumps, "start_direct_runtime", lambda *a: calls.append("start"))
    monkeypatch.setattr(journey, "wait_ready", lambda *a: None)
    def installation(*_):
        if state["snapshot"] and state["release"] == 0:
            state["checks"] += 1
        wrong = wrong_selection if state["checks"] >= change_at and state["release"] == 0 else None
        return (tmp_path / ("wrong" if wrong == "path" else "v" + str(state["release"])),
                tmp_path / ("other-data" if wrong == "data" else "data"),
                {"release": 1 if wrong == "release" else state["release"],
                 "identity": "wrong" if wrong == "version" else "selected"})
    monkeypatch.setattr(journey, "installation", installation)
    monkeypatch.setattr(jumps, "installed_release", lambda version: version["release"])
    monkeypatch.setattr(journey, "selected_tuple", lambda *a: {})
    monkeypatch.setattr(journey, "assert_direct_service_ownership",
                        lambda *a, **_kwargs: calls.append("ownership"))
    monkeypatch.setattr(journey, "assert_one_runtime", lambda *a: calls.append("one"))
    def verify(mode, *args, **kwargs):
        calls.append("verify-" + mode)
        receipt = args[2] / f"verify-{mode}.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        passed = mode != "observe"
        receipt.write_text(json.dumps({"passed": passed,
                                       "failures": [] if passed else ["capabilities.approval", "capabilities.required"]}))
        return receipt
    monkeypatch.setattr(journey, "verify", verify)
    monkeypatch.setattr(jumps, "write_diagnostic_release0_preconditions", lambda *a, **k: None)
    def workspace(*args):
        if state["release"] == 0:
            state["pid"] += 1  # Each offline workspace operation restarts the Runtime.
            if args[4] == "snapshot":
                state["snapshot"] = True
        return {"project": "p"} if args[4] == "seed" else {"snapshot": True}
    monkeypatch.setattr(jumps, "direct_workspace", workspace)
    monkeypatch.setattr(jumps, "task_after_update", lambda *a: "task")
    monkeypatch.setattr(journey, "healthy", lambda *a: {"pid": state["pid"],
                                                         "runtime_instance_id": str(state["pid"])} )
    def process_birth(pid):
        calls.append(("birth", pid))
        return f"birth-{pid}" if state["release"] == 0 else None
    monkeypatch.setattr(journey, "process_birth", process_birth)
    def bootstrap(*args):
        calls.append("update")
        state["release"] = 1
        state["pid"] = 4
    monkeypatch.setattr(journey, "bootstrap", bootstrap)
    monkeypatch.setattr(jumps, "selected_candidate", lambda *a: {})
    monkeypatch.setattr(jumps, "assert_preserved", lambda *a: None)

    report = keepgoing.execute(args, tmp_path / "program", tmp_path / "data", evidence,
                               SimpleNamespace(pid=1), "birth")
    rows = {row["stage"]: row for row in report["stages"]}
    assert report["classification"] == "NON-PROOF"
    assert report["qualification"]["eligible"] is False
    assert json.loads((evidence / "journey-result.json").read_text())["receipt_kind"] == "diagnostic-only"
    if wrong_selection:
        failed_stage = "release0-owner" if change_at == 1 else "release1-update"
        assert rows[failed_stage]["result"] == "fail"
        assert "Selected installation changed" in rows[failed_stage]["first_error"]
        if change_at == 1:
            assert rows["release1-update"]["result"] == "not-run"
        assert "update" not in calls
        return
    assert rows["release0-owner"]["result"] == "pass"
    assert rows["release1-selection"]["result"] == "pass"
    assert ("birth", 3) in calls[calls.index("update") + 1:]
    observation = rows["release0-verify"]
    assert observation["result"] == "observed-failed"
    assert observation["observation"] == {
        "passed": False,
        "failures": ["capabilities.approval", "capabilities.required"],
    }
    assert observation["failure_class"] == "real"
    assert observation["forced_continuation_used"] is False
    assert rows["release1-update"]["result"] == "pass" and "update" in calls
    assert rows["release1-verify-update"]["result"] == "pass"
    assert any(item["name"] == "verified0" and item["source"] == "genuine"
               for item in rows["release1-verify-update"]["preconditions"])
    rendered = (evidence / "keepgoing-report.md").read_text()
    assert "| stage | result | observation | forced-continuation-used | first error |" in rendered
    assert "| release0-verify | observed-failed | {\"failures\": [\"capabilities.approval\", \"capabilities.required\"], \"passed\": false} |" in rendered
    assert "REAL failures: release0-verify" in rendered
    stage_index = [row["stage"] for row in report["stages"]].index("release0-verify")
    assert f"style s{stage_index} fill:#f99" in rendered


def test_release0_owner_captures_service_health_and_process_identity(monkeypatch, tmp_path):
    calls = []
    health = {"pid": 431, "runtime_instance_id": "r0-instance"}
    monkeypatch.setattr(keepgoing.journey, "assert_direct_service_ownership",
                        lambda root, data: calls.append(("service", root, data)) or {"manager": "test"})
    monkeypatch.setattr(keepgoing.journey, "healthy", lambda data: calls.append(("health", data)) or health)
    monkeypatch.setattr(keepgoing.journey, "process_birth", lambda pid: calls.append(("birth", pid)) or "birth-1")

    owner = keepgoing.release0_owner(tmp_path / "root", tmp_path / "data")

    assert owner == {"service": {"manager": "test"}, "health": health, "pid": 431, "birth": "birth-1"}
    assert [call[0] for call in calls] == ["service", "health", "birth"]


def test_release0_owner_requires_a_process_birth_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(keepgoing.journey, "assert_direct_service_ownership", lambda *_: {})
    monkeypatch.setattr(keepgoing.journey, "healthy", lambda _: {"pid": 431, "runtime_instance_id": "r0"})
    monkeypatch.setattr(keepgoing.journey, "process_birth", lambda _: None)

    with pytest.raises(RuntimeError, match="Cannot establish the release-0 Runtime process identity"):
        keepgoing.release0_owner(tmp_path / "root", tmp_path / "data")


def test_update_preflight_requires_same_live_service_owner(monkeypatch, tmp_path):
    calls = []
    owner = {"health": {"pid": 431, "runtime_instance_id": "r0-instance"},
             "pid": 431, "birth": "birth-1"}
    monkeypatch.setattr(keepgoing.journey, "assert_direct_service_ownership",
                        lambda *_: calls.append("service") or {})
    monkeypatch.setattr(keepgoing.journey, "healthy",
                        lambda _: calls.append("health") or {"pid": 431, "runtime_instance_id": "r0-instance"})
    monkeypatch.setattr(keepgoing.journey, "process_birth", lambda pid: calls.append(("birth", pid)) or "birth-1")

    assert keepgoing.assert_release0_owner(tmp_path / "root", tmp_path / "data", owner) == owner["health"]
    assert calls == ["service", "health", ("birth", 431)]


@pytest.mark.parametrize("health,birth", [
    ({"pid": 431, "runtime_instance_id": "replacement-instance"}, "birth-1"),
    ({"pid": 432, "runtime_instance_id": "r0-instance"}, "birth-1"),
    ({"pid": 431, "runtime_instance_id": "r0-instance"}, "replacement-birth"),
])
def test_update_preflight_rejects_changed_runtime_owner(monkeypatch, tmp_path, health, birth):
    owner = {"health": {"pid": 431, "runtime_instance_id": "r0-instance"},
             "pid": 431, "birth": "birth-1"}
    monkeypatch.setattr(keepgoing.journey, "assert_direct_service_ownership", lambda *_: {})
    monkeypatch.setattr(keepgoing.journey, "healthy", lambda _: health)
    monkeypatch.setattr(keepgoing.journey, "process_birth", lambda _: birth)

    with pytest.raises(RuntimeError, match="owner changed before the direct release-1 update"):
        keepgoing.assert_release0_owner(tmp_path / "root", tmp_path / "data", owner)


@pytest.mark.parametrize("action", ["seed", "snapshot"])
def test_release0_workspace_uses_current_helper_signature(monkeypatch, tmp_path, action):
    calls = []

    def current_helper(root, version, data, evidence, helper_action, request):
        calls.append((root, version, data, evidence, helper_action, request))
        return {"action": helper_action}

    monkeypatch.setattr(keepgoing.jumps, "direct_workspace", current_helper)
    args = (tmp_path / "root", {"root": "v0"}, tmp_path / "data",
            tmp_path / "evidence", action, {"project": "diagnostic"})

    assert keepgoing.release0_workspace(*args) == {"action": action}
    assert calls == [args]
