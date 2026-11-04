"""Stop the registered R0 service and wait for its realm owner lock.

This is a standalone upgrade primitive. The caller supplies the selected realm
root and serializes the installer transaction; it must not use this operation
for signalling arbitrary processes or for stopping R1.
"""
from __future__ import annotations

import math
import os
import re
import stat
import time
from pathlib import Path

from runtime_protocol import platform

from .service import LABEL, TASK


class R0StopError(RuntimeError):
    """R0 could not be stopped safely; the upgrade must be refused."""


def _launchd_uid():
    return os.getuid()


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
        # The command's arguments can contain arbitrary multi-line source. Only
        # inspect manager fields preceding that block, never the process action.
        head = re.split(r"(?m)^[ \t]*arguments = \{[ \t]*$", output, maxsplit=1)[0]
        paths = re.findall(r"(?m)^[ \t]*path = ([^\r\n]+?)[ \t]*$", head)
        if not output.startswith(target + " = {\n") or paths != [str(service.path)]:
            raise R0StopError("The registered R0 launchd plist path does not match the installer unit; refusing to stop it.")
    elif service.system != "win32":
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
            service.run(["launchctl", "bootstrap", f"gui/{uid}", service.path])
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


def stop_r0_and_wait(service, realm_root: Path, *, timeout: float = 90.0, poll_interval: float = 0.2) -> None:
    """Disable/stop R0 via its OS manager, then wait boundedly for its lock.

    Success leaves R0 disabled and releases the observed lock; the caller must
    promptly perform the read-only adoption and handle later rollback. Failure
    after a stop attempt re-enables R0 and refuses. No service or data files
    are changed by this operation.
    """
    if not math.isfinite(timeout) or timeout < 0 or not math.isfinite(poll_interval) or poll_interval <= 0:
        raise ValueError("A finite nonnegative timeout and positive poll interval are required.")
    realm_root = Path(realm_root)
    if not realm_root.is_absolute():
        raise ValueError("The selected R0 realm root must be absolute.")
    uid = _launchd_uid() if service.system == "darwin" else None
    _validate_manager(service, uid)
    stopped = False
    try:
        _disable_and_stop(service, uid)
        stopped = True
        _wait_for_owner_lock(realm_root, timeout, poll_interval)
    except Exception as exc:
        try:
            _reenable(service, uid, stopped=stopped)
        except Exception as recovery:
            raise R0StopError(f"R0 stop failed ({exc}); automatic re-enable also failed ({recovery}). Re-enable the Astrid service in the OS service manager, then run the install line again.") from recovery
        if isinstance(exc, R0StopError):
            raise
        raise R0StopError(f"R0 stop or owner-lock check failed: {exc}. Run the install line again after checking the Astrid service.") from exc
