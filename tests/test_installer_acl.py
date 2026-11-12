"""Installer admission and lock placement through the Windows permission seam."""

import json
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
from astrid.installer.core import read_json, write_json
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
    monkeypatch.setattr(windows_security, "restrict", Mock())
    backend = Mock()
    backend.service.recorded.return_value = {}
    backend.source_checkout_ownership.return_value = None
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
    monkeypatch.setattr(windows_security, "restrict", Mock())
    backend = Mock()
    backend.service.recorded.return_value = {}
    backend.source_checkout_ownership.return_value = None
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

    def private_directory(path):
        assert Path(path) != local
        Path(path).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(windows_security, "private_directory", private_directory)
    monkeypatch.setattr(windows_security, "restrict", Mock())
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    (root / "owned").mkdir()
    data.mkdir()
    (data / "sentinel").write_text("workspace data")
    backend = Backend(root, platform.log_dir() / "install.log", local / "Astrid Installer/bootstrap/uv.exe")
    backend.service = SimpleNamespace(root=root, recorded=lambda: {}, selection=lock_path.with_name("astrid-install.json"), system="win32", say=print,
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

    with monkeypatch.context() as scoped:
        scoped.setattr(shutil, "rmtree", windows_remove)
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
    monkeypatch.setattr(windows_security, "restrict", Mock())
    backend = Mock()
    backend.service.recorded.return_value = {}
    backend.source_checkout_ownership.return_value = None
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
    backend.source_checkout_ownership.return_value = None
    monkeypatch.setattr(cli, "Backend", Mock(return_value=backend))
    monkeypatch.setattr(cli, "current", lambda _: SimpleNamespace(python=Path(__file__)))
    monkeypatch.setattr(cli, "Installer", Mock())
    monkeypatch.setattr(platform, "lock_file", Mock())
    monkeypatch.setattr(platform, "unlock_file", Mock())

    assert cli.main(["--root", str(tmp_path / "program"), "--data-root", str(tmp_path / "workspace")]) == 0
    original = default_program_root(Path.home(), system).parent / "astrid-install.lock"
    assert original.is_file()
    assert not (tmp_path / "logs" / "installer").exists()


@pytest.fixture
def windows_dacls(monkeypatch, windows_move):
    """Model Windows DACLs by inode, which a rename keeps: only restrict and
    private_directory create protected owner/SYSTEM descriptors."""
    protected = set()

    def key(path_or_fd):
        info = os.fstat(path_or_fd) if isinstance(path_or_fd, int) else os.stat(path_or_fd)
        return info.st_dev, info.st_ino

    def unsafe():
        return PermissionError("unsafe Windows owner/DACL: a protected, non-NULL DACL is required")

    def private_directory(path):
        path = Path(path)
        if path.exists():
            if key(path) not in protected:
                raise unsafe()
            return
        if not path.parent.exists():
            private_directory(path.parent)
        path.mkdir()
        protected.add(key(path))

    def read_private(path):
        # windows_security.read_private admits the parent and then the file.
        path = Path(path)
        for entry in (path.parent, path):
            if key(entry) not in protected:
                raise unsafe()
        return path.read_bytes()

    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(windows_security, "restrict", lambda target: protected.add(key(target)))
    monkeypatch.setattr(windows_security, "private_directory", private_directory)
    monkeypatch.setattr(windows_security, "read_private", read_private)
    return SimpleNamespace(protected=protected, key=key, private_directory=private_directory)


def _runtime_private_read(path):
    import importlib

    bootstrap = importlib.import_module("astrid_runtime.bootstrap")
    return bootstrap._private_handoff_bytes(path)


def test_windows_install_records_pass_the_runtime_private_read(tmp_path, monkeypatch, windows_dacls):
    """pinokio-windows run 42390048122: uninstall could not read the stop binding.

    Runtime reads every Pinokio stop-binding input with read_private. Records the
    installer writes must therefore be protected, inside protected directories.
    """
    from astrid.installer.core import Version, switch
    from astrid.installer.service import install_selection
    from astrid_runtime.paths import _install_record_path

    local, home = tmp_path / "local", tmp_path / "home"
    local.mkdir()  # LOCALAPPDATA: an inherited, unprotected DACL.
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    root = tmp_path / "program"
    platform.private_directory(root)

    selection = install_selection(home, "win32")
    assert selection == local / "Astrid Installer" / "astrid-install.json"
    assert selection == _install_record_path(home)
    write_json(selection, {"owner": LABEL, "managed_by": "pinokio"})
    write_json(root / "install.json", {"owner": LABEL, "managed_by": "pinokio"})
    folder = root / "versions" / "v1"
    folder.mkdir(parents=True)
    platform.owner_only(folder)
    write_json(folder / "version.json", {"root": str(folder)})
    switch(root, Version(folder, folder / "py" / "Scripts" / "python.exe", folder / "profile.json", {}))

    for record in (selection, root / "install.json", root / "current", folder / "version.json"):
        assert _runtime_private_read(record)
    legacy = local / "astrid-install.json"
    legacy.write_text("{}", encoding="utf-8")
    with pytest.raises(Exception, match="Pinokio stop binding is unavailable"):
        _runtime_private_read(legacy)


def test_posix_write_json_keeps_its_permissions(tmp_path, monkeypatch):
    restrict = Mock()
    monkeypatch.setattr(windows_security, "restrict", restrict)
    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="linux"))
    path = tmp_path / "state" / "install.json"
    old = os.umask(0o022)
    try:
        write_json(path, {"owner": LABEL})
    finally:
        os.umask(old)
    assert path.stat().st_mode & 0o777 == 0o644
    assert (tmp_path / "state").stat().st_mode & 0o777 == 0o755
    restrict.assert_not_called()


