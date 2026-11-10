"""Focused F2 coverage for retained release-0 diagnostic checkpoint fidelity."""

import argparse
import os
import subprocess
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
    release9_sources = {"agent": {"sha": jumps.RELEASE9_IDENTITY["agent"]},
                        "runtime": {"sha": jumps.RELEASE9_IDENTITY["runtime"]}}
    release0_manifest = tmp_path / "release9-manifest.json"
    journey.write_json(release0_manifest, {"release": 9, **release9_sources, "tools": {}})
    journey.write_json(version_root / "manifest.json", {"release": 9, **release9_sources, "tools": {}})
    target_manifest = {
        "release": 10,
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
        leg="release9-then-install",
        mode="direct",
        shell="sh",
        job_id=None,
        source_override=False,
        app_checkout=app,
        candidate=candidate_path,
        candidate_agent_checkout=tmp_path / "agent",
        candidate_runtime_checkout=tmp_path / "runtime",
        pinokio_checkout=tmp_path / "pinokio",
        release9_app_checkout=tmp_path / "release9-app",
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
    monkeypatch.setattr(jumps, "release9_admission", lambda unused, output: (
        tmp_path / "source", release0_manifest, fixture_receipt))
    monkeypatch.setattr(journey, "selected_manifest", lambda *unused, **kwargs: (
        tmp_path / "selected-manifest.json", target_manifest))
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *unused: target_manifest)
    monkeypatch.setattr(journey, "candidate_checkouts", lambda unused: checkouts)
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *unused: {"manager": "test"})
    monkeypatch.setattr(journey, "healthy", lambda *unused: {"pid": 42, "runtime_instance_id": "r0"})
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
    evidence = run_root / "evidence" / "release9-then-install-diagnostic-release0-api-seed"
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
    actions = []
    monkeypatch.setattr(journey, "verify", lambda *unused, **kwargs: actions.append("verify"))
    monkeypatch.setattr(jumps, "task_after_update", lambda *unused: "task-1")

    def workspace(_root, _data, _evidence, action, request):
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

    monkeypatch.setattr(jumps, "live_workspace", workspace)
    result = jumps.execute_diagnostic_release0_stage(args, root, data, evidence)

    assert custody == ["owner", "health"]
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
    monkeypatch.setattr(jumps, "live_workspace", lambda *unused: workspace_calls.append(unused))
    with pytest.raises(RuntimeError, match="workspace changed after its pre-seed checkpoint"):
        jumps.execute_diagnostic_release0_stage(args, root, data, evidence)

    assert workspace_calls == []
    receipt = journey.read_json(evidence / "journey-result.json")
    assert receipt["status"] == "fail"
    assert receipt["checks"] == {"preconditions": "not-run", "seed": "not-run", "snapshot": "not-run"}
    assert receipt["qualification"]["eligible"] is False


def test_live_root_lock_is_inventoried_without_reading_and_data_stays_bound(tmp_path, monkeypatch):
    data = tmp_path / "data"
    realm = data / "runtime" / "realms" / "r0"
    realm.mkdir(parents=True)
    journey.write_json(data / "runtime" / "discovery.json", {"realm_root": str(realm)})
    lock = realm / "owner.lock"
    lock.write_bytes(b"locked byte")
    # Real frozen R0 manifest bytes exercise the ordinary data path; they are
    # fixture inputs, not a claim to be a captured realm database.
    manifest = jumps.RELEASE0_FIXTURE / "served" / "install-manifest.json"
    (realm / "data.json").write_bytes(manifest.read_bytes())
    original_read = Path.read_bytes

    def guarded_read(path):
        if path == lock:
            raise PermissionError("live lock must never be read")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    before = jumps._preseed_workspace_state(data)
    assert before["entry_count"] == 2
    (realm / "data.json").write_bytes(b"changed")
    assert jumps._preseed_workspace_state(data) != before
    (realm / "data.json").write_bytes(original_read(manifest))
    lock.write_bytes(b"different size")
    assert jumps._preseed_workspace_state(data) != before
    lock.unlink()
    assert jumps._preseed_workspace_state(data) != before


