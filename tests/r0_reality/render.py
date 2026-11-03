"""Test-only R0 definition renderer, copied from pinned Agent source.

Only the renderer name and injectable Windows clock differ from the source.
"""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import plistlib
import subprocess
from xml.sax.saxutils import escape

LABEL = "haus.astrid.runtime"
SERVICE_SETTINGS = frozenset({"ASTRID_WARM_IDLE_SECONDS", "ASTRID_ALLOW_UNQUALIFIED_GPU"})

@dataclass(frozen=True)
class Version:
    root: Path
    python: Path
    profile: Path
    env: dict[str, str]


def command(version: Version, data: Path, *, windows: bool = False, log: Path | None = None,
            env_file: Path | None = None, extra_args: list[str] | None = None,
            lock_fd: int | None = None) -> list[str]:
    python = version.python.with_name("pythonw.exe") if windows else version.python
    arguments = ["run", "--profile", "astrid", "--data-root", str(data),
                 "--source-manifest", str(version.profile), *(extra_args or [])]
    if env_file is None and not windows and lock_fd is None:
        return [str(python), "-X", "utf8", "-I", "-m", "astrid_runtime", *arguments]
    # Task Scheduler cannot set an environment and Hidden does not hide a
    # console. pythonw executes the foreground Runtime in this same process.
    script = "import json,os,sys\nfrom pathlib import Path\n"
    if lock_fd is not None:
        # The foreground owner inherited this locked descriptor across exec.
        # Keep it open here, but never pass it to a subsequently exec'd child.
        script += f"os.set_inheritable({lock_fd},False)\n"
    if windows:
        if log is None:
            raise ValueError("A log path is required for the Windows Runtime service.")
        log = log.with_name("runtime-service.log")
        script += (f"os.environ.update({version.env!r})\n"
                   f"sys.stdout=sys.stderr=open({str(log)!r},'a',encoding='utf-8',buffering=1)\n")
    if env_file is not None:
        script += (f"path=Path({str(env_file)!r})\n"
                   "settings=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}\n"
                   f"if (not isinstance(settings,dict) or set(settings)-set({sorted(SERVICE_SETTINGS)!r}) "
                   "or any(not isinstance(value,str) for value in settings.values())):\n"
                   "    raise ValueError('Invalid Astrid service-env.json; use the documented string settings.')\n"
                   "os.environ.update(settings)\n")
    script += f"from astrid_runtime.cli import main\nsys.exit(main({arguments!r}))"
    return [str(python), "-X", "utf8", "-I", "-c", script]


def systemd_quote(value: str) -> str:
    # systemd performs specifier expansion even inside double quotes.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("\n", "\\n") + '"'


def render_definition(system: str, version: Version, data: Path, log: Path, *, user: str = "",
           env_file: Path | None = None, start_boundary: str | None = None) -> str:
    log = log.with_name("runtime-service.log")
    argv = command(version, data, windows=system == "win32", log=log, env_file=env_file)
    if system == "darwin":
        return plistlib.dumps({
            "Label": LABEL, "ProgramArguments": argv, "RunAtLoad": True,
            "KeepAlive": True, "ExitTimeOut": 90,
            "EnvironmentVariables": version.env,
            "StandardOutPath": str(log), "StandardErrorPath": str(log),
        }, sort_keys=True).decode("utf-8")
    if system == "linux":
        executable = " ".join(systemd_quote(s).replace("$", "$$") for s in argv)
        environment = "".join("Environment=" + systemd_quote(k + "=" + v) + "\n" for k, v in sorted(version.env.items()))
        return ("[Unit]\nDescription=Astrid Runtime\n\n[Service]\n"
                f"ExecStart={executable}\n{environment}Restart=always\nRestartSec=5\n"
                "KillMode=mixed\nTimeoutStopSec=90\n"
                f"StandardOutput={systemd_quote('append:' + str(log))}\n"
                f"StandardError={systemd_quote('append:' + str(log))}\n"
                "\n[Install]\nWantedBy=default.target\n")
    if system != "win32":
        raise ValueError(f"Unsupported operating system: {system}")
    arguments = subprocess.list2cmdline(argv[1:])
    start_boundary = start_boundary or datetime.now().astimezone().isoformat(timespec="seconds")
    # Register-ScheduledTask's -Xml parameter receives a PowerShell string,
    # not encoded bytes. An encoding declaration makes Task Scheduler reject
    # that string because PowerShell cannot provide the declared byte encoding.
    return f'''<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Astrid Runtime</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><UserId>{escape(user)}</UserId></LogonTrigger><TimeTrigger><Enabled>true</Enabled><StartBoundary>{escape(start_boundary)}</StartBoundary><Repetition><Interval>PT1M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition></TimeTrigger></Triggers>
  <Principals><Principal id="Owner"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled>
    <RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>false</WakeToRun><ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Owner"><Exec><Command>{escape(argv[0])}</Command><Arguments>{escape(arguments)}</Arguments><WorkingDirectory>{escape(str(version.root))}</WorkingDirectory></Exec></Actions>
</Task>
'''
