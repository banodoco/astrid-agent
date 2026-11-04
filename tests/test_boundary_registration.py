"""Manager fakes exercise the R0/R1 boundary without touching host services."""
import json
import os
import sys
import xml.etree.ElementTree as ET
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
        if "--property=LoadState" in text: return "not-found\n"
        if text == "launchctl list": return "PID\tStatus\tLabel\n"
        return ""


@pytest.fixture(params=["linux", "darwin", "win32"])
def scenario(tmp_path, request):
    system = request.param
    root, home = tmp_path / "program space $% & 'quoted'", tmp_path / "home"
    old, new = tmp_path / "data", tmp_path / "adopted-r1"
    old.mkdir(); new.mkdir()
    (old / "state").write_text("R0 original")
    (new / "state").write_text("R1 accepted work")
    folder = root / "versions" / "v1"
    exe = folder / "py" / ("Scripts/python.exe" if system == "win32" else "bin/python")
    exe.parent.mkdir(parents=True)
    exe.write_text("interpreter")
    if system == "win32":
        exe.with_name("pythonw.exe").write_text("windowed interpreter")
    profile = folder / "profile.json"
    profile.write_text("{}")
    record = {"root": str(folder), "python": str(exe), "profile": str(profile),
              "env": {"ASTRID_RUNTIME_DATA_ROOT": str(old), "PATH": "selected path $ %",
                      "ASTRID_HOST_EXECUTION_PROFILE": "installed", "ASTRID_SOURCE_PROVENANCE_ROOT": str(folder / "a")},
              "identity": {"release": 0, "managed_by": None}}
    (folder / "version.json").write_text(json.dumps(record))
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
    assert "source-manifest" in definition
    assert not service.launcher.exists()
    if service.system == "linux":
        assert R1_LINUX in manager.calls[-1] and R0_LINUX not in manager.calls[-1]
    elif service.system == "darwin":
        plist = plistlib.loads(service.r1_path.read_bytes())
        assert plist["Label"] == R1_MAC
        assert plist["ProgramArguments"][0].endswith("/py/bin/python")
        assert plist["EnvironmentVariables"]["ASTRID_RUNTIME_DATA_ROOT"] == str(new)
    else:
        assert R1_WINDOWS in manager.calls[-1]
        assert "-Force" not in manager.calls[-1]
        assert R0_WINDOWS + "'" not in manager.calls[-1]
    manager.calls.clear()
    def wait():
        assert len(manager.calls) == (2 if service.system == "darwin" else 1)
        manager.calls.append("lock free")
    service.rollback(wait)
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
        service.rollback(lambda: None)
    assert not any((R0_LINUX in s or R0_MAC in s and R1_MAC not in s or
                    "Enable-ScheduledTask -TaskName 'Astrid Runtime'" in s) for s in manager.calls)
    manager.calls.clear(); manager.fail = None
    def busy():
        raise TimeoutError("R1 still owns its lock")
    with pytest.raises(TimeoutError):
        service.rollback(busy)
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


def test_pinned_command_executes_with_selected_environment_and_detects_drift(scenario, tmp_path):
    from astrid.installer.boundary_registration import _command, _selected
    from astrid.installer.core import Version
    service, _, old, new, _ = scenario
    selected, name, digest = _selected(service.root, service.system)
    env = {**selected.env, "ASTRID_RUNTIME_DATA_ROOT": str(new), "ASTRID_RUNTIME_SOURCE_MANIFEST": str(selected.profile)}
    selected = Version(selected.root, selected.python, selected.profile, env, selected.release, selected.managed_by)
    argv = _command(selected, new, service.root, name, digest, tmp_path / "runtime.log", service.system)
    module = tmp_path / "astrid_runtime"
    module.mkdir()
    (module / "__init__.py").write_text("")
    (module / "cli.py").write_text("import json,os\ndef main(args):\n open(os.environ['CAPTURE'], 'w').write(json.dumps([args, dict(os.environ)]))\n return 0\n")
    capture = tmp_path / "capture.json"
    # Exercise the generated Python body with a recording Runtime module.
    code = f"import sys; sys.path.insert(0, {str(tmp_path)!r})\n" + argv[-1]
    child_env = {**os.environ, "CAPTURE": str(capture), "ASTRID_RUNTIME_DATA_ROOT": str(old)}
    subprocess.run([sys.executable, "-c", code], env=child_env, check=True)
    args, actual = json.loads(capture.read_text())
    assert args == ["run", "--profile", "astrid", "--data-root", str(new), "--source-manifest", str(selected.profile)]
    for key, value in env.items():
        assert actual[key] == value
    capture.unlink()
    (service.root / "current").write_text("v2\n")
    assert subprocess.run([sys.executable, "-c", code], env=child_env, capture_output=True).returncode != 0
    assert not capture.exists()
    (service.root / "current").write_text("v1\n")
    (selected.root / "version.json").write_text("{}")
    assert subprocess.run([sys.executable, "-c", code], env=child_env, capture_output=True).returncode != 0
    assert not capture.exists()


def test_refuses_service_or_log_inside_r0(scenario):
    service, manager, old, new, _ = scenario
    with pytest.raises(ValueError, match="outside R0"):
        service.register(old, new, old / "runtime.log", user="sid")
    assert not manager.calls
    assert not service.r1_path.exists()


