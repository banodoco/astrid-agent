"""Manager fakes exercise the R0/R1 boundary without touching host services."""
import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from astrid.installer.boundary_registration import (
    BoundaryRegistration, R0_LINUX, R1_LINUX, R0_MAC, R1_MAC, R0_WINDOWS, R1_WINDOWS,
)

FIXTURES = Path(__file__).parent / "fixtures" / "boundary_r0"


class Manager:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.enabled = {"r0": False, "r1": False}
        self.running = {"r0": False, "r1": False}

    def __call__(self, argv):
        text = " ".join(map(str, argv))
        self.calls.append(text)
        if self.fail and self.fail in text:
            raise subprocess.CalledProcessError(1, argv)
        if "systemctl" in text:
            if "enable --now " in text:
                key = "r1" if R1_LINUX in text else "r0"
                self.enabled[key] = self.running[key] = True
            elif "disable --now " in text:
                self.enabled["r1"] = self.running["r1"] = False
        elif "launchctl" in text:
            key = "r1" if R1_MAC in text else "r0"
            if " enable " in text: self.enabled[key] = True
            if " disable " in text: self.enabled[key] = False
            if " bootstrap " in text: self.running[key] = True
            if " bootout " in text: self.running[key] = False
        elif "Register-ScheduledTask" in text:
            self.enabled["r1"] = self.running["r1"] = True
        elif "Disable-ScheduledTask" in text:
            self.enabled["r1"] = self.running["r1"] = False
        elif "Enable-ScheduledTask" in text:
            self.enabled["r0"] = self.running["r0"] = True
        return ""


@pytest.fixture(params=["linux", "darwin", "win32"])
def scenario(tmp_path, request):
    system = request.param
    root, home = tmp_path / "program", tmp_path / "home"
    old, new = tmp_path / "data", tmp_path / "adopted-r1"
    old.mkdir(); new.mkdir()
    (old / "state").write_text("R0 original")
    (new / "state").write_text("R1 accepted work")
    exe = root / "versions" / "v1" / "bin" / ("runtime.exe" if system == "win32" else "runtime")
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\nprintf '%s\\n' v1 \"$@\"\n")
    exe.chmod(0o755)
    (root / "current").write_text("v1\n")
    manager = Manager()
    service = BoundaryRegistration(root, home, system, manager, uid="501")
    if system != "win32":
        service.r0_path.parent.mkdir(parents=True, exist_ok=True)
        fixture = "linux.service" if system == "linux" else "macos.plist"
        service.r0_path.write_bytes((FIXTURES / fixture).read_bytes())
        original = service.r0_path.read_bytes()
    else:
        # This is the actual evidence, not a fabricated Task Scheduler export.
        original = (FIXTURES / "windows.txt").read_bytes()
        assert b"definition unavailable" in original
    return service, manager, old, new, original


def test_registration_and_rollback_keep_r0_and_both_data_roots(scenario, tmp_path):
    service, manager, old, new, original = scenario
    service.register(old, new, tmp_path / "runtime.log", user="S-1-5-21")
    assert manager.enabled == {"r0": False, "r1": True}
    assert manager.running == {"r0": False, "r1": True}
    definition = service.r1_path.read_text()
    assert str(new) in definition
    assert str(old) not in definition
    assert str(service.launcher) in definition
    if service.system == "linux":
        assert R1_LINUX in manager.calls[-1] and R0_LINUX not in manager.calls[-1]
    elif service.system == "darwin":
        plist = plistlib.loads(service.r1_path.read_bytes())
        assert plist["Label"] == R1_MAC
        assert plist["ProgramArguments"][0] == str(service.launcher)
    else:
        assert R1_WINDOWS in manager.calls[-1]
        assert "-Force" not in manager.calls[-1]
        assert R0_WINDOWS + "'" not in manager.calls[-1]
    manager.calls.clear()
    def wait():
        assert len(manager.calls) == (2 if service.system == "darwin" else 1)
        manager.calls.append("lock free")
    service.rollback(wait, lambda: None)
    assert manager.enabled == {"r0": True, "r1": False}
    assert manager.running == {"r0": True, "r1": False}
    calls = "\n".join(manager.calls)
    assert calls.index("lock free") < calls.rindex("enable" if service.system != "win32" else "Enable-ScheduledTask")
    assert (old / "state").read_text() == "R0 original"
    assert (new / "state").read_text() == "R1 accepted work"
    if service.r0_path:
        assert service.r0_path.read_bytes() == original


