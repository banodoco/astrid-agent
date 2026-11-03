"""Portable evidence tests; native Task Scheduler execution remains required."""

import hashlib
import json
import os
import re
import shutil

import pytest

from scripts import ci_journey as shared
from scripts import ci_windows_journey as windows
from scripts import journey_version_jumps as jumps


@pytest.fixture
def capture(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "runtime").mkdir(parents=True)
    evidence = tmp_path / "evidence" / "release0-to-release1"
    nested = evidence / "release0-api-seed" / "release0-task"
    native = tmp_path / "native-logs"
    local = tmp_path / "localappdata"
    native.mkdir()
    (local / "Astrid Runtime/logs").mkdir(parents=True)
    monkeypatch.setattr(windows, "EVIDENCE", nested)
    monkeypatch.setattr(windows, "data_root", lambda: data)
    monkeypatch.setenv(windows.NATIVE_RUNTIME_LOG_DIR_ENV, str(native))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(windows, "version", lambda: {})
    health = {"status": "ok", "runtime_instance_id": "current", "services": [
        {"name": "pack-host", "state": "starting", "last_exit": None,
         "restart_count": 0, "reason": None}]}
    monkeypatch.setattr(windows, "health", lambda: health)
    return data, evidence, nested, native, local, health


def manifest(nested, observation="last-failed-poll"):
    return json.loads((nested / f"pack-host-readiness-{observation}.json").read_text())


@pytest.mark.parametrize("case", ["unavailable", "unhealthy", "no-service", "starting",
                                    "missing", "malformed", "stale", "failed-marker", "no-instance", "ready"])
def test_capture_preserves_every_original_readiness_rejection(capture, case):
    data, _, nested, _, _, health = capture
    ready = {"status": "ready", "runtime_instance_id": "current"}
    health["services"][0]["state"] = "running"
    if case == "unavailable":
        health.clear()
    elif case == "unhealthy":
        health["status"] = "error"
    elif case == "no-service":
        health["services"] = []
    elif case == "starting":
        health["services"][0]["state"] = "starting"
    elif case == "stale":
        ready["runtime_instance_id"] = "old"
    elif case == "failed-marker":
        ready["status"] = "failed"
    elif case == "no-instance":
        health.pop("runtime_instance_id")
    if case != "missing":
        (data / "runtime/generic-host.ready.json").write_text(
            "{" if case == "malformed" else json.dumps(ready))
    assert windows.host_ready() is (case == "ready")
    if case != "ready":
        result = manifest(nested)
        assert result["predicate_result"] is False
        assert result["predicates"]["accepted"] is False
        assert result["observed_at_unix_ns"] > 0


def test_first_exit_and_latest_sample_survive_actual_nested_timeout(capture, monkeypatch):
    _, evidence, nested, native, _, health = capture
    child = native / "service-pack-host.log"
    child.write_text("first child error\n")
    assert windows.host_ready() is False
    first = (nested / "pack-host-readiness-first-failed-poll.json").read_bytes()
    health["services"][0].update(state="restarting", last_exit=1, reason="exited", restart_count=1)
    assert windows.host_ready() is False
    first_exit = (nested / "pack-host-readiness-first-service-exit-service-pack-host.log").read_bytes()
    child.write_text("latest child error\n")
    health["services"][0].update(state="failed", restart_count=9)
    monkeypatch.setattr(jumps.sys, "platform", "win32")
    original_wait = windows.wait_for
    monkeypatch.setattr(windows, "wait_for", lambda label, probe: original_wait(label, probe, seconds=0))
    monkeypatch.setattr(windows, "task", lambda *a, **k: pytest.fail("task must remain unrun"))
    with pytest.raises(RuntimeError, match="Timed out after 0s: pack-host is running with its current ready file"):
        jumps.task_after_update({}, nested, "project")
    assert (nested / "pack-host-readiness-first-failed-poll.json").read_bytes() == first
    assert (nested / "pack-host-readiness-first-service-exit-service-pack-host.log").read_bytes() == first_exit
    assert first_exit == b"first child error\n"
    assert manifest(nested)["health"]["pack_host"]["restart_count"] == 9
    assert len(list(nested.glob("pack-host-readiness-*.json"))) == 3
    shared.retain_named_evidence(evidence, "before-cleanup")
    before = evidence.parent.parent / "evidence-upload/before-cleanup"
    names = {p.name: p.read_bytes() for p in before.iterdir()}
    assert "runtime-service-service-readiness-1-last-failed-poll.json.log" in names
    assert all(re.fullmatch(r"runtime-service-service-.+\.log", name) for name in names)
    shutil.rmtree(evidence / "release0-api-seed")
    shared.retain_named_evidence(evidence, "after-cleanup")
    after = before.parent / "after-cleanup"
    assert {p.name: p.read_bytes() for p in after.iterdir()} == names


