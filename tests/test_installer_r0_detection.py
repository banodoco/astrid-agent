"""Detection at the direct install boundary, before any data mutation."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer import boundary_handoff, r0_stop
from astrid.installer.core import Installer, Version, write_json
from astrid.installer.service import LABEL, Service


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return Service(tmp_path / "program", tmp_path / "log", None,
                   home=tmp_path / "home", system="linux")


def test_fresh_marker_proceeds_through_handoff_and_installer(tmp_path, service, monkeypatch):
    root, data = service.root, tmp_path / "data"
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    backend = SimpleNamespace(service=service)
    assert boundary_handoff.handle(root, data, backend, manifest_loader=lambda: pytest.fail("no handoff")) is False
    reached = []
    monkeypatch.setattr(Installer, "_run_locked", lambda self, mode, manifest, **kw: reached.append(mode))
    Installer(root, data, backend).run("install", {"release": 1})
    assert reached == ["install"]


@pytest.mark.parametrize("release", [0, 1])
def test_current_version_remains_authoritative(tmp_path, release):
    root = tmp_path / "program"
    folder = root / "versions" / "selected"
    version = Version(folder, folder / "py/bin/python", folder / "profile.json", {}, release=release)
    write_json(folder / "version.json", version.document())
    (root / "current").write_text("selected\n")
    assert r0_stop.is_r0_installation(root, None) is (release == 0)


@pytest.mark.parametrize("system,fixture", [("linux", "linux"), ("darwin", "macos")])
def test_captured_r0_entry_detected_without_current(tmp_path, monkeypatch, system, fixture):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    service = Service(tmp_path / "program", tmp_path / "log", None,
                      home=tmp_path / "home", system=system)
    evidence = Path("/home/user/merge-docs-fresh/docs/merge-plan/evidence/actions/41625801442")
    actual = evidence / f"journey-receipt-direct-{fixture}" / "before-cleanup/unit-file-before-cleanup.txt"
    captured = Path(__file__).parent / "r0_service_data" / f"{fixture}.txt"
    if actual.is_file():
        assert captured.read_bytes() == actual.read_bytes()
    service.path.parent.mkdir(parents=True)
    service.path.write_bytes(captured.read_bytes())
    assert r0_stop.is_r0_installation(service.root, service)
    with pytest.raises(r0_stop.R0StopError, match="copy-only upgrade boundary"):
        Installer(service.root, tmp_path / "data", SimpleNamespace(service=service)).run("install", {})


def test_dangling_service_entry_is_not_treated_as_absent(service):
    service.path.parent.mkdir(parents=True)
    service.path.symlink_to(service.path.parent / "missing")
    assert r0_stop.is_r0_installation(service.root, service)


def test_unreadable_service_entry_refuses(service, monkeypatch):
    original = Path.lstat

    def lstat(path):
        if path == service.path:
            raise PermissionError("cannot inspect service")
        return original(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(PermissionError, match="cannot inspect"):
        r0_stop.is_r0_installation(service.root, service)


def test_windows_queries_actual_task_and_propagates_errors(tmp_path):
    service = Service(tmp_path / "program", tmp_path / "log", None,
                      home=tmp_path / "home", system="win32")
    calls = []
    service.powershell = lambda script: calls.append(script) or ""
    assert not r0_stop.is_r0_installation(service.root, service)
    service.powershell = lambda script: calls.append(script) or "Astrid Runtime\n"
    assert r0_stop.is_r0_installation(service.root, service)
    assert all("Get-ScheduledTask" in script for script in calls)
    def unavailable(script):
        raise OSError("Task Scheduler unavailable")
    service.powershell = unavailable
    with pytest.raises(OSError, match="unavailable"):
        r0_stop.is_r0_installation(service.root, service)


def test_missing_service_interface_refuses(tmp_path):
    with pytest.raises(r0_stop.R0StopError, match="Cannot determine"):
        r0_stop.is_r0_installation(tmp_path, None)
