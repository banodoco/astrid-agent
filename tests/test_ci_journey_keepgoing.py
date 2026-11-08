"""Focused controls for the disposable full-view keep-going driver."""
import json
from types import SimpleNamespace

import pytest

from scripts import ci_journey as journey
from scripts import ci_journey_keepgoing as keepgoing
from scripts import journey_version_jumps as jumps


@pytest.mark.parametrize("platform,release,fresh_install,expected", [
    ("win32", 1, False, 0),
    ("linux", 1, False, 1),
    ("darwin", 1, False, 1),
    ("linux", 1, True, 0),
    ("darwin", 1, True, 0),
    ("win32", 0, False, 0),
])
def test_cleanup_checks_only_platform_supported_service_ownership(monkeypatch, tmp_path,
                                                                  platform, release, fresh_install,
                                                                  expected):
    calls = []
    monkeypatch.setattr(journey.sys, "platform", platform)
    monkeypatch.setattr(journey, "assert_direct_service_ownership",
                        lambda root, data, *, release: calls.append((root, data, release)) or {})

    assert journey.assert_cleanup_service_ownership(tmp_path / "root", tmp_path / "data",
                                                    release=release,
                                                    fresh_install=fresh_install) == {}

    assert calls == [(tmp_path / "root", tmp_path / "data", expected)]


@pytest.mark.parametrize("release", [1, 7])
@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_fresh_release1_seeds_task_project_on_each_platform(tmp_path, monkeypatch, platform, release):
    monkeypatch.setattr(keepgoing.sys, "platform", platform)
    app = tmp_path / 'app'
    app.mkdir()
    candidate = tmp_path / 'candidate.json'
    candidate.write_text('{}')
    args = SimpleNamespace(job_id='direct-linux', mode='direct', leg='fresh-install',
                           shell='sh', app_checkout=app, candidate=candidate)
    root, data = tmp_path / 'program', tmp_path / 'data'
    manifest = {'release': release}
    calls = []
    task_calls = []
    monkeypatch.setattr(journey, 'selected_manifest', lambda *a, **k: (candidate, manifest))
    monkeypatch.setattr(keepgoing.gate, 'validate_tuple', lambda *a: manifest)
    monkeypatch.setattr(journey, 'sentinel_observation', lambda *a: {'alive': True})
    monkeypatch.setattr(journey, 'bootstrap', lambda *a, **k: None)
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


@pytest.mark.parametrize("bad", [True, "1", -1, None])
def test_fresh_admission_needs_an_integer_release_but_not_release_one(tmp_path, monkeypatch, bad):
    app = tmp_path / 'app'
    app.mkdir()
    candidate = tmp_path / 'candidate.json'
    candidate.write_text('{}')
    args = SimpleNamespace(job_id='direct-linux', mode='direct', leg='fresh-install',
                           shell='sh', app_checkout=app, candidate=candidate)
    manifest = {'release': bad}
    monkeypatch.setattr(journey, 'selected_manifest', lambda *a, **k: (candidate, manifest))
    monkeypatch.setattr(keepgoing.gate, 'validate_tuple', lambda *a: manifest)
    report = keepgoing.execute(args, tmp_path / 'root', tmp_path / 'data', tmp_path / 'evidence',
                               SimpleNamespace(pid=1), 'birth')
    admission = report['stages'][0]
    assert admission['stage'] == 'admission' and admission['result'] == 'fail'
    assert 'integer release' in admission['first_error']


