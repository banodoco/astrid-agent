"""The real R0 task action exercises the shared redirector admission rule."""

import copy
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer import windows_owner as rule
from astrid.installer.service import Service
from scripts import ci_journey as journey


@pytest.fixture
def selected(tmp_path):
    venv = tmp_path / "program" / "versions" / "v1" / "py"
    venv.mkdir(parents=True)
    (venv / "Scripts").mkdir()
    (venv / "Scripts" / "pythonw.exe").touch()
    (venv / "pyvenv.cfg").write_text("home = C:\\Python311\n")
    # Authenticated R0 fixture: preserve its complete multiline Python action.
    raw = (Path(__file__).parent /
           "fixtures/release0/services/hosted-41756671212-task.xml").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "d627f38c130c4cb074b25576f781cc6a98d74a7bf2b01d980165c91bed3d2c93"
    task = ET.fromstring(raw.decode("utf-8-sig"))
    tail = " " + task.find(".//{*}Arguments").text
    parent_exe = str(venv / "Scripts" / "pythonw.exe")
    child_exe = r"C:\Python311\pythonw.exe"
    rows = [
        {"ProcessId": 10, "ParentProcessId": 1, "ExecutablePath": parent_exe,
         "CommandLine": f'"{parent_exe}"{tail}', "CreationTime": "100"},
        {"ProcessId": 11, "ParentProcessId": 10, "ExecutablePath": child_exe,
         "CommandLine": f'"{child_exe}"{tail}', "CreationTime": "101"},
    ]
    return venv, rows


def test_real_r0_pair_selects_child_independent_of_enumeration_order(selected):
    venv, rows = selected
    assert rule.owner_pids(rows, venv) == [11]
    assert rule.owner_pids(rows[::-1], venv) == [11]
    assert rule.owner_pids(rows) == [10, 11]


@pytest.mark.parametrize("index,field,value", [
    (0, "ExecutablePath", r"C:\foreign\Scripts\pythonw.exe"),
    (1, "ExecutablePath", r"C:\foreign\pythonw.exe"),
    (0, "ExecutablePath", r"C:\Python311\python.exe"),
    (1, "ParentProcessId", 9),
    (1, "ParentProcessId", "10"),
    (0, "CreationTime", "101"),
    (0, "CreationTime", "102"),
    (0, "CreationTime", None),
    (1, "CreationTime", "unknown"),
    (1, "ProcessId", 10),
    (0, "ProcessId", True),
    (1, "CommandLine", '"C:\\Python311\\pythonw.exe" -m runtime_protocol start'),
])
def test_near_matches_fail_closed(selected, index, field, value):
    venv, rows = selected
    rows[index][field] = value
    assert rule.redirector_pair(rows, venv) is None


@pytest.mark.parametrize("config", ["", "home = relative", "home = C:\\Other",
                                         "home = C:\\Python311\nhome = C:\\Python311"])
def test_unknown_base_fails_closed(selected, config):
    venv, rows = selected
    (venv / "pyvenv.cfg").write_text(config)
    assert rule.owner_pids(rows, venv) == [10, 11]


def test_missing_config_and_extra_owner_fail_closed(selected):
    venv, rows = selected
    extra = {**rows[1], "ProcessId": 12}
    assert rule.owner_pids([*rows, extra], venv) == [10, 11, 12]
    (venv / "pyvenv.cfg").unlink()
    assert rule.owner_pids(rows, venv) == [10, 11]


def test_missing_installed_launcher_fails_closed(selected):
    venv, rows = selected
    (venv / "Scripts" / "pythonw.exe").unlink()
    assert rule.owner_pids(rows, venv) == [10, 11]


@pytest.mark.posix_native
def test_aliased_venv_fails_closed(selected):
    venv, rows = selected
    cfg = venv / "pyvenv.cfg"
    contents = cfg.read_bytes()
    cfg.unlink()
    target = venv / "elsewhere.cfg"
    target.write_bytes(contents)
    cfg.symlink_to(target)
    assert rule.owner_pids(rows, venv) == [10, 11]


