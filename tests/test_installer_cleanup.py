"""Uninstall also accounts for a bootstrap outside a custom program folder."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from astrid.installer.backend import Backend
from astrid.installer.core import Installer, Version, write_json
from astrid.installer.service import LABEL, bootstrap_root


def installation(tmp_path, monkeypatch, system=None):
    system = system or sys.platform
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    root, data = tmp_path / "custom program", tmp_path / "workspace"
    data.mkdir()
    (data / "sentinel").write_text("keep")
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    if system == "win32":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
        monkeypatch.setattr("astrid.installer.launcher._update_user_path", Mock())
    bootstrap = bootstrap_root(home, system)
    bootstrap.mkdir(parents=True)
    uv = bootstrap / ("uv.exe" if system == "win32" else "uv")
    uv.write_text("installer-owned bootstrap")
    backend = Backend(root, tmp_path / "log", uv)
    backend.service.system = system
    return backend, root, data, bootstrap


def test_custom_root_uninstall_removes_programs_and_keeps_data(tmp_path, monkeypatch):
    backend, root, data, bootstrap = installation(tmp_path, monkeypatch)

    assert backend.remove(root, data, delete_data=False) == []

    assert not root.exists()
    assert bootstrap.exists() is (backend.service.system == "win32")
    assert (data / "sentinel").read_text() == "keep"


def test_custom_root_reports_locked_default_bootstrap(tmp_path, monkeypatch):
    backend, root, data, bootstrap = installation(tmp_path, monkeypatch)
    remove_tree = shutil.rmtree

    def remove(path, *args, **kwargs):
        if Path(path) == bootstrap:
            raise PermissionError("running bootstrap is locked")
        return remove_tree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", remove)

    assert backend.remove(root, data, delete_data=False) == ([] if backend.service.system == "win32" else [bootstrap])
    assert not root.exists()
    assert bootstrap.is_dir()
    assert (data / "sentinel").read_text() == "keep"


def test_custom_root_does_not_remove_a_linked_bootstrap(tmp_path, monkeypatch):
    backend, root, data, bootstrap = installation(tmp_path, monkeypatch)
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel").write_text("keep")
    shutil.rmtree(bootstrap)
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(bootstrap), str(external)],
                       check=True, capture_output=True, text=True)
    else:
        bootstrap.symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="cannot contain links"):
        backend.remove(root, data, delete_data=False)
    assert (root / "install.json").is_file()
    assert (external / "sentinel").read_text() == "keep"
    assert (data / "sentinel").read_text() == "keep"


def test_custom_uv_does_not_authorize_removing_default_bootstrap(tmp_path, monkeypatch):
    backend, root, data, bootstrap = installation(tmp_path, monkeypatch)
    backend.uv = tmp_path / "other-tools" / "uv"

    assert backend.remove(root, data, delete_data=False) == []
    assert bootstrap.is_dir()
    assert (data / "sentinel").read_text() == "keep"


@pytest.mark.parametrize("relation", ["same", "child", "parent"])
@pytest.mark.parametrize("tree_name", ["program", "bootstrap"])
def test_support_overlap_is_refused_before_any_removal(tmp_path, monkeypatch, relation, tree_name):
    backend, root, _, bootstrap = installation(tmp_path, monkeypatch)
    tree = root if tree_name == "program" else bootstrap
    data = {"same": tree, "child": tree / "workspace", "parent": tree.parent}[relation]
    data.mkdir(parents=True, exist_ok=True)
    sentinel = data / "kept-data"
    sentinel.write_bytes(b"workspace bytes")
    before = sentinel.stat().st_mtime_ns
    remove = Mock(side_effect=AssertionError("no removal may precede preflight"))
    monkeypatch.setattr(shutil, "rmtree", remove)

    with pytest.raises(ValueError, match="support directory overlaps"):
        backend.remove(root, data, delete_data=False)

    remove.assert_not_called()
    assert sentinel.read_bytes() == b"workspace bytes"
    assert sentinel.stat().st_mtime_ns == before
    assert (root / "install.json").is_file()
    assert backend.uv.is_file()


@pytest.mark.parametrize("relation", ["same", "child", "parent"])
@pytest.mark.parametrize("tree_name", ["program", "bootstrap"])
def test_attached_overlap_is_refused_before_unregister(tmp_path, monkeypatch, relation, tree_name):
    backend, root, data, bootstrap = installation(tmp_path, monkeypatch)
    tree = root if tree_name == "program" else bootstrap
    realm = {"same": tree, "child": tree / "realm", "parent": tree.parent}[relation]
    realm.mkdir(parents=True, exist_ok=True)
    sentinel = realm / "kept-film"
    sentinel.write_bytes(b"media bytes")
    before = sentinel.stat().st_mtime_ns
    write_json(data / "runtime/catalog.json", {"test": "Runtime owns interpretation"})
    monkeypatch.setattr(backend, "workspace", lambda version, support: {"realm_root": str(realm)})
    stop, unregister = Mock(), Mock()
    monkeypatch.setattr(backend, "stop", stop)
    monkeypatch.setattr(backend, "unregister", unregister)

    with pytest.raises(ValueError, match="attached workspace overlaps"):
        Installer(root, data, backend).run("uninstall", {})

    stop.assert_not_called()
    unregister.assert_not_called()
    assert sentinel.read_bytes() == b"media bytes"
    assert sentinel.stat().st_mtime_ns == before
    assert (root / "install.json").is_file()
    assert backend.uv.is_file()


def test_unknown_attached_identity_preserves_program_and_bootstrap(tmp_path, monkeypatch):
    backend, root, data, bootstrap = installation(tmp_path, monkeypatch)
    write_json(data / "runtime/catalog.json", {"test": "unreadable identity"})
    monkeypatch.setattr(backend, "workspace", Mock(side_effect=ValueError("identity is ambiguous")))
    unregister = Mock()
    monkeypatch.setattr(backend, "unregister", unregister)

    with pytest.raises(ValueError, match="identity is ambiguous"):
        Installer(root, data, backend).run("uninstall", {})

    unregister.assert_not_called()
    assert (root / "install.json").is_file()
    assert backend.uv.is_file()
    assert (data / "sentinel").read_text() == "keep"


def test_partial_uninstall_inspects_kept_catalog_with_bootstrap_runtime(tmp_path, monkeypatch):
    backend, root, data, bootstrap = installation(tmp_path, monkeypatch)
    (root / "current").write_text("removed-version\n")
    write_json(data / "runtime/catalog.json", {"test": "kept catalog"})
    runtime = Mock(return_value={"ok": True, "realm_id": "kept", "realm_root": str(data)})
    monkeypatch.setattr(backend, "runtime", runtime)

    assert backend.remove(root, data, delete_data=False) == []

    runtime.assert_called_once_with(None, data, "workspace", "inspect")
    assert not root.exists() and not bootstrap.exists()
    assert (data / "sentinel").read_text() == "keep"


def test_missing_interpreter_uses_bootstrap_for_workspace_inspection(tmp_path, monkeypatch):
    backend, root, data, _ = installation(tmp_path, monkeypatch)
    version = Version(root / "versions/removed", root / "missing/python", root / "profile", {})
    runtime = Mock(return_value={"ok": True, "realm_id": "kept", "realm_root": str(data)})
    monkeypatch.setattr(backend, "runtime", runtime)

    assert backend.workspace(version, data)["realm_id"] == "kept"
    runtime.assert_called_once_with(None, data, "workspace", "inspect")


def test_retained_external_identity_is_inspected_after_support_removal(tmp_path, monkeypatch):
    backend, root, data, _ = installation(tmp_path, monkeypatch)
    shutil.rmtree(data)
    realm = tmp_path / "external-realm"
    realm.mkdir()
    (realm / "sentinel").write_text("keep")
    monkeypatch.setattr(backend.service, "recorded", lambda: {
        "owner": LABEL, "realm_root": str(realm), "realm_id": "kept",
    })
    runtime = Mock(return_value={"ok": True, "realm_id": "kept", "realm_root": str(realm)})
    monkeypatch.setattr(backend, "runtime", runtime)

    assert backend.remove(root, data, delete_data=False) == []

    runtime.assert_called_once_with(None, data, "workspace", "inspect", "--realm-root", str(realm),
                                    "--expected-realm-id", "kept")
    assert (realm / "sentinel").read_text() == "keep"


def test_already_deleted_recorded_workspace_does_not_block_residue_cleanup(tmp_path, monkeypatch):
    backend, root, data, _ = installation(tmp_path, monkeypatch)
    shutil.rmtree(data)
    monkeypatch.setattr(backend.service, "recorded", lambda: {
        "owner": LABEL, "realm_root": str(tmp_path / "deleted-realm"), "realm_id": "removed",
    })
    runtime = Mock(side_effect=AssertionError("already deleted data needs no inspection"))
    monkeypatch.setattr(backend, "runtime", runtime)

    assert backend.remove(root, data, delete_data=True) == []
    runtime.assert_not_called()
    assert not root.exists()


@pytest.mark.parametrize("realm_exists", [True, False])
def test_retained_selection_recovers_version_with_only_support_logs(tmp_path, monkeypatch, realm_exists):
    backend, root, data, _ = installation(tmp_path, monkeypatch)
    (data / "logs").mkdir()
    version = Version(root / "versions/v1", root / "python", root / "profile", {})
    write_json(version.root / "version.json", version.document())
    (root / "current").write_text("v1\n")
    realm = tmp_path / "kept-realm"
    if realm_exists:
        realm.mkdir()
        (realm / "sentinel").write_text("keep")
    monkeypatch.setattr(backend.service, "recorded", lambda: {
        "owner": LABEL, "realm_root": str(realm), "realm_id": "kept",
    })
    runtime = Mock(return_value={"ok": True, "realm_id": "kept", "realm_root": str(realm)})
    monkeypatch.setattr(backend, "runtime", runtime)

    assert backend.remove(root, data, delete_data=False) == []

    if realm_exists:
        runtime.assert_called_once_with(None, data, "workspace", "inspect", "--realm-root", str(realm),
                                        "--expected-realm-id", "kept")
        assert (realm / "sentinel").read_text() == "keep"
    else:
        runtime.assert_not_called()
    assert (data / "sentinel").read_text() == "keep"


def test_windows_defers_running_bootstrap_to_parent_after_core_exit(tmp_path, monkeypatch):
    backend, root, data, bootstrap = installation(tmp_path, monkeypatch, system="win32")
    lines = []
    backend.service.say = lines.append
    remove_tree = shutil.rmtree

    def remove(path, *args, **kwargs):
        if Path(path) == bootstrap:
            raise PermissionError("bootstrap is locked")
        return remove_tree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", remove)

    assert backend.remove(root, data, delete_data=False) == []

    assert lines == []
    assert bootstrap == tmp_path / "local-app-data/Astrid Installer/bootstrap"
    assert not root.exists()
    assert backend.uv.is_file()



@pytest.mark.parametrize("workspace", ["support", "attached"])
def test_windows_parent_cleanup_protects_workspace_beside_bootstrap(tmp_path, monkeypatch, workspace):
    backend, root, data, bootstrap = installation(tmp_path, monkeypatch, system="win32")
    protected = bootstrap.parent / "workspace"
    protected.mkdir()
    sentinel = protected / "film"
    sentinel.write_bytes(b"keep")
    if workspace == "support":
        data = protected
    else:
        write_json(data / "runtime/catalog.json", {"selected": "realm"})
        monkeypatch.setattr(backend, "workspace", lambda *_args: {"realm_root": str(protected)})
    remove = Mock(side_effect=AssertionError("workspace must be protected before removal"))
    monkeypatch.setattr(shutil, "rmtree", remove)

    with pytest.raises(ValueError, match="workspace.*overlaps|support directory overlaps"):
        backend.remove(root, data, delete_data=False)

    remove.assert_not_called()
    assert sentinel.read_bytes() == b"keep"
    assert backend.uv.is_file()


def test_windows_program_cleanup_includes_retained_ownership_marker(tmp_path, monkeypatch):
    backend, root, data, _ = installation(tmp_path, monkeypatch, system="win32")
    lines = []
    backend.service.say = lines.append
    locked = root / "versions"
    locked.mkdir()
    remove_tree = shutil.rmtree

    def remove(path, *args, **kwargs):
        if Path(path) == locked:
            raise PermissionError("installed Python is locked")
        return remove_tree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", remove)

    assert backend.remove(root, data, delete_data=False) == [locked]

    assert lines == []
    assert (root / "install.json").is_file()
    assert (data / "sentinel").read_text() == "keep"
