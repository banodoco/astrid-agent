"""Adopt the frozen registered release-0 login-service definitions."""

import hashlib
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock

import pytest

from astrid.installer import service
from astrid.installer.core import Version, read_json, write_json
from astrid.installer.service import LABEL, Service, command, render

FIXTURES = Path(__file__).parent / "fixtures" / "release0" / "services"
CHECKSUMS = Path(__file__).parent / "fixtures" / "release0" / "SHA256SUMS"
WINDOWS_NS = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _frozen(name: str) -> bytes:
    """Read the immutable fixture and assert it still has Q's registered bytes."""
    expected = {
        row.split(maxsplit=1)[1]: row.split(maxsplit=1)[0]
        for row in CHECKSUMS.read_text(encoding="utf-8").splitlines()
    }
    relative = f"services/{name}"
    value = (FIXTURES / name).read_bytes()
    assert hashlib.sha256(value).hexdigest() == expected[relative]
    return value


def _selected(root: Path, data: Path, *, windows: bool) -> Version:
    folder = root / "versions" / "v1"
    folder.mkdir(parents=True)
    environment = ({"PATH": r"C:\Astrid\tools;C:\Windows\System32", "PYTHONUTF8": "1"}
                   if windows else {"PATH": "/astrid/tools:/usr/bin:/bin", "PYTHONUTF8": "1"})
    python = folder / ("py/Scripts/python.exe" if windows else "py/bin/python")
    version = Version(folder, python, folder / "profile.json", environment, 0)
    write_json(folder / "version.json", version.document())
    (root / "current").write_text("v1\n", encoding="utf-8")
    return version


def _portable_posix_definition(fixture: bytes, version: Version, data: Path, log: Path) -> bytes:
    return (fixture.replace(b"/astrid/versions/v1", str(version.root).encode())
            .replace(b"/astrid data", str(data).encode())
            .replace(b"/logs/runtime-service.log", str(log).encode()))


def _portable_windows_definition(fixture: bytes, version: Version, data: Path, log: Path) -> bytes:
    replacements = (
        (br"C:\Astrid\versions\v1\py\Scripts\pythonw.exe",
         str(version.python.with_name("pythonw.exe")).encode()),
        (br"C:\\Astrid\\versions\\v1\\service-env.json",
         str(version.root / "service-env.json").encode()),
        (br"C:\\Astrid\\versions\\v1\\profile.json", str(version.profile).encode()),
        (br"C:\Astrid\versions\v1", str(version.root).encode()),
        (br"C:\\Astrid\\versions\\v1", str(version.root).encode()),
        (br"C:\\Astrid\\logs\\runtime-service.log", str(log).encode()),
        (br"C:\\Astrid Data", str(data).encode()),
    )
    for old, new in replacements:
        fixture = fixture.replace(old, new)
    return fixture


def _posix_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: str):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root, data = tmp_path / "program", tmp_path / "data"
    version = _selected(root, data, windows=False)
    runner = Mock(return_value="")
    manager = Service(root, tmp_path / "log", runner, home=tmp_path, system=system)
    if system == "darwin":
        monkeypatch.setattr(service.platform, "current_user", lambda: os.getuid())
    write_json(manager.selection, {"owner": LABEL, "program_root": str(root), "data_root": str(data)})
    legacy_settings = _frozen("service-env.json")
    (version.root / "service-env.json").write_bytes(legacy_settings)
    fixture = _frozen("launchd.plist" if system == "darwin" else "systemd.service")
    definition = _portable_posix_definition(fixture, version, data, manager.log)
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    manager.path.write_bytes(definition)
    return manager, version, data, definition, legacy_settings


@pytest.mark.parametrize("system", ["darwin", "linux"])
def test_release0_posix_service_is_adopted_and_freshly_registered(tmp_path, monkeypatch, system):
    manager, version, data, legacy, legacy_settings = _posix_service(tmp_path, monkeypatch, system)

    manager.publish_launch_admission(version, data)

    # Admission uses the frozen release-0 sidecar but does not create the
    # release-1 sidecar until registration.
    assert manager.path.read_bytes() != legacy
    assert (version.root / "service-env.json").read_bytes() == legacy_settings
    assert not manager.env_file.exists()

    manager.register(version, data)

    assert manager.path.read_text(encoding="utf-8") == render(
        system, version, data, manager.log, env_file=manager.env_file,
    )
    assert read_json(manager.env_file) == {}
    calls = [call.args[0] for call in manager.run.call_args_list]
    if system == "darwin":
        assert calls == [["launchctl", "bootstrap", f"gui/{os.getuid()}", manager.path]]
    else:
        assert calls == [
            ["systemctl", "--user", "show-environment"],
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "show-environment"],
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "astrid.service"],
        ]


