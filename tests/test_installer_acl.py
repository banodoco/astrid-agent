"""Installer admission and lock placement through the Windows permission seam."""

import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from runtime_protocol import platform, windows_security

from astrid.installer import cli
from astrid.installer.backend import Backend
from astrid.installer.core import write_json
from astrid.installer.service import LABEL, default_program_root


@pytest.fixture(name="tmp_path")
def compact_installer_path():
    # Pytest/xdist path nesting must not trip the real Windows root-length guard.
    with TemporaryDirectory(prefix="ia-") as directory:
        yield Path(directory)


@pytest.fixture
def windows_move(monkeypatch):
    # Keep durable_replace and its Windows retry/flush path; fake only Win32.
    move = Mock(side_effect=os.replace)
    monkeypatch.setattr(platform, "_move_file_write_through", move)
    return move


@pytest.mark.parametrize("custom_root", [False, True])
def test_windows_installer_lock_stays_outside_program_and_workspace(tmp_path, monkeypatch, custom_root, windows_move):
    local = tmp_path / "local"
    log_dir = local / "Astrid Runtime" / "logs"
    root = tmp_path / "custom" if custom_root else local / "Astrid"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(platform, "log_dir", lambda: log_dir)
    admitted = []

    def private_directory(path):
        path = Path(path)
        assert path != local, "the installer must never change LOCALAPPDATA's ACL"
        admitted.append(path)
        path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(windows_security, "private_directory", private_directory)
    backend = Mock()
    backend.service.recorded.return_value = {}
    monkeypatch.setattr(cli, "Backend", Mock(return_value=backend))
    data = platform.data_root()
    monkeypatch.setattr(cli, "current", lambda _: SimpleNamespace(python=Path(__file__)))
    installer = Mock()
    monkeypatch.setattr(cli, "Installer", Mock(return_value=installer))
    locks = []
    monkeypatch.setattr(platform, "lock_file", lambda fd, **_: locks.append(fd))
    unlock = Mock()
    monkeypatch.setattr(platform, "unlock_file", unlock)

    args = ["--data-root", str(data)]
    if custom_root:
        args.extend(["--root", str(root)])
    assert cli.main(args) == 0
    lock = local / "Astrid Installer" / "astrid-install.lock"
    assert admitted == [log_dir, lock.parent, root]
    assert lock.is_file()
    assert root not in lock.parents
    assert data not in lock.parents
    assert not (local / "astrid-install.lock").exists()
    assert len(locks) == 1
    unlock.assert_called_once_with(locks[0])
    installer.run.assert_called_once()
    windows_move.assert_any_call(root / "install.json.tmp", root / "install.json")


@pytest.mark.parametrize("directory", ["program", "state"])
def test_windows_installer_does_not_bypass_existing_directory_validation(tmp_path, monkeypatch, directory):
    log_dir = tmp_path / "logs"
    root = tmp_path / "program"
    root.mkdir()
    local = tmp_path / "local"
    state = local / "Astrid Installer"
    state.mkdir(parents=True)
    unsafe = root if directory == "program" else state
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(platform, "log_dir", lambda: log_dir)

    def private_directory(path):
        if Path(path) == unsafe:
            raise PermissionError("unprotected existing DACL")
        Path(path).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(windows_security, "private_directory", private_directory)
    backend = Mock()
    backend.service.recorded.return_value = {}
    monkeypatch.setattr(cli, "Backend", Mock(return_value=backend))
    monkeypatch.setattr(platform, "lock_file", Mock())
    monkeypatch.setattr(platform, "unlock_file", Mock())
    installer = Mock()
    monkeypatch.setattr(cli, "Installer", installer)

    assert cli.main(["--root", str(root), "--data-root", str(tmp_path / "workspace")]) == 1
    installer.assert_not_called()
    assert not (root / "install.json").exists()


