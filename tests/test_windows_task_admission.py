"""Read-only admission against independent selection and scheduler evidence."""
import ast
import json
import xml.etree.ElementTree as ET
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from astrid.installer import windows_task_admission as admission
from astrid.installer.core import Version
from astrid.installer.service import LABEL
from astrid.installer.windows_task_identity import TaskIdentityError

NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
XML = (Path(__file__).parent / "fixtures/windows-task-boundary-41906110096.xml").read_text(encoding="utf-8")
SID = "S-1-5-21-3708201392-4242277230-667464164-500"
ROOT = PureWindowsPath(r"D:\a\_temp\astrid-r1-direct-windows\program")
DATA = PureWindowsPath(r"D:\a\_temp\astrid-r1-direct-windows\data")
VERSION_ROOT = ROOT / "versions" / "3204bb00-6d53a190-46310e10"


@pytest.fixture
def selected(monkeypatch):
    # Reconstruct test selection from the retained native export, solely as
    # fixture setup; the admission code never derives selection from the XML.
    action = ET.fromstring(XML).find(f"{NS}Actions/{NS}Exec/{NS}Arguments").text
    env_line = action.splitlines()[2]
    env = ast.literal_eval(env_line[len("os.environ.update("):-1])
    version = Version(VERSION_ROOT, VERSION_ROOT / "py/Scripts/python.exe",
                      VERSION_ROOT / "profile.json", env)
    record = {"owner": LABEL, "program_root": str(ROOT), "data_root": str(DATA)}
    response = {"status": "present", "sid": SID, "name": "Astrid Runtime", "path": "\\", "xml": XML}
    calls = []
    def powershell(script):
        calls.append(script)
        return json.dumps(response)
    manager = SimpleNamespace(system="win32", root=ROOT, log=PureWindowsPath(
        r"D:\a\_temp\astrid-r1-direct-windows\localappdata\Astrid Runtime\logs\runtime-service.log"),
        env_file=ROOT / "service-env.json", recorded=lambda: record.copy(), powershell=powershell)
    monkeypatch.setattr(admission, "absolute", lambda path: path)
    monkeypatch.setattr(admission, "current", lambda root: version)
    monkeypatch.setattr(admission, "_query", lambda: powershell(admission._QUERY))
    return manager, version, record, response, calls


def test_captured_task_binds_reconstructed_selected_r0_and_current_sid(selected):
    manager, _, _, _, calls = selected
    observed = admission.observe_selected_r0(manager, DATA)
    assert observed.identity.definition_xml == XML
    assert observed.identity.sid == SID and observed.enabled is True
    assert len(calls) == 1
    assert "Get-ScheduledTask" in calls[0] and "Export-ScheduledTask" in calls[0]
    assert "WindowsIdentity]::GetCurrent().User.Value" in calls[0]
    assert not any(action in calls[0] for action in ("Stop-ScheduledTask", "Set-ScheduledTask", "Register-ScheduledTask"))


@pytest.mark.parametrize("change", ["sid", "name", "folder", "action", "version", "data"])
def test_independent_binding_mismatch_is_refused(selected, monkeypatch, change):
    manager, version, _, response, _ = selected
    if change == "sid":
        response["sid"] = "S-1-5-18"
    elif change == "name":
        response["name"] = "astrid runtime"
    elif change == "folder":
        response["path"] = "\\Other\\"
    elif change == "action":
        response["xml"] = XML.replace("-X utf8 -I -c", "-X utf8 -I -m", 1)
    elif change == "version":
        other = VERSION_ROOT.with_name("other")
        monkeypatch.setattr(admission, "current", lambda _: Version(other, other / "py/Scripts/python.exe",
                            other / "profile.json", version.env))
    else:
        manager.recorded = lambda: {"owner": LABEL, "program_root": str(ROOT), "data_root": str(DATA) + "-other"}
    with pytest.raises((TaskIdentityError, ValueError)):
        admission.observe_selected_r0(manager, DATA)


@pytest.mark.parametrize("output", ["", "error", "{}", '{"status":"absent","sid":"S-1-5-18","status":"absent"}',
                                    '{"status":"absent","sid":"S-1-5-18","xml":"error"}',
                                    '{"status":"absent","sid":null}'])
def test_malformed_query_never_means_absence(selected, output, monkeypatch):
    manager, _, _, _, _ = selected
    monkeypatch.setattr(admission, "_query", lambda: output)
    with pytest.raises(ValueError):
        admission.observe_selected_r0(manager, DATA)


def test_explicit_absence_and_changed_selection(selected, monkeypatch):
    manager, _, record, response, _ = selected
    response.clear()
    response.update(status="absent", sid=SID)
    assert admission.observe_selected_r0(manager, DATA) is None
    def changed(_):
        record["data_root"] += "-other"
        return json.dumps(response)
    monkeypatch.setattr(admission, "_query", lambda: changed(admission._QUERY))
    with pytest.raises(ValueError, match="changed during observation"):
        admission.observe_selected_r0(manager, DATA)


def test_query_captures_output_without_using_installer_log(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout='{"status":"absent"}', stderr="")
    monkeypatch.setattr(admission.subprocess, "run", run)
    assert admission._query() == '{"status":"absent"}'
    assert len(calls) == 1 and calls[0][1]["check"] and calls[0][1]["capture_output"]
    assert calls[0][1]["timeout"] == 30
    monkeypatch.setattr(admission.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="", stderr="warning"))
    with pytest.raises(ValueError, match="diagnostics"):
        admission._query()


def test_disabled_observation(selected):
    manager, _, _, response, _ = selected
    response["xml"] = XML.replace("<Settings>", "<Settings><Enabled>false</Enabled>", 1)
    assert admission.observe_selected_r0(manager, DATA).enabled is False


def test_release1_selection_refused_before_query(selected, monkeypatch):
    manager, version, _, _, calls = selected
    replacement = Version(version.root, version.python, version.profile, version.env, release=1)
    monkeypatch.setattr(admission, "current", lambda _: replacement)
    with pytest.raises(ValueError, match="not a direct R0"):
        admission.observe_selected_r0(manager, DATA)
    assert calls == []
