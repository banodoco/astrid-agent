"""Startup choice reconciliation and manual restart through the real installer."""

from __future__ import annotations

import json
import os
import plistlib
import signal
import socket
import subprocess
import sys
import time
import venv
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from runtime_protocol import platform

from astrid.installer import local_update
from astrid.installer.backend import Backend
from astrid.installer.choices import RECOMMENDED_CHOICES, resolve_choices
from astrid.installer.core import Installer, Version, current, read_json, switch, write_json
from astrid.installer.service import LABEL, Service, installer_lock_path, render
from tests.test_installer_local_update import _manifest


class StartupOS:
    """Only the service-manager subprocess boundary is modeled."""

    def __init__(self, system, *, systemd=True):
        self.system, self.systemd = system, systemd
        self.calls = []
        self.actions = None

    def __call__(self, argv):
        self.calls.append([str(part) for part in argv])
        if self.system == "linux" and not self.systemd:
            raise FileNotFoundError("no systemd user manager")
        if self.system != "win32":
            return ""
        script = argv[-1]
        if "WindowsIdentity" in script:
            return "S-1-5-21-1234"
        if "Register-ScheduledTask -TaskName" in script:
            ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
            document = ET.fromstring(self.manager.path.read_text())
            self.actions = [{
                "Execute": document.findtext(".//t:Exec/t:Command", namespaces=ns),
                "Arguments": document.findtext(".//t:Exec/t:Arguments", namespaces=ns),
            }]
        elif "Unregister-ScheduledTask" in script:
            assert "$prior -cne" in script  # Compare again at deletion.
            self.actions = None
        elif "ConvertTo-Json" in script:
            return json.dumps(self.actions) if self.actions is not None else ""
        elif "$t.TaskName" in script:
            return "Astrid Runtime" if self.actions else ""
        elif "$t.State" in script:
            return "Ready" if self.actions else ""
        return ""


def selected_python(folder):
    """A contained interpreter with only the explicitly selected test inputs."""
    import runtime_protocol

    environment = folder / "py"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    python_version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    selected_site = Path(sys.prefix) / "lib" / python_version / "site-packages"
    site = environment / "lib" / python_version / "site-packages"
    (site / "selected-inputs.pth").write_text("\n".join(map(str, (
        selected_site, Path(runtime_protocol.__file__).resolve().parents[1],
        Path(__file__).resolve().parents[1],
    ))) + "\n")
    return environment / "bin" / "python"


def manager_fixture(tmp_path, monkeypatch, system, *, systemd=True):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    root, data = tmp_path / "program", tmp_path / "data"
    folder = root / "versions" / "one"
    folder.mkdir(parents=True)
    version = Version(folder, folder / "python.exe", folder / "profile.json", {}, 7)
    write_json(folder / "version.json", version.document())
    runner = StartupOS(system, systemd=systemd)
    manager = Service(root, tmp_path / "install.log", runner, home=tmp_path, system=system,
                      say=lambda _: None)
    runner.manager = manager
    return manager, version, data, runner


@pytest.mark.parametrize("system", ["linux", "darwin", "win32"])
@pytest.mark.parametrize("choice", [None, True, False])
def test_registration_uses_default_or_saved_choice(tmp_path, monkeypatch, system, choice):
    manager, version, data, runner = manager_fixture(tmp_path, monkeypatch, system)
    if choice is not None:
        write_json(manager.selection, {"owner": LABEL, "choices": {
            **RECOMMENDED_CHOICES, "autostart": choice}, "unknown": "keep"})
    manager.register(version, data)
    manager.register(version, data)
    assert manager.path.exists() is (choice is not False)
    assert bool(runner.actions) is (system == "win32" and choice is not False)
    record = manager.recorded()
    assert record["identity"] == version.document()["identity"]
    assert record["data_root"] == str(data)
    if choice is not None:
        assert record["choices"]["autostart"] is choice
        assert record["unknown"] == "keep"
    if choice is False:
        assert not any("enable" in call or "bootstrap" in call for call in runner.calls)
        assert manager.start() is False


