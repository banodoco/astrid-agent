"""Focused F2 coverage for retained release-0 diagnostic checkpoint fidelity."""

import argparse
import sys
from pathlib import Path

import pytest

from scripts import ci_journey as journey
from scripts import journey_version_jumps as jumps


def _retained_preseed_state(tmp_path, monkeypatch):
    """Build the smallest owned R0 realm with a real checkpoint fingerprint."""
    run_root = tmp_path / "run"
    root, data = run_root / "program", run_root / "data"
    version_root = root / "versions" / "release0"
    realm = run_root / "realms" / "release0"
    version_root.mkdir(parents=True)
    realm.mkdir(parents=True)
    (realm / "attachments").mkdir()
    (realm / "realm.sqlite3").write_bytes(b"empty release-0 realm")
    (realm / "preferences.json").write_text('{"theme":"system"}\n', encoding="utf-8")
    journey.write_json(data / "runtime" / "discovery.json", {"realm_root": str(realm)})
    journey.write_json(root / "install.json", {"data_root": str(data)})
    journey.write_json(run_root / journey.OWNER_FILE, {
        "schema_version": 1,
        "run_id": "a" * 32,
        "root": str(root),
        "data_root": str(data),
    })
    (root / "current").write_text("release0\n", encoding="utf-8")
    version_path = version_root / "version.json"
    journey.write_json(version_path, {"root": str(version_root), "python": sys.executable, "env": {}})
    release0_sources = {"agent": {"sha": "1" * 40}, "runtime": {"sha": "2" * 40}}
    release0_manifest = tmp_path / "release0-manifest.json"
    journey.write_json(release0_manifest, {"release": 0, **release0_sources, "tools": {}})
    journey.write_json(version_root / "manifest.json", {"release": 0, **release0_sources, "tools": {}})
    target_manifest = {
        "release": 1,
        "agent": {"sha": "3" * 40},
        "runtime": {"sha": "4" * 40, "schema_digest": "f" * 64},
        "tools": {"python": {"version": "3.11.13"}},
    }
    candidate = {
        "app": "5" * 40,
        "agent": "3" * 40,
        "runtime": "4" * 40,
        "pinokio": "6" * 40,
        "manifest_sha256": "7" * 64,
        "artifacts": {"manifest": "7" * 64},
    }
    candidate_path = tmp_path / "candidate.json"
    journey.write_json(candidate_path, candidate)
    app = tmp_path / "app"
    app.mkdir()
    args = argparse.Namespace(
        run_root=run_root,
        root=root,
        data_root=data,
        leg="release0-to-release1",
        mode="direct",
        shell="sh",
        job_id=None,
        source_override=False,
        app_checkout=app,
        candidate=candidate_path,
        candidate_agent_checkout=tmp_path / "agent",
        candidate_runtime_checkout=tmp_path / "runtime",
        pinokio_checkout=tmp_path / "pinokio",
        release0_fixture=tmp_path / "fixture",
        allow_release0_to_release1=True,
        diagnostic_start_at=jumps.DIAGNOSTIC_START_STAGE,
        diagnostic_state=run_root / jumps.DIAGNOSTIC_STATE_NAME,
    )
    fixture_receipt = {"fixture_sums_sha256": "8" * 64, "manifest_sha256": "9" * 64}
    checkouts = {name: {"sha": sha, "repo": str(tmp_path / name)} for name, sha in {
        "app": "5" * 40,
        "agent": "3" * 40,
        "runtime": "4" * 40,
        "pinokio": "6" * 40,
    }.items()}
    monkeypatch.setattr(jumps, "release0_admission", lambda unused: (
        tmp_path / "fixture", release0_manifest, fixture_receipt))
    monkeypatch.setattr(journey, "selected_manifest", lambda *unused, **kwargs: (
        tmp_path / "selected-manifest.json", target_manifest))
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *unused: target_manifest)
    monkeypatch.setattr(journey, "candidate_checkouts", lambda unused: checkouts)
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *unused: {"manager": "test"})
    monkeypatch.setattr(journey, "healthy", lambda *unused: {"pid": 42, "runtime_instance_id": "r0"})
    monkeypatch.setattr(journey, "assert_one_runtime", lambda *unused: None)
    version_path, _, version = journey.installation(root, data)
    args.candidate_checkouts = checkouts
    jumps.write_diagnostic_release0_preconditions(
        args,
        root,
        data,
        fixture_receipt=fixture_receipt,
        release0_manifest=release0_manifest,
        target_manifest=target_manifest,
        candidate=candidate,
        version_path=version_path,
        version=version,
    )
    evidence = run_root / "evidence" / "release0-to-release1-diagnostic-release0-api-seed"
    return args, root, data, realm, evidence


