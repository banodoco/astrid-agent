"""Portable error/retention tests; these are not native Windows evidence."""
import errno
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from scripts import ci_journey as journey
from scripts import ci_journey_windows_capture as observer


@pytest.fixture
def authority(tmp_path):
    root = tmp_path / "data" / "runtime"
    root.mkdir(parents=True)
    registration = {"pid": 41, "process_birth_id": "windows-creation:original-host",
                    "runtime_service_launch_id": "a" * 32, "support_root": str(root),
                    "runtime_instance_id": "original-owner", "token": "DO-NOT-RETAIN",
                    "credential_file": str(root / "credential.json"),
                    "process_image": "selected-r0-image", "source_checkout": "selected-r0-purelib"}
    receipt = {"version": 1, "service_name": "pack-host", "support_root": str(root), "receipts": [
        {"pid": 41, "process_birth_id": "windows-creation:original-host",
         "runtime_service_launch_id": "a" * 32, "job_drained": True}]}
    for name, value in (("generic-host.json", registration), ("pack-host-job-cleanup.json", receipt),
                        ("discovery.json", {"pid": 31, "runtime_instance_id": "original-owner"})):
        (root / name).write_text(json.dumps(value))
    (root / ".pack-host-job-cleanup.lock").write_bytes(b"existing-lock-bytes")
    return root


@pytest.fixture
def native():
    calls = []
    platform = SimpleNamespace(__name__="runtime_protocol.platform", __file__=observer.__file__,
                               lock_file=lambda fd, **kw: calls.append(("lock", fd, kw)),
                               unlock_file=lambda fd: calls.append(("unlock", fd)))
    security = SimpleNamespace(__name__="runtime_protocol.windows_security", __file__=observer.__file__,
                               validate=lambda value: calls.append(("validate", value)))
    return platform, security, calls


@pytest.mark.parametrize("phase", observer.PHASES)
def test_capture_preserves_all_authority_bytes_modes_and_names(authority, native, monkeypatch, phase):
    before = {p.name: (p.read_bytes(), p.stat().st_mode) for p in authority.iterdir()}
    platform, security, calls = native
    opened = []
    real_open = observer.os.open

    def check_open(path, flags, *args, **kwargs):
        assert not flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        opened.append((Path(path).name, flags))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(observer.os, "open", check_open)
    result = observer.capture(authority, phase, platform, security)
    assert {p.name: (p.read_bytes(), p.stat().st_mode) for p in authority.iterdir()} == before
    assert len(opened) == 4
    assert {name for name, _ in opened} == set(observer.AUTHORITY_NAMES)
    assert next(c for c in calls if c[0] == "lock")[2] == {"exclusive": True, "blocking": False}
    assert sum(c[0] == "unlock" for c in calls) == 1
    assert result["lock"]["release"] == "ok"
    assert result["registration"]["projection"]["fields"]["runtime_service_launch_id"] == "a" * 32
    assert result["registration"]["projection"]["fields"]["source_checkout"] == "selected-r0-purelib"
    assert "DO-NOT-RETAIN" not in json.dumps(result)
    assert result["receipt_read_under_lock"]["parse"] == "ok"
    assert result["receipt_read_under_lock"]["opened_native_acl"]["status"].startswith("accepted")
    assert result["completeness"] == {"snapshot_finished": True, "producer_trace": False,
                                      "actual_consumer_reads": False}
    assert result["producer_events"] == result["consumer_matcher_read_1"] == result["consumer_matcher_read_2"] == "not-observed"
    assert result["admission"] == "not-evaluated"
    assert result["end"]["monotonic_ns"] >= result["start"]["monotonic_ns"]


def test_missing_lock_is_never_created_or_receipt_read(authority, native):
    (authority / ".pack-host-job-cleanup.lock").unlink()
    platform, security, calls = native
    result = observer.capture(authority, observer.PHASES[0], platform, security)
    assert result["files_before"][".pack-host-job-cleanup.lock"] == {"status": "absent"}
    assert result["receipt_read_under_lock"] == {"status": "not-attempted"}
    assert result["lock"]["open"] == "not-attempted"
    assert not any(c[0] in {"lock", "unlock"} for c in calls)
    assert not (authority / ".pack-host-job-cleanup.lock").exists()


