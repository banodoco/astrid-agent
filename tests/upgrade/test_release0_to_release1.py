"""Focused controls for the bounded authentic release-0 -> release-1 leg."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import journey_version_jumps as jumps


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


def test_special_leg_lifts_only_its_windows_historical_refusal(monkeypatch, tmp_path):
    args = argparse.Namespace(leg="release0-to-release1")
    monkeypatch.setattr(jumps.sys, "platform", "win32")
    monkeypatch.setattr(jumps, "execute_release0_to_release1", lambda *unused: {"status": "special-windows-admitted"})
    assert jumps.execute_owned(args, tmp_path / "program", tmp_path / "data", tmp_path / "evidence",
                               argparse.Namespace(pid=1), "birth") == {"status": "special-windows-admitted"}


def test_semantic_snapshot_requires_every_seeded_data_class():
    before = {"project": "project", "timeline": "timeline", "preferences": "preferences",
              "task": "task", "cas_sha256": "cas"}
    after = {**before, "schema_version": 2, "target_schema_version": 2}
    jumps.assert_preserved(before, after)
    with pytest.raises(RuntimeError, match="seeded workspace task"):
        jumps.assert_preserved({key: value for key, value in before.items() if key != "task"}, after)


def test_direct_leg_records_native_service_semantics_and_tasks_in_api_project(tmp_path, monkeypatch):
    """The unit route covers the receipt contract without faking a hosted OS."""
    app, evidence, data = tmp_path / "app", tmp_path / "evidence", tmp_path / "data"
    app.mkdir()
    release0_manifest = tmp_path / "release0.json"
    release0 = {"release": 0, "agent": {"sha": "old-agent"}, "runtime": {"sha": "old-runtime"}}
    release0_manifest.write_text(json.dumps(release0))
    release1 = {"release": 1, "agent": {"sha": "new-agent"}, "runtime": {"sha": "new-runtime"}}
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({"manifest_sha256": "a" * 64, "artifacts": {}}))
    old_root, new_root = tmp_path / "old", tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    (old_root / "manifest.json").write_text('{"release": 0}')
    (new_root / "manifest.json").write_text('{"release": 1}')
    old = {"root": str(old_root), "identity": None}
    new = {"root": str(new_root), "identity": {"release": 1, "managed_by": None}}
    args = argparse.Namespace(
        job_id="direct-linux", mode="direct", leg="release0-to-release1", shell="sh",
        app_checkout=app, candidate=candidate, candidate_checkouts={},
        service_manager_prerequisite={"required": True, "outcome": "pass"}, source_override=False,
        allow_release0_to_release1=True, release0_fixture=jumps.RELEASE0_FIXTURE,
    )
    monkeypatch.setattr(jumps, "release0_admission", lambda unused: (
        tmp_path / "fixture", release0_manifest,
        {"fixture_sums_sha256": "f" * 64, "manifest_sha256": "m" * 64,
         "provenance_sha256": "p" * 64, "sources": {"agent": "old", "runtime": "old"}},
    ))
    monkeypatch.setattr(jumps.journey, "selected_manifest", lambda *unused, **kwargs: (tmp_path / "release1.json", release1))
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *unused: release1)
    monkeypatch.setattr(jumps.journey, "git_source", lambda unused: {"sha": "app-sha"})
    monkeypatch.setattr(jumps, "bootstrap_release0", lambda *unused: None)
    monkeypatch.setattr(jumps, "start_direct_runtime", lambda *unused: None)
    monkeypatch.setattr(jumps.journey, "bootstrap", lambda *unused: None)
    monkeypatch.setattr(jumps.journey, "wait_ready", lambda *unused: None)
    installed = iter(((old_root / "version.json", data, old), (new_root / "version.json", data, new)))
    monkeypatch.setattr(jumps.journey, "installation", lambda *unused: next(installed))
    monkeypatch.setattr(jumps.journey, "selected_tuple", lambda manifest, unused: {
        "agent": manifest["agent"]["sha"], "runtime": manifest["runtime"]["sha"],
        "schema_digest": "schema", "tools": {"python": "3.11"},
    })
    services = iter(({"manager": "native-old", "foreground_fallback": False},
                     {"manager": "native-new", "foreground_fallback": False}))
    monkeypatch.setattr(jumps.journey, "assert_direct_service_ownership", lambda *unused: next(services))
    health = iter(({"pid": 42, "runtime_instance_id": "old"},
                   {"pid": 43, "runtime_instance_id": "new"}))
    monkeypatch.setattr(jumps.journey, "healthy", lambda *unused: next(health))
    births = iter(("old-birth", None))
    monkeypatch.setattr(jumps.journey, "process_birth", lambda *unused: next(births))
    monkeypatch.setattr(jumps.journey, "assert_one_runtime", lambda *unused: None)
    monkeypatch.setattr(jumps.journey, "sentinel_observation", lambda *unused: {"alive": True})
    monkeypatch.setattr(jumps.journey, "verify", lambda mode, *unused, **kwargs: (
        jumps.journey.write_json(evidence / "verify-install.json", {"details": {"origin.imports": {"ok": True}}})
        if mode == "install" else None))
    semantic = {"project": {"id": "api-project"}, "timeline": {"id": "timeline"},
                "preferences": {"content": "kept"}, "task": {"id": "old-task", "status": "completed"},
                "cas_sha256": "c" * 64, "schema_version": 2, "target_schema_version": 2}
    monkeypatch.setattr(jumps, "workspace", lambda version, unused_data, unused_evidence, action, request: (
        {"project": "api-project", "timeline": "timeline", "object_digest": "object"}
        if action == "seed" else dict(semantic)))
    tasks = []
    monkeypatch.setattr(jumps, "task_after_update", lambda version, unused_evidence, project: (
        tasks.append((version["root"], project)) or "old-task"))

    result = jumps.execute_release0_to_release1(
        args, tmp_path / "program", data, evidence, SimpleNamespace(pid=999), "sentinel-birth")

    assert result["status"] == "pass"
    assert result["service_manager"] == {"manager": "native-new", "foreground_fallback": False}
    assert result["semantic_data"]["identical"] is True
    assert result["checks"]["task_settled"] == "pass"
    assert tasks == [(str(old_root), "api-project"), (str(new_root), "api-project")]
