"""Safe diagnostics on the unchanged release-0 checkpoint traversal."""
import argparse
from contextlib import nullcontext
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from scripts import ci_journey as journey
from scripts import journey_version_jumps as jumps


def realm_at(tmp_path):
    data = tmp_path / "data"
    realm = data / "realm"
    realm.mkdir(parents=True)
    journey.write_json(data / "runtime" / "discovery.json", {"realm_root": str(realm)})
    return data, realm


@pytest.mark.parametrize("operation,name,expected", [
    ("rglob", "realm.sqlite3", "<redacted>"),
    ("is_symlink", "realm.sqlite3", "realm.sqlite3"),
    ("is_dir", "realm.sqlite3", "realm.sqlite3"),
    ("is_file", "realm.sqlite3", "realm.sqlite3"),
    ("read_bytes", "realm.sqlite3-shm", "realm.sqlite3-shm"),
    ("read_bytes", "secret-name/secret-file", "<redacted>"),
    ("read_bytes", "nested/owner.lock", "<redacted>"),
])
def test_denial_preserves_exception_and_redacts(tmp_path, monkeypatch, operation, name, expected):
    data, realm = realm_at(tmp_path)
    target = realm / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"private data")
    denied = PermissionError(13, "private detail", str(target))
    original = getattr(Path, operation)
    def fail(path, *args, **kwargs):
        if path == (realm if operation == "rglob" else target):
            raise denied
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, operation, fail)
    with pytest.raises(PermissionError) as caught:
        jumps._preseed_workspace_state(data)
    assert caught.value is denied
    assert denied.preseed_denial == {"operation": operation, "relative_path": expected,
                                   "path_redacted": expected == "<redacted>"}
    assert journey.format_journey_error(denied) == "PermissionError: Permission denied"


def test_inner_regular_file_check_is_annotated(tmp_path, monkeypatch):
    data, realm = realm_at(tmp_path)
    target = realm / "realm.sqlite3"
    target.write_bytes(b"data")
    original = Path.is_file
    count = 0
    denied = PermissionError(13, "private", str(target))
    def is_file(path):
        nonlocal count
        if path == target:
            count += 1
            if count == 2:
                raise denied
        return original(path)
    monkeypatch.setattr(Path, "is_file", is_file)
    with pytest.raises(PermissionError) as caught:
        jumps._preseed_workspace_state(data)
    assert caught.value is denied
    assert denied.preseed_denial["operation"] == "is_file"
    assert denied.preseed_denial["relative_path"] == "realm.sqlite3"


def test_diagnostic_receipt_preserves_failure_without_private_path(tmp_path, monkeypatch):
    from tests.test_journey_diagnostic_checkpoint import _retained_preseed_state
    args, root, data, realm, evidence = _retained_preseed_state(tmp_path, monkeypatch)
    target = realm / "private-name"
    target.write_bytes(b"secret")
    denied = PermissionError(13, "private detail", str(target))
    original = Path.read_bytes
    def read(path):
        if path == target:
            raise denied
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(PermissionError) as caught:
        jumps.execute_diagnostic_release0_stage(args, root, data, evidence)
    assert caught.value is denied
    receipt = journey.read_json(evidence / "journey-result.json")
    assert receipt["status"] == "fail"
    assert receipt["error"] == "PermissionError: Permission denied"
    assert receipt["preseed_denial"] == {"operation": "read_bytes", "relative_path": "<redacted>",
                                         "path_redacted": True}
    assert "private" not in str(receipt)
    assert receipt["checks"] == {"preconditions": "not-run", "seed": "not-run", "snapshot": "not-run"}


def test_outer_failure_preserves_diagnostic_and_redacts_stderr(tmp_path, monkeypatch, capsys):
    root, data, evidence = tmp_path / "program", tmp_path / "data", tmp_path / "evidence"
    evidence.mkdir()
    args = argparse.Namespace(run_root=tmp_path, root=root, data_root=data, leg="release0-to-release1",
                              shell="sh", retain_for_next_leg=True)
    denied = PermissionError(13, "private detail", str(data / "private-name"))
    with pytest.raises(PermissionError):
        jumps._preseed_call("read_bytes", "private-name", lambda: (_ for _ in ()).throw(denied))
    monkeypatch.setattr(journey, "run_scope", lambda *a, **kw: (tmp_path, root, data, evidence))
    monkeypatch.setattr(journey, "assert_runner_identity", lambda *a: None)
    monkeypatch.setattr(journey, "assert_host_available", lambda *a: None)
    monkeypatch.setattr(journey, "isolated_environment", lambda *a: nullcontext())
    monkeypatch.setattr(journey, "sentinel_process", lambda *a: nullcontext((SimpleNamespace(pid=1), "birth")))
    monkeypatch.setattr(journey, "snapshot_failure_evidence", lambda *a: None)
    def fail(*unused):
        journey.write_json(evidence / "journey-result.json", {"status": "fail", "preseed_denial": denied.preseed_denial})
        raise denied
    monkeypatch.setattr(journey, "_execute_owned", fail)
    with pytest.raises(PermissionError) as caught:
        journey.execute(args)
    assert caught.value is denied
    receipt = journey.read_json(evidence / "journey-result.json")
    assert receipt["error"] == "PermissionError: Permission denied"
    assert receipt["preseed_denial"]["relative_path"] == "<redacted>"
    assert "private" not in str(receipt) and "private" not in capsys.readouterr().err


