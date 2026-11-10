"""Portable bounded capture tests; native Windows service execution is separate."""

import json
import os
import re
import subprocess

import pytest

from scripts import ci_journey as journey
from scripts import ci_windows_journey as windows


@pytest.fixture
def capture(tmp_path, monkeypatch):
    data = tmp_path / "data"
    runtime = data / "runtime"
    runtime.mkdir(parents=True)
    evidence = tmp_path / "evidence" / "release9-then-install"
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


def test_real_profile_pack_host_capture_is_owned_bounded_and_redacted(capture, monkeypatch):
    _, _, evidence, _, _ = capture
    monkeypatch.setattr(journey.sys, "platform", "win32")
    run_root = evidence.parent
    profile = run_root / "real-user-profile"
    source = profile / "Astrid Runtime" / "logs" / "service-pack-host.log"
    source.parent.mkdir(parents=True)
    source.write_bytes(
        b"x" * (journey.WINDOWS_PROFILE_SERVICE_LOG_LIMIT + 512) +
        b"\nAuthorization: Bearer top-secret\n"
        b"failed at C:\\Users\\private-user\\Documents\\personal.txt\n"
        b"also /Users/private-user/private.txt\n")
    commands = []
    action = {"executable": str(run_root) + r"\program\pythonw.exe",
              "arguments": f'-c private-secret --data-root "{run_root / "data"}"',
              "run_root_match": True, "data_root_match": True}
    observed = {"task_found": True, "task_path": "\\", "action_count": 1,
                "actions": [action], "owned_count": 1, "profile_local": str(profile)}

    def owned_task_probe(command, **kwargs):
        commands.append(command[-1])
        assert kwargs["timeout"] == 30 and kwargs["capture_output"] and not kwargs["check"]
        return subprocess.CompletedProcess(command, 0, json.dumps(observed).encode(), b"")

    monkeypatch.setattr(journey.subprocess, "run", owned_task_probe)
    journey.capture_runtime_service_logs(evidence, run_root, "before-cleanup")

    captured = evidence / "runtime-service-service-pack-host-real-profile-before-cleanup.log"
    text = captured.read_text(encoding="utf-8")
    receipt = json.loads((evidence / "windows-real-profile-pack-host-before-cleanup.json").read_text())
    assert receipt["status"] == "captured"
    assert receipt["evidence"] == captured.name
    assert receipt["source_bytes"] > journey.WINDOWS_PROFILE_SERVICE_LOG_LIMIT
    assert receipt["truncated"] is True
    assert receipt["captured_bytes"] == captured.stat().st_size <= journey.WINDOWS_PROFILE_SERVICE_LOG_LIMIT
    assert "top-secret" not in text and "private-user" not in text and "personal.txt" not in text
    assert "<redacted>" in text and "<path>" in text
    assert str(run_root) in commands[0] and str(run_root / "data") in commands[0]
    assert str(profile) not in json.dumps(receipt)
    assert "private-secret" not in json.dumps(receipt)
    assert receipt["probe"]["exit_code"] == 0
    assert receipt["probe"]["actions"][0]["data_root_match"] is True
    assert "<data-root>" in receipt["probe"]["actions"][0]["arguments"]
    journey.retain_named_evidence(evidence, "before-cleanup")
    staged = evidence.parent.parent / "evidence-upload" / "before-cleanup" / "windows-real-profile-pack-host-before-cleanup.json"
    assert json.loads(staged.read_text()) == receipt


@pytest.mark.parametrize("found,root_match,data_match,owned_count", [
    (False, False, False, 0),
    (True, False, True, 0),
    (True, True, False, 0),
    (True, True, True, 2),
])
def test_real_profile_log_probe_rejects_unowned_or_ambiguous(capture, monkeypatch, found, root_match, data_match, owned_count):
    _, _, evidence, _, _ = capture
    monkeypatch.setattr(journey.sys, "platform", "win32")
    action = {"executable": r"C:\Users\private-person\secret.exe",
              "arguments": "--password private-secret", "run_root_match": root_match,
              "data_root_match": data_match}
    observed = {"task_found": found, "task_path": "\\" if found else None,
                "action_count": owned_count or int(found), "actions": [action] if found else [],
                "owned_count": owned_count, "profile_local": "/private/should-not-read"}
    monkeypatch.setattr(journey.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a[0], 0, json.dumps(observed).encode(), b""))
    journey.capture_runtime_service_logs(evidence, evidence.parent, "before-cleanup")
    receipt = json.loads((evidence / "windows-real-profile-pack-host-before-cleanup.json").read_text())
    assert receipt["status"] == "not_owned"
    assert receipt["probe"]["task_found"] is found
    assert receipt["probe"]["owned_count"] == owned_count
    assert "private" not in json.dumps(receipt)
    assert not (evidence / "runtime-service-service-pack-host-real-profile-before-cleanup.log").exists()