@pytest.mark.parametrize("name", ["realm.sqlite3", "nested/owner.lock", "instance.lock"])
def test_unreadable_other_files_are_not_exempt(tmp_path, monkeypatch, name):
    data = tmp_path / "data"
    realm = data / "realm"
    realm.mkdir(parents=True)
    journey.write_json(data / "runtime" / "discovery.json", {"realm_root": str(realm)})
    target = realm / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"data")
    original_read = Path.read_bytes

    def guarded_read(path):
        if path == target:
            raise PermissionError("unreadable data")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    with pytest.raises(PermissionError, match="unreadable data"):
        jumps._preseed_workspace_state(data)


def test_census_rejects_enumeration_failure_and_symlink(tmp_path, monkeypatch):
    data = tmp_path / "data"
    realm = data / "realm"
    (realm / "nested").mkdir(parents=True)
    journey.write_json(data / "runtime" / "discovery.json", {"realm_root": str(realm)})
    original_scandir = os.scandir

    def guarded_scandir(path):
        if path == realm / "nested":
            raise PermissionError("cannot enumerate")
        return original_scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", guarded_scandir)
        with pytest.raises(PermissionError, match="cannot enumerate"):
            jumps._preseed_workspace_state(data)
    try:
        (realm / "owner.lock").symlink_to(realm / "nested", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(RuntimeError, match="symlink or reparse point"):
        jumps._preseed_workspace_state(data)


def test_census_rejects_changes_between_passes(tmp_path, monkeypatch):
    data = tmp_path / "data"
    realm = data / "realm"
    realm.mkdir(parents=True)
    journey.write_json(data / "runtime" / "discovery.json", {"realm_root": str(realm)})
    lock = realm / "owner.lock"
    lock.write_bytes(b"a")
    original_scandir = os.scandir
    calls = 0

    def changing_scandir(path):
        nonlocal calls
        if path == realm:
            calls += 1
            if calls == 2:
                lock.write_bytes(b"changed size")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", changing_scandir)
    with pytest.raises(RuntimeError, match="changed while checkpointing"):
        jumps._preseed_workspace_state(data)


def test_real_r0_realm_checkpoint_when_runtime_checkout_available(tmp_path, monkeypatch):
    checkout = os.environ.get("JOURNEY_TEST_R0_RUNTIME")
    if not checkout:
        pytest.skip("set JOURNEY_TEST_R0_RUNTIME to the frozen R0 Runtime checkout")
    revision = subprocess.check_output(
        ["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip()
    assert revision == jumps.RELEASE0_REFS["runtime"]
    monkeypatch.syspath_prepend(checkout)
    from runtime_protocol.store import RealmStore

    data = tmp_path / "data"
    realm = data / "runtime" / "realms" / "r0"
    store = RealmStore.initialize(realm)
    try:
        journey.write_json(data / "runtime" / "discovery.json", {"realm_root": str(realm)})
        lock = realm / "owner.lock"
        assert lock.is_file()
        assert (realm / "realm.sqlite3").is_file()
        original_read = Path.read_bytes

        def guarded_read(path):
            if path == lock:
                raise PermissionError("live R0 owner lock must not be read")
            return original_read(path)

        monkeypatch.setattr(Path, "read_bytes", guarded_read)
        before = jumps._preseed_workspace_state(data)
        assert before == jumps._preseed_workspace_state(data)
        (realm / "extra-data").write_bytes(b"changed")
        assert before != jumps._preseed_workspace_state(data)
    finally:
        store.close()


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows byte-range locks")
def test_native_windows_locked_owner_file_is_not_read(tmp_path):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    data = tmp_path / "data"
    realm = data / "realm"
    realm.mkdir(parents=True)
    journey.write_json(data / "runtime" / "discovery.json", {"realm_root": str(realm)})
    lock = realm / "owner.lock"
    lock.write_bytes(b"x")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
    kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, ctypes.POINTER(Overlapped)]
    overlap = Overlapped()
    with lock.open("r+b") as holder:
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(holder.fileno()))
        if not kernel.LockFileEx(handle, 3, 0, 1, 0, ctypes.byref(overlap)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            with pytest.raises(PermissionError):
                lock.read_bytes()
            assert jumps._preseed_workspace_state(data)["entry_count"] == 1
        finally:
            if not kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlap)):
                raise ctypes.WinError(ctypes.get_last_error())
