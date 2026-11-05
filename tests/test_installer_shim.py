"""User command exposure through the installer's service and registry seams."""

import json
import os
import subprocess
import sys
import venv
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrid.installer import launcher
from astrid.installer.backend import Backend
from astrid.installer.cli import _print_run_summary
from astrid.installer.core import Installer, Version, read_json, write_json
from astrid.installer.service import LABEL, Service
from tests.test_installer_lifecycle import LifecycleBackend


@pytest.fixture
def registry(monkeypatch):
    values = {}
    writes = []

    class Key:
        def __init__(self, hive):
            self.hive = hive

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    def query(key, name):
        assert name == "Path"
        if key.hive not in values:
            raise FileNotFoundError(name)
        return values[key.hive]

    def set_value(key, name, reserved, kind, value):
        assert key.hive == "user", "the system PATH must never be changed"
        values[key.hive] = value, kind
        writes.append((value, kind))

    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace(
        HKEY_CURRENT_USER="user", HKEY_LOCAL_MACHINE="machine", KEY_READ=1, KEY_WRITE=2,
        REG_SZ=1, REG_EXPAND_SZ=2, OpenKey=lambda hive, path: Key(hive),
        CreateKeyEx=lambda hive, path, *args: Key(hive), QueryValueEx=query, SetValueEx=set_value,
    ))
    return values, writes


@pytest.fixture(params=["linux", "darwin", "win32"])
def manager(request, tmp_path, monkeypatch, registry):
    monkeypatch.setenv("PATH", "")
    root = tmp_path / "Owner's $HOME Astrid"
    root.mkdir()
    service = Service(root, tmp_path / "install.log", Mock(return_value=""),
                      home=tmp_path / "home", system=request.param)
    return service


def test_shim_written_idempotent_and_removed(manager):
    details = {}
    target, hint = launcher.install_shim(manager, details=details)
    contents = target.read_bytes()
    assert launcher.owned_shim(target)
    assert launcher.install_shim(manager) == (target, hint)
    assert target.read_bytes() == contents
    if manager.system == "win32":
        assert b'"%~dp0run-astrid.cmd" %*\r\n' in contents
        assert hint == "Open a new terminal, then run astrid."
        assert manager.run.call_count == 1
        broadcast = manager.run.call_args.args[0][-1]
        assert "SendMessageTimeout" in broadcast and "0xffff, 0x001a" in broadcast
        assert "'Environment'" in broadcast
    else:
        assert target.stat().st_mode & 0o777 == 0o755
        assert hint == launcher.PATH_HINT
    launcher.remove_owned_command(manager, str(target), details.get("path_added"))
    launcher.remove_owned_command(manager, str(target), details.get("path_added"))
    assert not target.exists()


@pytest.mark.parametrize("location", ["target", "earlier-path", "broken-link"])
def test_foreign_command_is_preserved_and_reported_once(manager, monkeypatch, location):
    target = manager.root / "astrid.cmd" if manager.system == "win32" else manager.home / ".local/bin/astrid"
    foreign = target if location != "earlier-path" else manager.home / "other" / ("astrid.exe" if manager.system == "win32" else "astrid")
    foreign.parent.mkdir(parents=True, exist_ok=True)
    if location == "broken-link":
        foreign.symlink_to(foreign.parent / "missing")
    else:
        foreign.write_text("user command")
        foreign.chmod(0o755)
    if location == "earlier-path":
        monkeypatch.setenv("PATH", str(foreign.parent) + (";" if manager.system == "win32" else ":") + str(target.parent))
    lines = []
    assert launcher.install_shim(manager, say=lines.append) == (None, None)
    assert len(lines) == 1 and str(foreign) in lines[0] and "run-astrid" in lines[0]
    launcher.remove_shim(manager)
    assert os.path.lexists(foreign)
    if location == "earlier-path":
        assert not target.exists()


def test_later_path_clash_does_not_hide_the_shim(manager, monkeypatch):
    target = manager.root / "astrid.cmd" if manager.system == "win32" else manager.home / ".local/bin/astrid"
    later = manager.home / "later"
    later.mkdir(parents=True)
    foreign = later / ("astrid.cmd" if manager.system == "win32" else "astrid")
    foreign.write_text("foreign command")
    foreign.chmod(0o755)
    monkeypatch.setenv("PATH", str(target.parent) + (";" if manager.system == "win32" else ":") + str(later))
    assert launcher.install_shim(manager)[0] == target
    assert foreign.read_text() == "foreign command"