@pytest.mark.parametrize("system", ["linux", "darwin", "win32"])
def test_turning_off_removes_owned_registration_and_can_turn_on_again(tmp_path, monkeypatch, system):
    manager, version, data, runner = manager_fixture(tmp_path, monkeypatch, system)
    manager.choices = dict(RECOMMENDED_CHOICES)
    manager.register(version, data)
    if system == "linux":
        enabled = manager.path.parent / "default.target.wants" / manager.path.name
        enabled.parent.mkdir()
        enabled.symlink_to(manager.path)
    manager.choices = resolve_choices(explicit={"autostart": False}, saved=manager.recorded()["choices"])
    assert manager.restart_required()
    # The caller has completed C1's stop/unload before register reconciles.
    manager.register(version, data)
    manager.register(version, data)
    assert not manager.path.exists()
    assert runner.actions is None
    assert manager.recorded()["choices"]["autostart"] is False
    if system == "linux":
        assert not enabled.is_symlink()
        assert sum("disable" in call for call in runner.calls) == 1
    manager.choices = resolve_choices(explicit={"autostart": True}, saved=manager.recorded()["choices"])
    assert manager.restart_required()
    manager.register(version, data)
    assert manager.path.exists()
    if system == "win32":
        assert "<Repetition>" not in manager.path.read_text()


@pytest.mark.parametrize("system", ["linux", "darwin", "win32"])
@pytest.mark.parametrize("foreign", ["action", "selection", "symlink"])
def test_off_preserves_foreign_registration(tmp_path, monkeypatch, system, foreign):
    manager, version, data, runner = manager_fixture(tmp_path, monkeypatch, system)
    manager.register(version, data)
    if foreign == "action":
        if system == "win32":
            runner.actions[0]["Execute"] = "foreign.exe"
        else:
            manager.path.write_text(manager.path.read_text().replace(str(version.python), "/foreign/python"))
    elif foreign == "selection":
        record = manager.recorded()
        write_json(manager.selection, {**record, "program_root": str(tmp_path / "other")})
    elif system == "win32":
        runner.actions[0]["Arguments"] = "--foreign"
    else:
        target = tmp_path / "foreign-service"
        manager.path.rename(target)
        manager.path.symlink_to(target)
    before = manager.path.read_bytes(), json.dumps(runner.actions)
    manager.choices = {**RECOMMENDED_CHOICES, "autostart": False}
    with pytest.raises(ValueError, match="unknown|owner|selection|action"):
        manager.register(version, data)
    assert (manager.path.read_bytes(), json.dumps(runner.actions)) == before


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_bound_action_publication_still_requires_a_retained_version(tmp_path, monkeypatch, system):
    manager, version, data, _ = manager_fixture(tmp_path, monkeypatch, system)
    manager.register(version, data)
    (version.root / "version.json").unlink()
    if system == "darwin":
        document = plistlib.loads(manager.path.read_bytes())
        document["EnvironmentVariables"]["ASTRID_INSTALLER_OPERATION_ID"] = "a" * 32
        manager.path.write_bytes(plistlib.dumps(document))
    else:
        manager.path.write_text(manager.path.read_text().replace(
            "[Service]\n", '[Service]\nEnvironment="ASTRID_INSTALLER_OPERATION_ID=' + "a" * 32 + '"\n'))
    before = manager.path.read_bytes()
    with pytest.raises(ValueError, match="unknown or changed action/owner"):
        manager.publish_launch_admission(version, data)
    assert manager.path.read_bytes() == before


@pytest.mark.parametrize("system", ["linux", "win32"])
def test_temporary_stop_keeps_registration_and_saved_on(tmp_path, monkeypatch, system):
    manager, version, data, runner = manager_fixture(tmp_path, monkeypatch, system)
    manager.choices = dict(RECOMMENDED_CHOICES)
    manager.register(version, data)
    before = manager.path.read_bytes(), manager.selection.read_bytes()
    manager.disable()
    manager.stop()
    assert (manager.path.read_bytes(), manager.selection.read_bytes()) == before
    assert not any("disable" in call or "Unregister-ScheduledTask" in call[-1] for call in runner.calls)


@pytest.mark.parametrize("enabled", [True, False])
def test_no_systemd_writes_no_unit_and_still_starts_detached(tmp_path, monkeypatch, enabled):
    manager, version, data, _ = manager_fixture(tmp_path, monkeypatch, "linux", systemd=False)
    manager.choices = {**RECOMMENDED_CHOICES, "autostart": enabled}
    manager.register(version, data)
    assert not manager.path.exists()
    assert not manager.path.parent.exists()
    assert manager.start() is False
    spawn = Mock(return_value=SimpleNamespace(pid=123))
    monkeypatch.setattr(platform, "spawn_tree", spawn)
    monkeypatch.setattr(platform, "process_identity", lambda _: {"birth_id": "birth"})
    manager.start_foreground(version, data)
    assert spawn.call_args.kwargs["detached"] is True
    assert manager.recorded()["choices"]["autostart"] is enabled