def test_lock_contention_records_its_leaf_without_waiting_or_unlocking(authority, native):
    platform, security, calls = native

    def busy(fd, **kwargs):
        assert kwargs == {"exclusive": True, "blocking": False}
        raise BlockingIOError(errno.EAGAIN, "busy")

    platform.lock_file = busy
    result = observer.capture(authority, observer.PHASES[1], platform, security)
    assert result["lock"]["open"] == "ok"
    assert result["lock"]["acquire"]["type"] == "BlockingIOError"
    assert result["receipt_read_under_lock"]["status"] == "not-attempted"
    assert not any(c[0] == "unlock" for c in calls)


def test_lock_open_error_remains_separate(authority, native, monkeypatch):
    platform, security, _ = native
    original = observer.os.open

    def fail(path, *args, **kwargs):
        if Path(path).name.endswith(".lock"):
            raise PermissionError(errno.EACCES, "denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(observer.os, "open", fail)
    result = observer.capture(authority, observer.PHASES[0], platform, security)
    assert result["lock"]["open"]["errno"] == errno.EACCES
    assert result["lock"]["acquire"] == "not-attempted"


@pytest.mark.parametrize("raw,expected", [(None, "absent"), (b"not json", "parse"),
                                        (b"\xff", "parse"), (b"x" * (observer.BYTE_LIMIT + 1), "limit")])
def test_receipt_absence_parse_and_size_are_distinguished(authority, native, raw, expected):
    path = authority / "pack-host-job-cleanup.json"
    path.unlink() if raw is None else path.write_bytes(raw)
    platform, security, _ = native
    result = observer.capture(authority, observer.PHASES[1], platform, security)
    read = result["receipt_read_under_lock"]
    if expected == "absent":
        assert read["open"]["type"] == "FileNotFoundError"
        assert read["read"] == "not-attempted"
    elif expected == "parse":
        assert read["read"] == "ok"
        assert read["parse"]["status"] == "error"
    else:
        assert read["read"] == "byte-limit-exceeded"
        assert read["complete_bytes_read"] is False
        assert read["parse"] == "not-attempted"
    assert result["lock"]["release"] == "ok"


def test_acl_rejection_is_not_confused_with_open_or_read_failure(authority, native):
    platform, security, _ = native

    def reject(_):
        exc = PermissionError(errno.EACCES, "unsafe native DACL")
        exc.winerror = 5
        raise exc

    security.validate = reject
    result = observer.capture(authority, observer.PHASES[0], platform, security)
    assert result["native_acl"]["pack-host-job-cleanup.json"]["winerror"] == 5
    read = result["receipt_read_under_lock"]
    assert read["opened_native_acl"]["errno"] == errno.EACCES
    assert read["open"] == read["read"] == read["parse"] == "ok"
    assert result["admission"] == "not-evaluated"


def test_read_error_releases_existing_lock(authority, native, monkeypatch):
    platform, security, _ = native
    real_read = observer.os.read

    def fail(fd, count):
        if os.fstat(fd).st_ino == (authority / "pack-host-job-cleanup.json").stat().st_ino:
            raise OSError(errno.EIO, "read failed")
        return real_read(fd, count)

    monkeypatch.setattr(observer.os, "read", fail)
    result = observer.capture(authority, observer.PHASES[1], platform, security)
    assert result["receipt_read_under_lock"]["read"]["errno"] == errno.EIO
    assert result["lock"]["release"] == "ok"


def test_projection_bounds_entries_strings_recursion_and_unknown_values():
    value = {"token": "SECRET", "support_root": "r" * (observer.STRING_LIMIT + 1),
             "receipts": [{"pid": i} for i in range(65)]}
    result = observer.projection(value)
    assert result["receipt_count"] == 65 and result["entries_omitted"] == 1
    assert len(result["receipts"]) == 64 and result["complete"] is False
    assert result["unsafe_or_oversize_field_count"] == 1
    assert "SECRET" not in json.dumps(result)
    result = observer.projection({"receipts": [{"receipts": [{"receipts": []}]}]})
    assert result["nested_entries_omitted"] == 1 and result["complete"] is False
    assert observer.projection([]) == {"json_type": "list", "complete": False}


@pytest.mark.parametrize("name", ["generic-host.json", "pack-host-job-cleanup.json", ".pack-host-job-cleanup.lock"])
def test_known_reparse_objects_are_refused(authority, native, name):
    platform, security, _ = native
    target = authority.parent / "foreign"
    target.write_bytes(b"DO NOT READ")
    path = authority / name
    path.unlink()
    path.symlink_to(target)
    result = observer.capture(authority, observer.PHASES[0], platform, security)
    assert result["native_acl"][name]["status"] == "reparse-refused"
    assert "DO NOT READ" not in json.dumps(result)
    assert target.read_bytes() == b"DO NOT READ"


def test_reparse_support_ancestor_is_refused(authority, native):
    platform, security, _ = native
    linked = authority.parent.parent / "linked"
    linked.symlink_to(authority.parent, target_is_directory=True)
    result = observer.capture(linked / "runtime", observer.PHASES[0], platform, security)
    assert result["support_path"]["status"] == "refused"
    assert result["completeness"]["snapshot_finished"] is False


def run_capture(tmp_path, monkeypatch, child):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setenv("PYTHONPATH", "must-not-inherit")
    monkeypatch.setenv("PYTHONHOME", "must-not-inherit")
    monkeypatch.setattr(journey.subprocess, "run", child)
    return journey.capture_windows_receipt({"python": "selected-r0-python", "env": {"R0_ENV": "selected"}},
                                            tmp_path / "data", tmp_path / "evidence", observer.PHASES[0])


def test_runner_uses_selected_isolated_python_exact_exit_and_unique_retained_files(tmp_path, monkeypatch):
    commands = []

    def child(command, *, env, stdout, stderr, timeout, check):
        assert command[:3] == ["selected-r0-python", "-B", "-I"]
        assert command[3].endswith("ci_journey_windows_capture.py")
        assert "PYTHONHOME" not in env and "PYTHONPATH" not in env
        assert env["R0_ENV"] == "selected" and timeout == 15 and check is False
        commands.append(command)
        stdout.write('{"completeness":{"snapshot_finished":true},"admission":"not-evaluated"}')
        return SimpleNamespace(returncode=0)

    first = run_capture(tmp_path, monkeypatch, child)
    second = run_capture(tmp_path, monkeypatch, child)
    assert first["inner_exit_code"] == 0 and first["capture_complete"] is True
    assert first["stdout"] != second["stdout"] and len(commands) == 2
    assert len(list((tmp_path / "evidence").iterdir())) == 6
    journey.retain_named_evidence(tmp_path / "evidence", "before-cleanup")
    staged = tmp_path.parent / "evidence-upload" / "before-cleanup"
    assert (staged / first["stdout"]).read_bytes() == (tmp_path / "evidence" / first["stdout"]).read_bytes()


@pytest.mark.parametrize("failure", ["timeout", "exit", "invalid-json"])
def test_runner_never_converts_child_failure_to_capture_success(tmp_path, monkeypatch, failure):
    def child(command, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 15)
        kwargs["stdout"].write("not-json" if failure == "invalid-json" else "")
        return SimpleNamespace(returncode=0 if failure == "invalid-json" else 17)

    result = run_capture(tmp_path, monkeypatch, child)
    assert result["capture_complete"] is False
    assert result["inner_exit_code"] == {"timeout": None, "exit": 17, "invalid-json": 0}[failure]
    exits = list((tmp_path / "evidence").glob("*-exit.log"))
    assert len(exits) == 1 and json.loads(exits[0].read_text()) == result


def test_evidence_under_product_data_is_rejected_before_any_write(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    with pytest.raises(ValueError, match="outside product data"):
        journey.capture_windows_receipt({}, tmp_path / "data", tmp_path / "data" / "evidence", observer.PHASES[0])
    assert not (tmp_path / "data").exists()


def test_nonwindows_boundary_is_an_inert_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "linux")
    before = set(tmp_path.iterdir())
    assert journey.capture_windows_receipt({}, tmp_path, tmp_path, "unused") == {"status": "not-applicable"}
    assert set(tmp_path.iterdir()) == before
