"""Login service definitions and operations; all OS branching lives here."""

from __future__ import annotations

import os
import platform as system_platform
import plistlib
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from runtime_protocol import platform

from .core import Version, read_json, write_json

LABEL = "haus.astrid.runtime"
TASK = "Astrid Runtime"
# The only keys service-env.json may set. ASTRID_ALLOW_UNQUALIFIED_GPU no longer
# changes anything; it stays accepted so existing settings files still load.
SERVICE_SETTINGS = frozenset({"ASTRID_WARM_IDLE_SECONDS", "ASTRID_ALLOW_UNQUALIFIED_GPU"})
# A named Get-ScheduledTask lookup reports a non-terminating error when absent.
# Enumerating and filtering makes an absent task a successful no-op while still
# propagating Task Scheduler access failures through ErrorActionPreference=Stop.
TASK_QUERY = f"$t=Get-ScheduledTask | Where-Object {{ $_.TaskName -eq '{TASK}' -and $_.TaskPath -eq '\\' }}; "


def platform_target() -> str:
    system = {"darwin": "macos", "linux": "linux", "win32": "windows"}.get(sys.platform)
    machine = system_platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if not system or not arch:
        raise ValueError(f"Unsupported operating system or CPU: {sys.platform}/{machine}")
    return f"{system}-{arch}"


def venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def default_program_root(home: Path, system: str) -> Path:
    if system == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "Astrid"
    if system == "linux":
        return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "astrid"
    return home / ".local" / "share" / "astrid"


def install_selection(home: Path, system: str) -> Path:
    return default_program_root(home, system).parent / "astrid-install.json"


def bootstrap_root(home: Path, system: str) -> Path:
    root = default_program_root(home, system)
    return (root.parent / "Astrid Installer" if system == "win32" else root) / "bootstrap"


def command(version: Version, data: Path, *, windows: bool = False, log: Path | None = None,
            env_file: Path | None = None) -> list[str]:
    python = version.python.with_name("pythonw.exe") if windows else version.python
    arguments = ["run", "--profile", "astrid", "--data-root", str(data),
                 "--source-manifest", str(version.profile)]
    if env_file is None and not windows:
        return [str(python), "-I", "-m", "astrid_runtime", *arguments]
    # Task Scheduler cannot set an environment and Hidden does not hide a
    # console. pythonw executes the foreground Runtime in this same process.
    script = "import json,os,sys\nfrom pathlib import Path\n"
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
    return [str(python), "-I", "-c", script]


def systemd_quote(value: str) -> str:
    # systemd performs specifier expansion even inside double quotes.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("\n", "\\n") + '"'


def render(system: str, version: Version, data: Path, log: Path, *, user: str = "",
           env_file: Path | None = None) -> str:
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
    start_boundary = datetime.now().astimezone().isoformat(timespec="seconds")
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


