"""Pinokio's foreground launch contract across the supported platforms."""

from __future__ import annotations

import ast
import os
import runpy
import sys
from pathlib import Path
from types import ModuleType

import pytest

from astrid.installer import foreground, launcher, service
from astrid.installer.core import Version, current, switch, write_json


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for key, value in {"HOME": home, "USERPROFILE": home,
                       "LOCALAPPDATA": home / "local", "XDG_DATA_HOME": home / "data"}.items():
        monkeypatch.setenv(key, str(value))


def _runtime_args(command):
    source = command[command.index("-c") + 1]
    return ast.literal_eval(ast.parse(source).body[-1].value.args[0].args[0])


def _installation(tmp_path: Path, name: str = "v1") -> tuple[Path, Path, Version]:
    root = tmp_path / "Astrid app"
    data = tmp_path / "Astrid data"
    version_root = root / "versions" / name
    python = version_root / "py" / "bin" / "python"
    profile = version_root / "profile.json"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    profile.write_text("{}\n", encoding="utf-8")
    version = Version(version_root, python, profile, {"ASTRID_VERSION_MARKER": name},
                      release=52, managed_by="pinokio")
    write_json(version_root / "version.json", version.document())
    write_json(root / "install.json", {
        "owner": "haus.astrid.runtime", "managed_by": "pinokio", "data_root": str(data)
    })
    (root / "current").write_text(name + "\n", encoding="utf-8")
    return root, data, version


def _clean_environment(version: Version) -> dict[str, str]:
    excluded = {
        "PYTHONPATH",
        "PYTHONHOME",
        "ASTRID_WORKSPACE_ENDPOINT",
        "ASTRID_WORKSPACE_CREDENTIAL",
        "ASTRID_RUNTIME_SOURCE_MANIFEST",
        "ASTRID_RUNTIME_DATA_ROOT",
        "ASTRID_PACKS_PATH",
        "ASTRID_INSTALLER_OPERATION_ID",
    }
    env = {key: value for key, value in os.environ.items() if key not in excluded}
    env.update(version.env)
    env["ASTRID_INSTALL_MANAGED_BY"] = "pinokio"
    return env


