"""Focused controls for the r0-handoff-then-update leg.

The leg starts where real pre-R1 installs are: a frozen R0, handed off to R1
storage by the published Release 9 installer, then the candidate's public
``install --update``. These tests drive the leg body with fakes so that the
two assertions it exists for are pinned: the update exits 0, and the selected
release afterwards is the candidate's.
"""
import argparse
import json
from pathlib import Path

import pytest

from scripts import ci_journey as journey
from scripts import journey_fresh_update as fresh
from scripts import journey_version_jumps as jumps


def _leg(monkeypatch, tmp_path, *, update_exit=0, update_installs=True, receipt_kind="updated",
         live=None):
    app, candidate_path, root = (tmp_path / name for name in ("app", "candidate.json", "program"))
    data = tmp_path / "data"
    app.mkdir()
    candidate_path.write_text("{}", encoding="utf-8")
    manifest = {"release": 10, "agent": {"sha": "agent-r10"}, "runtime": {"sha": "runtime-r10"}}
    versions = {"r0": {"root": str(root / "versions/r0")}, "r9": {"root": str(root / "versions/r9")},
                "r10": {"root": str(root / "versions/r10")}}
    state = {"current": "r0", "updated": False}
    bootstraps = []
    args = argparse.Namespace(job_id="direct-linux", mode="direct", leg=jumps.R0_HANDOFF_LEG,
                              shell="sh", candidate_checkouts={}, service_manager_prerequisite={},
                              app_checkout=app, candidate=candidate_path)

    def installation(*_args):
        version = versions[state["current"]]
        return Path(version["root"]) / "version.json", data, version

    def handoff(source, manifest_path, shell, mode, evidence, *_rest, **_kwargs):
        bootstraps.append((source, mode))
        state["current"] = "r9"

    def candidate_update(*_args):
        if update_installs:
            state["current"] = "r10"
        state["updated"] = True
        return {"command": "install --update", "exit_code": update_exit, "log": "update.log", "last_lines": []}

    release = {"r0": 0, "r9": 9, "r10": 10}
    monkeypatch.setattr(jumps, "r0_handoff_admission", lambda *_: (
        (tmp_path / "fixture", tmp_path / "r0.json", {"fixture": "r0"}),
        (tmp_path / "release9-source", tmp_path / "r9.json", {"app_commit": jumps.RELEASE9_APP_COMMIT})))
    monkeypatch.setattr(jumps.journey, "selected_manifest", lambda *_a, **_k: (app / "manifest.json", manifest))
    monkeypatch.setattr(jumps.journey, "read_json", lambda path: {"manifest_sha256": "candidate", "artifacts": {}}
                        if Path(path) == candidate_path else {})
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *_: manifest)
    monkeypatch.setattr(jumps.journey, "git_source", lambda *_: {"sha": "app-r10"})
    monkeypatch.setattr(jumps.journey, "sentinel_observation", lambda *_: {"alive": True})
    monkeypatch.setattr(jumps, "bootstrap_release0", lambda *_args: None)
    monkeypatch.setattr(jumps.journey, "bootstrap", handoff)
    monkeypatch.setattr(jumps, "candidate_update", candidate_update)
    monkeypatch.setattr(jumps, "run_receipt", lambda _data: {
        "format": "astrid-last-run/v1", "kind": receipt_kind, "exit_code": 0 if receipt_kind == "updated" else 3,
        "start": 9, "still": 10 if receipt_kind == "updated" else 9, "target": 10})
    monkeypatch.setattr(jumps, "live_runtime_owners", lambda _data: (
        live if live is not None else [{"data_root": str(data), "pid": 1, "runtime_instance_id": "new"}]))
    monkeypatch.setattr(jumps, "selected_data_root", lambda _root, selected, **_k: selected)
    monkeypatch.setattr(jumps.journey, "wait_ready", lambda *_: None)
    monkeypatch.setattr(jumps.journey, "installation", installation)
    monkeypatch.setattr(jumps, "installed_release", lambda version: next(
        release[name] for name, value in versions.items() if value is version))
    monkeypatch.setattr(jumps.journey, "selected_tuple", lambda *_: {"agent": "old", "runtime": "old"})
    monkeypatch.setattr(jumps.journey, "assert_direct_service_ownership",
                        lambda *_a, release=0: {"manager": "systemd-user", "foreground_fallback": False,
                                                "phase": release})
    monkeypatch.setattr(jumps, "handed_off_start", lambda *_: {"phase": "complete",
                                                               "r0_data_root": str(data),
                                                               "r1_data_root": str(data) + "-r1"})
    monkeypatch.setattr(jumps.journey, "verify", lambda *_a, **_k: None)
    monkeypatch.setattr(jumps, "live_workspace", lambda *_args, **_kwargs: (
        {"project": "film"} if _args[3] == "seed" else {
            "project": "film", "timeline": "kept", "preferences": "kept", "task": "settled", "cas_sha256": "abc"}))
    monkeypatch.setattr(jumps.journey, "healthy", lambda *_: {
        "runtime_instance_id": "new" if state["current"] == "r10" else "old",
        "identity": {"release": release[state["current"]]}})
    monkeypatch.setattr(jumps, "selected_candidate", lambda *_: {"agent": "agent-r10", "runtime": "runtime-r10"})
    monkeypatch.setattr(jumps, "task_after_update", lambda *_args: "task-id")
    monkeypatch.setattr(jumps, "assert_preserved", lambda *_: None)
    monkeypatch.setattr(jumps, "semantic_digest", lambda *_: "same-content")

    def run():
        return jumps.execute_r0_handoff_then_update(args, root, data, tmp_path / "evidence",
                                                    argparse.Namespace(pid=123), "birth")
    return run, bootstraps, tmp_path / "evidence" / "journey-result.json"


