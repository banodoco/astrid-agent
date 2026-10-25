"""Stable user terminal launchers for the selected installed version."""

from __future__ import annotations

import importlib
import ntpath
import os
import shlex
from pathlib import Path
from typing import Any

from ..core.util import process as _process

SHIM_MARKER = "Astrid managed command shim"
PATH_HINT = 'Add ~/.local/bin to your PATH to run astrid from anywhere: export PATH="$HOME/.local/bin:$PATH"'


def owned_shim(path: Path) -> bool:
    try:
        return not path.is_symlink() and any(line in {f"# {SHIM_MARKER}", f"rem {SHIM_MARKER}"}
                                             for line in path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeError):
        return False


def _windows_path_key(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(ntpath.expandvars(value.strip().strip('"'))))


def _registry_path(machine: bool = False) -> str:
    winreg: Any = importlib.import_module("winreg")  # Windows only; mypy pins platform=darwin.

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE if machine else winreg.HKEY_CURRENT_USER,
                            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment" if machine else "Environment") as key:
            return str(winreg.QueryValueEx(key, "Path")[0])
    except FileNotFoundError:
        return ""


def _update_user_path(service, *, add: bool) -> None:
    winreg: Any = importlib.import_module("winreg")  # Windows only; mypy pins platform=darwin.

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0,
                           winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            previous, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            previous, kind = "", winreg.REG_EXPAND_SZ
        root = str(service.root.absolute())
        parts = []
        found = False
        for part in previous.split(";") if previous else []:
            if part and _windows_path_key(part) == _windows_path_key(root):
                if add and not found:
                    parts.append(part)  # Preserve expansion, spelling and precedence.
                found = True
            else:
                parts.append(part)
        if add and not found:
            parts.append(root)
        updated = ";".join(parts)
        if updated == previous:
            return
        winreg.SetValueEx(key, "Path", 0, kind, updated)
    service.powershell('''Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices;
public class AstridEnvironment { [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
public static extern IntPtr SendMessageTimeout(IntPtr h, uint m, UIntPtr w, string l, uint f, uint t, out UIntPtr r); }';
$result=[UIntPtr]::Zero;
[AstridEnvironment]::SendMessageTimeout([IntPtr]0xffff, 0x001a, [UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$result) | Out-Null''')


def install_shim(service, *, say=print) -> tuple[Path | None, str | None]:
    """Expose the stable launcher without replacing another command."""
    windows = service.system == "win32"
    root = service.root.absolute()
    target = root / "astrid.cmd" if windows else service.home / ".local" / "bin" / "astrid"
    paths = [os.environ.get("PATH", "")]
    if windows:
        paths.append(";".join((_registry_path(True), _registry_path(), str(root))))
    clash = target if os.path.lexists(target) and not owned_shim(target) else None
    for path in paths:
        if clash:
            break
        for entry in path.split(";" if windows else ":"):
            folder = Path(ntpath.expandvars(entry.strip('"')) if windows else entry).absolute()
            names = ["astrid" + ext.lower() for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")] if windows else ["astrid"]
            for name in names:
                candidate = folder / name
                if candidate.is_file() and (windows or os.access(candidate, os.X_OK)) and not owned_shim(candidate):
                    clash = candidate
                    break
            if clash or (_windows_path_key(str(folder)) == _windows_path_key(str(target.parent)) if windows else folder == target.parent):
                break
    if clash:
        say(f"Astrid command shim skipped: {clash} already exists and is not managed by Astrid; use {root / ('run-astrid.cmd' if windows else 'run-astrid')}.")
        return None, None
    target.parent.mkdir(parents=True, exist_ok=True)
    if windows:
        target.write_text(f'@echo off\r\nrem {SHIM_MARKER}\r\n"%~dp0run-astrid.cmd" %*\r\n', encoding="utf-8", newline="")
        _update_user_path(service, add=True)
        return target, "Open a new terminal, then run astrid."
    target.write_text(f'#!/bin/sh\n# {SHIM_MARKER}\nexec {shlex.quote(str(root / "run-astrid"))} "$@"\n', encoding="utf-8", newline="\n")
    target.chmod(0o755)
    on_path = any(Path(entry).absolute() == target.parent for entry in paths[0].split(":"))
    return target, None if on_path else PATH_HINT


def remove_shim(service) -> None:
    windows = service.system == "win32"
    target = service.root / "astrid.cmd" if windows else service.home / ".local" / "bin" / "astrid"
    if owned_shim(target):
        # A second installation may have taken over the shared POSIX command.
        if windows or f'exec {shlex.quote(str(service.root.absolute() / "run-astrid"))} "$@"' in target.read_text(encoding="utf-8").splitlines():
            target.unlink()
    if windows:
        _update_user_path(service, add=False)


_RUNNER = '''import json, os, re, sys
from pathlib import Path

root = Path(__file__).resolve().parent
name = (root / "current").read_text(encoding="utf-8").strip()
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
    raise SystemExit("Astrid's current version pointer is invalid.")
version_root = root / "versions" / name
record = json.loads((version_root / "version.json").read_text(encoding="utf-8"))
if Path(record["root"]).resolve() != version_root.resolve():
    raise SystemExit("Astrid's current version points outside its program folder.")
python = str(Path(record["python"]))
env = dict(os.environ)
env.update({str(key): str(value) for key, value in record["env"].items()})
env["ASTRID_INSTALL_PROGRAM_ROOT"] = str(root)
if sys.argv[1:2] == ["credential"]:
    os.environ.update(env)
    from astrid.core.util.credential_store import main
    raise SystemExit(main(sys.argv[2:]))
replace_process([python, "-I", "-m", "astrid.omp_agent", *sys.argv[1:]], env)
'''


def install(root: Path) -> tuple[Path, ...]:
    """Write stable launchers that follow root/current without changing PATH."""
    root = Path(root).absolute()
    runner = root / "run_astrid.py"
    # This launcher is rewritten before repair/update, while the selected
    # version may predate the helper. Embed its authoritative implementation
    # so the runner also works after a failed update or rollback.
    process_source = Path(_process.__file__).read_text(encoding="utf-8")
    runner.write_text(process_source + "\n" + _RUNNER, encoding="utf-8", newline="\n")
    if os.name == "nt":
        launcher = root / "run-astrid.cmd"
        launcher.write_text(
            '@echo off\r\n'
            'set "ASTRID_PROGRAM_ROOT=%~dp0"\r\n'
            'for /f "usebackq delims=" %%V in ("%ASTRID_PROGRAM_ROOT%current") do set "ASTRID_VERSION=%%V"\r\n'
            '"%ASTRID_PROGRAM_ROOT%versions\\%ASTRID_VERSION%\\py\\Scripts\\python.exe" -I "%ASTRID_PROGRAM_ROOT%run_astrid.py" %*\r\n',
            encoding="utf-8", newline="",
        )
        return launcher, runner
    launcher = root / "run-astrid"
    launcher.write_text(
        '#!/bin/sh\n'
        'set -eu\n'
        'root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)\n'
        'version=$(cat "$root/current")\n'
        'case "$version" in *[!A-Za-z0-9._-]*|"") echo "Astrid current version pointer is invalid." >&2; exit 2;; esac\n'
        'exec "$root/versions/$version/py/bin/python" -I "$root/run_astrid.py" "$@"\n',
        encoding="utf-8", newline="\n",
    )
    launcher.chmod(0o755)
    return launcher, runner