@pytest.mark.parametrize("system", ["darwin", "linux", "win32"])
def test_launch_uses_current_version_service_command_and_pinokio_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: str
) -> None:
    root, data, version = _installation(tmp_path)
    settings = {"ASTRID_WARM_IDLE_SECONDS": "17", "ASTRID_ALLOW_UNQUALIFIED_GPU": "1"}
    write_json(root / "service-env.json", settings)
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "ASTRID_WORKSPACE_ENDPOINT",
        "ASTRID_WORKSPACE_CREDENTIAL",
        "ASTRID_RUNTIME_SOURCE_MANIFEST",
        "ASTRID_RUNTIME_DATA_ROOT",
        "ASTRID_PACKS_PATH",
    ):
        monkeypatch.setenv(key, "ambient-value")
    monkeypatch.setenv("ASTRID_INSTALL_MANAGED_BY", "another-manager")

    seen: dict[str, object] = {}
    lock_file = foreground.platform.lock_file

    def acquire(fd, **kwargs):
        seen["lock_fd"] = fd
        lock_file(fd, **kwargs)

    monkeypatch.setattr(foreground.platform, "lock_file", acquire)

    class ExecCalled(Exception):
        pass

    def execvpe(file: str, argv: list[str], env: dict[str, str]) -> None:
        seen.update(file=file, argv=argv, env=env)
        assert os.get_inheritable(seen["lock_fd"])
        raise ExecCalled

    monkeypatch.setattr(foreground.os, "execvpe", execvpe)
    extra_args = ["--hosted-origin", "off", "--label", "a name with spaces"]
    if system == "win32":
        windows_python = version.root / "py" / "Scripts" / "python.exe"
        windows_python.parent.mkdir(parents=True)
        windows_python.write_text("fake console Python", encoding="utf-8")
        version = Version(version.root, windows_python, version.profile, version.env,
                          version.release, version.managed_by)
        write_json(version.root / "version.json", version.document())
        expected = service.command(
            version,
            data,
            # Windows must use console python.exe so Ctrl+C and console
            # control events remain with the foreground Runtime owner.
            windows=False,
            env_file=root / "service-env.json",
            extra_args=extra_args,
        )
        windows_env = dict(os.environ)
        monkeypatch.setattr(os, "environ", windows_env)
        monkeypatch.setattr(os, "chdir", lambda _path: None)
        # The Windows launcher evaluates Runtime's foreground command in this
        # process so its installed signal handlers remain attached to the owner.
        calls: list[tuple[list[str], int]] = []
        package = ModuleType("astrid_runtime")
        package.__path__ = []  # type: ignore[attr-defined]
        runtime_cli = ModuleType("astrid_runtime.cli")

        def main(argv: list[str]) -> int:
            calls.append((list(argv), os.getpid()))
            return 0

        runtime_cli.main = main  # type: ignore[attr-defined]
        package.cli = runtime_cli  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "astrid_runtime", package)
        monkeypatch.setitem(sys.modules, "astrid_runtime.cli", runtime_cli)

        with pytest.raises(SystemExit) as stopped:
            foreground.launch(root, extra_args, system=system)
        assert stopped.value.code == 0
        assert len(calls) == 1
        assert calls[0][0][-len(extra_args):] == extra_args
        assert calls[0][1] == os.getpid()
        assert expected[0] == str(windows_python)
        assert Path(expected[0]).name.lower() == "python.exe"
        assert expected[1:5] == ["-X", "utf8", "-I", "-c"]
        assert _runtime_args(expected)[-len(extra_args):] == extra_args
        task_command = service.command(version, data, windows=True,
                                       log=root / "runtime-service.log",
                                       env_file=root / "service-env.json", extra_args=extra_args)
        assert _runtime_args(task_command) == _runtime_args(expected)
        assert Path(task_command[0]).name == "pythonw.exe"
        assert str(root / "service-env.json") in expected[-1]
        assert windows_env["ASTRID_INSTALL_MANAGED_BY"] == "pinokio"
        assert windows_env["ASTRID_INSTALL_RELEASE"] == "52"
        assert windows_env["ASTRID_VERSION_MARKER"] == "v1"
        assert "PYTHONPATH" not in windows_env
        assert windows_env["ASTRID_WARM_IDLE_SECONDS"] == "17"
        return

    with pytest.raises(ExecCalled):
        foreground.launch(root, extra_args, system=system)

    selected = current(root)
    assert selected is not None
    expected = service.command(
        selected,
        data,
        windows=False,
        env_file=root / "service-env.json",
        extra_args=extra_args,
        lock_fd=seen["lock_fd"],
    )
    assert seen["file"] == expected[0]
    assert seen["argv"] == expected
    assert seen["env"] == _clean_environment(selected)
    assert seen["env"]["ASTRID_INSTALL_MANAGED_BY"] == "pinokio"  # type: ignore[index]
    assert seen["env"]["ASTRID_INSTALL_RELEASE"] == "52"  # type: ignore[index]
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "ASTRID_WORKSPACE_ENDPOINT",
        "ASTRID_WORKSPACE_CREDENTIAL",
        "ASTRID_RUNTIME_SOURCE_MANIFEST",
        "ASTRID_RUNTIME_DATA_ROOT",
        "ASTRID_PACKS_PATH",
        "ASTRID_INSTALLER_OPERATION_ID",
    ):
        assert key not in seen["env"]  # type: ignore[operator]
    # The settings file is consumed by the embedded service command itself.
    script = expected[expected.index("-c") + 1]
    assert str(root / "service-env.json") in script
    assert "ASTRID_WARM_IDLE_SECONDS" in script
    assert _runtime_args(expected)[-len(extra_args):] == extra_args