def test_leg_passes_only_when_update_installs_the_candidate(monkeypatch, tmp_path):
    run, bootstraps, _ = _leg(monkeypatch, tmp_path)
    result = run()
    assert result["status"] == "pass"
    # The handoff is the published Release 9 installer's --update, from the frozen R0.
    assert bootstraps == [(tmp_path / "release9-source", "update")]
    assert result["handoff"]["phase"] == "complete" and result["handoff"]["release"] == 9
    assert result["update"]["exit_code"] == 0
    assert result["release_transition"] == {"from": 9, "to": 10, "start_state": "r0-handoff-complete",
                                            "source": "candidate-install-update"}
    assert result["health_transition"]["candidate_live_release"] == 10
    assert all(value == "pass" for value in result["checks"].values())


def test_update_that_exits_zero_and_installs_nothing_fails(monkeypatch, tmp_path):
    """The field failure: exit 0, still the handed-off release."""
    run, _, receipt = _leg(monkeypatch, tmp_path, update_installs=False)
    with pytest.raises(RuntimeError, match="exited 0 but installed nothing: the selected release is still 9, "
                                           "not the candidate release 10"):
        run()
    recorded = json.loads(receipt.read_text())
    assert recorded["status"] == "fail"
    assert recorded["checks"]["handed_off_start"] == "pass"
    assert recorded["checks"]["update_exit"] == "pass"
    assert recorded["checks"]["release"] == "not-run"


def test_update_that_exits_nonzero_fails_on_the_exit_code(monkeypatch, tmp_path):
    run, _, receipt = _leg(monkeypatch, tmp_path, update_exit=3, update_installs=False)
    with pytest.raises(RuntimeError, match="install --update exited 3"):
        run()
    assert json.loads(receipt.read_text())["checks"]["update_exit"] == "not-run"


def test_candidate_update_reports_the_exit_code_instead_of_raising(monkeypatch, tmp_path):
    calls = []

    class Done:
        returncode = 0

    def fake_run(argv, **kwargs):
        calls.append(argv)
        kwargs["stdout"].write("Astrid is using /run/data-r1; R0 is retained at /run/data.\n")
        return Done()

    monkeypatch.setattr(jumps.sys, "platform", "linux")
    monkeypatch.setattr(jumps.subprocess, "run", fake_run)
    app = tmp_path / "app"
    report = jumps.candidate_update(app, tmp_path / "m.json", "sh", tmp_path / "ev",
                                    tmp_path / "program", tmp_path / "data")
    assert calls == [["sh", str(app / "public" / "install"), "--manifest", str(tmp_path / "m.json"),
                      "--root", str(tmp_path / "program"), "--data-root", str(tmp_path / "data"), "--update"]]
    assert report["exit_code"] == 0 and report["command"] == "install --update"
    assert report["last_lines"] == ["Astrid is using /run/data-r1; R0 is retained at /run/data."]


def test_handed_off_state_is_a_start_state_not_a_forbidden_one(tmp_path):
    data = tmp_path / "data"
    handed_off = {"journals": ["astrid-r1-handoff.json", "astrid-r1-registration.json"],
                  "r1_data_root": str(data) + "-r1", "selection_r1_data_root": str(data) + "-r1",
                  "r1_data_exists": True}
    assert fresh.is_handed_off(handed_off, data)
    assert not fresh.is_handed_off(fresh.NO_BOUNDARY, data)
    assert not fresh.is_handed_off({**handed_off, "journals": []}, data)
    assert not fresh.is_handed_off({**handed_off, "r1_data_root": str(data)}, data)


