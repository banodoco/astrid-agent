"""Login service definitions and operations; all OS branching lives here."""

from __future__ import annotations

import ast
import inspect
import json
import os
import platform as system_platform
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

from runtime_protocol import platform

from ..core.execution.platform_helpers import owned_by_current_user
from .core import (
    Version,
    absolute,
    current,
    persist_install_identity,
    read_json,
    version_id,
    write_json,
)

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


UNSUPERVISED_WARNING = (
    "No active systemd user supervisor was detected for Astrid Runtime. "
    "The installer's detached fallback survives terminal closure, but does not "
    "restart after a crash, logout cleanup, container shutdown, or reboot. "
    "Run the install line to start it again. For automatic recovery, run "
    "astrid-runtime run under your container or another service supervisor; "
    "see docs/setup/local-source-install.md#linux-without-systemd. "
    "If you already use an external supervisor, verify its restart policy."
)


def supervision_warning(data_root: Path) -> str | None:
    """Read-only Linux check; external supervisor policies cannot be inferred."""
    if sys.platform != "linux":
        return None
    record = read_json(install_selection(Path.home(), sys.platform))
    if record.get("managed_by") == "pinokio":
        return None  # Pinokio explicitly owns this launch, rather than the fallback.
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "astrid.service", "--property=MainPID", "--value"],
            check=True, capture_output=True, text=True, timeout=3,
        )
        pid = int(result.stdout.strip())
        discovery = read_json(data_root / "runtime" / "discovery.json")
        if pid > 0 and pid == discovery.get("pid"):
            return None
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return UNSUPERVISED_WARNING


def installer_lock_path(home: Path, system: str) -> Path:
    parent = default_program_root(home, system).parent
    # The lock must survive removal of program files and workspace data.
    if system == "win32":
        parent = parent / "Astrid Installer"
    return parent / "astrid-install.lock"


def bootstrap_root(home: Path, system: str) -> Path:
    root = default_program_root(home, system)
    return (root.parent / "Astrid Installer" if system == "win32" else root) / "bootstrap"


def default_uv() -> Path:
    """The pinned user-local uv, unless the bootstrap supplies ASTRID_INSTALL_UV."""
    return Path(os.environ.get("ASTRID_INSTALL_UV", str(
        bootstrap_root(Path.home(), sys.platform) / ("uv.exe" if sys.platform == "win32" else "uv"))))


def pinokio_owned(record: dict) -> bool:
    return record.get("owner") == LABEL and record.get("managed_by") == "pinokio"


def launch_admission(version: Version, data: Path) -> bool:
    """Validate the selected managed launch; return whether it is activating.

    The pending journal selects a version, never a process or stop authority.
    This function is also embedded verbatim in the generated command prelude.
    """
    if version.root.parent.name != "versions":
        return False  # Source checkouts are outside the managed launch contract.
    root = absolute(version.root.parent.parent)
    path = absolute(root / "update-transaction.json")
    try:
        selected = current(root)
        if selected is None or selected.document() != version.document():
            raise ValueError("The managed launch is not the selected version.")
        if not path.exists():
            return False
        transaction = read_json(path)
        if (transaction.get("format") != "astrid-install-update/v1"
                or not isinstance(transaction.get("operation_id"), str)
                or re.fullmatch(r"[0-9a-f]{32}", transaction["operation_id"]) is None
                or transaction.get("transaction_kind") not in {None, "app", "migration"}
                or absolute(transaction["data_root"]) != absolute(data)):
            raise ValueError("Invalid update journal identity.")

        realm = transaction.get("realm")
        if (not isinstance(realm, dict) or not isinstance(realm.get("realm_id"), str)
                or not realm["realm_id"] or absolute(realm["realm_root"]) != Path(realm["realm_root"])):
            raise ValueError("Invalid update workspace identity.")
        terminal = transaction.get("step") in {"complete", "rolled_back"}

        def retained(value):
            candidate = Version.load(value)
            folder = absolute(root / "versions" / version_id(candidate.root.name))
            if (candidate.root != folder or not candidate.python.is_absolute()
                    or not candidate.python.is_relative_to(folder)
                    or not candidate.profile.is_relative_to(folder)
                    or ("identity" in value and value["identity"] != candidate.document()["identity"])
                    or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value["env"].items())
                    or any(str(candidate.document()[key]) != value[key] for key in ("root", "python", "profile"))
                    or ((not terminal or (folder / "version.json").exists())
                        and Version.load(read_json(absolute(folder / "version.json"))).document() != candidate.document())):
                raise ValueError("Invalid managed update version.")
            return candidate

        old = retained(transaction["old"])
        candidate = retained(transaction["candidate"]) if transaction.get("candidate") else None
        step = transaction.get("step")
        if step in {"complete", "rolled_back"}:
            return False
        allowed = candidate if step == "switched" else old if step == "rollback_selected" else None
        if allowed is None or allowed.document() != version.document():
            raise ValueError("The pending update does not permit this launch.")
        return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        action = ("Choose Pinokio's Update button to finish it."
                  if version.managed_by == "pinokio" else
                  "Run the install line to finish it: curl -fsSL https://astrid.haus/install | sh"
                  if os.name != "nt" else
                  "Run the install line to finish it: irm https://astrid.haus/install.ps1 | iex")
        raise ValueError("An Astrid update was interrupted; " + action) from exc


