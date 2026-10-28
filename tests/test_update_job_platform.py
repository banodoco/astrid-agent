import base64
import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest
from runtime_protocol import platform as runtime_platform

from astrid.installer import update_job
from astrid.installer import update_job_platform as platform
from astrid.installer.core import Version, write_json


@pytest.fixture(autouse=True)
def simulated_posix_user(monkeypatch):
    # Linux/macOS command captures also run on Windows, which has no getuid.
    monkeypatch.setattr(platform.os, "getuid", getattr(platform.os, "getuid", lambda: 501), raising=False)


OPERATION = "0123456789abcdef0123456789abcdef"


def job(tmp_path):
    root = tmp_path / "Astrid user's 安装"
    root.mkdir()
    return {"job_name": "astrid-update-" + OPERATION, "operation_id": OPERATION,
            "program_root": str(root), "log_path": str(root / "logs" / "update.log")}


def completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_describe_is_durable_and_does_not_register(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(platform, "_run", lambda args: calls.append(args) or completed(args))
    record = job(tmp_path)
    command = ["/opt/Astrid's app/astrid", "apply", "--title", "über 雪"]

    linux = platform.describe(record, command, system="linux")
    mac = platform.describe(record, command, system="darwin")

    assert calls == []
    assert linux["kind"] == "systemd-user-transient"
    assert linux["user"] == str(platform.os.getuid())
    assert linux["unit"] == "astrid-update-" + OPERATION + ".service"
    assert mac["kind"] == "launchd-gui-one-shot"
    assert mac["name"] == "astrid-update-" + OPERATION
    assert mac["domain"] == f"gui/{platform.os.getuid()}"
    assert Path(mac["plist_path"]).name.endswith(".plist")
    assert linux["command"] == command


def test_linux_launch_exact_transient_argv_and_log_redirection(tmp_path, monkeypatch):
    record = job(tmp_path)
    record["log_path"] = str(Path(record["program_root"]) / "logs 50% 李's" / "update.log")
    command = ["/path with spaces/$worker", "--arg", "it's 雪"]
    descriptor = platform.describe(record, command, system="linux")
    calls = []
    monkeypatch.setattr(platform, "_run", lambda args: calls.append(args) or completed(args))

    platform.launch(descriptor, command, record["log_path"], system="linux")

    escaped_log = record["log_path"].replace("%", "%%")
    assert calls == [["systemd-run", "--user", "--no-block", "--expand-environment=no",
                      "--unit=" + descriptor["name"], "--collect",
                      "--property=Type=oneshot", "--property=Restart=no",
                      "--property=StandardOutput=append:" + escaped_log,
                      "--property=StandardError=append:" + escaped_log,
                      "--", *command]]


def test_linux_query_distinguishes_active_inactive_unknown_and_cleanup(tmp_path, monkeypatch):
    descriptor = platform.describe(job(tmp_path), ["worker"], system="linux")
    responses = [completed([], stdout="active\n"), completed([], stdout="inactive\n"),
                 completed([], returncode=4, stderr="Unit not found")]
    monkeypatch.setattr(platform, "_run", lambda args: responses.pop(0))
    assert platform.is_active(descriptor)
    assert not platform.is_active(descriptor)
    assert not platform.is_active(descriptor)

    monkeypatch.setattr(platform, "_run", lambda args: completed(args, stdout="active\n"))
    assert platform.cleanup(descriptor) is None  # --collect owns post-exit removal

    monkeypatch.setattr(platform, "_run", lambda args: completed(args, returncode=1, stderr="dbus failed"))
    with pytest.raises(RuntimeError, match="Could not query"):
        platform.is_active(descriptor)


def test_linux_cleanup_accepts_already_collected_unit(tmp_path, monkeypatch):
    descriptor = platform.describe(job(tmp_path), ["worker"], system="linux")
    calls = []

    def run(args):
        calls.append(args)
        if args[0] == "systemctl" and "show" in args:
            return completed(args, stdout="inactive\n")
        return completed(args, returncode=1, stderr=f"Unit {descriptor['name']} not loaded.")

    monkeypatch.setattr(platform, "_run", run)
    assert platform.cleanup(descriptor) is None
    assert calls[-1] == ["systemctl", "--user", "reset-failed", descriptor["name"]]


def test_mac_launch_writes_private_one_shot_plist_with_literal_argv(tmp_path, monkeypatch):
    record = job(tmp_path)
    command = ["/Applications/Astrid's app/bin/worker", "--name", "雪 ' hello"]
    descriptor = platform.describe(record, command, system="darwin")
    calls = []
    monkeypatch.setattr(platform, "_run", lambda args: calls.append(args) or completed(args))

    platform.launch(descriptor, command, record["log_path"], system="darwin")

    plist = Path(descriptor["plist_path"])
    assert plist.exists()
    assert plist.stat().st_mode & 0o777 == 0o600
    tree = ET.parse(plist)
    values = tree.getroot().find("dict")
    items = list(values)
    decoded = {items[index].text: items[index + 1] for index in range(0, len(items), 2)}
    assert [x.text for x in decoded["ProgramArguments"]] == command
    assert decoded["RunAtLoad"].tag == "true"
    assert "KeepAlive" not in decoded
    assert "StartCalendarInterval" not in decoded
    assert calls == [["launchctl", "bootstrap", descriptor["domain"], str(plist)]]


def test_mac_cleanup_retains_active_and_removes_inactive_plist_before_bootout(tmp_path, monkeypatch):
    record = job(tmp_path)
    descriptor = platform.describe(record, ["worker"], system="darwin")
    plist = Path(descriptor["plist_path"])
    plist.parent.mkdir(parents=True)
    plist.write_text("owned")
    responses = [completed([], stdout="state = running\npid = 123\n"),
                 completed([], returncode=113, stderr="Could not find service")]
    calls = []

    def run(args):
        calls.append((args, plist.exists()))
        return responses.pop(0)

    monkeypatch.setattr(platform, "_run", run)
    with pytest.raises(RuntimeError, match="still active"):
        platform.cleanup(descriptor)
    assert plist.exists()

    monkeypatch.setattr(platform, "_run", lambda args: completed(args, returncode=113,
                                                                      stderr="Could not find service"))
    assert platform.cleanup(descriptor) is None
    assert not plist.exists()

    plist.write_text("owned")
    calls = []
    monkeypatch.setattr(platform, "_run", lambda args: calls.append((args, plist.exists())) or completed(args))
    assert platform.cleanup(descriptor, finishing=True) is None
    assert calls == [(["launchctl", "bootout", f"{descriptor['domain']}/{descriptor['name']}"], False)]
    assert not plist.exists()


def test_mac_cleanup_accepts_already_absent_service(tmp_path, monkeypatch):
    descriptor = platform.describe(job(tmp_path), ["worker"], system="darwin")
    calls = []

    def run(args):
        calls.append(args)
        if args[1] == "print":
            return completed(args, returncode=113, stderr="Could not find specified service.")
        return completed(args, returncode=3, stderr="No such process")

    monkeypatch.setattr(platform, "_run", run)
    assert platform.cleanup(descriptor) is None
    assert calls == [["launchctl", "print", f"{descriptor['domain']}/{descriptor['name']}"],
                     ["launchctl", "bootout", f"{descriptor['domain']}/{descriptor['name']}"]]


def test_windows_uses_current_sid_on_demand_and_safely_encoded_arguments(tmp_path, monkeypatch):
    record = job(tmp_path)
    calls = []

    def run(args):
        calls.append(args)
        script = base64.b64decode(args[-1]).decode("utf-16le") if "-EncodedCommand" in args else ""
        if "WindowsIdentity" in script:
            return completed(args, stdout="S-1-5-21-12345\n")
        return completed(args)

    monkeypatch.setattr(platform, "_run", run)
    command = [r"C:\Program Files\Astrid\worker.exe", "--label", "雪 O'Brien"]
    descriptor = platform.describe(record, command, system="win32")
    assert descriptor["user"] == "S-1-5-21-12345"
    assert "WindowsIdentity" in base64.b64decode(calls[0][-1]).decode("utf-16le")

    platform.launch(descriptor, command, record["log_path"], system="win32")
    task_script = base64.b64decode(calls[-1][-1]).decode("utf-16le")
    assert descriptor["name"] == "astrid-update-" + OPERATION
    assert "New-ScheduledTaskPrincipal -UserId 'S-1-5-21-12345' -LogonType Interactive -RunLevel Limited" in task_script
    assert "Register-ScheduledTask" in task_script
    assert "Start-ScheduledTask" in task_script
    assert "-TaskPath '\\'" in task_script
    worker_encoded = __import__("re").search(r"-EncodedCommand ([A-Za-z0-9+/=]+)", task_script).group(1)
    worker_script = base64.b64decode(worker_encoded).decode("utf-16le")
    assert "O''Brien" in worker_script
    assert "雪" in worker_script
    assert "*>> $log" in worker_script
    assert "-Trigger" not in task_script
    assert "-Repetition" not in task_script
    assert "Restart" not in task_script
    assert "-Settings $s" in task_script
    assert "-MultipleInstances IgnoreNew" in task_script
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in task_script
    assert "-AllowStartIfOnBatteries" in task_script
    assert "-DontStopIfGoingOnBatteries" in task_script
    assert "-StartWhenAvailable" not in task_script
    assert "-Force" not in task_script
    assert "-EncodedCommand" in calls[1]


def test_windows_worker_command_runs_private_utf8_script_with_complex_values(tmp_path, monkeypatch):
    monkeypatch.setattr(update_job, "sys", SimpleNamespace(platform="win32"))
    operation = "abcdef0123456789abcdef0123456789"
    private = tmp_path / f"astrid-update-{operation} O'Brien 雪 inputs"
    private.mkdir()
    runtime_platform.private_directory(private)
    manifest, bootstrap = private / "manifest", private / "bootstrap"
    manifest.write_text("manifest", encoding="utf-8")
    bootstrap.write_text("bootstrap", encoding="utf-8")
    runtime_platform.owner_only(manifest)
    runtime_platform.owner_only(bootstrap)

    source = tmp_path / "source O'Brien 雪 checkout"
    package = source / "astrid" / "installer"
    package.mkdir(parents=True)
    (source / "astrid" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "update_job.py").write_text(
        "import os\n"
        "def run_job(root, operation):\n"
        "    return {'root': root, 'operation': operation, 'env': os.environ.get('ASTRID_TEST_VALUE')}\n",
        encoding="utf-8")
    old_root = tmp_path / "old O'Brien 雪 version"
    old_root.mkdir()
    profile = old_root / "profile.json"
    write_json(profile, {"runtime_checkout": str(source)})
    log = tmp_path / "Astrid O'Brien 雪" / "logs O'Brien 雪" / "update.log"
    program_root = tmp_path / "Astrid O'Brien 雪"
    program_root.mkdir()
    log.parent.mkdir(parents=True)
    job = {
        "operation_id": operation,
        "program_root": str(program_root),
        "log_path": str(log),
        "metadata": {
            "manifest_path": str(manifest), "bootstrap_path": str(bootstrap),
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "bootstrap_sha256": hashlib.sha256(bootstrap.read_bytes()).hexdigest(),
        },
    }
    old = Version(old_root, Path(sys.executable), profile,
                  {"ASTRID_TEST_VALUE": "O'Brien with spaces 雪"}, release=7)

    worker = private / f"worker-{operation}.py"
    worker.write_text("unrelated pre-existing content", encoding="utf-8")
    runtime_platform.owner_only(worker)
    with pytest.raises(ValueError, match="already exists"):
        update_job._worker_command(old, job)
    assert worker.read_text(encoding="utf-8") == "unrelated pre-existing content"
    worker.unlink()
    target = private / "unrelated target"
    target.write_text("leave this alone", encoding="utf-8")
    try:
        worker.symlink_to(target)
    except (OSError, NotImplementedError):
        pass
    else:
        with pytest.raises(ValueError, match="already exists"):
            update_job._worker_command(old, job)
        assert target.read_text(encoding="utf-8") == "leave this alone"
        worker.unlink()

    command = update_job._worker_command(old, job)

    assert command == [sys.executable, "-B", "-X", "utf8", "-I",
                       str(private / f"worker-{operation}.py")]
    worker = Path(command[-1])
    assert worker.exists() and runtime_platform.is_owner_only(worker)

    # Capture the actual Windows adapter wrapper generated around this argv.
    record = {"job_name": "astrid-update-" + operation, "operation_id": operation,
              "program_root": str(program_root), "log_path": str(log)}
    calls = []

    def capture(args):
        calls.append(args)
        script = base64.b64decode(args[-1]).decode("utf-16le") if "-EncodedCommand" in args else ""
        if "WindowsIdentity" in script:
            return completed(args, stdout="S-1-5-21-98765")
        return completed(args)

    monkeypatch.setattr(platform, "_run", capture)
    descriptor = platform.describe(record, command, system="win32")
    platform.launch(descriptor, command, str(log), system="win32")
    task_script = base64.b64decode(calls[-1][-1]).decode("utf-16le")
    worker_encoded = __import__("re").search(r"-EncodedCommand ([A-Za-z0-9+/=]+)", task_script).group(1)
    wrapper = base64.b64decode(worker_encoded).decode("utf-16le")
    assert "-c" not in wrapper
    assert str(worker).replace("'", "''") in wrapper
    assert "O''Brien with spaces 雪" not in wrapper
    assert str(program_root) not in wrapper

    result = subprocess.run(command, check=True, capture_output=True, text=True)
    assert result.stdout == ""
    output = json.loads(log.read_text(encoding="utf-8").strip())
    assert output == {"root": str(program_root), "operation": operation,
                      "env": "O'Brien with spaces 雪"}

    job["metadata"].update({
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "bootstrap_sha256": hashlib.sha256(bootstrap.read_bytes()).hexdigest(),
    })
    update_job._cleanup_private_inputs(job, "clean")
    assert not worker.exists()
    assert target.read_text(encoding="utf-8") == "leave this alone"


def test_windows_query_cleanup_and_failure(tmp_path, monkeypatch):
    record = job(tmp_path)
    calls = []

    def run(args):
        calls.append(args)
        script = base64.b64decode(args[-1]).decode("utf-16le") if "-EncodedCommand" in args else ""
        if "WindowsIdentity" in script:
            return completed(args, stdout="S-1-5-21-2")
        if "Get-ScheduledTask" in script:
            return completed(args, stdout="Running")
        return completed(args)

    monkeypatch.setattr(platform, "_run", run)
    descriptor = platform.describe(record, ["worker"], system="win32")
    with pytest.raises(RuntimeError, match="still active"):
        platform.cleanup(descriptor)
    query_script = next(base64.b64decode(call[-1]).decode("utf-16le") for call in calls
                        if "-EncodedCommand" in call
                        and "Get-ScheduledTask" in base64.b64decode(call[-1]).decode("utf-16le"))
    assert "Get-ScheduledTask -TaskPath '\\'" in query_script
    assert "Where-Object { $_.TaskName -eq 'astrid-update-" in query_script
    assert not any("Unregister-ScheduledTask" in base64.b64decode(call[-1]).decode("utf-16le")
                   for call in calls if "-EncodedCommand" in call)

    def finishing_run(args):
        calls.append(args)
        script = base64.b64decode(args[-1]).decode("utf-16le")
        return completed(args, stdout="S-1-5-21-2" if "WindowsIdentity" in script else "")

    monkeypatch.setattr(platform, "_run", finishing_run)
    assert platform.cleanup(descriptor, finishing=True) is None
    cleanup_script = base64.b64decode(calls[-1][-1]).decode("utf-16le")
    assert "Unregister-ScheduledTask" in cleanup_script
    assert "-TaskPath '\\'" in cleanup_script
    assert descriptor["name"] in cleanup_script

    def query_failure(args):
        script = base64.b64decode(args[-1]).decode("utf-16le")
        if "WindowsIdentity" in script:
            return completed(args, stdout="S-1-5-21-2")
        return completed(args, returncode=1, stderr="task query denied")

    monkeypatch.setattr(platform, "_run", query_failure)
    with pytest.raises(RuntimeError, match="Could not query"):
        platform.is_active(descriptor)


def test_windows_cleanup_reports_access_denied_instead_of_suppressing_it(tmp_path, monkeypatch):
    descriptor = None

    def run(args):
        script = base64.b64decode(args[-1]).decode("utf-16le")
        if "WindowsIdentity" in script:
            return completed(args, stdout="S-1-5-21-3")
        assert "Get-ScheduledTask -TaskPath '\\'" in script
        assert "Unregister-ScheduledTask" in script
        assert "-ErrorAction Stop" in script
        assert "SilentlyContinue" not in script
        return completed(args, returncode=1, stderr="Access denied")

    monkeypatch.setattr(platform, "_run", run)
    descriptor = platform.describe(job(tmp_path), ["worker"], system="win32")
    with pytest.raises(RuntimeError, match="Could not clean up update job: Access denied"):
        platform.cleanup(descriptor, finishing=True)


def test_launch_failure_is_reported_without_detached_process_fallback(tmp_path, monkeypatch):
    record = job(tmp_path)
    descriptor = platform.describe(record, ["worker"], system="linux")
    calls = []
    monkeypatch.setattr(platform, "_run", lambda args: calls.append(args)
                        or completed(args, returncode=1, stderr="systemd unavailable"))
    with pytest.raises(RuntimeError, match="systemd unavailable"):
        platform.launch(descriptor, ["worker"], record["log_path"], system="linux")
    assert calls[0][0] == "systemd-run"
    assert all(arg not in {"Popen", "start_new_session"} for arg in calls[0])


def test_descriptor_rejects_unowned_names_and_platform_mismatch(tmp_path):
    descriptor = platform.describe(job(tmp_path), ["worker"], system="linux")
    descriptor["name"] = "unrelated.service"
    with pytest.raises(ValueError, match="does not own"):
        platform.is_active(descriptor)
    descriptor["name"] = descriptor["unit"]
    with pytest.raises(ValueError, match="another system"):
        platform.is_active(descriptor, system="darwin")