def test_windows_staged_version_folder_is_a_protected_record_parent(tmp_path, monkeypatch, windows_dacls):
    import astrid.installer.backend as backend_module

    root = tmp_path / "program"
    platform.private_directory(root)
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv", managed_by="pinokio")
    backend.uv.touch()
    monkeypatch.setattr(backend, "run", lambda argv, **_kwargs: "uv 1.0.0")

    class Staged(Exception):
        pass

    def source(name, _sha, destination, **_kwargs):
        # 297056f writes manifest.json after the Agent fetch (it validates the
        # explicit pin against the staged Agent first), so stop at the Runtime.
        if name == "agent":
            return destination
        raise Staged

    monkeypatch.setattr(backend_module, "source", source)
    manifest = {"agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40},
                "tools": {"uv": {"version": "1.0.0"}, "python": {"version": "3.11.9"}}}
    with pytest.raises(Staged):
        backend.stage(manifest, root, tmp_path / "workspace")

    (folder,) = (root / "versions").iterdir()
    assert windows_dacls.key(folder) in windows_dacls.protected
    assert _runtime_private_read(folder / "manifest.json")


def _legacy_selection(local, record):
    legacy = local / "astrid-install.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps(record), encoding="utf-8")
    return legacy


def test_windows_migrates_owned_legacy_selection_under_the_installer_lock(tmp_path, monkeypatch, windows_dacls):
    from astrid.installer.service import install_selection, migrate_install_selection

    local, home = tmp_path / "local", tmp_path / "home"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    record = {"owner": LABEL, "managed_by": "pinokio", "program_root": str(tmp_path / "program")}
    legacy = _legacy_selection(local, record)
    locks = []
    monkeypatch.setattr(platform, "lock_file", lambda fd, **kwargs: locks.append(kwargs))
    monkeypatch.setattr(platform, "unlock_file", Mock())

    migrate_install_selection(home, "win32")

    selection = install_selection(home, "win32")
    assert json.loads(selection.read_text(encoding="utf-8")) == record
    assert not legacy.exists()
    assert locks == [{"blocking": False}]
    assert (selection.parent / "astrid-install.lock").is_file()
    assert _runtime_private_read(selection)


