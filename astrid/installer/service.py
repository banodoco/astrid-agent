"""Login service definitions and operations; all OS branching lives here."""

from __future__ import annotations

import json
import os
import platform as system_platform
import plistlib
import subprocess
import sys
import time
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


def installer_lock_path(home: Path, system: str) -> Path:
    parent = default_program_root(home, system).parent
    # The lock must survive removal of program files and workspace data.
    if system == "win32":
        parent = parent / "Astrid Installer"
    return parent / "astrid-install.lock"


def bootstrap_root(home: Path, system: str) -> Path:
    root = default_program_root(home, system)
    return (root.parent / "Astrid Installer" if system == "win32" else root) / "bootstrap"


def command(version: Version, data: Path, *, windows: bool = False, log: Path | None = None,
            env_file: Path | None = None, extra_args: list[str] | None = None,
            lock_fd: int | None = None, installer_operation_id: str | None = None) -> list[str]:
    python = version.python.with_name("pythonw.exe") if windows else version.python
    arguments = ["run", "--profile", "astrid", "--data-root", str(data),
                 "--source-manifest", str(version.profile), *(extra_args or [])]
    if (env_file is None and not windows and lock_fd is None and not installer_operation_id
            and "ASTRID_INSTALLER_OPERATION_ID" not in os.environ
            and "ASTRID_INSTALLER_OPERATION_ID" not in version.env):
        return [str(python), "-X", "utf8", "-I", "-m", "astrid_runtime", *arguments]
    # Task Scheduler cannot set an environment and Hidden does not hide a
    # console. pythonw executes the foreground Runtime in this same process.
    script = "import json,os,sys\nfrom pathlib import Path\n"
    script += f"os.environ.update({version.env!r})\n"
    if installer_operation_id:
        transaction_path = version.root.parent.parent / "update-transaction.json"
        script += f"_astrid_operation_id={installer_operation_id!r}\n"
        script += f"_astrid_version={version.document()!r}\n"
        script += f"_astrid_transaction_path=Path({str(transaction_path)!r})\n"
        script += (
            "try:\n"
            "    _astrid_tx=json.loads(_astrid_transaction_path.read_text(encoding='utf-8'))\n"
            "    _astrid_intent=_astrid_tx.get('launch_intent') or {}\n"
            "    _astrid_authorized=(_astrid_tx.get('step') not in {'complete','rolled_back'} "
            "and _astrid_tx.get('operation_id') == _astrid_operation_id "
            "and _astrid_intent.get('operation_id') == _astrid_operation_id "
            "and _astrid_intent.get('purpose') in {'backup','migration','service_start','pinokio_start'} "
            "and _astrid_intent.get('version') == _astrid_version "
            "and _astrid_version in (_astrid_tx.get('old'),_astrid_tx.get('candidate')))\n"
            "except (OSError,ValueError,TypeError,AttributeError):\n"
            "    _astrid_authorized=False\n"
            "if _astrid_authorized: os.environ['ASTRID_INSTALLER_OPERATION_ID']=_astrid_operation_id\n"
            "else: os.environ.pop('ASTRID_INSTALLER_OPERATION_ID',None)\n"
        )
    else:
        script += "os.environ.pop('ASTRID_INSTALLER_OPERATION_ID',None)\n"
    if lock_fd is not None:
        # The foreground owner inherited this locked descriptor across exec.
        # Keep it open here, but never pass it to a subsequently exec'd child.
        script += f"os.set_inheritable({lock_fd},False)\n"
    if windows:
        if log is None:
            raise ValueError("A log path is required for the Windows Runtime service.")
        log = log.with_name("runtime-service.log")
        script += f"sys.stdout=sys.stderr=open({str(log)!r},'a',encoding='utf-8',buffering=1)\n"
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