def test_native_child_wrapper_roots_bounds_and_secret_redaction(capture):
    data, _, nested, native, local, _ = capture
    (data / "logs").mkdir()
    (data / "logs/service-pack-host.log").write_text("wrong root decoy")
    raw = b"x" * 70000 + b"\nTraceback: child failure\nAuthorization: Bearer abc123\n"
    (native / "service-pack-host.log").write_bytes(raw)
    (native / "runtime.log").write_text("native supervisor")
    (local / "Astrid Runtime/logs/runtime-service.log").write_text("isolated wrapper")
    (data / "runtime/generic-host.json").write_text(json.dumps({"pid": 27, "token": "hidden"}))
    assert windows.host_ready() is False
    result = manifest(nested)
    logs = {item["source"]: item for item in result["logs"]}
    child = logs["service-pack-host.log"]
    safe = (nested / child["evidence"]).read_bytes()
    assert child["search_root"] == "native-runtime-logs"
    assert child["truncated"] is True
    assert child["raw_tail_sha256"] == hashlib.sha256(raw[-65536:]).hexdigest()
    assert child["captured_sha256"] == hashlib.sha256(safe).hexdigest()
    assert len(safe) <= 65536
    assert b"child failure" in safe and b"abc123" not in safe and b"wrong root" not in safe
    assert logs["runtime-service.log"]["search_root"] == "isolated-service-logs"
    assert (nested / logs["runtime-service.log"]["evidence"]).read_text() == "isolated wrapper"
    assert "hidden" not in json.dumps(result)


@pytest.mark.parametrize("mode", ["symlink", "oversized", "permission"])
def test_unsafe_or_unavailable_capture_is_explicit_and_stays_rejected(capture, monkeypatch, mode):
    data, _, nested, _, _, _ = capture
    path = data / "runtime/generic-host.ready.json"
    if mode == "symlink":
        target = data / "secret"
        target.write_text("do not retain")
        path.symlink_to(target)
    elif mode == "oversized":
        path.write_bytes(b"x" * 65537)
    else:
        original = windows.Path.lstat
        def lstat(self, *a, **k):
            if self == path:
                raise PermissionError("private path and data")
            return original(self, *a, **k)
        monkeypatch.setattr(windows.Path, "lstat", lstat)
    assert windows.host_ready() is False
    result = manifest(nested)
    assert result["ready_read"]["status"] == {"symlink": "unsafe_type", "oversized": "oversized", "permission": "error"}[mode]
    assert "do not retain" not in json.dumps(result)
    assert "private path and data" not in json.dumps(result)


def test_capture_error_cannot_replace_original_result_or_exception(capture, monkeypatch):
    monkeypatch.setattr(windows, "capture_pack_host_readiness", lambda *a: (_ for _ in ()).throw(OSError("private")))
    assert windows.host_ready() is False
    data, _, _, _, _, health = capture
    health["services"][0]["state"] = "running"
    (data / "runtime/generic-host.ready.json").write_text("[]")
    with pytest.raises(AttributeError):
        windows.host_ready()


def test_isolation_preserves_and_restores_native_runtime_log_root(tmp_path, monkeypatch):
    native = tmp_path / "login-local"
    monkeypatch.setenv("LOCALAPPDATA", str(native))
    monkeypatch.delenv(windows.NATIVE_RUNTIME_LOG_DIR_ENV, raising=False)
    monkeypatch.setattr(shared.sys, "platform", "win32")
    with shared.isolated_environment(tmp_path / "run", tmp_path / "run/program", tmp_path / "run/data"):
        assert os.environ[windows.NATIVE_RUNTIME_LOG_DIR_ENV] == str(native / "Astrid Runtime/logs")
        assert os.environ["LOCALAPPDATA"] == str(tmp_path / "run/localappdata")
    assert os.environ["LOCALAPPDATA"] == str(native)
    assert windows.NATIVE_RUNTIME_LOG_DIR_ENV not in os.environ