def test_no_systemd_retires_previous_owned_unit_and_enablement(tmp_path, monkeypatch):
    manager, version, data, runner = manager_fixture(tmp_path, monkeypatch, "linux")
    manager.register(version, data)
    link = manager.path.parent / "default.target.wants" / manager.path.name
    link.parent.mkdir()
    link.symlink_to(manager.path)
    runner.systemd = False
    assert manager.restart_required()
    manager.register(version, data)
    assert not manager.path.exists()
    assert not link.is_symlink()
    assert manager.start() is False


@pytest.mark.parametrize("systemd", [True, False])
def test_off_removes_owned_enablement_when_unit_is_already_missing(tmp_path, monkeypatch, systemd):
    manager, version, data, runner = manager_fixture(tmp_path, monkeypatch, "linux")
    manager.register(version, data)
    link = manager.path.parent / "default.target.wants" / manager.path.name
    link.parent.mkdir()
    link.symlink_to(manager.path)
    manager.path.unlink()
    runner.systemd = systemd
    manager.choices = {**RECOMMENDED_CHOICES, "autostart": False}
    assert manager.restart_required()
    manager.register(version, data)
    assert not manager.path.exists()
    assert not link.is_symlink()


def test_off_preserves_foreign_enablement_link(tmp_path, monkeypatch):
    manager, version, data, _ = manager_fixture(tmp_path, monkeypatch, "linux")
    manager.register(version, data)
    link = manager.path.parent / "default.target.wants" / manager.path.name
    link.parent.mkdir()
    foreign = tmp_path / "other.service"
    foreign.write_text("foreign")
    link.symlink_to(foreign)
    before = manager.path.read_bytes()
    manager.choices = {**RECOMMENDED_CHOICES, "autostart": False}
    with pytest.raises(ValueError, match="unknown owner or target"):
        manager.register(version, data)
    assert link.resolve() == foreign
    assert foreign.read_text() == "foreign"
    assert manager.path.read_bytes() == before


def test_macos_temporary_c1_stop_keeps_saved_on_and_plist(tmp_path, monkeypatch):
    from tests.test_installer_launchd_stop_handoff import _launchd_harness, _real_c1_stop, _version
    from tests.test_installer_lifecycle import _installer_runtime, _publish_owner

    backend, data, paths, _, boundary, _ = _installer_runtime(tmp_path, monkeypatch)
    version = _version(backend, "old", 7)
    switch(backend.root, version)
    _publish_owner(paths)
    events = []
    _real_c1_stop(data, boundary, events)
    _, _, bootouts, _, _ = _launchd_harness(backend, data, version, boundary, events)
    record = backend.service.recorded()
    write_json(backend.service.selection, {**record, "choices": RECOMMENDED_CHOICES})
    before = backend.service.path.read_bytes()
    backend.stop(version, data)
    assert len(bootouts) == 1
    assert backend.service.path.read_bytes() == before
    assert backend.service.recorded()["choices"]["autostart"] is True


@pytest.mark.parametrize("system", ["linux", "darwin", "win32"])
def test_backend_manual_start_detaches_and_windows_launch_is_hidden(tmp_path, monkeypatch, system):
    manager, version, data, runner = manager_fixture(tmp_path, monkeypatch, system)
    manager.choices = {**RECOMMENDED_CHOICES, "autostart": False}
    manager.register(version, data)
    backend = Backend(manager.root, manager.log, tmp_path / "uv")
    backend.service = manager
    spawn = Mock(return_value=SimpleNamespace(pid=123))
    monkeypatch.setattr(platform, "spawn_tree", spawn)
    monkeypatch.setattr(platform, "process_identity", lambda _: {"birth_id": "birth"})
    before = manager.recorded()["choices"]
    runner.calls.clear()
    backend.start(version, data)
    argv = spawn.call_args.args[0]
    assert argv[0] == str(version.python.with_name("pythonw.exe") if system == "win32" else version.python)
    assert spawn.call_args.kwargs["detached"] is True
    assert spawn.call_args.kwargs["hidden"] is True
    assert spawn.call_args.kwargs["stdin"] == subprocess.DEVNULL
    assert manager.recorded()["choices"] == before
    assert runner.calls == []


@pytest.mark.parametrize("marker_owner", [LABEL, None, "another-installer"])
def test_choice_persistence_uses_only_an_owned_complete_install_marker(tmp_path, monkeypatch, marker_owner):
    manager, version, data, _ = manager_fixture(tmp_path, monkeypatch, "linux")
    backend = Backend(manager.root, manager.log, tmp_path / "uv")
    backend.service = manager
    manager.choices = {**RECOMMENDED_CHOICES, "autostart": False}
    if marker_owner is not None:
        write_json(manager.root / "install.json", {"owner": marker_owner, "data_root": str(data)})
    backend.persist_identity(version)
    if marker_owner == LABEL:
        record = manager.recorded()
        assert record["owner"] == LABEL
        assert record["program_root"] == str(manager.root)
        assert record["data_root"] == str(data)
        assert record["choices"] == manager.choices
    else:
        assert not manager.selection.exists()