class Service:
    def __init__(self, root: Path, log: Path, run, *, home: Path | None = None,
                 system: str | None = None, say=print):
        self.root, self.log, self.run = root, log.with_name("runtime-service.log"), run
        self.home, self.system, self.say = home or Path.home(), system or sys.platform, say
        self.selection = install_selection(self.home, self.system)
        self.env_file = root / "service-env.json"
        if self.system == "darwin":
            self.path = self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        elif self.system == "linux":
            self.path = Path(os.environ.get("XDG_CONFIG_HOME") or self.home / ".config") / "systemd" / "user" / "astrid.service"
        else:
            self.path = root / "task.xml"

    def recorded(self) -> dict:
        record = read_json(self.selection)
        if record and record.get("owner") != LABEL:
            raise ValueError("The service record belongs to another installer.")
        return record

    def powershell(self, script: str) -> str:
        executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        return self.run([executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-Command", "$ErrorActionPreference='Stop'; " + script])

    def systemd(self) -> bool:
        if self.system != "linux":
            return False
        try:
            self.run(["systemctl", "--user", "show-environment"])
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def disable(self) -> None:
        if self.system == "darwin":
            if self.path.exists():
                # bootout both disables KeepAlive and waits for the foreground owner.
                target = f"gui/{platform.current_user()}/{LABEL}"
                try:
                    self.run(["launchctl", "print", target])
                except subprocess.CalledProcessError as exc:
                    if "Could not find service" not in str(exc.output) and "Could not find specified service" not in str(exc.output):
                        raise
                    return  # A missing/unloaded login item is already stopped.
                self.run(["launchctl", "bootout", target])
        elif self.system == "linux":
            if self.systemd() and self.path.exists():
                self.run(["systemctl", "--user", "disable", "astrid.service"])
        else:
            self.powershell(TASK_QUERY + "if ($t) { $t | Disable-ScheduledTask | Out-Null }")

    def stop_foreground(self) -> None:
        """Stop a recorded fallback owner that has not published discovery yet."""
        foreground = self.recorded().get("foreground")
        if foreground is None:
            return
        if (not isinstance(foreground, dict) or type(foreground.get("pid")) is not int
                or foreground["pid"] <= 0 or not isinstance(foreground.get("birth_id"), str)
                or not foreground["birth_id"].strip()):
            raise ValueError("The foreground Astrid Runtime identity is incomplete.")
        pid = foreground["pid"]
        identity = platform.process_identity(pid)
        if identity is None or identity.get("birth_id") != foreground["birth_id"]:
            return  # The recorded incarnation is already gone.
        if identity.get("pid") != pid or identity.get("uid") != platform.current_user():
            raise RuntimeError("The foreground Astrid Runtime identity changed; refusing to signal it.")
        try:
            platform.signal_process(identity, platform.TERM)
        except ProcessLookupError:
            pass  # It exited after observation; never reopen or signal a PID.

    def stop(self) -> None:
        if self.system == "linux" and self.systemd() and self.path.exists():
            self.run(["systemctl", "--user", "stop", "astrid.service"])
        elif self.system == "win32":
            # Runtime's birth-checked down has already drained its children.
            self.powershell(TASK_QUERY + "if ($t -and $t.State -eq 'Running') { $t | Stop-ScheduledTask }")
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                state = self.powershell(TASK_QUERY + "if ($t) { $t.State }").strip()
                if state != "Running":
                    return
                time.sleep(0.2)
            raise RuntimeError("The Astrid sign-in task did not stop.")
        foreground = self.recorded().get("foreground")
        if foreground:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                identity = platform.process_identity(int(foreground["pid"]))
                if identity is None or identity.get("birth_id") != foreground["birth_id"]:
                    return
                time.sleep(0.2)
            raise RuntimeError("The foreground Astrid Runtime did not finish stopping.")

    def register(self, version: Version, data: Path) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.env_file.exists():
            write_json(self.env_file, {})
        settings = self.settings()
        user = self.powershell("[Security.Principal.WindowsIdentity]::GetCurrent().User.Value").strip() if self.system == "win32" else ""
        self.path.write_text(render(self.system, version, data, self.log, user=user, env_file=self.env_file), encoding="utf-8", newline="\n")
        if self.system == "darwin":
            self.run(["launchctl", "bootstrap", f"gui/{platform.current_user()}", self.path])
        elif self.system == "linux":
            if self.systemd():
                self.run(["systemctl", "--user", "daemon-reload"])
                self.run(["systemctl", "--user", "enable", "astrid.service"])
            else:
                self.say("There is no systemd user service; run the install line to restart Astrid.")
        else:
            xml_path = str(self.path).replace("'", "''")
            self.powershell(f"Register-ScheduledTask -TaskName '{TASK}' -Xml ([IO.File]::ReadAllText('{xml_path}')) -Force | Out-Null")
        record = {"owner": LABEL, "program_root": str(self.root),
                  "data_root": str(data),
                  "service_settings": settings}
        write_json(self.selection, record)

    def settings(self) -> dict:
        settings = read_json(self.env_file)
        if (set(settings) - SERVICE_SETTINGS
                or any(not isinstance(value, str) for value in settings.values())):
            raise ValueError("Invalid Astrid service-env.json; use the documented string settings.")
        return settings

    def restart_required(self) -> bool:
        # Legacy services did not record settings. An empty file preserves their
        # default behavior; a nonempty file needs one restart to establish it.
        return self.recorded().get("service_settings", {}) != self.settings()

    def start(self) -> bool:
        if self.system == "darwin":
            self.run(["launchctl", "kickstart", f"gui/{platform.current_user()}/{LABEL}"])
        elif self.system == "linux":
            if not self.systemd():
                return False
            self.run(["systemctl", "--user", "start", "astrid.service"])
        else:
            self.powershell(f"Enable-ScheduledTask -TaskName '{TASK}' | Out-Null; Start-ScheduledTask -TaskName '{TASK}'")
        return True

    def start_foreground(self, version: Version, data: Path) -> None:
        """No systemd: detach Runtime's own supervisor; the line is the restart."""
        env = {k: v for k, v in os.environ.items() if k not in {
            "PYTHONPATH", "PYTHONHOME", "ASTRID_WORKSPACE_ENDPOINT",
            "ASTRID_WORKSPACE_CREDENTIAL", "ASTRID_RUNTIME_SOURCE_MANIFEST",
            "ASTRID_RUNTIME_DATA_ROOT", "ASTRID_PACKS_PATH"}}
        env.update(version.env)
        with self.log.open("a", encoding="utf-8") as stream:
            process = platform.spawn_tree(command(version, data, env_file=self.env_file), detached=True,
                                          cwd=version.root, env=env, stdin=subprocess.DEVNULL,
                                          stdout=stream, stderr=subprocess.STDOUT)
        identity = platform.process_identity(process.pid)
        if identity is None:
            raise RuntimeError("The foreground Astrid Runtime exited before startup.")
        record = self.recorded()
        write_json(self.selection, {**record, "foreground": {
            "pid": process.pid, "birth_id": identity["birth_id"]}})

    def unregister(self) -> None:
        if self.system == "win32":
            self.powershell(TASK_QUERY + "if ($t) { $t | Unregister-ScheduledTask -Confirm:$false }")
        self.path.unlink(missing_ok=True)
        if self.system == "linux" and self.systemd():
            self.run(["systemctl", "--user", "daemon-reload"])


