"""Focused controls for the bounded authentic release-0 -> release-1 leg."""
import argparse
from pathlib import Path

import pytest

from scripts import journey_version_jumps as jumps


@pytest.mark.parametrize(("task_name", "expected"), [
    (None, "Astrid Runtime"),
    ("Astrid Runtime R1", "Astrid Runtime R1"),
])
def test_windows_start_direct_runtime_uses_selected_task_name(tmp_path, monkeypatch, task_name, expected):
    calls = []
    monkeypatch.setattr(jumps.sys, "platform", "win32")
    monkeypatch.setattr(jumps.journey.windows, "powershell", lambda *args, **kwargs: calls.append((args, kwargs)))

    if task_name is None:
        jumps.start_direct_runtime(tmp_path / "evidence")
    else:
        jumps.start_direct_runtime(tmp_path / "evidence", task_name=task_name)

    assert calls == [((f"Start-ScheduledTask -TaskName '{expected}' -TaskPath '\\'",
                       "start-scheduled-task.log"), {"show": False, "timeout": 30})]


def test_release0_fixture_is_the_approved_hashed_input_set():
    root, manifest, receipt = jumps.verified_release0_fixture(jumps.RELEASE0_FIXTURE)
    assert root == jumps.RELEASE0_FIXTURE.resolve()
    assert manifest == root / "served" / "install-manifest.json"
    assert receipt["fixture_sums_sha256"] == jumps.RELEASE0_SUMS_SHA256
    assert receipt["sources"] == jumps.RELEASE0_REFS
    assert set(receipt["bootstrap_sha256"]) == {"install", "install.ps1"}