def render(system: str, version: Version, data: Path, log: Path, *, user: str = "",
           env_file: Path | None = None, installer_operation_id: str | None = None) -> str:
    log = log.with_name("runtime-service.log")
    argv = command(version, data, windows=system == "win32", log=log, env_file=env_file,
                   installer_operation_id=installer_operation_id)
    service_env = {key: value for key, value in version.env.items()
                   if key != "ASTRID_INSTALLER_OPERATION_ID"}
    if installer_operation_id:
        service_env["ASTRID_INSTALLER_OPERATION_ID"] = installer_operation_id
    if system == "darwin":
        return plistlib.dumps({
            "Label": LABEL, "ProgramArguments": argv, "RunAtLoad": True,
            # KeepAlive's SuccessfulExit condition is false: launchd respawns
            # crashes, while an orderly Runtime exit stays stopped.
            "KeepAlive": {"SuccessfulExit": False}, "ExitTimeOut": 90,
            "EnvironmentVariables": service_env,
            "StandardOutPath": str(log), "StandardErrorPath": str(log),
        }, sort_keys=True).decode("utf-8")
    if system == "linux":
        executable = " ".join(systemd_quote(s).replace("$", "$$") for s in argv)
        environment = "".join("Environment=" + systemd_quote(k + "=" + v) + "\n" for k, v in sorted(service_env.items()))
        return ("[Unit]\nDescription=Astrid Runtime\n\n[Service]\n"
                f"ExecStart={executable}\n{environment}Restart=on-failure\nRestartSec=5\n"
                "KillMode=mixed\nTimeoutStopSec=90\n"
                f"StandardOutput={systemd_quote('append:' + str(log))}\n"
                f"StandardError={systemd_quote('append:' + str(log))}\n"
                "\n[Install]\nWantedBy=default.target\n")
    if system != "win32":
        raise ValueError(f"Unsupported operating system: {system}")
    arguments = subprocess.list2cmdline(argv[1:])
    # Register-ScheduledTask's -Xml parameter receives a PowerShell string,
    # not encoded bytes. An encoding declaration makes Task Scheduler reject
    # that string because PowerShell cannot provide the declared byte encoding.
    return f'''<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Astrid Runtime</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger></Triggers>
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
        # Only this launch controller retains custody of an unpublished child.
        # A persisted PID/birth receipt alone never authorizes its termination.
        self._foreground_process = None
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

    def login_service_exists(self) -> bool:
        if self.system == "win32":
            return bool(self.powershell(TASK_QUERY + "if ($t) { $t.TaskName }").strip())
        return self.path.exists()

    def systemd(self) -> bool:
        if self.system != "linux":
            return False
        try:
            self.run(["systemctl", "--user", "show-environment"])
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def validate_stop_target(self, version: Version | None, data: Path, *, owner_pid: int | None,
                             installer_operation_id: str | None = None,
                             installer_launch_version: Version | None = None) -> None:
        """Read-only confirmation that the named OS job belongs to this owner.

        The install selection alone cannot authorize stopping a label that was
        replaced outside the installer. Runtime still owns the actual stop.
        """
        self._validated_systemd_stop = False
        if self.system == "linux":
            if not self.systemd():
                foreground = self.recorded().get("foreground")
                if self.path.exists() and (
                    not isinstance(foreground, dict)
                    or not foreground.get("birth_id")
                    or (owner_pid is not None and foreground.get("pid") != owner_pid)
                ):
                    raise ValueError("Cannot validate the registered systemd manager; refusing to stop it.")
                return  # The retained foreground child is Runtime's to validate.
            value = self.run(["systemctl", "--user", "show", "astrid.service",
                              "--property=MainPID,FragmentPath,LoadState"])
            fields = dict(line.split("=", 1) for line in value.splitlines() if "=" in line)
            pid = int(fields["MainPID"])
            if fields.get("LoadState") == "not-found" and pid == 0:
                return
            if (fields.get("FragmentPath") != str(self.path)
                    or pid not in {0, owner_pid}):
                raise ValueError("The systemd service does not match the selected Runtime owner.")
            self._validated_systemd_stop = True
            return
        if self.system == "darwin":
            target = f"gui/{platform.current_user()}/{LABEL}"
            try:
                value = self.run(["launchctl", "print", target])
            except subprocess.CalledProcessError as exc:
                if "Could not find service" in str(exc.output) or "Could not find specified service" in str(exc.output):
                    return
                raise
            import re

            match = re.search(r"^\s*pid = (\d+)\s*$", value, re.MULTILINE)
            if match is None or int(match[1]) != owner_pid:
                raise ValueError("The launchd service does not match the selected Runtime owner.")
            return
        # Task Scheduler exposes the registered action, rather than a reliable
        # owner PID. Compare it with the command for an owned retained version.
        value = self.powershell(TASK_QUERY + "if ($t) { ConvertTo-Json -Compress -InputObject @($t.Actions | Select-Object Execute,Arguments) }")
        if not value.strip():
            return
        actions = json.loads(value)
        candidates = [version] if version is not None else []
        for folder in (self.root / "versions").glob("*"):
            if folder.is_dir() and not folder.is_symlink() and (folder / "version.json").is_file():
                candidate = Version.load(read_json(folder / "version.json"))
                if candidate.root != folder:
                    raise ValueError("The retained service version is outside this installation.")
                candidates.append(candidate)
        allowed = []
        for candidate in candidates:
            for env_file in (None, self.env_file):
                launch_version = (installer_launch_version is not None
                                  and candidate.document() == installer_launch_version.document())
                launch_ids = ((installer_operation_id, None)
                              if launch_version and installer_operation_id else (None,))
                for launch_id in launch_ids:
                    argv = command(candidate, data, windows=True, log=self.log, env_file=env_file,
                                   installer_operation_id=launch_id)
                    allowed.append([{"Execute": argv[0], "Arguments": subprocess.list2cmdline(argv[1:])}])
        if actions not in allowed:
            raise ValueError("The scheduled task does not match the selected installation.")

    def disable(self) -> None:
        if self.system == "darwin":
            if self.path.exists():
                # This runs in the post-fence stop callback. Unload the job for
                # this GUI session, but retain its LaunchAgents plist so
                # launchd can honor the saved autostart choice next login.
                target = f"gui/{platform.current_user()}/{LABEL}"
                try:
                    self.run(["launchctl", "print", target])
                except subprocess.CalledProcessError as exc:
                    if "Could not find service" not in str(exc.output) and "Could not find specified service" not in str(exc.output):
                        raise
                    return  # Already unloaded after an orderly Runtime stop.
                self.run(["launchctl", "bootout", target])
            return
        if self.system == "linux":
            available = self.systemd()
            if getattr(self, "_validated_systemd_stop", False) and not available:
                raise RuntimeError("The validated systemd manager is no longer available.")
            # Do not disable the unit: an orderly Runtime exit plus systemctl
            # stop below preserves its saved enablement for the next login.
            return
        # Linux systemctl stop and Windows Stop-ScheduledTask stop the current
        # invocation without changing whether the service starts at sign-in.

    def stop_foreground(self) -> None:
        """Legacy entry point: Runtime owns unpublished-child stop authority."""
        raise RuntimeError("Use Runtime's validated installer stop handoff for foreground cleanup.")

    def stop(self) -> None:
        if self.system == "linux":
            available = self.systemd()
            if getattr(self, "_validated_systemd_stop", False) and not available:
                raise RuntimeError("The validated systemd manager is no longer available.")
            if available and self.path.exists():
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

    def register(self, version: Version, data: Path, *, installer_operation_id: str | None = None,
                 refresh_only: bool = False) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.env_file.exists():
            write_json(self.env_file, {})
        settings = self.settings()
        user = self.powershell("[Security.Principal.WindowsIdentity]::GetCurrent().User.Value").strip() if self.system == "win32" else ""
        self.path.write_text(render(
            self.system, version, data, self.log, user=user, env_file=self.env_file,
            installer_operation_id=installer_operation_id,
        ), encoding="utf-8", newline="\n")
        if self.system == "darwin":
            if not refresh_only:
                self.run(["launchctl", "bootstrap", f"gui/{platform.current_user()}", self.path])
        elif self.system == "linux":
            if self.systemd():
                self.run(["systemctl", "--user", "daemon-reload"])
                if not refresh_only:
                    self.run(["systemctl", "--user", "enable", "astrid.service"])
            else:
                self.say("There is no systemd user service; run the install line to restart Astrid.")
        else:
            xml_path = str(self.path).replace("'", "''")
            self.powershell(f"Register-ScheduledTask -TaskName '{TASK}' -Xml ([IO.File]::ReadAllText('{xml_path}')) -Force | Out-Null")
        record = self.recorded()
        record.pop("uninstalled", None)
        record = {**record, "owner": LABEL, "program_root": str(self.root),
                  "data_root": str(data), "service_settings": settings,
                  "identity": {"release": version.release, "managed_by": version.managed_by}}
        write_json(self.selection, record)

    def clear_launch_operation_id(self, version: Version, data: Path) -> None:
        """Rewrite the on-disk manager spec without triggering another start."""
        self.register(version, data, refresh_only=True)

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

    def start_foreground(self, version: Version, data: Path, *, installer_operation_id: str | None = None) -> None:
        """No systemd: detach Runtime's own supervisor; the line is the restart."""
        env = {k: v for k, v in os.environ.items() if k not in {
            "PYTHONPATH", "PYTHONHOME", "ASTRID_WORKSPACE_ENDPOINT",
            "ASTRID_WORKSPACE_CREDENTIAL", "ASTRID_RUNTIME_SOURCE_MANIFEST",
            "ASTRID_RUNTIME_DATA_ROOT", "ASTRID_PACKS_PATH", "ASTRID_INSTALLER_OPERATION_ID"}}
        env.update(version.env)
        env.pop("ASTRID_INSTALLER_OPERATION_ID", None)
        if installer_operation_id:
            env["ASTRID_INSTALLER_OPERATION_ID"] = installer_operation_id
        with self.log.open("a", encoding="utf-8") as stream:
            process = platform.spawn_tree(command(
                version, data, env_file=self.env_file,
                installer_operation_id=installer_operation_id,
            ), detached=True,
                                          cwd=version.root, env=env, stdin=subprocess.DEVNULL,
                                          stdout=stream, stderr=subprocess.STDOUT)
        self._foreground_process = process
        identity = platform.process_identity(process.pid)
        if identity is None:
            raise RuntimeError("The foreground Astrid Runtime exited before startup.")
        record = self.recorded()
        catalog = read_json(data / "runtime" / "catalog.json")
        realm_id = catalog.get("selected_realm_id")
        realm = next((row for row in catalog.get("realms", [])
                      if row.get("realm_id") == realm_id), {})
        foreground = {"pid": process.pid, "birth_id": identity["birth_id"]}
        if realm_id and realm.get("data_root"):
            foreground.update(realm_id=realm_id, realm_root=realm["data_root"])
        write_json(self.selection, {**record, "foreground": foreground})

    def unregister(self) -> None:
        if self.system == "win32":
            self.powershell(TASK_QUERY + "if ($t) { $t | Unregister-ScheduledTask -Confirm:$false }")
        self.path.unlink(missing_ok=True)
        if self.system == "linux" and self.systemd():
            self.run(["systemctl", "--user", "daemon-reload"])