@pytest.mark.parametrize("wrong_selection,change_at", [
    (None, 0),
    *((kind, stage) for kind in ("path", "data", "release", "version") for stage in (1, 2)),
])
def test_release0_live_snapshots_keep_selected_owner_and_guard_selection(
        tmp_path, monkeypatch, wrong_selection, change_at):
    evidence = tmp_path / "run" / "evidence" / "release9-then-install"
    app = tmp_path / "app"
    app.mkdir()
    candidate = tmp_path / "candidate.json"
    candidate.write_text("{}")
    manifest0 = tmp_path / "manifest0.json"
    manifest0.write_text("{}")
    args = SimpleNamespace(job_id="direct-linux", mode="direct", leg="release9-then-install",
                           shell="sh", app_checkout=app, candidate=candidate)
    calls = []
    state = {"release": 9, "pid": 1, "snapshot": False, "checks": 0}
    manifest = {"release": 10}
    monkeypatch.setattr(jumps, "release9_admission", lambda *_: (tmp_path, manifest0, {}))
    monkeypatch.setattr(journey, "selected_manifest", lambda *a, **k: (candidate, manifest))
    monkeypatch.setattr(keepgoing.gate, "validate_tuple", lambda *a: manifest)
    monkeypatch.setattr(journey, "sentinel_observation", lambda *a: {"alive": True})
    monkeypatch.setattr(jumps, "bootstrap_release9", lambda *a: calls.append("bootstrap0"))
    monkeypatch.setattr(jumps, "start_direct_runtime", lambda *a: calls.append("start"))
    monkeypatch.setattr(jumps, "selected_data_root", lambda root, data: data)
    monkeypatch.setattr(journey, "wait_ready", lambda data: calls.append(("ready", data)))
    def installation(*_):
        if state["snapshot"] and state["release"] == 9:
            state["checks"] += 1
        wrong = wrong_selection if state["checks"] >= change_at and state["release"] == 9 else None
        return (tmp_path / ("wrong" if wrong == "path" else "v" + str(state["release"])),
                tmp_path / ("other-data" if wrong == "data" else "data"),
                {"release": 10 if wrong == "release" else state["release"],
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
                                       "failures": [] if passed else ["capabilities.approval", "capabilities.required"],
                                       "details": {
                                           "capabilities.required": {
                                               "unready": ["generation.generate_image_codex", "stream_content.distill"],
                                               "install_root": "/private/install/root",
                                           },
                                           "capabilities.approval": {
                                               "unapproved": ["video_editing.event_talks"],
                                               "token": "must-not-be-captured",
                                           },
                                       }}))
        return receipt
    monkeypatch.setattr(journey, "verify", verify)
    monkeypatch.setattr(jumps, "write_diagnostic_release0_preconditions", lambda *a, **k: None)
    def live_workspace(root, data, evidence, action, request, *, release=0):
        calls.append(("live", data, release, action, dict(request)))
        if action == "seed":
            return {"project": "p", "timeline": "t", "revision": "r",
                    "generation": "release0", "object_digest": "digest"}
        if release == 0:
            state["snapshot"] = True
        return {"snapshot": True}
    monkeypatch.setattr(jumps, "live_workspace", live_workspace)
    monkeypatch.setattr(jumps, "task_after_update", lambda *a: "task")
    monkeypatch.setattr(journey, "healthy", lambda data: calls.append(("health", data)) or
                        {"pid": state["pid"], "runtime_instance_id": str(state["pid"])})
    def process_birth(pid):
        calls.append(("birth", pid))
        return f"birth-{pid}" if state["release"] == 9 else None
    monkeypatch.setattr(journey, "process_birth", process_birth)
    def bootstrap(*args):
        calls.append("update")
        state["release"] = 10
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
    # The candidate upgrade is an in-place plain install, so both sides keep one data root.
    r0 = r1 = tmp_path / "data"
    assert ("ready", r0) in calls and ("ready", r1) in calls
    assert ("health", r0) in calls and ("health", r1) in calls
    assert ("verify-root", "observe", r0) in calls
    assert ("verify-root", "update", r1) not in calls
    assert ("verify-root", "install", r1) in calls
    assert any(call[:4] == ("live", r0, 0, "seed") for call in calls)
    before_call = next(call for call in calls if call[:4] == ("live", r0, 0, "snapshot"))
    after_call = [call for call in calls if call[:4] == ("live", r1, 0, "snapshot")][-1]
    assert before_call[4]["revision"] == after_call[4]["revision"] == "r"
    assert before_call[4]["generation"] == after_call[4]["generation"] == "release0"
    assert ("birth", 1) in calls[calls.index("update") + 1:]
    observation = rows["release0-verify"]
    assert observation["result"] == "observed-failed"
    assert observation["observation"] == {
        "passed": False,
        "failures": ["capabilities.approval", "capabilities.required"],
        "failed_check_details": [
            {"check": "capabilities.required",
             "ids": ["generation.generate_image_codex", "stream_content.distill"],
             "omitted_count": 0},
            {"check": "capabilities.approval", "ids": ["video_editing.event_talks"],
             "omitted_count": 0},
        ],
    }
    assert "/private/install/root" not in json.dumps(observation["observation"])
    assert "must-not-be-captured" not in json.dumps(observation["observation"])
    assert observation["failure_class"] == "real"
    assert observation["forced_continuation_used"] is False
    assert rows["release1-update"]["result"] == "pass" and "update" in calls
    dependent = rows["release1-verify-update"]
    assert dependent["result"] == "not-run"
    assert dependent["failure_class"] == "diagnostic-tainted:release0-verify"
    assert any(item["name"] == "verified0" and item["source"] == "unavailable"
               for item in dependent["preconditions"])
    rendered = (evidence / "keepgoing-report.md").read_text()
    assert "| stage | result | observation | forced-continuation-used | first error |" in rendered
    assert "| release0-verify | observed-failed |" in rendered
    assert '"failed_check_details"' in rendered
    assert '"generation.generate_image_codex"' in rendered
    assert "REAL failures: release0-verify" in rendered
    stage_index = [row["stage"] for row in report["stages"]].index("release0-verify")
    assert f"style s{stage_index} fill:#f99" in rendered


