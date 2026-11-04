"""Focused controls for the disposable full-view keep-going driver."""
import json
from types import SimpleNamespace

import pytest

from scripts import ci_journey as journey
from scripts import ci_journey_keepgoing as keepgoing
from scripts import journey_version_jumps as jumps


def test_failed_release0_observation_is_reported_without_gating_continuation(tmp_path, monkeypatch):
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
    state = {"release": 0}
    manifest = {"release": 1}
    monkeypatch.setattr(jumps, "release0_admission", lambda _: (tmp_path, manifest0, {}))
    monkeypatch.setattr(journey, "selected_manifest", lambda *a, **k: (candidate, manifest))
    monkeypatch.setattr(keepgoing.gate, "validate_tuple", lambda *a: manifest)
    monkeypatch.setattr(journey, "sentinel_observation", lambda *a: {"alive": True})
    monkeypatch.setattr(jumps, "bootstrap_release0", lambda *a: calls.append("bootstrap0"))
    monkeypatch.setattr(jumps, "start_direct_runtime", lambda *a: calls.append("start"))
    monkeypatch.setattr(journey, "wait_ready", lambda *a: None)
    monkeypatch.setattr(journey, "installation", lambda *a: (tmp_path / ("v" + str(state["release"])),
                                                               tmp_path / "data", {"release": state["release"]}))
    monkeypatch.setattr(jumps, "installed_release", lambda version: version["release"])
    monkeypatch.setattr(journey, "selected_tuple", lambda *a: {})
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *a: calls.append("ownership"))
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
    monkeypatch.setattr(jumps, "direct_workspace", lambda *a: {"project": "p"} if a[4] == "seed" else {"snapshot": True})
    monkeypatch.setattr(jumps, "task_after_update", lambda *a: "task")
    monkeypatch.setattr(journey, "healthy", lambda *a: {"pid": state["release"] + 1,
                                                         "runtime_instance_id": str(state["release"])} )
    monkeypatch.setattr(journey, "process_birth", lambda pid: "birth" if state["release"] == 0 else None)
    def bootstrap(*args):
        calls.append("update")
        state["release"] = 1
    monkeypatch.setattr(journey, "bootstrap", bootstrap)
    monkeypatch.setattr(jumps, "selected_candidate", lambda *a: {})
    monkeypatch.setattr(jumps, "assert_preserved", lambda *a: None)

    report = keepgoing.execute(args, tmp_path / "program", tmp_path / "data", evidence,
                               SimpleNamespace(pid=1), "birth")
    rows = {row["stage"]: row for row in report["stages"]}
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
