"""Pinokio's foreground owner, using the login service's Runtime command."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from runtime_protocol import platform

from .core import absolute, current, read_json, transaction_launch_version
from .service import LABEL, command, installer_lock_path


def launch(root: Path, argv: list[str], *, system: str | None = None) -> None:
    system = system or sys.platform
    if (os.environ.get("ASTRID_INSTALLER_OPERATION_ID") is None
            and (absolute(root) / "install.json").is_file()):
        from .app_update import locked

        # Updates release the lifetime lock to their bound session. Ordinary
        # Start must not race that handoff or an online candidate preparation.
        with locked(absolute(root) / ".app-update.lock"):
            pass
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


def _run(root: Path, argv: list[str], system: str, lock_fd: int) -> None:
    root = absolute(root)
    record = read_json(root / "install.json")
    if record.get("owner") != LABEL or record.get("managed_by") != "pinokio":
        raise ValueError("This copy of Astrid wasn't installed with Pinokio.")
    version = current(root)
    if version is None:
        raise ValueError("Astrid isn't fully installed. Choose Pinokio's Reset button, then install again.")
    data = absolute(record["data_root"])
    operation_id = os.environ.get("ASTRID_INSTALLER_OPERATION_ID")
    if operation_id is not None:
        if not isinstance(operation_id, str) or re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
            raise ValueError("The Pinokio transaction launch binding is invalid.")
        transaction = read_json(root / "update-transaction.json")
        intended = transaction_launch_version(transaction) if transaction else None
        if (not transaction or transaction.get("step") in {"complete", "rolled_back"}
                or transaction.get("transaction_kind") not in {"app", "migration"}
                or transaction.get("operation_id") != operation_id
                or absolute(transaction.get("data_root", "")) != data
                or (transaction.get("launch_intent") or {}).get("purpose") != "pinokio_start"
                or intended is None or intended.document() != version.document()):
            raise ValueError("The Pinokio transaction launch binding does not match the pending app update.")
    else:
        transaction = read_json(root / "update-transaction.json")
        if transaction and transaction.get("step") not in {"complete", "rolled_back"}:
            raise ValueError("Choose Pinokio's Update button to recover the interrupted update before choosing Start.")
    env = {key: value for key, value in os.environ.items() if key not in {
        "PYTHONPATH", "PYTHONHOME", "ASTRID_WORKSPACE_ENDPOINT",
        "ASTRID_WORKSPACE_CREDENTIAL", "ASTRID_RUNTIME_SOURCE_MANIFEST",
        "ASTRID_RUNTIME_DATA_ROOT", "ASTRID_PACKS_PATH", "ASTRID_INSTALLER_OPERATION_ID",
    }}
    env.update(version.env)
    env.pop("ASTRID_INSTALLER_OPERATION_ID", None)
    if operation_id is not None:
        env["ASTRID_INSTALLER_OPERATION_ID"] = operation_id
    env["ASTRID_INSTALL_MANAGED_BY"] = "pinokio"
    # Use the console interpreter on Windows. pythonw is specific to the
    # hidden Scheduled Task; the same Runtime command/settings run here in
    # the console owner so Ctrl+Break and console-close reach its handlers.
    command_options = {"extra_args": argv, "lock_fd": lock_fd if system != "win32" else None}
    if operation_id is not None:
        command_options["installer_operation_id"] = operation_id
    arguments = command(version, data, env_file=root / "service-env.json", **command_options)
    if system != "win32":
        # The kernel lock follows this descriptor into the Runtime owner. Its
        # command prelude restores close-on-exec before Runtime starts children.
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