def test_installer_bootstrap_entry_point_does_not_block_shim(manager, monkeypatch, tmp_path):
    prefix = tmp_path / "astrid-install-temp" / "venv"
    folder = prefix / ("Scripts" if manager.system == "win32" else "bin")
    folder.mkdir(parents=True)
    command = folder / ("astrid.exe" if manager.system == "win32" else "astrid")
    command.write_text("bootstrap entry point")
    command.chmod(0o755)
    monkeypatch.setattr(launcher.sys, "prefix", str(prefix))
    monkeypatch.setenv("PATH", str(folder))

    shim, _ = launcher.install_shim(manager)

    assert shim is not None and launcher.owned_shim(shim)


@pytest.mark.parametrize("kind", [1, 2])
def test_windows_preexisting_equivalent_path_is_preserved_and_not_claimed(tmp_path, monkeypatch, registry, kind):
    values, writes = registry
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    root = tmp_path / "Astrid"
    root.mkdir()
    service = Service(root, tmp_path / "log", Mock(return_value=""), home=tmp_path, system="win32")
    existing = f'%LOCALAPPDATA%/Astrid;C:\\Tools;{str(root).upper()}/'
    values["user"] = existing, kind
    values["machine"] = r"%SystemRoot%\System32", 2
    monkeypatch.setenv("PATH", "")
    details = {}
    assert launcher.install_shim(service, details=details)[0] == root / "astrid.cmd"
    assert details["path_added"] is None and details["path_present"] is True
    assert values["user"] == (existing, kind)
    assert values["machine"] == (r"%SystemRoot%\System32", 2)
    assert len(writes) == 0
    result = launcher.remove_owned_command(service, str(root / "astrid.cmd"), None)
    assert result == {"shim": True, "path": False}
    assert values["user"] == (existing, kind)
    assert service.run.call_count == 0


def test_windows_owned_path_removal_is_exact_and_preserves_user_edits(tmp_path, monkeypatch, registry):
    values, writes = registry
    root = tmp_path / "Astrid"
    service = Service(root, tmp_path / "log", Mock(return_value=""), home=tmp_path, system="win32")
    monkeypatch.setenv("PATH", "")
    details = {}
    shim, _ = launcher.install_shim(service, details=details)
    owned = str(root)
    assert details["path_added"] == owned
    assert values["user"][0] == owned

    # A user edit changes the owned entry's spelling; off must preserve it.
    edited = str(root).upper()
    values["user"] = f"C:\\Tools;{edited};C:\\Later", values["user"][1]
    result = launcher.remove_owned_command(service, str(shim), owned)
    assert result == {"shim": True, "path": False}
    assert values["user"][0] == f"C:\\Tools;{edited};C:\\Later"
    assert len(writes) == 1


def test_windows_owned_path_removal_preserves_unrelated_entries(tmp_path, monkeypatch, registry):
    values, _writes = registry
    root = tmp_path / "Astrid"
    service = Service(root, tmp_path / "log", Mock(return_value=""), home=tmp_path, system="win32")
    monkeypatch.setenv("PATH", "")
    details = {}
    shim, _ = launcher.install_shim(service, details=details)
    values["user"] = f"C:\\Before;{details['path_added']};C:\\After", values["user"][1]
    result = launcher.remove_owned_command(service, str(shim), details["path_added"])
    assert result == {"shim": True, "path": True}
    assert values["user"][0] == r"C:\Before;C:\After"


def test_windows_detects_registry_path_clash(tmp_path, monkeypatch, registry):
    values, writes = registry
    folder = tmp_path / "machine-bin"
    folder.mkdir()
    (folder / "astrid.exe").write_text("someone else's command")
    values["machine"] = str(folder), 2
    root = tmp_path / "Astrid"
    service = Service(root, tmp_path / "log", Mock(), home=tmp_path, system="win32")
    monkeypatch.setenv("PATH", "")
    assert launcher.install_shim(service, say=lambda _: None) == (None, None)
    assert not root.exists() and not writes


