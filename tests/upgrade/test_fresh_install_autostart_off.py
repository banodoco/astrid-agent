"""Focused controls for the fresh-install-autostart-off leg.

A real fresh install from the candidate tuple, run with ``--autostart off``. The leg
records four named assertions: the selection record says autostart is false; GET
update/status on the live Runtime reports reason autostart-off; a POST update is refused
with autostart-off and changes nothing; and the App capture of the autostart-off card is
at deviceScaleFactor 2. These tests drive the leg with fakes.
"""
import argparse
import json
import subprocess
from pathlib import Path

import pytest

from scripts import ci_journey as journey
from scripts import journey_matrix
from scripts import journey_version_jumps as jumps

LEG = "fresh-install-autostart-off"
ROWS = ("direct-linux-autostart-off", "direct-macos-autostart-off")
DETACHED = {"manager": "detached", "autostart": False, "foreground_fallback": False,
            "detached": {"pid": 4242, "birth_verified": True, "workspace": "/w", "version_root": "/v",
                         "runtime_instance_id": "before"},
            "sign_in_registration": {"manager": "systemd-user", "unit": "astrid.service",
                                     "load_state": "not-found"}}


def _leg(monkeypatch, tmp_path, *, install_exit=0, choices_autostart=False, status_reason="autostart-off",
         post_status=409, post_reason="autostart-off", runtime_after="same", selection_after="same",
         data_after_change=False, capture_dpr=2, build_error=None):
    app, candidate_path, root = (tmp_path / name for name in ("app", "candidate.json", "program"))
    data = tmp_path / "data"
    app.mkdir(exist_ok=True)
    candidate_path.write_text("{}", encoding="utf-8")
    manifest = {"release": 12, "agent": {"sha": "e2a7fad07e4a8bef9aa4c6e126911a1c5a29caf2"},
                "runtime": {"sha": "744cf24874ea851f95949a706725aba19d18222f"}}
    args = argparse.Namespace(job_id="direct-linux-autostart-off", mode="direct", leg=LEG, shell="sh",
                              candidate_checkouts={}, service_manager_prerequisite={},
                              app_checkout=app, candidate=candidate_path)
    state = {"snapshots": 0, "post": 0}
    version = {"root": str(root / "versions/c12")}

    def read_selection():
        record = {"format": "astrid-install/v1", "choices": {"autostart": choices_autostart}}
        changed = selection_after != "same" and state["post"] > 0
        return record, ("b" if changed else "a") * 64

    def request(method, path):
        if method == "GET":
            return 200, {"available": False, "reason": status_reason}
        state["post"] += 1
        return post_status, {"code": "conflict", "details": {"reason": post_reason}}

    def live_workspace(*_args, **_kwargs):
        if _args[3] == "seed":
            return {"project": "film"}
        state["snapshots"] += 1
        timeline = "changed" if data_after_change and state["snapshots"] == 2 else "kept"
        return {"project": "film", "timeline": timeline, "preferences": "kept", "task": "settled",
                "cas_sha256": "abc"}

    def healthy(*_args):
        instance = "after" if runtime_after != "same" and state["post"] else "before"
        return {"runtime_instance_id": instance, "identity": {"release": 12}}

    def capture(evidence, app_path, dist_path, capture_dir):
        return {"path": str(evidence / "autostart-off-card.png"), "sha256": "c" * 64,
                "device_scale_factor": capture_dpr, "surface": "autostart-off card"}

    monkeypatch.setattr(jumps.journey, "selected_manifest", lambda *_a, **_k: (app / "manifest.json", manifest))
    monkeypatch.setattr(jumps.journey, "read_json", lambda path: {"manifest_sha256": "candidate",
                                                                   "artifacts": {}}
                        if Path(path) == candidate_path else {})
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *_: manifest)

    def build(app_path, evidence_path):
        if build_error:
            raise RuntimeError(build_error)
        return built_dist(tmp_path)

    monkeypatch.setattr(jumps, "build_hosted_app", build)
    monkeypatch.setattr(jumps, "install_capture_browser", lambda root: tmp_path / "capture-browser")
    monkeypatch.setattr(jumps.journey, "git_source", lambda *_: {"sha": "app-r12"})
    monkeypatch.setattr(jumps.journey, "sentinel_observation", lambda *_: {"alive": True})
    monkeypatch.setattr(jumps, "candidate_install_autostart_off", lambda *_a, **_k: {
        "command": "install --autostart off", "exit_code": install_exit, "log": "install.log", "last_lines": []})
    monkeypatch.setattr(jumps, "start_direct_runtime", lambda *_a, **_k: None)
    monkeypatch.setattr(jumps, "selected_data_root", lambda _root, selected, **_k: selected)
    monkeypatch.setattr(jumps.journey, "wait_ready", lambda *_: None)
    monkeypatch.setattr(jumps.journey, "installation", lambda *_a: (Path(version["root"]) / "version.json", data,
                                                                     version))
    monkeypatch.setattr(jumps, "installed_release", lambda _version: 12)
    def foreground_banned(*_a, **_k):
        raise AssertionError("the autostart-off leg must not use the direct service-manager check")

    monkeypatch.setattr(jumps.journey, "assert_direct_service_ownership", foreground_banned)
    monkeypatch.setattr(jumps.journey, "assert_detached_autostart_off_runtime", lambda *_a, **_k: dict(DETACHED))
    monkeypatch.setattr(jumps.journey, "verify", lambda *_a, **_k: None)
    monkeypatch.setattr(jumps.journey, "selected_tuple", lambda *_: {
        "agent": "e2a7fad07e4a8bef9aa4c6e126911a1c5a29caf2", "runtime": "744cf24874ea851f95949a706725aba19d18222f"})
    monkeypatch.setattr(jumps.journey, "healthy", healthy)
    monkeypatch.setattr(jumps, "read_selection", read_selection)
    monkeypatch.setattr(jumps, "runtime_request", request)
    monkeypatch.setattr(jumps, "capture_autostart_card", capture)
    monkeypatch.setattr(jumps, "live_workspace", live_workspace)
    monkeypatch.setattr(jumps, "task_after_update", lambda *_args: "task-id")

    def run():
        return jumps.execute_fresh_install_autostart_off(args, root, data, tmp_path / "evidence",
                                                         argparse.Namespace(pid=123), "birth")
    return run, tmp_path / "evidence" / "journey-result.json", None