def test_release0_bootstrap_uses_the_frozen_served_script_without_a_url_fallback(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(jumps.sys, "platform", "linux")
    monkeypatch.setattr(jumps.journey, "run", lambda argv, log, **kwargs: calls.append((argv, log, kwargs)))
    root, manifest, _receipt = jumps.verified_release0_fixture(jumps.RELEASE0_FIXTURE)
    program, data = tmp_path / "program", tmp_path / "data"
    jumps.bootstrap_release0(root, manifest, "sh", tmp_path / "evidence", program, data)
    argv, log, _kwargs = calls[0]
    assert argv == ["sh", root / "served" / "install", "--manifest", manifest,
                    "--root", program, "--data-root", data]
    assert log.name == "release0-bootstrap.log"


def test_windows_release0_bootstrap_drops_inherited_powershell_module_path(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(jumps.sys, "platform", "win32")
    monkeypatch.setenv("PSModulePath", r"C:\pwsh\Modules;C:\pwsh\7\Modules")
    monkeypatch.setenv("KEEP_ME", "preserved")
    monkeypatch.setattr(jumps.journey, "run", lambda argv, log, **kwargs: calls.append((argv, log, kwargs)))

    root, manifest, _receipt = jumps.verified_release0_fixture(jumps.RELEASE0_FIXTURE)
    program, data = tmp_path / "program", tmp_path / "data"
    jumps.bootstrap_release0(root, manifest, "powershell.exe", tmp_path / "evidence", program, data)

    argv, log, kwargs = calls[0]
    assert argv == ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", root / "served" / "install.ps1", "--manifest", manifest,
                    "--root", program, "--data-root", data]
    assert log.name == "release0-bootstrap.log"
    assert all(key.casefold() != "psmodulepath" for key in kwargs["env"])
    assert kwargs["env"]["KEEP_ME"] == "preserved"


@pytest.mark.parametrize("changes", (
    {"mode": "pinokio-backend"},
    {"allow_release0_to_release1": False},
    {"source_override": True},
    {"release0_fixture": None},
))
def test_release0_admission_is_narrow_and_rejects_general_bypasses(changes):
    values = {"mode": "direct", "job_id": "direct-linux", "candidate": Path("candidate.json"),
              "allow_release0_to_release1": True, "release0_fixture": jumps.RELEASE0_FIXTURE,
              "source_override": False}
    values.update(changes)
    with pytest.raises(ValueError, match="release0-to-release1"):
        jumps.release0_admission(argparse.Namespace(**values))


def test_seeded_release0_leg_is_not_admitted_on_windows(monkeypatch, tmp_path):
    args = argparse.Namespace(leg="release0-to-release1")
    monkeypatch.setattr(jumps.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="narrowed refusal"):
        jumps.execute_owned(args, tmp_path / "program", tmp_path / "data", tmp_path / "evidence",
                            argparse.Namespace(pid=1), "birth")


def test_release0_to_release1_receipt_exposes_release1_service_manager(monkeypatch, tmp_path):
    app, candidate_path, root, data = (tmp_path / name for name in ("app", "candidate.json", "program", "data"))
    app.mkdir()
    candidate_path.write_text("{}", encoding="utf-8")
    evidence = tmp_path / "evidence"
    manifest = {"release": 1, "agent": {"sha": "agent-r1"}, "runtime": {"sha": "runtime-r1"}}
    old_service = {"manager": "systemd-user", "foreground_fallback": False, "release": 0}
    new_service = {"manager": "systemd-user", "foreground_fallback": False, "release": 1}
    state = {"release": 0}
    old_version = {"root": str(root / "versions/r0")}
    new_version = {"root": str(root / "versions/r1")}
    args = argparse.Namespace(job_id="direct-linux", mode="direct", leg="release0-to-release1",
                              shell="sh", candidate_checkouts={}, service_manager_prerequisite={},
                              app_checkout=app, candidate=candidate_path)

    monkeypatch.setattr(jumps, "release0_admission", lambda *_: (tmp_path / "fixture", tmp_path / "r0.json", {}))
    monkeypatch.setattr(jumps.journey, "selected_manifest", lambda *_args, **_kwargs: (app / "manifest.json", manifest))
    monkeypatch.setattr(jumps.journey, "read_json", lambda path: {"manifest_sha256": "candidate", "artifacts": {}}
                        if Path(path) == candidate_path else {})
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *_: manifest)
    monkeypatch.setattr(jumps.journey, "git_source", lambda *_: {"sha": "app-r1"})
    monkeypatch.setattr(jumps.journey, "sentinel_observation", lambda *_: {"alive": True})
    monkeypatch.setattr(jumps, "bootstrap_release0", lambda *_: None)
    monkeypatch.setattr(jumps.journey, "bootstrap", lambda *_args, **_kwargs: state.update(release=1))
    monkeypatch.setattr(jumps, "start_direct_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jumps, "selected_data_root", lambda _root, selected_data: selected_data)
    monkeypatch.setattr(jumps.journey, "wait_ready", lambda *_: None)
    monkeypatch.setattr(jumps.journey, "installation", lambda *_: (
        Path((new_version if state["release"] else old_version)["root"]) / "version.json", data,
        new_version if state["release"] else old_version))
    monkeypatch.setattr(jumps, "installed_release", lambda _version: state["release"])
    monkeypatch.setattr(jumps.journey, "selected_tuple", lambda *_: {"agent": "old-agent", "runtime": "old-runtime"})
    monkeypatch.setattr(jumps.journey, "assert_direct_service_ownership",
                        lambda *_args, release=0: new_service if release == 1 else old_service)
    monkeypatch.setattr(jumps, "write_diagnostic_release0_preconditions", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jumps.journey, "verify", lambda mode, *_args, **_kwargs: (
        state.update(release=1) if mode == "install" else None))
    monkeypatch.setattr(jumps, "live_workspace", lambda *_args, **_kwargs: (
        {"project": "film"} if _args[3] == "seed" else {
            "project": "film", "timeline": "kept", "preferences": "kept", "task": "settled",
            "cas_sha256": "abc"}))
    monkeypatch.setattr(jumps.journey, "healthy", lambda *_: {"runtime_instance_id": str(state["release"])})
    monkeypatch.setattr(jumps, "selected_candidate", lambda *_: {"agent": "agent-r1", "runtime": "runtime-r1"})
    monkeypatch.setattr(jumps, "task_after_update", lambda *_args: "task-id")
    monkeypatch.setattr(jumps, "assert_preserved", lambda *_: None)
    monkeypatch.setattr(jumps, "semantic_digest", lambda *_: "same-content")

    result = jumps.execute_release0_to_release1(args, root, data, evidence,
                                               argparse.Namespace(pid=123), "sentinel-birth")

    assert result["status"] == "pass"
    assert result["service_transition"]["release1"] == new_service
    assert result["service_manager"] == new_service


def test_semantic_snapshot_requires_every_seeded_data_class():
    before = {"project": "project", "timeline": "timeline", "preferences": "preferences",
              "task": "task", "cas_sha256": "cas", "schema_version": 1, "target_schema_version": 1}
    after = {**before, "schema_version": 2, "target_schema_version": 2}
    jumps.assert_preserved(before, after)
    with pytest.raises(RuntimeError, match="seeded workspace task"):
        jumps.assert_preserved({key: value for key, value in before.items() if key != "task"}, after)
    for invalid_before, invalid_after in (({**before, "schema_version": 0}, after),
                                          (before, {**after, "schema_version": 1})):
        with pytest.raises(RuntimeError, match="does not match its Runtime schema"):
            jumps.assert_preserved(invalid_before, invalid_after)
    for snapshot in (before, after):
        invalid = {**snapshot, "target_schema_version": 999}
        with pytest.raises(RuntimeError, match="Runtime schema"):
            jumps.assert_preserved(invalid if snapshot is before else before,
                                   invalid if snapshot is after else after)
