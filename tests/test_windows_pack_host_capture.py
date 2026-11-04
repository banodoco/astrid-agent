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
    first_exit = (nested / "pack-host-readiness-first-service-exit.json").read_bytes()
    child.write_text("latest child error\n")
    health["services"][0].update(state="failed", restart_count=9)
    monkeypatch.setattr(jumps.sys, "platform", "win32")
    original_wait = windows.wait_for
    monkeypatch.setattr(windows, "wait_for", lambda label, probe: original_wait(label, probe, seconds=0))
    monkeypatch.setattr(windows, "task", lambda *a, **k: pytest.fail("task must remain unrun"))
    with pytest.raises(RuntimeError, match="Timed out after 0s: pack-host is running with its current ready file"):
        jumps.task_after_update({}, nested, "project")
    assert (nested / "pack-host-readiness-first-failed-poll.json").read_bytes() == first
    assert (nested / "pack-host-readiness-first-service-exit.json").read_bytes() == first_exit
    assert manifest(nested)["health"]["pack_host"]["restart_count"] == 9
    assert len(list(nested.glob("pack-host-readiness-*.json"))) == 3
    (evidence / "journey-result.json").write_text(json.dumps({"status": "fail"}))
    monkeypatch.setattr(shared, "capture_runtime_service_logs", lambda *a: None)
    monkeypatch.setattr(shared, "capture_manager_state", lambda *a: None)
    shared.snapshot_failure_evidence(evidence, evidence.parent.parent, evidence.parent.parent / "data", "before-cleanup")
    before = evidence.parent.parent / "evidence-upload/before-cleanup"
    names = {p.name: p.read_bytes() for p in before.iterdir()}
    assert "runtime-service-service-readiness-1-last-failed-poll.json.log" in names
    assert all(name == "journey-result.json" or re.fullmatch(r"runtime-service-service-.+\.log", name) for name in names)
    receipt = json.loads(names["journey-result.json"])
    bindings = receipt["failure_diagnostics"]["before-cleanup"]["pack_host_readiness"]
    assert len(bindings) == 3
    for item in bindings:
        path = evidence.parent.parent / item["path"]
        assert item["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    shutil.rmtree(evidence / "release0-api-seed")
    shared.retain_named_evidence(evidence, "after-cleanup")
    after = before.parent / "after-cleanup"
    assert {p.name: p.read_bytes() for p in after.iterdir()} == names


def test_native_child_wrapper_roots_and_metadata_without_content(capture):
    data, _, nested, native, local, _ = capture
    (data / "logs").mkdir()
    (data / "logs/service-pack-host.log").write_text("wrong root decoy")
    raw = b"x" * 70000 + b"\nAuthorization: Bearer abc123\n"
    (native / "service-pack-host.log").write_bytes(raw)
    (local / "Astrid Runtime/logs/runtime-service.log").write_text("isolated wrapper")
    (data / "runtime/generic-host.json").write_text(json.dumps({"pid": 27, "token": "hidden"}))
    assert windows.host_ready() is False
    result = manifest(nested)
    logs = {item["source"]: item for item in result["logs"]}
    assert logs["service-pack-host.log"]["search_root"] == "native-runtime-logs"
    assert logs["service-pack-host.log"]["truncated"] is True
    assert logs["service-pack-host.log"]["bytes"] == len(raw)
    assert logs["service-pack-host.log"]["content_retained"] is False
    assert logs["runtime-service.log"]["search_root"] == "isolated-service-logs"
    assert not list(nested.glob("pack-host-readiness-*.log"))
    assert all(secret not in json.dumps(result) for secret in ("abc123", "hidden", "isolated wrapper"))


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


@pytest.mark.parametrize("case,field", [
    ("unhealthy", "health_status_ok"),
    ("starting", "pack_host_running"),
    ("missing", "ready_file_is_file"),
    ("malformed", "ready_json_object"),
    ("wrong-status", "ready_status_ready"),
    ("mismatch", "ready_runtime_instance_matches"),
])
def test_readiness_manifest_identifies_rejected_predicate(capture, case, field):
    data, _, nested, _, _, health = capture
    health["services"][0]["state"] = "running"
    marker = {"status": "ready", "runtime_instance_id": "current"}
    if case == "unhealthy":
        health["status"] = "error"
    elif case == "starting":
        health["services"][0]["state"] = "starting"
    elif case == "wrong-status":
        marker["status"] = "starting"
    elif case == "mismatch":
        marker["runtime_instance_id"] = "old"
    if case != "missing":
        (data / "runtime/generic-host.ready.json").write_text(
            "{" if case == "malformed" else json.dumps(marker))
    assert windows.host_ready() is False
    observation = manifest(nested)
    assert observation["predicates"][field] is False
    assert observation["predicate_result"] is False
    if case == "mismatch":
        assert observation["health"]["runtime_instance_id_sha256"] == hashlib.sha256(b"current").hexdigest()
        assert observation["markers"][0]["content"]["runtime_instance_id_sha256"] == hashlib.sha256(b"old").hexdigest()


def test_arbitrary_secret_text_and_huge_keys_never_enter_promoted_manifests(capture, monkeypatch):
    data, evidence, nested, native, _, health = capture
    secrets = ["synthetic_one", "synthetic_two", "synthetic_private", "synthetic_code", "synthetic_argument"]
    payload = ("Cookie: session=synthetic_one; csrftoken=synthetic_two\n"
               "2026 logger -----BEGIN PRIVATE KEY-----\nsynthetic_private\n"
               "2026 logger -----END PRIVATE KEY-----\nhttps://example.invalid/?code=synthetic_code")
    (native / "service-pack-host.log").write_text(payload)
    (data / "runtime/generic-host.ready.json").write_text("{malformed " + payload)
    (data / "runtime/generic-host.json").write_text(json.dumps({"pid": 5, "unknown": payload, "status": payload}))
    (data / "runtime/discovery.json").write_text(json.dumps({"runtime_instance_id": "current", "token": payload}))
    health["services"][0]["reason"] = {"k" * 1_100_000: payload}
    declaration = data / "declaration.json"
    declaration.write_text(json.dumps({"services": [{"name": "pack-host", "restart": payload,
        "argv": ["synthetic_argument", payload], "ready": {"file": payload}}]}))
    profile = data / "profile.json"
    profile.write_text(json.dumps({"service_declaration": str(declaration)}))
    monkeypatch.setattr(windows, "version", lambda: {"profile": str(profile)})
    assert windows.host_ready() is False
    manifests = list(nested.glob("pack-host-readiness-*.json"))
    assert len(manifests) == 3
    for path in manifests:
        content = path.read_bytes()
        assert len(content) <= windows.PACK_HOST_DIAGNOSTIC_MANIFEST_BYTES
        assert all(secret.encode() not in content for secret in secrets)
        result = json.loads(content)
        assert result["health"]["pack_host"]["reason"] == "other"
        assert result["markers"][0]["status"] == "malformed"
        assert "content" not in result["markers"][0]
        assert result["service_action"]["action"]["restart"] == "other"
    (evidence / "journey-result.json").write_text('{}')
    monkeypatch.setattr(shared, "capture_runtime_service_logs", lambda *a: None)
    monkeypatch.setattr(shared, "capture_manager_state", lambda *a: None)
    run_root = evidence.parent.parent
    shared.snapshot_failure_evidence(evidence, run_root, data, "before-cleanup")
    staged = list((run_root / "evidence-upload/before-cleanup").iterdir())
    assert len(staged) == 4
    assert all(not any(secret.encode() in path.read_bytes() for secret in secrets) for path in staged)


def test_manifest_ceiling_and_oversized_staging_failure_are_explicit(capture, monkeypatch):
    data, evidence, nested, _, _, _ = capture
    monkeypatch.setattr(windows, "PACK_HOST_DIAGNOSTIC_MANIFEST_BYTES", 1024)
    assert windows.host_ready() is False
    result = manifest(nested)
    assert result["status"] == "truncated" and result["reason"] == "manifest_size_limit"
    assert len((nested / "pack-host-readiness-last-failed-poll.json").read_bytes()) <= 1024
    (nested / "pack-host-readiness-last-failed-poll.json").write_bytes(b"x" * 1025)
    (evidence / "journey-result.json").write_text('{}')
    monkeypatch.setattr(shared, "capture_runtime_service_logs", lambda *a: None)
    monkeypatch.setattr(shared, "capture_manager_state", lambda *a: None)
    run_root = evidence.parent.parent
    shared.snapshot_failure_evidence(evidence, run_root, data, "before-cleanup")
    receipt = json.loads((run_root / "evidence-upload/before-cleanup/journey-result.json").read_text())
    diagnostic = receipt["failure_diagnostics"]["before-cleanup"]
    assert diagnostic["pack_host_readiness_staging_errors"] == [
        {"index": 1, "observation": "last-failed-poll", "status": "oversized"}]
    assert len(diagnostic["pack_host_readiness"]) == 1