def test_changed_argument_or_command_image_fails_closed(selected):
    venv, rows = selected
    rows[1]["CommandLine"] += " "
    assert rule.owner_pids(rows, venv) == [10, 11]
    rows[1]["CommandLine"] = rows[0]["CommandLine"]
    assert rule.owner_pids(rows, venv) == [10, 11]


def test_stop_requires_both_incarnations_gone_and_no_new_runtime(selected):
    _, rows = selected
    pair = tuple(copy.deepcopy(rows))
    assert not rule.pair_exited(rows[:1], pair)
    assert not rule.pair_exited(rows[1:], pair)
    assert not rule.pair_exited([{**rows[0], "CommandLine": "changed"}], pair)
    assert not rule.pair_exited([{**rows[0], "CommandLine": "changed", "CreationTime": None}], pair)
    assert rule.pair_exited([{**rows[0], "CommandLine": "unrelated", "CreationTime": "200"}], pair)
    assert not rule.pair_exited([{**rows[1], "ProcessId": 30}], pair)
    assert rule.pair_exited([], pair)


def test_installer_and_harness_share_real_r0_selection(selected, monkeypatch):
    venv, rows = selected
    version = SimpleNamespace(root=venv.parent, python=venv / "Scripts" / "python.exe")
    service = SimpleNamespace(root=venv.parent.parent.parent,
                              powershell=lambda command: json.dumps(rows))
    assert Service.windows_runtime_pair(service, version, 11) == tuple(rows)
    with pytest.raises(ValueError, match="ambiguous"):
        Service.windows_runtime_pair(service, version, 10)
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setattr(journey.windows, "EVIDENCE", venv)
    monkeypatch.setattr(journey.windows, "powershell", lambda *args, **kwargs: json.dumps(rows))
    monkeypatch.setattr(journey, "installation", lambda root, data: (
        None, data, {"root": str(version.root), "python": str(version.python)}))
    monkeypatch.setattr(journey, "healthy", lambda data: {"pid": 11})
    journey.assert_one_runtime(venv, root=venv.parent.parent.parent)
    rows.append({**rows[1], "ProcessId": 12})
    with pytest.raises(ValueError, match="ambiguous"):
        Service.windows_runtime_pair(service, version, 11)
    with pytest.raises(RuntimeError, match="found"):
        journey.assert_one_runtime(venv, root=venv.parent.parent.parent)


def test_installer_wait_never_signals_or_stops_scheduler(selected, monkeypatch):
    _, rows = selected
    responses = iter([rows[:1], []])
    service = SimpleNamespace(powershell=lambda command: json.dumps(next(responses)))
    monkeypatch.setattr("astrid.installer.service.time.sleep", lambda seconds: None)
    Service.wait_windows_runtime_pair(service, tuple(rows))


def test_installer_timeout_refuses_without_signal(selected, monkeypatch):
    _, rows = selected
    service = SimpleNamespace(powershell=lambda command: json.dumps(rows[:1]))
    times = iter([0, 91])
    monkeypatch.setattr("astrid.installer.service.time.monotonic", lambda: next(times))
    with pytest.raises(RuntimeError, match="both stop"):
        Service.wait_windows_runtime_pair(service, tuple(rows))


def test_harness_stops_child_and_waits_for_launcher(selected, monkeypatch):
    venv, rows = selected
    samples = iter([rows, rows[:1], []])
    calls = []
    monkeypatch.setattr(journey.windows, "powershell", lambda *args, **kwargs: json.dumps(next(samples)))
    monkeypatch.setattr(journey, "healthy", lambda data: {"pid": 11})
    monkeypatch.setattr(journey.time, "sleep", lambda seconds: calls.append("wait"))
    def stop(version, argv, evidence, name, **kwargs):
        assert argv == ["down", "--profile", "astrid", "--data-root", str(venv)]
        assert kwargs["module"] == "astrid_runtime"
        calls.append("child-stop")
    monkeypatch.setattr(journey, "installed_cli", stop)
    journey.stop_windows_runtime({"root": str(venv.parent)}, venv, venv, "stop.log")
    assert calls == ["child-stop", "wait"]


