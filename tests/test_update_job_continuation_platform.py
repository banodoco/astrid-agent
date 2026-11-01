"""Native registration ownership checks used by Update continuation."""
from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from runtime_protocol import platform as runtime_platform

from astrid.installer import update_job_platform as platform


def _descriptor(tmp_path):
    operation_id = "a" * 32
    root = tmp_path / "Astrid Program"
    log = root / "logs" / "update.log"
    job = {
        "job_name": f"astrid-update-{operation_id}",
        "operation_id": operation_id,
        "program_root": str(root),
        "data_root": str(tmp_path / "Workspace"),
        "log_path": str(log),
    }
    command = ["/old/python", "-B", "-X", "utf8", "-I", "-c",
               "x = '雪; O\\'Brien'; print(x)"]
    return platform.describe(job, command, system="linux"), command


def _exec_start(command, signature="a(sasbttttuii)"):
    fields = [signature, "1", command[0], str(len(command)), *command,
              "false", "0", "0", "0", "0", "0", "0", "0"]
    return " ".join(shlex.quote(item) for item in fields) + "\n"


def test_registration_query_binds_exact_systemd_command(tmp_path, monkeypatch):
    descriptor, command = _descriptor(tmp_path)
    calls = []

    def run(args):
        calls.append(args)
        if "--property=LoadState" in args:
            return CompletedProcess(args, 0, stdout="loaded\n", stderr="")
        return CompletedProcess(args, 0, stdout=_exec_start(command), stderr="")

    monkeypatch.setattr(platform, "_run", run)
    assert platform.is_registered(descriptor, command, system="linux") is True
    assert calls == [
        ["systemctl", "--user", "show", "--property=LoadState", "--value", descriptor["name"]],
        ["busctl", "--user", "--no-pager", "get-property", "org.freedesktop.systemd1",
         "/org/freedesktop/systemd1/unit/astrid_2dupdate_2d" + "a" * 32 + "_2eservice",
         "org.freedesktop.systemd1.Service", "ExecStart"],
    ]


def test_registration_query_reports_only_positive_not_found_as_absent(tmp_path, monkeypatch):
    descriptor, command = _descriptor(tmp_path)
    monkeypatch.setattr(platform, "_run", lambda args: CompletedProcess(args, 0, "not-found\n", ""))
    assert platform.is_registered(descriptor, command, system="linux") is False


def test_registration_query_rejects_same_name_different_native_command(tmp_path, monkeypatch):
    descriptor, command = _descriptor(tmp_path)
    foreign = ["/other/python", *command[1:]]
    monkeypatch.setattr(platform, "_run", lambda args: CompletedProcess(
        args, 0, "loaded\n" if "--property=LoadState" in args else _exec_start(foreign), ""))
    with pytest.raises(RuntimeError, match="does not match"):
        platform.is_registered(descriptor, command, system="linux")


@pytest.mark.parametrize("responses", [
    [CompletedProcess([], 1, stdout="", stderr="permission denied")],
    [CompletedProcess([], 0, stdout="weird\n", stderr="")],
    [CompletedProcess([], 0, stdout="loaded\n", stderr=""),
     CompletedProcess([], 0, stdout="not-an-array 1\n", stderr="")],
    [CompletedProcess([], 0, stdout="loaded\n", stderr=""),
     CompletedProcess([], 0, stdout="a(sasbttttuii) 2\n", stderr="")],
    [CompletedProcess([], 0, stdout="loaded\n", stderr=""),
     CompletedProcess([], 1, stdout="", stderr="busctl denied")],
])
def test_registration_query_refuses_unknown_or_ambiguous_native_state(tmp_path, monkeypatch, responses):
    descriptor, command = _descriptor(tmp_path)
    calls = []

    def run(args):
        calls.append(args)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    monkeypatch.setattr(platform, "_run", run)
    with pytest.raises(RuntimeError):
        platform.is_registered(descriptor, command, system="linux")


def _mac_descriptor(tmp_path):
    operation_id = "b" * 32
    root = tmp_path / "Astrid Program"
    log = root / "logs" / "update.log"
    job = {"job_name": f"astrid-update-{operation_id}", "operation_id": operation_id,
           "program_root": str(root), "log_path": str(log)}
    command = ["/Applications/Astrid/python", "-c", "print('hello; 雪')"]
    return platform.describe(job, command, system="darwin"), command