@pytest.mark.parametrize("state", ["foreign", "malformed", "link", "busy"])
def test_windows_leaves_unowned_or_contended_legacy_selection_untouched(tmp_path, monkeypatch, windows_dacls, state):
    from astrid.installer.service import install_selection, migrate_install_selection

    local, home = tmp_path / "local", tmp_path / "home"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    legacy = _legacy_selection(local, {"owner": "someone.else" if state == "foreign" else LABEL})
    if state == "malformed":
        legacy.write_text("[", encoding="utf-8")
    if state == "link":
        target = tmp_path / "elsewhere.json"
        legacy.replace(target)
        legacy.symlink_to(target)
    before = legacy.read_bytes()

    def lock_file(_fd, **_kwargs):
        if state == "busy":
            raise BlockingIOError

    monkeypatch.setattr(platform, "lock_file", lock_file)
    monkeypatch.setattr(platform, "unlock_file", Mock())

    migrate_install_selection(home, "win32")

    assert legacy.read_bytes() == before
    assert not install_selection(home, "win32").exists()


def test_windows_retained_private_selection_wins_over_legacy_receipt(tmp_path, monkeypatch, windows_dacls):
    from astrid.installer.service import install_selection, migrate_install_selection

    local, home = tmp_path / "local", tmp_path / "home"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    legacy = _legacy_selection(local, {"owner": LABEL, "data_root": "stale", "uninstalled": True})
    selection = install_selection(home, "win32")
    write_json(selection, {"owner": LABEL, "data_root": "current"})
    monkeypatch.setattr(platform, "lock_file", Mock())
    monkeypatch.setattr(platform, "unlock_file", Mock())

    migrate_install_selection(home, "win32")

    assert read_json(selection) == {"owner": LABEL, "data_root": "current"}
    assert not legacy.exists()


def _selected_release(root, release):
    from astrid.installer.core import Version

    folder = root / "versions" / f"v{release}"
    folder.mkdir(parents=True)
    version = Version(folder, folder / "py" / "Scripts" / "python.exe", folder / "profile.json", {}, release)
    (folder / "version.json").write_text(json.dumps(version.document()), encoding="utf-8")
    (root / "current").write_text(folder.name + "\n", encoding="utf-8")


def _login_record(root, **fields):
    return {"owner": LABEL, "program_root": str(root), "data_root": str(root.parent / "data"), **fields}


@pytest.mark.parametrize("retained", [None, "uninstalled-receipt"])
def test_windows_live_r0_login_selection_stays_where_r0_reads_it(tmp_path, monkeypatch, windows_dacls, retained):
    """The R1 installer refuses a Windows login R0 upgrade; R0 keeps its own record.

    Run 42406972807: the candidate moved a live R0 record into the private
    directory before refusing, so the retained R0 uninstall found no record.
    """
    from astrid.installer.service import install_selection, migrate_install_selection

    local, home = tmp_path / "local", tmp_path / "home"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    root = tmp_path / "r0"
    _selected_release(root, 0)
    legacy = _legacy_selection(local, _login_record(root))
    before = legacy.read_bytes()
    selection = install_selection(home, "win32")
    receipt = _login_record(tmp_path / "earlier", uninstalled=True)
    if retained:
        write_json(selection, receipt)
    monkeypatch.setattr(platform, "lock_file", Mock())
    monkeypatch.setattr(platform, "unlock_file", Mock())

    migrate_install_selection(home, "win32")

    assert legacy.read_bytes() == before
    assert read_json(selection) == (receipt if retained else {})
    assert os.path.lexists(selection) is bool(retained)


@pytest.mark.parametrize("state", ["no-current", "linked-root", "malformed-version", "no-program-root"])
def test_windows_unproven_live_login_selection_is_left_untouched(tmp_path, monkeypatch, windows_dacls, state):
    from astrid.installer.service import install_selection, migrate_install_selection

    local, home = tmp_path / "local", tmp_path / "home"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    root = tmp_path / "program"
    if state == "linked-root":
        _selected_release(tmp_path / "target", 1)
        root.symlink_to(tmp_path / "target", target_is_directory=True)
    elif state == "malformed-version":
        _selected_release(root, 1)
        (root / "versions" / "v1" / "version.json").write_text("[", encoding="utf-8")
    else:
        root.mkdir()
    record = _login_record(root)
    if state == "no-program-root":
        record.pop("program_root")
    legacy = _legacy_selection(local, record)
    before = legacy.read_bytes()
    monkeypatch.setattr(platform, "lock_file", Mock())
    monkeypatch.setattr(platform, "unlock_file", Mock())

    migrate_install_selection(home, "win32")

    assert legacy.read_bytes() == before
    assert not os.path.lexists(install_selection(home, "win32"))


