"""Focused F2 coverage for retained release-0 diagnostic checkpoint fidelity."""

import argparse
import json
from contextlib import nullcontext
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


@pytest.mark.parametrize("failure", ["owner-lock-read", "workspace-read", "checkpoint-write", "verify-launch",
                                   "capture-failure"])
def test_windows_permission_site_preserves_failure_and_distinguishes_operations(tmp_path, monkeypatch, failure):
    args, root, data, realm, evidence = _retained_preseed_state(tmp_path, monkeypatch)
    args.diagnostic_state.unlink()
    args.job_id = None
    args.diagnostic_start_at = None
    args.retain_for_next_leg = True
    monkeypatch.setattr(journey, "run_scope", lambda *unused, **kwargs: (args.run_root, root, data, evidence))
    monkeypatch.setattr(journey, "assert_host_available", lambda *unused: None)
    monkeypatch.setattr(journey, "assert_runner_identity", lambda *unused: None)
    monkeypatch.setattr(journey, "isolated_environment", lambda *unused: nullcontext())
    monkeypatch.setattr(journey, "sentinel_process", lambda *unused: nullcontext((argparse.Namespace(pid=42), "birth")))
    monkeypatch.setattr(journey, "capture_manager_state", lambda *unused: None)
    monkeypatch.setattr(journey, "git_source", lambda unused: {"sha": "5" * 40})
    monkeypatch.setattr(journey, "sentinel_observation", lambda *unused: {"alive": True})
    monkeypatch.setattr(jumps, "bootstrap_release0", lambda *unused: None)
    monkeypatch.setattr(jumps, "start_direct_runtime", lambda *unused: None)
    monkeypatch.setattr(journey, "wait_ready", lambda *unused: None)
    original = PermissionError(13, "private-error-token", "private-filename-token")
    original.winerror = 33
    target = realm / ("owner.lock" if failure == "owner-lock-read" else "private-workspace-token")
    target.write_bytes(b"private-file-content-token")
    read_bytes = Path.read_bytes
    write_json = journey.write_json

    def denied_read(path):
        private_local = "private-local-token"
        if path == target:
            raise original
        return read_bytes(path)

    def denied_write(path, value):
        if path == args.diagnostic_state:
            raise original
        return write_json(path, value)

    def denied_verify(*unused):
        raise original

    if failure in {"owner-lock-read", "workspace-read", "capture-failure"}:
        monkeypatch.setattr(Path, "read_bytes", denied_read)
    elif failure == "checkpoint-write":
        monkeypatch.setattr(journey, "write_json", denied_write)
    monkeypatch.setattr(journey, "verify", denied_verify)
    if failure == "capture-failure":
        monkeypatch.setattr(jumps, "_permission_failure_site", lambda unused: 1 / 0)
    # Exercise the Windows branch without performing any native OS operation.
    with monkeypatch.context() as native:
        native.setattr(jumps.sys, "platform", "win32")
        with pytest.raises(PermissionError) as caught:
            journey.execute(args)
    assert caught.value is original
    receipt = journey.read_json(evidence / "journey-result.json")
    assert receipt["status"] == "fail"
    assert set(receipt["checks"].values()) == {"not-run"}
    site = receipt["permission_failure_site"]
    if failure == "capture-failure":
        assert site == {"status": "unavailable"}
        return
    assert site["errno"] == 13 and site["winerror"] == 33
    expected = {"owner-lock-read": "realm-owner-lock", "workspace-read": "workspace-entry"}
    assert site["hash_target_role"] == expected.get(failure)
    functions = [frame["function"] for frame in site["frames"]]
    assert "execute_release0_to_release1" in functions
    assert ("_bytes_sha256" in functions) == (failure in expected)
    assert functions[-1] == {"checkpoint-write": "denied_write", "verify-launch": "denied_verify"}.get(
        failure, "denied_read")
    assert any(frame["source"] == "scripts/journey_version_jumps.py" for frame in site["frames"])
    encoded = json.dumps(site)
    assert "private-" not in encoded and str(tmp_path) not in encoded
    # The existing named evidence path retains the discriminator without new upload allowances.
    journey.retain_named_evidence(evidence, "before-cleanup")
    staged = evidence.parent.parent / "evidence-upload" / "before-cleanup" / "journey-result.json"
    assert journey.read_json(staged)["permission_failure_site"] == site


def test_permission_site_bounds_deep_traceback_without_messages_or_source():
    def recurse(depth):
        if depth:
            return recurse(depth - 1)
        raise PermissionError(13, "private-message-token")

    try:
        recurse(80)
    except PermissionError as exc:
        site = jumps._permission_failure_site(exc)
    assert len(site["frames"]) == 24 and site["frames_omitted"] > 0
    assert "private-" not in json.dumps(site)
    assert len(json.dumps(site)) < 16384


