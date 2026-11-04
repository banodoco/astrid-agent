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