def _second_version(root: Path, name: str = "candidate") -> Version:
    folder = root / "versions" / name
    python = folder / "py" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    profile = folder / "profile.json"
    profile.write_text("{}\n", encoding="utf-8")
    result = Version(folder, python, profile, {"ASTRID_VERSION_MARKER": name},
                    release=53, managed_by="pinokio")
    write_json(folder / "version.json", result.document())
    return result


def test_foreground_binds_migration_journal_to_exact_retained_session(tmp_path, monkeypatch):
    root, data, old = _installation(tmp_path)
    candidate = _second_version(root)
    switch(root, candidate)
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "1" * 32,
        "transaction_kind": "migration", "step": "switched", "data_root": str(data),
        "old": old.document(), "candidate": candidate.document(),
        "realm": {"realm_id": "test-realm", "realm_root": str(data / "realm")},
    })

    class ExecCalled(Exception):
        pass

    monkeypatch.setenv("ASTRID_INSTALLER_OPERATION_ID", "not-used")
    monkeypatch.setattr(foreground.os, "execvpe", lambda *_args: (_ for _ in ()).throw(ExecCalled()))
    with (tmp_path / "installer.lock").open("a+b") as lock:
        with pytest.raises(ExecCalled):
            foreground._run(root, [], "linux", lock.fileno())


def test_foreground_refuses_unmatched_transaction_binding_before_launch(tmp_path, monkeypatch):
    root, data, old = _installation(tmp_path)
    candidate = _second_version(root)
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "1" * 32,
        "transaction_kind": "migration", "step": "migrated", "data_root": str(data),
        "old": old.document(), "candidate": candidate.document(),
    })

    with pytest.raises(ValueError, match="interrupted|Update"):
        foreground._run(root, [], "linux", 123)


@pytest.mark.parametrize(("step", "selected", "allowed", "data_matches"), [
    ("switched", "candidate", True, True),
    ("switched", "old", False, True),
    ("rollback_selected", "old", True, True),
    ("rollback_selected", "candidate", False, True),
    ("migrated", "old", False, True),
    ("future-step", "old", False, True),
    ("switched", "candidate", False, False),
    ("complete", "old", True, True),
    ("rolled_back", "old", True, True),
    (None, "old", True, True),
])
def test_generated_foreground_retains_exact_admission_across_rollback(
        tmp_path, monkeypatch, step, selected, allowed, data_matches):
    root, data, old = _installation(tmp_path)
    candidate = _second_version(root)
    chosen = candidate if selected == "candidate" else old
    if selected == "candidate":
        switch(root, candidate)
    if step is not None:
        write_json(root / "update-transaction.json", {
            "format": "astrid-install-update/v1", "operation_id": "a" * 32,
            "transaction_kind": "migration", "step": step,
            "data_root": str(data if data_matches else data / "other"),
            "old": old.document(), "candidate": candidate.document(),
            "realm": {"realm_id": "test-realm", "realm_root": str(data / "realm")},
        })
    launcher.install(root)

    # The generated entry carries the admission prelude even when the selected
    # retained package predates the current service builder.
    monkeypatch.setattr(foreground, "launch", lambda *_args, **_kwargs: pytest.fail("old foreground imported"))
    monkeypatch.setattr(sys, "argv", [str(root / "run_runtime.py"), "--hosted-origin", "off"])
    observed = []

    class ExecCalled(Exception):
        pass

    def capture(_file, arguments, environment):
        observed.append((arguments, environment))
        raise ExecCalled

    monkeypatch.setattr(os, "execvpe", capture)
    if allowed:
        with pytest.raises(ExecCalled):
            runpy.run_path(str(root / "run_runtime.py"))
        arguments, environment = observed[0]
        assert arguments[0] == str(chosen.python)
        assert _runtime_args(arguments) == [
            "run", "--profile", "astrid", "--data-root", str(data),
            "--source-manifest", str(chosen.profile), "--hosted-origin", "off",
        ]
        assert "ASTRID_INSTALLER_OPERATION_ID" not in environment
    else:
        with pytest.raises(ValueError, match="interrupted|Update"):
            runpy.run_path(str(root / "run_runtime.py"))
        assert observed == []


