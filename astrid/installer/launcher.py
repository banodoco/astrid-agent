"""Stable user terminal launchers for the selected installed version."""

from __future__ import annotations

import importlib
import ntpath
import os
import shlex
import sys
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


def _in_installer_prefix(candidate: Path) -> bool:
    try:
        return candidate.resolve().is_relative_to(Path(sys.prefix).resolve())
    except (OSError, RuntimeError):
        return False


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
                if _in_installer_prefix(candidate):
                    continue
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
if (not isinstance(record["env"].get("ASTRID_INSTALL_RELEASE"), str)
        or not re.fullmatch(r"0|[1-9][0-9]*", record["env"]["ASTRID_INSTALL_RELEASE"])
        or record["env"].get("ASTRID_INSTALL_MANAGED_BY") not in ("", "pinokio")):
    raise SystemExit("Astrid's selected install identity needs repair; run the install line again.")
if sys.argv[1:2] == ["credential"]:
    os.environ.update(env)
    from astrid.core.util.credential_store import main
    raise SystemExit(main(sys.argv[2:]))
replace_process([python, "-X", "utf8", "-I", "-m", "astrid.omp_agent", *sys.argv[1:]], env)
'''


def publish_runtime_runner(root: Path) -> Path:
    """Durably publish current admission before an update can stop or migrate."""
    from runtime_protocol import platform

    from . import custody, environment, foreground, service

    root = Path(root).absolute()
    runtime_runner = root / "run_runtime.py"
    # The retained version supplies only the interpreter and Runtime. Carry the
    # complete current launcher so an older installed Agent is never imported.
    import inspect

    prelude_source = service._launch_prelude_source()
    environment_source = (
        "from collections.abc import Mapping\n"
        "from pathlib import PureWindowsPath\n"
        "from runtime_protocol.door import DEFAULT_DOOR_PORT\n"
        f"_PORT_RE = re.compile({environment._PORT_RE.pattern!r})\n"
        f"_FIRST_UNPRIVILEGED_PORT = {environment._FIRST_UNPRIVILEGED_PORT!r}\n"
        f"_LAST_PORT = {environment._LAST_PORT!r}\n"
        + inspect.getsource(environment.runtime_door_port)
        + "\n"
        + inspect.getsource(environment.canonical_environment)
    )
    service_source = (
        prelude_source + '\n'
        'from runtime_protocol import platform\n'
        f'LABEL={service.LABEL!r}\nSERVICE_SETTINGS=frozenset({sorted(service.SERVICE_SETTINGS)!r})\n'
        f'_EMBEDDED_LAUNCH_PRELUDE={prelude_source!r}\n'
        + environment_source + '\n'
        + "\n".join(inspect.getsource(function) for function in (
            service.default_program_root, service.installer_lock_path, service.pinokio_lock_paths,
            service.command,
        ))
    )
    # Keep the generated foreground self-contained: retained release-0 Agents
    # do not provide the current installer modules.  The ticket client uses the
    # already-embedded admission helpers, so it never imports the retained Agent.
    custody_attach_source = '''import hashlib,hmac,secrets,socket
def _custody_send(stream, value):
    stream.write(json.dumps(value).encode() + b"\\n")
    stream.flush()

def _custody_receive(stream):
    line = stream.readline(65537)
    if not line or len(line) > 65536:
        raise ValueError("The stopped-update lock owner disconnected.")
    return json.loads(line)

def _custody_identity_matches(expected, observed):
    return (isinstance(expected, dict) and isinstance(observed, dict)
            and all(expected.get(field) == observed.get(field)
                    for field in ("pid", "uid", "birth_id")))

def _CUSTODY_ATTACH(ticket, root, version, data):
    try:
        root, data = absolute(root), absolute(data)
        marker = read_json(root / "install.json")
        if (marker.get("owner") != "haus.astrid.runtime" or marker.get("managed_by") != "pinokio"
                or absolute(marker["data_root"]) != data or current(root) != version
                or not launch_admission(version, data, custody=True)):
            raise ValueError("The stopped update does not authorize this foreground activation.")
        journal = read_json(root / "update-transaction.json")
        try:
            with (root / ".app-update.lock").open("r+b") as operation_lock:
                try:
                    platform.lock_file(operation_lock.fileno(), blocking=False)
                except BlockingIOError:
                    pass
                else:
                    platform.unlock_file(operation_lock.fileno())
                    raise ValueError("The stopped update no longer owns activation admission.")
        except FileNotFoundError:
            raise ValueError("The stopped update no longer owns activation admission.") from None
        binding = {"root": str(root), "data": str(data), "version": version.document(),
                   "operation_id": journal["operation_id"], "step": journal["step"]}
        if binding != ticket["binding"]:
            raise ValueError("The stopped-update activation identity changed.")
        receipt = json.loads((root / ".app-update-custody.json").read_text(encoding="utf-8"))
        observed = platform.process_identity(receipt["identity"]["pid"])
        if (receipt["port"] != ticket["port"] or receipt["root"] != str(root.resolve())
                or receipt["lock"] != ticket["lock"]
                or not _custody_identity_matches(receipt["identity"], observed)):
            raise ValueError("The stopped-update lock owner identity changed.")
        challenge = secrets.token_hex(32)
        connection = socket.create_connection(("127.0.0.1", ticket["port"]), timeout=10)
        try:
            with connection.makefile("rwb", buffering=0) as stream:
                _custody_send(stream, {"token": ticket["token"], "binding": binding,
                                       "challenge": challenge})
                reply = _custody_receive(stream)
                proof = hmac.new(bytes.fromhex(receipt["key"]), challenge.encode(), hashlib.sha256).hexdigest()
                if reply.get("accepted") is not True or not hmac.compare_digest(reply.get("proof", ""), proof):
                    raise ValueError("The stopped-update activation ticket was refused.")
            connection.settimeout(None)
            return connection
        except BaseException:
            connection.close()
            raise
    except (OSError, KeyError, TypeError) as exc:
        raise ValueError("The stopped-update activation ticket was refused.") from exc