def test_rejects_missing_version_launcher_before_service_changes(scenario, tmp_path):
    service, manager, old, new, _ = scenario
    (service.root / "current").write_text("not-installed\n")
    with pytest.raises((ValueError, FileNotFoundError)):
        service.register(old, new, tmp_path / "log", user="sid")
    assert not manager.calls
    assert not service.r1_path.exists()


def test_rendering_quotes_arguments_and_preserves_version_environment(scenario, tmp_path):
    from astrid.installer.boundary_registration import _command, _selected
    from astrid.installer.core import Version
    from astrid.installer.service import systemd_quote
    service, _, old, new, _ = scenario
    selected, name, digest = _selected(service.root, service.system)
    selected = Version(selected.root, selected.python, selected.profile,
                       {**selected.env, "ASTRID_RUNTIME_DATA_ROOT": str(new), "ASTRID_RUNTIME_SOURCE_MANIFEST": str(selected.profile)}, selected.release, selected.managed_by)
    log = tmp_path / "log & $% 'quoted'"
    expected = _command(selected, new.resolve(), service.root, name, digest, log.absolute(), service.system)
    service.register(old, new, log, user="S-1-5-21&test")
    definition = service.r1_path.read_text()
    if service.system == "linux":
        action = next(line for line in definition.splitlines() if line.startswith("ExecStart="))
        assert action == "ExecStart=" + " ".join(systemd_quote(arg).replace("$", "$$") for arg in expected)
        assert 'Environment=' + systemd_quote('PATH=selected path $ %') in definition
    elif service.system == "darwin":
        plist = plistlib.loads(service.r1_path.read_bytes())
        assert plist["ProgramArguments"] == expected
        assert plist["EnvironmentVariables"] == selected.env
    else:
        task = ET.fromstring(definition)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        assert task.find(".//t:Command", ns).text == expected[0]
        assert task.find(".//t:Arguments", ns).text == subprocess.list2cmdline(expected[1:])
        assert task.find(".//t:UserId", ns).text == "S-1-5-21&test"
        assert task.find(".//t:WorkingDirectory", ns).text == str(selected.root)


def test_manager_ambiguity_or_old_launcher_prevents_creation(scenario, tmp_path):
    service, manager, old, new, _ = scenario
    service.launcher.parent.mkdir(parents=True)
    service.launcher.write_text("unknown existing launcher")
    with pytest.raises(FileExistsError):
        service.register(old, new, tmp_path / "log", user="sid")
    assert not manager.calls and not service.r1_path.exists()
    service.launcher.unlink()
    if service.system == "win32":
        return  # Scheduler's query and non-forced registration are in the manager action.
    service.run = lambda argv: "loaded\n" if service.system == "linux" else "unexpected output\n"
    with pytest.raises(ValueError, match="ambiguous|Cannot determine"):
        service.register(old, new, tmp_path / "log", user="sid")
    assert not service.r1_path.exists()


def test_service_settings_are_applied_and_cannot_override_root(scenario, tmp_path):
    from astrid.installer.boundary_registration import _command, _selected
    from astrid.installer.core import Version
    service, _, old, new, _ = scenario
    selected, name, digest = _selected(service.root, service.system)
    selected = Version(selected.root, selected.python, selected.profile,
                       {**selected.env, "ASTRID_RUNTIME_DATA_ROOT": str(new), "ASTRID_RUNTIME_SOURCE_MANIFEST": str(selected.profile)}, selected.release, selected.managed_by)
    argv = _command(selected, new, service.root, name, digest, tmp_path / "log", service.system)
    module = tmp_path / "astrid_runtime"
    module.mkdir()
    (module / "__init__.py").write_text("")
    (module / "cli.py").write_text("import os\ndef main(args):\n open(os.environ['CAPTURE'], 'w').write(os.environ['ASTRID_WARM_IDLE_SECONDS'])\n return 0\n")
    capture = tmp_path / "capture"
    child_env = {**os.environ, "CAPTURE": str(capture)}
    code = f"import sys; sys.path.insert(0, {str(tmp_path)!r})\n" + argv[-1]
    settings = service.root / "service-env.json"
    settings.write_text(json.dumps({"ASTRID_WARM_IDLE_SECONDS": "42"}))
    subprocess.run([sys.executable, "-c", code], env=child_env, check=True)
    assert capture.read_text() == "42"
    capture.unlink()
    settings.write_text(json.dumps({"ASTRID_RUNTIME_DATA_ROOT": str(old)}))
    assert subprocess.run([sys.executable, "-c", code], env=child_env, capture_output=True).returncode != 0
    assert not capture.exists()


@pytest.mark.parametrize("missing", ["interpreter", "profile"])
def test_missing_staged_artifact_fails_before_manager_changes(scenario, tmp_path, missing):
    service, manager, old, new, _ = scenario
    folder = service.root / "versions" / "v1"
    path = (folder / ("py/Scripts/pythonw.exe" if service.system == "win32" else "py/bin/python")
            if missing == "interpreter" else folder / "profile.json")
    path.unlink()
    with pytest.raises(ValueError, match="interpreter or profile"):
        service.register(old, new, tmp_path / "log", user="sid")
    assert not manager.calls and not service.r1_path.exists()