def command(version: Version, data: Path, *, windows: bool = False, log: Path | None = None,
            env_file: Path | None = None, extra_args: list[str] | None = None,
            lock_fd: int | None = None) -> list[str]:
    version = Version(version.root, version.python, version.profile,
                      dict(sorted(version.env.items())), version.release, version.managed_by)
    python = version.python.with_name("pythonw.exe") if windows else version.python
    arguments = ["run", "--profile", "astrid", "--data-root", str(data),
                 "--source-manifest", str(version.profile), *(extra_args or [])]
    # Every managed run enters the same admission, including POSIX services.
    source = globals().get("_ADMISSION_SOURCE") or inspect.getsource(launch_admission)
    script = ("import json,os,re,sys\nfrom pathlib import Path\n"
              "from astrid.installer.core import Version,absolute,current,read_json,version_id\n"
              + source + "\n"
              + f"launch_admission(Version(Path({str(version.root)!r}),Path({str(version.python)!r}),"
              + f"Path({str(version.profile)!r}),{version.env!r},{version.release!r},{version.managed_by!r}),Path({str(data)!r}))\n"
              + f"os.environ.update({version.env!r})\n")
    if lock_fd is not None:
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
           env_file: Path | None = None) -> str:
    log = log.with_name("runtime-service.log")
    argv = command(version, data, windows=system == "win32", log=log, env_file=env_file)
    service_env = dict(version.env)
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
        # Retain our child handle for bookkeeping; C1 and the live recorded
        # process identity decide whether an unpublished owner can be stopped.
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

    def _launchd_stop_identity(self, value: str, data: Path) -> tuple[dict, dict]:
        """Validate and snapshot the loaded, installer-owned LaunchAgent."""
        target = f"gui/{platform.current_user()}/{LABEL}"
        if not isinstance(value, str) or not value.strip():
            raise ValueError("The launchd service state is malformed.")
        rendered = value.rstrip("\r\n")
        if (not rendered.startswith(f"{target} = {{\n")
                or not rendered.endswith("\n}")):
            raise ValueError("The launchd service print result has an invalid target header.")
        starts = list(re.finditer(r"^[ \t]*arguments = \{[ \t]*$", value, re.MULTILINE))
        if len(starts) != 1:
            raise ValueError("The launchd service identity is malformed.")
        opening_indent = re.match(r"^([ \t]*)", value[starts[0].start():]).group(1)
        tail = value[starts[0].end():]
        closing = re.search(r"^" + re.escape(opening_indent) + r"\}[ \t]*$", tail, re.MULTILINE)
        if closing is None:
            raise ValueError("The launchd service arguments are malformed.")
        argument_text = tail[:closing.start()]
        # Parse only fields outside arguments; the Python command contains
        # ordinary source lines that can have the same names as print fields.
        metadata = value[:starts[0].start()] + tail[closing.end():]
        states = re.findall(r"^[ \t]*state = ([^\r\n]+)[ \t]*$", metadata, re.MULTILINE)
        pids = re.findall(r"^[ \t]*pid = (\d+)[ \t]*$", metadata, re.MULTILINE)
        if len(re.findall(r"^[ \t]*pid[ \t]*=", metadata, re.MULTILINE)) != len(pids):
            raise ValueError("The launchd service PID is malformed.")
        paths = re.findall(r"^[ \t]*path = ([^\r\n]+)[ \t]*$", metadata, re.MULTILINE)
        programs = re.findall(r"^[ \t]*program = ([^\r\n]+)[ \t]*$", metadata, re.MULTILINE)
        if len(states) != 1 or len(pids) > 1 or len(paths) != 1 or len(programs) != 1:
            raise ValueError("The launchd service identity is malformed.")
        state = states[0].strip()
        if state not in {"running", "not running"}:
            raise ValueError("The launchd service state is unknown; refusing to stop it.")
        pid = int(pids[0]) if pids else None
        if pid is not None and pid <= 0:
            raise ValueError("The launchd service PID is malformed.")
        # launchctl prints raw argv lines, including a multiline final script.
        # Only an exact action recognized by the retained-action validator can
        # establish ownership; unknown output is not stopped-state evidence.
        loaded_argv = None
        raw_arguments = argument_text.strip("\r\n")

        record = self.recorded()
        if (record.get("owner") != LABEL or record.get("program_root") != str(self.root)
                or record.get("data_root") != str(data)):
            raise ValueError("The launchd service has no matching install selection.")
        try:
            observed_stat = self.path.lstat()
            if not stat.S_ISREG(observed_stat.st_mode) or not owned_by_current_user(self.path, observed_stat):
                raise ValueError("The launchd service file is not installer-owned.")
            raw = self.path.read_bytes()
            document = plistlib.loads(raw)
            action = document.get("ProgramArguments")
            if (document.get("Label") != LABEL or not isinstance(action, list)
                    or any(not isinstance(part, str) for part in action)):
                raise ValueError("The launchd service configuration is malformed.")
            script = action[-1] if len(action) >= 5 and action[1:5] == ["-X", "utf8", "-I", "-c"] else None
            if not self._known_launch_action(action, data, windows=False, script=script):
                raise ValueError("The launchd service action is not owned by this installation.")
        except (OSError, TypeError, KeyError, plistlib.InvalidFileException) as exc:
            raise ValueError("The launchd service configuration is malformed.") from exc
        if loaded_argv is None:
            first_line = next((line for line in raw_arguments.splitlines() if line.strip()), "")
            arg_indent = re.match(r"^[ \t]*", first_line).group(0)
            argument_forms = {raw_arguments}
            if arg_indent:
                argument_forms.add("\n".join(
                    line[len(arg_indent):] if line.startswith(arg_indent) else line
                    for line in raw_arguments.split("\n")))
            for rendered in argument_forms:
                lines = rendered.split("\n")
                # A direct argv has one line per element. A generated `-c`
                # command has exactly five fixed prefix arguments and one
                # multiline final script. The retained-action validator then
                # accepts only known bytes, including retained legacy actions.
                possibilities = [lines]
                if len(lines) >= 6 and lines[1:5] == ["-X", "utf8", "-I", "-c"]:
                    possibilities.append(lines[:5] + ["\n".join(lines[5:])])
                for candidate in possibilities:
                    candidate_script = (candidate[-1] if len(candidate) >= 5
                                        and candidate[1:5] == ["-X", "utf8", "-I", "-c"] else None)
                    if (candidate and candidate[0] == programs[0].strip()
                            and self._known_launch_action(candidate, data, windows=False,
                                                          script=candidate_script)):
                        loaded_argv = candidate
                        break
                if loaded_argv is not None:
                    break
        loaded_script = (loaded_argv[-1] if loaded_argv and len(loaded_argv) >= 5
                         and loaded_argv[1:5] == ["-X", "utf8", "-I", "-c"] else None)
        if (loaded_argv is None or paths[0].strip() != str(self.path)
                or programs[0].strip() != loaded_argv[0]
                or not self._known_launch_action(loaded_argv, data, windows=False, script=loaded_script)):
            raise ValueError("The loaded launchd action does not match its owned configuration.")
        identity = {
            "target": target, "path": paths[0].strip(), "program": programs[0].strip(),
            "arguments": tuple(loaded_argv), "plist": raw,
            "plist_stat": tuple(getattr(observed_stat, key) for key in
                                 ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size",
                                  "st_mtime_ns", "st_ctime_ns")),
        }
        return identity, {"state": state, "pid": pid}

    def validate_stop_target(self, version: Version | None, data: Path, *, owner_pid: int | None,
                             expected_identity: dict | None = None,
                             require_stopped: bool = False) -> dict | None:
        """Read-only confirmation that the named OS job belongs to this owner.

        The install selection alone cannot authorize stopping a label that was
        replaced outside the installer. Runtime still owns the actual stop.
        """
        self._validated_systemd_stop = False
        if self.system == "darwin" and not require_stopped:
            self._validated_launchd_stop = None
            self._validated_launchd_stop_checked = False
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
                    if require_stopped:
                        self._validated_launchd_stop = None
                        self._validated_launchd_stop_checked = True
                    return None
                raise
            identity, observation = self._launchd_stop_identity(value, data)
            state, pid = observation["state"], observation["pid"]
            if expected_identity is not None and identity != expected_identity:
                raise ValueError("The loaded launchd service changed during Runtime stop.")
            if require_stopped:
                if state != "not running" or pid is not None:
                    raise ValueError("The launchd service did not remain stopped after Runtime exit.")
            elif state == "running":
                if owner_pid is None or pid != owner_pid:
                    raise ValueError("The launchd service does not match the selected Runtime owner.")
            elif pid is not None:
                raise ValueError("The launchd service does not match the selected Runtime owner.")
            if require_stopped:
                self._validated_launchd_stop = identity
                self._validated_launchd_stop_checked = True
            return identity
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
                argv = command(candidate, data, windows=True, log=self.log, env_file=env_file)
                allowed.append([{"Execute": argv[0], "Arguments": subprocess.list2cmdline(argv[1:])}])
        if actions not in allowed:
            raise ValueError("The scheduled task does not match the selected installation.")

    def _known_launch_action(self, action, data: Path, *, windows: bool, script: str | None) -> bool:
        """Compare historical generated bytes without executing retained code."""
        def encode(argv):
            return ([{"Execute": argv[0], "Arguments": subprocess.list2cmdline(argv[1:])}]
                    if windows else argv)

        transaction = read_json(self.root / "update-transaction.json")
        for folder in (self.root / "versions").iterdir():
            if folder.is_symlink() or not (folder / "version.json").is_file():
                continue
            retained = Version.load(read_json(folder / "version.json"))
            if retained.root != absolute(folder):
                raise ValueError("The retained service version is outside this installation.")
            for env_file in (None, self.env_file):
                prior = command(retained, data, windows=windows, log=self.log, env_file=env_file)
                # This is the exact historical wrapper suffix, obtained from
                # the sole command builder rather than a second argv builder.
                body = prior[-1][prior[-1].index("os.environ.update("):]
                environment, suffix = body.split("\n", 1)
                # Historical dictionaries preserve insertion order in the
                # action text, whereas version.json sorts their keys. Compare
                # their values without granting arbitrary Python execution.
                if script is not None:
                    lines = script.splitlines()
                    if len(lines) >= 3 and lines[2].startswith("os.environ.update(") and lines[2].endswith(")"):
                        try:
                            historical_env = ast.literal_eval(lines[2][len("os.environ.update("):-1])
                        except (ValueError, SyntaxError):
                            continue
                        if historical_env != retained.env:
                            continue
                        environment = lines[2]
                header = "import json,os,sys\nfrom pathlib import Path\n" + environment + "\n"
                legacy = header + "os.environ.pop('ASTRID_INSTALLER_OPERATION_ID',None)\n" + suffix
                if action in (encode([*prior[:-1], legacy]), encode(prior)):
                    return True
                if not windows and env_file is None:
                    arguments = ["run", "--profile", "astrid", "--data-root", str(data),
                                 "--source-manifest", str(retained.profile)]
                    if action == [str(retained.python), "-X", "utf8", "-I", "-m", "astrid_runtime", *arguments]:
                        return True
                # A retained in-flight bound action must still match its exact
                # replay journal, version and code before it can be migrated.
                replay_id = transaction.get("operation_id")
                if isinstance(replay_id, str) and re.fullmatch(r"[0-9a-f]{32}", replay_id):
                    historical_version = repr(retained.document())
                    if script is not None:
                        for line in script.splitlines():
                            if line.startswith("_astrid_version="):
                                literal = line[len("_astrid_version="):]
                                try:
                                    if ast.literal_eval(literal) != retained.document():
                                        continue
                                except (ValueError, SyntaxError):
                                    continue
                                historical_version = literal
                    binding = (
                        f"_astrid_operation_id={replay_id!r}\n"
                        f"_astrid_version={historical_version}\n"
                        f"_astrid_transaction_path=Path({str(self.root / 'update-transaction.json')!r})\n"
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
                    legacy = header + binding + suffix
                    if action == encode([*prior[:-1], legacy]):
                        return True
        return False

    def _publish_posix_launch_admission(self, version: Version, data: Path) -> None:
        if not self.path.exists() and not self.path.is_symlink():
            return  # Autostart off stays off.
        message = f"The Astrid service {self.path} has an unknown or changed action/owner. Re-run the install line to repair it."
        selection = self.recorded()
        observed_stat = self.path.lstat()
        if (not stat.S_ISREG(observed_stat.st_mode) or not owned_by_current_user(self.path, observed_stat)
                or selection.get("owner") != LABEL or selection.get("program_root") != str(self.root)
                or selection.get("data_root") != str(data)):
            raise ValueError(message)
        observed = self.path.read_bytes()

        def confirm_unchanged():
            latest = self.path.lstat()
            if (any(getattr(latest, key) != getattr(observed_stat, key) for key in
                    ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns"))
                    or self.path.read_bytes() != observed):
                raise ValueError(message)

        def flush_current():
            # Also completes a prior rename whose directory flush failed.
            confirm_unchanged()
            with self.path.open("rb") as stream:
                os.fsync(stream.fileno())
            platform.fsync_directory(self.path.parent)
            confirm_unchanged()

        canonical = command(version, data, env_file=self.env_file)
        try:
            if self.system == "darwin":
                document = plistlib.loads(observed)
                if document.get("Label") != LABEL:
                    raise ValueError(message)
                action = document["ProgramArguments"]
                environment = document.get("EnvironmentVariables", {})
                if not isinstance(environment, dict):
                    raise ValueError(message)
                if action == canonical and "ASTRID_INSTALLER_OPERATION_ID" not in environment:
                    flush_current()
                    return
                script = action[-1] if isinstance(action, list) and action[1:5] == ["-X", "utf8", "-I", "-c"] else None
                if not self._known_launch_action(action, data, windows=False, script=script):
                    raise ValueError(message)
                document["ProgramArguments"] = canonical
                environment.pop("ASTRID_INSTALLER_OPERATION_ID", None)
                replacement = plistlib.dumps(document, fmt=plistlib.FMT_BINARY if observed.startswith(b"bplist") else plistlib.FMT_XML)
            else:
                text = observed.decode("utf-8")
                # Accept only the exact quoted action syntax emitted by render().
                lines = text.splitlines(keepends=True)
                actions = [line for line in lines if line.lstrip().startswith("ExecStart=")]
                if len(actions) != 1 or not actions[0].startswith("ExecStart="):
                    raise ValueError(message)
                section = None
                for line in lines:
                    if line.strip().startswith("["):
                        section = line.strip()
                    if line == actions[0] and section != "[Service]":
                        raise ValueError(message)
                encoded = actions[0].removeprefix("ExecStart=").removesuffix("\n")
                tokens = re.findall(r'"(?:\\.|[^"\\])*"', encoded)
                def decode(token):
                    return re.sub(r'\\([\\"n])', lambda m: "\n" if m[1] == "n" else m[1], token[1:-1]).replace("%%", "%").replace("$$", "$")
                action = [decode(token) for token in tokens]
                def encode(argv):
                    return " ".join(systemd_quote(arg).replace("$", "$$") for arg in argv)
                if encode(action) != encoded:
                    raise ValueError(message)
                bindings = [line for line in lines if "ASTRID_INSTALLER_OPERATION_ID=" in line
                            and line.lstrip().startswith("Environment=")]
                if any(re.fullmatch(r'Environment="ASTRID_INSTALLER_OPERATION_ID=[0-9a-f]{32}"\n', line) is None
                       for line in bindings):
                    raise ValueError(message)
                if action == canonical and not bindings:
                    flush_current()
                    if self.systemd():
                        self.run(["systemctl", "--user", "daemon-reload"])
                    return
                script = action[-1] if action[1:5] == ["-X", "utf8", "-I", "-c"] else None
                if not self._known_launch_action(action, data, windows=False, script=script):
                    raise ValueError(message)
                replacement = "".join("ExecStart=" + encode(canonical) + "\n" if line == actions[0] else line
                                      for line in lines if line not in bindings).encode("utf-8")
        except (KeyError, TypeError, IndexError, AttributeError, UnicodeError, plistlib.InvalidFileException) as exc:
            raise ValueError(message) from exc
        # Keep properties and permissions; recheck the observed file immediately
        # before atomic publication. A failed write never truncates the old unit.
        with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".astrid-", delete=False) as pending:
            temporary = Path(pending.name)
            try:
                pending.write(replacement)
                pending.flush()
                os.fchmod(pending.fileno(), stat.S_IMODE(observed_stat.st_mode))
                os.fsync(pending.fileno())
                confirm_unchanged()
                platform.durable_replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
        if self.system == "linux" and self.systemd():
            self.run(["systemctl", "--user", "daemon-reload"])

    def publish_launch_admission(self, version: Version, data: Path) -> None:
        """Publish the canonical gate for an exactly known owned service action."""
        if self.system != "win32":
            self._publish_posix_launch_admission(version, data)
            return
        query = TASK_QUERY + "if ($t) { ConvertTo-Json -Compress -InputObject @($t.Actions | Select-Object Execute,Arguments) }"
        observed = self.powershell(query).strip()
        if not observed:
            return
        action = json.loads(observed)
        argv = command(version, data, windows=True, log=self.log, env_file=self.env_file)
        canonical = [{"Execute": argv[0], "Arguments": subprocess.list2cmdline(argv[1:])}]
        if action == canonical:
            return
        selection = self.recorded()
        if (selection.get("owner") != LABEL or selection.get("program_root") != str(self.root)
                or selection.get("data_root") != str(data)):
            raise ValueError("The Astrid Runtime scheduled task has no matching install selection. Re-run the install line to repair it.")
        # Decode only the final argument emitted by list2cmdline. Re-encoding
        # must reproduce the entire action; no shell/code is executed here.
        script = None
        if (isinstance(action, list) and len(action) == 1 and isinstance(action[0], dict)
                and isinstance(action[0].get("Arguments"), str)):
            prefix = "-X utf8 -I -c "
            encoded = action[0]["Arguments"].removeprefix(prefix)
            if action[0]["Arguments"].startswith(prefix) and encoded.startswith('"') and encoded.endswith('"'):
                def unquote(match):
                    slashes = len(match.group(1))
                    if slashes % 2 == 0:
                        raise ValueError("Invalid quoted task action.")
                    return "\\" * (slashes // 2) + '"'
                try:
                    decoded = re.sub(r'(\\*)"', unquote, encoded[1:-1])
                    if subprocess.list2cmdline([decoded]) == encoded:
                        script = decoded
                except ValueError:
                    pass
        if not self._known_launch_action(action, data, windows=True, script=script):
            raise ValueError("The Astrid Runtime scheduled task has an unknown action. Re-run the install line to repair it.")
        def quote(value):
            return "'" + value.replace("'", "''") + "'"
        # Recheck exact observed bytes in the same Scheduler operation before
        # changing only the action; triggers/settings/enablement survive.
        self.powershell(TASK_QUERY +
            "if (-not $t) { throw 'The Astrid Runtime scheduled task disappeared.' }; " +
            "$prior=ConvertTo-Json -Compress -InputObject @($t.Actions | Select-Object Execute,Arguments); " +
            f"if ($prior -cne {quote(observed)}) {{ throw 'The Astrid Runtime scheduled task action changed.' }}; " +
            f"$a=New-ScheduledTaskAction -Execute {quote(argv[0])} " +
            f"-Argument {quote(subprocess.list2cmdline(argv[1:]))} -WorkingDirectory {quote(str(version.root))}; " +
            "$t | Set-ScheduledTask -Action $a | Out-Null")

    def disable(self) -> None:
        if self.system == "darwin":
            try:
                selected = getattr(self, "_validated_launchd_stop", None)
                checked = getattr(self, "_validated_launchd_stop_checked", False)
                if not checked and not self.path.exists():
                    return  # Autostart is off; there is no loaded file to unload.
                if not checked:
                    raise RuntimeError("LaunchAgent bootout requires the validated Runtime C1 stop callback.")
                if selected is not None and not self.path.exists():
                    raise ValueError("The selected launchd service file disappeared before bootout.")
                record = self.recorded()
                data = Path(record["data_root"])
                latest = self.validate_stop_target(
                    None, data, owner_pid=None, expected_identity=selected, require_stopped=True)
                if selected is None and latest is not None:
                    raise ValueError("A launchd service appeared after the C1 stop admission.")
                if latest is None:
                    return  # Already unloaded after the C1 callback.
                # Reprint and compare exact loaded identity immediately at
                # the bootout boundary; C1 remains the only stop authority.
                target = f"gui/{platform.current_user()}/{LABEL}"
                self.run(["launchctl", "bootout", target])
            finally:
                self._validated_launchd_stop = None
                self._validated_launchd_stop_checked = False
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

    def stop_foreground(self, *, expected: dict | None = None) -> None:
        """Stop the recorded incarnation when called under Runtime's C1 fence."""
        if expected is None:
            raise RuntimeError("Use Runtime's validated installer stop handoff for foreground cleanup.")
        record = self.recorded()
        if record.get("foreground") != expected:
            raise ValueError("The selected foreground owner changed before stop.")
        catalog = read_json(absolute(record["data_root"]) / "runtime" / "catalog.json")
        realm_id = catalog.get("selected_realm_id")
        realm = next((row for row in catalog.get("realms", [])
                      if row.get("realm_id") == realm_id), None)
        expected_realm = expected.get("realm_id", record.get("realm_id"))
        expected_root = expected.get("realm_root", record.get("realm_root"))
        if (not realm_id or realm is None or expected_realm != realm_id
                or not isinstance(expected_root, str)
                or absolute(expected_root) != absolute(realm["data_root"])):
            raise ValueError("The foreground process does not match the selected workspace.")
        pid, birth = expected.get("pid"), expected.get("birth_id")
        if type(pid) is not int or pid <= 0 or not isinstance(birth, str) or not birth:
            raise ValueError("The foreground process identity is incomplete.")
        identity = platform.process_identity(pid)
        if identity is None:
            return
        if identity.get("pid") != pid or identity.get("birth_id") != birth:
            raise ValueError("The foreground process identity changed; refusing to signal it.")
        if identity.get("uid") != platform.current_user():
            raise ValueError("The foreground process user changed; refusing to signal it.")
        platform.signal_process(identity, platform.TERM)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            observed = platform.process_identity(pid)
            if observed is None or observed.get("birth_id") != birth:
                return
            time.sleep(0.1)
        raise RuntimeError("The foreground Astrid Runtime did not finish stopping.")

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

    def register(self, version: Version, data: Path) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.env_file.exists():
            write_json(self.env_file, {})
        settings = self.settings()
        user = self.powershell("[Security.Principal.WindowsIdentity]::GetCurrent().User.Value").strip() if self.system == "win32" else ""
        self.path.write_text(render(
            self.system, version, data, self.log, user=user, env_file=self.env_file,
        ), encoding="utf-8", newline="\n")
        if self.system == "darwin":
            self.run(["launchctl", "bootstrap", f"gui/{platform.current_user()}", self.path])
        elif self.system == "linux":
            if self.systemd():
                self.run(["systemctl", "--user", "daemon-reload"])
                self.run(["systemctl", "--user", "enable", "astrid.service"])
            else:
                self.say(UNSUPERVISED_WARNING)
        else:
            xml_path = str(self.path).replace("'", "''")
            self.powershell(f"Register-ScheduledTask -TaskName '{TASK}' -Xml ([IO.File]::ReadAllText('{xml_path}')) -Force | Out-Null")
        persist_install_identity(version, self.root, selection=self.selection, fields={
            "owner": LABEL, "program_root": str(self.root),
            "data_root": str(data), "service_settings": settings,
        })

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
        """Detach one Runtime process without crash recovery or boot startup."""
        env = {k: v for k, v in os.environ.items() if k not in {
            "PYTHONPATH", "PYTHONHOME", "ASTRID_WORKSPACE_ENDPOINT",
            "ASTRID_WORKSPACE_CREDENTIAL", "ASTRID_RUNTIME_SOURCE_MANIFEST",
            "ASTRID_RUNTIME_DATA_ROOT", "ASTRID_PACKS_PATH"}}
        env.update(version.env)
        with self.log.open("a", encoding="utf-8") as stream:
            process = platform.spawn_tree(command(
                version, data, env_file=self.env_file,
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


