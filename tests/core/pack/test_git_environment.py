from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from astrid.core.pack import install
from astrid.core.pack.install import PackInstallError, _git, _git_environment

WINDOWS_SOURCE = {
    "Path": r"C:\Tools\Git\cmd;C:\Windows\System32",
    "SYSTEMROOT": r"C:\Windows",
    "SYSTEMDRIVE": "C:",
    "WINDIR": r"C:\Windows",
    "COMSPEC": r"C:\Windows\System32\cmd.exe",
    "PATHEXT": ".COM;.EXE;.BAT;.CMD",
    # Ambient configuration that must never reach the pack's Git process.
    "GIT_CONFIG_PARAMETERS": "'core.askpass=evil'",
    "GIT_SSH_COMMAND": "evil-ssh",
    "GIT_EXEC_PATH": r"C:\evil",
    "HTTPS_PROXY": "http://proxy.example.test:8080",
    "APPDATA": r"C:\Users\someone\AppData\Roaming",
}

POSIX_SOURCE = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "GIT_CONFIG_PARAMETERS": "'core.askpass=evil'",
    "GIT_SSH_COMMAND": "evil-ssh",
    "HOME": "/home/someone",
    "SSH_AUTH_SOCK": "/tmp/agent.sock",
}

ISOLATION_KEYS = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "LC_ALL": "C",
}


def test_posix_environment_is_isolated_and_uses_null_devices(tmp_path: Path) -> None:
    work = tmp_path / "work"
    askpass = str(work / "askpass")

    env = _git_environment(work, askpass, windows=False, source=POSIX_SOURCE)

    assert env == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(work),
        "GIT_ASKPASS": askpass,
        "GIT_SSH_COMMAND": f"ssh -F {os.devnull} -oBatchMode=yes",
        **ISOLATION_KEYS,
    }
    assert "/bin/false" not in env.values()
    assert "SSH_AUTH_SOCK" not in env
    assert "GIT_CONFIG_PARAMETERS" not in env


def test_windows_environment_passes_only_required_system_variables(tmp_path: Path) -> None:
    work = tmp_path / "work"
    askpass = str(work / "askpass.cmd")

    env = _git_environment(work, askpass, windows=True, source=WINDOWS_SOURCE)

    assert env == {
        "PATH": WINDOWS_SOURCE["Path"],
        "HOME": str(work),
        "USERPROFILE": str(work),
        "TEMP": str(work),
        "TMP": str(work),
        "SYSTEMROOT": r"C:\Windows",
        "SYSTEMDRIVE": "C:",
        "WINDIR": r"C:\Windows",
        "COMSPEC": r"C:\Windows\System32\cmd.exe",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "GIT_ASKPASS": askpass,
        "GIT_SSH_COMMAND": f"ssh -F {os.devnull} -oBatchMode=yes",
        **ISOLATION_KEYS,
    }
    assert "GIT_EXEC_PATH" not in env
    assert "HTTPS_PROXY" not in env
    assert "APPDATA" not in env


def test_windows_variable_names_are_matched_case_insensitively(tmp_path: Path) -> None:
    source = {"PATH": r"C:\Git", "systemroot": r"C:\Windows"}
    env = _git_environment(tmp_path, "askpass.cmd", windows=True, source=source)

    assert env["PATH"] == r"C:\Git"
    assert env["SYSTEMROOT"] == r"C:\Windows"


def test_default_platform_follows_os_name_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "nt")
    env = _git_environment(tmp_path, "askpass.cmd", source=WINDOWS_SOURCE)

    assert env["SYSTEMROOT"] == r"C:\Windows"
    assert env["USERPROFILE"] == str(tmp_path)
    assert env["GIT_SSH_COMMAND"] == f"ssh -F {os.devnull} -oBatchMode=yes"


def test_default_platform_follows_os_name_posix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    env = _git_environment(tmp_path, "askpass", source=POSIX_SOURCE)

    assert "SYSTEMROOT" not in env
    assert "USERPROFILE" not in env
    assert "TEMP" not in env
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"


def test_askpass_program_is_a_no_op_for_each_platform(tmp_path: Path) -> None:
    posix = install._askpass_program(tmp_path, windows=False)
    windows = install._askpass_program(tmp_path, windows=True)

    assert posix == str(tmp_path / "askpass")
    assert windows == str(tmp_path / "askpass.cmd")
    assert (tmp_path / "askpass").read_bytes() == b"#!/bin/sh\nexit 1\n"
    assert (tmp_path / "askpass.cmd").read_bytes() == b"@echo off\r\nexit /b 1\r\n"


def test_git_is_resolved_with_which_from_the_real_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_which(name, path=None):
        seen["name"], seen["path"] = name, path
        return "/opt/git/bin/git"

    def fake_run(command, **kwargs):
        seen["command"], seen["env"] = command, kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(install.shutil, "which", fake_which)
    monkeypatch.setattr(install.subprocess, "run", fake_run)
    monkeypatch.setenv("PATH", "/opt/git/bin:/usr/bin")

    assert _git(tmp_path, "rev-parse", "HEAD") == b"ok"
    assert seen["name"] == "git"
    assert seen["path"] == "/opt/git/bin:/usr/bin"
    assert seen["command"][0] == "/opt/git/bin/git"
    assert seen["env"]["PATH"] == "/opt/git/bin:/usr/bin"
    assert seen["env"]["GIT_CONFIG_GLOBAL"] == os.devnull


def test_missing_git_is_a_safe_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install.shutil, "which", lambda name, path=None: None)

    with pytest.raises(PackInstallError) as raised:
        _git(tmp_path, "fetch", "https://user:secret@example.test/repo.git")

    assert raised.value.code == "git_failed"
    assert str(raised.value) == "Git source operation failed (fetch; git not found)."


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (subprocess.CompletedProcess(["git"], 128, stdout=b"", stderr=b"fatal: https://user:secret@x/y"),
         "Git source operation failed (fetch; exit 128)."),
        (OSError("boom"), "Git source operation failed (fetch; OSError)."),
    ],
)
def test_git_diagnostic_names_operation_and_exit_or_error_class_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome, expected
) -> None:
    monkeypatch.setattr(install.shutil, "which", lambda name, path=None: "/usr/bin/git")

    def fake_run(command, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(install.subprocess, "run", fake_run)

    with pytest.raises(PackInstallError) as raised:
        _git(tmp_path, "fetch", "https://user:secret@example.test/repo.git")

    message = str(raised.value)
    assert message == expected
    assert "secret" not in message
    assert "example.test" not in message
    assert "fatal" not in message
