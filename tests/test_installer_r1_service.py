"""Service boundary tests against the captured R0 entries."""
import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import pytest

from astrid.installer import r0_stop, r1_service
from astrid.installer.service import LABEL
from tests.test_installer_r0_stop import Manager, FIXTURES


@pytest.mark.parametrize("system,fixture", [("linux", "linux.txt"), ("darwin", "macos.txt"), ("win32", "windows.txt")])
def test_distinct_registration_activation_and_rollback(tmp_path, monkeypatch, system, fixture):
    monkeypatch.setattr(r0_stop, "_launchd_uid", lambda: 501)
    root = tmp_path / "program with spaces"
    root.mkdir()
    old = tmp_path / ("astrid.service" if system == "linux" else f"{LABEL}.plist" if system == "darwin" else "task.xml")
    shutil.copyfile(FIXTURES / fixture, old)
    before = old.read_bytes()
    manager = Manager(system, old)
    manager.root, manager.log = root, tmp_path / "service.log"
    data = tmp_path / "data-r1"
    r1_service.register_r1(manager, data)
    entry = r1_service._path(manager)
    assert old.read_bytes() == before
    assert entry != old and (root / "bin/runtime").exists()
    text = entry.read_text()
    if system == "linux":
        assert 'ExecStart=' in text and str(root / "bin/runtime") in text
        assert not any("enable" in call for call in manager.calls)
    elif system == "darwin":
        value = plistlib.loads(entry.read_bytes())
        assert value["Label"] == r1_service.R1_LABEL
        assert value["ProgramArguments"] == [str(root / "bin/runtime"), str(data)]
        assert not manager.calls
    else:
        value = ElementTree.fromstring(text)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        assert value.findtext("t:Settings/t:Enabled", namespaces=ns) == "false"
        assert str(root / "bin/runtime") in value.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns)
    r1_service.activate_r1(manager)
    r1_service.rollback_r1(manager)
    assert old.read_bytes() == before
    calls = [str(c) for c in manager.calls]
    disable = next(i for i, c in enumerate(calls) if ("disable" in c.lower() and (r1_service.R1_UNIT in c or r1_service.R1_LABEL in c or r1_service.R1_TASK in c)))
    enable_r0 = next(i for i, c in enumerate(calls) if i > disable and "enable" in c.lower() and ("astrid.service" in c or LABEL in c or "Astrid Runtime'" in c))
    assert disable < enable_r0


def test_launcher_follows_current_and_execs_selected_version(tmp_path):
    root = tmp_path / "program"
    root.mkdir()
    manager = Manager("linux", tmp_path / "astrid.service")
    manager.root, manager.log = root, tmp_path / "service.log"
    r1_service.register_r1(manager, tmp_path / "data")
    for version in ("one", "two"):
        python = root / "versions" / version / "py/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text('#!/bin/sh\nprintf "%s\\n" "$0"\n')
        python.chmod(0o755)
        (root / "current").write_text(version)
        result = subprocess.run([root / "bin/runtime", tmp_path / "data"], check=True, capture_output=True, text=True)
        assert result.stdout.strip() == str(python)
    (root / "current").write_text("../escape")
    result = subprocess.run([root / "bin/runtime", tmp_path / "data"], capture_output=True)
    assert result.returncode != 0


def test_runner_uses_selected_version_environment_and_data(tmp_path):
    root = tmp_path / "program"
    version = root / "versions" / "r1"
    package = version / "astrid_runtime"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text("import os\ndef main(argv):\n print(os.environ['TEST_VERSION'], *argv)\n return 0\n")
    (root / "current").write_text("r1")
    (version / "version.json").write_text(json.dumps({"root": str(version), "profile": str(version / "profile.json"), "env": {"TEST_VERSION": "selected"}}))
    # Run the exact generated runner with a controlled module path.
    runner = root / "runner.py"
    runner.write_text(r1_service._RUNNER)
    result = subprocess.run([os.sys.executable, runner, root, tmp_path / "r1-data", "r1"], env={**os.environ, "PYTHONPATH": str(version)}, check=True, capture_output=True, text=True)
    assert "selected run --profile astrid --data-root" in result.stdout
    assert str(tmp_path / "r1-data") in result.stdout
    assert str(version / "profile.json") in result.stdout


@pytest.mark.parametrize("system", ["linux", "darwin", "win32"])
def test_failed_r1_disable_never_reenables_r0(tmp_path, monkeypatch, system):
    monkeypatch.setattr(r0_stop, "_launchd_uid", lambda: 501)
    manager = Manager(system, tmp_path / "old")
    manager.root, manager.log = tmp_path / "root", tmp_path / "log"
    manager.fail = {"linux": "disable", "darwin": "disable", "win32": "Disable-ScheduledTask"}[system]
    with pytest.raises(RuntimeError):
        r1_service.rollback_r1(manager)
    assert not any("enable" in str(call).lower() for call in manager.calls)


def test_registration_refuses_existing_unit_without_leaving_new_launcher(tmp_path):
    root = tmp_path / "program"
    root.mkdir()
    manager = Manager("linux", tmp_path / "astrid.service")
    manager.root, manager.log = root, tmp_path / "log"
    foreign = tmp_path / r1_service.R1_UNIT
    foreign.write_text("other service")
    with pytest.raises(FileExistsError):
        r1_service.register_r1(manager, tmp_path / "data")
    assert foreign.read_text() == "other service"
    assert not (root / "bin/runtime").exists()
    assert not (root / "bin/runtime.py").exists()
    assert not manager.calls


def test_macos_rollback_before_activation_restores_r0(tmp_path, monkeypatch):
    monkeypatch.setattr(r0_stop, "_launchd_uid", lambda: 501)
    manager = Manager("darwin", tmp_path / "old.plist")
    manager.root, manager.log = tmp_path / "root", tmp_path / "log"
    original = manager.run
    def run(argv):
        if argv[:2] == ["launchctl", "print"]:
            raise subprocess.CalledProcessError(1, argv, output="Could not find service")
        return original(argv)
    manager.run = run
    r1_service.rollback_r1(manager)
    assert [call[1] for call in manager.calls] == ["disable", "enable", "bootstrap", "kickstart"]
