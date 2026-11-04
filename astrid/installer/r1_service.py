"""R1 service-manager boundary for a stopped R0 installation.

The transaction owns ordering: stop R0, prepare R1, register, switch current,
then activate.  This module never inspects R0's process or data files.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

from . import r0_stop
from .service import systemd_quote

R1_LABEL = "haus.astrid.runtime.r1"
R1_UNIT = "astrid-r1.service"
R1_TASK = "Astrid Runtime R1"

# Runs under the interpreter selected by the fixed launcher. The environment
# belongs to the selected version, not to the service definition.
_RUNNER = '''import json, os, re, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
data = sys.argv[2]
name = sys.argv[3]
if (root / "current").read_text(encoding="utf-8").strip() != name:
    raise SystemExit("Astrid current version changed during launch.")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
    raise SystemExit("Invalid Astrid current version.")
folder = root / "versions" / name
record = json.loads((folder / "version.json").read_text(encoding="utf-8"))
if Path(record["root"]).resolve() != folder.resolve():
    raise SystemExit("Invalid Astrid version root.")
profile = Path(record["profile"]).resolve()
if not profile.is_relative_to(folder.resolve()):
    raise SystemExit("Invalid Astrid version profile.")
env = record["env"]
if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
    raise SystemExit("Invalid Astrid version environment.")
os.environ.update(env)
settings_file = root / "service-env.json"
settings = json.loads(settings_file.read_text(encoding="utf-8")) if settings_file.exists() else {}
if not isinstance(settings, dict) or set(settings) - {"ASTRID_WARM_IDLE_SECONDS", "ASTRID_ALLOW_UNQUALIFIED_GPU"} or any(not isinstance(v, str) for v in settings.values()):
    raise SystemExit("Invalid Astrid service settings.")
os.environ.update(settings)
from astrid_runtime.cli import main
raise SystemExit(main(["run", "--profile", "astrid", "--data-root", data, "--source-manifest", str(profile)]))
'''

_POSIX = '''#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
version=$(cat "$root/current")
case "$version" in ''|[!A-Za-z0-9]*|*[!A-Za-z0-9._-]*) echo 'Invalid Astrid current version.' >&2; exit 2;; esac
[ ${#version} -le 64 ] || exit 2
exec "$root/versions/$version/py/bin/python" -X utf8 -I "$root/bin/runtime.py" "$root" "$1" "$version"
'''

_WINDOWS = r'''$ErrorActionPreference = 'Stop'
$root = $args[0]
$version = ([IO.File]::ReadAllText((Join-Path $root 'current'))).Trim()
if ($version -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') { throw 'Invalid Astrid current version.' }
$python = Join-Path $root "versions\$version\py\Scripts\python.exe"
& $python -X utf8 -I (Join-Path $root 'bin\runtime.py') $root $args[1] $version *>> $args[2]
exit $LASTEXITCODE
'''


def _path(service) -> Path:
    if service.system == "linux":
        return service.path.with_name(R1_UNIT)
    if service.system == "darwin":
        return service.path.with_name(R1_LABEL + ".plist")
    if service.system == "win32":
        return service.root / "task-r1.xml"
    raise ValueError("Unsupported R1 service manager.")


def _ps(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _render(service, data: Path, user: str = "") -> str:
    launcher = service.root / "bin" / "runtime"
    log = service.log.with_name("runtime-service.log")
    if service.system == "linux":
        args = " ".join(systemd_quote(str(x)).replace("$", "$$") for x in (launcher, data))
        output = systemd_quote("append:" + str(log))
        return ("[Unit]\nDescription=Astrid Runtime R1\n\n[Service]\n"
                f"ExecStart={args}\nRestart=on-failure\nRestartSec=5\nKillMode=mixed\nTimeoutStopSec=90\n"
                f"StandardOutput={output}\nStandardError={output}\n\n[Install]\nWantedBy=default.target\n")
    if service.system == "darwin":
        return plistlib.dumps({"Label": R1_LABEL, "ProgramArguments": [str(launcher), str(data)],
            "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ExitTimeOut": 90,
            "StandardOutPath": str(log), "StandardErrorPath": str(log)}, sort_keys=True).decode()
    if service.system != "win32":
        raise ValueError("Unsupported R1 service manager.")
    # PowerShell reads the extensionless fixed launcher; Task Scheduler invokes
    # the OS interpreter because Windows cannot execute an extensionless script.
    command = "& ([scriptblock]::Create([IO.File]::ReadAllText(" + _ps(str(launcher)) + "))) " + _ps(str(service.root)) + " " + _ps(str(data)) + " " + _ps(str(log))
    args = subprocess.list2cmdline(["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command])
    return f'''<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<RegistrationInfo><Description>Astrid Runtime R1</Description></RegistrationInfo>
<Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger></Triggers>
<Principals><Principal id="Owner"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable><AllowStartOnDemand>true</AllowStartOnDemand><Enabled>false</Enabled><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure></Settings>
<Actions Context="Owner"><Exec><Command>powershell.exe</Command><Arguments>{escape(args)}</Arguments><WorkingDirectory>{escape(str(service.root))}</WorkingDirectory></Exec></Actions></Task>'''


def _create(path: Path, contents: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())


def register_r1(service, data: Path) -> None:
    """Create a distinct, initially inactive R1 entry; never replace R0."""
    data = Path(data)
    if not data.is_absolute() or not service.root.is_absolute():
        raise ValueError("Program and R1 data roots must be absolute.")
    path = _path(service)
    if service.system == "win32":
        exists = service.powershell(f"Get-ScheduledTask | Where-Object {{ $_.TaskName -eq {_ps(R1_TASK)} -and $_.TaskPath -eq '\\' }} | ForEach-Object {{ $_.TaskName }}")
        if exists.strip():
            raise ValueError("The R1 scheduled task already exists.")
        user = service.powershell("[Security.Principal.WindowsIdentity]::GetCurrent().User.Value").strip()
    else:
        user = ""
    # Exclusive creates refuse foreign or stale entries, including symlinks.
    created = []
    try:
        for target, content, mode in (
            (service.root / "bin" / "runtime.py", _RUNNER, 0o600),
            (service.root / "bin" / "runtime", _WINDOWS if service.system == "win32" else _POSIX, 0o755),
            (path, _render(service, data, user), 0o600),
        ):
            _create(target, content, mode)
            created.append(target)
    except BaseException:
        for target in reversed(created):
            target.unlink(missing_ok=True)
        raise
    if service.system == "linux":
        service.run(["systemctl", "--user", "daemon-reload"])
    elif service.system == "win32":
        service.powershell(f"Register-ScheduledTask -TaskName {_ps(R1_TASK)} -Xml ([IO.File]::ReadAllText({_ps(str(path))})) | Out-Null")


def activate_r1(service) -> None:
    """Enable and start only after the caller has switched current to R1."""
    path = _path(service)
    if not path.is_file():
        raise ValueError("R1 service is not registered.")
    if service.system == "linux":
        service.run(["systemctl", "--user", "enable", "--now", R1_UNIT])
    elif service.system == "darwin":
        uid = r0_stop._launchd_uid()
        service.run(["launchctl", "enable", f"gui/{uid}/{R1_LABEL}"])
        service.run(["launchctl", "bootstrap", f"gui/{uid}", path])
    else:
        service.powershell(f"Enable-ScheduledTask -TaskName {_ps(R1_TASK)} -TaskPath '\\' | Out-Null; Start-ScheduledTask -TaskName {_ps(R1_TASK)} -TaskPath '\\'")


def rollback_r1(service) -> None:
    """Stop and disable R1 first; only then restore the untouched R0 entry."""
    if service.system == "linux":
        service.run(["systemctl", "--user", "disable", "--now", R1_UNIT])
        uid = None
    elif service.system == "darwin":
        uid = r0_stop._launchd_uid()
        target = f"gui/{uid}/{R1_LABEL}"
        service.run(["launchctl", "disable", target])
        try:
            service.run(["launchctl", "print", target])
        except subprocess.CalledProcessError as exc:
            if not any(message in str(exc.output) for message in ("Could not find service", "Could not find specified service")):
                raise
        else:
            service.run(["launchctl", "bootout", target])
    elif service.system == "win32":
        uid = None
        service.powershell(f"Disable-ScheduledTask -TaskName {_ps(R1_TASK)} -TaskPath '\\' | Out-Null; "
                           f"$t=Get-ScheduledTask -TaskName {_ps(R1_TASK)} -TaskPath '\\'; "
                           "if ($t.State -eq 'Running') { $t | Stop-ScheduledTask; "
                           "$deadline=(Get-Date).AddSeconds(90); "
                           "do { Start-Sleep -Milliseconds 200; $t=Get-ScheduledTask -TaskName "
                           f"{_ps(R1_TASK)} -TaskPath '\\' "
                           "} while ($t.State -eq 'Running' -and (Get-Date) -lt $deadline); "
                           "if ($t.State -eq 'Running') { throw 'R1 task did not stop' } }")
    else:
        raise ValueError("Unsupported R1 service manager.")
    r0_stop._reenable(service, uid, stopped=True)
