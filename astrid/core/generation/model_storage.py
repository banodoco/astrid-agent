"""Read-only capacity admission for managed model fetches.

MooseFS reports cluster capacity through statvfs, independently of subtree
quotas. Its native quota query supplies the enforced logical-byte headroom.
Unavailable quota observations fall back to statvfs; unavailable capacity never blocks preparation.
"""
from __future__ import annotations

import heapq
import os
import selectors
import signal
import re
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .engine_contracts import IntegrityError, UnsafePathError

STORAGE_WARNING = "storage capacity could not be measured; proceeding"
_NETWORK_FILESYSTEMS = frozenset({"nfs", "nfs4", "cifs", "smb3", "ceph", "lustre", "9p"})


def storage_refusal_message(needed: int, free: int, task: str = "<task>") -> str:
    return (f"Model download refused (reason=insufficient_storage): {needed} bytes needed, "
            f"{free} bytes free; use a larger volume; "
            f"see `astrid tasks logs {task}` (storage.inventory).")


class ModelStorageInsufficient(IntegrityError):
    """Only fixed labels and numeric capacity facts cross the remote boundary."""

    code = "insufficient_storage"

    def __init__(self, needed: int, free: int, task: str = "<task>"):
        super().__init__(storage_refusal_message(needed, free, task), step_id="fetch")
        self.details = {"needed_bytes": needed, "free_bytes": free}


@dataclass(frozen=True)
class StorageCapacity:
    """A measured budget and precisely the destination directories it covers."""

    scope: tuple[str, int | Path]
    parents: frozenset[Path]
    free_bytes: int


@dataclass(frozen=True)
class _QuotaRow:
    current: int
    soft: int | None
    hard: int | None


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def _mount(path: Path, content: str) -> tuple[Path, str, str]:
    matches = []
    for line in content.splitlines():
        left, separator, right = line.partition(" - ")
        fields, filesystem = left.split(), right.split()
        if not separator or len(fields) < 6 or len(filesystem) < 3:
            raise ValueError("invalid mount evidence")
        mount = Path(_unescape(fields[4]))
        if path == mount or mount in path.parents:
            matches.append((mount, filesystem[0], _unescape(filesystem[1])))
    if not matches:
        raise ValueError("missing mount evidence")
    deepest = max(len(item[0].parts) for item in matches)
    selected = [item for item in matches if len(item[0].parts) == deepest]
    if len(selected) != 1:
        raise ValueError("ambiguous mount evidence")
    return selected[0]


def _quota_rows(text: str) -> dict[str, _QuotaRow]:
    """Parse mfsgetquota -n's exact-number table; format drift is not unlimited."""
    rows = {}
    for line in text.splitlines():
        fields = [value.strip() for value in line.split("|")]
        if fields[0] not in {"inodes", "length", "size", "realsize"}:
            continue
        if (len(fields) != 7 or fields[-1] or fields[0] in rows
                or not fields[1].isascii() or not fields[1].isdigit()
                or any(value != "-" and (not value.isascii() or not value.isdigit())
                       for value in (fields[2], fields[4]))):
            raise ValueError("invalid quota evidence")
        rows[fields[0]] = _QuotaRow(int(fields[1]),
                                  int(fields[2]) if fields[2] != "-" else None,
                                  int(fields[4]) if fields[4] != "-" else None)
    if set(rows) != {"inodes", "length", "size", "realsize"}:
        raise ValueError("incomplete quota evidence")
    return rows