def test_r0_handoff_admission_needs_a_qualification_job_and_both_pinned_sources(tmp_path):
    values = {"mode": "direct", "job_id": "direct-linux", "candidate": tmp_path / "candidate.json",
              "allow_release0_to_release1": True, "release0_fixture": jumps.RELEASE0_FIXTURE,
              "release9_app_checkout": None, "source_override": False}
    with pytest.raises(ValueError, match="pinned Release-9"):
        jumps.r0_handoff_admission(argparse.Namespace(**values), tmp_path / "source")
    with pytest.raises(ValueError, match="qualification job"):
        jumps.r0_handoff_admission(argparse.Namespace(**{**values, "job_id": None}), tmp_path / "source")
    with pytest.raises(ValueError, match="release-0 start"):
        jumps.r0_handoff_admission(argparse.Namespace(**{**values, "release0_fixture": None}), tmp_path / "source")


def test_r0_handoff_leg_is_not_admitted_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(jumps.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="narrowed refusal"):
        jumps.execute_owned(argparse.Namespace(leg=jumps.R0_HANDOFF_LEG), tmp_path / "program",
                            tmp_path / "data", tmp_path / "evidence", argparse.Namespace(pid=1), "birth")


@pytest.mark.parametrize("drop", ["--release0-fixture", "--release9-app-checkout", "--allow-release0-to-release1"])
def test_driver_requires_both_start_sources_for_the_handoff_leg(tmp_path, monkeypatch, drop):
    monkeypatch.setattr(journey, "execute", lambda *unused: pytest.fail("must reject before execution"))
    monkeypatch.setattr(journey.sys, "platform", "linux")
    argv = ["--leg", jumps.R0_HANDOFF_LEG, "--mode", "direct", "--job-id", "direct-linux",
            "--app-checkout", str(tmp_path), "--run-root", str(tmp_path / "run"),
            "--root", str(tmp_path / "run" / "program"), "--data-root", str(tmp_path / "run" / "data"),
            "--candidate", str(tmp_path / "candidate.json"),
            "--candidate-agent-checkout", str(tmp_path / "agent"),
            "--candidate-runtime-checkout", str(tmp_path / "runtime"),
            "--pinokio-checkout", str(tmp_path / "pinokio"), "--shell", "sh",
            "--release0-fixture", str(tmp_path / "fixture"),
            "--release9-app-checkout", str(tmp_path / "release9"), "--allow-release0-to-release1"]
    index = argv.index(drop)
    del argv[index:index + (1 if drop.startswith("--allow") else 2)]
    with pytest.raises(SystemExit) as failure:
        journey.main(argv)
    assert failure.value.code == 2


def test_update_whose_receipt_is_not_updated_fails_the_run_receipt_check(monkeypatch, tmp_path):
    run, _, receipt = _leg(monkeypatch, tmp_path, receipt_kind="not-updated")
    with pytest.raises(RuntimeError, match="run receipt is not updated to release 10"):
        run()
    recorded = json.loads(receipt.read_text())
    assert recorded["checks"]["release"] == "not-run" and recorded["checks"]["run_receipt"] == "not-run"


def test_a_second_live_runtime_after_the_update_fails_the_leg(monkeypatch, tmp_path):
    data = tmp_path / "data"
    run, _, receipt = _leg(monkeypatch, tmp_path, live=[
        {"data_root": str(data), "pid": 1, "runtime_instance_id": "r0-era"},
        {"data_root": str(data) + "-r1", "pid": 2, "runtime_instance_id": "new"}])
    with pytest.raises(RuntimeError, match="Exactly one live Runtime"):
        run()
    assert json.loads(receipt.read_text())["checks"]["one_runtime"] == "not-run"


def test_run_receipt_is_read_beside_the_candidate_install_log(monkeypatch, tmp_path):
    data = tmp_path / "data"
    log_dir = (tmp_path / "home" / "Library" / "Logs" / "Astrid Runtime" if jumps.sys.platform == "darwin"
               else tmp_path / "home" / ".local" / "state" / "astrid-runtime" / "logs")
    log_dir.mkdir(parents=True)
    assert jumps.run_receipt(data) is None
    (log_dir / "last-run.json").write_text(json.dumps({"kind": "updated"}))
    assert jumps.run_receipt(data) == {"kind": "updated"}
