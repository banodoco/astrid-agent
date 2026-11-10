from __future__ import annotations

import os
from pathlib import Path

import pytest

from astrid.installer import tools


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    path.chmod(0o755)
    return path


def _run(output: str, calls: list[list[str]]):
    def run(argv):
        calls.append(list(argv))
        return output
    return run


def test_install_records_the_installing_users_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git = _executable(tmp_path / "Program Files" / "Git" / "cmd" / "git")
    monkeypatch.setenv("PATH", str(git.parent))
    monkeypatch.chdir(tmp_path)
    calls: list[list[str]] = []
    env: dict[str, str] = {}

    tools._record_installing_git(env, _run("git version 2.47.0\n", calls), windows=False)

    assert env == {"ASTRID_GIT_EXECUTABLE": str(git)}
    assert calls == [[str(git), "--version"]]


def test_install_records_windows_git_on_the_installing_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git = _executable(tmp_path / "Git" / "cmd" / "git.exe")
    monkeypatch.setenv("PATH", ";".join(("", ".", str(git.parent))))
    monkeypatch.chdir(tmp_path)
    env: dict[str, str] = {}

    tools._record_installing_git(env, _run("git version 2.47.0.windows.1\n", []), windows=True)

    assert env == {"ASTRID_GIT_EXECUTABLE": str(git)}


def test_install_never_records_git_from_the_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    _executable(project / "git")
    monkeypatch.chdir(project)
    monkeypatch.setenv("PATH", os.pathsep.join((".", "")))
    calls: list[list[str]] = []
    env: dict[str, str] = {}

    tools._record_installing_git(env, _run("git version 2.47.0\n", calls), windows=False)

    assert env == {}
    assert calls == []


def test_install_without_git_records_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.chdir(tmp_path)
    env: dict[str, str] = {}

    tools._record_installing_git(env, _run("", []), windows=False)

    assert env == {}


def test_install_refuses_a_git_that_does_not_identify_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git = _executable(tmp_path / "bin" / "git")
    monkeypatch.setenv("PATH", str(git.parent))
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="did not identify itself as Git"):
        tools._record_installing_git({}, _run("not a tool\n", []), windows=False)
