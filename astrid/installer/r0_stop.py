"""Stop the registered R0 service and wait for its realm owner lock.

This is a standalone upgrade primitive. The caller supplies the selected realm
root and serializes the installer transaction; it must not use this operation
for signalling arbitrary processes or for stopping R1.
"""
from __future__ import annotations

import hashlib
import json
import math
import plistlib
import os
import re
import stat
import tempfile
import time
from pathlib import Path

from runtime_protocol import platform

from .service import LABEL, TASK


class R0StopError(ValueError):
    """R0 could not be stopped safely; the upgrade must be refused."""


def _launchd_uid():
    return os.getuid()


def _unit_digest(service):
    path = Path(service.path)
    if path.is_symlink() or not path.is_file():
        raise R0StopError("R0 service file is missing or unsafe.")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_plist(service):
    try:
        value = plistlib.loads(Path(service.path).read_bytes())
        args = value["ProgramArguments"]
        executable = Path(args[0])
        root = Path(service.root)
        if (value.get("Label") != LABEL or not isinstance(args, list) or not args
                or not all(isinstance(arg, str) for arg in args)
                or not executable.is_absolute() or not executable.is_relative_to(root)):
            raise ValueError("invalid action")
    except (OSError, ValueError, KeyError, IndexError, TypeError, plistlib.InvalidFileException) as exc:
        raise R0StopError("The R0 plist action does not match its program root.") from exc
    return args


def _validate_manager(service, uid):
    if service.system == "linux":
        output = service.run(["systemctl", "--user", "show", "astrid.service", "--property=FragmentPath"])
        fields = [line.partition("=")[2] for line in output.splitlines()
                  if line.startswith("FragmentPath=")]
        if fields != [str(service.path)]:
            raise R0StopError("The registered R0 systemd FragmentPath does not match the installer unit; refusing to stop it.")
    elif service.system == "darwin":
        target = f"gui/{uid}/{LABEL}"
        output = service.run(["launchctl", "print", target])
        head, separator, rest = output.partition("\targuments = {\n")
        paths = re.findall(r"(?m)^[ \t]*path = ([^\r\n]+?)[ \t]*$", head)
        args = _validate_plist(service)
        expected = "".join("\t\t" + arg + "\n" for arg in args) + "\t}\n"
        if (not output.startswith(target + " = {\n") or paths != [str(service.path)]
                or not separator or not rest.startswith(expected)):
            raise R0StopError("The registered R0 launchd plist path or ProgramArguments does not match the installer unit.")
    elif service.system == "win32":
        raise R0StopError("The R0 scheduled task cannot be verified; refusing to stop it.")
    else:
        raise R0StopError(f"Unsupported R0 service manager: {service.system}")


def _disable_and_stop(service, uid):
    if service.system == "linux":
        service.run(["systemctl", "--user", "disable", "--now", "astrid.service"])
    elif service.system == "darwin":
        target = f"gui/{uid}/{LABEL}"
        service.run(["launchctl", "disable", target])
        service.run(["launchctl", "bootout", target])
    else:
        service.powershell(f"Disable-ScheduledTask -TaskName '{TASK}' -TaskPath '\\' | Out-Null")
        service.powershell(f"Stop-ScheduledTask -TaskName '{TASK}' -TaskPath '\\'")


def _reenable(service, uid, *, stopped):
    if service.system == "linux":
        service.run(["systemctl", "--user", "enable", "--now", "astrid.service"])
    elif service.system == "darwin":
        target = f"gui/{uid}/{LABEL}"
        service.run(["launchctl", "enable", target])
        if stopped:
            try:
                _validate_manager(service, uid)
            except Exception:
                service.run(["launchctl", "bootstrap", f"gui/{uid}", service.path])
                _validate_manager(service, uid)
            service.run(["launchctl", "kickstart", target])
    else:
        service.powershell(f"Enable-ScheduledTask -TaskName '{TASK}' -TaskPath '\\' | Out-Null")
        if stopped:
            service.powershell(f"Start-ScheduledTask -TaskName '{TASK}' -TaskPath '\\'")


def _wait_for_owner_lock(realm_root: Path, timeout: float, poll_interval: float):
    # Do not create, truncate, read, copy, or unlink this file. An absent or
    # unsafe lock is uncertain, not proof of an idle R0.
    path = realm_root / "owner.lock"
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise R0StopError("R0 owner.lock is not a regular file; refusing the upgrade.")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise R0StopError("R0 owner.lock changed while opening it; refusing the upgrade.")
        deadline = time.monotonic() + timeout
        while True:
            try:
                platform.lock_file(fd, blocking=False)
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise R0StopError("R0 is still busy. Finish or cancel running tasks, then run the install line again.")
                time.sleep(min(poll_interval, remaining))
            else:
                try:
                    current = path.lstat()
                    if not stat.S_ISREG(current.st_mode) or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                        raise R0StopError("R0 owner.lock changed during the wait; refusing the upgrade.")
                finally:
                    platform.unlock_file(fd)
                return
    finally:
        os.close(fd)


