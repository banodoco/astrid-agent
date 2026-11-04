"""R1 service registration at the R0 boundary.

The caller must stop R0 and complete adoption before calling register. This
module intentionally does not inspect R0 data or implement the stop protocol.
"""
from __future__ import annotations

import os
import plistlib
import re
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

from .service import systemd_quote

R0_LINUX = "astrid.service"
R1_LINUX = "astrid-r1.service"
R0_MAC = "haus.astrid.runtime"
R1_MAC = "haus.astrid.runtime.r1"
R0_WINDOWS = "Astrid Runtime"
R1_WINDOWS = "Astrid Runtime R1"


def _ps(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _create(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Never replace an existing entry, even one claiming to be ours.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _wrapper(system: str) -> str:
    if system == "win32":
        return ("$ErrorActionPreference = 'Stop'\n"
                "$root = Split-Path -Parent $PSScriptRoot\n"
                "$version = [IO.File]::ReadAllText((Join-Path $root 'current')).Trim()\n"
                "if ($version -cnotmatch '^[A-Za-z0-9._-]+$' -or $version -in @('.', '..')) { exit 2 }\n"
                "$target = Join-Path $root ('versions/' + $version + '/bin/runtime.exe')\n"
                "& $target @args\nexit $LASTEXITCODE\n")
    return ('#!/bin/sh\nset -eu\n'
            'root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)\n'
            'version=$(cat "$root/current")\n'
            'case "$version" in ""|.|..|*[!A-Za-z0-9._-]*) exit 2;; esac\n'
            'exec "$root/versions/$version/bin/runtime" "$@"\n')


class BoundaryRegistration:
    """Fresh R1 entry and rollback; manager operations raise on any failure.

    ``run`` accepts argv and returns stdout. ``wait_r1_unlocked`` must raise
    unless the R1 owner lock is free after the service has been stopped.
    """

    def __init__(self, root: Path, home: Path, system: str, run, *, uid: str = "",
                 config_home: Path | None = None):
        if system not in {"linux", "darwin", "win32"}:
            raise ValueError("Unsupported service manager")
        self.root, self.home, self.system, self.run, self.uid = root.absolute(), home.absolute(), system, run, uid
        if system == "linux":
            self.r0_path = (config_home or home / ".config") / "systemd/user" / R0_LINUX
            self.r1_path = self.r0_path.with_name(R1_LINUX)
        elif system == "darwin":
            self.r0_path = home / "Library/LaunchAgents" / (R0_MAC + ".plist")
            self.r1_path = self.r0_path.with_name(R1_MAC + ".plist")
            if not re.fullmatch(r"[0-9]+", uid):
                raise ValueError("A numeric launchd user ID is required")
        else:
            self.r0_path = None  # Task Scheduler owns the R0 definition.
            self.r1_path = self.root / "task-r1.xml"
        self.launcher = self.root / "bin" / ("runtime.ps1" if system == "win32" else "runtime")

    def powershell(self, script: str) -> str:
        exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        return self.run([str(exe), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-Command", "$ErrorActionPreference='Stop'; " + script])

    def register(self, r0_data: Path, r1_data: Path, log: Path, *, user: str = "") -> None:
        """Register and start R1 after the caller has stopped R0 and adopted data."""
        r0_data, r1_data = r0_data.resolve(), r1_data.resolve()
        if (r0_data == r1_data or r0_data in r1_data.parents or r1_data in r0_data.parents
                or not r1_data.is_dir()):
            raise ValueError("R1 needs a separate, existing adopted data root")
        for target in (self.root, self.r1_path, log):
            resolved = target.resolve()
            if resolved == r0_data or r0_data in resolved.parents:
                raise ValueError("R1 service files and logs must be outside R0 data")
        name = (self.root / "current").read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name in {".", ".."}:
            raise ValueError("Invalid current version")
        selected = self.root / "versions" / name / "bin" / ("runtime.exe" if self.system == "win32" else "runtime")
        if not selected.is_file():
            raise ValueError("Selected version has no Runtime launcher")
        args = [str(self.launcher), "run", "--profile", "astrid", "--data-root", str(r1_data)]
        if self.system == "linux":
            action = " ".join(systemd_quote(x).replace("$", "$$") for x in args)
            output = systemd_quote("append:" + str(log.absolute()))
            definition = ("[Unit]\nDescription=Astrid Runtime R1\n\n[Service]\n"
                          f"ExecStart={action}\nRestart=on-failure\nRestartSec=5\nKillMode=mixed\n"
                          f"TimeoutStopSec=90\nStandardOutput={output}\nStandardError={output}\n"
                          "\n[Install]\nWantedBy=default.target\n")
        elif self.system == "darwin":
            definition = plistlib.dumps({"Label": R1_MAC, "ProgramArguments": args,
                "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ExitTimeOut": 90,
                "StandardOutPath": str(log.absolute()), "StandardErrorPath": str(log.absolute())}).decode()
        else:
            if not user:
                raise ValueError("A Windows user SID is required")
            cmd = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
            arguments = subprocess.list2cmdline(["-NoProfile", "-NonInteractive", "-File", *args])
            definition = ('<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
                '<Triggers><LogonTrigger><Enabled>true</Enabled><UserId>' + escape(user) + '</UserId></LogonTrigger></Triggers>'
                '<Principals><Principal id="Owner"><UserId>' + escape(user) + '</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>'
                '<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>'
                '<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>'
                '<ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure></Settings>'
                '<Actions Context="Owner"><Exec><Command>' + escape(cmd) + '</Command><Arguments>' + escape(arguments) + '</Arguments></Exec></Actions></Task>')
        _create(self.launcher, _wrapper(self.system), 0o755)
        _create(self.r1_path, definition)
        if self.system == "linux":
            self.run(["systemctl", "--user", "daemon-reload"])
            self.run(["systemctl", "--user", "enable", "--now", R1_LINUX])
        elif self.system == "darwin":
            self.run(["launchctl", "enable", f"gui/{self.uid}/{R1_MAC}"])
            self.run(["launchctl", "bootstrap", f"gui/{self.uid}", str(self.r1_path)])
        else:
            self.powershell(f"if (Get-ScheduledTask | Where-Object {{ $_.TaskName -eq {_ps(R1_WINDOWS)} -and $_.TaskPath -eq '\\' }}) {{ throw 'R1 task already exists' }}; "
                            f"Register-ScheduledTask -TaskName {_ps(R1_WINDOWS)} -TaskPath '\\' -Xml ([IO.File]::ReadAllText({_ps(str(self.r1_path))})) | Out-Null; "
                            f"Start-ScheduledTask -TaskName {_ps(R1_WINDOWS)} -TaskPath '\\'")

    def rollback(self, wait_r1_unlocked) -> None:
        """Disable R1, confirm its lock free, then re-enable the intact R0 entry."""
        if not callable(wait_r1_unlocked):
            raise ValueError("An R1 owner-lock wait is required")
        if self.system == "linux":
            self.run(["systemctl", "--user", "disable", "--now", R1_LINUX])
        elif self.system == "darwin":
            self.run(["launchctl", "disable", f"gui/{self.uid}/{R1_MAC}"])
            self.run(["launchctl", "bootout", f"gui/{self.uid}/{R1_MAC}"])
        else:
            self.powershell(f"Disable-ScheduledTask -TaskName {_ps(R1_WINDOWS)} -TaskPath '\\' | Out-Null; Stop-ScheduledTask -TaskName {_ps(R1_WINDOWS)} -TaskPath '\\'")
        wait_r1_unlocked()
        if self.system == "linux":
            self.run(["systemctl", "--user", "enable", "--now", R0_LINUX])
        elif self.system == "darwin":
            self.run(["launchctl", "enable", f"gui/{self.uid}/{R0_MAC}"])
            self.run(["launchctl", "bootstrap", f"gui/{self.uid}", str(self.r0_path)])
        else:
            self.powershell(f"Enable-ScheduledTask -TaskName {_ps(R0_WINDOWS)} -TaskPath '\\' | Out-Null; Start-ScheduledTask -TaskName {_ps(R0_WINDOWS)} -TaskPath '\\'")
