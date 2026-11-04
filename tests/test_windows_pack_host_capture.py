"""Portable bounded capture tests; native Windows service execution is separate."""

import json
import os
import re

import pytest

from scripts import ci_journey as journey
from scripts import ci_windows_journey as windows


@pytest.fixture
def capture(tmp_path, monkeypatch):
    data = tmp_path / "data"
    runtime = data / "runtime"
    runtime.mkdir(parents=True)
    evidence = tmp_path / "evidence" / "release0-to-release1"
    native = tmp_path / "native-logs"
    isolated = tmp_path / "isolated-localappdata"
    native.mkdir()
    (isolated / "Astrid Runtime" / "logs").mkdir(parents=True)
    monkeypatch.setattr(windows, "EVIDENCE", evidence / "release0-api-seed" / "release0-task")
    monkeypatch.setattr(windows, "data_root", lambda: data)
    monkeypatch.setattr(windows, "health", lambda: {"status": "ok", "runtime_instance_id": "instance-1",
        "services": [{"name": "pack-host", "state": "starting", "last_exit": None,
                      "restart_count": 0, "reason": None}]})
    monkeypatch.setenv(windows.NATIVE_RUNTIME_LOG_DIR_ENV, str(native))
    monkeypatch.setenv("LOCALAPPDATA", str(isolated))
    return data, runtime, evidence, native, isolated


def manifest(capture, observation):
    return json.loads((capture[2] / "release0-api-seed" / "release0-task" /
                       f"pack-host-readiness-{observation}.json").read_text())


def test_first_failed_poll_first_exit_and_latest_state_are_retained(capture, monkeypatch):
    data, runtime, evidence, native, isolated = capture
    child = native / "service-pack-host.log"
    child.write_text('boot failure: Authorization: Bearer secret-abc; {"access_token":"json-secret"}\n', encoding="utf-8")
    assert windows.host_ready() is False
    task_evidence = evidence / "release0-api-seed" / "release0-task"
    first = (task_evidence / "pack-host-readiness-first-failed-poll.json").read_bytes()
    first_log = (task_evidence / "pack-host-readiness-first-failed-poll-service-pack-host.log").read_bytes()
    assert b"secret-abc" not in first_log and b"json-secret" not in first_log
    assert len(first_log) <= windows.PACK_HOST_LOG_TAIL_BYTES

    health = windows.health()
    health["services"][0].update(state="restarting", last_exit=1, reason="child exited", restart_count=1)
    monkeypatch.setattr(windows, "health", lambda: health)
    child.write_bytes(b"x" * 70000 + b"\n" + windows._EXIT_SIGNATURES["no_retained_job_custody"])
    assert windows.host_ready() is False
    exit_evidence = manifest(capture, "first-service-exit")
    assert exit_evidence["health"]["pack_host"]["last_exit"] == 1
    assert exit_evidence["first_exit_discriminator"]["categories"] == ["no_retained_job_custody"]
    exit_log = task_evidence / "pack-host-readiness-first-service-exit-service-pack-host.log"
    assert exit_log.stat().st_size <= windows.PACK_HOST_LOG_TAIL_BYTES

    health["services"][0].update(state="failed", restart_count=8)
    assert windows.host_ready() is False
    assert (task_evidence / "pack-host-readiness-first-failed-poll.json").read_bytes() == first
    assert (task_evidence / "pack-host-readiness-first-failed-poll-service-pack-host.log").read_bytes() == first_log
    assert manifest(capture, "last-failed-poll")["health"]["pack_host"]["restart_count"] == 8


def test_capture_failure_does_not_change_readiness_or_original_exception(capture, monkeypatch):
    data, runtime, _, _, _ = capture
    monkeypatch.setattr(windows, "capture_pack_host_readiness", lambda *args: (_ for _ in ()).throw(OSError("private")))
    assert windows.host_ready() is False
    health = {"status": "ok", "runtime_instance_id": "i", "services": [{"name": "pack-host", "state": "running"}]}
    monkeypatch.setattr(windows, "health", lambda: health)
    (runtime / "generic-host.ready.json").write_text("[]")
    with pytest.raises(AttributeError):
        windows.host_ready()


def test_native_log_environment_and_allowlisted_evidence_staging(capture, monkeypatch):
    _, _, evidence, native, isolated = capture
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(native.parent))
    monkeypatch.delenv(windows.NATIVE_RUNTIME_LOG_DIR_ENV, raising=False)
    run_root = evidence.parent
    with journey.isolated_environment(run_root, run_root / "program", run_root / "data"):
        assert os.environ[windows.NATIVE_RUNTIME_LOG_DIR_ENV] == str(native.parent / "Astrid Runtime" / "logs")
        assert os.environ["LOCALAPPDATA"] == str(run_root / "localappdata")
    assert windows.NATIVE_RUNTIME_LOG_DIR_ENV not in os.environ

    task_dir = evidence / "release0-api-seed" / "release0-task"
    task_dir.mkdir(parents=True)
    (task_dir / "pack-host-readiness-last-failed-poll.json").write_text("{}")
    (task_dir / "pack-host-readiness-last-failed-poll-service-pack-host.log").write_bytes(b"log")
    journey.retain_named_evidence(evidence, "before-cleanup")
    before = evidence.parent.parent / "evidence-upload" / "before-cleanup"
    names = {path.name for path in before.iterdir()}
    assert "runtime-service-service-readiness-1-last-failed-poll.json.log" in names
    assert all(re.fullmatch(r"runtime-service-service-.+\.log", name) for name in names)
    (evidence / "release0-api-seed").rename(evidence / "removed-seed")
    journey.retain_named_evidence(evidence, "after-cleanup")
    after = evidence.parent.parent / "evidence-upload" / "after-cleanup"
    assert {path.name for path in after.iterdir()} == names


def test_bounded_marker_redacts_secret_fields(capture):
    data, runtime, _, native, isolated = capture
    (runtime / "generic-host.ready.json").write_text(json.dumps({
        "status": "failed", "runtime_instance_id": "instance-1", "access_token": "hidden",
        "details": {"password": "hidden-too", "state": "booting"}}))
    assert windows.host_ready() is False
    record = manifest(capture, "last-failed-poll")
    serialized = json.dumps(record)
    assert "hidden" not in serialized
    assert record["ready_marker"]["content"]["access_token"] == "<redacted>"