def test_failed_capability_detail_projection_is_allowlisted_and_bounded():
    receipt = {"details": {
        "capabilities.required": {"unready": [f"capability.{index:02d}" for index in range(40)]},
        "capabilities.approval": {"unapproved": ["../private/token", "safe.capability"]},
        "runtime.identity": {"version_root": "/private/root"},
    }}
    projected = keepgoing.failed_capability_details(
        receipt, ["capabilities.required", "capabilities.approval", "runtime.identity"])
    assert len(projected) == 2
    assert projected[0]["ids"] == [f"capability.{index:02d}" for index in range(32)]
    assert projected[0]["omitted_count"] == 8
    assert projected[1] == {"check": "capabilities.approval", "ids": ["safe.capability"],
                            "omitted_count": 0}


def test_expected_driver_exit_is_reported_only_after_every_stage_and_cleanup_pass(tmp_path):
    evidence = tmp_path / "run" / "evidence" / "fresh-install"
    report = keepgoing.initial(evidence, SimpleNamespace(job_id="direct-windows", leg="fresh-install"))
    report["stages"] = [{"stage": name, "result": "pass", "forced_continuation_used": False,
                         "first_error": None, "preconditions": [], "postconditions": []}
                        for name in (*keepgoing.FRESH_STAGES, "final-cleanup")]
    keepgoing.write(evidence, report)
    receipt_path = evidence / "journey-result.json"
    receipt = journey.read_json(receipt_path)
    receipt["final_cleanup"] = "pass"
    journey.write_json(receipt_path, receipt)
    (evidence.parent.parent / "evidence-upload" / "before-cleanup").mkdir(parents=True)
    (evidence.parent.parent / "evidence-upload" / "after-cleanup").mkdir(parents=True)
    command = ["python", "-u", "agent/scripts/ci_journey.py", "--diagnostic-keep-going"]

    assert keepgoing.record_expected_nonzero_exit(evidence, command)

    recorded = journey.read_json(evidence / "keepgoing-report.json")["driver_exit"]
    assert recorded == {
        "phase": "post-journey diagnostic qualification gate",
        "command": command,
        "exit_code": 1,
        "reason": "diagnostic-only receipts are intentionally qualification-ineligible",
    }
    receipt = journey.read_json(receipt_path)
    assert receipt["status"] == "fail" and receipt["qualification"]["eligible"] is False
    assert "Driver exit: 1 during post-journey diagnostic qualification gate" in (
        evidence / "keepgoing-report.md").read_text()
    for phase in ("before-cleanup", "after-cleanup"):
        staged = evidence.parent.parent / "evidence-upload" / phase / "keepgoing-report.json"
        assert journey.read_json(staged)["driver_exit"] == recorded


def test_expected_driver_exit_does_not_hide_a_real_stage_failure(tmp_path):
    evidence = tmp_path / "run" / "evidence" / "fresh-install"
    report = keepgoing.initial(evidence, SimpleNamespace(job_id="direct-windows", leg="fresh-install"))
    report["stages"] = [{"stage": "release1-start", "result": "fail",
                         "forced_continuation_used": False, "first_error": "service failed",
                         "preconditions": [], "postconditions": []}]
    keepgoing.write(evidence, report)
    receipt_path = evidence / "journey-result.json"
    receipt = journey.read_json(receipt_path)
    receipt["final_cleanup"] = "pass"
    journey.write_json(receipt_path, receipt)

    assert not keepgoing.record_expected_nonzero_exit(evidence, ["python", "ci_journey.py"])
    assert "driver_exit" not in journey.read_json(evidence / "keepgoing-report.json")


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

    def current_helper(root, data, evidence, helper_action, request, *, release):
        calls.append((root, data, evidence, helper_action, request, release))
        return {"action": helper_action}

    monkeypatch.setattr(keepgoing.jumps, "live_workspace", current_helper)
    args = (tmp_path / "root", tmp_path / "data", tmp_path / "evidence",
            action, {"project": "diagnostic"})

    assert keepgoing.release0_workspace(*args) == {"action": action}
    assert calls == [(args[0], args[1], args[2], args[3], args[4], 0)]


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
    def fail(*args, **kwargs):
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