def test_rollback_does_not_enable_r0_if_stop_or_lock_wait_fails(scenario):
    service, manager, *_ = scenario
    manager.fail = {"linux": "disable --now", "darwin": "bootout", "win32": "Stop-ScheduledTask"}[service.system]
    with pytest.raises(subprocess.CalledProcessError):
        service.rollback(lambda: None, lambda: None)
    assert not any((R0_LINUX in s or R0_MAC in s and R1_MAC not in s or
                    "Enable-ScheduledTask -TaskName 'Astrid Runtime'" in s) for s in manager.calls)
    manager.calls.clear(); manager.fail = None
    def busy():
        raise TimeoutError("R1 still owns its lock")
    with pytest.raises(TimeoutError):
        service.rollback(busy, lambda: None)
    assert not any((R0_LINUX in s or R0_MAC in s and R1_MAC not in s or
                    "Enable-ScheduledTask -TaskName 'Astrid Runtime'" in s) for s in manager.calls)


def test_refuses_unadopted_or_existing_r1(scenario, tmp_path):
    service, manager, old, new, _ = scenario
    with pytest.raises(ValueError):
        service.register(old, old, tmp_path / "log", user="sid")
    assert not manager.calls
    service.register(old, new, tmp_path / "log", user="sid")
    before = service.r1_path.read_bytes()
    with pytest.raises(FileExistsError):
        service.register(old, new, tmp_path / "log", user="sid")
    assert service.r1_path.read_bytes() == before


def test_posix_fixed_launcher_tracks_current_without_registration_rewrite(scenario, tmp_path):
    service, _, old, new, _ = scenario
    if service.system == "win32":
        service.register(old, new, tmp_path / "log", user="sid")
        wrapper = service.launcher.read_text()
        assert "current" in wrapper and "versions/" in wrapper and "runtime.exe" in wrapper
        return
    service.register(old, new, tmp_path / "log", user="sid")
    service_before = service.r1_path.read_bytes()
    one = subprocess.check_output([str(service.launcher), "argument with spaces"], text=True).splitlines()
    second = service.root / "versions/v2/bin/runtime"
    second.parent.mkdir(parents=True)
    second.write_text("#!/bin/sh\nprintf '%s\\n' v2 \"$@\"\n")
    second.chmod(0o755)
    (service.root / "current").write_text("v2\n")
    two = subprocess.check_output([str(service.launcher), "argument with spaces"], text=True).splitlines()
    assert one == ["v1", "argument with spaces"]
    assert two == ["v2", "argument with spaces"]
    assert service.r1_path.read_bytes() == service_before
    (service.root / "current").write_text("../escape\n")
    assert subprocess.run([str(service.launcher)], check=False).returncode != 0


def test_refuses_service_or_log_inside_r0(scenario):
    service, manager, old, new, _ = scenario
    with pytest.raises(ValueError, match="outside R0"):
        service.register(old, new, old / "runtime.log", user="sid")
    assert not manager.calls
    assert not service.launcher.exists()


def test_rejects_missing_version_launcher_before_service_changes(scenario, tmp_path):
    service, manager, old, new, _ = scenario
    (service.root / "current").write_text("not-installed\n")
    with pytest.raises(ValueError, match="no Runtime launcher"):
        service.register(old, new, tmp_path / "log", user="sid")
    assert not manager.calls
    assert not service.r1_path.exists()


def test_rollback_refuses_changed_r0_before_reenable(scenario):
    service, manager, *_ = scenario
    checks = []
    def validate():
        checks.append(True)
        if len(checks) == 2:
            raise ValueError("R0 entry changed")
    with pytest.raises(ValueError, match="changed"):
        service.rollback(lambda: None, validate)
    assert len(checks) == 2
    assert not any("enable --now astrid.service" in c or
                   "enable gui/501/haus.astrid.runtime" in c or
                   "Enable-ScheduledTask -TaskName 'Astrid Runtime'" in c
                   for c in manager.calls)


@pytest.mark.parametrize("pointer", [" leading", "v1 ", "-v1", "v" * 65])
def test_pointer_must_match_installer_format(scenario, tmp_path, pointer):
    service, manager, old, new, _ = scenario
    (service.root / "current").write_text(pointer + "\n")
    with pytest.raises(ValueError):
        service.register(old, new, tmp_path / "log", user="sid")
    assert not manager.calls
    assert not service.launcher.exists()


def test_existing_definition_does_not_create_launcher(scenario, tmp_path):
    service, manager, old, new, _ = scenario
    service.r1_path.parent.mkdir(parents=True, exist_ok=True)
    service.r1_path.write_text("existing")
    with pytest.raises(FileExistsError):
        service.register(old, new, tmp_path / "log", user="sid")
    assert not service.launcher.exists()
    assert not manager.calls
