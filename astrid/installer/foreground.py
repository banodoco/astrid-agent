"""Pinokio's foreground owner, using the login service's Runtime command."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from runtime_protocol import platform

from .core import absolute, current, read_json
from .service import LABEL, command, installer_lock_path, launch_admission, launch_environment


def _probe_operation_lock(path: Path) -> None:
    """Prove no app updater owns ``path`` without importing installed Agent code."""
    platform.private_directory(path.parent)
    with path.open("a+b") as lock:
        try:
            platform.lock_file(lock.fileno(), blocking=False)
        except BlockingIOError:
            raise ValueError("Another Astrid operation still owns the installation lock.") from None
        platform.unlock_file(lock.fileno())


def launch(root: Path, argv: list[str], *, system: str | None = None) -> None:
    system = system or sys.platform
    root = absolute(root)
    record = read_json(root / "install.json")
    version = current(root)
    if version is None:
        raise ValueError("Astrid isn't fully installed. Choose Pinokio's Reset button, then install again.")
    ticket = os.environ.pop("ASTRID_INSTALL_CUSTODY", None)
    activating = launch_admission(version, absolute(record["data_root"]), custody=ticket is not None)
    if not activating and (root / "install.json").is_file():
        # The journal admits only the activation selected by this updater.
        # Ordinary Start still contends with online candidate preparation.
        _probe_operation_lock(root / ".app-update.lock")
    if ticket is not None:
        import json

        attach: Any = globals().get("_CUSTODY_ATTACH")
        if attach is None:
            from .custody import attach

        if not activating:
            raise ValueError("The stopped update does not authorize this foreground activation.")
        connection = attach(json.loads(ticket), root, version, absolute(record["data_root"]))
        try:
            if system != "win32":
                connection.set_inheritable(True)
            _run(root, argv, system, None, custody_fd=connection.fileno())
        finally:
            connection.close()
        return
    lock_path = installer_lock_path(Path.home(), system)
    platform.private_directory(lock_path.parent)
    with lock_path.open("a+b") as lock:
        try:
            platform.lock_file(lock.fileno(), blocking=False)
        except BlockingIOError:
            raise ValueError("Astrid is already running or being installed. Try again after it stops.") from None
        try:
            # Read the selected version only after excluding installers.
            _run(root, argv, system, lock.fileno())
        finally:
            platform.unlock_file(lock.fileno())


def _run(root: Path, argv: list[str], system: str, lock_fd: int | None,
         *, custody_fd: int | None = None) -> None:
    root = absolute(root)
    record = read_json(root / "install.json")
    if record.get("owner") != LABEL or record.get("managed_by") != "pinokio":
        raise ValueError("This copy of Astrid wasn't installed with Pinokio.")
    version = current(root)
    if version is None:
        raise ValueError("Astrid isn't fully installed. Choose Pinokio's Reset button, then install again.")
    data = absolute(record["data_root"])
    launch_admission(version, data, custody=custody_fd is not None)
    env = launch_environment(version, data)
    # Use the console interpreter on Windows. pythonw is specific to the
    # hidden Scheduled Task; the same Runtime command/settings run here in
    # the console owner so Ctrl+Break and console-close reach its handlers.
    arguments = command(version, data, env_file=root / "service-env.json", extra_args=argv,
                        lock_fd=lock_fd if system != "win32" else None, custody_fd=custody_fd)
    if system != "win32":
        # The kernel lock follows this descriptor into the Runtime owner. Its
        # command prelude restores close-on-exec before Runtime starts children.
        if lock_fd is not None:
            os.set_inheritable(lock_fd, True)
        os.execvpe(arguments[0], arguments, env)
    else:
        os.environ.clear()
        os.environ.update(env)
        os.chdir(version.root)
        # os.exec on Windows creates another process. Evaluate the canonical
        # -c command in this interpreter instead, with no waiting shim/child.
        exec(compile(arguments[-1], "<astrid-runtime run>", "exec"), {"__name__": "__main__"})


if __name__ == "__main__":
    launch(Path(sys.argv[1]), sys.argv[2:])
