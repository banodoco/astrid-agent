"""Stop the registered R0 service and wait for its realm owner lock.

The direct installer holds its per-user lock while using this boundary.
The handoff retains this stop intent until rollback restores R0.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
import plistlib
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from runtime_protocol import platform

from .service import LABEL, TASK

R1_LABEL = "haus.astrid.runtime.r1"
R1_UNIT = "astrid-r1.service"


def _run(service, argv):
    # R0's own stop grace is 90 seconds. Never inherit Backend's 30 minute
    # command timeout for an unresponsive manager.
    return service.run(argv, timeout=105)


def _launchd_print(service, target):
    try:
        return _run(service, ["launchctl", "print", target])
    except subprocess.CalledProcessError as exc:
        output = str(exc.output or "")
        if "Could not find service" in output and target.rsplit("/", 1)[-1] in output:
            return None
        raise


def _require_no_r1(service):
    """Only an absent R1 job permits R0 restart in this pre-handoff piece.

    A registered inactive job can restart automatically. Leave it and the
    recovery intent alone for B's explicit disable-R1/re-enable-R0 rollback.
    This is a manager query, never process/discovery matching.
    """
    if service.system == "linux":
        argv = ["systemctl", "--user", "show", R1_UNIT,
                "--property=LoadState", "--property=ActiveState"]
        try:
            output = _run(service, argv)
        except subprocess.CalledProcessError as exc:
            output = str(exc.output or "")
        fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if fields.get("LoadState") == "not-found" and fields.get("ActiveState") == "inactive":
            return
    elif service.system == "darwin":
        if _launchd_print(service, f"gui/{_launchd_uid()}/{R1_LABEL}") is None:
            return
    else:
        raise R0StopError("The R1 scheduled task state cannot be verified; R0 was not restarted.")
    raise R0StopError("R1 is registered or its state is unknown. R0 was not restarted; use the R1 service rollback before retrying.")


class R0StopError(ValueError):
    """R0 could not be stopped safely; the upgrade must be refused."""


def _read_record(path):
    from .core import absolute, read_json
    path = absolute(path)
    if not path.is_file():
        raise R0StopError(f"Missing upgrade evidence: {path}")
    return read_json(path)


def _selected_realm(data):
    from .core import absolute
    catalog = _read_record(data / "runtime" / "catalog.json")
    selected = catalog.get("selected_realm_id")
    rows = catalog.get("realms")
    if (catalog.get("version") != 1 or not isinstance(selected, str) or not selected
            or not isinstance(rows, list)):
        raise R0StopError("R0's selected realm cannot be read safely; no service was stopped.")
    matches = [row for row in rows if isinstance(row, dict) and row.get("realm_id") == selected]
    if len(matches) != 1 or not isinstance(matches[0].get("data_root"), str):
        raise R0StopError("R0's selected realm is ambiguous; no service was stopped.")
    return selected, absolute(matches[0]["data_root"])


def is_r0_installation(root, service):
    from .core import current
    version = current(root)
    if version is not None:
        return version.release == 0
    # The CLI writes install.json before Installer.run, so its ownership alone
    # cannot distinguish R0 from a fresh or interrupted R1 installation.
    if service is None:
        raise R0StopError("Cannot determine whether an R0 service exists; installation was not changed.")
    if service.system in {"linux", "darwin"}:
        try:
            Path(service.path).lstat()
        except FileNotFoundError:
            return False
        return True  # Include unsafe or dangling entries; the handoff will refuse them.
    if service.system == "win32":
        return service.login_service_exists()  # Propagate manager query failures.
    raise R0StopError(f"Unsupported R0 service manager: {service.system}")


def refuse_legacy_r0_recovery(root):
    from .core import Version, current, pending_transaction
    pending = pending_transaction(root)
    if pending and pending.get("transaction_kind") != "app":
        old = pending.get("old")
        selected = current(root)
        if ((selected is not None and selected.release == 0)
                or not isinstance(old, dict) or Version.load(old).release == 0):
            raise R0StopError(
                "An interrupted legacy R0 update is retained. Automatic in-place restore or migration "
                "is disabled; preserve both roots and resolve that journal before the copy-only upgrade."
            )


def refuse_unintegrated_r1_handoff(root, service):
    from .boundary_registration import journal_path
    from .core import read_json
    from .installation import normalised_handoff

    if normalised_handoff(service, root):
        return  # A finished, normalised handoff is an ordinary direct install.

    if os.path.lexists(journal_path(service)):
        raise R0StopError("An R1 registration needs recovery. Run the install line again; both data roots were kept.")
    marker = read_json(root / "install.json")
    selection = read_json(service.selection)
    if service.system == "linux":
        definition = service.path.with_name(R1_UNIT)
    elif service.system == "darwin":
        definition = service.path.with_name(R1_LABEL + ".plist")
    else:
        definition = root / "task-r1.xml"
    if (marker.get("r1_data_root") or selection.get("r1_data_root")
            or definition.exists() or definition.is_symlink()):
        raise R0StopError(
            "An R1 handoff is already present without matching adoption authority. "
            "Preserve both roots and recover its installer journal before proceeding."
        )


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
                or not executable.is_absolute()
                or not Path(os.path.abspath(executable)).is_relative_to(root)):
            raise ValueError("invalid action")
    except (OSError, ValueError, KeyError, IndexError, TypeError, plistlib.InvalidFileException) as exc:
        raise R0StopError("The R0 plist action does not match its program root.") from exc
    return args


def _validate_manager(service, uid):
    if service.system == "linux":
        output = _run(service, ["systemctl", "--user", "show", "astrid.service", "--property=FragmentPath"])
        fields = [line.partition("=")[2] for line in output.splitlines()
                  if line.startswith("FragmentPath=")]
        if fields != [str(service.path)]:
            raise R0StopError("The registered R0 systemd FragmentPath does not match the installer unit; refusing to stop it.")
    elif service.system == "darwin":
        target = f"gui/{uid}/{LABEL}"
        output = _launchd_print(service, target)
        if output is None:
            raise R0StopError("The R0 launchd service is not loaded.")
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
        _run(service, ["systemctl", "--user", "disable", "--now", "astrid.service"])
    elif service.system == "darwin":
        target = f"gui/{uid}/{LABEL}"
        _run(service, ["launchctl", "disable", target])
        _run(service, ["launchctl", "bootout", target])
    else:
        service.powershell(f"Disable-ScheduledTask -TaskName '{TASK}' -TaskPath '\\' | Out-Null")
        service.powershell(f"Stop-ScheduledTask -TaskName '{TASK}' -TaskPath '\\'")


def _reenable(service, uid, *, stopped):
    if service.system == "linux":
        _run(service, ["systemctl", "--user", "enable", "--now", "astrid.service"])
    elif service.system == "darwin":
        target = f"gui/{uid}/{LABEL}"
        _run(service, ["launchctl", "enable", target])
        if stopped:
            _run(service, ["launchctl", "kickstart", target])
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


@contextmanager
def owner_lock(realm):
    """Retain the existing read-only lifetime lock through a selection switch."""
    from .core import absolute
    path = absolute(realm) / "owner.lock"
    before = path.lstat()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    locked = False
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(before.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise R0StopError("Owner lock changed while opening it.")
        platform.lock_file(fd, blocking=False)
        locked = True
        if (path.lstat().st_dev, path.lstat().st_ino) != (opened.st_dev, opened.st_ino):
            raise R0StopError("Owner lock changed while acquiring it.")
        yield
    finally:
        if locked:
            platform.unlock_file(fd)
        os.close(fd)


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


def recover_r0(service):
    """Re-run recovery: check R1's manager, restore the bound R0, clear intent."""
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
        _require_no_r1(service)
        uid = _launchd_uid() if service.system == "darwin" else None
        if service.system == "linux":
            _validate_manager(service, uid)
        elif service.system == "darwin":
            _validate_plist(service)
            # A loaded foreign job must never be enabled or started.
            target = f"gui/{uid}/{LABEL}"
            if _launchd_print(service, target) is None:
                # launchd will reject bootstrap while the label is disabled.
                _run(service, ["launchctl", "enable", target])
                _run(service, ["launchctl", "bootstrap", f"gui/{uid}", service.path])
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
    destination = Path(str(data_root) + "-r1")
    if os.path.lexists(destination):
        raise R0StopError(f"The R1 destination already exists: {destination}.")
    uid = _launchd_uid() if service.system == "darwin" else None
    _validate_manager(service, uid)
    _require_no_r1(service)
    _write_marker(service, data_root)
    try:
        _disable_and_stop(service, uid)
        _wait_for_owner_lock(realm_root, timeout, poll_interval)
    except BaseException as exc:
        try:
            recover_r0(service)
        except BaseException as recovery:
            raise R0StopError(f"R0 stop failed ({exc}); automatic re-enable also failed ({recovery}). Re-enable the Astrid service in the OS service manager, then run the install line again.") from recovery
        if isinstance(exc, R0StopError) or not isinstance(exc, Exception):
            raise
        raise R0StopError(f"R0 stop or owner-lock check failed: {exc}. Run the install line again after checking the Astrid service.") from exc