@pytest.mark.parametrize("receipt_read_denied", [False, True])
def test_windows_permission_in_lifecycle_receipt_finally_is_captured_by_outer_driver(
        tmp_path, monkeypatch, capsys, receipt_read_denied):
    # A PermissionError in a lifecycle finally block bypasses its body's except.
    # Keep that competing operation distinguishable at the real outer boundary.
    run_root = tmp_path / "run"
    root, data = run_root / "program", run_root / "data"
    evidence = run_root / "evidence" / "release0-to-release1"
    args = argparse.Namespace(run_root=run_root, root=root, data_root=data,
                              leg="release0-to-release1", shell="powershell.exe", mode="direct",
                              retain_for_next_leg=True)
    original = PermissionError(13, "private-finally-token")

    def lifecycle(*unused):
        try:
            journey.write_json(evidence / "journey-result.json", {"status": "not-run"})
        finally:
            raise original

    monkeypatch.setattr(journey, "assert_host_available", lambda *unused: None)
    monkeypatch.setattr(journey, "assert_runner_identity", lambda *unused: None)
    monkeypatch.setattr(journey, "isolated_environment", lambda *unused: nullcontext())
    monkeypatch.setattr(journey, "sentinel_process", lambda *unused: nullcontext((argparse.Namespace(pid=42), "birth")))
    monkeypatch.setattr(journey, "capture_manager_state", lambda *unused: None)
    monkeypatch.setattr(journey, "_execute_owned", lifecycle)
    if receipt_read_denied:
        monkeypatch.setattr(journey, "read_json", lambda *unused: (_ for _ in ()).throw(original))
    with monkeypatch.context() as native:
        native.setattr(journey.sys, "platform", "win32")
        with pytest.raises(PermissionError) as caught:
            journey.execute(args)
    assert caught.value is original
    prefix = "Journey permission failure site: "
    line = next(line for line in capsys.readouterr().err.splitlines() if line.startswith(prefix))
    logged_site = json.loads(line[len(prefix):])
    assert logged_site["errno"] == 13 and "private-" not in line
    if receipt_read_denied:
        return
    receipt = journey.read_json(evidence / "journey-result.json")
    assert receipt["status"] == "fail"
    site = receipt["permission_failure_site"]
    assert [frame["function"] for frame in site["frames"]] == ["execute", "lifecycle"]
    assert site["hash_target_role"] is None
    assert "private-" not in json.dumps(site)
    assert site == logged_site
    staged = run_root / "evidence-upload" / "before-cleanup" / "journey-result.json"
    assert journey.read_json(staged)["permission_failure_site"] == site


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


def test_windows_empty_realm_owner_lock_uses_exact_empty_digest_without_read(tmp_path, monkeypatch):
    args, root, data, realm, evidence = _retained_preseed_state(tmp_path, monkeypatch)
    lock = realm / "owner.lock"
    lock.touch()
    original_read = Path.read_bytes
    reads = []

    def denied_lock(path):
        reads.append(path)
        if path == lock:
            raise PermissionError(13, "locked")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", denied_lock)
    with monkeypatch.context() as native:
        native.setattr(jumps.sys, "platform", "win32")
        state = jumps._preseed_workspace_state(data)
        assert state == jumps._preseed_workspace_state(data)
    assert lock not in reads
    expected = [
        {"path": "attachments", "kind": "directory"},
        {"path": "owner.lock", "kind": "file", "sha256": jumps.hashlib.sha256(b"").hexdigest()},
        {"path": "preferences.json", "kind": "file", "sha256": jumps.hashlib.sha256((realm / "preferences.json").read_bytes()).hexdigest()},
        {"path": "realm.sqlite3", "kind": "file", "sha256": jumps.hashlib.sha256((realm / "realm.sqlite3").read_bytes()).hexdigest()},
    ]
    assert state == {"realm_root": str(realm), "entry_count": len(expected),
                     "tree_sha256": jumps.semantic_digest({"entries": expected})}


@pytest.mark.parametrize("target", ["owner.lock", "attachments/owner.lock", "realm.sqlite3"])
def test_windows_denied_nonempty_or_other_workspace_file_fails_closed(tmp_path, monkeypatch, target):
    args, root, data, realm, evidence = _retained_preseed_state(tmp_path, monkeypatch)
    path = realm / target
    path.write_bytes(b"persistent bytes")
    original_read = Path.read_bytes
    def denied(path_to_read):
        if path_to_read == path:
            raise PermissionError(13, "denied")
        return original_read(path_to_read)
    monkeypatch.setattr(Path, "read_bytes", denied)
    with monkeypatch.context() as native:
        native.setattr(jumps.sys, "platform", "win32")
        with pytest.raises(PermissionError):
            jumps._preseed_workspace_state(data)