def test_harness_ambiguous_owner_never_stops(selected, monkeypatch):
    venv, rows = selected
    rows[1]["ParentProcessId"] = 99
    monkeypatch.setattr(journey.windows, "powershell", lambda *args, **kwargs: json.dumps(rows))
    monkeypatch.setattr(journey, "healthy", lambda data: {"pid": 11})
    monkeypatch.setattr(journey, "installed_cli", lambda *args, **kwargs: pytest.fail("must not signal"))
    with pytest.raises(RuntimeError, match="ambiguous"):
        journey.stop_windows_runtime({"root": str(venv.parent)}, venv, venv, "stop.log")


@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize("foreground", [False, True])
def test_pair_stop_keeps_runtime_fence_and_data(tmp_path, monkeypatch, blocked, foreground):
    from astrid.installer.core import RuntimeOperationError, write_json
    from tests.test_installer_busy_fence import Boundary, publish, realm_fixture

    backend, old, data, paths, realm, calls = realm_fixture(
        tmp_path, monkeypatch, system="win32", blocker="running" if blocked else None)
    publish(paths, realm)
    boundary = Boundary(paths, calls)
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary", lambda: boundary)
    pair = ({"ProcessId": 122}, {"ProcessId": 123})
    monkeypatch.setattr(backend.service, "windows_runtime_pair", lambda version, pid: pair)
    monkeypatch.setattr(backend.service, "wait_windows_runtime_pair", lambda value: calls.append("pair-exited"))
    if foreground:
        record = backend.service.recorded()
        record["foreground"] = {"pid": 122, "birth_id": "launcher-birth"}
        write_json(backend.service.selection, record)
    database = realm / "realm.sqlite3"
    before = database.read_bytes(), (data / "workspace-sentinel").read_bytes()
    if blocked:
        with pytest.raises(RuntimeOperationError, match="busy"):
            backend.stop(old, data)
        assert calls == []
    else:
        backend.stop(old, data)
        assert calls == ["prepare", "runtime-stop", "pair-exited"] + ([] if foreground else ["disable"])
        assert "foreground" not in backend.service.recorded()
    assert (database.read_bytes(), (data / "workspace-sentinel").read_bytes()) == before


@pytest.mark.windows_native
def test_native_venv_pythonw_pair_exits_when_child_stops(tmp_path):
    import subprocess
    import time
    import venv as venv_module

    import psutil

    venv = tmp_path / "py"
    venv_module.EnvBuilder(with_pip=False).create(venv)
    launcher = subprocess.Popen([str(venv / "Scripts" / "pythonw.exe"),
                                 "-c", "import time; time.sleep(60)"])
    child = None
    try:
        deadline = time.monotonic() + 20
        pair = None
        while pair is None and time.monotonic() < deadline:
            output = subprocess.check_output([
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "$ErrorActionPreference='Stop'; " + rule.PROCESS_QUERY], text=True, timeout=30)
            rows = json.loads(output)
            selected = [row for row in rows if row["ProcessId"] == launcher.pid or
                        row["ParentProcessId"] == launcher.pid]
            pair = rule.redirector_pair(selected, venv)
            if pair is None:
                time.sleep(0.1)
        assert pair is not None
        child = psutil.Process(pair[1]["ProcessId"])
        child.terminate()  # Test owns this newly created sleeping child.
        child.wait(timeout=10)
        launcher.wait(timeout=10)
    finally:
        if launcher.poll() is None:
            for descendant in psutil.Process(launcher.pid).children(recursive=True):
                if descendant.is_running():
                    descendant.terminate()
            psutil.wait_procs(psutil.Process(launcher.pid).children(recursive=True), timeout=10)
        if child is not None and child.is_running():
            child.kill()
            child.wait(timeout=10)
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=10)
