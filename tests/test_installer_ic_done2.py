"""The four frozen DONE-2 contracts, exercised through the installer boundary."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from runtime_protocol import platform

from astrid.installer import cli, local_update
from astrid.installer.backend import Backend
from astrid.installer.core import Installer, Version, current, read_json, write_json
from astrid.installer.service import Service, install_selection
from astrid.skills import cli as skills_cli
from tests.installer_skill_fixtures import skill_installation


@pytest.fixture
def installation(tmp_path, monkeypatch):
    """Use real selection, service, shim, and skill code with external edges recorded."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    for key in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(key, str(home))
    for key in ({name for name in os.environ if name.startswith("XDG_")} |
                {"XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
                 "XDG_RUNTIME_DIR", "APPDATA", "LOCALAPPDATA"}):
        monkeypatch.setenv(key, str(tmp_path / key.lower()))
    for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "HERMES_HOME"):
        monkeypatch.delenv(key, raising=False)
    for harness in ("claude", "codex", "hermes"):
        (home / ("." + harness)).mkdir(exist_ok=True)
    bindir = home / ".local/bin"
    monkeypatch.setenv("PATH", str(bindir))
    root, data = tmp_path / "program", tmp_path / "workspace"
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "data_root", lambda: data)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr("webbrowser.open", Mock(return_value=True))
    network = Mock(side_effect=AssertionError("unexpected network"))
    monkeypatch.setattr(cli, "download", network)
    monkeypatch.setattr(urllib.request, "urlopen", network)
    monkeypatch.setattr(urllib.request, "build_opener", network)
    launches = Mock(side_effect=AssertionError("unexpected subprocess launch"))
    monkeypatch.setattr(subprocess, "run", launches)
    monkeypatch.setattr(subprocess, "Popen", launches)
    spawn = Mock(return_value=SimpleNamespace(pid=12345))
    monkeypatch.setattr(platform, "spawn_tree", spawn)
    monkeypatch.setattr(platform, "process_identity", lambda _: {"birth_id": "test"})
    monkeypatch.setattr(cli, "validate_manifest", lambda manifest: manifest)
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {"release": 1})
    calls = []

    def run(self, argv, **kwargs):
        calls.append(tuple(map(str, argv)))
        if "astrid.skills" in argv:
            result = io.StringIO()
            with contextlib.redirect_stdout(result):
                assert skills_cli.main([str(arg) for arg in argv[argv.index("astrid.skills") + 1:]]) == 0
            return result.getvalue()
        if "-c" in argv and "inspect_canonical_schema" in str(argv[argv.index("-c") + 1]):
            return json.dumps({"identity": {"version": 1}, "target": 1})
        return ""

    monkeypatch.setattr(Backend, "run", run)
    monkeypatch.setattr(Backend, "preflight_stop", lambda *_: None)
    monkeypatch.setattr(Backend, "source_checkout_ownership", lambda *_: False)
    monkeypatch.setattr(Backend, "stop", lambda *_: None)
    monkeypatch.setattr(Backend, "verify", lambda *_, **__: None)
    monkeypatch.setattr(Backend, "validate_candidate", lambda *_: None)
    monkeypatch.setattr(Backend, "publish_launch_admission", lambda *_: None)
    monkeypatch.setattr(Backend, "repair_chat_command", lambda *_: False)
    realm = {"realm_id": "selected", "realm_root": str(data / "realm")}
    monkeypatch.setattr(Backend, "workspace", lambda *_: realm)
    monkeypatch.setattr(Backend, "create", lambda *_: realm)
    counter = iter(range(100))

    def stage(self, manifest, root, data):
        folder = root / "versions" / f"v{next(counter)}"
        folder.mkdir(parents=True)
        python = folder / "python"
        python.touch()
        profile = folder / "profile.json"
        write_json(profile, {})
        version = Version(folder, python, profile, {}, release=manifest["release"])
        write_json(folder / "version.json", version.document())
        return version

    monkeypatch.setattr(Backend, "stage", stage)
    # Explicit Linux manager keeps service files and calls inside the test tree.
    original_init = Backend.__init__
    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.service = Service(self.root, self.log, self.run, home=Path.home(), system="linux", say=self.say)
    monkeypatch.setattr(Backend, "__init__", init)
    targets = {name: home / f".{name}/skills/astrid" for name in ("claude", "codex", "hermes")}
    args = ["--root", str(root), "--data-root", str(data), "--manifest", str(manifest)]
    return SimpleNamespace(root=root, data=data, home=home, bindir=bindir, targets=targets,
                           args=args, calls=calls, network=network, manifest=manifest,
                           launches=launches, spawn=spawn, initial_path=str(bindir), tmp=tmp_path)


