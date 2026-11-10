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
    with pytest.raises(ValueError, match="release-0 start"):
        jumps.release0_admission(argparse.Namespace(**values))


def test_seeded_release0_leg_is_not_admitted_on_windows(monkeypatch, tmp_path):
    args = argparse.Namespace(leg="release9-then-install")
    monkeypatch.setattr(jumps.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="narrowed refusal"):
        jumps.execute_owned(args, tmp_path / "program", tmp_path / "data", tmp_path / "evidence",
                            argparse.Namespace(pid=1), "birth")


def test_release9_to_candidate_receipt_keeps_default_service_identity(monkeypatch, tmp_path):
    app, candidate_path, root, data = (tmp_path / name for name in ("app", "candidate.json", "program", "data"))
    app.mkdir()
    candidate_path.write_text("{}", encoding="utf-8")
    evidence = tmp_path / "evidence"
    manifest = {"release": 10, "agent": {"sha": "agent-r10"}, "runtime": {"sha": "runtime-r10"}}
    old_service = {"manager": "systemd-user", "foreground_fallback": False, "phase": "release9"}
    new_service = {"manager": "systemd-user", "foreground_fallback": False, "phase": "candidate"}
    services = [old_service, new_service]
    upgraded = {"done": False}
    old_version = {"root": str(root / "versions/r9")}
    new_version = {"root": str(root / "versions/r10")}
    args = argparse.Namespace(job_id="direct-linux", mode="direct", leg="release9-then-install",
                              shell="sh", candidate_checkouts={}, service_manager_prerequisite={},
                              app_checkout=app, candidate=candidate_path)

    def current_version(*_args):
        version = new_version if upgraded["done"] else old_version
        return Path(version["root"]) / "version.json", data, version

    def plain_install(*_args, **_kwargs):
        assert _args[3] == "install"
        upgraded["done"] = True

    monkeypatch.setattr(jumps, "release9_admission", lambda *_: (tmp_path / "source", tmp_path / "r9.json",
                                                                 {"app_commit": jumps.RELEASE9_APP_COMMIT}))
    monkeypatch.setattr(jumps.journey, "selected_manifest", lambda *_args, **_kwargs: (app / "manifest.json", manifest))
    monkeypatch.setattr(jumps.journey, "read_json", lambda path: {"manifest_sha256": "candidate", "artifacts": {}}
                        if Path(path) == candidate_path else {})
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *_: manifest)
    monkeypatch.setattr(jumps.journey, "git_source", lambda *_: {"sha": "app-r10"})
    monkeypatch.setattr(jumps.journey, "sentinel_observation", lambda *_: {"alive": True})
    monkeypatch.setattr(jumps, "bootstrap_release9", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jumps.journey, "bootstrap", plain_install)
    monkeypatch.setattr(jumps, "start_direct_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jumps, "selected_data_root", lambda _root, selected_data: selected_data)
    monkeypatch.setattr(jumps.journey, "wait_ready", lambda *_: None)
    monkeypatch.setattr(jumps.journey, "installation", current_version)
    monkeypatch.setattr(jumps, "installed_release", lambda version: 10 if version is new_version else 9)
    monkeypatch.setattr(jumps.journey, "selected_tuple", lambda *_: {"agent": "old-agent", "runtime": "old-runtime"})
    monkeypatch.setattr(jumps.journey, "assert_direct_service_ownership", lambda *_args, **_kwargs: services.pop(0))
    monkeypatch.setattr(jumps, "write_diagnostic_release0_preconditions", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jumps.journey, "verify", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jumps, "live_workspace", lambda *_args, **_kwargs: (
        {"project": "film"} if _args[3] == "seed" else {
            "project": "film", "timeline": "kept", "preferences": "kept", "task": "settled",
            "cas_sha256": "abc"}))
    monkeypatch.setattr(jumps.journey, "healthy", lambda *_: {
        "runtime_instance_id": "new" if upgraded["done"] else "old"})
    monkeypatch.setattr(jumps, "selected_candidate", lambda *_: {"agent": "agent-r10", "runtime": "runtime-r10"})
    monkeypatch.setattr(jumps, "task_after_update", lambda *_args: "task-id")
    monkeypatch.setattr(jumps, "assert_preserved", lambda *_: None)
    monkeypatch.setattr(jumps, "semantic_digest", lambda *_: "same-content")

    result = jumps.execute_release9_then_install(args, root, data, evidence,
                                               argparse.Namespace(pid=123), "sentinel-birth")

    assert result["status"] == "pass"
    assert result["release_transition"] == {"from": 9, "to": 10, "source": "pinned-release9-plain-install"}
    assert result["service_transition"] == {"release9": old_service, "candidate": new_service}
    assert result["service_manager"] == new_service


@pytest.mark.parametrize("manifest", [
    {"release": 9}, {"release": 1}, {"release": "10"}, {},
])
def test_candidate_must_be_newer_than_release_nine(manifest):
    with pytest.raises(RuntimeError, match="newer than Release 9"):
        jumps.candidate_release(manifest)
    assert jumps.candidate_release({"release": 10}) == 10


@pytest.mark.parametrize(("changes", "message"), [
    ({"mode": "pinokio-backend"}, "pinned Release-9"),
    ({"release9_app_checkout": None}, "pinned Release-9"),
    ({"allow_release0_to_release1": False}, "pinned Release-9"),
    ({"source_override": True}, "source override"),
])
def test_release9_admission_is_narrow(tmp_path, changes, message):
    values = {"mode": "direct", "job_id": "direct-linux", "candidate": tmp_path / "candidate.json",
              "allow_release0_to_release1": True, "release9_app_checkout": tmp_path,
              "source_override": False, **changes}
    with pytest.raises(ValueError, match=message):
        jumps.release9_admission(argparse.Namespace(**values), tmp_path / "source")


def test_release9_source_must_resolve_to_the_pinned_identity(monkeypatch, tmp_path):
    def resolved(identity, commit=jumps.RELEASE9_APP_COMMIT):
        return lambda *_args, **_kwargs: {
            "status": "verified", "app_manifest_commit": commit, "identity": identity,
            "manifest_sha256": "a" * 64, "bootstrap": [], "local_candidate_used": False,
        }
    monkeypatch.setattr(jumps.upgrade_release, "resolve", resolved(jumps.RELEASE9_IDENTITY, commit="b" * 40))
    with pytest.raises(RuntimeError, match="pinned App commit"):
        jumps.verified_release9_source(tmp_path, tmp_path / "source")
    monkeypatch.setattr(jumps.upgrade_release, "resolve", resolved({**jumps.RELEASE9_IDENTITY, "agent": "e" * 40}))
    with pytest.raises(RuntimeError, match="identity differs"):
        jumps.verified_release9_source(tmp_path, tmp_path / "source")


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
