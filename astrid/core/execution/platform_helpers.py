"""Agent policy composed from Runtime's native platform primitives.

Candidates to upstream: ownership checks accepting both an open handle and
existing metadata, and recovery of an observed, birth-anchored POSIX group.
"""
from __future__ import annotations

import os
import stat
import sys

from runtime_protocol import platform


def owned_by_current_user(path_or_fd, metadata, *, mode: int | None = None,
                          forbidden: int = 0) -> bool:
    # Windows stat.st_uid has no SID. Keep ownership-only checks distinct
    # from private-state DACL checks, as on POSIX (media may be shared).
    if sys.platform == "win32":
        if path_or_fd is None:
            return False
        if mode is not None or forbidden:
            return platform.is_owner_only(path_or_fd)
        from runtime_protocol import windows_security
        try:
            with windows_security._handle(path_or_fd) as (handle, _info):
                windows_security._validate_handle(handle, owner_only=True)
            return True
        except OSError:
            return False
    return (platform.is_current_user(metadata)
            and (mode is None or stat.S_IMODE(metadata.st_mode) == mode)
            and not metadata.st_mode & forbidden)


_probes: dict[str, dict] = {}
# Capability children are separate processes: the host names its support root
# here so they read the same process-control.json (the file decides the mode).
PROCESS_CONTROL_SUPPORT_ENV = "ASTRID_PROCESS_CONTROL_SUPPORT"
_support_root: str | None = None


def process_control_report() -> dict:
    """Runtime's live signalling probe, taken once per process (and mode).

    The one answer to "can we signal safely here": custody and exact-stop
    gates read it instead of checking whether pidfd wrappers are callable,
    which they are in seccomp sandboxes where the syscall itself fails.
    """
    mode = platform.process_control()
    report = _probes.get(mode)
    if report is None:
        report = _probes[mode] = platform.process_control_probe()
    return report


def configure_process_control(support_root, *, announce: bool = True) -> str:
    """Fix this process's signalling mode from Runtime support before spawning.

    Runtime's process-control.json (``workspace create|attach --process-control``)
    is the only switch; missing means strict. Hosts announce the live probe once.
    """
    from runtime_protocol import process_control

    global _support_root
    mode = process_control.configure_from(support_root)
    _support_root = str(support_root)
    if announce:
        process_control.announce(process_control_report())
    return mode


def process_control_support() -> str | None:
    """The support root this process configured from, for its children."""
    return _support_root


def _configure_from_environment() -> None:
    support = os.environ.get(PROCESS_CONTROL_SUPPORT_ENV)
    if support and platform.process_control() == "strict":
        try:
            configure_process_control(support, announce=False)
        except Exception:  # noqa: BLE001 - an unreadable setting leaves strict, which fails closed
            pass


def signal_observed_group(pgid: int, signum: int, *, leader_birth: str | None) -> None:
    """Recover a journalled group without granting signal authority to a PGID.

    Each member is revalidated by Runtime while binding its kernel signal
    handle. A reused leader invalidates the entire observation. On Linux the
    walk is Runtime's (one identity, and its sandbox fallback); the leader
    check stays here because the journalled birth is in the agent's format.
    """
    from .process_group import _process_snapshot

    if pgid <= 1 or pgid == platform.process_group(os.getpid()) or not leader_birth:
        raise ValueError("process group lacks a safe birth anchor")
    if sys.platform.startswith("linux"):
        _signal_linux_observed_group(pgid, signum, leader_birth)
        return
    census = _process_snapshot()
    if not census:
        raise OSError("process census unavailable")
    leader = census.get(pgid)
    if leader is not None and leader.birth != leader_birth:
        raise ProcessLookupError("process group leader was replaced")
    expected = []
    for pid, info in census.items():
        if info.pgid != pgid:
            continue
        identity = platform.process_identity(pid)
        if identity is None:
            continue
        # Compare against a second agent census; its persisted birth format is
        # intentionally different from Runtime's Linux identity format.
        current = _process_snapshot().get(pid)
        if current is None or current.birth != info.birth or current.pgid != pgid:
            raise ProcessLookupError("process group member changed")
        expected.append(identity)
    for identity in reversed(expected):
        try:
            platform.signal_process(identity, signum)
        except ProcessLookupError:
            pass


def _signal_linux_observed_group(pgid: int, signum: int, leader_birth: str) -> None:
    from .process_group import process_birth_identity

    leader = platform.process_identity(pgid)
    if leader is not None:
        if process_birth_identity(pgid) != leader_birth:
            raise ProcessLookupError("process group leader was replaced")
        if leader.get("process_group") != pgid:
            leader = None  # the leader left its group: signal only the members
    try:
        platform.signal_observed_group(pgid, signum, leader=leader)
    except ProcessLookupError as exc:
        if exc.strerror != "no process in the group could be signalled":
            raise  # nothing left to signal was a quiet no-op here before


_configure_from_environment()