def test_diagnostic_checkpoint_accepts_only_unchanged_preseed_realm_and_keeps_custody_checks(
        tmp_path, monkeypatch):
    args, root, data, realm, evidence = _retained_preseed_state(tmp_path, monkeypatch)
    checkpoint = journey.read_json(args.diagnostic_state)
    assert checkpoint["preseed_workspace"] == jumps._preseed_workspace_state(data)
    assert checkpoint["preseed_workspace"]["realm_root"] == str(realm)

    custody = []
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *unused: (
        custody.append("owner") or {"manager": "test"}))
    monkeypatch.setattr(journey, "healthy", lambda *unused: custody.append("health") or {"pid": 42})
    monkeypatch.setattr(journey, "assert_one_runtime", lambda *unused: custody.append("one-runtime"))
    actions = []
    monkeypatch.setattr(journey, "verify", lambda *unused, **kwargs: actions.append("verify"))
    monkeypatch.setattr(jumps, "task_after_update", lambda *unused: "task-1")

    def workspace(_root, _version, _data, _evidence, action, request):
        actions.append(action)
        if action == "seed":
            assert request == {
                "project": "tst-m4-3b-release0",
                "generation": "release0",
                "task_id": None,
                "create_project": True,
            }
            return {"project": request["project"]}
        assert request == {"project": "tst-m4-3b-release0", "task_id": "task-1"}
        return {"snapshot": "preseed preserved"}

    monkeypatch.setattr(jumps, "direct_workspace", workspace)
    result = jumps.execute_diagnostic_release0_stage(args, root, data, evidence)

    assert custody == ["owner", "health", "one-runtime"]
    assert actions == ["verify", "seed", "snapshot"]
    assert result["status"] == "pass"
    assert result["qualification"]["eligible"] is False
    assert result["preconditions"]["preseed_workspace"] == checkpoint["preseed_workspace"]


@pytest.mark.parametrize("case", ["partial-seed", "complete-seed", "changed-data"])
def test_diagnostic_checkpoint_refuses_each_changed_retained_realm_before_seed(
        tmp_path, monkeypatch, case):
    args, root, data, realm, evidence = _retained_preseed_state(tmp_path, monkeypatch)
    if case == "partial-seed":
        (realm / "realm.sqlite3-wal").write_bytes(b"partial project write")
    elif case == "complete-seed":
        (realm / "realm.sqlite3").write_bytes(b"complete project, timeline, task, and artifacts")
    else:
        (realm / "preferences.json").write_text('{"theme":"changed"}\n', encoding="utf-8")

    workspace_calls = []
    monkeypatch.setattr(jumps, "direct_workspace", lambda *unused: workspace_calls.append(unused))
    with pytest.raises(RuntimeError, match="workspace changed after its pre-seed checkpoint"):
        jumps.execute_diagnostic_release0_stage(args, root, data, evidence)

    assert workspace_calls == []
    receipt = journey.read_json(evidence / "journey-result.json")
    assert receipt["status"] == "fail"
    assert receipt["checks"] == {"preconditions": "not-run", "seed": "not-run", "snapshot": "not-run"}
    assert receipt["qualification"]["eligible"] is False
