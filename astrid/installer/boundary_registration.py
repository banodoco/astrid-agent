"""R1 service registration at the R0 boundary.

The caller must stop R0 and complete adoption before calling register. This
module intentionally does not inspect R0 data or implement the stop protocol.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .core import Version, absolute, version_id
from .service import SERVICE_SETTINGS, render

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


def _selected(root: Path, system: str) -> tuple[Version, str, str]:
    """Pin one staged record; an update needs a new service definition."""
    name = version_id((root / "current").read_text(encoding="utf-8").strip())
    folder = absolute(root / "versions" / name)
    record = absolute(folder / "version.json")
    contents = record.read_bytes()
    value = json.loads(contents)
    if not isinstance(value, dict) or not isinstance(value.get("env"), dict):
        raise ValueError("Invalid selected version record")
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in value["env"].items()):
        raise ValueError("Invalid selected version environment")
    version = Version.load(value)
    if (version.root != folder or not version.python.is_relative_to(folder)
            or not version.profile.is_relative_to(folder)):
        raise ValueError("Selected version paths must be inside its directory")
    executable = version.python.with_name("pythonw.exe") if system == "win32" else version.python
    if not executable.is_file() or not version.profile.is_file():
        raise ValueError("Selected version interpreter or profile is missing")
    return version, name, hashlib.sha256(contents).hexdigest()


def _command(version: Version, data: Path, root: Path, name: str, digest: str,
             log: Path, system: str) -> list[str]:
    """Use the staged interpreter with a pinned manifest and environment."""
    python = version.python.with_name("pythonw.exe") if system == "win32" else version.python
    args = ["run", "--profile", "astrid", "--data-root", str(data),
            "--source-manifest", str(version.profile)]
    env_file = root / "service-env.json"
    script = ("import hashlib,json,os,sys\nfrom pathlib import Path\n"
              + f"root=Path({str(root)!r})\n"
              + f"if (root.joinpath('current').read_text(encoding='utf-8').strip() != {name!r} or "
              + f"hashlib.sha256(root.joinpath('versions',{name!r},'version.json').read_bytes()).hexdigest() != {digest!r}):\n"
              + "    raise ValueError('R1 selected version changed; registration must be reconciled')\n"
              + f"os.environ.update({version.env!r})\n")
    if system == "win32":
        script += f"sys.stdout=sys.stderr=open({str(log.with_name('runtime-service.log'))!r},'a',encoding='utf-8',buffering=1)\n"
    script += (f"path=Path({str(env_file)!r})\n"
               "settings=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}\n"
               f"if (not isinstance(settings,dict) or set(settings)-set({sorted(SERVICE_SETTINGS)!r}) "
               "or any(not isinstance(value,str) for value in settings.values())):\n"
               "    raise ValueError('Invalid Astrid service-env.json; use the documented string settings.')\n"
               "os.environ.update(settings)\n"
               f"from astrid_runtime.cli import main\nsys.exit(main({args!r}))")
    return [str(python), "-X", "utf8", "-I", "-c", script]


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
        version, name, digest = _selected(self.root, self.system)
        environment = dict(version.env)
        environment["ASTRID_RUNTIME_DATA_ROOT"] = str(r1_data)
        environment["ASTRID_RUNTIME_SOURCE_MANIFEST"] = str(version.profile)
        version = Version(version.root, version.python, version.profile, environment,
                          version.release, version.managed_by)
        if self.system == "win32" and not user:
            raise ValueError("A Windows user SID is required")
        # A leftover wrapper from the earlier registration format is ambiguous.
        if self.launcher.exists() or self.launcher.is_symlink():
            raise FileExistsError(f"Existing R1 launcher: {self.launcher}")
        if self.r1_path.exists() or self.r1_path.is_symlink():
            raise FileExistsError(f"Existing R1 definition: {self.r1_path}")
        if self.system == "linux":
            state = self.run(["systemctl", "--user", "show", R1_LINUX, "--property=LoadState", "--value"]).strip()
            if state != "not-found":
                raise ValueError("R1 systemd definition exists or its state is ambiguous")
        elif self.system == "darwin":
            listing = self.run(["launchctl", "list"])
            lines = listing.splitlines()
            if not lines or lines[0].strip() != "PID\tStatus\tLabel":
                raise ValueError("Cannot determine existing launchd definitions")
            if any(line.split("\t")[-1].strip() == R1_MAC for line in lines[1:]):
                raise ValueError("R1 launchd definition already exists")
        args = _command(version, r1_data, self.root, name, digest, log.absolute(), self.system)
        definition = render(self.system, version, r1_data, log.absolute(), user=user,
                            _argv=args, _label=R1_MAC, _description="Astrid Runtime R1")
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
