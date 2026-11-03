"""Diagnostic capture is private, redacted and cannot admit a refused service."""

import hashlib
import json
import os
import plistlib
import stat

import pytest

from astrid.installer import service
from astrid.installer.core import write_json
from tests.test_installer_release0_service_adoption import (
    _captured_r0_macos_definition,
    _posix_service,
)


@pytest.fixture
def capture_dir(tmp_path):
    directory = tmp_path / "private-capture"
    directory.mkdir(mode=0o700)
    return directory


def _read_capture(directory):
    files = list(directory.iterdir())
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    return json.loads(files[0].read_text())


def _checks(capture, name):
    return [value for key, value in capture["checks"] if key == name]


def test_macos_capture_is_absent_without_opt_in(tmp_path, monkeypatch, capture_dir):
    monkeypatch.delenv("ASTRID_MAC_ADMISSION_CAPTURE_DIR", raising=False)
    manager, version, data, *_ = _posix_service(tmp_path, monkeypatch, "darwin")
    manager.publish_launch_admission(version, data)
    assert list(capture_dir.iterdir()) == []


def test_macos_capture_records_redacted_observed_publication_and_recheck(tmp_path, monkeypatch, capture_dir):
    secret = "PRIVATE_ENV_AND_PLIST_SENTINEL_7e949d"
    monkeypatch.setenv("ASTRID_MAC_ADMISSION_CAPTURE_DIR", str(capture_dir))
    monkeypatch.setenv("PRIVATE_VALUE_FOR_TEST", secret)
    manager, version, data, *_ = _posix_service(tmp_path, monkeypatch, "darwin")
    doc = plistlib.loads(_captured_r0_macos_definition(version, data))
    doc["EnvironmentVariables"]["SECRET"] = secret
    doc["SECRET_PLIST_KEY"] = secret
    observed = plistlib.dumps(doc)
    manager.path.write_bytes(observed)

    manager.publish_launch_admission(version, data)
    capture = _read_capture(capture_dir)
    encoded = json.dumps(capture)
    assert secret not in encoded
    assert str(tmp_path) not in encoded
    assert "ProgramArguments" not in encoded
    assert capture["outcome"] == "returned" and not capture["truncated"]
    assert capture["source_sha256"] == hashlib.sha256(open(service.__file__, "rb").read()).hexdigest()
    assert capture["current_uid"] == os.getuid()
    assert capture["effective_uid"] == os.geteuid()
    assert capture["pid"] == os.getpid() and type(capture["started_ns"]) is int
    assert _checks(capture, "service_regular") == [True]
    assert _checks(capture, "service_owned") == [True]
    assert _checks(capture, "selection_owner_mismatch") == [False]
    assert _checks(capture, "selection_program_mismatch") == [False]
    assert _checks(capture, "selection_data_mismatch") == [False]
    assert _checks(capture, "plist_parse_completed") == [True]
    assert _checks(capture, "action_is_list") == [True]
    assert _checks(capture, "environment_is_dict") == [True]
    assert _checks(capture, "canonical_action_match") == [False]
    assert _checks(capture, "r0_program_root_match") == [True]
    assert _checks(capture, "known_action_match") == [True]
    assert _checks(capture, "recheck_bytes_mismatch") == [False]
    assert [item["phase"] for item in capture["stats"]] == ["publication", "recheck"]
    assert [item["sha256"] for item in capture["digests"]] == [hashlib.sha256(observed).hexdigest()] * 2


@pytest.mark.parametrize("condition", ["foreign_owner", "foreign_program", "foreign_data", "foreign_action", "wrong_label", "bad_environment", "bad_plist", "symlink"])
def test_macos_capture_preserves_refusals_and_original_bytes(tmp_path, monkeypatch, capture_dir, condition):
    manager, version, data, *_ = _posix_service(tmp_path, monkeypatch, "darwin")
    doc = plistlib.loads(_captured_r0_macos_definition(version, data))
    if condition == "foreign_action":
        doc["ProgramArguments"][0] += "-foreign"
    elif condition == "wrong_label":
        doc["Label"] = "foreign"
    elif condition == "bad_environment":
        doc["EnvironmentVariables"] = []
    original = b"not-a-plist" if condition == "bad_plist" else plistlib.dumps(doc)
    manager.path.write_bytes(original)
    if condition in {"foreign_owner", "foreign_program", "foreign_data"}:
        selection = {"owner": service.LABEL, "program_root": str(manager.root), "data_root": str(data)}
        selection[{"foreign_owner": "owner", "foreign_program": "program_root", "foreign_data": "data_root"}[condition]] = "foreign"
        write_json(manager.selection, selection)
    if condition == "symlink":
        target = tmp_path / "target"
        target.write_bytes(original)
        manager.path.unlink()
        manager.path.symlink_to(target)

    errors = []
    for enabled in (False, True):
        if enabled:
            monkeypatch.setenv("ASTRID_MAC_ADMISSION_CAPTURE_DIR", str(capture_dir))
        else:
            monkeypatch.delenv("ASTRID_MAC_ADMISSION_CAPTURE_DIR", raising=False)
        with pytest.raises(ValueError) as error:
            manager.publish_launch_admission(version, data)
        errors.append((type(error.value), str(error.value)))
        assert manager.path.read_bytes() == original
    assert errors[0] == errors[1]
    capture = _read_capture(capture_dir)
    assert capture["outcome"] == "raised"
    if condition == "foreign_action":
        assert _checks(capture, "r0_program_root_match") == [False]
        assert _checks(capture, "r0_version_root_match") == [False]
        assert _checks(capture, "known_action_match") == [False]
    if condition == "foreign_owner":
        assert _checks(capture, "record_owner_mismatch") == [True]
        assert capture["stats"] == []  # short-circuited; not observed
    if condition == "bad_plist":
        assert _checks(capture, "plist_parse_started") == [True]
        assert _checks(capture, "plist_parse_completed") == []


