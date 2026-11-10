from __future__ import annotations

import os
import shlex
import shutil
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
    # Absolute entries that hold no ssh, so the expected ssh program is the bare name.
    "PATH": "/astrid-test-missing/bin:/astrid-test-missing/sbin",
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
        "PATH": "/astrid-test-missing/bin:/astrid-test-missing/sbin",
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
    path_value = ";".join((str(tmp_path / "Git" / "cmd"), str(tmp_path / "System32")))
    source = {**WINDOWS_SOURCE, "Path": path_value}

    env = _git_environment(work, askpass, windows=True, source=source)

    assert env == {
        "PATH": path_value,
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
    git_dir = tmp_path / "Git"
    source = {"PATH": str(git_dir), "systemroot": str(tmp_path / "Windows")}
    env = _git_environment(tmp_path, "askpass.cmd", windows=True, source=source)

    assert env["PATH"] == str(git_dir)
    assert env["SYSTEMROOT"] == str(tmp_path / "Windows")


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
    assert env["PATH"] == "/astrid-test-missing/bin:/astrid-test-missing/sbin"


def test_askpass_program_is_a_no_op_for_each_platform(tmp_path: Path) -> None:
    posix = install._askpass_program(tmp_path, windows=False)
    windows = install._askpass_program(tmp_path, windows=True)

    assert posix == str(tmp_path / ".askpass")
    assert windows == str(tmp_path / ".askpass.cmd")
    assert (tmp_path / ".askpass").read_bytes() == b"#!/bin/sh\nexit 1\n"
    assert (tmp_path / ".askpass.cmd").read_bytes() == b"@echo off\r\nexit /b 1\r\n"


def test_askpass_name_cannot_collide_with_a_pack_id(tmp_path: Path) -> None:
    # Pack ids never start with a dot, so the helper cannot share a name with one.
    assert Path(install._askpass_program(tmp_path, windows=False)).name.startswith(".")
    assert Path(install._askpass_program(tmp_path, windows=True)).name.startswith(".")


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    path.chmod(0o755)
    return path


def test_git_is_resolved_from_the_real_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tmp_path / "opt" / "git" / "bin"
    _executable(real / "git")
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"], seen["env"] = command, kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    monkeypatch.setenv("PATH", str(real))
    (tmp_path / "work").mkdir()

    assert _git(tmp_path / "work", "rev-parse", "HEAD") == b"ok"
    assert seen["command"][0] == str(real / "git")
    assert seen["env"]["PATH"] == str(real)
    assert seen["env"]["GIT_CONFIG_GLOBAL"] == os.devnull


def test_planted_git_in_the_working_directory_is_never_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    planted_dir = tmp_path / "project"
    _executable(planted_dir / "git")
    _executable(planted_dir / "git.exe")
    monkeypatch.chdir(planted_dir)
    for windows in (False, True):
        # Current directory as a relative entry, an empty entry, and as an absolute entry.
        for path_value in (".", "", str(planted_dir)):
            assert install._git_executable(path_value, windows=windows, cwd=planted_dir) is None


def test_windows_resolver_skips_cwd_and_finds_the_real_git(tmp_path: Path) -> None:
    planted_dir = tmp_path / "project"
    _executable(planted_dir / "git.exe")
    real = tmp_path / "Git" / "cmd"
    real_git = _executable(real / "git.exe")

    found = install._git_executable(";".join(("", ".", str(planted_dir), str(real))),
                                    windows=True, cwd=planted_dir)

    assert found == str(real_git)
    assert Path(found).parent != planted_dir


def test_posix_resolver_skips_cwd_and_finds_the_real_git(tmp_path: Path) -> None:
    planted_dir = tmp_path / "project"
    _executable(planted_dir / "git")
    real = _executable(tmp_path / "bin" / "git")

    found = install._git_executable(os.pathsep.join((".", str(planted_dir), str(real.parent))),
                                    windows=False, cwd=planted_dir)

    assert found == str(real)


def test_git_missing_from_absolute_path_entries_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _executable(tmp_path / "project" / "git")
    monkeypatch.chdir(tmp_path / "project")
    monkeypatch.setenv("PATH", ".")
    (tmp_path / "work").mkdir()

    with pytest.raises(PackInstallError) as raised:
        _git(tmp_path / "work", "fetch", "https://example.test/repo.git")

    assert str(raised.value) == "Git source operation failed (fetch; git not found)."


def test_missing_git_is_a_safe_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    (tmp_path / "work").mkdir()

    with pytest.raises(PackInstallError) as raised:
        _git(tmp_path / "work", "fetch", "https://user:secret@example.test/repo.git")

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


def test_git_child_path_keeps_only_absolute_entries_outside_the_working_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = {"PATH": os.pathsep.join((".", "", str(project), "relative/dir", "/astrid-test-missing/bin"))}

    env = _git_environment(tmp_path, "askpass", windows=False, source=source, cwd=project)

    assert env["PATH"] == "/astrid-test-missing/bin"


def test_ssh_is_resolved_to_an_absolute_path_when_found(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    ssh = _executable(bin_dir / "ssh")

    env = _git_environment(tmp_path, "askpass", windows=False, source={"PATH": str(bin_dir)},
                           cwd=tmp_path / "project")

    assert env["GIT_SSH_COMMAND"] == f"{shlex.quote(str(ssh))} -F {os.devnull} -oBatchMode=yes"


def test_planted_windows_ssh_in_working_directory_is_not_resolved(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _executable(project / "ssh.exe")
    real_ssh = _executable(tmp_path / "Git" / "usr" / "bin" / "ssh.exe")
    source = {"PATH": ".;;" + str(project) + ";" + str(real_ssh.parent)}

    env = _git_environment(tmp_path, "askpass.cmd", windows=True, source=source, cwd=project)

    assert env["GIT_SSH_COMMAND"] == f'"{real_ssh}" -F {os.devnull} -oBatchMode=yes'
    assert env["PATH"] == str(real_ssh.parent)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell and planted executable")
def test_planted_ssh_in_working_directory_never_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_git = shutil.which("git")
    if real_git is None:
        pytest.skip("git is not installed")
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "planted-ssh-ran"
    planted = project / "ssh"
    planted.write_text(f"#!/bin/sh\necho ran > {marker}\nexit 1\n", encoding="ascii")
    planted.chmod(0o755)
    monkeypatch.chdir(project)
    # "." and an empty entry on PATH, plus the real git directory.
    monkeypatch.setenv("PATH", os.pathsep.join((".", "", os.path.dirname(real_git))))
    work = tmp_path / "work"
    work.mkdir()
    install._git(work, "init", "--bare", "--template=")

    with pytest.raises(PackInstallError):
        install._git(work, "ls-remote", "ssh://git@127.0.0.1:1/repo.git")

    assert not marker.exists()


def test_backslash_led_path_entry_is_relative_on_posix_and_never_runs(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    # On POSIX a "\\x" entry is a relative name: a directory called "\\x" in the cwd.
    project = tmp_path / "project"
    planted = project / "\\x"
    _executable(planted / "git")
    _executable(planted / "ssh")
    monkeypatch.chdir(project)

    assert install._git_executable("\\x", windows=False, cwd=project) is None
    env = _git_environment(tmp_path / "work", "askpass", windows=False, source={"PATH": "\\x"},
                           cwd=project)

    assert env["PATH"] == ""
    assert env["GIT_SSH_COMMAND"].startswith("ssh -F ")
    assert str(planted) not in env["GIT_SSH_COMMAND"]
