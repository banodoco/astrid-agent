"""Task scopes: native containment of an ordinary command.

The command leader starts a new session, so its process group is its own.
Nothing observes the tree while the command runs.  Cleanup signals the group
only (``killpg(TERM)``, grace, ``killpg(KILL)``) and then confirms with
censuses that nothing remains, reporting ``settled`` or ``unresolved`` with the
survivors named.  A census is evidence, never signal authority: a process found
only by census (e.g. one that left the group with ``setsid``) is reported, not
signalled.

Runtime retains a Windows Job Object for the whole scope. POSIX cleanup uses
the unreaped group where available; after a caller reaps its leader, recovery
binds individual native identities after reconciling the original birth.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from runtime_protocol import platform
from runtime_protocol.dirfd import filesystem as os

from .platform_helpers import process_control_report, signal_observed_group

_DARWIN_ZOMBIE = 5


class TaskScopeUnresolved(RuntimeError):
    """Processes remained after KILL; fails the task, never the host."""

    def __init__(self, report: "ScopeReport"):
        self.report = report
        super().__init__(report.describe())


@dataclass(frozen=True)
class Survivor:
    pid: int
    command: str


@dataclass(frozen=True)
class ScopeReport:
    status: str  # "settled" | "unresolved"
    pgid: int
    survivors: tuple[Survivor, ...] = ()
    detail: str = ""

    def describe(self) -> str:
        names = ", ".join(f"{s.pid} ({s.command})" for s in self.survivors)
        return (f"cleanup unresolved for process group {self.pgid}"
                + (f": still running after KILL: {names}" if names else "")
                + (f" [{self.detail}]" if self.detail else ""))


# Crash-orphan journal: one owner-only file in the host's support root listing
# each live scope as {pid, start, pgid, task}.  An entry is removed when the
# scope settles; the next host start kills whatever a dead host left behind.
_journal: Path | None = None
_journal_lock = threading.Lock()
_owners: weakref.WeakValueDictionary[int, subprocess.Popen] = weakref.WeakValueDictionary()


def set_orphan_journal(path: Path | str | None) -> None:
    global _journal
    _journal = Path(path) if path is not None else None


def _journal_edit(path: Path, edit) -> None:
    """Read-modify-write the journal under a lock; never raises."""
    try:
        with _journal_lock:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "r+") as stream:
                platform.owner_only(stream.fileno())
                platform.lock_file(stream.fileno())
                try:
                    entries = json.loads(stream.read() or "[]")
                    entries = entries if isinstance(entries, list) else []
                except ValueError:
                    entries = []
                entries = edit(entries)
                stream.seek(0)
                stream.truncate()
                stream.write(json.dumps(entries))
                stream.flush()
    except OSError:
        pass


def _birth(pid: int) -> str | None:
    from astrid.core.execution import process_group as pg
    try:
        if sys.platform == "win32":
            return pg.process_birth_identity(pid) or None
        info = pg._process_snapshot().get(pid)
    except Exception:
        return None
    return info.birth if info is not None else None


def reap_orphans(path: Path | str) -> list[str]:
    """Kill groups a crashed host left running, then clear the journal.

    A group is signalled only when its recorded leader still exists with the
    recorded start time, or the leader is gone and the group still has members
    (a live group keeps its pgid from being reused).  A pid now owned by an
    unrelated process is never signalled.  Returns notes for what was killed
    or could not be.
    """
    path = Path(path)
    try:
        entries = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    notes: list[str] = []
    remaining = []
    if sys.platform == "win32":
        # The dead host closed its last Job handles. A persisted PID is never
        # authority to acquire or terminate a replacement Windows process.
        for entry in entries if isinstance(entries, list) else []:
            try:
                pid, birth = int(entry["pid"]), entry.get("start")
                deadline = time.monotonic() + 3.0
                while True:
                    identity = platform.process_identity(pid)
                    if identity is None or (birth and identity["birth_id"] != birth):
                        break
                    if not birth:
                        remaining.append(entry)
                        notes.append(f"orphan scope {pid} unchecked: birth unavailable")
                        break
                    if time.monotonic() >= deadline:
                        remaining.append(entry)
                        notes.append(f"orphan scope {pid} still live after Job owner exit")
                        break
                    time.sleep(0.05)
            except (TypeError, ValueError, KeyError, OSError) as exc:
                remaining.append(entry)
                notes.append(f"orphan scope entry {entry!r} unchecked: {exc}")
        _journal_edit(path, lambda entries: remaining)
        return notes
    from astrid.core.execution import process_group as pg
    for entry in entries if isinstance(entries, list) else []:
        try:
            pid, pgid, start = int(entry["pid"]), int(entry["pgid"]), entry.get("start")
            census = pg._process_snapshot()
            if not census:
                remaining.append(entry)
                notes.append(f"orphan scope {pgid} unchecked: census unavailable")
                continue
            leader = census.get(pid)
            if leader is not None and leader.status != _DARWIN_ZOMBIE and leader.birth != start:
                continue  # the pid belongs to an unrelated process now
            members = [p for p, i in census.items() if i.pgid == pgid and i.status != _DARWIN_ZOMBIE]
            if not members:
                continue
            for sig, wait in ((platform.TERM, 2.0), (platform.KILL, 3.0)):
                signal_observed_group(pgid, sig, leader_birth=start)
                deadline = time.monotonic() + wait
                while time.monotonic() < deadline:
                    members = [p for p, i in pg._process_snapshot().items()
                               if i.pgid == pgid and i.status != _DARWIN_ZOMBIE]
                    if not members:
                        break
                    time.sleep(0.05)
                if not members:
                    break
            notes.append(f"killed orphan task scope {pgid} ({entry.get('task')}) left by a crashed host"
                         if not members else
                         f"orphan task scope {pgid} ({entry.get('task')}) still running after KILL: {members}")
            if members:
                remaining.append(entry)
        except Exception as exc:
            remaining.append(entry)
            notes.append(f"orphan scope entry {entry!r} unchecked: {exc}")
    _journal_edit(path, lambda entries: remaining)
    return notes


def spawn(argv: Sequence[str], **popen_kwargs: Any) -> subprocess.Popen:
    """Start ``argv`` as the leader of a new session and group."""
    process = platform.spawn_tree(list(argv), **{**popen_kwargs, "start_new_session": True})
    setattr(process, "_astrid_task_scope", True)
    birth = _birth(process.pid)
    setattr(process, "_astrid_scope_birth", birth)
    _owners[process.pid] = process
    if _journal is not None:
        entry = {"pid": process.pid, "pgid": process.pid, "start": birth,
                 "task": str(popen_kwargs.get("cwd") or "")}
        _journal_edit(_journal, lambda entries: [*entries, entry])
    return process


def _command_of(pid: int) -> str:
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=2, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out[:200] or "unknown"


def _scope_census(pgid: int, leader: int) -> dict[int, str]:
    """``pid -> birth`` of live processes in the group or descending from it."""
    from astrid.core.execution import process_group as pg
    census = pg._process_snapshot()
    if not census:
        raise RuntimeError(pg._census_message("process census unavailable", census))
    live = {pid: info for pid, info in census.items()
            if pid != leader and info.status != _DARWIN_ZOMBIE}
    found = {pid for pid, info in live.items() if info.pgid == pgid}
    grew = True
    while grew:
        grew = False
        for pid, info in live.items():
            if pid not in found and info.ppid in (found | {leader}):
                found.add(pid)
                grew = True
    return {pid: live[pid].birth for pid in found}


def _alive(pgid: int, known: dict[int, str]) -> dict[int, str]:
    """Group members and descendants now, plus earlier-seen ones still alive."""
    now = _scope_census(pgid, pgid)
    from astrid.core.execution import process_group as pg
    census = pg._process_snapshot()
    kept = {pid: birth for pid, birth in known.items()
            if (info := census.get(pid)) is not None and info.birth == birth
            and info.status != _DARWIN_ZOMBIE}
    return {**kept, **now}


def _killpg(pgid: int, sig: int) -> str:
    try:
        process = _owners.get(pgid)
        if process is None:
            return "process scope lacks its retained owner"
        try:
            platform.signal_tree(process, sig)
        except ProcessLookupError:
            # Some command callers already reaped the leader. Reconcile the
            # recorded birth before Runtime binds each remaining member.
            signal_observed_group(pgid, sig, leader_birth=getattr(process, "_astrid_scope_birth"))
    except ProcessLookupError:
        return ""
    except PermissionError as exc:  # a zombie-only group can answer EPERM on macOS
        return f"killpg({int(sig)}): {exc}{_signal_denied_cause()}"
    except (OSError, ValueError) as exc:
        return f"scope signal({int(sig)}): {exc}"
    return ""


def _signal_denied_cause() -> str:
    """Name the real cause when this platform cannot signal process groups at all."""
    if not sys.platform.startswith("linux"):
        return ""
    try:
        probe = process_control_report()
    except Exception:  # noqa: BLE001 - the report must still be written
        return ""
    return f" ({probe['reason']})" if not probe.get("ok") and probe.get("reason") else ""


def stop(process: subprocess.Popen, grace: float = 2.0, kill_wait: float = 3.0) -> ScopeReport:
    """TERM the group, wait ``grace``, KILL it, reap the leader, confirm absence."""
    report = _stop(process, grace, kill_wait)
    if _journal is not None and report.status == "settled":
        _journal_edit(_journal, lambda entries: [e for e in entries if e.get("pgid") != report.pgid])
    return report


def _stop(process: subprocess.Popen, grace: float, kill_wait: float) -> ScopeReport:
    pgid = process.pid
    if sys.platform == "win32":
        try:
            if getattr(process, "_runtime_job", None) is None:
                raise RuntimeError("task scope lacks retained Job custody")
            platform.stop_tree(process, grace=max(grace, 0.0))
            return ScopeReport("settled", pgid)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return ScopeReport("unresolved", pgid, detail=str(exc))
    notes: list[str] = []
    try:
        members = _scope_census(pgid, pgid)  # evidence for naming survivors
    except Exception as exc:
        members, _ = {}, notes.append(f"pre-census failed: {exc}")
    # A group member is signalled only through the anchored group.
    notes.append(_killpg(pgid, platform.TERM))
    escalated = False
    deadline = time.monotonic() + max(grace, 0.0)
    alive: dict[int, str] = dict(members)
    problem = ""
    while True:
        leader_gone = process.poll() is not None  # a live leader may still fork in a TERM handler
        try:
            alive, problem = _alive(pgid, members), ""
        except Exception as exc:
            problem = f"census failed: {exc}"
        if (leader_gone and not alive and not problem) or time.monotonic() >= deadline:
            if (alive or problem or not leader_gone) and not escalated:
                notes.append(_killpg(pgid, platform.KILL))
                escalated = True
                deadline = time.monotonic() + kill_wait
            else:
                break
        time.sleep(0.02)
    # Reap the leader last (it may already have been reaped by poll()).
    try:
        process.wait(timeout=kill_wait)
    except subprocess.TimeoutExpired:
        try:
            platform.signal_tree(process, platform.KILL)
            process.wait(timeout=kill_wait)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        except PermissionError as exc:
            notes.append(f"final killpg({int(platform.KILL)}): {exc}{_signal_denied_cause()}")
    detail = "; ".join(n for n in (*notes, problem) if n)
    if alive or problem:
        return ScopeReport("unresolved", pgid,
                           tuple(Survivor(pid, _command_of(pid)) for pid in alive), detail)
    return ScopeReport("settled", pgid)
