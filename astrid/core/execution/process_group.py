"""Process-only helpers for cancellation of owned subprocesses."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime_protocol import platform

from astrid.core.execution.custody_broker import (
    CustodyError as CleanupUncertainError,
)
from astrid.core.execution.custody_broker import (
    CustodyLockBusy,
    ProcessCustody,
    RoleBoundCustodyBroker,
    _atomic_owner_json,
    _candidate_reconciliation_deadline,
    _darwin_process_records,
    _ps_lstart_birth,
)


@dataclass(frozen=True)
class _ProcessInfo:
    pid: int
    ppid: int
    pgid: int
    birth: str
    status: int | None = None
    start_sec: int | None = None
    start_usec: int | None = None
    uid: int | None = None


# ``ps`` reads the whole process table, which can take longer than a short
# deadline on a loaded machine.  Bounded retries tolerate that transient
# slowness; they do not change what a census proves (see _process_snapshot).
_CENSUS_TIMEOUT_SECONDS = 5.0
_CENSUS_ATTEMPTS = 3
_CENSUS_RETRY_DELAY_SECONDS = 0.1


class _Census(dict):
    """A process census.  When it is empty, ``failure`` says why ps gave none.

    ``exited`` maps pid -> birth token for rows this census saw as unreaped
    zombies (Linux).  Such a row is not in the census proper, which lists live
    processes only; it is positive proof that exactly that incarnation exited.
    """

    failure: str | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.exited: dict[int, str] = {}


def _failed_census(reason: str) -> _Census:
    census = _Census()
    census.failure = reason
    return census


def _census_message(message: str, census: Mapping[int, _ProcessInfo]) -> str:
    reason = getattr(census, "failure", None)
    return f"{message}: {reason}" if reason else message


def _process_snapshot() -> dict[int, _ProcessInfo]:
    """Read a process census without invoking a shell."""
    if sys.platform == "win32":
        raise CleanupUncertainError("Windows process trees require retained Job custody, not a POSIX census")
    if sys.platform == "darwin":
        return {
            row["pid"]: _ProcessInfo(row["pid"], row["parent_pid"], row["pgid"],
                                     _ps_lstart_birth(row["start_sec"]) if row["pid"] > 0 else "",
                                     row["status"], row["start_sec"], row["start_usec"], row.get("uid"))
            for row in _darwin_process_records()
        }
    ps = next(
        (candidate for candidate in ("/bin/ps", "/usr/bin/ps") if os.path.isfile(candidate)),
        "ps",
    )
    outcomes: list[str] = []
    for attempt in range(_CENSUS_ATTEMPTS):
        if attempt:
            time.sleep(_CENSUS_RETRY_DELAY_SECONDS)
        try:
            result = subprocess.run(
                [ps, "-axo", "pid=,ppid=,pgid=,lstart="],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=_CENSUS_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            outcomes.append(f"timed out after {_CENSUS_TIMEOUT_SECONDS:g}s")
            continue
        except (OSError, subprocess.SubprocessError) as exc:
            # A missing or unrunnable ps is not transient; do not retry.
            return _failed_census(f"ps could not run: {exc}")
        if result.returncode != 0:
            stderr = " ".join((result.stderr or "").split())[:200]
            outcomes.append(f"rc={result.returncode}" + (f": {stderr}" if stderr else ""))
            continue
        return _parse_census(result.stdout)
    if len(set(outcomes)) == 1:
        return _failed_census(f"ps {outcomes[0]} x{len(outcomes)}")
    return _failed_census("ps " + "; ".join(outcomes))


def _parse_census(stdout: str) -> _Census:
    entries = _Census()
    vanished: set[int] = set()
    for line in stdout.splitlines():
        fields = line.strip().split(None, 3)
        if len(fields) != 4:
            continue
        try:
            pid, ppid, pgid = (int(value) for value in fields[:3])
        except ValueError:
            continue
        birth = process_birth_identity(pid) if sys.platform.startswith("linux") else "ps-lstart:" + fields[3]
        if birth:
            entries[pid] = _ProcessInfo(pid, ppid, pgid, birth)
        else:
            vanished.add(pid)
            if sys.platform.startswith("linux") and (exited := _zombie_birth_identity(pid)):
                entries.exited[pid] = exited
    if vanished:
        # ``ps`` lists a process, then its /proc identity is read later.  A
        # parent that exits in between is dropped from the census while its
        # children's listed ppid still names it, which reads as an unverified
        # child of a missing parent.  The kernel reparents a process's
        # children before it is visible as a zombie, so a fresh ppid is
        # authoritative: re-read it for exactly those children.
        for pid, info in tuple(entries.items()):
            if info.ppid in vanished:
                fresh = _linux_ppid(pid) if sys.platform.startswith("linux") else None
                if fresh is not None and fresh != info.ppid:
                    entries[pid] = _ProcessInfo(pid, fresh, info.pgid, info.birth)
    return entries


def _linux_ppid(pid: int) -> int | None:
    try:
        with open("/proc/%d/stat" % pid, encoding="ascii") as stream:
            return int(stream.read().rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError, UnicodeError):
        return None


CLEANUP_LATCH_NAME = "generic-host.cleanup-uncertain.json"


def write_cleanup_latch(path: Path, *, notes: list[str], since: str) -> None:
    """Record a host's unresolved-cleanup notes for ``astrid doctor``.  Never raises.

    This file is a record only: it never gates admission.  Each note names
    something that remains (a process group, a credential copy, an attempt
    folder) after the task that caused it failed.
    """
    try:
        pid = os.getpid()
        info = _process_snapshot().get(pid) if sys.platform != "win32" else None
        record = {
            "pid": pid,
            "process_birth_id": (process_birth_identity(pid) if sys.platform == "win32"
                                 else info.birth if info is not None else None),
            "since": since,
            "notes": list(notes),
        }
        temporary = path.with_name(f"{path.name}.{pid}.tmp")
        temporary.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    except Exception:
        return


def read_cleanup_latch(path: Path) -> dict[str, Any] | None:
    """Return the latch record while its host is alive, else ``None``.

    A record whose host pid is gone or was reused is stale and is ignored.  If
    the census itself is unavailable, the record is returned with
    ``liveness`` set to ``unverified`` instead of being dropped.
    """
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("pid"), int):
        return None
    if sys.platform == "win32":
        try:
            birth = process_birth_identity(record["pid"])
        except OSError as exc:
            return {**record, "liveness": f"unverified: {exc}"}
        if not birth or record.get("process_birth_id") not in (None, birth):
            return None
        return {**record, "liveness": "verified" if record.get("process_birth_id") else "unverified: birth unknown"}
    try:
        census = _process_snapshot()
    except CleanupUncertainError as exc:
        return {**record, "liveness": f"unverified: {exc}"}
    if not census:
        reason = getattr(census, "failure", None) or "census empty"
        return {**record, "liveness": f"unverified: {reason}"}
    info = census.get(record["pid"])
    if info is None:
        return None
    if record.get("process_birth_id") not in (None, info.birth):
        return None
    return {**record, "liveness": "verified" if record.get("process_birth_id") else "unverified: birth unknown"}


def describe_cleanup_latch(record: Mapping[str, Any]) -> str:
    """One doctor line for a host that holds unresolved cleanup notes."""
    return (f"pack host has unresolved cleanup since {record.get('since')} (still accepting work): "
            + "; ".join(str(note) for note in record.get("notes", [])))


def process_birth_identity(pid: int | None = None) -> str:
    """Return the canonical PID-reuse token shared with remote worker ownership.

    Linux exposes a kernel boot ID and process start tick in ``/proc``. Using
    that pair lets the SSH-side owner and GenericPackHost attest the same
    incarnation. Other platforms retain the existing ``ps lstart`` token.
    """
    process_id = int(pid if pid is not None else os.getpid())
    if process_id <= 0:
        return ""
    if sys.platform == "win32":
        identity = platform.process_identity(process_id)
        return str(identity["birth_id"]) if identity is not None else ""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/%d/stat" % process_id, encoding="ascii") as stream:
                stat_fields = stream.read().rsplit(")", 1)[1].split()
            if stat_fields[0] in {"Z", "X"}:
                return ""
            with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as stream:
                boot_id = stream.read().strip()
            start_ticks = stat_fields[19]
            if not boot_id or not start_ticks.isdigit():
                return ""
            return boot_id + ":" + start_ticks
        except (OSError, IndexError, UnicodeError):
            return ""
    info = _process_snapshot().get(process_id)
    return info.birth if info is not None else ""


def _zombie_birth_identity(pid: int) -> str:
    """Linux: the birth token of *pid* only while it is an exited, unreaped zombie.

    Returns ``""`` for a live, absent, unreadable or merely dying (``X``) pid.
    The token is built exactly like ``process_birth_identity`` so an equal token
    names the same incarnation; a reused pid carries a different start tick.
    """
    try:
        with open("/proc/%d/stat" % pid, encoding="ascii") as stream:
            stat_fields = stream.read().rsplit(")", 1)[1].split()
        if stat_fields[0] != "Z":
            return ""
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as stream:
            boot_id = stream.read().strip()
        start_ticks = stat_fields[19]
        if not boot_id or not start_ticks.isdigit():
            return ""
        return boot_id + ":" + start_ticks
    except (OSError, IndexError, UnicodeError):
        return ""


def held_child_birth_identity(pid: int) -> str:
    """Birth token of a child we hold unreaped: live, or an exited zombie.

    An unreaped child's pid cannot be reused, so a child that already exited
    still has one definite incarnation; its exit is a known outcome, not a
    custody violation.  Empty only when the pid is gone or unreadable.
    """
    if sys.platform.startswith("linux"):
        return process_birth_identity(pid) or _zombie_birth_identity(pid)
    return process_birth_identity(pid)


def launch_exit_witnessed(process: subprocess.Popen, birth: str) -> bool:
    """True once a census listed *process* as an unreaped zombie of incarnation *birth*.

    Evidence is monotone (a zombie never becomes live again, and its pid cannot
    be reused before it is reaped), so a witness from any earlier census still
    explains the launch's absence from the live set.  Absence without a witness
    proves nothing and stays uncertain.
    """
    return bool(birth) and getattr(process, "_astrid_census_exited", {}).get(process.pid) == birth


def popen_owned_process(argv: list[str], **kwargs: Any) -> subprocess.Popen:
    """Launch with retained custody while preserving the requested session."""
    options = dict(kwargs)
    new_session = bool(options.pop("start_new_session", False))
    detached = bool(options.pop("detached", False))
    delegate_inventory = options.pop("_delegate_inventory", True)
    if type(delegate_inventory) is not bool:
        raise CleanupUncertainError("inventory launch selection must be explicit")
    custody = ProcessCustody()
    context = {"environment": dict(os.environ if options.get("env") is None else options["env"]), "cwd": str(options.get("cwd") or os.getcwd())} if delegate_inventory else None
    wrapped, registration_env = custody.prepare_launch(argv, start_new_session=new_session, inventory_context=context)
    if registration_env:
        options["env"] = {**(os.environ if options.get("env") is None else options["env"]), **registration_env}
        options["start_new_session"] = False  # The Darwin custody wrapper creates it.
    else:
        options["start_new_session"] = new_session
    try:
        process = platform.spawn_tree(wrapped, detached=detached, **options)
    except BaseException:
        custody.cleanup_failed_launch(signum=platform.TERM)
        raise
    process._astrid_custody = custody
    if new_session:
        process._astrid_process_group_id = process.pid
    try:
        custody.bind_launch(process)
        process._astrid_process_birth = custody._handles[process.pid][0]
        if sys.platform == "win32":
            return process
        observe_tree(process)
        custody.start_observer()
        custody.start_inventory_owner()
        custody.release_launch()
    except BaseException as error:
        # Keep the initiating error and its retained obligation visible.
        error.process = process
        error.custody = custody
        try:
            custody.cleanup_failed_launch(signum=platform.TERM)
        except BaseException as cleanup_error:
            custody._uncertain = custody._uncertain or cleanup_error
            process._astrid_tree_uncertain = str(cleanup_error) or type(cleanup_error).__name__
            raise error from cleanup_error
        raise
    return process


def popen_owned_group(argv: list[str], **kwargs: Any) -> subprocess.Popen:
    """Launch a fresh session with individual incarnation-bound custody."""
    return popen_owned_process(argv, **{**kwargs, "start_new_session": True})


def _group_id(process: subprocess.Popen) -> int:
    return int(getattr(process, "_astrid_process_group_id", process.pid))


def _leader_birth(process: subprocess.Popen, snapshot: dict[int, _ProcessInfo]) -> str | None:
    birth = getattr(process, "_astrid_process_birth", None)
    if isinstance(birth, str):
        return birth
    info = snapshot.get(process.pid)
    if info is not None:
        birth = info.birth
        process._astrid_process_birth = birth  # type: ignore[attr-defined]
        return birth
    return None


def _snapshot_group(process: subprocess.Popen) -> dict[int, str]:
    """Capture current group members as ``pid -> birth token``."""
    group_id = _group_id(process)
    return {
        info.pid: info.birth
        for info in _process_snapshot().values()
        if info.pgid == group_id
    }


def _live_members(members: dict[int, str]) -> list[int]:
    """Return only PIDs whose birth token still matches a snapshot."""
    if not members:
        return []
    snapshot = _process_snapshot()
    return [
        pid
        for pid, birth in members.items()
        if (info := snapshot.get(pid)) is not None and info.birth == birth
    ]


def _group_members_owned(
    process: subprocess.Popen,
    known: dict[int, str],
    snapshot: dict[int, _ProcessInfo] | None = None,
) -> tuple[dict[int, str], bool]:
    """Refresh group members while rejecting a reused leader/group id."""
    snapshot = _process_snapshot() if snapshot is None else snapshot
    group_id = _group_id(process)
    birth = _leader_birth(process, snapshot)
    leader = snapshot.get(group_id)
    if leader is not None and birth is not None and leader.birth != birth:
        return {}, False
    if leader is not None and birth is None:
        if process.poll() is not None:
            return {}, False
        birth = leader.birth
        process._astrid_process_birth = birth  # type: ignore[attr-defined]
    members = {
        pid: info.birth
        for pid, info in snapshot.items()
        if info.pgid == group_id
    }
    known.update(members)
    return members, True


def group_exists(process: subprocess.Popen) -> bool:
    """Report direct-process liveness; group cleanup uses its own census."""
    return process.poll() is None


def signal_group(process: subprocess.Popen, sig: int) -> None:
    """Signal registered incarnations; a PGID is never signal authority."""
    if sys.platform == "win32":
        if getattr(process, "_runtime_job", None) is None:
            raise CleanupUncertainError("Windows signal lacks retained Job custody")
        platform.signal_tree(process, sig)
        return
    live = observe_tree(process)
    _signal_valid_tree_members(process, live, sig, strict=True)


def terminate_group(process: subprocess.Popen, *, grace_seconds: float = 1.0) -> None:
    """Clean the retained graph without signalling its numeric group."""
    terminate_tree(process, grace_seconds=grace_seconds)
    verify_tree_absent(process)


def _tree_members(
    process: subprocess.Popen,
    known: dict[int, str],
    snapshot: dict[int, _ProcessInfo] | None = None,
) -> dict[int, str]:
    """Refresh a process tree rooted at *process*, validating identities."""
    snapshot = _process_snapshot() if snapshot is None else snapshot
    root_birth = _leader_birth(process, snapshot)
    if root_birth is None:
        return {}
    root = snapshot.get(process.pid)
    if root is not None and root.birth != root_birth:
        return {}
    known[process.pid] = root_birth
    children: dict[int, list[_ProcessInfo]] = {}
    for info in snapshot.values():
        children.setdefault(info.ppid, []).append(info)
    pending = [process.pid, *known]
    while pending:
        parent = pending.pop()
        # A PID in ``known`` is only an identity, not a permanent process.
        # Revalidate that identity before following its children.  Without
        # this guard, a child that exits and whose PID is reused can make us
        # adopt and signal an unrelated process tree hanging off the reused
        # PID.  Missing parents are also a fail-closed boundary: retain
        # already-known descendants for cleanup, but never discover new ones.
        expected_birth = known.get(parent)
        if expected_birth is None:
            continue
        current = snapshot.get(parent)
        if current is None or current.birth != expected_birth:
            continue
        for info in children.get(parent, ()):
            prior = known.get(info.pid)
            if prior is not None and prior != info.birth:
                continue
            if prior is None:
                known[info.pid] = info.birth
            pending.append(info.pid)
    return {
        pid: birth
        for pid, birth in known.items()
        if (info := snapshot.get(pid)) is not None and info.birth == birth
    }


def _ordinary_command_custody(custody) -> bool:
    broker = getattr(custody, "_launch_broker", None)
    return (sys.platform == "darwin" and isinstance(custody, ProcessCustody)
            and isinstance(broker, RoleBoundCustodyBroker) and broker.role == "command")


def observe_tree(process: subprocess.Popen, *, deadline: float | None = None) -> dict[int, str]:
    """Remember birth-fenced ancestry while parents still exist, across sessions.

    An empty or failed census is never evidence of absence.
    Never adopt children of a missing or recycled previously observed parent.
    """
    if sys.platform == "win32":
        job = getattr(process, "_runtime_job", None)
        if job is None:
            raise CleanupUncertainError("Windows process lacks retained Job custody")
        return ({process.pid: process._astrid_process_birth}
                if process.poll() is None else {})
    custody = getattr(process, "_astrid_custody", None)
    ordinary = _ordinary_command_custody(custody)
    bounded = (isinstance(custody, ProcessCustody) and sys.platform == "darwin"
               and (ordinary or deadline is not None))
    if ordinary:
        deadline = _candidate_reconciliation_deadline(deadline)
    # Serialize census bookkeeping with registration and concurrent cleanup.
    try:
        guard = (custody._observation_guard(deadline) if bounded
                 else getattr(custody, "_lock", nullcontext()))
        with guard:
            uncertainty = getattr(process, "_astrid_tree_uncertain", None)
            if uncertainty is not None:
                raise CleanupUncertainError(uncertainty)
            return _observe_tree(process, deadline=deadline if bounded else None)
    except CustodyLockBusy:
        raise  # lock never taken: no observation, no uncertainty
    except BaseException as exc:
        if not getattr(process, "_astrid_tree_uncertain", None):
            process._astrid_tree_uncertain = str(exc) or type(exc).__name__
        raise


def _observe_tree(process: subprocess.Popen, *, deadline: float | None = None) -> dict[int, str]:
    snapshot = _process_snapshot()
    if not snapshot:
        raise CleanupUncertainError(_census_message("owned process census unavailable", snapshot))
    known = getattr(process, "_astrid_tree_members", None)
    if known is None:
        known = {}
        process._astrid_tree_members = known
    if not known and process.pid not in snapshot:
        # An unreaped leader that already exited is a known outcome when the
        # census lists exactly the incarnation custody bound at launch.
        bound = getattr(process, "_astrid_process_birth", None)
        if not (bound and getattr(snapshot, "exited", {}).get(process.pid) == bound):
            raise CleanupUncertainError("owned leader exited before ancestry capture")
    for pid, birth in tuple(known.items()):
        current = snapshot.get(pid)
        if current is not None and current.birth != birth:
            raise CleanupUncertainError("owned process identity was reused")
        if current is None and any(
            info.ppid == pid and info.pid not in known for info in snapshot.values()
        ):
            raise CleanupUncertainError("unverified child of missing owned parent")
    live = _tree_members(process, known, snapshot)
    if (witness := getattr(snapshot, "exited", {}).get(process.pid)) is not None:
        # Only the retained child itself; other zombies are no one's evidence.
        process._astrid_census_exited = {process.pid: witness}  # type: ignore[attr-defined]
    if process.pid not in known:
        raise CleanupUncertainError("owned leader identity unavailable")
    custody = getattr(process, "_astrid_custody", None)
    if custody is None:
        raise CleanupUncertainError("owned tree lacks retained cleanup custody")
    if isinstance(custody, ProcessCustody):
        live = custody.observe_census(
            live, census=_census_evidence(snapshot),
            fresh_census=lambda: _census_evidence(_process_snapshot()),
            **({"deadline": deadline} if deadline is not None else {}),
        )
        # Fresh reconciliation observations must survive subsequent reparenting.
        known.update({pid: birth for pid, (birth, _) in custody._handles.items()})
        if sys.platform == "darwin" and getattr(getattr(custody, "_launch_broker", None), "role", None) == "command":
            process._astrid_cleanup_semantics = {
                "coverage": "retained_observed_set_and_final_owned_group_checks",
                "complete_descendant_history": "UNPROVED",
                "unadmitted_evidence": str(custody.root / "observed-containment.json")
                    if custody._unadmitted_absent else None,
            }
    else:
        custody.observe(live)
    return live


def _census_evidence(snapshot):
    return {pid: {"pid": info.pid, "birth_id": info.birth, "parent_pid": info.ppid,
                  "pgid": info.pgid,
                  **{key: value for key in ("status", "start_sec", "start_usec", "uid")
                     if (value := getattr(info, key)) is not None}}
            for pid, info in snapshot.items()}


def verify_tree_absent(process: subprocess.Popen, *, deadline: float | None = None) -> None:
    """Fail closed unless tracked identities and a private owned group are gone."""
    try:
        _verify_tree_absent(process, deadline=deadline)
    except BaseException as exc:
        if not getattr(process, "_astrid_tree_uncertain", None):
            process._astrid_tree_uncertain = str(exc) or type(exc).__name__
        raise


def _verify_tree_absent(process: subprocess.Popen, *, deadline: float | None = None) -> None:
    if sys.platform == "win32":
        if process.poll() is None or not platform.tree_drained(process):
            raise CleanupUncertainError("Windows Job cleanup is unproved")
        return
    custody = getattr(process, "_astrid_custody", None)
    if isinstance(custody, ProcessCustody) and process.poll() is not None:
        custody.finish_inventory_owner()
    custody = getattr(process, "_astrid_custody", None)
    ordinary = _ordinary_command_custody(custody)
    bounded = (isinstance(custody, ProcessCustody) and sys.platform == "darwin"
               and (ordinary or deadline is not None))
    if ordinary:
        deadline = _candidate_reconciliation_deadline(deadline)
    guard = (custody._observation_guard(deadline) if bounded
             else getattr(custody, "_lock", nullcontext()))
    with guard:
        if observe_tree(process, **({"deadline": deadline} if bounded else {})):
            raise CleanupUncertainError("owned command descendants remain")
        if isinstance(custody, ProcessCustody) or hasattr(process, "_astrid_process_group_id"):
            snapshot = _process_snapshot()
            if not snapshot:
                raise CleanupUncertainError("owned group absence census unavailable")
            if isinstance(custody, ProcessCustody):
                custody.validate_final_census(_census_evidence(snapshot))
            if (hasattr(process, "_astrid_process_group_id")
                    and any(info.pgid == _group_id(process) for info in snapshot.values())):
                raise CleanupUncertainError("owned group members remain")
    # Tree and group are verified absent and the leader is reaped, so the retained
    # pidfds no longer guard anything; release them instead of holding them for
    # the life of the parent process.
    if isinstance(custody, ProcessCustody) and process.poll() is not None:
        custody.release_pidfds()


# Floor of the cleanup budget beyond the grace period. It caps the wait, so a healthy
# cleanup is no slower; on a loaded host a census holding the custody lock can take
# over a second, and a 1 s floor turned Ctrl+C into a CustodyError.
_CLEANUP_FLOOR_SECONDS = 5.0

def terminate_tree(process: subprocess.Popen, *, grace_seconds: float = 1.0) -> None:
    """Stop a birth/ancestry-fenced tree, descendants before the leader.

    Unlike group cleanup this never signals a containing/inherited group.
    Ownership is remembered during execution, not recovered by name/path after
    a parent exits. Success certifies observed identities absent.
    """
    deadline = time.monotonic() + max(0.0, grace_seconds) + max(_CLEANUP_FLOOR_SECONDS, grace_seconds)
    try:
        _terminate_tree(process, grace_seconds=grace_seconds, cleanup_deadline=deadline)
    except BaseException as exc:
        if not getattr(process, "_astrid_tree_uncertain", None):
            process._astrid_tree_uncertain = str(exc) or type(exc).__name__
        # The observation failure stays authoritative. This only tries the
        # capabilities retained before that failure, never a fresh PID census.
        contain_retained_handles(process, cleanup_deadline=deadline,
                                 first_error=process._astrid_tree_uncertain)
        raise


def contain_retained_handles(process: subprocess.Popen, *, cleanup_deadline: float,
                             first_error: str) -> None:
    """Best effort on retained capabilities; never certifies tree cleanup."""
    custody = getattr(process, "_astrid_custody", None)
    report: dict[str, Any] = {"status": "UNCERTAIN_RETAINED_HANDLES_ONLY", "first_error": first_error,
              "complete_descendant_history": "UNPROVED", "signals": [], "errors": [],
              "original_wait": None, "deadline": cleanup_deadline}
    process._astrid_retained_containment = report
    if not isinstance(custody, ProcessCustody):
        report["errors"].append({"stage": "retained-snapshot", "error": "no retained custody"})
        return
    frozen = ()
    remaining = max(0.0, cleanup_deadline - time.monotonic())
    if remaining > 0 and custody._lock.acquire(timeout=remaining):
        try:
            frozen = tuple((pid, entry[0]) for pid, entry in custody._handles.items())
        finally:
            custody._lock.release()
    else:
        report["errors"].append({"stage": "retained-snapshot", "error": "deadline expired"})
    members = tuple(row for row in reversed(frozen) if row[0] != process.pid)
    members += tuple(row for row in frozen if row[0] == process.pid)
    report["retained_members"] = [{"pid": pid, "birth": birth} for pid, birth in members]
    for signum in (platform.TERM, platform.KILL):
        for pid, birth in members:
            if time.monotonic() >= cleanup_deadline:
                report["errors"].append({"stage": "signal", "pid": pid, "signal": int(signum), "error": "deadline expired"})
                continue
            try:
                custody.signal(pid, birth, signum, cleanup_deadline=cleanup_deadline)
                report["signals"].append({"pid": pid, "signal": int(signum)})
            except BaseException as exc:
                report["errors"].append({"stage": "signal", "pid": pid, "signal": int(signum),
                                         "error_type": type(exc).__name__, "error": str(exc)[:600]})
        if signum == platform.TERM:
            remaining = max(0.0, cleanup_deadline - time.monotonic())
            try:
                report["original_wait"] = process.wait(timeout=remaining / 2)
            except BaseException as exc:
                report["errors"].append({"stage": "original-term-wait", "error_type": type(exc).__name__})
    try:
        report["original_wait"] = process.wait(timeout=max(0.0, cleanup_deadline - time.monotonic()))
    except BaseException as exc:
        report["errors"].append({"stage": "original-final-wait", "error_type": type(exc).__name__})
    try:
        custody.release_pidfds(only_exited=True)  # Exited incarnations need no capability.
    except BaseException as exc:  # noqa: BLE001 - like every stage here, containment must still write its report
        report["errors"].append({"stage": "release-exited-pidfds", "error_type": type(exc).__name__})
    report["deadline_expired"] = time.monotonic() > cleanup_deadline
    try:
        _atomic_owner_json(custody.root / "retained-cleanup-containment.json", report)
    except BaseException as exc:
        report["errors"].append({"stage": "evidence-persistence", "error_type": type(exc).__name__})


def _terminate_tree(process: subprocess.Popen, *, grace_seconds: float,
                    cleanup_deadline: float | None = None) -> None:
    if sys.platform == "win32":
        if getattr(process, "_runtime_job", None) is None:
            raise CleanupUncertainError("Windows process lacks retained Job custody")
        platform.stop_tree(process, grace=max(0.0, grace_seconds))
        return
    if cleanup_deadline is None:
        cleanup_deadline = time.monotonic() + max(0.0, grace_seconds) + max(_CLEANUP_FLOOR_SECONDS, grace_seconds)
    custody = getattr(process, "_astrid_custody", None)
    observation_options = ({"deadline": cleanup_deadline}
                           if isinstance(custody, ProcessCustody) and sys.platform == "darwin" else {})
    if isinstance(custody, ProcessCustody):
        custody.cancel_inventory_owner()
    live = observe_tree(process, **observation_options)
    known = process._astrid_tree_members
    descendants = {pid: birth for pid, birth in live.items() if pid != process.pid}
    _signal_valid_tree_members(process, descendants, platform.TERM, strict=True,
                              **({"cleanup_deadline": cleanup_deadline} if observation_options else {}))
    deadline = min(cleanup_deadline, time.monotonic() + max(0.0, grace_seconds))
    while time.monotonic() < deadline:
        live = observe_tree(process, **observation_options)
        if not any(pid != process.pid for pid in live):
            break
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
    # Keep the root alive through the descendant census and escalation. Late
    # same-group members remain covered by the caller's owned-group cleanup.
    live = observe_tree(process, **observation_options)
    _signal_valid_tree_members(
        process, {pid: birth for pid, birth in live.items() if pid != process.pid},
        platform.KILL, strict=True,
        **({"cleanup_deadline": cleanup_deadline} if observation_options else {}),
    )
    # kill_deadline times the root's escalation (how long SIGTERM gets before SIGKILL);
    # signalling and reaping get the whole cleanup budget, because each signal waits for
    # the custody lock, which a census on a busy host can hold for over a second.
    kill_deadline = min(cleanup_deadline, time.monotonic() + max(1.0, grace_seconds))
    _signal_valid_tree_members(process, {process.pid: known[process.pid]}, platform.TERM,
                              strict=True, cleanup_deadline=cleanup_deadline)
    # Wait only on the original child, reserving half the root budget for KILL.
    root_grace = min(max(0.0, grace_seconds), max(0.0, kill_deadline - time.monotonic()) / 2)
    if process.poll() is None and root_grace > 0:
        try:
            process.wait(timeout=root_grace)
        except subprocess.TimeoutExpired:
            pass
    while time.monotonic() < cleanup_deadline:
        process.poll()  # reap our direct child before its absence check
        live = observe_tree(process, **observation_options)
        if not live:
            break
        _signal_valid_tree_members(process, live, platform.KILL,
                                  strict=True, cleanup_deadline=cleanup_deadline)
        time.sleep(0.02)
    try:
        process.wait(timeout=0)
    except subprocess.TimeoutExpired:
        raise CleanupUncertainError("owned leader survived bounded termination")
    if observe_tree(process, **observation_options):
        raise CleanupUncertainError("owned descendants survived bounded termination")
    if isinstance(getattr(process, "_astrid_custody", None), ProcessCustody):
        verify_tree_absent(process, **observation_options)


def _signal_valid_tree_members(
    process: subprocess.Popen,
    members: dict[int, str],
    sig: int,
    snapshot: dict[int, _ProcessInfo] | None = None,
    *, strict: bool = False, cleanup_deadline: float | None = None,
) -> None:
    snapshot = _process_snapshot() if snapshot is None else snapshot
    if strict and not snapshot:
        raise CleanupUncertainError(_census_message("owned signal census unavailable", snapshot))
    for pid, birth in reversed(tuple(members.items())):
        info = snapshot.get(pid)
        if info is None:
            continue
        if info.birth != birth:
            if strict:
                raise CleanupUncertainError("owned signal identity was reused")
            continue
        try:
            custody = getattr(process, "_astrid_custody", None)
            if custody is None:
                raise CleanupUncertainError("owned signal lacks retained cleanup custody")
            if isinstance(custody, ProcessCustody) and cleanup_deadline is not None:
                custody.signal(pid, birth, sig, cleanup_deadline=cleanup_deadline)
            else:
                custody.signal(pid, birth, sig)
        except ProcessLookupError:
            pass
        except (OSError, CleanupUncertainError) as exc:
            # GenericHost exposes this message as the task error. Preserve the
            # first signal callee/result without leaking capability bytes or
            # substituting a later host-stop failure for the original cause.
            result = getattr(exc, "result", None)
            observed_errno = getattr(exc, "observed_errno", getattr(exc, "errno", None))
            raise CleanupUncertainError(
                "owned process signal failed "
                f"(stage=custody-signal, candidate_pid={pid}, root_pid={process.pid}, "
                f"observer_pid={os.getpid()}, signal={int(sig)}, error_type={type(exc).__name__}, "
                f"kernel_result={result if type(result) is int else 'unknown'}, "
                f"errno={observed_errno if type(observed_errno) is int else 'unknown'}): "
                + str(exc)[:600]
            ) from exc


def release_group(process: subprocess.Popen) -> None:
    """Release a completed group, including descendants after leader exit."""
    terminate_group(process, grace_seconds=0.2)