@pytest.mark.parametrize("system", ["darwin", "linux"])
def test_tampered_release0_posix_service_refuses_before_mutation(tmp_path, monkeypatch, system):
    manager, version, data, _legacy, legacy_settings = _posix_service(tmp_path, monkeypatch, system)
    tampered = manager.path.read_bytes().replace(str(version.python).encode(), b"/foreign-python", 1)
    manager.path.write_bytes(tampered)
    manager.stop, manager.unregister = Mock(), Mock()

    with pytest.raises(ValueError, match="unknown or changed action/owner"):
        manager.publish_launch_admission(version, data)

    assert manager.path.read_bytes() == tampered
    assert (version.root / "service-env.json").read_bytes() == legacy_settings
    assert not manager.env_file.exists()
    assert manager.run.call_args_list == []
    manager.stop.assert_not_called()
    manager.unregister.assert_not_called()
    assert not list(manager.path.parent.glob(".astrid-*"))


def _windows_service(tmp_path: Path):
    root, data = tmp_path / "program", tmp_path / "data"
    version = _selected(root, data, windows=True)
    manager = Service(root, tmp_path / "log", Mock(), home=tmp_path, system="win32")
    write_json(manager.selection, {"owner": LABEL, "program_root": str(root), "data_root": str(data)})
    legacy_settings = _frozen("service-env.json")
    (version.root / "service-env.json").write_bytes(legacy_settings)
    definition = _portable_windows_definition(_frozen("task.xml"), version, data, manager.log)
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    manager.path.write_bytes(definition)
    root_xml = ET.fromstring(definition)
    action = root_xml.find(".//task:Actions/task:Exec", WINDOWS_NS)
    assert action is not None
    stored = [{"Execute": action.findtext("task:Command", namespaces=WINDOWS_NS),
               "Arguments": action.findtext("task:Arguments", namespaces=WINDOWS_NS)}]
    return manager, version, data, definition, legacy_settings, stored


def test_release0_windows_task_is_adopted_and_freshly_registered(tmp_path):
    manager, version, data, legacy, legacy_settings, stored = _windows_service(tmp_path)
    calls, writes = [], []
    canonical = command(version, data, windows=True, log=manager.log, env_file=manager.env_file)

    def powershell(script):
        calls.append(script)
        if "Set-ScheduledTask" in script:
            writes.append(script)
            stored[:] = [{"Execute": canonical[0],
                          "Arguments": subprocess.list2cmdline(canonical[1:])}]
            return ""
        if "Register-ScheduledTask" in script:
            writes.append(script)
            return ""
        if "WindowsIdentity" in script:
            return "S-1-5-21-1234"
        return json.dumps(stored, separators=(",", ":"))

    manager.powershell = powershell
    manager.publish_launch_admission(version, data)

    assert len(writes) == 1 and "Set-ScheduledTask" in writes[0]
    assert manager.path.read_bytes() == legacy
    assert (version.root / "service-env.json").read_bytes() == legacy_settings
    assert not manager.env_file.exists()

    manager.register(version, data)

    assert manager.path.read_text(encoding="utf-8") == render(
        "win32", version, data, manager.log, user="S-1-5-21-1234", env_file=manager.env_file,
    )
    assert read_json(manager.env_file) == {}
    assert len(writes) == 2 and "Register-ScheduledTask" in writes[1]
    assert all("Unregister-ScheduledTask" not in value and "Stop-ScheduledTask" not in value
               for value in calls)


def test_tampered_release0_windows_task_refuses_before_mutation(tmp_path):
    manager, version, data, legacy, legacy_settings, stored = _windows_service(tmp_path)
    tampered = legacy.replace(
        str(version.python.with_name("pythonw.exe")).encode(), b"/foreign-pythonw.exe", 1,
    )
    manager.path.write_bytes(tampered)
    root_xml = ET.fromstring(tampered)
    stored[0]["Execute"] = root_xml.findtext(".//task:Actions/task:Exec/task:Command", namespaces=WINDOWS_NS)
    calls = []

    def powershell(script):
        calls.append(script)
        return json.dumps(stored, separators=(",", ":"))

    manager.powershell = powershell
    manager.stop, manager.unregister = Mock(), Mock()

    with pytest.raises(ValueError, match="scheduled task has an unknown action"):
        manager.publish_launch_admission(version, data)

    assert manager.path.read_bytes() == tampered
    assert (version.root / "service-env.json").read_bytes() == legacy_settings
    assert not manager.env_file.exists()
    assert all("Set-ScheduledTask" not in value and "Register-ScheduledTask" not in value
               and "Stop-ScheduledTask" not in value and "Unregister-ScheduledTask" not in value
               for value in calls)
    manager.stop.assert_not_called()
    manager.unregister.assert_not_called()
