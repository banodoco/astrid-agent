"""Read-only capacity admission for managed model fetches.

MooseFS reports cluster capacity through statvfs, independently of subtree
quotas. Its native quota query supplies the enforced logical-byte headroom.
Declared volume capacity minus a complete allocated-byte walk supplies another
independent observation. Unavailable capacity never blocks preparation.
"""
from __future__ import annotations

import heapq
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from .engine_contracts import IntegrityError, UnsafePathError

STORAGE_WARNING = "storage capacity could not be measured; proceeding"
_NETWORK_FILESYSTEMS = frozenset({"nfs", "nfs4", "cifs", "smb3", "ceph", "lustre", "9p"})


@dataclass(frozen=True)
class DeclaredVolume:
    """Controller-recorded capacity; GB uses the larger binary interpretation."""

    mount_path: Path
    size_gb: int


DECLARED_VOLUME: ContextVar[DeclaredVolume | None] = ContextVar("model_storage_volume", default=None)


@dataclass(frozen=True)
class VolumeSpace:
    mount_path: Path
    device: int
    free_bytes: int

    def covers(self, directory: Path) -> bool:
        if directory != self.mount_path and self.mount_path not in directory.parents:
            return False
        try:
            probe = directory
            while not probe.exists():
                probe = probe.parent
            return probe.stat(follow_symlinks=False).st_dev == self.device
        except OSError:
            return False


def measure_declared_volume(volume: DeclaredVolume, directory: Path, *, deadline: float,
                            check: Callable[[], None]) -> VolumeSpace | None:
    """Measure a whole declared mount, or return unknown without partial credit.

    The walk gets at most one quarter of the remaining preparation time (60s
    maximum). Directory descriptors prevent following a replaced symlink.
    Files are never opened; metadata and hardlink identities suffice.
    """
    check()
    root = volume.mount_path
    if (not root.is_absolute() or ".." in root.parts or volume.size_gb <= 0
            or (directory != root and root not in directory.parents)):
        return None
    now = time.monotonic()
    end = min(deadline, now + min(60, max(0, deadline - now) / 4))
    seen: set[tuple[int, int]] = set()
    used, entries, directories = 0, 0, 0
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def bound() -> None:
        check()
        if time.monotonic() >= end:
            raise ValueError("volume walk budget exhausted")

    def visit(fd: int, depth: int, device: int) -> None:
        nonlocal used, entries, directories
        bound()
        directories += 1
        if depth > 128 or directories > 10_000:
            raise ValueError("volume walk directory bound exhausted")
        with os.scandir(fd) as scan:
            for entry in scan:
                bound()
                entries += 1
                if entries > 100_000:
                    raise ValueError("volume walk entry bound exhausted")
                fact = entry.stat(follow_symlinks=False)
                if fact.st_dev != device:
                    continue
                identity = (fact.st_dev, fact.st_ino)
                if identity in seen:
                    continue
                seen.add(identity)
                used += fact.st_blocks * 512
                if stat.S_ISDIR(fact.st_mode):
                    child = os.open(entry.name, flags, dir_fd=fd)
                    try:
                        current = os.fstat(child)
                        if (current.st_dev, current.st_ino) != identity:
                            raise ValueError("volume directory changed during walk")
                        visit(child, depth + 1, device)
                    finally:
                        os.close(child)
        bound()

    try:
        fd = os.open(root.anchor, flags)
        try:
            for part in root.parts[1:]:
                bound()
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            fact = os.fstat(fd)
            observation = VolumeSpace(root, fact.st_dev, 0)
            if not observation.covers(directory):
                return None
            seen.add((fact.st_dev, fact.st_ino))
            used = fact.st_blocks * 512
            visit(fd, 0, fact.st_dev)
            return VolumeSpace(root, fact.st_dev, max(0, volume.size_gb * 2**30 - used))
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return None


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


def available_model_bytes(parent: Path, *, deadline: float,
                          check: Callable[[], None]) -> tuple[int | None, bool]:
    """Tightest measured budget for one directory; never infer a shared scope."""
    check()
    probe = parent
    try:
        while not probe.exists():
            probe = probe.parent
        if not probe.is_dir() or any(path.is_symlink() for path in (parent, *parent.parents)):
            raise UnsafePathError("unsafe target path")
    except OSError:
        return None, True
    available = []
    try:
        space = os.statvfs(probe)
        available.append(max(0, space.f_bavail * space.f_frsize))
    except (OSError, ValueError):
        pass
    try:
        content = Path("/proc/self/mountinfo").read_text()
        mount, filesystem, source = _mount(probe, content)
    except (OSError, ValueError):
        return (min(available) if available else None), True
    unknown = False
    if filesystem in {"fuse.mfs", "fuse.moosefs", "mfs"} or source.startswith("mfs#"):
        constrained = False
        for ancestor in (probe, *probe.parents):
            if ancestor != mount and mount not in ancestor.parents:
                break
            check()
            try:
                rows = _quota(ancestor, deadline=deadline)
            except (OSError, ValueError, subprocess.SubprocessError):
                unknown = True
                continue
            for kind, row in rows.items():
                if kind == "length" and row.hard is not None:
                    available.append(max(0, row.hard - row.current))
                    constrained = True
                elif kind != "length" and (row.soft is not None or row.hard is not None):
                    unknown = True  # Other dimensions cannot measure payload bytes.
        unknown |= not constrained
    elif filesystem.startswith("fuse") or filesystem in _NETWORK_FILESYSTEMS:
        unknown = True
    return (min(available) if available else None), unknown or not available