def _assert_absent(env, choice):
    if choice == "autostart":
        assert not (Path(os.environ["XDG_CONFIG_HOME"]) / "systemd/user/astrid.service").exists()
        assert not list(Path(os.environ["XDG_CONFIG_HOME"]).glob("systemd/user/*.wants/astrid.service"))
        assert not (env.home / "Library/LaunchAgents/haus.astrid.runtime.plist").exists()
        assert not (env.root / "task.xml").exists()
        assert not (Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/Astrid.lnk").exists()
    elif choice == "command":
        assert not (env.bindir / "astrid").exists()
        assert not (env.root / "astrid.cmd").exists()
        assert read_json(env.root / "install.json").get("command_path") is None
        assert os.environ["PATH"] == env.initial_path
        for name in (".profile", ".bashrc", ".bash_profile", ".zshrc", ".zprofile"):
            path = env.home / name
            assert not path.exists() or "astrid" not in path.read_text().lower()
    else:
        for target in env.targets.values():
            assert not target.exists() and not target.is_symlink()


def test_done2_unattended_fresh_install_without_yes_prints_plan_and_stops(installation, monkeypatch, capsys):
    env = installation
    register = Mock(side_effect=AssertionError("registration before consent"))
    shim = Mock(side_effect=AssertionError("shim before consent"))
    skills = Mock(side_effect=AssertionError("skills before consent"))
    monkeypatch.setattr(Service, "register", register)
    monkeypatch.setattr("astrid.installer.backend.install_shim", shim)
    monkeypatch.setattr(Backend, "sync_skills", skills)
    assert cli.main(env.args) == 2
    output = capsys.readouterr().out
    assert "Astrid install plan" in output
    for choice in ("autostart", "command", "skills"):
        assert f"{choice}: on" in output
        _assert_absent(env, choice)
    register.assert_not_called()
    shim.assert_not_called()
    skills.assert_not_called()
    env.network.assert_not_called()
    env.launches.assert_not_called()
    env.spawn.assert_not_called()
    assert env.calls == []


def test_done2_off_means_absent(installation):
    env = installation
    assert cli.main(env.args + ["--yes", "--autostart", "off", "--command", "off", "--skills", "off"]) == 0
    for choice in ("autostart", "command", "skills"):
        _assert_absent(env, choice)
    env.network.assert_not_called()


@pytest.mark.parametrize("choice", ["autostart", "command", "skills"])
def test_done2_choice_does_not_return_after_repair_update_reinstall(installation, monkeypatch, choice):
    env = installation
    def assert_saved():
        assert read_json(install_selection(env.home, "linux"))["choices"][choice] is False

    assert cli.main(env.args + ["--yes", f"--{choice}", "off"]) == 0
    assert_saved()
    _assert_absent(env, choice)
    assert cli.main(env.args + ["--yes"]) == 0  # repair
    assert_saved()
    _assert_absent(env, choice)
    write_json(env.manifest, {"release": 2})
    assert cli.main(env.args + ["--yes", "--update"]) == 0
    assert_saved()
    _assert_absent(env, choice)
    backend = local_update._backend(env.root)
    monkeypatch.setattr(local_update, "_verify_selected_identity", lambda *_: None)
    monkeypatch.setattr(local_update, "validate_manifest", lambda manifest: manifest)
    local_update.run_update(env.root, backend, {"release": 2},
                            {"targetSha": "a" * 40, "launcherChanged": False},
                            SimpleNamespace(call=lambda *a, **kw: None))
    assert_saved()
    _assert_absent(env, choice)
    # Simulate removed program selection with the retained workspace intact.
    (env.root / "current").unlink()
    # Removing the program also removes its sign-in unit. With no current
    # version, a surviving unit is classified as an R0 service by the handoff gate.
    (Path(os.environ["XDG_CONFIG_HOME"]) / "systemd/user/astrid.service").unlink(missing_ok=True)
    write_json(env.data / "runtime/catalog.json", {"selected_realm_id": "selected"})
    attach = Mock(return_value={"realm_id": "selected", "realm_root": str(env.data / "realm")})
    monkeypatch.setattr(Backend, "attach_existing", attach)
    assert cli.main(env.args + ["--yes"]) == 0  # reinstall into kept workspace
    _assert_absent(env, choice)
    attach.assert_called_once()
    assert_saved()
    # Uninstall retains the selection as a receipt for the kept workspace.
    shutil.rmtree(env.root)
    (Path(os.environ["XDG_CONFIG_HOME"]) / "systemd/user/astrid.service").unlink(missing_ok=True)  # uninstall removes the unit
    selection = install_selection(env.home, "linux")
    write_json(selection, {**read_json(selection), "uninstalled": True})
    assert cli.main(env.args + ["--yes"]) == 0
    _assert_absent(env, choice)
    assert_saved()
    assert not read_json(selection).get("uninstalled")
    env.network.assert_not_called()


@pytest.mark.parametrize("all_off", [False, True])
def test_done2_end_summary_matches_reality(installation, monkeypatch, capsys, all_off):
    env = installation
    flags = ["--autostart", "off", "--command", "off", "--skills", "off"] if all_off else []
    assert cli.main(env.args + ["--yes", *flags]) == 0
    terminal = capsys.readouterr().out
    assert "Astrid setup summary:" not in terminal  # the summary is in install.log (card 2)
    output = (env.tmp / "logs" / "install.log").read_text(encoding="utf-8").split("Astrid setup summary:")[-1]
    if all_off:
        for line in ("Your choice: start at sign-in is off.", "Your choice: the astrid command is off.",
                     "Your choice: Astrid skills are off."):
            assert line in terminal
    else:
        assert "Your choice:" not in terminal
    registered = (Path(os.environ["XDG_CONFIG_HOME"]) / "systemd/user/astrid.service").exists()
    assert registered is not all_off
    assert f"Sign-in autostart: {'registered' if registered else 'not registered'}." in output
    assert "Startup: running and ready." in output
    shim = env.bindir / "astrid"
    assert shim.exists() is not all_off
    if all_off:
        assert "Command/PATH: off" in output
        for choice in ("autostart", "command", "skills"):
            _assert_absent(env, choice)
        assert "Skill destinations: no target changes were reported." in output
    else:
        assert f"shim created at {shim}" in output
        for target in env.targets.values():
            assert target.is_dir()
            assert str(target) in output
        # Compare against the shared fixture's known copied installs, isolated
        # from the fresh installation; its subprocess boundary uses run above.
        with monkeypatch.context() as reference_patch:
            reference_root = env.tmp / "reference"
            reference_root.mkdir()
            reference_home = reference_root / "home"
            reference_home.mkdir()
            reference_patch.setenv("HOME", str(reference_home))
            for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
                reference_patch.setenv(name, str(reference_root / name.lower()))
            reference = skill_installation(reference_root, reference_patch)
            assert set(reference.targets) == set(env.targets)
            assert all(path.is_dir() for path in reference.targets.values())
    assert "Way back:" in output