'''
    foreground_source = "\n".join(inspect.getsource(function) for function in (
        foreground._probe_operation_lock, foreground.launch, foreground._run,
    ))
    source = (
        service_source + '\n'
        + custody_attach_source + '\n'
        + f'_CUSTODY_KEEPER_SOURCE = {inspect.getsource(custody.keeper)!r}\n'
        + '# <Astrid foreground launcher>\n'
        + foreground_source + '\n'
        + 'launch(Path(__file__).resolve().parent, sys.argv[1:])\n'
    )
    fd, temporary = platform.private_temporary(root, ".run_runtime.py.")
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
        platform.durable_replace(temporary, runtime_runner)
    finally:
        temporary.unlink(missing_ok=True)
    return runtime_runner


def install_runtime(root: Path) -> None:
    """Refresh Runtime admission without replacing the retained terminal command."""
    root = Path(root).absolute()
    publish_runtime_runner(root)
    # Both renderings are useful for packaging/review; only the native one
    # is invoked. They follow current just like the agent launcher below.
    (root / "run.cmd").write_text(
        '@echo off\r\nsetlocal\r\n'
        'set "ASTRID_PROGRAM_ROOT=%~dp0"\r\n'
        'for /f "usebackq delims=" %%V in ("%ASTRID_PROGRAM_ROOT%current") do set "ASTRID_VERSION=%%V"\r\n'
        '"%ASTRID_PROGRAM_ROOT%versions\\%ASTRID_VERSION%\\py\\Scripts\\python.exe" -X utf8 -I "%ASTRID_PROGRAM_ROOT%run_runtime.py" %*\r\n'
        'exit /b %errorlevel%\r\n',
        encoding="utf-8", newline="",
    )
    runtime_launcher = root / "run"
    runtime_launcher.write_text(
        '#!/bin/sh\nset -eu\n'
        'root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)\n'
        'version=$(cat "$root/current")\n'
        'case "$version" in *[!A-Za-z0-9._-]*|"") echo "Astrid current version pointer is invalid." >&2; exit 2;; esac\n'
        'exec "$root/versions/$version/py/bin/python" -X utf8 -I "$root/run_runtime.py" "$@"\n',
        encoding="utf-8", newline="\n",
    )
    runtime_launcher.chmod(0o755)


def install(root: Path) -> tuple[Path, ...]:
    """Write stable launchers after the selected version has identity stamps."""
    root = Path(root).absolute()
    install_runtime(root)
    runner = root / "run_astrid.py"
    # The selected package may predate the process helper. Embed it so the
    # runner also works after a failed update or rollback.
    process_source = Path(_process.__file__).read_text(encoding="utf-8")
    runner.write_text(process_source + "\n" + _RUNNER, encoding="utf-8", newline="\n")
    if os.name == "nt":
        launcher = root / "run-astrid.cmd"
        launcher.write_text(
            '@echo off\r\n'
            'set "ASTRID_PROGRAM_ROOT=%~dp0"\r\n'
            'for /f "usebackq delims=" %%V in ("%ASTRID_PROGRAM_ROOT%current") do set "ASTRID_VERSION=%%V"\r\n'
            '"%ASTRID_PROGRAM_ROOT%versions\\%ASTRID_VERSION%\\py\\Scripts\\python.exe" -X utf8 -I "%ASTRID_PROGRAM_ROOT%run_astrid.py" %*\r\n',
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
        'exec "$root/versions/$version/py/bin/python" -X utf8 -I "$root/run_astrid.py" "$@"\n',
        encoding="utf-8", newline="\n",
    )
    launcher.chmod(0o755)
    return launcher, runner
