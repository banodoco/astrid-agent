"""Process-only helpers for cancellation of owned subprocesses."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import nullcontext
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


from astrid.core.execution.custody_broker import (
    CustodyError as CleanupUncertainError, CustodyRootBinding, ProcessCustody, RetainedCustodyPopen,
    _darwin_process_records, _ps_lstart_birth,
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
    """A process census.  When it is empty, ``failure`` says why ps gave none."""

    failure: str | None = None


def _failed_census(reason: str) -> _Census:
    census = _Census()
    census.failure = reason
    return census


def _census_message(message: str, census: Mapping[int, _ProcessInfo]) -> str:
    reason = getattr(census, "failure", None)
    return f"{message}: {reason}" if reason else message


def _process_snapshot() -> dict[int, _ProcessInfo]:
    """Read a process census without invoking a shell."""
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
    return entries


CLEANUP_LATCH_NAME = "generic-host.cleanup-uncertain.json"


def write_cleanup_latch(path: Path, *, reason: str, since: str, recover_with: str) -> None:
    """Record a host's cleanup latch for ``astrid doctor``.  Never raises.

    The in-process latch is what blocks admissions.  This file only makes the
    block observable to another process, so a failure to write it is logged by
    the caller and does not change the block.
    """
    try:
        pid = os.getpid()
        info = _process_snapshot().get(pid)
        record = {
            "pid": pid,
            "process_birth_id": info.birth if info is not None else None,
            "since": since,
            "reason": reason,
            "recover_with": recover_with,
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
    """One doctor line for a blocked host; the recovery command is part of it."""
    return (
        f"pack host blocked by cleanup uncertainty since {record.get('since')}; "
        f"cause {record.get('reason')}; recover with {record.get('recover_with')}"
    )


def process_birth_identity(pid: int | None = None) -> str:
    """Return the canonical PID-reuse token shared with remote worker ownership.

    Linux exposes a kernel boot ID and process start tick in ``/proc``. Using
    that pair lets the SSH-side owner and GenericPackHost attest the same
    incarnation. Darwin renders native start seconds as a fixed English UTC
    ``ps-lstart`` token. Other platforms retain the existing ``ps lstart`` token.
    """
    process_id = int(pid if pid is not None else os.getpid())
    if process_id <= 0:
        return ""
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


def popen_owned_process(argv: list[str], *, custody_root: CustodyRootBinding | None = None, _child_bridge_launch=None, **kwargs: Any) -> subprocess.Popen:
    """Launch with retained custody while preserving the requested session."""
    options = dict(kwargs)
    new_session = bool(options.pop("start_new_session", False))
    delegate_inventory = options.pop("_delegate_inventory", True)
    if type(delegate_inventory) is not bool:
        raise CleanupUncertainError("inventory launch selection must be explicit")
    if _child_bridge_launch is not None:
        from ._child_pipe import _ChildBridgeLaunchBinding
        if type(_child_bridge_launch) is not _ChildBridgeLaunchBinding:
            raise CleanupUncertainError('child bridge requires its private launch binding')
        try:
            if options.get('pass_fds'):
                raise CleanupUncertainError('child bridge cannot admit caller descriptors')
            _child_bridge_launch.verify_launch(argv, options, new_session=new_session)
        except BaseException:
            _child_bridge_launch.bridge.revoke()
            raise
        # The application pair never shares the command-inventory channel.
        delegate_inventory = False
    if sys.platform == 'darwin' and options.get('pass_fds'):
        raise CleanupUncertainError('unmatched inherited channels require an admitted pipe consumer')
    custody = ProcessCustody(custody_root=custody_root) if custody_root is not None else ProcessCustody()
    context = {"environment": dict(os.environ if options.get("env") is None else options["env"]), "cwd": str(options.get("cwd") or os.getcwd())} if delegate_inventory else None
    try:
        if _child_bridge_launch is None:
            wrapped, registration_env = custody.prepare_launch(argv, start_new_session=new_session, inventory_context=context)
        else:
            wrapped, registration_env = custody.prepare_launch(argv, start_new_session=new_session,
                inventory_context=context, child_bridge_launch=_child_bridge_launch)
    except BaseException:
        if _child_bridge_launch is not None:
            _child_bridge_launch.bridge.revoke()
        raise
    if registration_env:
        options["env"] = {**(os.environ if options.get("env") is None else options["env"]), **registration_env}
        # Only the host broker creates these channels. Caller-supplied FD lists
        # cannot select the bootstrap's matched operation pipes.
        broker_fds = custody._launch_broker.child_pipe_fds
        if _child_bridge_launch is not None:
            broker_fds += _child_bridge_launch.descriptors
        options['pass_fds'] = tuple(options.get('pass_fds', ())) + broker_fds
        options['close_fds'] = True
    else:
        options["start_new_session"] = new_session
    custody.begin_launch_spawn()
    try:
        process = (RetainedCustodyPopen if sys.platform == 'darwin' else subprocess.Popen)(wrapped, **options)
    except BaseException:
        if _child_bridge_launch is not None:
            _child_bridge_launch.bridge.revoke()
        custody.cleanup_failed_launch(signum=signal.SIGTERM)
        raise
    process._astrid_custody = custody
    if new_session:
        process._astrid_process_group_id = process.pid
    try:
        custody.bind_launch(process)
        process._astrid_process_birth = custody._handles[process.pid][0]
        observe_tree(process)
        custody.start_observer()
        custody.start_inventory_owner()
        custody.release_launch()
    except BaseException as error:
        if _child_bridge_launch is not None:
            _child_bridge_launch.bridge.revoke()
        # Keep the initiating error and its retained obligation visible.
        error.process = process
        error.custody = custody
        try:
            custody.cleanup_failed_launch(signum=signal.SIGTERM)
        except BaseException as cleanup_error:
            custody._uncertain = custody._uncertain or cleanup_error
            process._astrid_tree_uncertain = str(cleanup_error) or type(cleanup_error).__name__
            raise error from cleanup_error
        raise
    return process


def popen_owned_group(argv: list[str], *, custody_root: CustodyRootBinding | None = None, **kwargs: Any) -> subprocess.Popen:
    """Launch a fresh session with individual incarnation-bound custody."""
    return popen_owned_process(argv, custody_root=custody_root, **{**kwargs, "start_new_session": True})


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


def observe_tree(process: subprocess.Popen) -> dict[int, str]:
    """Remember birth-fenced ancestry while parents still exist, across sessions.

    An empty or failed census is never evidence of absence.
    Never adopt children of a missing or recycled previously observed parent.
    """
    custody = getattr(process, "_astrid_custody", None)
    # Serialize census bookkeeping with registration and concurrent cleanup.
    with getattr(custody, "_lock", nullcontext()):
        uncertainty = getattr(process, "_astrid_tree_uncertain", None)
        if uncertainty is not None:
            raise CleanupUncertainError(uncertainty)
        try:
            return _observe_tree(process)
        except BaseException as exc:
            # A failed census or an unresolved admitted obligation remains sticky.
            process._astrid_tree_uncertain = str(exc) or type(exc).__name__
            raise


def _observe_tree(process: subprocess.Popen) -> dict[int, str]:
    snapshot = _process_snapshot()
    if not snapshot:
        raise CleanupUncertainError(_census_message("owned process census unavailable", snapshot))
    known = getattr(process, "_astrid_tree_members", None)
    if known is None:
        known = {}
        process._astrid_tree_members = known
    if not known and process.pid not in snapshot:
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
    if process.pid not in known:
        raise CleanupUncertainError("owned leader identity unavailable")
    custody = getattr(process, "_astrid_custody", None)
    if custody is None:
        raise CleanupUncertainError("owned tree lacks retained cleanup custody")
    if isinstance(custody, ProcessCustody):
        live = custody.observe_census(
            live, census=_census_evidence(snapshot),
            fresh_census=lambda: _census_evidence(_process_snapshot()),
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


def verify_tree_absent(process: subprocess.Popen) -> None:
    """Fail closed unless tracked identities and a private owned group are gone."""
    try:
        _verify_tree_absent(process)
    except BaseException as exc:
        process._astrid_tree_uncertain = str(exc) or type(exc).__name__
        raise


def _verify_tree_absent(process: subprocess.Popen) -> None:
    custody = getattr(process, "_astrid_custody", None)
    if isinstance(custody, ProcessCustody) and process.poll() is not None:
        custody.finish_inventory_owner()
    custody = getattr(process, "_astrid_custody", None)
    with getattr(custody, "_lock", nullcontext()):
        if observe_tree(process):
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
        if isinstance(custody, ProcessCustody):
            # The original Popen wait is positive child evidence. Final census
            # remains a separate obligation and never creates this settlement.
            custody.settle_launch()


def terminate_tree(process: subprocess.Popen, *, grace_seconds: float = 1.0) -> None:
    """Stop a birth/ancestry-fenced tree, descendants before the leader.

    Unlike group cleanup this never signals a containing/inherited group.
    Ownership is remembered during execution, not recovered by name/path after
    a parent exits. Success certifies observed identities absent.
    """
    try:
        _terminate_tree(process, grace_seconds=grace_seconds)
    except BaseException as exc:
        process._astrid_tree_uncertain = str(exc) or type(exc).__name__
        raise


def _terminate_tree(process: subprocess.Popen, *, grace_seconds: float) -> None:
    custody = getattr(process, "_astrid_custody", None)
    if isinstance(custody, ProcessCustody):
        custody.cancel_inventory_owner()
    live = observe_tree(process)
    known = process._astrid_tree_members
    descendants = {pid: birth for pid, birth in live.items() if pid != process.pid}
    _signal_valid_tree_members(process, descendants, signal.SIGTERM, strict=True)
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        live = observe_tree(process)
        if not any(pid != process.pid for pid in live):
            break
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
    # Keep the root alive through the descendant census and escalation. Late
    # same-group members remain covered by the caller's owned-group cleanup.
    live = observe_tree(process)
    _signal_valid_tree_members(
        process, {pid: birth for pid, birth in live.items() if pid != process.pid},
        signal.SIGKILL, strict=True,
    )
    kill_deadline = time.monotonic() + max(1.0, grace_seconds)
    _signal_valid_tree_members(process, {process.pid: known[process.pid]}, signal.SIGTERM,
                              strict=True, cleanup_deadline=kill_deadline)
    # Wait only on the original child, reserving half the root budget for KILL.
    root_grace = min(max(0.0, grace_seconds), max(0.0, kill_deadline - time.monotonic()) / 2)
    if process.poll() is None and root_grace > 0:
        try:
            process.wait(timeout=root_grace)
        except subprocess.TimeoutExpired:
            pass
    while time.monotonic() < kill_deadline:
        process.poll()  # reap our direct child before its absence check
        live = observe_tree(process)
        if not live:
            break
        _signal_valid_tree_members(process, live, signal.SIGKILL,
                                  strict=True, cleanup_deadline=kill_deadline)
        time.sleep(0.02)
    try:
        process.wait(timeout=0)
    except subprocess.TimeoutExpired:
        raise CleanupUncertainError("owned leader survived bounded termination")
    if observe_tree(process):
        raise CleanupUncertainError("owned descendants survived bounded termination")
    if isinstance(getattr(process, "_astrid_custody", None), ProcessCustody):
        verify_tree_absent(process)


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
