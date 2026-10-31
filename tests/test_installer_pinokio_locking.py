"""Cross-platform ownership tests for Pinokio's shared foreground/install lock."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import venv
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer import cli, foreground
from astrid.installer.core import Version, read_json, switch, write_json
from astrid.installer.service import (
    LABEL,
    default_program_root,
    install_selection,
    installer_lock_path,
)


def _set_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "local-app-data"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg-config"))
    from runtime_protocol import platform

    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    return home


@pytest.mark.parametrize("system", ["darwin", "linux", "win32"])
def test_installer_lock_path_is_one_user_location_per_platform(tmp_path, monkeypatch, system):
    home = _set_home(tmp_path, monkeypatch)
    local = home / "local-app-data"
    xdg = home / "xdg-data"

    if system == "darwin":
        expected = home / ".local" / "share" / "astrid-install.lock"
    elif system == "linux":
        expected = xdg / "astrid-install.lock"
    else:
        expected = local / "Astrid Installer" / "astrid-install.lock"

    assert installer_lock_path(home, system) == expected


def test_foreground_run_is_refused_while_an_installer_holds_the_lock(tmp_path, monkeypatch):
    from runtime_protocol import platform

    home = _set_home(tmp_path, monkeypatch)
    root = default_program_root(home, sys.platform)
    data = tmp_path / "workspace"
    folder = root / "versions" / "old"
    folder.mkdir(parents=True)
    python, profile = folder / "bin" / "python", folder / "profile.json"
    python.parent.mkdir(parents=True)
    python.write_text("placeholder", encoding="utf-8")
    profile.write_text("{}\n", encoding="utf-8")
    version = Version(folder, python, profile, {"PATH": os.defpath}, managed_by="pinokio")
    write_json(folder / "version.json", version.document())
    switch(root, version)
    write_json(root / "install.json", {
        "owner": LABEL, "managed_by": "pinokio", "data_root": str(data),
    })
    path = installer_lock_path(home, sys.platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as held:
        platform.lock_file(held.fileno(), blocking=False)
        with pytest.raises(ValueError, match="already running or being installed"):
            foreground.launch(root, [], system=sys.platform)
        platform.unlock_file(held.fileno())


@pytest.mark.parametrize("mode", ["install", "update", "uninstall"])
def test_busy_pinokio_mutation_refuses_before_writing_install_state(tmp_path, monkeypatch, capsys, mode):
    from runtime_protocol import platform

    home = _set_home(tmp_path, monkeypatch)
    root = default_program_root(home, sys.platform)
    data = tmp_path / "workspace"
    marker_path = root / "install.json"
    selection_path = install_selection(home, sys.platform)
    if mode != "install":
        write_json(marker_path, {"owner": LABEL, "managed_by": "pinokio", "data_root": str(data)})
        write_json(selection_path, {
            "owner": LABEL, "managed_by": "pinokio", "program_root": str(root),
            "data_root": str(data),
        })
    if mode == "update":
        folder = root / "versions" / "old"
        folder.mkdir(parents=True)
        python, profile = folder / "bin" / "python", folder / "profile.json"
        python.parent.mkdir()
        python.write_text("placeholder", encoding="utf-8")
        profile.write_text("{}\n", encoding="utf-8")
        old = Version(folder, python, profile, {}, release=1, managed_by="pinokio")
        write_json(folder / "version.json", old.document())
        switch(root, old)
        from astrid.installer.backend import Backend

        def fenced_preflight(_backend, _data):
            raise ValueError("Astrid is busy; the C1 preflight refused this update.")

        monkeypatch.setattr(Backend, "preflight_stop", fenced_preflight)
    marker_before = marker_path.read_bytes() if marker_path.exists() else None
    selection_before = selection_path.read_bytes() if selection_path.exists() else None
    lock_path = installer_lock_path(home, sys.platform)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+b")
    platform.lock_file(lock.fileno(), blocking=False)
    current_calls = []
    if mode != "update":
        monkeypatch.setattr(cli, "current", lambda _root: current_calls.append(_root))
    monkeypatch.setattr("astrid.installer.backend.Backend.source_checkout_ownership",
                        lambda *_args, **_kwargs: pytest.fail("source guard ran before Pinokio lock refusal"))

    try:
        if mode == "install":
            argv = ["--managed-by", "pinokio", "--root", str(root)]
        else:
            argv = ["--root", str(root), "--update" if mode == "update" else "--uninstall"]
        assert cli.main(argv) == 1
        output = capsys.readouterr().out
        if mode == "update":
            assert "Astrid is busy; the C1 preflight refused this update." in output
            assert (root / "current").read_text().strip() == "old"
            assert not (root / "update-transaction.json").exists()
        else:
            assert "Astrid is still running. Choose Pinokio's Stop button, then try again." in output
            assert current_calls == []
        assert (marker_path.read_bytes() if marker_path.exists() else None) == marker_before
        assert (selection_path.read_bytes() if selection_path.exists() else None) == selection_before
    finally:
        platform.unlock_file(lock.fileno())
        lock.close()


def test_doctor_remains_read_only_while_foreground_holds_lock(tmp_path, monkeypatch):
    from runtime_protocol import platform

    home = _set_home(tmp_path, monkeypatch)
    root = default_program_root(home, sys.platform)
    data = tmp_path / "workspace"
    marker = root / "install.json"
    write_json(marker, {"owner": LABEL, "managed_by": "pinokio", "data_root": str(data)})
    selection = install_selection(home, sys.platform)
    write_json(selection, {
        "owner": LABEL, "managed_by": "pinokio", "program_root": str(root),
        "data_root": str(data),
    })
    before = marker.read_bytes(), selection.read_bytes()
    lock_path = installer_lock_path(home, sys.platform)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as held:
        platform.lock_file(held.fileno(), blocking=False)
        monkeypatch.setattr(platform, "lock_file", lambda *_args, **_kwargs: pytest.fail("doctor tried to acquire the run lock"))
        monkeypatch.setattr(cli, "current", lambda _root: None)
        monkeypatch.setattr(cli, "Installer", lambda *_args, **_kwargs: SimpleNamespace(
            run=lambda *_args, **_kwargs: None
        ))
        assert cli.main(["--doctor"]) == 0
        assert (marker.read_bytes(), selection.read_bytes()) == before
        platform.unlock_file(held.fileno())


def test_cli_recovery_releases_live_runtime_before_workspace_and_returns_recovered_owner(
    tmp_path, monkeypatch, capsys,
):
    """A stopped-step recovery restores the old owner without fresh update inputs."""
    from astrid_runtime.bootstrap import BootstrapConfig
    from astrid_runtime.paths import RuntimePaths
    from runtime_protocol.store import RealmStore

    from astrid.installer.backend import Backend

    home = _set_home(tmp_path, monkeypatch)
    root, data = tmp_path / "app" / "astrid", tmp_path / "workspace"
    data.mkdir()
    paths = RuntimePaths.current(data_root=data)
    realm_root = data / "realm"
    RealmStore.initialize(realm_root, realm_id="selected").close()
    write_json(paths.catalog_path, {
        "version": 1, "selected_realm_id": "selected",
        "realms": [{"realm_id": "selected", "display_name": "Selected", "data_root": str(realm_root)}],
        "source_profiles": {},
    })
    folder = root / "versions" / "old"
    folder.mkdir(parents=True)
    python = folder / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("placeholder", encoding="utf-8")
    profile = folder / "profile.json"
    profile.write_text("{}\n", encoding="utf-8")
    old = Version(folder, python, profile, {"PATH": os.defpath})
    write_json(folder / "version.json", old.document())
    switch(root, old)
    candidate_folder = root / "versions" / "candidate"
    candidate_python = candidate_folder / "py" / "bin" / "python"
    candidate_python.parent.mkdir(parents=True)
    candidate_python.write_text("placeholder", encoding="utf-8")
    candidate_profile = candidate_folder / "profile.json"
    candidate_profile.write_text("{}\n", encoding="utf-8")
    candidate = Version(candidate_folder, candidate_python, candidate_profile,
                        {"PATH": os.defpath}, release=old.release + 1, managed_by="pinokio")
    write_json(candidate_folder / "version.json", candidate.document())
    write_json(root / "install.json", {
        "owner": LABEL, "managed_by": "pinokio", "data_root": str(data),
    })
    write_json(install_selection(home, sys.platform), {
        "owner": LABEL, "managed_by": "pinokio", "program_root": str(root),
        "data_root": str(data),
    })
    realm = {"realm_id": "selected", "realm_root": str(realm_root)}
    discovery = paths.discovery_path
    from tests.test_installer_busy_fence import publish
    publish(paths, realm_root)
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "a" * 32,
        "transaction_kind": "migration", "step": "stopped", "data_root": str(data),
        "old": old.document(), "candidate": candidate.document(), "realm": realm, "backup": None,
    })
    events = []
    from runtime_protocol import platform

    monkeypatch.setattr(platform, "process_identity", lambda pid: {
        "pid": pid, "birth_id": "birth",
    })
    boundary_calls = []

    class RecoveryBoundary:
        stopped = False

        def prepare_restart(self, **_kwargs):
            return None

        def is_pid_alive(self, _pid):
            return not self.stopped

        def process_birth_identity(self, _pid):
            return None if self.stopped else "birth"

        def validate_owner(self, **_kwargs):
            return True

        def endpoint_metadata(self, **_kwargs):
            return {"runtime_instance_id": "instance", "realm_id": "selected"}

        def stop_owner(self, **_kwargs):
            # This is the actual destructive stop seam. The shared transaction
            # lock must remain held while recovery stops and inspects Runtime.
            boundary_calls.append("stop")
            lock_path = root / ".app-update.lock"
            with lock_path.open("a+b") as contender:
                with pytest.raises(BlockingIOError):
                    platform.lock_file(contender.fileno(), blocking=False)
            discovery.unlink(missing_ok=True)
            paths.instance_lock_path.unlink(missing_ok=True)
            self.stopped = True
            events.append("down")
            return {"status": "stopped"}

    # Recovery uses the same fenced C1 stop as an ordinary down, with no
    # serialized stop receipt or launch tag in the durable journal.
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary", RecoveryBoundary)
    monkeypatch.setattr(BootstrapConfig, "resolve_source_profile", lambda *_: object())

    class RecoveryBackend(Backend):
        def __init__(self, backend_root, log, executable, *, process_control=None):
            super().__init__(backend_root, log, executable, process_control=process_control)
            self.managed_by = "pinokio"

        def existing(self, _data):
            return old

        def run(self, *_args, **_kwargs):
            pytest.fail("Recovery test attempted an external command")

        def runtime(self, _version, _data, *args, **_kwargs):
            if args == ("workspace", "inspect"):
                events.append("workspace")
                return realm
            if args == ("upgrade",):
                events.append("upgrade")
                return {}
            raise AssertionError(f"Unexpected managed Runtime command: {args}")

        def start(self, *_args):
            pytest.fail("Pinokio retry attempted to start a login service")

        def verify(self, *_args, **_kwargs):
            pytest.fail("Pinokio retry attempted to contact a browser door")

        def open(self):
            pytest.fail("Pinokio retry attempted to open a browser")

        def sync_skills(self, _version):
            events.append("sync_skills")

        def backup(self, *_args):
            events.append("backup")
            return None

        def stage(self, *_args):
            events.append("stage")
            folder = root / "versions" / "recovered"
            python = folder / "py" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("placeholder", encoding="utf-8")
            profile = folder / "profile.json"
            write_json(profile, {})
            candidate = Version(folder, python, profile, {"PATH": os.defpath})
            write_json(folder / "version.json", candidate.document())
            return candidate

        def restore(self, *_args):
            events.append("restore")

    from astrid.installer import app_update

    monkeypatch.setattr(app_update, "PinokioLifecycle", lambda _root, backend: SimpleNamespace(
        validate=lambda _version: None,
        stop=backend.stop,
        start=lambda version, _data, _realm: events.append("session_ready:" + version.root.name),
    ))

    monkeypatch.setattr(cli, "Backend", RecoveryBackend)
    monkeypatch.setattr(cli, "validate_manifest", lambda _value: {})
    assert cli.main([
        "--root", str(root), "--managed-by", "pinokio", "--update",
        "--manifest", str(tmp_path / "unavailable-manifest"),
        "--uv", str(tmp_path / "unavailable-uv"),
    ]) == 0
    capsys.readouterr()
    assert events[0] == "down"
    assert "workspace" not in events  # Recovery uses the journal's retained realm identity.
    assert not any(event in events for event in ("backup", "stage", "upgrade", "sync_skills"))
    assert "session_ready:old" in events
    assert "verify" not in events
    assert boundary_calls == ["stop"]
    assert read_json(root / "update-transaction.json")["step"] == "rolled_back"
    assert "stop_authority" not in read_json(root / "update-transaction.json")
    assert (root / "current").read_text().strip() == "old"


def test_cli_recovery_preserves_fenced_busy_refusal_before_workspace_or_backup(
    tmp_path, monkeypatch, capsys,
):
    """A blocker discovered by Runtime prevents recovery mutations in this caller lane."""
    from astrid_runtime.bootstrap import BootstrapConfig
    from astrid_runtime.paths import RuntimePaths
    from runtime_protocol import platform
    from runtime_protocol.store import RealmStore

    from astrid.installer.backend import Backend

    home = _set_home(tmp_path, monkeypatch)
    root, data = tmp_path / "app" / "astrid", tmp_path / "workspace"
    data.mkdir()
    paths = RuntimePaths.current(data_root=data)
    realm_root = data / "realm"
    RealmStore.initialize(realm_root, realm_id="selected").close()
    write_json(paths.catalog_path, {
        "version": 1, "selected_realm_id": "selected",
        "realms": [{"realm_id": "selected", "display_name": "Selected", "data_root": str(realm_root)}],
        "source_profiles": {},
    })
    with sqlite3.connect(realm_root / "realm.sqlite3") as db:
        db.execute(
            "INSERT INTO runs(id, capability, status, created_at, updated_at) "
            "VALUES ('run-busy', 'render', 'running', '2000-01-01T00:00:00Z', '2000-01-01T00:00:00Z')"
        )
        db.execute(
            "INSERT INTO tasks(id, run_id, capability, status, created_at, updated_at) "
            "VALUES ('task-busy', 'run-busy', 'render', 'running', '2000-01-01T00:00:00Z', '2000-01-01T00:00:00Z')"
        )
    folder = root / "versions" / "old"
    folder.mkdir(parents=True)
    python = folder / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("placeholder", encoding="utf-8")
    profile = folder / "profile.json"
    profile.write_text("{}\n", encoding="utf-8")
    old = Version(folder, python, profile, {"PATH": os.defpath})
    write_json(folder / "version.json", old.document())
    switch(root, old)
    candidate_folder = root / "versions" / "candidate"
    candidate_python = candidate_folder / "py" / "bin" / "python"
    candidate_python.parent.mkdir(parents=True)
    candidate_python.write_text("placeholder", encoding="utf-8")
    candidate_profile = candidate_folder / "profile.json"
    candidate_profile.write_text("{}\n", encoding="utf-8")
    candidate = Version(candidate_folder, candidate_python, candidate_profile,
                        {"PATH": os.defpath}, release=old.release + 1, managed_by="pinokio")
    write_json(candidate_folder / "version.json", candidate.document())
    write_json(root / "install.json", {
        "owner": LABEL, "managed_by": "pinokio", "data_root": str(data),
    })
    write_json(install_selection(home, sys.platform), {
        "owner": LABEL, "managed_by": "pinokio", "program_root": str(root),
        "data_root": str(data),
    })
    from tests.test_installer_busy_fence import publish
    publish(paths, realm_root)
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "b" * 32,
        "transaction_kind": "migration", "step": "stopped", "data_root": str(data),
        "old": old.document(), "candidate": candidate.document(),
        "realm": {"realm_id": "selected", "realm_root": str(realm_root)},
        "backup": None,
    })
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {})
    uv = tmp_path / "uv"
    uv.write_text("placeholder", encoding="utf-8")
    events = []

    class RefusalBackend(Backend):
        def __init__(self, backend_root, log, executable, *, process_control=None):
            super().__init__(backend_root, log, executable, process_control=process_control)
            self.managed_by = "pinokio"

        def existing(self, _data):
            return old

        def runtime(self, _version, _data, *args, **_kwargs):
            events.append(args)
            pytest.fail("Busy recovery reached a Runtime operation after stop refusal")

        def backup(self, *_args):
            events.append("backup")
            pytest.fail("Busy recovery backed up workspace after stop refusal")

        def stage(self, *_args):
            pytest.fail("Busy recovery staged a version after stop refusal")

        def restore(self, *_args):
            pytest.fail("Busy recovery restored state after stop refusal")

        def start(self, *_args):
            pytest.fail("Busy recovery started Runtime after stop refusal")

    from astrid.installer import app_update

    monkeypatch.setattr(app_update, "PinokioLifecycle", lambda _root, backend: SimpleNamespace(
        validate=lambda _version: None,
        stop=backend.stop,
        start=lambda *_args: pytest.fail("Busy recovery activated a session after refusal"),
    ))

    monkeypatch.setattr(cli, "Backend", RefusalBackend)
    monkeypatch.setattr(cli, "validate_manifest", lambda _value: {})
    monkeypatch.setattr(BootstrapConfig, "resolve_source_profile", lambda *_: object())
    monkeypatch.setattr(platform, "process_identity", lambda pid: {
        "pid": pid, "birth_id": "birth",
    })

    assert cli.main([
        "--root", str(root), "--managed-by", "pinokio", "--update",
        "--manifest", str(manifest), "--uv", str(uv),
    ]) == 1
    output = capsys.readouterr().out
    assert "Astrid is busy (1 tasks running). Try again when they finish." in output
    assert "task-busy" in output
    assert events == []
    assert read_json(root / "update-transaction.json")["step"] == "stopped"
    assert (root / "current").read_text().strip() == "old"


def test_windows_foreground_keeps_lock_open_through_inline_runtime(tmp_path, monkeypatch):
    import runtime_protocol

    home = _set_home(tmp_path, monkeypatch)
    root = tmp_path / "app"
    version_root = root / "versions" / "v1"
    version_root.mkdir(parents=True)
    python = version_root / "py" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("fake executable", encoding="utf-8")
    profile = version_root / "profile.json"
    profile.write_text("{}\n", encoding="utf-8")
    version = Version(version_root, python, profile, {"PATH": os.defpath})
    write_json(version_root / "version.json", version.document())
    write_json(root / "install.json", {
        "owner": LABEL, "managed_by": "pinokio", "data_root": str(tmp_path / "workspace"),
    })
    switch(root, version)

    acquired = {}
    original_lock = runtime_protocol.platform.lock_file

    def track_lock(fd, **kwargs):
        acquired["fd"] = fd
        return original_lock(fd, **kwargs)

    monkeypatch.setattr(runtime_protocol.platform, "lock_file", track_lock)
    output = tmp_path / "inline-runtime.txt"
    command_options = {}

    def inline_command(_version, _data, **kwargs):
        command_options.update(kwargs)
        fd = acquired["fd"]
        path = installer_lock_path(home, "win32")
        script = (
            "import os\nfrom runtime_protocol import platform\n"
            f"os.fstat({fd})\nprobe=open({str(path)!r},'a+b')\n"
            "try:\n"
            "    platform.lock_file(probe.fileno(),blocking=False)\n"
            "    raise AssertionError('foreground released its lock early')\n"
            "except BlockingIOError:\n"
            f"    open({str(output)!r},'w').write('lock-held')\n"
            "finally:\n    probe.close()\n"
        )
        return [str(python), "-I", "-c", script]

    monkeypatch.setattr(foreground, "command", inline_command)
    real_environ = os.environ
    test_environ = dict(real_environ)
    test_environ.update({"HOME": str(home), "LOCALAPPDATA": str(home / "local-app-data")})
    monkeypatch.setattr(os, "environ", test_environ)
    monkeypatch.setattr(os, "chdir", lambda _path: None)

    foreground.launch(root, [], system="win32")

    assert command_options["lock_fd"] is None
    assert output.read_text(encoding="utf-8") == "lock-held"
    path = installer_lock_path(home, "win32")
    with path.open("a+b") as fresh:
        original_lock(fresh.fileno(), blocking=False)
        runtime_protocol.platform.unlock_file(fresh.fileno())


def _foreground_fixture(tmp_path, monkeypatch):
    import runtime_protocol

    home = _set_home(tmp_path, monkeypatch)
    app_root = tmp_path / "app" / "astrid"
    version_root = app_root / "versions" / "fixture"
    environment = version_root / "py"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / "bin" / "python"
    site_packages = next((environment / "lib").glob("python*/site-packages"))
    checkout = Path(__file__).resolve().parents[1]
    protocol_root = Path(runtime_protocol.__file__).resolve().parent.parent
    (site_packages / "astrid-lock-test.pth").write_text(
        f"{checkout}\n{protocol_root}\n", encoding="utf-8"
    )
    profile = version_root / "profile.json"
    profile.write_text("{}\n", encoding="utf-8")
    version = Version(version_root, python, profile, {"PATH": os.defpath})
    write_json(version_root / "version.json", version.document())
    write_json(app_root / "install.json", {
        "owner": LABEL, "managed_by": "pinokio", "data_root": str(tmp_path / "workspace"),
    })
    switch(app_root, version)
    write_json(install_selection(home, sys.platform), {
        "owner": LABEL, "managed_by": "pinokio", "program_root": str(app_root),
        "data_root": str(tmp_path / "workspace"), "identity": version.document()["identity"],
    })
    from astrid.installer.launcher import install as install_launcher

    install_launcher(app_root)
    return home, app_root, version, site_packages


@pytest.mark.posix_native
def test_posix_run_lock_is_not_inherited_by_child_and_crash_releases_it(tmp_path, monkeypatch, capsys):
    from runtime_protocol import platform

    home, app_root, _version, site_packages = _foreground_fixture(tmp_path, monkeypatch)
    lock_path = installer_lock_path(home, sys.platform)
    result_path = tmp_path / "child-lock-fd.json"
    runtime_package = site_packages / "astrid_runtime"
    runtime_package.mkdir()
    (runtime_package / "__init__.py").write_text("", encoding="utf-8")
    (runtime_package / "cli.py").write_text(
        "import json, os, signal, subprocess, sys\n"
        "from pathlib import Path\n"
        "from runtime_protocol import platform\n"
        "def main(argv):\n"
        "    path = os.environ['ASTRID_TEST_LOCK_PATH']\n"
        "    target = os.stat(path)\n"
        "    held = []\n"
        "    for name in os.listdir('/dev/fd'):\n"
        "        try:\n"
        "            observed = os.fstat(int(name))\n"
        "        except (OSError,ValueError):\n"
        "            continue\n"
        "        if (observed.st_dev,observed.st_ino)==(target.st_dev,target.st_ino):\n"
        "            held.append(int(name))\n"
        "    assert len(held)==1, held\n"
        "    fd = held[0]\n"
        "    inheritable = os.get_inheritable(fd)\n"
        "    probe = open(path, 'a+b')\n"
        "    try:\n"
        "        platform.lock_file(probe.fileno(), blocking=False)\n"
        "        blocked = False\n"
        "        platform.unlock_file(probe.fileno())\n"
        "    except BlockingIOError:\n"
        "        blocked = True\n"
        "    child_code = (\"import json,os,sys; from pathlib import Path\\n\"\n"
        "                  \"try:\\n\"\n"
        "                  \"    stat=os.fstat(int(sys.argv[1])); target=os.stat(sys.argv[2]); present=(stat.st_dev,stat.st_ino)==(target.st_dev,target.st_ino)\\n\"\n"
        "                  \"except OSError:\\n\"\n"
        "                  \"    present=False\\n\"\n"
        "                  \"Path(sys.argv[3]).write_text(json.dumps({'lock_fd_inherited':present}),encoding='utf-8')\\n\")\n"
        "    child = subprocess.Popen([sys.executable, '-I', '-c', child_code, str(fd), path, os.environ['ASTRID_TEST_CHILD_RESULT']], close_fds=False)\n"
        "    child.wait()\n"
        "    record = {'argv': list(argv), 'pid': os.getpid(), 'lock_fd_inheritable': inheritable, 'lock_held': blocked}\n"
        "    os.write(int(os.environ['ASTRID_TEST_READY_FD']), (json.dumps(record)+'\\n').encode())\n"
        "    signal.pause()\n"
        "    return 0\n",
        encoding="utf-8",
    )

    read_fd, write_fd = os.pipe()
    helper_env = os.environ.copy()
    checkout = Path(__file__).resolve().parents[1]
    helper_env.update({"ASTRID_TEST_LOCK_PATH": str(lock_path),
                       "ASTRID_TEST_READY_FD": str(write_fd),
                       "ASTRID_TEST_CHILD_RESULT": str(result_path)})
    process = None
    try:
        process = subprocess.Popen(
            [str(app_root / "run")],
            cwd=checkout,
            env=helper_env,
            pass_fds=(write_fd,),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        os.close(write_fd)
        write_fd = -1
        line = bytearray()
        while b"\n" not in line:
            chunk = os.read(read_fd, 1)
            if not chunk:
                detail = process.stderr.read().decode("utf-8", errors="replace")
                process.wait()
                raise AssertionError(f"Foreground Runtime failed before readiness: {detail}")
            line.extend(chunk)
        record = json.loads(line)
        child_record = json.loads(result_path.read_text(encoding="utf-8"))
        assert process.pid == record["pid"]
        assert record["lock_fd_inheritable"] is False
        assert record["lock_held"] is True
        assert child_record == {"lock_fd_inherited": False}
        from astrid.installer.backend import Backend

        def refuse_unpublished_test_owner(_backend, _data):
            raise ValueError("C1 preflight refused: test owner has no Runtime custody report.")

        monkeypatch.setattr(Backend, "preflight_stop", refuse_unpublished_test_owner)
        assert cli.main(["--update", "--root", str(app_root)]) == 1
        assert "C1 preflight refused: test owner has no Runtime custody report." in capsys.readouterr().out
        assert not (app_root / "update-transaction.json").exists()

        process.send_signal(signal.SIGKILL)
        process.wait()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as released:
            platform.lock_file(released.fileno(), blocking=False)
            platform.unlock_file(released.fileno())
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGKILL)
            process.wait()
        if process is not None and process.stderr is not None:
            process.stderr.close()