def test_windows_live_legacy_selection_beats_a_private_uninstalled_receipt(tmp_path, monkeypatch, windows_dacls):
    from astrid.installer.service import install_selection, migrate_install_selection

    local, home = tmp_path / "local", tmp_path / "home"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    root = tmp_path / "r1"
    _selected_release(root, 1)
    live = _login_record(root)
    legacy = _legacy_selection(local, live)
    selection = install_selection(home, "win32")
    write_json(selection, _login_record(tmp_path / "earlier", uninstalled=True))
    monkeypatch.setattr(platform, "lock_file", Mock())
    monkeypatch.setattr(platform, "unlock_file", Mock())

    migrate_install_selection(home, "win32")

    assert read_json(selection) == live
    assert not legacy.exists()
    assert _runtime_private_read(selection)


def test_windows_never_deletes_a_live_legacy_selection_it_did_not_migrate(tmp_path, monkeypatch, windows_dacls):
    from astrid.installer.service import install_selection, migrate_install_selection

    local, home = tmp_path / "local", tmp_path / "home"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    root, other = tmp_path / "r1", tmp_path / "other"
    _selected_release(root, 1)
    legacy = _legacy_selection(local, _login_record(root))
    before = legacy.read_bytes()
    selection = install_selection(home, "win32")
    private = _login_record(other)
    write_json(selection, private)
    monkeypatch.setattr(platform, "lock_file", Mock())
    monkeypatch.setattr(platform, "unlock_file", Mock())

    migrate_install_selection(home, "win32")

    assert read_json(selection) == private
    assert legacy.read_bytes() == before


@pytest.mark.parametrize("release", [0, 1])
def test_windows_uninstalled_receipt_migrates_for_reinstall(tmp_path, monkeypatch, windows_dacls, release):
    from astrid.installer.service import install_selection, migrate_install_selection

    local, home = tmp_path / "local", tmp_path / "home"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    root = tmp_path / "program"
    _selected_release(root, release)
    receipt = _login_record(root, uninstalled=True)
    legacy = _legacy_selection(local, receipt)
    monkeypatch.setattr(platform, "lock_file", Mock())
    monkeypatch.setattr(platform, "unlock_file", Mock())

    migrate_install_selection(home, "win32")

    assert read_json(install_selection(home, "win32")) == receipt
    assert not legacy.exists()


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_posix_selection_is_unchanged_and_never_migrated(tmp_path, monkeypatch, system):
    from astrid.installer.service import install_selection, migrate_install_selection

    home = tmp_path / "home"
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    record = home / ".local" / "share" / "astrid-install.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"owner": LABEL}), encoding="utf-8")
    assert install_selection(home, system) == record
    migrate_install_selection(home, system)
    assert record.is_file()


def test_installer_migrates_before_any_selection_read(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(platform, "log_dir", lambda: log_dir)
    events = []
    backend = Mock()
    backend.service.home, backend.service.system = tmp_path / "home", "win32"
    backend.service.recorded.side_effect = lambda: events.append("read") or {}
    backend.source_checkout_ownership.return_value = None
    monkeypatch.setattr(cli, "Backend", Mock(return_value=backend))
    monkeypatch.setattr(cli, "migrate_install_selection", lambda home, system: events.append(("migrate", home, system)))
    monkeypatch.setattr(cli, "current", lambda _: SimpleNamespace(python=Path(__file__)))
    monkeypatch.setattr(cli, "Installer", Mock())
    monkeypatch.setattr(platform, "lock_file", Mock())
    monkeypatch.setattr(platform, "unlock_file", Mock())
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    cli.main(["--root", str(tmp_path / "program"), "--data-root", str(tmp_path / "workspace")])

    assert events[0] == ("migrate", tmp_path / "home", "win32")
    assert "read" in events[1:]