def test_macos_capture_observes_both_canonical_rechecks_and_race_refusal(tmp_path, monkeypatch, capture_dir):
    monkeypatch.setenv("ASTRID_MAC_ADMISSION_CAPTURE_DIR", str(capture_dir))
    manager, version, data, *_ = _posix_service(tmp_path, monkeypatch, "darwin")
    canonical = service.render("darwin", version, data, manager.log, env_file=manager.env_file)
    manager.path.write_text(canonical)
    original_fsync = service.platform.fsync_directory
    def race(directory):
        original_fsync(directory)
        manager.path.write_bytes(manager.path.read_bytes() + b" ")
    monkeypatch.setattr(service.platform, "fsync_directory", race)
    with pytest.raises(ValueError, match="unknown or changed action/owner"):
        manager.publish_launch_admission(version, data)
    capture = _read_capture(capture_dir)
    assert capture["outcome"] == "raised"
    assert _checks(capture, "canonical_action_match") == [True]
    assert _checks(capture, "operation_id_absent") == [True]
    assert [item["phase"] for item in capture["stats"]] == ["publication", "recheck", "recheck"]
    assert _checks(capture, "recheck_bytes_mismatch") == [False]
    assert _checks(capture, "recheck_st_size_mismatch") == [False, True]


@pytest.mark.parametrize("unsafe", ["world_readable", "symlink", "missing"])
def test_macos_capture_rejects_unsafe_destination_without_affecting_admission(tmp_path, monkeypatch, capture_dir, unsafe):
    destination = capture_dir
    if unsafe == "world_readable":
        destination.chmod(0o755)
    elif unsafe == "symlink":
        destination = tmp_path / "link"
        destination.symlink_to(capture_dir, target_is_directory=True)
    else:
        destination = tmp_path / "missing"
    monkeypatch.setenv("ASTRID_MAC_ADMISSION_CAPTURE_DIR", str(destination))
    manager, version, data, *_ = _posix_service(tmp_path, monkeypatch, "darwin")
    manager.publish_launch_admission(version, data)
    assert list(capture_dir.iterdir()) == []


def test_macos_capture_write_failure_preserves_admission_and_refusal(tmp_path, monkeypatch, capture_dir):
    manager, version, data, *_ = _posix_service(tmp_path, monkeypatch, "darwin")
    original = _captured_r0_macos_definition(version, data)
    manager.path.write_bytes(original)
    real_open = os.open
    def refuse_output(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            raise OSError("private diagnostic failure")
        return real_open(path, flags, mode)
    monkeypatch.setattr(os, "open", refuse_output)
    monkeypatch.setenv("ASTRID_MAC_ADMISSION_CAPTURE_DIR", str(capture_dir))
    manager.publish_launch_admission(version, data)
    assert manager.path.read_bytes() != original
    manager.path.write_bytes(b"malformed plist")
    with pytest.raises(ValueError, match="unknown or changed action/owner"):
        manager.publish_launch_admission(version, data)
    assert manager.path.read_bytes() == b"malformed plist"
    assert list(capture_dir.iterdir()) == []


def test_macos_capture_records_second_recheck_after_canonical_flush(tmp_path, monkeypatch, capture_dir):
    monkeypatch.setenv("ASTRID_MAC_ADMISSION_CAPTURE_DIR", str(capture_dir))
    manager, version, data, *_ = _posix_service(tmp_path, monkeypatch, "darwin")
    canonical = service.render("darwin", version, data, manager.log, env_file=manager.env_file)
    manager.path.write_text(canonical)
    manager.publish_launch_admission(version, data)
    capture = _read_capture(capture_dir)
    assert capture["outcome"] == "returned"
    assert [item["phase"] for item in capture["stats"]] == ["publication", "recheck", "recheck"]
    assert _checks(capture, "recheck_bytes_mismatch") == [False, False]


def test_macos_capture_records_byte_mismatch_at_recheck_without_publication(tmp_path, monkeypatch, capture_dir):
    monkeypatch.setenv("ASTRID_MAC_ADMISSION_CAPTURE_DIR", str(capture_dir))
    manager, version, data, *_ = _posix_service(tmp_path, monkeypatch, "darwin")
    original = _captured_r0_macos_definition(version, data)
    manager.path.write_bytes(original)
    real_read = type(manager.path).read_bytes
    reads = 0
    def changed_bytes(path):
        nonlocal reads
        value = real_read(path)
        if path == manager.path:
            reads += 1
            if reads == 2:
                return value + b" "
        return value
    monkeypatch.setattr(type(manager.path), "read_bytes", changed_bytes)
    with pytest.raises(ValueError, match="unknown or changed action/owner"):
        manager.publish_launch_admission(version, data)
    assert real_read(manager.path) == original
    capture = _read_capture(capture_dir)
    assert _checks(capture, "recheck_bytes_mismatch") == [True]
    assert capture["digests"][0]["sha256"] != capture["digests"][1]["sha256"]