@pytest.fixture
def real_install(tmp_path, monkeypatch):
    """A tiny real Runtime service; no provisioning, network or provider calls."""
    import runtime_protocol

    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path / "home"))
    for name, folder in (("XDG_DATA_HOME", "xdg-data"), ("XDG_CONFIG_HOME", "xdg-config"),
                         ("LOCALAPPDATA", "local-app-data")):
        monkeypatch.setenv(name, str(tmp_path / folder))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    root, data = tmp_path / "program", tmp_path / "workspace"
    folder = root / "versions" / "one"
    folder.mkdir(parents=True)
    service_script = tmp_path / "service.py"
    service_script.write_text("import pathlib,time,sys\np=pathlib.Path(sys.argv[1]);p.parent.mkdir(parents=True,exist_ok=True);p.write_text('{}');time.sleep(300)\n")
    services = folder / "services.json"
    write_json(services, {"version": 1, "services": [{
        "name": "pack-host", "argv": [sys.executable, str(service_script), str(data / "runtime/generic-host.ready.json")],
        "restart": "always", "ready": {"file": "generic-host.ready.json", "timeout_seconds": 5},
    }]})
    profile = folder / "profile.json"
    write_json(profile, {"profile": "astrid", "runtime_checkout": str(Path(runtime_protocol.__file__).resolve().parent.parent),
                         "source_checkout": str(Path(__file__).resolve().parents[1]), "service_declaration": str(services)})
    version = Version(folder, selected_python(folder), profile, {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "ASTRID_RUNTIME_DOOR_PORT": str(port),
        "ASTRID_RUNTIME_DATA_ROOT": str(data),
    }, 7)
    write_json(folder / "version.json", version.document())
    write_json(folder / "manifest.json", _manifest(7, "a" * 40, "b" * 40))
    switch(root, version)
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv", say=lambda _: None)
    backend.uv.touch()
    backend.runtime(version, data, "workspace", "create", "--realm-root", str(data / "runtime/realms/selected"),
                    "--realm-id", "selected", "--door-port", str(port), "--hosted-origin", "off", profile=True)
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    write_json(backend.service.selection, {
        "owner": LABEL, "program_root": str(root), "data_root": str(data),
        "choices": {**RECOMMENDED_CHOICES, "autostart": False},
        "identity": version.document()["identity"],
    })
    backend.register(version, data)
    assert backend.service.recorded()["owner"] == LABEL
    assert backend.service.recorded()["program_root"] == str(root)
    assert backend.service.recorded()["data_root"] == str(data)
    yield SimpleNamespace(backend=backend, version=version, root=root, data=data, tmp=tmp_path)
    selected = current(root)
    backend.stop(selected, data)
    process = backend.service._foreground_process
    if process is not None:
        process.wait(timeout=10)