def test_windows_sibling_executable_would_shadow_the_new_cmd(tmp_path, monkeypatch, registry):
    root = tmp_path / "Astrid"
    root.mkdir()
    (root / "astrid.exe").write_text("foreign executable")
    service = Service(root, tmp_path / "log", Mock(), home=tmp_path, system="win32")
    monkeypatch.setenv("PATH", "")
    assert launcher.install_shim(service, say=lambda _: None) == (None, None)
    assert not (root / "astrid.cmd").exists()


def test_upgrade_repoints_posix_shim_and_old_uninstall_keeps_it(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    services = [Service(tmp_path / name, tmp_path / "log", Mock(), home=tmp_path, system="linux")
                for name in ("old", "new")]
    shim, _ = launcher.install_shim(services[0])
    previous = shim.read_text()
    assert launcher.install_shim(services[1])[0] == shim
    assert shim.read_text() != previous
    launcher.remove_shim(services[0])
    assert shim.exists()
    launcher.remove_shim(services[1])
    assert not shim.exists()


@pytest.mark.parametrize("mode", ["install", "update", "repair"])
@pytest.mark.parametrize("system", ["linux", "darwin", "win32"])
def test_verified_lifecycle_records_shim_and_prints_final_hint(tmp_path, monkeypatch, registry, mode, system):
    root, data = tmp_path / "program", tmp_path / "data"
    root.mkdir()
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    backend = LifecycleBackend(root, data)
    lines = []
    backend.service = Service(root, tmp_path / "install.log", Mock(return_value=""), home=tmp_path / "home", system=system)
    shim_path = root / "astrid.cmd" if system == "win32" else backend.service.home / ".local/bin/astrid"
    backend.log, backend.say = tmp_path / "install.log", lines.append
    backend.open = lambda: Backend.open(backend)
    monkeypatch.setattr("webbrowser.open", lambda *_: None)
    monkeypatch.setenv("PATH", "")
    if mode != "install":
        old_root = root / "versions/old"
        python = old_root / "py/bin/python"
        python.parent.mkdir(parents=True)
        python.touch()
        backend.old = Version(old_root, python, old_root / "profile.json", {})
        write_json(old_root / "version.json", backend.old.document())
        if mode == "repair":
            backend.verify = lambda *_args, **_kwargs: None
            backend.restart_required = lambda: False
        shim, _ = launcher.install_shim(backend.service)
        shim.write_text(shim.read_text() + "\n# old content\n")
    Installer(root, data, backend, say=lines.append).run(mode, {})
    record = read_json(root / "install.json")
    if system == "win32":
        assert launcher.owned_shim(Path(record["command_shim"]))
        assert "old content" not in Path(record["command_shim"]).read_text()
    else:
        assert record["command_shim"] == str(shim_path)
    hint = "Open a new terminal, then run astrid." if system == "win32" else launcher.PATH_HINT
    assert lines[-1] == hint and lines.count(hint) == 1
    # The real removal boundary cleans up the shim before deleting programs.
    backend.preflight_remove = lambda *_args, **_kwargs: None
    backend.uv = tmp_path / "uv"
    assert Backend.remove(backend, root, data, delete_data=False) == []
    assert not shim_path.exists()


def test_installer_clash_records_fallback_logs_reason_and_preserves_foreign_cmd(tmp_path, monkeypatch, registry):
    root = tmp_path / "program"
    root.mkdir()
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    backend.service = Service(root, backend.log, Mock(), home=tmp_path, system="win32")
    write_json(root / "install.json", {"owner": LABEL})
    foreign = root / "astrid.cmd"
    foreign.write_text("user command")
    monkeypatch.setattr("webbrowser.open", lambda *_: None)
    monkeypatch.setenv("PATH", "")
    lines = []
    backend.say = lines.append
    backend.open()
    assert read_json(root / "install.json")["command_shim"] is None
    assert len(lines) == 1 and lines[0] in backend.log.read_text()
    monkeypatch.setattr(backend, "preflight_remove", lambda *_args, **_kwargs: None)
    assert backend.remove(root, tmp_path / "data", delete_data=False) == [foreign]
    assert foreign.read_text() == "user command"


def test_open_records_posix_shim_only_when_its_folder_is_on_path(tmp_path, monkeypatch):
    root = tmp_path / "program"
    root.mkdir()
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    backend.service = Service(root, backend.log, Mock(), home=tmp_path / "home", system="linux")
    monkeypatch.setattr("webbrowser.open", lambda *_: None)
    monkeypatch.setenv("PATH", str(backend.service.home / ".local/bin") + ":/usr/bin:/bin")
    backend.open()
    record = read_json(root / "install.json")
    assert record["command_shim"] == str(backend.service.home / ".local/bin/astrid")


@pytest.mark.parametrize("system", ["linux", "win32"])
def test_command_off_removes_only_recorded_artifacts_and_reports_actual_result(
        tmp_path, monkeypatch, registry, system):
    root = tmp_path / "program"
    root.mkdir()
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    backend.service = Service(root, backend.log, Mock(return_value=""), home=tmp_path / "home", system=system)
    backend.service.choices = {"command": True}
    monkeypatch.setattr("webbrowser.open", lambda *_: None)
    monkeypatch.setenv("PATH", "")
    if system == "win32":
        values, _ = registry
        values["user"] = r"C:\Before;C:\After", 2
    backend.open()
    marker = read_json(root / "install.json")
    assert marker["command_shim"]
    if system == "win32":
        assert marker["command_path"] == str(root)
        values["user"] = f"C:\\Before;{marker['command_path']};C:\\After", values["user"][1]
    else:
        assert marker["command_path"] is None

    backend.service.choices = {"command": False}
    backend.open()
    marker = read_json(root / "install.json")
    assert marker["command_shim"] is None and marker["command_path"] is None
    assert backend.command_result["action"] == "disabled"
    assert backend.command_result["removed_shim"] is True
    if system == "win32":
        assert backend.command_result["removed_path"] is True
        assert values["user"][0] == r"C:\Before;C:\After"
    else:
        assert backend.command_result["removed_path"] is False
        assert not (backend.service.home / ".local/bin/astrid").exists()


def test_command_off_removes_exact_legacy_posix_shim_without_receipt(tmp_path, monkeypatch, capsys):
    root = tmp_path / "program"
    root.mkdir()
    home = tmp_path / "home"
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    backend.service = Service(root, backend.log, Mock(), home=home, system="linux")
    backend.service.choices = {"command": False}
    monkeypatch.setenv("PATH", "")
    shim, _ = launcher.install_shim(backend.service)
    write_json(root / "install.json", {"owner": LABEL, "command_shim": None, "command_path": None})
    rc = home / ".bashrc"
    rc.parent.mkdir(parents=True, exist_ok=True)
    rc.write_bytes(b"# user's shell setup  \nexport X=1\n")
    rc_before = rc.read_bytes()
    path_before = "/existing/bin:/usr/bin"
    monkeypatch.setenv("PATH", path_before)
    monkeypatch.setattr("webbrowser.open", lambda *_: None)

    backend.open()

    assert not shim.exists()
    assert backend.command_result["removed_shim"] is True
    assert backend.command_result["preserved_shim"] is False
    _print_run_summary({"command": False}, mode="install", outcomes={"command": backend.command_result})
    assert "Astrid's command shim removed" in capsys.readouterr().out
    assert os.environ["PATH"] == path_before
    assert rc.read_bytes() == rc_before


@pytest.mark.parametrize("kind", ["foreign", "edited", "newline-edited", "other-install"])
def test_command_off_preserves_and_reports_unverified_posix_shim(tmp_path, monkeypatch, kind, capsys):
    root = tmp_path / "program"
    root.mkdir()
    home = tmp_path / "home"
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    backend.service = Service(root, backend.log, Mock(), home=home, system="linux")
    backend.service.choices = {"command": False}
    monkeypatch.setenv("PATH", "")
    if kind == "other-install":
        other_service = Service(
            tmp_path / "other-program", tmp_path / "other.log", Mock(), home=home, system="linux")
        shim, _ = launcher.install_shim(other_service)
    else:
        shim, _ = launcher.install_shim(backend.service)
    if kind == "edited":
        shim.write_text(shim.read_text() + "# user edit\n")
    elif kind == "newline-edited":
        shim.write_bytes(shim.read_bytes().replace(b"\n", b"\r\n"))
    elif kind == "foreign":
        shim.write_text("#!/bin/sh\n# user command\nexit 0\n")
    original = shim.read_bytes()
    write_json(root / "install.json", {"owner": LABEL, "command_shim": None, "command_path": None})
    rc = home / ".bashrc"
    rc.write_bytes(b"export KEEP=1  \n\n")
    rc_before = rc.read_bytes()
    monkeypatch.setenv("PATH", "/existing/bin:/usr/bin")
    monkeypatch.setattr("webbrowser.open", lambda *_: None)

    backend.open()
    _print_run_summary({"command": False}, mode="install", outcomes={"command": backend.command_result})

    summary = capsys.readouterr().out
    assert shim.read_bytes() == original
    assert backend.command_result["removed_shim"] is False
    assert backend.command_result["preserved_shim"] is True
    assert f"preserved at {shim}" in backend.log.read_text()
    assert "preserved because ownership could not be verified" in summary
    assert os.environ["PATH"] == "/existing/bin:/usr/bin"
    assert rc.read_bytes() == rc_before


def test_uninstall_removes_exact_legacy_posix_shim_without_receipt(tmp_path, monkeypatch):
    root, home = tmp_path / "program", tmp_path / "home"
    root.mkdir()
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    backend.service = Service(root, backend.log, Mock(), home=home, system="linux")
    backend.uv = tmp_path / "uv"
    monkeypatch.setenv("PATH", "")
    shim, _ = launcher.install_shim(backend.service)
    write_json(root / "install.json", {"owner": LABEL, "command_shim": None, "command_path": None})
    monkeypatch.setattr(backend, "preflight_remove", lambda *_args, **_kwargs: None)

    residue = backend.remove(root, tmp_path / "data", delete_data=False)

    assert not shim.exists()
    assert not root.exists()
    assert residue == []


def test_uninstall_reports_retained_edited_legacy_posix_shim(tmp_path, monkeypatch):
    root, home = tmp_path / "program", tmp_path / "home"
    root.mkdir()
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    backend.service = Service(root, backend.log, Mock(), home=home, system="linux")
    backend.uv = tmp_path / "uv"
    monkeypatch.setenv("PATH", "")
    shim, _ = launcher.install_shim(backend.service)
    shim.write_text(shim.read_text() + "# user edit\n")
    original = shim.read_bytes()
    write_json(root / "install.json", {"owner": LABEL, "command_shim": None, "command_path": None})
    rc = home / ".bashrc"
    rc.write_bytes(b"export KEEP=1  \n\n")
    rc_before = rc.read_bytes()
    path_before = "/existing/bin:/usr/bin"
    monkeypatch.setenv("PATH", path_before)
    monkeypatch.setattr(backend, "preflight_remove", lambda *_args, **_kwargs: None)

    residue = backend.remove(root, tmp_path / "data", delete_data=False)

    assert shim.read_bytes() == original
    assert residue == [shim]
    assert os.environ["PATH"] == path_before
    assert rc.read_bytes() == rc_before


@pytest.mark.parametrize("system", ["linux", "win32"])
def test_fresh_command_off_creates_no_shim_or_path_change(tmp_path, monkeypatch, registry, system):
    root = tmp_path / "program"
    root.mkdir()
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    backend.service = Service(root, backend.log, Mock(return_value=""), home=tmp_path / "home", system=system)
    backend.service.choices = {"command": False}
    monkeypatch.setattr("webbrowser.open", lambda *_: None)
    monkeypatch.setenv("PATH", "")
    if system == "win32":
        values, _ = registry
        values["user"] = r"C:\Before;C:\After", 2
    backend.open()
    marker = read_json(root / "install.json")
    assert marker["command_shim"] is None and marker["command_path"] is None
    assert backend.command_result["action"] == "disabled"
    if system == "win32":
        assert values["user"] == (r"C:\Before;C:\After", 2)
        assert not (root / "astrid.cmd").exists()
    else:
        assert not (backend.service.home / ".local/bin/astrid").exists()


def test_backend_does_not_claim_a_preexisting_windows_path_entry(tmp_path, monkeypatch, registry):
    root = tmp_path / "program"
    root.mkdir()
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    backend.service = Service(root, backend.log, Mock(return_value=""), home=tmp_path, system="win32")
    backend.service.choices = {"command": True}
    values, writes = registry
    existing = f"C:\\Tools;{str(root).upper()}/;C:\\Other"
    values["user"] = existing, 2
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("webbrowser.open", lambda *_: None)

    backend.open()

    marker = read_json(root / "install.json")
    assert marker["command_shim"] == str(root / "astrid.cmd")
    assert marker["command_path"] is None
    assert values["user"] == (existing, 2) and writes == []

    # The equivalent path pre-dated this install. Its later removal is still
    # the user's choice; repair must not recreate it.
    values["user"] = r"C:\Tools;C:\Other", 2
    backend.open()
    assert values["user"] == (r"C:\Tools;C:\Other", 2)
    assert backend.command_result["path_added"] is None
    assert backend.command_result["path_present"] is False
    assert "Add " + str(root) in backend.command_result["path_hint"]


def test_repair_does_not_restore_a_removed_owned_windows_path_entry(tmp_path, monkeypatch, registry):
    root = tmp_path / "program"
    root.mkdir()
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    backend.service = Service(root, backend.log, Mock(return_value=""), home=tmp_path, system="win32")
    backend.service.choices = {"command": True}
    values, _ = registry
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("webbrowser.open", lambda *_: None)

    backend.open()
    owned = read_json(root / "install.json")["command_path"]
    assert owned == str(root) and values["user"][0] == owned
    values["user"] = r"C:\Tools", values["user"][1]

    backend.open()
    assert values["user"][0] == r"C:\Tools"
    assert read_json(root / "install.json")["command_path"] == owned
    assert backend.command_result["path_added"] is None
    assert backend.command_result["path_present"] is False
    assert "Add " + owned in backend.command_result["path_hint"]


def test_open_shim_failure_is_logged_and_does_not_fail_install(tmp_path, monkeypatch):
    root = tmp_path / "program"
    root.mkdir()
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    backend.service = Service(root, backend.log, Mock(), home=tmp_path / "home", system="linux")
    monkeypatch.setattr("astrid.installer.backend.install_shim", Mock(side_effect=PermissionError()))
    monkeypatch.setattr("webbrowser.open", lambda *_: None)
    backend.open()
    assert read_json(root / "install.json")["command_shim"] is None
    assert "command shim setup failed (PermissionError)" in backend.log.read_text()


@pytest.mark.posix_native
def test_scratch_install_runs_real_astrid_help_and_forwards_arguments(tmp_path, monkeypatch):
    root = tmp_path / "Owner's $HOME Astrid"
    version_root = root / "versions/v1"
    environment = version_root / "py"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / "bin/python"
    purelib = subprocess.check_output([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip()
    (Path(purelib) / "astrid.pth").write_text(str(Path(__file__).resolve().parents[1]) + "\n")
    write_json(version_root / "version.json", Version(version_root, python, version_root / "profile.json", {}).document())
    (root / "current").write_text("v1\n")
    launcher.install(root)
    home = tmp_path / "home"
    shim_dir = home / ".local/bin"
    monkeypatch.setenv("PATH", f"{shim_dir}:/usr/bin:/bin")
    service = Service(root, tmp_path / "log", Mock(), home=home, system="linux")
    shim, hint = launcher.install_shim(service)
    assert hint is None
    result = subprocess.run(["/usr/bin/env", "-i", f"HOME={home}", f"PATH={shim_dir}:/usr/bin:/bin", "astrid", "--help"],
                            cwd=tmp_path, capture_output=True, text=True, check=True)
    assert "usage: astrid" in result.stdout and "interactive Astrid agent" in result.stdout
    # Exercise spaces, quotes and empty arguments through the POSIX exec.
    (root / "run-astrid").write_text(f'#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
    result = subprocess.check_output([str(shim), "a b", "'quoted'", "", "$HOME"], text=True)
    assert json.loads(result) == ["a b", "'quoted'", "", "$HOME"]
