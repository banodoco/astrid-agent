"""Exercise Windows PowerShell exit semantics without touching real login tasks."""

import shutil
import subprocess
import uuid

import pytest

from astrid.installer import service
from astrid.installer.core import Version
from astrid.installer.service import Service

pytestmark = pytest.mark.windows_installer


@pytest.mark.parametrize("operation", ["disable", "stop", "unregister"])
@pytest.mark.parametrize("scenario", ["absent", "present", "lookup_failure", "action_failure"])
def test_task_cleanup_preserves_powershell_success_and_failure(tmp_path, operation, scenario):
    powershell = shutil.which("powershell.exe")
    assert powershell, "Windows PowerShell is required to exercise its native exit semantics"
    invocations = []

    def run(argv):
        # Each invocation is a new PowerShell process. The polling query observes
        # the successful stop from the preceding invocation.
        state = "Running" if not invocations else "Ready"
        body = ""
        if scenario == "lookup_failure":
            body = "throw 'Task Scheduler unavailable'"
        elif scenario != "absent":
            body = f"[pscustomobject]@{{ TaskName='Astrid Runtime'; TaskPath='\\'; State='{state}' }}"
        action = "throw 'Cannot change task'" if scenario == "action_failure" else ""
        prelude = (
            "function Get-ScheduledTask { [CmdletBinding()] param([string]$TaskName); "
            # Reproduce the cmdlet's named lookup error on a fresh installation.
            + ("if ($TaskName) { $PSCmdlet.WriteError([System.Management.Automation.ErrorRecord]::new("
               "[Exception]::new('No matching task'), 'NoMatchingTask', "
               "[System.Management.Automation.ErrorCategory]::ObjectNotFound, $TaskName)); return }; "
               if scenario == "absent" else "")
            + body + " }; "
            "function Disable-ScheduledTask { process { " + action + " } }; "
            "function Stop-ScheduledTask { process { " + action + " } }; "
            "function Unregister-ScheduledTask { param([switch]$Confirm); process { " + action + " } }; "
        )
        script = argv[-1]
        invocations.append(script)
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", prelude + script],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return result.stdout

    manager = Service(tmp_path / "program", tmp_path / "log", run, home=tmp_path, system="win32")
    if scenario.endswith("failure"):
        with pytest.raises(subprocess.CalledProcessError):
            getattr(manager, operation)()
    else:
        getattr(manager, operation)()
    assert invocations


def test_register_scheduled_task_accepts_rendered_xml_string(tmp_path, monkeypatch):
    """Exercise the UTF-8 file -> PowerShell string -> Task Scheduler boundary."""
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    assert powershell, "Windows PowerShell is required to register a native task"
    task_name = f"Astrid XML Smoke {uuid.uuid4().hex}"
    monkeypatch.setattr(service, "TASK", task_name)
    original_render = service.render

    def render_without_triggers(*args, **kwargs):
        # Keep the registration definition inert so this smoke cannot launch
        # the runtime while it checks Task Scheduler's XML parser.
        return original_render(*args, **kwargs).replace(
            "<Enabled>true</Enabled>", "<Enabled>false</Enabled>", 2)

    monkeypatch.setattr(service, "render", render_without_triggers)

    def run(argv):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=True)
        return result.stdout

    manager = Service(tmp_path / "program", tmp_path / "log", run,
                      home=tmp_path, system="win32")
    version = Version(tmp_path / "version", tmp_path / "pythonw.exe", tmp_path / "profile.json", {})
    try:
        manager.register(version, tmp_path / "data")
        xml = manager.path.read_text(encoding="utf-8")
        assert xml.startswith("<Task ")
        assert "<?xml" not in xml
        manager.powershell(f"Get-ScheduledTask -TaskName '{task_name}' -ErrorAction Stop | Out-Null")
    finally:
        manager.unregister()