def test_leg_is_selected_on_linux_and_macos_and_not_admitted_on_windows(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(jumps, "execute_fresh_install_autostart_off", lambda *args: seen.append(args) or "ok")
    for platform in ("linux", "darwin"):
        monkeypatch.setattr(jumps.sys, "platform", platform)
        assert jumps.execute_owned(argparse.Namespace(leg=LEG), tmp_path / "program", tmp_path / "data",
                                   tmp_path / "evidence", argparse.Namespace(pid=1), "birth") == "ok"
    assert len(seen) == 2
    monkeypatch.setattr(jumps.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="narrowed refusal"):
        jumps.execute_owned(argparse.Namespace(leg=LEG), tmp_path / "program", tmp_path / "data",
                            tmp_path / "evidence", argparse.Namespace(pid=1), "birth")


def test_the_install_is_run_with_the_autostart_off_flag_and_never_update(monkeypatch, tmp_path):
    calls = []

    class Done:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append(argv)
        kwargs["stdout"].write("installed\n")
        return Done()

    monkeypatch.setattr(jumps.sys, "platform", "linux")
    monkeypatch.setattr(jumps.subprocess, "run", fake_run)
    app = tmp_path / "app"
    report = jumps.candidate_install_autostart_off(app, tmp_path / "m.json", "sh", tmp_path / "ev",
                                                   tmp_path / "program", tmp_path / "data")
    assert calls == [["sh", str(app / "public" / "install"), "--manifest", str(tmp_path / "m.json"),
                      "--root", str(tmp_path / "program"), "--data-root", str(tmp_path / "data"),
                      "--autostart", "off", "--yes"]]
    assert "--update" not in calls[0]
    assert report["exit_code"] == 0


def test_leg_passes_only_when_all_four_assertions_hold(monkeypatch, tmp_path):
    run, receipt, _ = _leg(monkeypatch, tmp_path)
    result = run()
    assert result["status"] == "pass"
    assert result["scenario"] == LEG
    assert result["selection"]["choices_autostart"] is False
    assert result["update_status"]["reason"] == "autostart-off"
    assert result["post_update"]["refused"] is True and result["post_update"]["unchanged"] is True
    assert result["post_update"]["status"] == 409
    assert result["post_update"]["body"] == {"code": "conflict", "details": {"reason": "autostart-off"}}
    assert result["capture"]["device_scale_factor"] == 2
    for check in jumps.AUTOSTART_CHECKS:
        assert result["checks"][check] == "pass", check


def test_selection_record_must_say_autostart_is_false(monkeypatch, tmp_path):
    run, receipt, _ = _leg(monkeypatch, tmp_path, choices_autostart=True)
    with pytest.raises(RuntimeError, match="selection record does not have choices.autostart false"):
        run()
    recorded = json.loads(receipt.read_text())
    assert recorded["status"] == "fail"
    assert recorded["checks"]["selection_autostart_false"] == "not-run"


def test_update_status_must_report_autostart_off(monkeypatch, tmp_path):
    run, receipt, _ = _leg(monkeypatch, tmp_path, status_reason="none")
    with pytest.raises(RuntimeError, match="GET /update/status does not report reason autostart-off"):
        run()
    assert json.loads(receipt.read_text())["checks"]["update_status_autostart_off"] == "not-run"


def test_refused_post_must_change_nothing(monkeypatch, tmp_path):
    message = "POST update was not refused with autostart-off, or it changed the selection, the data or the live Runtime"
    run, receipt, _ = _leg(monkeypatch, tmp_path, post_reason="busy")
    with pytest.raises(RuntimeError, match=message):
        run()
    assert json.loads(receipt.read_text())["checks"]["post_update_refused_unchanged"] == "not-run"
    run, _, _ = _leg(monkeypatch, tmp_path, runtime_after="changed")
    with pytest.raises(RuntimeError, match=message):
        run()
    run, _, _ = _leg(monkeypatch, tmp_path, data_after_change=True)
    with pytest.raises(RuntimeError, match=message):
        run()


@pytest.mark.parametrize("body", [
    {"code": "conflict", "details": {"reason": "busy"}},
    {"code": "conflict"},
])
def test_post_with_other_or_missing_details_reason_is_not_refused(monkeypatch, tmp_path, body):
    run, _, _ = _leg(monkeypatch, tmp_path)
    monkeypatch.setattr(jumps, "runtime_request", lambda method, path:
                        (200, {"available": False, "reason": "autostart-off"}) if method == "GET"
                        else (409, body))
    with pytest.raises(RuntimeError, match="POST update was not refused with autostart-off"):
        run()


def test_post_receipt_body_redacts_credentials():
    assert jumps._safe_runtime_body({"code": "conflict", "access_token": "sensitive",
                                     "message": "Bearer sensitive"}) == {
        "code": "conflict", "access_token": "<redacted>", "message": "Bearer <redacted>"}


def test_the_autostart_card_capture_must_be_at_device_scale_factor_two(monkeypatch, tmp_path):
    run, receipt, _ = _leg(monkeypatch, tmp_path, capture_dpr=1)
    with pytest.raises(RuntimeError, match="autostart card capture is absent or not at deviceScaleFactor 2"):
        run()
    assert json.loads(receipt.read_text())["checks"]["autostart_card_capture"] == "not-run"


def test_install_exit_must_be_zero(monkeypatch, tmp_path):
    run, receipt, _ = _leg(monkeypatch, tmp_path, install_exit=3)
    with pytest.raises(RuntimeError, match="install --autostart off exited 3"):
        run()
    assert json.loads(receipt.read_text())["checks"]["install_exit"] == "not-run"


def test_the_two_autostart_rows_are_linux_and_macos_direct_rows():
    matrix = {row["job_id"]: row for row in journey_matrix.RELEASE1_MATRIX if row["job_id"] in ROWS}
    assert set(matrix) == set(ROWS)
    assert matrix["direct-linux-autostart-off"]["runner"] == "ubuntu-24.04"
    assert matrix["direct-macos-autostart-off"]["runner"] == "macos-15"
    assert all(row["leg"] == LEG and row["mode"] == "direct" and row["shell"] == "sh" for row in matrix.values())


def failing_capture(*_args):
    raise RuntimeError("intercept refused")


def built_dist(tmp_path):
    dist = tmp_path / "app-dist"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (dist / "build.json").write_text("{}", encoding="utf-8")
    return dist


def test_a_linux_build_failure_fails_the_leg(monkeypatch, tmp_path):
    run, receipt, _ = _leg(monkeypatch, tmp_path, build_error="npm ci exited 1")
    with pytest.raises(RuntimeError, match="npm ci exited 1"):
        run()
    recorded = json.loads(receipt.read_text())
    assert recorded["status"] == "fail"
    assert recorded["checks"]["autostart_card_capture"] == "not-run"


def test_the_capture_runs_inside_the_astrid_haus_intercept_at_the_approved_route(monkeypatch, tmp_path):
    from scripts import journey_hosted_update as hosted
    events, configs = [], []

    class FakeIntercept:
        def __init__(self, routes, *, types=None, **_kwargs):
            self.leaf_spki = "spki-pin"

        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *_exc):
            events.append("exit")

    class Done:
        returncode = 0

    def fake_run(argv, **_kwargs):
        config = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        configs.append(config)
        events.append("node")
        Path(config["screenshot"]).write_bytes(b"png")
        Path(config["result"]).write_text(json.dumps({"ok": True, "device_scale_factor": 2}), encoding="utf-8")
        return Done()

    monkeypatch.setattr(hosted, "Intercept", FakeIntercept)
    monkeypatch.setattr(hosted, "page_routes", lambda dist: ({"/": dist / "index.html"}, {}))
    monkeypatch.setattr(hosted, "release_routes", lambda app, manifest: {})
    monkeypatch.setattr(jumps.subprocess, "run", fake_run)
    app = tmp_path / "app"
    (app / "public").mkdir(parents=True)
    (app / "public" / "install-manifest.json").write_text("{}", encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (dist / "build.json").write_text("{}", encoding="utf-8")
    capture = jumps.capture_autostart_card(tmp_path / "evidence", app, dist, tmp_path / "capture-browser")
    assert events == ["enter", "node", "exit"]
    assert configs[0]["playwright"] == str(tmp_path / "capture-browser" / "node_modules" / "playwright" / "index.mjs")
    assert configs[0]["origin"] == jumps.AUTOSTART_ORIGIN == "https://astrid.haus"
    assert configs[0]["entry"] == jumps.AUTOSTART_ENTRY == "/tools/video-editor?settings=updates"
    assert configs[0]["text"] == jumps.AUTOSTART_CARD_TEXT
    assert configs[0]["spki"] == "spki-pin" and configs[0]["dpr"] == 2
    assert capture["device_scale_factor"] == 2 and capture["path"] == "autostart-off-card.png"


def test_the_wait_text_is_the_approved_card_sentence_and_the_script_waits_for_it():
    assert jumps.AUTOSTART_CARD_TEXT == (
        "Start at sign-in is off, so update from a terminal. This command updates Astrid and starts it; "
        "your choice stays off.")
    script = (journey.ROOT / "scripts" / "journey_autostart_capture.mjs").read_text(encoding="utf-8")
    assert "waitFor" in script and "Run this command" not in script and "saved autostart is off" not in script


def test_a_macos_capture_failure_is_recorded_as_unverified(monkeypatch, tmp_path):
    run, receipt, _ = _leg(monkeypatch, tmp_path)
    monkeypatch.setattr(jumps, "capture_autostart_card", failing_capture)
    monkeypatch.setattr(jumps.sys, "platform", "darwin")
    result = run()
    assert result["status"] == "pass"
    assert result["checks"]["autostart_card_capture"] == "unverified"
    assert result["capture"]["status"] == "unverified" and "intercept refused" in result["capture"]["error"]
    for check in ("selection_autostart_false", "update_status_autostart_off", "post_update_refused_unchanged"):
        assert result["checks"][check] == "pass", check


def test_a_linux_capture_failure_fails_the_leg(monkeypatch, tmp_path):
    run, receipt, _ = _leg(monkeypatch, tmp_path)
    monkeypatch.setattr(jumps, "capture_autostart_card", failing_capture)
    with pytest.raises(RuntimeError, match="intercept refused"):
        run()
    assert json.loads(receipt.read_text())["checks"]["autostart_card_capture"] == "not-run"


def test_the_macos_assertions_are_recorded_before_the_build(monkeypatch, tmp_path):
    run, receipt, _ = _leg(monkeypatch, tmp_path)
    monkeypatch.setattr(jumps.sys, "platform", "darwin")
    seen = {}

    def build_after_checkpoint(app_path, evidence_path):
        checkpoint = json.loads(receipt.read_text(encoding="utf-8"))
        seen.update({key: checkpoint["checks"][key] for key in
                     ("selection_autostart_false", "update_status_autostart_off", "post_update_refused_unchanged")})
        seen["capture_before_build"] = checkpoint["checks"]["autostart_card_capture"]
        return built_dist(tmp_path)

    monkeypatch.setattr(jumps, "build_hosted_app", build_after_checkpoint)
    result = run()
    assert seen == {"selection_autostart_false": "pass", "update_status_autostart_off": "pass",
                    "post_update_refused_unchanged": "pass", "capture_before_build": "not-run"}
    assert result["status"] == "pass"


def test_a_failing_macos_build_leaves_the_assertions_and_the_row_passes_with_capture_unverified(monkeypatch, tmp_path):
    run, receipt, _ = _leg(monkeypatch, tmp_path, build_error="npm ci exited 1")
    monkeypatch.setattr(jumps.sys, "platform", "darwin")
    result = run()
    assert result["status"] == "pass"
    for check in ("install_exit", "selection_autostart_false", "update_status_autostart_off",
                  "post_update_refused_unchanged", "service", "health", "unrelated_sentinel"):
        assert result["checks"][check] == "pass", check
    assert result["checks"]["autostart_card_capture"] == "unverified"
    assert result["capture"]["status"] == "unverified" and "npm ci exited 1" in result["capture"]["error"]


def test_the_hosted_app_is_built_with_npm_ci_then_build_hosted(monkeypatch, tmp_path):
    calls = []

    class Done:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs.get("cwd")))
        if argv[-1] == "build:hosted":
            dist = tmp_path / "app" / "dist"
            dist.mkdir(parents=True)
            (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
            (dist / "build.json").write_text("{}", encoding="utf-8")
        return Done()

    monkeypatch.setattr(jumps.subprocess, "run", fake_run)
    dist = jumps.build_hosted_app(tmp_path / "app", tmp_path / "evidence")
    assert dist == tmp_path / "app" / "dist"
    assert calls == [(["npm", "ci", "--legacy-peer-deps"], tmp_path / "app"),
                     (["npm", "run", "build:hosted"], tmp_path / "app")]


def test_the_capture_browser_is_pinned_to_playwright_1_47_2(monkeypatch, tmp_path):
    calls = []

    class Done:
        returncode = 0

    monkeypatch.setattr(jumps.subprocess, "run", lambda argv, **kwargs: calls.append(list(argv)) or Done())
    directory = jumps.install_capture_browser(tmp_path / "program")
    assert directory == tmp_path / "autostart-capture"
    assert calls[0][:4] == ["npm", "install", "--prefix", str(directory)] and "playwright@1.47.2" in calls[0]
    assert calls[1][-3:] == ["install", "--with-deps", "chromium"]


def test_the_workflow_leaves_the_build_and_the_capture_browser_to_the_leg(monkeypatch):
    workflow = (journey.ROOT / ".github" / "workflows" / "journey.yml").read_text(encoding="utf-8")
    assert "Install the autostart-off capture browser" not in workflow
    assert "ASTRID_AUTOSTART_CAPTURE_DIR" not in workflow
    build = workflow.split("      - name: Build the candidate's hosted App and the browser that clicks it\n", 1)[1]
    assert build.split("\n", 1)[0] == "        if: matrix.job_id == 'direct-linux-fresh'"
    assert "matrix.leg == 'fresh-install-autostart-off'" in workflow.split("      - name: Set up Node", 1)[1].split("\n", 2)[1]


def _live_workspace_host(monkeypatch, tmp_path):
    calls = []
    data = tmp_path / "data"
    monkeypatch.setattr(jumps, "selected_data_root", lambda _root, selected, **_k: selected)
    monkeypatch.setattr(jumps.journey, "assert_direct_service_ownership",
                        lambda *_a, **_k: calls.append("direct") or {"manager": "systemd-user"})
    monkeypatch.setattr(jumps.journey, "healthy", lambda *_a: {"runtime_instance_id": "i1"})
    monkeypatch.setattr(jumps.live, "selection_for", lambda *_a: {"realm_root": str(tmp_path / "realm")})
    monkeypatch.setattr(jumps.journey, "workspace_root", lambda *_a: tmp_path / "realm")
    monkeypatch.setattr(jumps.live, "check_selection", lambda *_a: None)
    monkeypatch.setattr(jumps.journey, "run", lambda *_a, **_k: json.dumps({"project": "film"}))
    return calls, data


def test_autostart_off_live_workspace_uses_the_detached_check_before_and_after(monkeypatch, tmp_path):
    calls, data = _live_workspace_host(monkeypatch, tmp_path)
    result = jumps.live_workspace(tmp_path / "program", data, tmp_path / "ev", "seed", {"project": "film"},
                                  ownership=lambda root, owner: calls.append("detached"))
    assert result == {"project": "film"}
    # Both the pre-check and the post-check use the leg's hook; the foreground-banning check is never reached.
    assert calls == ["detached", "detached"]


def test_ordinary_live_workspace_keeps_the_direct_check_before_and_after(monkeypatch, tmp_path):
    calls, data = _live_workspace_host(monkeypatch, tmp_path)
    jumps.live_workspace(tmp_path / "program", data, tmp_path / "ev", "seed", {"project": "film"})
    assert calls == ["direct", "direct"]