def test_windows_uninstall_deletes_default_workspace_while_lock_is_held(tmp_path, monkeypatch, capsys, windows_move):
    local = tmp_path / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="win32"))
    root, data = local / "Astrid", platform.data_root()
    lock_path = local / "Astrid Installer" / "astrid-install.lock"
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    (root / "owned").mkdir()
    data.mkdir()
    (data / "sentinel").write_text("workspace data")

    def private_directory(path):
        assert Path(path) != local
        Path(path).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(windows_security, "private_directory", private_directory)
    backend = Backend(root, platform.log_dir() / "install.log", local / "Astrid Installer/bootstrap/uv.exe")
    backend.service = SimpleNamespace(root=root, recorded=lambda: {}, selection=local / "astrid-install.json", system="win32", say=print,
                                     home=Path.home())
    monkeypatch.setattr("astrid.installer.launcher._update_user_path", Mock())
    monkeypatch.setattr(backend, "existing", Mock(return_value=None))
    monkeypatch.setattr(backend, "stop", Mock())
    monkeypatch.setattr(backend, "unregister", Mock())
    monkeypatch.setattr(cli, "Backend", Mock(return_value=backend))
    held = set()
    monkeypatch.setattr(platform, "lock_file", lambda fd, **_: held.add(fd))
    monkeypatch.setattr(platform, "unlock_file", held.remove)
    remove_tree = shutil.rmtree
    deletions = []

    def windows_remove(path, *args, **kwargs):
        assert held, "uninstall must retain its installer lock throughout deletion"
        path = Path(path)
        # POSIX would unlink a live lock successfully. Simulate Windows CRT
        # sharing instead so an incorrectly nested lock cannot mask residue.
        if any(os.path.samestat(os.fstat(fd), entry.stat())
               for entry in path.rglob("*") if entry.is_file() for fd in held):
            raise PermissionError("the installer lock is still open")
        deletions.append(path)
        return remove_tree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", windows_remove)

    assert cli.main(["--uninstall", "--delete-data"]) == 0
    assert data in deletions
    assert not data.exists()
    assert not root.exists()
    assert lock_path.is_file()
    assert not held
    output = capsys.readouterr().out
    assert "Astrid was uninstalled." in output
    assert "Remaining:" not in output
    windows_move.assert_any_call(root / "install.json.tmp", root / "install.json")


@pytest.mark.parametrize("selected", ["root", "data"])
@pytest.mark.parametrize("directory", ["state", "parent", "child", "bootstrap"])
def test_windows_refuses_selected_roots_containing_installer_lock(tmp_path, monkeypatch, selected, directory):
    local = tmp_path / "local"
    state = local / "Astrid Installer"
    chosen = {"state": state, "parent": local, "child": state / "workspace", "bootstrap": state / "bootstrap"}[directory]
    root = chosen if selected == "root" else tmp_path / "program"
    data = chosen if selected == "data" else tmp_path / "workspace"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="win32"))

    def private_directory(path):
        assert Path(path) != local
        Path(path).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(windows_security, "private_directory", private_directory)
    backend = Mock()
    backend.service.recorded.return_value = {}
    monkeypatch.setattr(cli, "Backend", Mock(return_value=backend))
    monkeypatch.setattr(platform, "lock_file", Mock())
    monkeypatch.setattr(platform, "unlock_file", Mock())
    installer = Mock()
    monkeypatch.setattr(cli, "Installer", installer)

    assert cli.main(["--root", str(root), "--data-root", str(data)]) == 1
    installer.assert_not_called()
    assert not (root / "install.json").exists()
    assert f"contains protected installer state at {state}" in (platform.log_dir() / "install.log").read_text()


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_unix_installer_retains_original_per_user_lock(tmp_path, monkeypatch, system):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform=system))
    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform=system))
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    backend = Mock()
    backend.service.recorded.return_value = {}
    monkeypatch.setattr(cli, "Backend", Mock(return_value=backend))
    monkeypatch.setattr(cli, "current", lambda _: SimpleNamespace(python=Path(__file__)))
    monkeypatch.setattr(cli, "Installer", Mock())
    monkeypatch.setattr(platform, "lock_file", Mock())
    monkeypatch.setattr(platform, "unlock_file", Mock())

    assert cli.main(["--root", str(tmp_path / "program"), "--data-root", str(tmp_path / "workspace")]) == 0
    original = default_program_root(Path.home(), system).parent / "astrid-install.lock"
    assert original.is_file()
    assert not (tmp_path / "logs" / "installer").exists()
