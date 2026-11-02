"""Fail-closed compatibility for the reviewed Release-0 Linux birth spelling."""

from __future__ import annotations


def _current_linux_scope(pid: int) -> tuple[str, str] | None:
    """Return the current boot and host PID namespace for one local PID.

    Runtime's scoped Linux spelling is produced from ``/proc/<pid>/ns/pid``.
    A Release-0 record did not retain either scope component, so bridge it only
    when the observed process is still in this installer's host namespace and
    the observed boot is the current boot.  Any unreadable or malformed proc
    fact is an unresolved identity, never compatibility.
    """
    from pathlib import Path
    from uuid import UUID

    if type(pid) is not int or pid <= 0:
        return None
    proc = Path("/proc")
    try:
        boot = (proc / "sys" / "kernel" / "random" / "boot_id").read_text(encoding="utf-8").strip()
        target_namespace = (proc / str(pid) / "ns" / "pid").stat().st_ino
        host_namespace = (proc / "self" / "ns" / "pid").stat().st_ino
    except (OSError, UnicodeError, ValueError):
        return None
    try:
        canonical_boot = str(UUID(boot))
    except ValueError:
        return None
    if canonical_boot != boot or type(target_namespace) is not int or type(host_namespace) is not int:
        return None
    if target_namespace <= 0 or host_namespace <= 0 or target_namespace != host_namespace:
        return None
    return boot, str(host_namespace)


def birth_ids_match(recorded, observed, recorded_pid: int, observed_pid: int) -> bool:
    """Match exact IDs, or only the proven R0-to-R1 Linux successor form.

    The Release-0 spelling contains merely a process start tick.  It is not
    authority by itself: it can bridge only to the reviewed scoped Release-1
    spelling for a caller-verified PID in the current boot and this host PID
    namespace.  All other unrecognized, partial, or unavailable observations
    fail closed.
    """
    import re
    import sys

    if (not isinstance(recorded, str) or not recorded or not isinstance(observed, str) or not observed
            or type(recorded_pid) is not int or recorded_pid <= 0
            or type(observed_pid) is not int or observed_pid != recorded_pid):
        return False
    if recorded == observed:
        return True
    if sys.platform != "linux":
        return False
    legacy = re.fullmatch(r"proc-start-ticks:([1-9][0-9]*)", recorded)
    scoped = re.fullmatch(
        r"proc-start-ticks:([1-9][0-9]*);boot:"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12});pidns:([1-9][0-9]*)",
        observed,
    )
    if legacy is None or scoped is None or legacy.group(1) != scoped.group(1):
        return False
    return _current_linux_scope(recorded_pid) == (scoped.group(2), scoped.group(3))
