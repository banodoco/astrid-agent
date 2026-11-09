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
    service.powershell = lambda script: calls.append(script) or "ASTRID_TASK_COUNT=0\n"
    assert not r0_stop.is_r0_installation(service.root, service)
    service.powershell = lambda script: calls.append(script) or "ASTRID_TASK_COUNT=1\n"
    assert r0_stop.is_r0_installation(service.root, service)
    assert all("Get-ScheduledTask -TaskPath '\\' -ErrorAction Stop" in script for script in calls)
    assert all("$_.TaskPath -eq '\\'" in script for script in calls)
    def unavailable(script):
        raise OSError("Task Scheduler unavailable")
    service.powershell = unavailable
    with pytest.raises(OSError, match="unavailable"):
        r0_stop.is_r0_installation(service.root, service)


@pytest.mark.parametrize("output", ["", "Astrid Runtime", "ASTRID_TASK_COUNT=2",
                                    "ASTRID_TASK_COUNT=0\nwarning", "error\nASTRID_TASK_COUNT=0",
                                    "ASTRID_TASK_COUNT=00", "ASTRID_TASK_COUNT=0\nASTRID_TASK_COUNT=0"])
def test_windows_task_query_uncertainty_never_means_absent(tmp_path, output):
    service = Service(tmp_path / "program", tmp_path / "log", None,
                      home=tmp_path / "home", system="win32")
    service.powershell = lambda script: output
    with pytest.raises(ValueError, match="ambiguous or malformed"):
        r0_stop.is_r0_installation(service.root, service)


def test_missing_service_interface_refuses(tmp_path):
    with pytest.raises(r0_stop.R0StopError, match="Cannot determine"):
        r0_stop.is_r0_installation(tmp_path, None)


WINDOWS_REFUSAL = ("Upgrading from an earlier Windows version isn't supported yet. "
                   "Your data was kept; reinstall to get the new version.")


def _tree(root):
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


def _forbid_mutations(monkeypatch, calls):
    """Every stop, registration, copy and legacy path records a call and fails."""
    def forbidden(name):
        def call(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"{name} ran before the Windows refusal")
        return call
    monkeypatch.setattr(boundary_handoff, "BoundaryHandoff", forbidden("BoundaryHandoff"))
    monkeypatch.setattr(boundary_handoff.registration, "BoundaryRegistration", forbidden("BoundaryRegistration"))
    for name in ("stop_r0_and_wait", "_disable_and_stop", "_reenable", "_write_marker"):
        monkeypatch.setattr(r0_stop, name, forbidden(name))
    monkeypatch.setattr(Installer, "run", forbidden("Installer.run"))
    return SimpleNamespace(service=None, stage=forbidden("stage"), runtime=forbidden("runtime"),
                           recover_r1_registration=forbidden("recover_r1_registration"),
                           preflight_stop=forbidden("preflight_stop"))


def _windows_r0(tmp_path, *, selected):
    service = Service(tmp_path / "program", tmp_path / "log", None,
                      home=tmp_path / "home", system="win32")
    root, data = service.root, tmp_path / "data"
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    if selected:
        folder = root / "versions" / "release0"
        write_json(folder / "version.json", Version(folder, folder / "py/python.exe",
                                                    folder / "profile.json", {}, release=0).document())
        (root / "current").write_text("release0\n")
    write_json(service.selection, {"owner": LABEL, "program_root": str(root), "data_root": str(data)})
    (data / "runtime").mkdir(parents=True)
    (data / "runtime/marker.txt").write_text("R0 workspace")
    return service, root, data


@pytest.mark.parametrize("selected", [True, False], ids=["selected-r0-version", "r0-task-only"])
def test_windows_r0_upgrade_refuses_with_exact_text_before_any_change(tmp_path, monkeypatch, selected):
    service, root, data = _windows_r0(tmp_path, selected=selected)
    queries = []
    service.powershell = lambda script: queries.append(script) or "ASTRID_TASK_COUNT=1\n"
    calls = []
    backend = _forbid_mutations(monkeypatch, calls)
    backend.service = service
    before = _tree(tmp_path)
    with pytest.raises(ValueError) as refused:
        boundary_handoff.handle(root, data, backend, manifest_loader=lambda: calls.append("manifest"))
    assert str(refused.value) == WINDOWS_REFUSAL == boundary_handoff.WINDOWS_UPGRADE_REFUSAL
    assert calls == []
    assert _tree(tmp_path) == before
    # Only the read-only existence query may reach Task Scheduler.
    assert len(queries) == (0 if selected else 1)
    assert all("Get-ScheduledTask" in script for script in queries)



@pytest.mark.parametrize("selected", [True, False], ids=["selected-r0-version", "r0-task-only"])
def test_windows_upgrade_refused_predicate_matches_the_refusal(tmp_path, selected):
    service, root, _ = _windows_r0(tmp_path, selected=selected)
    service.powershell = lambda script: "ASTRID_TASK_COUNT=1\n"
    assert boundary_handoff.windows_upgrade_refused(root, service) is True


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_posix_r0_still_reaches_the_copy_only_handoff(tmp_path, monkeypatch, system):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    service = Service(tmp_path / "program", tmp_path / "log", None, home=tmp_path / "home", system=system)
    root, data = service.root, tmp_path / "data"
    folder = root / "versions" / "release0"
    write_json(folder / "version.json", Version(folder, folder / "py/bin/python",
                                                folder / "profile.json", {}, release=0).document())
    (root / "current").write_text("release0\n")
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    reached = []

    class Handoff:
        def __init__(self, backend):
            reached.append(backend.service.system)

        def run(self, run_data, loader, *, say):
            reached.append((run_data, loader()))

    monkeypatch.setattr(boundary_handoff, "BoundaryHandoff", Handoff)
    backend = SimpleNamespace(service=service)
    assert boundary_handoff.handle(root, data, backend, manifest_loader=lambda: "manifest") is True
    assert reached == [system, (data, "manifest")]