def test_windows_lock_change_and_persistent_change_refuse_checkpoint(tmp_path, monkeypatch):
    args, root, data, realm, evidence = _retained_preseed_state(tmp_path, monkeypatch)
    lock = realm / "owner.lock"
    lock.touch()
    with monkeypatch.context() as native:
        native.setattr(jumps.sys, "platform", "win32")
        baseline = jumps._preseed_workspace_state(data)
        lock.write_bytes(b"unknown")
        assert jumps._preseed_workspace_state(data) != baseline
        lock.write_bytes(b"")
        (realm / "realm.sqlite3").write_bytes(b"changed")
        assert jumps._preseed_workspace_state(data) != baseline


def test_preseed_directory_access_denial_fails_closed(tmp_path, monkeypatch):
    args, root, data, realm, evidence = _retained_preseed_state(tmp_path, monkeypatch)
    original_scandir = jumps.os.scandir
    def denied(path):
        if Path(path) == realm / "attachments":
            raise PermissionError(13, "directory denied")
        return original_scandir(path)
    monkeypatch.setattr(jumps.os, "scandir", denied)
    with pytest.raises(PermissionError):
        jumps._preseed_workspace_state(data)


def test_windows_changed_lock_refuses_diagnostic_before_seed(tmp_path, monkeypatch):
    args, root, data, realm, evidence = _retained_preseed_state(tmp_path, monkeypatch)
    lock = realm / "owner.lock"
    lock.touch()
    with monkeypatch.context() as native:
        native.setattr(jumps.sys, "platform", "win32")
        args.diagnostic_state.unlink()
        version_path, _, version = journey.installation(root, data)
        fixture, manifest, receipt = jumps.release0_admission(args)
        _, target = journey.selected_manifest(args.app_checkout)
        jumps.write_diagnostic_release0_preconditions(
            args, root, data, fixture_receipt=receipt, release0_manifest=manifest,
            target_manifest=target, candidate=journey.read_json(args.candidate),
            version_path=version_path, version=version)
        lock.write_bytes(b"changed")
        with pytest.raises(RuntimeError, match="workspace changed after its pre-seed checkpoint"):
            jumps.execute_diagnostic_release0_stage(args, root, data, evidence)
    assert journey.read_json(evidence / "journey-result.json")["checks"] == {
        "preconditions": "not-run", "seed": "not-run", "snapshot": "not-run"}


def test_windows_empty_nested_lock_still_requires_read(tmp_path, monkeypatch):
    _, _, data, realm, _ = _retained_preseed_state(tmp_path, monkeypatch)
    nested = realm / "attachments" / "owner.lock"
    nested.touch()
    original_read = Path.read_bytes

    def denied(path):
        if path == nested:
            raise PermissionError(13, "denied")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", denied)
    with monkeypatch.context() as native:
        native.setattr(jumps.sys, "platform", "win32")
        with pytest.raises(PermissionError):
            jumps._preseed_workspace_state(data)


def test_preseed_rejects_changes_between_census_passes(tmp_path, monkeypatch):
    _, _, data, realm, _ = _retained_preseed_state(tmp_path, monkeypatch)
    lock = realm / "owner.lock"
    lock.touch()
    original_scandir = jumps.os.scandir
    root_scans = 0

    def changing(path):
        nonlocal root_scans
        if Path(path) == realm:
            root_scans += 1
            if root_scans == 2:
                lock.write_bytes(b"changed")
        return original_scandir(path)

    monkeypatch.setattr(jumps.os, "scandir", changing)
    with monkeypatch.context() as native:
        native.setattr(jumps.sys, "platform", "win32")
        with pytest.raises(RuntimeError, match="changed while checkpointing"):
            jumps._preseed_workspace_state(data)


def test_windows_owner_lock_symlink_is_rejected(tmp_path, monkeypatch):
    _, _, data, realm, _ = _retained_preseed_state(tmp_path, monkeypatch)
    (realm / "owner.lock").symlink_to(realm / "realm.sqlite3")
    with monkeypatch.context() as native:
        native.setattr(jumps.sys, "platform", "win32")
        with pytest.raises(RuntimeError, match="contains a symlink"):
            jumps._preseed_workspace_state(data)
