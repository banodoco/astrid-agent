"""Failure-closed transient staging tests; macOS ACL commands are simulated."""
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from scripts import private_macos_admission as diag


@pytest.fixture
def record():
    return {"format": "astrid-macos-admission-diagnostic/v1", "checks": [["selection_read_started", True]],
            "stats": [], "digests": [], "truncated": False, "current_uid": os.getuid(),
            "effective_uid": os.geteuid(), "source_sha256": diag.EXPECTED, "outcome": "returned",
            "exception_category": None, "pid": 123, "started_ns": 456}


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    capture, staging = tmp_path / "capture", tmp_path / "staging"
    capture.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    monkeypatch.setattr(diag, "_acl_check", lambda *args, **kwargs: None)
    return capture, staging


def write(capture, value):
    path = capture / ("admission-" + "a" * 32 + ".json")
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def test_validated_records_staged_without_console(dirs, record, capsys):
    capture, staging = dirs
    path = write(capture, record)
    assert diag.stage(capture, staging) == 1
    assert (staging / path.name).read_bytes() == path.read_bytes()
    assert not capsys.readouterr().out


@pytest.mark.parametrize("target,mode", [("capture", 0o750), ("staging", 0o701), ("file", 0o644)])
def test_unsafe_modes_rejected(dirs, record, target, mode):
    capture, staging = dirs
    path = write(capture, record)
    {"capture": capture, "staging": staging, "file": path}[target].chmod(mode)
    with pytest.raises(ValueError):
        diag.stage(capture, staging)
    assert not list(staging.iterdir())


@pytest.mark.parametrize("target", ["directory", "file"])
def test_symlinks_rejected(dirs, record, target, tmp_path):
    capture, staging = dirs
    path = write(capture, record)
    if target == "directory":
        link = tmp_path / "link"
        link.symlink_to(capture, target_is_directory=True)
        capture = link
    else:
        path.unlink()
        path.symlink_to(tmp_path / "missing")
    with pytest.raises((ValueError, OSError)):
        diag.stage(capture, staging)


@pytest.mark.parametrize("mutation", ["extra", "digest", "truncated", "missing_admission", "bad_check", "bad_stats", "bad_digests"])
def test_invalid_record_rejected(dirs, record, mutation):
    capture, staging = dirs
    if mutation == "extra": record["secret"] = "sentinel"
    if mutation == "digest": record["source_sha256"] = "0" * 64
    if mutation == "truncated": record["truncated"] = True
    if mutation == "missing_admission": record["checks"] = []
    if mutation == "bad_check": record["checks"].append(["untrusted", True])
    if mutation == "bad_stats": record["stats"] = "sentinel"
    if mutation == "bad_digests": record["digests"] = [{"phase": "recheck", "sha256": "sentinel"}]
    write(capture, record)
    with pytest.raises(ValueError): diag.stage(capture, staging)
    assert not list(staging.iterdir())


def test_missing_malformed_duplicate_and_truncated_json(dirs, record):
    capture, staging = dirs
    with pytest.raises(ValueError): diag.stage(capture, staging)
    path = write(capture, record)
    for raw in ('{"secret":', '{"format":1,"format":2}', json.dumps(record)[:-1]):
        path.write_text(raw)
        with pytest.raises(ValueError): diag.stage(capture, staging)
    assert not list(staging.iterdir())


def test_acl_output_and_command_errors_fail_closed(tmp_path, monkeypatch):
    calls = []
    def fake(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=b"one\n extended entry\n")
    monkeypatch.setattr(diag.subprocess, "run", fake)
    with pytest.raises(ValueError): diag._acl_check(tmp_path, directory=True)
    assert calls[0][0:2] == ["ls", "-lde"]
    def failure(*args, **kwargs): raise subprocess.CalledProcessError(1, "chmod")
    monkeypatch.setattr(diag.subprocess, "run", failure)
    with pytest.raises(subprocess.CalledProcessError): diag._acl_clear(tmp_path)


def test_acl_failure_does_not_stage(dirs, record, monkeypatch):
    capture, staging = dirs
    write(capture, record)
    def fail(path, **kwargs): raise ValueError("ACL")
    monkeypatch.setattr(diag, "_acl_check", fail)
    with pytest.raises(ValueError): diag.stage(capture, staging)
    assert not list(staging.iterdir())


def test_lifecycle_failure_exit_preserved_with_private_env_and_cleanup(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(diag.sys, "platform", "darwin")
    monkeypatch.setattr(diag, "_acl_clear", lambda path: None)
    monkeypatch.setattr(diag, "stage", lambda *args: (_ for _ in ()).throw(ValueError("secret")))
    def lifecycle(command, *, env, check):
        capture = Path(env["ASTRID_MAC_ADMISSION_CAPTURE_DIR"])
        assert capture.is_dir() and capture.stat().st_mode & 0o777 == 0o700
        assert command == ["driver"] and not check
        return SimpleNamespace(returncode=37)
    monkeypatch.setattr(diag.subprocess, "run", lifecycle)
    assert diag.run(["driver"], tmp_path) == 37
    assert not list(tmp_path.iterdir())
    assert "secret" not in capsys.readouterr().err


def test_setup_acl_failure_never_starts_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(diag.sys, "platform", "darwin")
    monkeypatch.setattr(diag, "_acl_clear", lambda path: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(diag.subprocess, "run", lambda *a, **k: pytest.fail("lifecycle started"))
    with pytest.raises(OSError): diag.run(["driver"], tmp_path)
    assert not list(tmp_path.iterdir())


def test_all_records_validate_before_any_copy(dirs, record):
    capture, staging = dirs
    write(capture, record)
    invalid = dict(record, source_sha256="0" * 64)
    other = capture / ("admission-" + "b" * 32 + ".json")
    other.write_text(json.dumps(invalid))
    other.chmod(0o600)
    with pytest.raises(ValueError): diag.stage(capture, staging)
    assert not list(staging.iterdir())


def test_hardlinks_rejected(dirs, record, tmp_path):
    capture, staging = dirs
    path = write(capture, record)
    os.link(path, tmp_path / "second-link")
    with pytest.raises(ValueError): diag.stage(capture, staging)


def test_acl_removal_and_verification_commands(tmp_path, monkeypatch):
    calls = []
    def fake(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=b"drwx------ directory\n")
    monkeypatch.setattr(diag.subprocess, "run", fake)
    diag._acl_clear(tmp_path)
    assert [c[:2] for c in calls] == [["chmod", "-N"], ["ls", "-lde"]]


def test_success_exit_preserved_if_capture_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(diag.sys, "platform", "darwin")
    monkeypatch.setattr(diag, "_acl_clear", lambda path: None)
    monkeypatch.setattr(diag, "stage", lambda *args: (_ for _ in ()).throw(ValueError()))
    monkeypatch.setattr(diag.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    assert diag.run(["driver"], tmp_path) == 0