def _owned_mac_plist(descriptor, command):
    path = Path(descriptor["plist_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(platform._plist(descriptor, command, Path(descriptor["log_path"])))
    os.chmod(path, 0o600)
    return path


def test_macos_registration_query_binds_launchd_action_and_owned_plist(tmp_path, monkeypatch):
    descriptor, command = _mac_descriptor(tmp_path)
    _owned_mac_plist(descriptor, command)
    calls = []
    monkeypatch.setattr(runtime_platform, "is_owner_only", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(platform, "_run", lambda args: calls.append(args) or CompletedProcess(
        args, 0, stdout=("program = " + command[0] + "\narguments = {\n"
                         + "\n".join(f'  "{item}",' for item in command)
                         + "\n}\n"), stderr=""))
    assert platform.is_registered(descriptor, command, system="darwin") is True
    assert calls == [["launchctl", "print", f"{descriptor['domain']}/{descriptor['name']}"]]


def test_macos_registration_accepts_literal_argument_lines_with_spaces(tmp_path, monkeypatch):
    descriptor, command = _mac_descriptor(tmp_path)
    command[0] = "/Applications/Astrid user's 安装/python"
    descriptor["command"] = command
    _owned_mac_plist(descriptor, command)
    output = ("program = " + command[0] + "\narguments = {\n"
              + "\n".join("  " + part for part in command) + "\n}\n")
    monkeypatch.setattr(platform, "_run", lambda args: CompletedProcess(args, 0, output, ""))
    assert platform.is_registered(descriptor, command, system="darwin") is True


@pytest.mark.parametrize("response", [
    CompletedProcess([], 113, stdout="", stderr="Could not find service"),
    CompletedProcess([], 1, stdout="", stderr="permission denied"),
    CompletedProcess([], 1, stdout="", stderr="Could not find service: query transport denied"),
])
def test_macos_registration_query_only_accepts_known_absence(tmp_path, monkeypatch, response):
    descriptor, command = _mac_descriptor(tmp_path)
    monkeypatch.setattr(platform, "_run", lambda _args: response)
    if response.returncode == 113:
        assert platform.is_registered(descriptor, command, system="darwin") is False
    else:
        with pytest.raises(RuntimeError):
            platform.is_registered(descriptor, command, system="darwin")


def test_macos_registration_query_rejects_extra_argument_and_unreadable_service(tmp_path, monkeypatch):
    descriptor, command = _mac_descriptor(tmp_path)
    _owned_mac_plist(descriptor, command)
    monkeypatch.setattr(runtime_platform, "is_owner_only", lambda *_args, **_kwargs: True)
    output = ("program = " + command[0] + "\narguments = {\n"
              + "\n".join(f'  "{item}",' for item in [*command, "--foreign"])
              + "\n}\n")
    monkeypatch.setattr(platform, "_run", lambda args: CompletedProcess(args, 0, output, ""))
    with pytest.raises(RuntimeError, match="does not match"):
        platform.is_registered(descriptor, command, system="darwin")
    monkeypatch.setattr(platform, "_run", lambda args: CompletedProcess(
        args, 1, stdout="", stderr="permission denied"))
    with pytest.raises(RuntimeError):
        platform.is_registered(descriptor, command, system="darwin")


def _windows_descriptor(tmp_path, monkeypatch):
    operation_id = "c" * 32
    root = tmp_path / "Astrid Program"
    log = root / "logs" / "update.log"
    sid = "S-1-5-21-12345"
    monkeypatch.setattr(platform, "_windows_sid", lambda: sid)
    job = {"job_name": f"astrid-update-{operation_id}", "operation_id": operation_id,
           "program_root": str(root), "log_path": str(log)}
    command = [r"C:\Astrid\python.exe", "-B", "-c", "print('hello; snow 雪')"]
    return platform.describe(job, command, system="win32"), command, sid


def _windows_action(descriptor, command, sid):
    worker_script = ("$ErrorActionPreference = 'Stop'\n"
                     f"$exe = {platform._ps_quote(command[0])}\n"
                     f"$workerArgs = @({', '.join(platform._ps_quote(x) for x in command[1:])})\n"
                     f"$log = {platform._ps_quote(descriptor['log_path'])}\n"
                     "try { & $exe @workerArgs *>> $log; exit $LASTEXITCODE } "
                     "catch { $_ | Out-String | Add-Content -LiteralPath $log; exit 1 }\n")
    encoded_worker = base64.b64encode(worker_script.encode("utf-16le")).decode("ascii")
    arguments = subprocess.list2cmdline(["-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_worker])
    return {"TaskPath": "\\", "TaskName": descriptor["task_name"], "UserId": sid,
            "LogonType": "Interactive", "RunLevel": "Limited", "TriggerCount": 0,
            "Actions": [{"Execute": "powershell.exe", "Arguments": arguments,
                         "WorkingDirectory": ""}]}


def test_windows_registration_query_binds_task_action_and_current_user(tmp_path, monkeypatch):
    descriptor, command, sid = _windows_descriptor(tmp_path, monkeypatch)
    expected = _windows_action(descriptor, command, sid)
    calls = []
    monkeypatch.setattr(platform, "_run", lambda args: calls.append(args) or CompletedProcess(
        args, 0, json.dumps(expected), ""))
    assert platform.is_registered(descriptor, command, system="win32") is True
    assert len(calls) == 1 and "Get-ScheduledTask" in base64.b64decode(calls[0][-1]).decode("utf-16le")


@pytest.mark.parametrize("change", ["user", "action", "extra-argument", "extra-action", "malformed"])
def test_windows_registration_query_rejects_mismatched_or_ambiguous_task(
    tmp_path, monkeypatch, change
):
    descriptor, command, sid = _windows_descriptor(tmp_path, monkeypatch)
    observed = _windows_action(descriptor, command, sid)
    if change == "user":
        observed["UserId"] = "S-1-5-21-foreign"
    elif change == "action":
        observed["Actions"][0]["Execute"] = "cmd.exe"
    elif change == "extra-argument":
        observed["Actions"][0]["Arguments"] += " --foreign"
    elif change == "extra-action":
        observed["Actions"].append(dict(observed["Actions"][0]))
    else:
        observed = {"not-json": True}
    monkeypatch.setattr(platform, "_run", lambda args: CompletedProcess(
        args, 0, "not-json" if change == "malformed" else json.dumps(observed), ""))
    with pytest.raises(RuntimeError):
        platform.is_registered(descriptor, command, system="win32")


def test_windows_registration_query_treats_only_explicit_missing_as_absent(tmp_path, monkeypatch):
    descriptor, command, _sid = _windows_descriptor(tmp_path, monkeypatch)
    monkeypatch.setattr(platform, "_run", lambda args: CompletedProcess(args, 0, "ASTRID_NOT_FOUND\n", ""))
    assert platform.is_registered(descriptor, command, system="win32") is False
    monkeypatch.setattr(platform, "_run", lambda args: CompletedProcess(args, 1, "", "access denied"))
    with pytest.raises(RuntimeError):
        platform.is_registered(descriptor, command, system="win32")