@pytest.mark.parametrize("result,error,exit_code", [
    (subprocess.CompletedProcess([], 5, b"private output", b"private error"), "command_failed", 5),
    (subprocess.CompletedProcess([], 0, b"bad output", b""), "JSONDecodeError", 0),
    (subprocess.TimeoutExpired("powershell", 30), "TimeoutExpired", None),
    (FileNotFoundError("private path"), "FileNotFoundError", None),
])
def test_real_profile_log_probe_failure_is_recorded_without_private_output(capture, monkeypatch, result, error, exit_code):
    _, _, evidence, _, _ = capture
    monkeypatch.setattr(journey.sys, "platform", "win32")
    def run(*args, **kwargs):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(journey.subprocess, "run", run)
    journey.capture_runtime_service_logs(evidence, evidence.parent, "before-cleanup")
    receipt = json.loads((evidence / "windows-real-profile-pack-host-before-cleanup.json").read_text())
    assert receipt["status"] == "error"
    assert receipt["probe"]["exit_code"] == exit_code
    assert receipt["probe"]["error"] == error
    assert "private" not in json.dumps(receipt)


def test_profile_probe_redacts_arbitrary_action_values():
    sanitize = journey._profile_probe_text
    root = r"C:\run"
    data = root + r"\data"
    assert sanitize(root + r"\program\pythonw.exe", root, data, arguments=False) == r"<run-root>\...\pythonw.exe"
    assert sanitize(r"C:\Users\private\pythonw.exe", root, data, arguments=False) == "<outside-run-root>"
    text = sanitize(f'-c "secret private" --data-root "{data}"', root, data, arguments=True)
    assert "secret" not in text and "private" not in text
    assert "--data-root" in text and "<data-root>" in text
    assert len(sanitize("secret " * 10000, root, data, arguments=True)) <= 2048


def test_windows_task_path_forms_include_exact_python_source_escape():
    data = r"D:\a\_temp\astrid-r1-direct-windows\data"
    assert journey._windows_task_path_forms(data) == (
        data, r"D:\\a\\_temp\\astrid-r1-direct-windows\\data")


def test_profile_probe_receipt_limits_actions_and_hides_task_data(capture, monkeypatch):
    _, _, evidence, _, _ = capture
    monkeypatch.setattr(journey.sys, "platform", "win32")
    action = {"executable": r"C:\Users\private-person\secret.exe",
              "arguments": "private-secret " * 2000, "run_root_match": False,
              "data_root_match": False}
    observed = {"task_found": True, "task_path": "\\private-folder\\", "action_count": 40,
                "actions": [action] * 40, "owned_count": 0, "profile_local": None}
    monkeypatch.setattr(journey.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a[0], 0, json.dumps(observed).encode(), b""))
    journey.capture_runtime_service_logs(evidence, evidence.parent, "before-cleanup")
    receipt = json.loads((evidence / "windows-real-profile-pack-host-before-cleanup.json").read_text())
    assert receipt["probe"]["task_path"] == "<other>"
    assert receipt["probe"]["action_count"] == 40
    assert receipt["probe"]["actions_truncated"] is True
    assert len(receipt["probe"]["actions"]) == 8
    assert len(json.dumps(receipt)) < 20000
    assert "private" not in json.dumps(receipt)


@pytest.mark.windows_native
@pytest.mark.parametrize("scenario,root_match,data_match,owned_count", [
    ("match", True, True, 1),
    ("case", True, True, 1),
    ("python-escaped-data", True, True, 1),
    ("root-sibling", False, True, 0),
    ("wrong-data", True, False, 0),
    ("duplicate", True, True, 2),
])
def test_native_profile_probe_predicates(tmp_path, monkeypatch, scenario, root_match, data_match, owned_count):
    root = tmp_path / "run"
    root.mkdir()
    evidence = root / "evidence"
    evidence.mkdir()
    profile = tmp_path / "profile"
    source = profile / "Astrid Runtime" / "logs" / "service-pack-host.log"
    source.parent.mkdir(parents=True)
    source.write_text("safe log")
    executable = str(root / "program" / "pythonw.exe")
    data = str(root / "data")
    if scenario == "case":
        executable, data = executable.upper(), data.upper()
    if scenario == "python-escaped-data":
        data = data.replace("\\", "\\\\")
    if scenario == "root-sibling":
        executable = str(root) + r"-other\pythonw.exe"
    if scenario == "wrong-data":
        data = str(tmp_path / "foreign-data")
    action = "[pscustomobject]@{ Execute = %s; Arguments = %s }" % (
        windows._powershell_quote(executable), windows._powershell_quote("--data-root " + data))
    actions = action + (", " + action if scenario == "duplicate" else "")
    prefix = ("function Get-ScheduledTask { param($TaskName,$TaskPath,$ErrorAction) "
              "[pscustomobject]@{ TaskPath = '\\'; Actions = @(" + actions + ") } }; ")
    real_run = subprocess.run
    def run(command, **kwargs):
        command = list(command)
        command[-1] = prefix + command[-1]
        result = real_run(command, **kwargs)
        if result.returncode == 0:
            observed = json.loads(result.stdout.decode("utf-8-sig"))
            if observed["owned_count"] == 1:
                observed["profile_local"] = str(profile)
            result.stdout = json.dumps(observed).encode()
        return result
    monkeypatch.setattr(journey.subprocess, "run", run)
    journey._capture_windows_profile_pack_host_log(evidence, root, "before-cleanup")
    receipt = json.loads((evidence / "windows-real-profile-pack-host-before-cleanup.json").read_text())
    assert receipt["probe"]["exit_code"] == 0
    assert receipt["probe"]["actions"][0]["run_root_match"] is root_match
    assert receipt["probe"]["actions"][0]["data_root_match"] is data_match
    assert receipt["probe"]["owned_count"] == owned_count
    assert receipt["status"] == ("captured" if owned_count == 1 else "not_owned")