@pytest.mark.linux_native
def test_yes_install_line_restarts_stopped_off_copy_and_survives_caller_exit(real_install):
    env = real_install
    saved = env.backend.service.recorded()["choices"]
    # A separate caller exercises the actual CLI, resolver, repair and Backend.
    # Only browser opening (an OS edge) is suppressed.
    ready = env.tmp / "caller-ready"
    script = (
        "import os,fcntl,termios,time,webbrowser; from pathlib import Path; "
        "os.setsid(); fcntl.ioctl(0,termios.TIOCSCTTY,0); "
        "webbrowser.open=lambda *a,**k:False; from astrid.installer.cli import main; "
        f"result=main(); assert result == 0; Path({str(ready)!r}).write_text('ready'); time.sleep(60)"
    )
    # Opening a real controlling terminal lets closing the master deliver HUP
    # to the install caller while its detached Runtime stays live.
    master, slave = os.openpty()
    caller = subprocess.Popen([sys.executable, "-I", "-c", script, "--yes", "--root", str(env.root),
                               "--uv", str(env.backend.uv), "--manifest", str(env.version.root / "manifest.json")],
                              stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    os.close(slave)
    try:
        deadline = time.monotonic() + 45
        while not ready.exists() and caller.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if not ready.exists():
            caller.terminate()
            output, _ = caller.communicate(timeout=10)
            pytest.fail("Install caller did not become ready: " + output)
        os.close(master)
        master = None
        output, _ = caller.communicate(timeout=10)
        assert caller.returncode == -signal.SIGHUP, output
        assert "Continue with this setup?" not in output
    finally:
        if master is not None:
            os.close(master)
        if caller.poll() is None:
            caller.kill()
        caller.wait(timeout=10)
    discovery = read_json(env.data / "runtime/discovery.json")
    pid = discovery["pid"]
    assert platform.process_identity(pid) is not None
    assert os.getsid(pid) == pid  # Independent of the exited install caller.
    assert env.backend.service.recorded()["choices"] == saved
    assert not env.backend.service.path.exists()
    env.backend.verify(env.version, env.data, "selected", timeout=5)


@pytest.mark.posix_native
@pytest.mark.parametrize("route", ["repair", "update", "rollback", "recovery", "local-update", "local-rollback"])
def test_saved_off_reconciles_all_activation_routes_with_real_backend(real_install, monkeypatch, route):
    env = real_install
    backend, version, root, data = env.backend, env.version, env.root, env.data
    backend.start(version, data)
    backend.verify(version, data, "selected", timeout=10)
    # Retained residue from an older installer must disappear on reconciliation.
    backend.service.path.parent.mkdir(parents=True, exist_ok=True)
    backend.service.path.write_text(render(backend.service.system, version, data, backend.log,
                                          env_file=backend.service.env_file))
    installer = Installer(root, data, backend, say=lambda _: None)
    if route == "repair":
        monkeypatch.setattr("webbrowser.open", lambda *_args, **_kwargs: False)
        installer.run("install", {"release": 7})
    elif route.startswith("local"):
        # The local adapter's candidate and previous-version callbacks share
        # this composition root, which reads saved choices on a fresh Backend.
        backend.stop(version, data)
        fresh = Backend(root, backend.log, backend.uv)
        hooks = SimpleNamespace(call=lambda *_args, **_kwargs: None)
        local_update._activate_backend(fresh, version, data, fresh.workspace(version, data), hooks)
    else:
        realm = backend.workspace(version, data)
        candidate_root = root / "versions" / "two"
        write_json(candidate_root / "profile.json", read_json(version.profile))
        candidate = Version(candidate_root, selected_python(candidate_root), candidate_root / "profile.json",
                            version.env, version.release)
        write_json(candidate_root / "version.json", candidate.document())
        class ControllerExit(BaseException):
            pass

        def activate(selected, _data, selected_realm):
            installer._activate(selected, selected_realm["realm_id"])
            if route == "rollback":
                raise ValueError("candidate readiness failed")
            if route == "recovery":
                raise ControllerExit()

        def transaction():
            installer._run_update_transaction(
                version, realm, prepare=lambda: candidate, stop=backend.stop,
                switch_candidate=lambda selected: switch(root, selected),
                switch_old=lambda old, _: switch(root, old), activate=activate,
                activate_old=lambda old, _, selected: installer._activate(old, selected["realm_id"]),
                restore_workspace=False,
            )
        lock_path = installer_lock_path(Path.home(), sys.platform)
        with lock_path.open("a+b") as lock:
            platform.lock_file(lock.fileno(), blocking=False)
            try:
                if route == "update":
                    transaction()
                elif route == "rollback":
                    with pytest.raises(ValueError, match="candidate readiness failed"):
                        transaction()
                    assert read_json(root / "update-transaction.json")["step"] == "rolled_back"
                else:
                    with pytest.raises(ControllerExit):
                        transaction()
                    fresh = Backend(root, backend.log, backend.uv)
                    Installer(root, data, fresh, say=lambda _: None)._recover_update()
                    assert read_json(root / "update-transaction.json")["step"] == "rolled_back"
            finally:
                platform.unlock_file(lock.fileno())
    assert not backend.service.path.exists()
    assert backend.service.recorded()["choices"]["autostart"] is False
    backend.verify(current(root), data, "selected", timeout=5)


@pytest.mark.posix_native
def test_pinokio_registration_preserves_external_startup_ownership(real_install):
    env = real_install
    backend = Backend(env.root, env.backend.log, env.backend.uv, managed_by="pinokio")
    original = env.backend.service.recorded()
    backend.service.path.parent.mkdir(parents=True, exist_ok=True)
    backend.service.path.write_text("external startup object")
    try:
        backend.register(env.version, env.data)
        assert backend.service.path.read_text() == "external startup object"
        assert backend.service.recorded()["managed_by"] == "pinokio"
    finally:
        # Restore the test's original manager before ordinary fixture teardown.
        backend.service.path.unlink()
        write_json(env.version.root / "version.json", env.version.document())
        write_json(env.backend.service.selection, original)
