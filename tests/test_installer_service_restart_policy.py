"""Failure-only service restart policy and preserved autostart selection."""

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrid.installer import service
from astrid.installer.core import Version, read_json, write_json


@pytest.mark.parametrize("system", ["darwin", "linux", "win32"])
def test_service_definitions_classify_orderly_exit_as_stop_and_failure_as_restart(system):
    root = Path("C:/Astrid/versions/v1") if system == "win32" else Path("/astrid/versions/v1")
    python = root / ("py/Scripts/python.exe" if system == "win32" else "py/bin/python")
    version = Version(root, python, root / "profile.json", {})
    rendered = service.render(system, version, Path("/data"), Path("/logs/install.log"),
                              user="S-1-5-21-1234")

    if system == "darwin":
        import plistlib

        keep_alive = plistlib.loads(rendered.encode())["KeepAlive"]
        assert keep_alive == {"SuccessfulExit": False}
    elif system == "linux":
        assert "Restart=on-failure\n" in rendered
        assert "Restart=always" not in rendered
    else:
        import xml.etree.ElementTree as ET

        root_xml = ET.fromstring(rendered)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        assert root_xml.find("t:Settings/t:RestartOnFailure", ns) is not None
        assert root_xml.find("t:Triggers/t:TimeTrigger", ns) is None
        assert root_xml.find("t:Triggers/t:LogonTrigger/t:Repetition", ns) is None


@pytest.mark.parametrize("saved_choice", [False, True])
@pytest.mark.parametrize("system", ["darwin", "linux", "win32"])
def test_explicit_stop_preflight_keeps_registration_and_saved_choice(tmp_path, system, saved_choice):
    manager = service.Service(tmp_path / "program", tmp_path / "log", Mock(),
                              home=tmp_path, system=system)
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    manager.path.write_text("registered", encoding="utf-8")
    write_json(manager.selection, {"owner": service.LABEL,
                                  "preferences": {"autostart": saved_choice, "other": "retained"}})
    before = manager.selection.read_bytes()

    if system == "darwin":
        with pytest.raises(RuntimeError, match="C1 stop callback"):
            manager.disable()
    else:
        manager.disable()

    assert manager.path.read_text(encoding="utf-8") == "registered"
    assert manager.selection.read_bytes() == before
    assert read_json(manager.selection)["preferences"]["autostart"] is saved_choice


def test_darwin_stop_reload_and_explicit_start_keep_launchagent_for_next_login(tmp_path, monkeypatch):
    from tests.test_installer_lifecycle import _installer_runtime, _publish_owner
    from tests.test_installer_launchd_stop_handoff import _launchd_harness, _real_c1_stop, _version

    # The plist this test writes is owned by the real process user, and the
    # installer's ownership check and gui domain both name platform.current_user().
    owner = service.platform.current_user()
    current_user = Mock(return_value=owner)
    monkeypatch.setattr(service.platform, "current_user", current_user)
    backend, data, paths, _realm, boundary, _manager_calls = _installer_runtime(tmp_path, monkeypatch)
    version = _version(backend, "old", 17)
    events = []
    _real_c1_stop(data, boundary, events)
    _loaded, _observations, bootouts, target, _crash = _launchd_harness(
        backend, data, version, boundary, events)
    manager = backend.service
    record = read_json(manager.selection)
    record["preferences"] = {"autostart": True}
    write_json(manager.selection, record)
    _publish_owner(paths)

    manager.register(version, data)
    assert manager.start()
    backend.stop(version, data)
    manager.register(version, data)
    assert manager.start()

    assert target == f"gui/{owner}/{service.LABEL}"
    assert bootouts == [("launchctl", "bootout", target)]
    assert events.index("c1-stop-owner") < events.index("launchctl-bootout")
    assert events.count("launchctl-bootstrap") == events.count("launchctl-kickstart") == 2
    assert manager.path.exists()  # launchd auto-loads this plist at next login.
    assert read_json(manager.selection)["preferences"]["autostart"] is True


@pytest.mark.parametrize("system", ["darwin", "linux", "win32"])
def test_off_choice_stop_does_not_create_startup_registration(tmp_path, system):
    calls = []
    manager = service.Service(tmp_path / "program", tmp_path / "log",
                              lambda argv: calls.append(argv) or "",
                              home=tmp_path, system=system)
    write_json(manager.selection, {"owner": service.LABEL,
                                  "preferences": {"autostart": False}})
    before = manager.selection.read_bytes()

    manager.disable()
    manager.stop()

    assert not manager.path.exists()
    assert manager.selection.read_bytes() == before
    commands = " ".join(str(part) for argv in calls for part in argv)
    for mutation in ("bootstrap", "Register-ScheduledTask", "Enable-ScheduledTask", "daemon-reload"):
        assert mutation not in commands


@pytest.mark.parametrize("case,expected", [
    ("orderly_stop", 0), ("startup_failure", 1),
    ("running_failure", 1), ("shutdown_failure", 1),
])
def test_foreground_runtime_exit_code_classifies_orderly_and_failure_paths(
    tmp_path, monkeypatch, case, expected,
):
    """Exercise the installed Runtime CLI classifier with a controlled boundary."""
    from astrid_runtime import cli

    stop_handler = {}

    class Boundary:
        def __init__(self):
            self._log_directory = None
            self.checks = 0

        def check_running(self):
            self.checks += 1
            if case == "startup_failure" and self.checks == 1:
                raise RuntimeError("simulated foreground failure")
            if case == "running_failure" and self.checks == 2:
                raise RuntimeError("simulated running failure")
            if case in {"orderly_stop", "shutdown_failure"} and self.checks == 1:
                stop_handler["callback"]()

        def stop(self):
            if case == "shutdown_failure":
                raise RuntimeError("simulated shutdown failure")
            return None

    class Log:
        def __init__(self, _path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(cli, "ForegroundRuntimeBoundary", Boundary)
    monkeypatch.setattr(cli, "bootstrap", lambda *_args: SimpleNamespace(
        door=None, door_endpoint=None, door_origin=None,
    ))
    monkeypatch.setattr(cli, "_emit_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.platform, "log_dir", lambda _home: tmp_path / "logs")
    monkeypatch.setattr(cli.platform, "owner_only", lambda _path: None)
    monkeypatch.setattr(cli.platform, "install_stop_handlers", lambda callback: (
        stop_handler.update(callback=callback) or (lambda: None)
    ))
    monkeypatch.setattr(cli.cancellation, "startup_scope", lambda _event: nullcontext())
    monkeypatch.setattr(cli.cancellation, "check", lambda: None)
    from runtime_protocol import logs

    monkeypatch.setattr(logs, "RotatingLog", Log)

    assert cli._run_foreground(SimpleNamespace(home=tmp_path), SimpleNamespace(door=None),
                               json_mode=True) == expected
