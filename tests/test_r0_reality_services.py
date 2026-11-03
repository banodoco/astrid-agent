"""R0 login-service source snapshots and portable manager-model behavior."""
import hashlib
import plistlib
import xml.etree.ElementTree as ET

import pytest

from tests.r0_reality.services import ServiceManagerFake, service_definition


def test_authentic_reviewed_service_definitions():
    expected = {
        "darwin": "42e9ddb04d8b6293ca4bfddc6bf08fb441eca52f13d831885ffa1e99220f71dc",
        "linux": "9f98c45a8aff00545a7b7ffa7d0156b661ee5867b500a134d36ea9e4d0887903",
        "win32": "a8b7a467136c7aaef9a128cee9247198dd86bea257249cd9355b478fb5feb944",
    }
    for system, digest in expected.items():
        assert hashlib.sha256(service_definition(system)).hexdigest() == digest
    launchd = plistlib.loads(service_definition("darwin"))
    assert launchd["Label"] == "haus.astrid.runtime"
    assert launchd["KeepAlive"] and launchd["RunAtLoad"] and launchd["ExitTimeOut"] == 90
    linux = service_definition("linux").decode()
    assert "Restart=always\nRestartSec=5\nKillMode=mixed\nTimeoutStopSec=90\n" in linux
    windows = ET.fromstring(service_definition("win32"))
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    for trigger in ("LogonTrigger", "TimeTrigger"):
        assert windows.find(f"t:Triggers/t:{trigger}/t:Repetition/t:Interval", ns).text == "PT1M"
    assert windows.find("t:Settings/t:MultipleInstancesPolicy", ns).text == "IgnoreNew"
    assert windows.find("t:Settings/t:RestartOnFailure/t:Count", ns).text == "999"
    with pytest.raises(ValueError):
        service_definition("other")


def test_linux_disable_does_not_stop_or_remove_restart_until_explicit_stop():
    manager = ServiceManagerFake("linux")
    manager.register()
    manager.start()
    manager.disable()
    assert manager.running and not manager.enabled
    manager.kill()
    manager.advance(4)
    assert not manager.running
    manager.advance(1)
    assert manager.running and manager.starts == 2
    manager.stop()
    manager.advance(60)
    assert not manager.running


def test_launchd_keepalive_and_bootout_disable_restart():
    manager = ServiceManagerFake("darwin")
    manager.register()
    assert manager.running
    manager.kill()
    manager.advance(0)
    assert manager.running and manager.starts == 2
    manager.disable()
    assert not manager.running and not manager.registered
    manager.advance(120)
    assert manager.starts == 2 and manager.events[-1] == "bootout"


def test_windows_repeat_ignore_new_disable_stop_and_enable():
    manager = ServiceManagerFake("win32")
    manager.register()
    manager.start()
    manager.advance(60)
    assert manager.starts == 1 and manager.events[-2:] == ["repeat", "ignore-new"]
    manager.kill(failed=False)
    manager.advance(59)
    assert not manager.running
    manager.advance(1)
    assert manager.running and manager.starts == 2
    manager.disable()
    assert manager.running  # R0 disables and stops as separate operations.
    manager.stop()
    manager.advance(120)
    assert not manager.running
    manager.start()
    assert manager.running and manager.enabled


def test_windows_failure_retry_and_repeat_are_single_instance():
    manager = ServiceManagerFake("win32")
    manager.register()
    manager.start()
    manager.kill()
    manager.advance(59)
    assert not manager.running
    manager.advance(1)
    assert manager.running and manager.starts == 2
    assert manager.events[-1] == "ignore-new"


@pytest.mark.parametrize("system", ["linux", "darwin", "win32"])
def test_renderer_matches_r0_reviewed_samples(system):
    from pathlib import Path, PureWindowsPath
    from tests.r0_reality.render import Version, render_definition

    root = PureWindowsPath("C:/Astrid/versions/v1") if system == "win32" else Path("/astrid/versions/v1")
    python = root / ("py/Scripts/python.exe" if system == "win32" else "py/bin/python")
    env = {"PATH": "C:\\Astrid\\tools;C:\\Windows\\System32" if system == "win32" else "/astrid/tools:/usr/bin:/bin", "PYTHONUTF8": "1"}
    data = PureWindowsPath("C:/Astrid Data") if system == "win32" else Path("/astrid data")
    log = PureWindowsPath("C:/Astrid/logs/install.log") if system == "win32" else Path("/logs/install.log")
    version = Version(root, python, root / "profile.json", env)
    rendered = render_definition(system, version, data, log, user="S-1-5-21-1234", start_boundary="2000-01-01T00:00:00+00:00")
    assert rendered.encode() == service_definition(system)


def test_renderer_registration_env_file_is_part_of_command():
    from pathlib import Path
    from tests.r0_reality.render import Version, render_definition

    root = Path("/example/v1")
    version = Version(root, root / "python", root / "profile.json", {})
    text = render_definition("linux", version, Path("/example/data"), Path("/example/install.log"), env_file=Path("/example/service-env.json"))
    assert '"-c"' in text
    assert "/example/service-env.json" in text