def _inventory_walk(root: Path, *, deadline: float, cancelled: Callable[[], bool]) -> str:
    """Bounded metadata-only walk; partial diagnostics never replace refusal."""
    sizes: dict[Path, int] = {root: 0}
    files: list[tuple[int, str]] = []
    stack: list[tuple[Path, tuple[Path, ...]]] = [(root, (root,))]
    entries, scheduled = 0, 1
    incomplete = False
    fact = root.stat(follow_symlinks=False)
    device = fact.st_dev
    if not stat.S_ISDIR(fact.st_mode) or any(path.is_symlink() for path in root.parents):
        raise OSError("models root is not a directory")
    sizes[root] = fact.st_blocks * 512
    while stack:
        if cancelled() or time.monotonic() >= deadline or entries >= 100_000 or scheduled >= 10_000:
            incomplete = True
            break
        directory, ancestors = stack.pop()
        try:
            with os.scandir(directory) as scan:
                for entry in scan:
                    if cancelled() or time.monotonic() >= deadline or entries >= 100_000 or scheduled >= 10_000:
                        incomplete = True
                        break
                    entries += 1
                    if entry.name in {".codex", ".env"}:
                        continue
                    fact = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(fact.st_mode) or fact.st_dev != device:
                        continue
                    allocated = fact.st_blocks * 512
                    for ancestor in ancestors:
                        sizes[ancestor] += allocated
                    path = Path(entry.path)
                    if stat.S_ISDIR(fact.st_mode):
                        scheduled += 1
                        tracked = ancestors
                        if len(ancestors) < 4:
                            sizes[path] = allocated
                            tracked += (path,)
                        stack.append((path, tracked))
                    elif stat.S_ISREG(fact.st_mode) and fact.st_size > 2**30:
                        heapq.heappush(files, (fact.st_size, str(path)))
                        if len(files) > 60:
                            heapq.heappop(files)
        except OSError:
            incomplete = True
        if incomplete and (cancelled() or time.monotonic() >= deadline or entries >= 100_000 or scheduled >= 10_000):
            break
    def rows(items: list[tuple[int, str]]) -> str:
        lines = []
        for size, name in sorted(items, reverse=True):
            raw = name.replace("\n", r"\n").replace("\r", r"\r").encode(errors="replace")
            lines.append(f"{size}\t{raw[:1024].decode(errors='ignore')}{'...' if len(raw) > 1024 else ''}\n")
        return "".join(lines)
    directories = heapq.nlargest(40, ((size, str(path)) for path, size in sizes.items()))
    return ("Largest directories (allocated bytes, top 40):\n" + rows(directories)
            + "Files over 1 GiB (logical bytes, top 60):\n" + rows(files)
            + ("incomplete: directory walk stopped or metadata unavailable\n" if incomplete else ""))


def declare_storage_inventory(out: Path, models_root: Path, *, deadline: float,
                              cancelled: Callable[[], bool]) -> None:
    """Declare bounded Python diagnostics before raising the measured refusal."""
    from astrid.core._shared import run_logs

    path = Path(out) / ".preparation-logs" / "storage.inventory.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    run_logs.declare(out, run_logs.LogDecl("storage.inventory", "run", str(path)))
    end = min(deadline, time.monotonic() + 55)
    intended = models_root.absolute()
    probe = intended
    while not probe.exists():
        probe = probe.parent
    root, text = probe, ""
    try:
        mounts = []
        content = Path("/proc/self/mountinfo").read_text(errors="replace")
        for line in content.splitlines():
            fields = line.partition(" - ")[0].split()
            if len(fields) >= 6 and Path(_unescape(fields[4])) in (intended, *intended.parents):
                mounts.append(line)
        raw = "\n".join(mounts).encode(errors="replace")
        text += "Mounts (/proc/self/mountinfo):\n" + raw[:8192].decode(errors="ignore") + "\n"
        if len(raw) > 8192:
            text += "incomplete: mount listing truncated\n"
        mount, _filesystem, _source = _mount(intended, content)
        if mount.stat(follow_symlinks=False).st_dev != probe.stat(follow_symlinks=False).st_dev:
            raise ValueError("mount device differs from models device")
        root = mount
    except (OSError, ValueError):
        text += "incomplete: covering mount unavailable; listing target subtree\n"
    text = f"Storage inventory: {root}\nRead-only; symlinks and .codex excluded.\n" + text
    try:
        space = os.statvfs(probe)
        text += f"statvfs: free_bytes={space.f_bavail * space.f_frsize} total_bytes={space.f_blocks * space.f_frsize}\n"
    except (OSError, ValueError):
        text += "incomplete: statvfs unavailable\n"
    try:
        text += _inventory_walk(root, deadline=end, cancelled=cancelled)
    except (OSError, ValueError):
        text += "incomplete: directory listing unavailable\n"
    encoded = text.encode(errors="replace")
    if len(encoded) > 131072:
        encoded = encoded[:131040] + b"\nincomplete: output truncated\n"
    path.write_text(encoded.decode(errors="ignore"), encoding="utf-8")