def _marker_path(service):
    # Outside both the selected program root and workspace root.
    from .service import installer_lock_path
    return installer_lock_path(service.home, service.system).parent / "astrid-r0-upgrade-in-progress.json"


def _sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write_marker(service, data_root):
    path = _marker_path(service)
    payload = {"version": 1, "system": service.system, "program_root": str(service.root),
               "data_root": str(data_root), "unit": str(service.path), "unit_sha256": _unit_digest(service)}
    fd, temporary = tempfile.mkstemp(prefix=".astrid-r0-intent-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        # Publish a complete record without replacing another operation's intent.
        os.link(temporary, path, follow_symlinks=False)
        _sync_directory(path.parent)
    finally:
        os.unlink(temporary)
        _sync_directory(path.parent)


def recover_r0(service, *, r1_inactive=None):
    """Restore R0 only with a manager-backed R1 exclusion under the installer lock.

    The future R1 handoff must supply a callable which checks its registered
    manager entry and prevents R1 starting until recovery has completed. An
    unavailable or ambiguous check is not evidence that R1 is inactive.
    """
    path = _marker_path(service)
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_file():
        raise R0StopError("Unsafe R0 upgrade marker; manual recovery is required.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        expected = {"version": 1, "system": service.system, "program_root": str(service.root),
                    "unit": str(service.path), "unit_sha256": _unit_digest(service)}
        if not isinstance(payload, dict) or any(payload.get(k) != v for k, v in expected.items()):
            raise ValueError("marker or service changed")
        if not Path(payload["data_root"]).is_absolute():
            raise ValueError("invalid data root")
        if r1_inactive is None or r1_inactive() is not True:
            raise ValueError("R1 service inactivity cannot be established; refusing to restart R0")
        uid = _launchd_uid() if service.system == "darwin" else None
        if service.system == "linux":
            _validate_manager(service, uid)
        elif service.system == "darwin":
            _validate_plist(service)
            # A loaded foreign job must never be enabled or started.
            try:
                service.run(["launchctl", "print", f"gui/{uid}/{LABEL}"])
            except Exception:
                service.run(["launchctl", "bootstrap", f"gui/{uid}", service.path])
            _validate_manager(service, uid)
        else:
            raise ValueError("unverified service manager")
        _reenable(service, uid, stopped=True)
        path.unlink()
        _sync_directory(path.parent)
        return True
    except Exception as exc:
        raise R0StopError(f"Could not safely restore R0; its recovery marker was retained: {exc}") from exc


def stop_r0_and_wait(service, realm_root: Path, *, data_root: Path | None = None,
                     timeout: float = 90.0, poll_interval: float = 0.2) -> None:
    """Stop the bound R0 job, retaining durable recovery intent until handoff."""
    if not math.isfinite(timeout) or timeout < 0 or not math.isfinite(poll_interval) or poll_interval <= 0:
        raise ValueError("A finite nonnegative timeout and positive poll interval are required.")
    realm_root = Path(realm_root)
    data_root = Path(data_root) if data_root is not None else realm_root
    if not realm_root.is_absolute() or not data_root.is_absolute():
        raise ValueError("The selected R0 roots must be absolute.")
    marker = _marker_path(service)
    if marker.is_relative_to(data_root) or marker.is_relative_to(service.root):
        raise R0StopError("The recovery marker must be outside the R0 roots.")
    # Until the adopt receipt is consumed by the subsequent piece, no existing
    # destination can be established as ours; do not touch it.
    destination = Path(str(data_root) + "-r1")
    if destination.exists() or destination.is_symlink():
        raise R0StopError(f"The R1 destination already exists: {destination}.")
    uid = _launchd_uid() if service.system == "darwin" else None
    _validate_manager(service, uid)
    _write_marker(service, data_root)
    try:
        _disable_and_stop(service, uid)
        _wait_for_owner_lock(realm_root, timeout, poll_interval)
    except BaseException as exc:
        try:
            # This call is still inside the serialized stop, before any R1
            # handoff can have started. Interrupted invocations need the R1
            # manager-backed exclusion described by recover_r0 instead.
            recover_r0(service, r1_inactive=lambda: True)
        except BaseException as recovery:
            raise R0StopError(f"R0 stop failed ({exc}); automatic re-enable also failed ({recovery}). Re-enable the Astrid service in the OS service manager, then run the install line again.") from recovery
        if isinstance(exc, R0StopError) or not isinstance(exc, Exception):
            raise
        raise R0StopError(f"R0 stop or owner-lock check failed: {exc}. Run the install line again after checking the Astrid service.") from exc
