"""One resolver of the Astrid installation on this computer, and where it lives.

The installer, the update job and the run receipt ask this module, and only
this module, which program root, data root and service label are active. A
finished R0 to R1 storage handoff is normalised here once, so afterwards a
handed-off install reads as an ordinary direct install whose data root is the
R1 copy. The R0 copy is kept as provenance (``retained_r0_data_root``).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .core import Version, absolute, current, owned_by_local_update, pending_transaction, read_json
from .service import LABEL, default_program_root, install_selection, installer_lock_path

KINDS = ("none", "r0", "handoff", "handoff-rolled-back", "direct", "pinokio", "source", "fully-local")
HANDOFF_JOURNAL = "astrid-r1-handoff.json"
HANDOFF_ARCHIVE = "astrid-r1-handoff.completed.json"


def handoff_journal(home: Path, system: str) -> Path:
    return installer_lock_path(home, system).with_name(HANDOFF_JOURNAL)


def handoff_archive(home: Path, system: str) -> Path:
    """The archived journal of a finished handoff: provenance, never authority."""
    return installer_lock_path(home, system).with_name(HANDOFF_ARCHIVE)


@dataclass(frozen=True)
class LiveRuntime:
    data_root: Path
    instance_id: str | None
    pid: int | None
    selected: bool
    alive: bool = False


@dataclass(frozen=True)
class State:
    kind: str
    program_root: Path
    active_data_root: Path | None
    service_label: str | None
    selected_version: Version | None
    phase: str | None = None
    retained_r0_data_root: Path | None = None
    live_identity: dict | None = None
    live_runtimes: tuple[LiveRuntime, ...] = ()
    free_bytes: int | None = None
    astrid_bytes: dict = field(default_factory=dict)

    @property
    def selected_release(self) -> int | None:
        return None if self.selected_version is None else self.selected_version.release

    @property
    def live_release(self) -> int | None:
        identity = self.live_identity or {}
        release = identity.get("release")
        return release if type(release) is int else None


def probe_runtime_identity(data_root: Path | None, program_root: Path | None = None) -> dict | None:
    """The live Runtime's health identity at a data root, or None when it is not answering.

    ``program_root`` is unused here; hermetic tests substitute a probe that reports
    the selected version's identity, so the post-condition still compares releases.
    """
    if data_root is None:
        return None
    try:
        discovery = read_json(Path(data_root) / "runtime" / "discovery.json")
        instance_id = discovery.get("runtime_instance_id")
        endpoint = discovery.get("endpoint") or discovery.get("door_endpoint")
        if not isinstance(instance_id, str) or not instance_id or not isinstance(endpoint, str) or not endpoint:
            return None
        from astrid_runtime.identity import validate_live_owner_identity
        from astrid_runtime.runtime_boundary import LocalRuntimeBoundary

        health = LocalRuntimeBoundary().owner_startup_identity(endpoint=endpoint, instance_id=instance_id)
        return {**validate_live_owner_identity(health, instance_id=instance_id), "instance_id": instance_id}
    except Exception:  # noqa: BLE001 - an unreachable Runtime is an observation, not a failure
        return None


probe_live = probe_runtime_identity  # hermetic tests replace this name; the real probe stays reachable


def _process_alive(pid: int | None, birth: str | None = None) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        from runtime_protocol import platform

        identity = platform.process_identity(pid)
    except Exception:  # noqa: BLE001 - an unknowable process is treated as live below
        return True
    if identity is None:
        return False
    return not birth or identity.get("birth_id") == birth


def _discovered(data_root: Path, *, selected: bool) -> LiveRuntime | None:
    try:
        discovery = json.loads((data_root / "runtime" / "discovery.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(discovery, dict):
        return None
    pid = discovery.get("pid")
    instance = discovery.get("runtime_instance_id")
    birth = discovery.get("process_birth_id")
    pid = pid if type(pid) is int else None
    return LiveRuntime(data_root, instance if isinstance(instance, str) else None, pid, selected,
                       _process_alive(pid, birth if isinstance(birth, str) else None))


def _size(path: Path | None) -> int:
    total = 0
    if path is None or not path.is_dir():
        return total
    for folder, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total


def _path(value) -> Path | None:
    try:
        return absolute(value) if isinstance(value, str) and value else None
    except ValueError:
        return None


class Installation:
    @staticmethod
    def resolve(home: Path, *, system: str | None = None, root: Path | None = None,
                probe=None, measure: bool = False) -> State:
        """Read, never write, which Astrid installation is active and where it lives."""
        system = system or sys.platform
        selection = read_json(install_selection(home, system))
        if selection.get("owner") not in (None, LABEL):
            selection = {}
        program_root = _path(selection.get("program_root")) if not selection.get("uninstalled") else None
        program_root = root or program_root or absolute(default_program_root(home, system))
        marker = read_json(program_root / "install.json")
        try:
            selected = current(program_root)
        except (OSError, ValueError, KeyError, TypeError):
            selected = None
        journal = read_json(handoff_journal(home, system)) if os.path.lexists(handoff_journal(home, system)) else {}
        retained = _path(selection.get("retained_r0_data_root") or marker.get("retained_r0_data_root"))
        data = _path(selection.get("data_root") or marker.get("data_root"))
        phase = journal.get("phase") if journal else None
        try:
            pending = pending_transaction(program_root) or {}
        except (OSError, ValueError):
            pending = {}
        if journal:
            kind = "handoff-rolled-back" if phase == "rolled_back" else "handoff"
            if phase == "complete":
                # Until normalisation the selection still names R0; the live copy is R1.
                data = _path(journal.get("r1_data_root")) or data
                retained = _path(journal.get("r0_data_root"))
        elif "pinokio" in (selection.get("managed_by"), marker.get("managed_by")):
            kind = "pinokio"
        elif pending and owned_by_local_update(pending):
            kind = "fully-local"
        elif selected is None:
            kind = "none" if not marker and not selection else "source"
        else:
            kind = "r0" if selected.release == 0 else "direct"
        identity = (probe or probe_live)(data, program_root)
        if kind in {"direct", "none"} and identity and identity.get("managed_by") == "source":
            kind = "source"
        runtimes = tuple(found for found in (
            _discovered(data, selected=True) if data else None,
            _discovered(retained, selected=False) if retained else None) if found is not None)
        try:
            free = shutil.disk_usage(data if data and data.exists() else program_root.parent).free
        except OSError:
            free = None
        sizes = {}
        if measure:
            sizes = {"versions": _size(program_root / "versions"), "backups": _size(program_root / "backups"),
                     "retained_r0": _size(retained), "workspace": _size(data)}
        return State(kind=kind, program_root=program_root, active_data_root=data,
                     service_label=selection.get("service_label") or (None if kind == "none" else LABEL),
                     selected_version=selected, phase=phase, retained_r0_data_root=retained,
                     live_identity=identity, live_runtimes=runtimes, free_bytes=free, astrid_bytes=sizes)


def normalised_handoff(service, root: Path) -> dict | None:
    """The archived journal when this install is a normalised handed-off direct install.

    Normalised means: the handoff journal was archived, and the marker and the
    selection name the R1 copy as ``data_root`` with R0 kept as provenance.
    """
    archive = handoff_archive(service.home, service.system)
    if os.path.lexists(handoff_journal(service.home, service.system)) or not os.path.lexists(archive):
        return None
    from .boundary_registration import _regular

    value = read_json(_regular(archive))
    marker = read_json(Path(root) / "install.json")
    selection = read_json(service.selection)
    if (value.get("phase") != "complete" or value.get("program_root") != str(absolute(root))
            or any(record.get("data_root") != value.get("r1_data_root")
                   or record.get("retained_r0_data_root") != value.get("r0_data_root")
                   or "r1_data_root" in record for record in (marker, selection))
            or selection.get("realm_root") != str(Path(value["r1_data_root"]) / "runtime/realms" / value["realm_id"])):
        raise ValueError("The archived storage move does not match this installation; both data folders were kept.")
    return value


def adopt_if_handed_off(backend, root: Path):
    """Point a backend's service at the R1 label when this install is a normalised handoff.

    Everything except ``--rollback`` operates the R1 service; rollback needs
    R0's own definition and therefore never adopts.
    """
    service = getattr(backend, "service", None)
    if (service is None or getattr(backend, "managed_by", None) == "pinokio"
            or getattr(service, "handed_off", False) or not hasattr(service, "home")):
        return backend
    if normalised_handoff(service, root):
        service.adopt_handed_off_label()
    return backend


def retained_versions(service, root: Path) -> set[Path]:
    """Version folders a finished handoff keeps for --rollback (the R0 program)."""
    if not hasattr(service, "home") or not hasattr(service, "system"):
        return set()  # not a login service, so there is no storage move to keep
    try:
        value = normalised_handoff(service, root)
    except (OSError, ValueError, TypeError, KeyError):
        # Fail closed: when the move's evidence cannot be read, keep every version.
        versions = absolute(root) / "versions"
        return {path for path in versions.iterdir()} if versions.is_dir() else set()
    if not value:
        return set()
    pointer = str(value.get("old_pointer") or "").strip()
    return {absolute(root) / "versions" / pointer} if pointer else set()


class OtherRuntimeRunning(ValueError):
    """A second live Runtime for this workspace that the installer may not stop."""


def _r0_label_pid(service) -> int | None:
    """The pid the R0 login label's manager runs now, or None when that label runs nothing."""
    from . import r0_stop

    if service.system == "linux":
        output = r0_stop._run(service, ["systemctl", "--user", "show", "astrid.service",
                                        "--property=MainPID", "--property=ActiveState"])
        fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        pid = int(fields.get("MainPID") or 0)
        return pid if fields.get("ActiveState") in {"active", "activating", "reloading"} and pid > 0 else None
    if service.system == "darwin":
        import re

        output = r0_stop._launchd_print(service, f"gui/{r0_stop._launchd_uid()}/{LABEL}")
        found = re.findall(r"(?m)^\tpid = (\d+)$", output or "")
        return int(found[0]) if len(found) == 1 else None
    return None


