"""CPU first-run contracts using the existing installer and Runtime facade.

Service/tool/Runtime effects are faked; local Git materialization is real.
The Windows cases exercise selection paths, not native Windows services.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer import service, tools
from astrid.installer.core import Installer, write_json
from astrid.runtime_cli import RuntimeCLI
from astrid.sdk import storage_root
from tests.test_installer_lifecycle import LifecycleBackend
from tests.test_installer_sources import repository  # noqa: F401 (existing local Git fixture)


def _snapshot(root):
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


@pytest.fixture(params=["linux", "win32"])
def selection(request, monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData/Local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "xdg-data"))
    monkeypatch.setenv("ASTRID_STATE_HOME", str(home / "state"))
    monkeypatch.delenv(storage_root.DATA_ROOT_ENV, raising=False)
    # Replace this module's sys reference, never the interpreter's platform.
    monkeypatch.setattr(storage_root, "sys", SimpleNamespace(platform=request.param))
    monkeypatch.setattr(storage_root, "CONFIG_PATH", tmp_path / "absent-config.json")
    return service.install_selection(home, request.param)


def test_repo_ref_install_repair_and_kept_workspace_reinstall(
    repository, selection, monkeypatch, tmp_path,
):
    repo, commit = repository
    original = (repo / "pyproject.toml").read_bytes()
    (repo / "pyproject.toml").write_text("dirty checkout is not installed\n")
    (repo / "untracked.txt").write_text("not a release input")
    program, data = tmp_path / "program", tmp_path / "selected-workspace"
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    monkeypatch.setattr(tools, "download", lambda *a, **k: pytest.fail("network download"))

    class RepoBackend(LifecycleBackend):
        def stage(self, manifest, root, support):
            version = super().stage(manifest, root, support)
            tools.source("agent", manifest["agent"]["sha"], version.root / "a",
                         repo=manifest["agent"]["repo"])
            return version

        def create(self, *args):
            super().create(*args)
            write_json(data / "runtime/catalog.json", {"selected_realm_id": "kept-realm"})
            write_json(data / "runtime/realms/kept-realm/project.json", {"id": "project-a"})
            return self.workspace()

        def register(self, version, support):
            super().register(version, support)
            write_json(selection, {"owner": service.LABEL, "data_root": str(support),
                                   "program_root": str(program)})

    manifest = {"agent": {"sha": commit, "repo": str(repo)}}
    before = _snapshot(tmp_path)
    assert storage_root.resolve_runtime_data_root() == Path.home() / ".astrid-data"
    assert _snapshot(tmp_path) == before

    backend = RepoBackend(program, data)
    Installer(program, data, backend, say=lambda _: None).run("install", manifest)
    assert (backend.candidate.root / "a/pyproject.toml").read_bytes() == original
    assert not (backend.candidate.root / "a/untracked.txt").exists()
    assert [event[0] for event in backend.calls].count("create") == 1
    assert storage_root.resolve_runtime_data_root() == data
    retained = _snapshot(data)
    record = selection.read_bytes()

    # Ordinary repeat install repairs the selected program and workspace.
    backend.old = backend.candidate
    backend.calls.clear()
    Installer(program, data, backend, say=lambda _: None).run("install", manifest)
    assert not {"stage", "create", "upgrade"} & {event[0] for event in backend.calls}
    assert _snapshot(data) == retained

    # Model the installer's kept-data uninstall boundary: program removed,
    # authoritative selection and Runtime-owned data retained for reattach.
    shutil.rmtree(program)
    write_json(selection, {**json.loads(record), "uninstalled": True})
    assert storage_root.resolve_runtime_data_root() == data
    backend = RepoBackend(program, data)
    Installer(program, data, backend, say=lambda _: None).run("install", manifest)
    assert "attach" in [event[0] for event in backend.calls]
    assert "create" not in [event[0] for event in backend.calls]
    assert _snapshot(data) == retained
    assert storage_root.resolve_runtime_data_root() == data
    assert not (Path.home() / ".astrid-data").exists()
    assert not list(unrelated.iterdir())


def test_installed_root_wins_over_stale_setup_and_checkout(selection, monkeypatch, tmp_path):
    data = tmp_path / "installed-data"
    write_json(selection, {"owner": service.LABEL, "data_root": str(data)})
    write_json(storage_root.CONFIG_PATH, {"version": 1, "data_root": "checkout-data"})
    write_json(storage_root._persistent_setup_selection_path(),
               {"setup_selection": {"support_root": str(tmp_path / "old-setup")}})
    before = _snapshot(tmp_path)
    assert storage_root.resolve_runtime_data_root() == data
    storage_root.ensure_no_unmigrated_runtime(data)
    assert _snapshot(tmp_path) == before


def test_blank_workspace_inspection_is_read_only_until_explicit_setup(selection, tmp_path):
    data, realm = tmp_path / "data", tmp_path / "realm"
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv[1:])
        if argv[1:3] == ["workspace", "create"]:
            write_json(data / "runtime/catalog.json", {"selected_realm_id": "realm-a"})
        exists = (data / "runtime/catalog.json").is_file()
        result = {"ok": exists, "realm_id": "realm-a"} if exists else {
            "ok": False, "problem_code": "workspace_missing"}
        return subprocess.CompletedProcess(argv, 0 if exists else 1, json.dumps(result), "")

    runtime = RuntimeCLI(command=["fake-runtime"], runner=runner)
    before = _snapshot(tmp_path)
    assert not runtime.inspect(support_root=data).ok
    assert not runtime.observe("status", support_root=data).ok
    assert _snapshot(tmp_path) == before
    assert runtime.configure("create", support_root=data, realm_root=realm,
                             realm_id="realm-a", display_name="Generation").ok
    catalog = (data / "runtime/catalog.json").read_bytes()
    assert runtime.inspect(support_root=data).data["realm_id"] == "realm-a"
    assert (data / "runtime/catalog.json").read_bytes() == catalog
    assert [call[:2] for call in calls] == [
        ["workspace", "inspect"], ["status", "--data-root"],
        ["workspace", "create"], ["workspace", "inspect"],
    ]


@pytest.mark.parametrize("contents", ["[]", "{", '{"owner":"haus.astrid.runtime","data_root":"relative"}'])
def test_invalid_install_selection_refuses_before_fallback(selection, contents, tmp_path):
    selection.parent.mkdir(parents=True, exist_ok=True)
    selection.write_text(contents)
    before = _snapshot(tmp_path)
    with pytest.raises(ValueError, match="install record"):
        storage_root.resolve_runtime_data_root()
    assert _snapshot(tmp_path) == before


@pytest.mark.posix_native
@pytest.mark.parametrize("linked_parent", [False, True])
def test_linked_install_selection_cannot_choose_blank_fallback(
    selection, linked_parent, tmp_path,
):
    target = tmp_path / "real-selection"
    target.mkdir()
    write_json(target / selection.name, {"owner": service.LABEL,
                                       "data_root": str(tmp_path / "existing-data")})
    if linked_parent:
        selection.parent.parent.mkdir(parents=True, exist_ok=True)
        selection.parent.symlink_to(target, target_is_directory=True)
    else:
        selection.parent.mkdir(parents=True, exist_ok=True)
        selection.symlink_to(target / selection.name)
    with pytest.raises(ValueError, match="symlink component"):
        storage_root.resolve_runtime_data_root()
    assert not (Path.home() / ".astrid-data").exists()


def test_explicit_root_still_overrides_invalid_install_selection(selection, monkeypatch, tmp_path):
    selection.parent.mkdir(parents=True, exist_ok=True)
    selection.write_text("invalid")
    chosen = tmp_path / "explicit"
    monkeypatch.setenv(storage_root.DATA_ROOT_ENV, str(chosen))
    assert storage_root.resolve_runtime_data_root() == chosen
    assert not chosen.exists()
