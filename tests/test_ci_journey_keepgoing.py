"""Focused controls for the disposable full-view keep-going driver."""
import json
from types import SimpleNamespace

import pytest

from scripts import ci_journey as journey
from scripts import ci_journey_keepgoing as keepgoing
from scripts import journey_version_jumps as jumps


@pytest.mark.parametrize("platform,release,expected", [
    ("win32", 1, 0),
    ("linux", 1, 1),
    ("darwin", 1, 1),
    ("win32", 0, 0),
])
def test_cleanup_checks_only_platform_supported_service_ownership(monkeypatch, tmp_path,
                                                                  platform, release, expected):
    calls = []
    monkeypatch.setattr(journey.sys, "platform", platform)
    monkeypatch.setattr(journey, "assert_direct_service_ownership",
                        lambda root, data, *, release: calls.append((root, data, release)) or {})

    assert journey.assert_cleanup_service_ownership(tmp_path / "root", tmp_path / "data",
                                                    release=release) == {}

    assert calls == [(tmp_path / "root", tmp_path / "data", expected)]


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_fresh_release1_seeds_task_project_on_each_platform(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(keepgoing.sys, "platform", platform)
    app = tmp_path / 'app'
    app.mkdir()
    candidate = tmp_path / 'candidate.json'
    candidate.write_text('{}')
    args = SimpleNamespace(job_id='direct-linux', mode='direct', leg='fresh-install',
                           shell='sh', app_checkout=app, candidate=candidate)
    root, data = tmp_path / 'program', tmp_path / 'data'
    manifest = {'release': 1}
    calls = []
    task_calls = []
    monkeypatch.setattr(journey, 'selected_manifest', lambda *a, **k: (candidate, manifest))
    monkeypatch.setattr(keepgoing.gate, 'validate_tuple', lambda *a: manifest)
    monkeypatch.setattr(journey, 'sentinel_observation', lambda *a: {'alive': True})
    monkeypatch.setattr(journey, 'bootstrap', lambda *a: None)
    monkeypatch.setattr(jumps, 'start_direct_runtime', lambda *a: None)
    monkeypatch.setattr(journey, 'wait_ready', lambda *a, **k: None)
    monkeypatch.setattr(journey, 'installation', lambda *a: (root / 'version.json', data, manifest))
    monkeypatch.setattr(jumps, 'installed_release', lambda version: version['release'])
    monkeypatch.setattr(journey, 'selected_tuple', lambda *a: None)
    monkeypatch.setattr(journey, 'assert_direct_service_ownership',
                        lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(journey, 'verify', lambda *a, **k: None)
    monkeypatch.setattr(journey.windows, 'EVIDENCE', None)
    monkeypatch.setattr(jumps, 'task_after_update',
                        lambda *a: pytest.fail("fresh-install must seed its project"))
    monkeypatch.setattr(journey, 'task', lambda *a, **k: task_calls.append(("sdk", a, k)))
    monkeypatch.setattr(journey.windows, 'wait_for', lambda *a: task_calls.append(("wait", a)))
    monkeypatch.setattr(journey.windows, 'host_ready', lambda: True)
    monkeypatch.setattr(journey.windows, 'snapshot', lambda *a: task_calls.append(("snapshot", a)))
    monkeypatch.setattr(journey.windows, 'task',
                        lambda *a, **k: task_calls.append(("windows", a, k)))
    evidence = tmp_path / 'evidence'
    report = keepgoing.execute(args, root, data, evidence, SimpleNamespace(pid=1), 'birth')
    assert all(stage['result'] == 'pass' for stage in report['stages'])
    assert [stage['stage'] for stage in report['stages']] == list(keepgoing.FRESH_STAGES)
    assert calls == [((root, data), {})]
    if platform == "win32":
        assert task_calls[0][0] == "wait"
        assert task_calls[1] == ("snapshot", ("processes-host-ready.json",))
        assert task_calls[-1] == ("windows", ("tst-m4-3b-release0",), {"create_project": True})
        assert journey.windows.EVIDENCE == evidence / "release1-working-task"
    else:
        assert task_calls == [("sdk", (manifest, evidence / "release1-working-task",
                                        "tst-m4-3b-release0"), {})]


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
    monkeypatch.setattr(jumps, "selected_data_root", lambda root, data: data.parent / ("data-r1" if state["release"] else "data"))
    monkeypatch.setattr(journey, "wait_ready", lambda data: calls.append(("ready", data)))
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
    def verify(mode, *args, **kwargs):
        calls.append("verify-" + mode)
        calls.append(("verify-root", mode, args[1]))
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
    def live_workspace(root, data, evidence, action, request, *, release):
        calls.append(("live", data, release))
        return {"snapshot": True}
    monkeypatch.setattr(jumps, "live_workspace", live_workspace)
    monkeypatch.setattr(jumps, "task_after_update", lambda *a: "task")
    monkeypatch.setattr(journey, "healthy", lambda data: calls.append(("health", data)) or
                        {"pid": state["pid"], "runtime_instance_id": str(state["pid"])})
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
    assert "one-runtime" not in rows
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
    r0, r1 = tmp_path / "data", tmp_path / "data-r1"
    assert ("ready", r0) in calls and ("ready", r1) in calls
    assert ("health", r0) in calls and ("health", r1) in calls
    assert ("verify-root", "observe", r0) in calls
    assert ("verify-root", "update", r1) in calls and ("verify-root", "install", r1) in calls
    assert ("live", r1, 1) in calls
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


def test_failed_fresh_bootstrap_capture_preserves_stage_failure(tmp_path, monkeypatch):
    from scripts import journey_windows_acl_capture as capture
    app = tmp_path / 'app'
    app.mkdir()
    candidate = tmp_path / 'candidate.json'
    candidate.write_text('{}')
    evidence = tmp_path / 'evidence'
    args = SimpleNamespace(job_id='direct-windows', mode='direct', leg='fresh-install',
                           shell='ps1', app_checkout=app, candidate=candidate)
    manifest = {'release': 1}
    calls = []
    monkeypatch.setattr(journey.sys, 'platform', 'win32')
    monkeypatch.setattr(journey, 'selected_manifest', lambda *a, **k: (candidate, manifest))
    monkeypatch.setattr(keepgoing.gate, 'validate_tuple', lambda *a: manifest)
    monkeypatch.setattr(journey, 'sentinel_observation', lambda *a: {'alive': True})
    def fail(*args):
        calls.append('bootstrap')
        raise ValueError('primary')
    def broken_capture(*args):
        calls.append('capture')
        raise RuntimeError('sink failure')
    monkeypatch.setattr(journey, 'bootstrap', fail)
    monkeypatch.setattr(capture, 'capture', broken_capture)
    report = keepgoing.execute(args, tmp_path / 'program', tmp_path / 'data', evidence,
                               SimpleNamespace(pid=1), 'birth')
    install = next(row for row in report['stages'] if row['stage'] == 'release1-install')
    assert install['result'] == 'fail' and install['first_error'] == 'ValueError: primary'
    assert calls == ['bootstrap', 'capture']
    assert next(row for row in report['stages'] if row['stage'] == 'release1-start')['result'] == 'not-run'
