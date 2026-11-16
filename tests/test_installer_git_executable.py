from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from astrid.installer import tools


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    path.chmod(0o755)
    return path


def _run(output: str, calls: list[list[str]]):
    def run(argv, **kwargs):
        calls.append(list(argv))
        assert kwargs == {"timeout": 60}
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


@pytest.mark.parametrize(("failure", "reason"), [
    ("not a tool\n", "did not identify itself as Git"),
    (subprocess.CalledProcessError(1, ["git"]), "failed to report its version (CalledProcessError)"),
    (subprocess.TimeoutExpired(["git"], 60), "failed to report its version (TimeoutExpired)"),
    (PermissionError(13, "denied"), "failed to report its version (PermissionError)"),
])
def test_install_records_nothing_and_continues_for_a_broken_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                failure, reason) -> None:
    git = _executable(tmp_path / "bin" / "git")
    monkeypatch.setenv("PATH", str(git.parent))
    monkeypatch.chdir(tmp_path)
    notes: list[str] = []
    env: dict[str, str] = {}

    def run(argv, **_kwargs):
        if isinstance(failure, BaseException):
            raise failure
        return failure

    tools._record_installing_git(env, run, windows=False, note=notes.append)

    assert env == {}
    assert notes == [f"Git for pack installs was not recorded: {git} {reason}."]


def test_installer_diagnostic_appends_one_line_to_the_install_log(tmp_path: Path) -> None:
    from astrid.installer.backend import Backend

    log = tmp_path / "install.log"
    backend = Backend(tmp_path / "program", log, tmp_path / "uv", say=lambda _line: None)

    backend.diagnostic("Git for pack installs was not recorded: /x/git did not identify itself as Git.")

    assert log.read_text(encoding="utf-8") == (
        "Git for pack installs was not recorded: /x/git did not identify itself as Git.\n")


@pytest.mark.posix_native
def test_path_alias_to_cwd_is_never_executed_or_recorded(tmp_path, monkeypatch):
    project = tmp_path / "project"
    _executable(project / "git")
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    monkeypatch.chdir(project)
    monkeypatch.setenv("PATH", str(alias))
    calls = []
    env = {}

    tools._record_installing_git(env, _run("git version 2.47.0", calls), windows=False)

    assert env == {}
    assert calls == []


@pytest.mark.posix_native
def test_recorded_symlink_is_canonical_and_survives_retarget(tmp_path, monkeypatch):
    first = _executable(tmp_path / "first" / "git")
    second = _executable(tmp_path / "second" / "git")
    first.write_text("#!/bin/sh\necho git version first\n", encoding="ascii")
    second.write_text("#!/bin/sh\necho git version second\n", encoding="ascii")
    alias = tmp_path / "selected-git"
    alias.symlink_to(first)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")
    env = {}
    def run(argv, **kwargs):
        return subprocess.check_output(argv, text=True, **kwargs)

    tools._record_installing_git(env, run, windows=False, recorded=str(alias))
    assert env["ASTRID_GIT_EXECUTABLE"] == str(first)
    alias.unlink()
    alias.symlink_to(second)
    assert run([env["ASTRID_GIT_EXECUTABLE"], "--version"]).strip() == "git version first"


def test_invalid_previous_git_falls_back_to_path(tmp_path, monkeypatch):
    previous = _executable(tmp_path / "old" / "git")
    fallback = _executable(tmp_path / "new" / "git")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(fallback.parent))
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv[0])
        return "not git" if argv[0] == str(previous) else "git version 2.47.0"

    env = {}
    tools._record_installing_git(env, run, windows=False, recorded=str(previous))
    assert env["ASTRID_GIT_EXECUTABLE"] == str(fallback)
    assert calls == [str(previous), str(fallback)]


@pytest.mark.posix_native
def test_path_git_symlink_is_recorded_as_its_canonical_target(tmp_path, monkeypatch):
    real = _executable(tmp_path / "real" / "git")
    alias_dir = tmp_path / "bin"
    alias_dir.mkdir()
    (alias_dir / "git").symlink_to(real)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(alias_dir))
    env = {}
    calls = []

    tools._record_installing_git(env, _run("git version 2.47.0", calls), windows=False)
    assert env["ASTRID_GIT_EXECUTABLE"] == str(real)
    assert calls == [[str(real), "--version"]]


@pytest.mark.parametrize("recorded", [42, "/invalid\x00path"])
def test_invalid_recorded_value_falls_back_to_path(tmp_path, monkeypatch, recorded):
    fallback = _executable(tmp_path / "bin" / "git")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(fallback.parent))
    env = {}

    tools._record_installing_git(env, _run("git version 2.47.0", []), windows=False, recorded=recorded)
    assert env["ASTRID_GIT_EXECUTABLE"] == str(fallback)


@pytest.mark.parametrize("platform_name,remedy", [
    ("win32", "Git for Windows from https://git-scm.com/download/win"),
    ("darwin", "xcode-select --install"),
    ("linux", "package manager"),
])
def test_preflight_missing_git_names_the_platform_remedy(tmp_path, monkeypatch, platform_name, remedy):
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("ASTRID_GIT_EXECUTABLE", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="Astrid needs Git") as caught:
        tools.require_git(platform_name=platform_name)
    assert remedy in str(caught.value)
    assert "run the same command again" in str(caught.value)


@pytest.mark.parametrize("failure", ["not Git", PermissionError("cannot run"),
                                      subprocess.TimeoutExpired("git", 60)])
def test_preflight_refuses_an_unusable_executable(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace

    selected = _executable(tmp_path / "bin" / "git")
    monkeypatch.setenv("PATH", str(selected.parent))
    monkeypatch.delenv("ASTRID_GIT_EXECUTABLE", raising=False)

    def run(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return SimpleNamespace(stdout=failure)

    monkeypatch.setattr(tools.subprocess, "run", run)
    with pytest.raises(ValueError, match="Astrid needs Git"):
        tools.require_git(platform_name="linux")


def test_missing_git_blocks_manifest_and_stage_before_downloads(tmp_path, monkeypatch):
    from astrid.installer import backend as backend_module
    from astrid.installer import cli, update_job

    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("ASTRID_GIT_EXECUTABLE", raising=False)
    monkeypatch.setattr(tools, "download", lambda *a, **k: pytest.fail("download before Git check"))
    monkeypatch.setattr(cli, "download", lambda *a, **k: pytest.fail("manifest before Git check"))
    monkeypatch.setattr(backend_module, "source", lambda *a, **k: pytest.fail("source before Git check"))
    backend = backend_module.Backend(tmp_path / "program", tmp_path / "log", tmp_path / "uv")
    backend.uv.touch()
    monkeypatch.setattr(backend, "run", lambda *a, **k: "uv 1.2.3")
    monkeypatch.setattr(backend, "existing", lambda data: None)
    manifest = {"tools": {"uv": {"version": "1.2.3"}}}
    for operation in (
        lambda: cli._download_manifest(backend.root, tmp_path / "manifest.json"),
        lambda: backend.stage(manifest, backend.root, tmp_path / "data"),
        lambda: update_job.fetch_update_inputs(tmp_path / "inputs", manifest_url="https://example.invalid/m",
                    bootstrap_url="https://example.invalid/b", installed_release=12),
    ):
        with pytest.raises(ValueError, match="Astrid needs Git"):
            operation()
    assert not backend.root.exists()
    assert not (tmp_path / "inputs").exists()
