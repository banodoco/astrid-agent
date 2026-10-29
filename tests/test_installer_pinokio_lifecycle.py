"""Pinokio owns the foreground Runtime process and never installs a login service."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer import cli
from astrid.installer.core import (
    Installer,
    RuntimeOperationError,
    Version,
    read_json,
    switch,
    write_json,
)
from astrid.installer.service import LABEL, default_program_root, install_selection


class PinokioBackend:
    """Small lifecycle double that records every manager boundary."""

    def __init__(self, root, data):
        self.root, self.data = root, data
        self.calls = []
        self.old = None
        self.candidate = None
        self.fail_upgrade = False

    def existing(self, _data):
        return self.old

    def has_workspace(self, _data):
        return False

    def reinstallable(self, _data):
        return False

    def stage(self, _manifest, root, _data):
        self.calls.append(("stage",))
        folder = root / "versions" / "candidate"
        folder.mkdir(parents=True)
        self.candidate = _version(folder)
        return self.candidate

    def workspace(self, _version, data):
        self.calls.append(("workspace", data))
        return {"realm_id": "test-realm", "realm_root": str(data / "realm")}

    def validate_candidate(self, candidate, data, realm):
        self.calls.append(("validate_candidate", candidate, data, realm))

    def create(self, *_args):
        self.calls.append(("create",))
        return {"realm_id": "test-realm", "realm_root": str(self.data / "realm")}

    def attach_existing(self, *_args):
        raise AssertionError("no existing workspace expected")

    def backup(self, *_args):
        self.calls.append(("backup",))
        backup = self.root / "backups" / "saved"
        backup.mkdir(parents=True)
        return {"root": str(backup)}

    def upgrade(self, version, data):
        self.calls.append(("upgrade", version, data))
        if self.fail_upgrade:
            raise RuntimeError("migration failed")

    def stop(self, version, data):
        self.calls.append(("stop", version, data))

    def register(self, version, data):
        self.calls.append(("register", version, data))

    def start(self, version, data):
        self.calls.append(("start", version, data))

    def verify(self, version, data, realm_id, **_kwargs):
        self.calls.append(("verify", version, data, realm_id))

    def sync_skills(self, version):
        self.calls.append(("sync_skills", version))

    def finish(self, old, new, backup):
        self.calls.append(("finish", old, new, backup))

    def open(self):
        self.calls.append(("open",))

    def doctor(self, version, data, bundle):
        self.calls.append(("doctor", version, data, bundle))
        return bundle

    def unregister(self):
        self.calls.append(("unregister",))

    def preflight_remove(self, root, data, *, delete_data):
        self.calls.append(("preflight_remove", root, data, delete_data))
        return None

    def remove(self, root, data, *, delete_data):
        self.calls.append(("remove", root, data, delete_data))
        return []

    def restore(self, old, data, realm, backup):
        self.calls.append(("restore", old, data, realm, backup))


def _version(folder: Path):
    python = folder / "py" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    version = Version(folder, python, folder / "profile.json", {})
    write_json(folder / "version.json", version.document())
    return version


def _old_version(root):
    folder = root / "versions" / "previous"
    folder.mkdir(parents=True)
    return _version(folder)


def _managed(root, data, backend):
    return Installer(root, data, backend, managed_by="pinokio", say=lambda _line: None)


def test_pinokio_install_creates_runtime_but_registers_no_login_service(tmp_path):
    root, data = tmp_path / "app", tmp_path / "workspace"
    backend = PinokioBackend(root, data)

    _managed(root, data, backend).run("install", {})

    names = [call[0] for call in backend.calls]
    # register persists the Pinokio ownership/workspace receipt; it must not
    # activate a login service in the managed backend.
    assert names.count("register") == 1
    assert "start" not in names
    assert "verify" not in names
    assert names.index("create") < names.index("register") < names.index("sync_skills") < names.index("finish")
    assert "open" not in names


def test_pinokio_update_never_registers_or_starts_login_service(tmp_path):
    root, data = tmp_path / "app", tmp_path / "workspace"
    root.mkdir()
    backend = PinokioBackend(root, data)
    backend.old = _old_version(root)

    _managed(root, data, backend).run("update", {})

    names = [call[0] for call in backend.calls]
    assert names.count("stop") >= 1
    assert names.index("stage") < names.index("validate_candidate") < names.index("stop")
    assert names.index("stop") < names.index("backup") < names.index("upgrade")
    assert names.count("register") == 1
    assert "start" not in names
    assert "verify" not in names
    assert "sync_skills" in names


def test_pinokio_update_rollback_keeps_login_service_unmodified(tmp_path):
    root, data = tmp_path / "app", tmp_path / "workspace"
    root.mkdir()
    backend = PinokioBackend(root, data)
    backend.old = _old_version(root)
    backend.fail_upgrade = True

    with pytest.raises(RuntimeError, match="migration failed"):
        _managed(root, data, backend).run("update", {})

    names = [call[0] for call in backend.calls]
    assert "restore" in names
    assert names.index("stage") < names.index("validate_candidate") < names.index("stop")
    assert names.count("register") >= 1
    assert "start" not in names
    assert "verify" not in names


def test_pinokio_doctor_is_read_only_and_does_not_start_service(tmp_path):
    root, data = tmp_path / "app", tmp_path / "workspace"
    root.mkdir()
    backend = PinokioBackend(root, data)
    backend.old = _old_version(root)
    bundle = tmp_path / "doctor.zip"

    _managed(root, data, backend).run("doctor", {}, bundle=bundle)

    assert [call[0] for call in backend.calls] == ["doctor"]


def test_pinokio_uninstall_keeps_workspace_and_skips_service_removal(tmp_path):
    root, data = tmp_path / "app", tmp_path / "workspace"
    root.mkdir()
    backend = PinokioBackend(root, data)
    backend.old = _old_version(root)
    data.mkdir()
    sentinel = data / "keep.txt"
    sentinel.write_text("workspace", encoding="utf-8")

    _managed(root, data, backend).run("uninstall", {})

    names = [call[0] for call in backend.calls]
    assert "unregister" in names  # Retains the workspace receipt; no OS service call.
    assert sentinel.read_text(encoding="utf-8") == "workspace"
    assert names.index("preflight_remove") < names.index("remove")
    assert backend.calls[-1] == ("remove", root, data, False)


@pytest.mark.parametrize("system", ["darwin", "linux", "win32"])
@pytest.mark.parametrize("temporary_daemon", [False, True])
def test_managed_backend_stop_and_register_do_not_touch_login_service(tmp_path, monkeypatch, system, temporary_daemon):
    from astrid_runtime.paths import RuntimePaths
    from runtime_protocol.store import RealmStore

    from astrid.installer.backend import Backend

    root, data = tmp_path / "app", tmp_path / "workspace"
    root.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "local-app-data"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg-config"))
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv", managed_by="pinokio")
    from astrid.installer.service import Service
    backend.service = Service(root, tmp_path / "install.log", backend.run, home=home, system=system)
    # Runtime's C1 reader follows the host platform's canonical selection
    # location; only the service backend is parameterized across OS formats.
    from astrid_runtime.paths import _install_record_path
    backend.service.selection = _install_record_path(home)
    calls = []
    for name in ("disable", "stop", "stop_foreground", "register", "unregister"):
        monkeypatch.setattr(backend.service, name, lambda *args, _name=name, **kwargs: calls.append(_name))
    monkeypatch.setattr(backend, "workspace", lambda *_args: {
        "realm_id": "selected", "realm_root": str(paths.realms_dir / "selected")
    })
    monkeypatch.setattr(backend, "_prior_chat_settings", lambda _version: (None, {}))
    paths = RuntimePaths.current(data_root=data)
    paths.ensure_support_dirs()
    realm_root = paths.realms_dir / "selected"
    RealmStore.initialize(realm_root, realm_id="selected").close()
    write_json(paths.catalog_path, {
        "version": 1, "selected_realm_id": "selected",
        "realms": [{"realm_id": "selected", "display_name": "Realm", "data_root": str(realm_root)}],
        "source_profiles": {},
    })
    version_root = root / "versions" / "v1"
    version_root.mkdir(parents=True)
    version = _version(version_root)
    if temporary_daemon:
        import astrid_runtime
        runtime_checkout = Path(astrid_runtime.__file__).resolve().parents[1]
        source_checkout = Path(__file__).resolve().parents[1]
        environment = version.python.parent.parent
        declaration = version_root / "services.json"
        write_json(declaration, {"services": []})
        profile = {
            "profile": "astrid", "runtime_checkout": str(runtime_checkout),
            "source_checkout": str(source_checkout), "runtime_environment": str(environment),
            "source_digest": "", "service_declaration": str(declaration),
        }
        write_json(version.profile, profile)
        write_json(paths.source_profiles_dir / "astrid.json", profile)
        catalog = read_json(paths.catalog_path)
        catalog["source_profiles"] = {"astrid": profile}
        write_json(paths.catalog_path, catalog)
        version = Version(version.root, version.python, version.profile, {}, 1, "pinokio")
        write_json(version_root / "version.json", version.document())
        switch(root, version)
    runtime_calls = []
    c1_calls = []
    if temporary_daemon:
        from astrid_runtime.bootstrap import BootstrapConfig

        from tests.test_installer_busy_fence import Boundary, publish

        publish(paths, realm_root)
        monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary",
                            lambda: Boundary(paths, c1_calls))
        monkeypatch.setattr(BootstrapConfig, "resolve_source_profile", lambda *_args: object())
        monkeypatch.setattr(backend, "_discovery_owner_state", lambda _path: "live")

        def runtime(_version, _data, *args, **kwargs):
            runtime_calls.append((args, kwargs))
            pytest.fail("Pinokio stop must use C1's Runtime fence, not a second down command")

        monkeypatch.setattr(backend, "runtime", runtime)

    backend.register(version, data)
    if temporary_daemon:
        backend.stop(version, data)
        backend.unregister()
    else:
        # Pinokio selection without a live owner or a completed C1 stop is
        # not sufficient authority to clean up the manager-owned Runtime.
        with pytest.raises(RuntimeOperationError) as raised:
            backend.stop(version, data)
        assert raised.value.details == {"safe": False, "code": "stop_unproven"}

    assert calls == []
    assert backend.service.recorded()["managed_by"] == "pinokio"
    assert backend.service.recorded()["program_root"] == str(root)
    assert runtime_calls == []
    assert c1_calls == (["prepare", "runtime-stop"] if temporary_daemon else [])


def _cli_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "local-app-data"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg-config"))
    from runtime_protocol import platform

    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    return home


def test_pinokio_manager_argument_requires_absolute_root():
    with pytest.raises(SystemExit) as raised:
        cli.main(["--managed-by", "pinokio"])

    assert raised.value.code == 2


@pytest.mark.parametrize("argv", [
    ["--managed-by", "other", "--root", "/tmp/astrid"],
    ["--managed-by", "pinokio", "--root", "relative/path"],
])
def test_pinokio_cli_rejects_unknown_manager_and_relative_root(argv):
    with pytest.raises(SystemExit) as raised:
        cli.main(argv)

    assert raised.value.code == 2


@pytest.mark.parametrize("mode", [[], ["--update"]])
def test_ordinary_cli_refuses_active_pinokio_pointer(tmp_path, monkeypatch, capsys, mode):
    home = _cli_home(tmp_path, monkeypatch)
    root = tmp_path / "pinokio-app"
    write_json(root / "install.json", {
        "owner": LABEL, "managed_by": "pinokio", "data_root": str(tmp_path / "workspace"),
    })
    write_json(install_selection(home, sys.platform), {
        "owner": LABEL, "managed_by": "pinokio", "program_root": str(root),
        "data_root": str(tmp_path / "workspace"),
    })

    assert cli.main(mode) == 1
    assert "Astrid on this computer was installed with Pinokio. Choose Pinokio's Update or Uninstall button." in capsys.readouterr().out


def test_second_pinokio_root_is_refused_while_first_install_record_exists(tmp_path, monkeypatch, capsys):
    home = _cli_home(tmp_path, monkeypatch)
    first_root = tmp_path / "first-pinokio-app"
    write_json(first_root / "install.json", {
        "owner": LABEL, "managed_by": "pinokio", "data_root": str(tmp_path / "workspace"),
    })
    write_json(install_selection(home, sys.platform), {
        "owner": LABEL, "managed_by": "pinokio", "program_root": str(first_root),
        "data_root": str(tmp_path / "workspace"),
    })

    assert cli.main(["--managed-by", "pinokio", "--root", str(tmp_path / "second-pinokio-app")]) == 1
    assert f"Astrid is already installed in Pinokio at {first_root}." in capsys.readouterr().out


def _stub_cli_install(tmp_path, monkeypatch, home):
    selection = install_selection(home, sys.platform)
    python = tmp_path / "python"
    python.write_text("placeholder", encoding="utf-8")
    captured = {}

    class BackendStub:
        def __init__(self, *_args, **_kwargs):
            self.managed_by = None
            self.service = SimpleNamespace(
                selection=selection,
                recorded=lambda: read_json(selection),
                login_service_exists=lambda: False,
            )

        def existing(self, _data):
            return None

        def source_checkout_ownership(self, *_args, **_kwargs):
            return False

    class InstallerStub:
        def __init__(self, _root, data, _backend, **kwargs):
            captured["data"] = data
            captured["managed_by"] = kwargs.get("managed_by")

        def run(self, mode, *_args, **_kwargs):
            captured["mode"] = mode

    class BackendWithReadOnlyPreflight(BackendStub):
        def preflight_stop(self, _data):
            pass

    monkeypatch.setattr(cli, "Backend", BackendWithReadOnlyPreflight)
    monkeypatch.setattr(cli, "Installer", InstallerStub)
    monkeypatch.setattr(cli, "current", lambda _root: SimpleNamespace(python=python))
    return selection, captured


def test_reset_removed_pinokio_root_releases_pointer_for_terminal_install(tmp_path, monkeypatch):
    home = _cli_home(tmp_path, monkeypatch)
    old_root = tmp_path / "deleted-pinokio-app"
    selection, captured = _stub_cli_install(tmp_path, monkeypatch, home)
    data = tmp_path / "workspace"
    write_json(selection, {
        "owner": LABEL, "managed_by": "pinokio", "program_root": str(old_root),
        "data_root": str(data), "realm_id": "kept-workspace",
    })

    assert not old_root.exists()  # Reset removed the program directory.
    assert cli.main([]) == 0

    released = read_json(selection)
    assert captured == {"data": data, "managed_by": None, "mode": "install"}
    assert released["owner"] == LABEL
    assert released["data_root"] == str(data)
    assert released["realm_id"] == "kept-workspace"
    assert released["uninstalled"] is True
    assert "managed_by" not in released
    assert "program_root" not in released


def test_second_pinokio_root_is_allowed_after_first_root_is_deleted(tmp_path, monkeypatch):
    home = _cli_home(tmp_path, monkeypatch)
    old_root = tmp_path / "deleted-pinokio-app"
    second_root = tmp_path / "second-pinokio-app"
    selection, captured = _stub_cli_install(tmp_path, monkeypatch, home)
    data = tmp_path / "workspace"
    write_json(selection, {
        "owner": LABEL, "managed_by": "pinokio", "program_root": str(old_root),
        "data_root": str(data),
    })

    assert not old_root.exists()
    assert cli.main(["--managed-by", "pinokio", "--root", str(second_root)]) == 0

    assert captured == {"data": data, "managed_by": "pinokio", "mode": "install"}
    recorded = read_json(selection)
    assert recorded["managed_by"] == "pinokio"
    assert recorded["program_root"] == str(second_root)


def test_pinokio_install_refuses_existing_login_install(tmp_path, monkeypatch, capsys):
    home = _cli_home(tmp_path, monkeypatch)
    login_root = default_program_root(home, sys.platform)
    write_json(login_root / "install.json", {"owner": LABEL, "data_root": str(tmp_path / "workspace")})
    pinokio_root = tmp_path / "pinokio-app"

    assert cli.main(["--managed-by", "pinokio", "--root", str(pinokio_root)]) == 1
    output = capsys.readouterr().out
    assert f"Astrid is already installed on this computer ({login_root})." in output


def test_pinokio_install_refuses_login_service_pointer(tmp_path, monkeypatch, capsys):
    home = _cli_home(tmp_path, monkeypatch)
    login_root = tmp_path / "login-service-app"
    write_json(install_selection(home, sys.platform), {
        "owner": LABEL, "program_root": str(login_root), "data_root": str(tmp_path / "workspace"),
    })

    assert cli.main(["--managed-by", "pinokio", "--root", str(tmp_path / "pinokio-app")]) == 1
    assert f"Astrid is already installed on this computer ({login_root})." in capsys.readouterr().out


@pytest.mark.parametrize("mode", [["--update"], ["--uninstall"]])
def test_root_marker_retains_pinokio_manager_for_update_and_uninstall(tmp_path, monkeypatch, mode):
    _cli_home(tmp_path, monkeypatch)
    root = tmp_path / "pinokio-app"
    write_json(root / "install.json", {"owner": LABEL, "managed_by": "pinokio",
                                       "data_root": str(tmp_path / "workspace")})
    selection = tmp_path / "selection.json"
    captured = {}
    data = tmp_path / "workspace"
    record = {"owner": LABEL, "managed_by": "pinokio", "program_root": str(root),
              "data_root": str(data)}
    if mode == ["--update"]:
        old_root = root / "versions" / "old"
        old_root.mkdir(parents=True)
        python, profile = old_root / "python", old_root / "profile.json"
        python.write_text("placeholder", encoding="utf-8")
        profile.write_text("{}", encoding="utf-8")
        old = Version(old_root, python, profile, {}, release=4, managed_by="pinokio")
        write_json(old_root / "version.json", old.document())
        switch(root, old)

    class BackendStub:
        def __init__(self, *_args, **_kwargs):
            self.managed_by = None
            self.service = SimpleNamespace(selection=selection, recorded=lambda: record.copy(),
                                           login_service_exists=lambda: False)

        def preflight_stop(self, _data):
            captured["preflight"] = True

        def existing(self, _data):
            return None

    class InstallerStub:
        def __init__(self, _root, _data, _backend, **kwargs):
            captured["managed_by"] = kwargs.get("managed_by")

        def run(self, *args, **kwargs):
            captured["run"] = args[0]

    monkeypatch.setattr(cli, "Backend", BackendStub)
    monkeypatch.setattr(cli, "Installer", InstallerStub)
    if mode == ["--uninstall"]:
        monkeypatch.setattr(cli, "current", lambda _root: None)
    monkeypatch.setattr(cli, "default_doctor_bundle", lambda: tmp_path / "doctor.zip")

    argv = [*mode, "--root", str(root)]
    if mode == ["--update"]:
        manifest, uv = tmp_path / "manifest.json", tmp_path / "uv"
        manifest.write_text("{}", encoding="utf-8")
        uv.write_text("", encoding="utf-8")
        monkeypatch.setattr(cli, "validate_manifest", lambda _value: {})
        from astrid.installer import app_update

        monkeypatch.setattr(app_update, "update_pinokio", lambda *args, **kwargs: captured.update(
            managed_by="pinokio", run="update", adapter=(args, kwargs)))
        argv += ["--manifest", str(manifest), "--uv", str(uv)]

    assert cli.main(argv) == 0
    if mode == ["--update"]:
        assert captured["managed_by"] == "pinokio"
        assert captured["run"] == "update"
        assert captured["preflight"] is True
        assert captured["adapter"][0][0] == root
        assert captured["adapter"][1]["force"] is True
    else:
        assert captured == {"managed_by": "pinokio", "run": "uninstall", "preflight": True}


def test_pinokio_doctor_without_root_follows_per_user_pointer(tmp_path, monkeypatch):
    home = _cli_home(tmp_path, monkeypatch)
    root = tmp_path / "pinokio-app"
    data = tmp_path / "workspace"
    write_json(root / "install.json", {"owner": LABEL, "managed_by": "pinokio", "data_root": str(data)})
    record = {"owner": LABEL, "managed_by": "pinokio", "program_root": str(root),
              "data_root": str(data)}
    write_json(install_selection(home, sys.platform), record)
    captured = {}

    class BackendStub:
        def __init__(self, selected_root, *_args, **_kwargs):
            captured.setdefault("roots", []).append(selected_root)
            self.managed_by = None
            self.service = SimpleNamespace(selection=install_selection(home, sys.platform),
                                           recorded=lambda: record, login_service_exists=lambda: False)

        def existing(self, _data):
            return None

    class InstallerStub:
        def __init__(self, selected_root, _data, _backend, **kwargs):
            captured["root"] = selected_root
            captured["managed_by"] = kwargs.get("managed_by")

        def run(self, mode, *_args, **_kwargs):
            captured["mode"] = mode

    monkeypatch.setattr(cli, "Backend", BackendStub)
    monkeypatch.setattr(cli, "Installer", InstallerStub)
    monkeypatch.setattr(cli, "current", lambda _root: None)
    monkeypatch.setattr(cli, "default_doctor_bundle", lambda: tmp_path / "doctor.zip")

    assert cli.main(["--doctor"]) == 0
    assert captured["root"] == root
    assert captured["managed_by"] == "pinokio"
    assert captured["mode"] == "doctor"


def test_managed_successful_uninstall_releases_pointer_but_keeps_workspace(tmp_path, monkeypatch):
    from astrid.installer.backend import Backend

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "local-app-data"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg-config"))
    root, data = tmp_path / "app", tmp_path / "workspace"
    root.mkdir()
    write_json(root / "install.json", {"owner": LABEL, "managed_by": "pinokio", "data_root": str(data)})
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv", managed_by="pinokio")
    write_json(backend.service.selection, {
        "owner": LABEL, "program_root": str(root), "data_root": str(data),
        "managed_by": "pinokio", "realm_id": "realm", "realm_root": str(data / "realm"),
        "chat_settings": {},
    })
    workspace_file = data / "keep.txt"
    workspace_file.parent.mkdir()
    workspace_file.write_text("preserved", encoding="utf-8")
    monkeypatch.setattr(backend, "preflight_remove", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("astrid.installer.backend.remove_shim", lambda _service: None)

    backend.unregister()
    residue = backend.remove(root, data, delete_data=False)

    assert residue == []
    assert not root.exists()
    assert workspace_file.read_text(encoding="utf-8") == "preserved"
    record = backend.service.recorded()
    assert record["uninstalled"] is True
    assert "managed_by" not in record
    assert "program_root" not in record


def test_managed_uninstall_keeps_manager_pointer_when_files_remain(tmp_path, monkeypatch):
    from astrid.installer.backend import Backend

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "local-app-data"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg-config"))
    root, data = tmp_path / "app", tmp_path / "workspace"
    root.mkdir()
    (root / "versions").mkdir()
    (root / "versions" / "leftover").mkdir()
    write_json(root / "install.json", {"owner": LABEL, "managed_by": "pinokio", "data_root": str(data)})
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv", managed_by="pinokio")
    write_json(backend.service.selection, {
        "owner": LABEL, "program_root": str(root), "data_root": str(data),
        "managed_by": "pinokio", "realm_id": "realm", "realm_root": str(data / "realm"),
        "chat_settings": {},
    })
    monkeypatch.setattr(backend, "preflight_remove", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("astrid.installer.backend.remove_shim", lambda _service: None)
    original_rmtree = __import__("shutil").rmtree

    def fail_versions(path, *args, **kwargs):
        if Path(path).name == "versions":
            raise OSError("still in use")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("astrid.installer.backend.shutil.rmtree", fail_versions)
    backend.unregister()
    residue = backend.remove(root, data, delete_data=False)

    assert root / "versions" in residue
    record = backend.service.recorded()
    assert record["managed_by"] == "pinokio"
    assert record["program_root"] == str(root)
    assert record["uninstalled"] is True
    assert data == Path(record["data_root"])


def test_interrupted_pinokio_update_recovery_does_not_activate_login_service(tmp_path):
    root, data = tmp_path / "app", tmp_path / "workspace"
    old_root, candidate_root = root / "versions" / "old", root / "versions" / "candidate"
    old_root.mkdir(parents=True)
    candidate_root.mkdir(parents=True)
    old, candidate = _version(old_root), _version(candidate_root)
    backend = PinokioBackend(root, data)
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "a" * 32,
        "step": "staged", "data_root": str(data), "old": old.document(),
        "candidate": candidate.document(), "realm": {"realm_id": "test-realm", "realm_root": str(data / "realm")},
        "backup": None,
    })

    _managed(root, data, backend)._recover_update()

    names = [call[0] for call in backend.calls]
    assert names == ["stop", "register", "workspace"]
    assert "start" not in names
    assert "verify" not in names


@pytest.mark.parametrize("manager", [None, "pinokio"])
def test_partial_uninstall_recreates_owned_marker_when_empty_program_directory_is_locked(tmp_path, monkeypatch, manager):
    from astrid.installer.backend import Backend

    _cli_home(tmp_path, monkeypatch)
    root, data = tmp_path / "app", tmp_path / "workspace"
    root.mkdir()
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv", managed_by=manager)
    record = {"owner": LABEL, "program_root": str(root), "data_root": str(data), "uninstalled": True}
    if manager:
        record["managed_by"] = manager
    write_json(backend.service.selection, record)
    monkeypatch.setattr(backend, "preflight_remove", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("astrid.installer.backend.remove_shim", lambda _service: None)
    original = Path.rmdir

    def locked(path):
        if path == root:
            raise OSError("program directory is still open")
        return original(path)

    monkeypatch.setattr(Path, "rmdir", locked)
    assert backend.remove(root, data, delete_data=False) == [root]
    marker = read_json(root / "install.json")
    assert marker["owner"] == LABEL and marker["data_root"] == str(data)
    assert marker.get("managed_by") == manager