def retire_r0_leftover(service, root: Path, *, timeout: float = 90.0, poll: float = 0.2) -> bool:
    """Before any stop: stop a known R0-era Runtime left by a finished storage move.

    Known means the retained R0 root publishes a live owner and R0's own login
    label (``astrid.service`` / ``haus.astrid.runtime``) runs exactly that
    process. It is disabled and stopped through that label; its files are kept
    for --rollback. Any other live second Runtime, or a leftover that does not
    stop, is refused with ``other-runtime-running`` before anything is stopped.
    Returns whether a leftover was stopped.
    """
    import time

    from . import r0_stop

    value = normalised_handoff(service, root)
    if not value:
        return False
    leftover = _discovered(absolute(value["r0_data_root"]), selected=False)
    if leftover is None or not leftover.alive:
        return False
    try:
        owner = _r0_label_pid(service)
    except Exception as failure:  # noqa: BLE001 - an unreadable manager is not a known leftover
        raise OtherRuntimeRunning("Another Astrid is running this workspace and its owner could not be checked.") from failure
    if owner is None or owner != leftover.pid:
        raise OtherRuntimeRunning(
            f"Another Astrid is running this workspace from {value['r0_data_root']} (pid {leftover.pid}) "
            "and it is not the earlier version's login service, so nothing was stopped.")
    try:
        r0_stop._disable_and_stop(service, r0_stop._launchd_uid() if service.system == "darwin" else None)
    except Exception as failure:  # noqa: BLE001 - reported as the refusal below
        raise OtherRuntimeRunning("The earlier Astrid copy could not be stopped.") from failure
    deadline = time.monotonic() + timeout
    while _process_alive(leftover.pid):
        if time.monotonic() >= deadline:
            raise OtherRuntimeRunning("The earlier Astrid copy did not stop.")
        time.sleep(poll)
    return True