def _quota(path: Path, *, deadline: float) -> dict[str, _QuotaRow]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("quota deadline exhausted")
    result = subprocess.run(["mfsgetquota", "-n", str(path)], capture_output=True, text=True,
                            check=False, timeout=min(10, remaining),
                            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C"})
    if result.returncode != 0 or len(result.stdout) > 65536:
        raise ValueError("quota query failed")
    return _quota_rows(result.stdout)


def available_model_bytes(parents: Iterable[Path], *, deadline: float,
                          check: Callable[[], None]) -> tuple[tuple[StorageCapacity, ...], bool]:
    """Return scoped measurements and whether proceeding needs a warning.

    A filesystem budget covers only destinations on that device; a hard-byte
    quota covers destinations beneath that quota on its mounted filesystem.
    Soft quotas never refuse, irrespective of grace. Unavailable observations
    are unknown, not zero; other measured budgets remain independently usable.
    """
    try:
        content = Path("/proc/self/mountinfo").read_text()
    except (OSError, ValueError):
        content = None
    requested = set(parents)
    filesystem_targets: dict[int, set[Path]] = {}
    filesystem_free: dict[int, list[int]] = {}
    scopes: dict[Path, set[Path]] = {}
    quota_unknown = content is None
    for parent in sorted(requested):
        check()
        try:
            probe = parent
            while not probe.exists():
                probe = probe.parent
            if not probe.is_dir() or any(path.is_symlink() for path in (probe, *probe.parents)):
                raise UnsafePathError("unsafe target path")
            device = probe.stat().st_dev
        except OSError:
            quota_unknown = True
            continue
        filesystem_targets.setdefault(device, set()).add(parent)
        try:
            space = os.statvfs(probe)
            filesystem_free.setdefault(device, []).append(max(0, space.f_bavail * space.f_frsize))
        except (OSError, ValueError):
            pass  # A native quota report may still establish capacity.
        if content is None:
            continue
        try:
            mount, filesystem, source = _mount(probe, content)
        except ValueError:
            quota_unknown = True
            continue
        if filesystem in {"fuse.mfs", "fuse.moosefs", "mfs"} or source.startswith("mfs#"):
            for scope in (mount, *(path for path in (probe, *probe.parents) if mount in path.parents)):
                scopes.setdefault(scope, set()).add(parent)
        elif filesystem.startswith("fuse") or filesystem in _NETWORK_FILESYSTEMS:
            quota_unknown = True
    available = [StorageCapacity(("filesystem", device), frozenset(filesystem_targets[device]), min(values))
                 for device, values in sorted(filesystem_free.items())]
    constrained = False
    for scope in sorted(scopes):
        check()
        try:
            rows = _quota(scope, deadline=deadline)
        except (OSError, ValueError, subprocess.SubprocessError):
            quota_unknown = True
            continue
        for kind, row in rows.items():
            if kind == "length":
                if row.hard is not None:
                    available.append(StorageCapacity(("quota", scope), frozenset(scopes[scope]),
                                                     max(0, row.hard - row.current)))
                    constrained = True
            elif row.soft is not None or row.hard is not None:
                # Inode/chunk/replica limits require other accounting. They
                # cannot turn unknown payload capacity into a refusal.
                quota_unknown = True
    if scopes and not constrained:
        quota_unknown = True
    measured = {parent for capacity in available for parent in capacity.parents}
    return tuple(available), quota_unknown or bool(requested - measured)



_INVENTORY_CUSTODY: dict[int, subprocess.Popen[bytes]] = {}


def _inventory_command(argv: list[str], *, deadline: float, cancelled: Callable[[], bool],
                       top: int | None = None) -> str:
    """Drain a read-only command without unbounded capture or sort temp files."""
    if time.monotonic() >= deadline or cancelled():
        return "could not gather: diagnostic time budget exhausted or cancelled\n"
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True,
                               env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C"})
    retained: list[tuple[int, str]] = []
    output, pending = bytearray(), b""
    interrupted = False
    unreaped = cleanup_unconfirmed = False

    def line(raw: bytes) -> None:
        if top is None:
            output.extend((raw + b"\n")[:max(0, 8192 - len(output))])
            return
        parts = raw.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            item = (int(parts[0]), parts[1][:4096].decode("utf-8", errors="replace"))
            heapq.heappush(retained, item)
            if len(retained) > top:
                heapq.heappop(retained)

    try:
        assert process.stdout is not None
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while process.poll() is None or selector.get_map():
                if cancelled() or time.monotonic() >= deadline:
                    interrupted = True
                    break
                for key, _event in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    block = os.read(key.fd, 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    pending += block
                    while b"\n" in pending:
                        raw, pending = pending.split(b"\n", 1)
                        line(raw)
                    if len(pending) > 16384:
                        line(pending[:16384])
                        pending = b""
        if pending:
            line(pending)
    finally:
        # A reaped leader does not establish that its inherited stdout or
        # process group drained. The new session still belongs to this command.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            cleanup_unconfirmed = True
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            unreaped = True
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            pass
        except OSError:
            cleanup_unconfirmed = True
        else:
            # A non-child descendant (including an unreaped zombie) cannot be
            # waited here. Keep its group custody and report the uncertainty.
            cleanup_unconfirmed = True
        if unreaped or cleanup_unconfirmed:
            _INVENTORY_CUSTODY[process.pid] = process
        if process.stdout is not None:
            process.stdout.close()
    text = ("".join(f"{size}\t{path}\n" for size, path in sorted(retained, reverse=True))
            if top is not None else output.decode("utf-8", errors="replace"))
    if interrupted or process.returncode != 0:
        text += "could not gather complete listing: timeout, cancellation, or command failure\n"
    if unreaped:
        text += f"could not reap inventory command (pid {process.pid}); cleanup unconfirmed; custody retained\n"
    if cleanup_unconfirmed:
        text += f"inventory process group {process.pid} cleanup unconfirmed; custody retained\n"
    return text


def declare_storage_inventory(out: Path, models_root: Path, *, deadline: float,
                              cancelled: Callable[[], bool]) -> None:
    """Declare diagnostics before refusal; command failure never replaces its numbers."""
    from astrid.core._shared import run_logs

    path = Path(out) / ".preparation-logs" / "storage.inventory.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    run_logs.declare(out, run_logs.LogDecl("storage.inventory", "run", str(path)))
    # Reserve cleanup time below the requested approximately 60-second ceiling.
    end = min(deadline, time.monotonic() + 55)
    volume = models_root
    while not volume.exists():
        volume = volume.parent
    note = ""
    try:
        volume, _filesystem, _source = _mount(volume, Path("/proc/self/mountinfo").read_text())
    except (OSError, ValueError):
        note = "could not resolve volume mount; listing the target subtree instead\n"
    commands = [
        (["findmnt", "-T", str(volume)], None, 5),
        (["df", "-B1", "--", str(volume)], None, 5),
        (["du", "-x", "-B1", "--max-depth=3", "--exclude=.codex", "--", str(volume)], 40, 20),
        (["find", str(volume), "-xdev", "-name", ".codex", "-prune", "-o", "-type", "f",
          "-size", "+1073741824c", "-printf", "%s\t%p\n"], 60, 25),
    ]
    with path.open("w", encoding="utf-8") as stream:
        stream.write(f"Storage inventory: {volume}\nRead-only; .codex directories excluded.\n" + note)
        for argv, top, seconds in commands:
            stream.write(f"\n$ {' '.join(argv)}\n")
            stream.flush()
            try:
                text = _inventory_command(argv, top=top, deadline=min(end, time.monotonic() + seconds),
                                          cancelled=cancelled)
            except (OSError, ValueError, subprocess.SubprocessError):
                text = "could not gather: command unavailable or listing failed\n"
            stream.write(text[:262144] + ("\nlisting truncated\n" if len(text) > 262144 else ""))
            stream.flush()