def test_generated_foreground_refuses_malformed_pending_journal(tmp_path, monkeypatch):
    root, _data, _old = _installation(tmp_path)
    candidate = _second_version(root)
    (root / "current").write_text(candidate.root.name + "\n", encoding="utf-8")
    (root / "update-transaction.json").write_text("{broken", encoding="utf-8")
    launcher.install(root)
    monkeypatch.setattr(sys, "argv", [str(root / "run_runtime.py")])
    monkeypatch.setattr(os, "execvpe", lambda *_args: pytest.fail("malformed journal launched Runtime"))
    with pytest.raises(ValueError, match="interrupted|Update"):
        runpy.run_path(str(root / "run_runtime.py"))


def test_launch_resolves_the_selected_version_each_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, data, first = _installation(tmp_path, "v1")
    second_root = root / "versions" / "v2"
    second_python = second_root / "py" / "bin" / "python"
    second_python.parent.mkdir(parents=True)
    second_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    second_python.chmod(0o755)
    second = Version(second_root, second_python, second_root / "profile.json", {"ASTRID_VERSION_MARKER": "v2"}, managed_by="pinokio")
    write_json(second_root / "version.json", second.document())
    calls: list[list[str]] = []

    class ExecCalled(Exception):
        pass

    def execvpe(_file: str, argv: list[str], _env: dict[str, str]) -> None:
        calls.append(argv)
        raise ExecCalled

    monkeypatch.setattr(foreground.os, "execvpe", execvpe)
    with pytest.raises(ExecCalled):
        foreground.launch(root, ["first"], system="linux")
    (root / "current").write_text("v2\n", encoding="utf-8")
    with pytest.raises(ExecCalled):
        foreground.launch(root, ["second"], system="linux")

    assert calls[0][0] == str(first.python)
    assert calls[1][0] == str(second.python)
    assert _runtime_args(calls[0])[-1] == "first"
    assert _runtime_args(calls[1])[-1] == "second"
    assert current(root) == second


def test_run_renderings_keep_arguments_and_windows_console_owner(tmp_path):
    root, _data, _version = _installation(tmp_path)
    launcher.install(root)
    posix = (root / "run").read_text()
    windows = (root / "run.cmd").read_text()
    assert 'exec "$root/versions/$version/py/bin/python" -X utf8 -I "$root/run_runtime.py" "$@"' in posix
    assert os.access(root / "run", os.X_OK)
    assert '"%ASTRID_PROGRAM_ROOT%versions\\%ASTRID_VERSION%\\py\\Scripts\\python.exe" -X utf8 -I "%ASTRID_PROGRAM_ROOT%run_runtime.py" %*' in windows
    assert 'exit /b %errorlevel%' in windows
    assert 'pythonw' not in windows and 'start ' not in windows.lower()


def test_publish_runtime_runner_fsyncs_before_atomic_replace_and_preserves_wrappers(tmp_path, monkeypatch):
    from runtime_protocol import platform

    root, _data, _version = _installation(tmp_path)
    launcher.install(root)
    wrappers = {path: path.read_bytes() for path in root.iterdir() if path.is_file()}
    runner = root / "run_runtime.py"
    runner.write_text("# obsolete admission\n")
    events = []
    fsync, replace = os.fsync, platform.durable_replace

    def flushed(fd):
        events.append("fsync")
        fsync(fd)

    def publish(source, target):
        assert events == ["fsync"]
        assert source.parent == root
        assert target == runner
        assert runner.read_text() == "# obsolete admission\n"
        compile(source.read_text(), str(source), "exec")
        replace(source, target)
        events.append("replaced")

    monkeypatch.setattr(os, "fsync", flushed)
    monkeypatch.setattr(platform, "durable_replace", publish)
    assert launcher.publish_runtime_runner(root) == runner
    assert events[-1] == "replaced"
    assert runner.read_bytes() == wrappers.pop(runner)
    assert all(path.read_bytes() == content for path, content in wrappers.items())
    assert list(root.glob(".run_runtime.py.*")) == []