def test_real_r0_owner_lock_denial_without_reading_contents(tmp_path, monkeypatch):
    checkout = os.environ["JOURNEY_TEST_R0_RUNTIME"]
    assert subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip() == jumps.RELEASE0_REFS["runtime"]
    monkeypatch.syspath_prepend(checkout)
    from runtime_protocol.store import RealmStore
    data = tmp_path / "data"
    realm = data / "runtime" / "realms" / "r0"
    store = RealmStore.initialize(realm)
    try:
        journey.write_json(data / "runtime" / "discovery.json", {"realm_root": str(realm)})
        lock = realm / "owner.lock"
        assert lock.is_file() and (realm / "realm.sqlite3").is_file()
        original = Path.read_bytes
        denied = PermissionError(13, "private detail", str(lock))
        attempts = []
        def read(path):
            if path == lock:
                attempts.append(path)
                raise denied  # Simulate Windows denial before any lock content is read.
            return original(path)
        monkeypatch.setattr(Path, "read_bytes", read)
        with pytest.raises(PermissionError) as caught:
            jumps._preseed_workspace_state(data)
        assert caught.value is denied and attempts == [lock]
        assert denied.preseed_denial == {"operation": "read_bytes", "relative_path": "owner.lock", "path_redacted": False}
    finally:
        store.close()


def test_lifecycle_receipt_records_denial_before_seed(tmp_path, monkeypatch):
    from tests.test_journey_diagnostic_checkpoint import _retained_preseed_state
    args, root, data, realm, _ = _retained_preseed_state(tmp_path, monkeypatch)
    target = realm / "realm.sqlite3-shm"
    target.write_bytes(b"fixture")
    denied = PermissionError(13, "private detail", str(target))
    original_read = Path.read_bytes
    def read(path):
        if path == target:
            raise denied
        return original_read(path)
    monkeypatch.setattr(Path, "read_bytes", read)
    manifest = {"release": 1, "agent": {"sha": "a"}, "runtime": {"sha": "b"}}
    monkeypatch.setattr(jumps, "release0_admission", lambda _: (tmp_path, tmp_path / "manifest", {}))
    monkeypatch.setattr(journey, "selected_manifest", lambda *a, **kw: (tmp_path / "manifest", manifest))
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *a: manifest)
    monkeypatch.setattr(journey, "git_source", lambda _: {"sha": "app"})
    monkeypatch.setattr(journey, "sentinel_observation", lambda *a: {"alive": True})
    monkeypatch.setattr(jumps, "bootstrap_release0", lambda *a: None)
    monkeypatch.setattr(jumps, "start_direct_runtime", lambda *a: None)
    monkeypatch.setattr(journey, "wait_ready", lambda *a: None)
    monkeypatch.setattr(journey, "installation", lambda *a: (tmp_path / "version", data, {}))
    monkeypatch.setattr(jumps, "installed_release", lambda _: 0)
    original_json = journey.read_json
    monkeypatch.setattr(journey, "read_json", lambda path: {} if path == tmp_path / "manifest" else original_json(path))
    monkeypatch.setattr(journey, "selected_tuple", lambda *a: {})
    monkeypatch.setattr(journey, "assert_release0_runtime", lambda *a: None)
    monkeypatch.setattr(jumps, "write_diagnostic_release0_preconditions",
                        lambda *a, **kw: jumps._preseed_workspace_state(data))
    actions = []
    monkeypatch.setattr(journey, "verify", lambda *a, **kw: actions.append("verify"))
    evidence = tmp_path / "lifecycle-evidence"
    with pytest.raises(PermissionError) as caught:
        jumps.execute_release0_to_release1(args, root, data, evidence, SimpleNamespace(pid=1), "birth")
    assert caught.value is denied and actions == []
    receipt = journey.read_json(evidence / "journey-result.json")
    assert receipt["status"] == "fail"
    assert all(value == "not-run" for value in receipt["checks"].values())
    assert receipt["error"] == "PermissionError: Permission denied"
    assert receipt["preseed_denial"] == {"operation": "read_bytes", "relative_path": "realm.sqlite3-shm", "path_redacted": False}
    assert "private" not in str(receipt)


def test_original_fingerprint_for_ordinary_files(tmp_path):
    import hashlib
    data, realm = realm_at(tmp_path)
    (realm / "nested").mkdir()
    (realm / "nested" / "content").write_bytes(b"ordinary contents")
    (realm / "realm.sqlite3").write_bytes(b"ordinary database")
    def original_entries():
        found = []
        for path in sorted(realm.rglob("*"), key=lambda item: item.relative_to(realm).as_posix()):
            relative = path.relative_to(realm).as_posix()
            if path.is_symlink():
                raise RuntimeError("symlink")
            if path.is_dir():
                found.append({"path": relative, "kind": "directory"})
            elif path.is_file():
                assert not path.is_symlink() and path.is_file()
                found.append({"path": relative, "kind": "file", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            else:
                raise RuntimeError("non-regular")
        return found
    entries = original_entries()
    assert jumps._preseed_workspace_state(data) == {"realm_root": str(realm), "entry_count": len(entries),
                                                     "tree_sha256": jumps.semantic_digest({"entries": entries})}
