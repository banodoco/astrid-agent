"""Sealed, write-once snapshot of an installed engine environment on a pod's network volume.

A fresh pod restores the snapshot to the SAME absolute container-disk engine root; the
unchanged canonical ``astrid engines install`` then re-verifies every step and skips what
verifies. The snapshot is a cache seed, never install authority: it never carries
``install.json``, session/custody state, model-verification records, flocks or models.

Trust boundary: the identity, the stat-bound manifest and the streamed sha256, member count and
byte total detect corruption, partial writes and identity drift. They are not tamper-proof
evidence against code running as the engine's user (which can rewrite a tar and its manifest
together), nor against changes that preserve the recorded (size, mtime_ns) between restores.
Extraction refuses hardlinks, special files, escaping names and escaping symlinks, never follows
a symlink and never trusts tar owners or modes, so a damaged archive cannot write outside its
staging tree. A separate isolation boundary (another UID or a sandbox) would be needed to make
the volume tamper-evident.

CLI: ``python -I -m astrid.packs.runpod.engine_snapshot {restore|publish|report|prune} --root ROOT
--cache CACHE --engine ENGINE --platform PLATFORM [--deadline S] [--volume-size-gb N --mount M]
[--identity ID [--confirm]]``. Each run prints one final ``ENGINE_SNAPSHOT_JSON=<json>`` line and
exits 0 except for usage errors. Standard library only, apart from the recipe and volume helpers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as _platform
import re
import shutil
import stat
import sys
import tarfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "astrid.engine-snapshot.v1"
PREFIX = "ENGINE_SNAPSHOT_JSON="
REASONS = frozenset({
    "already_published", "count_mismatch", "corrupt_archive", "deadline_exceeded", "digest_mismatch",
    "identity_unavailable", "insufficient_space", "internal_error", "io_error", "lost_race",
    "no_snapshot", "not_installed", "partial_content", "tree_changed", "unsafe_member", "unsafe_root",
    "unsafe_tree",
})
INPUT_KEYS = ("schema", "engine", "platform", "recipe_sha256", "helper_sha256", "requirements_locks",
              "python_versions", "python_abi", "machine")
PARTIAL_GRACE_SECONDS = 3600
SPACE_MARGIN = 1.10
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_LOCKISH = re.compile(r"(\.lock|\.flock|\.owner)$|^owner")
_MANIFEST_LIMIT = 1024 * 1024


class Skip(Exception):
    def __init__(self, reason, **facts):
        assert reason in REASONS
        super().__init__(reason)
        self.reason, self.facts = reason, facts


# ---- identity -------------------------------------------------------------------------------

def describe(engine, platform):
    """Identity inputs, human-readable pins and the primary venv path from the packaged recipe."""
    from astrid.core.generation import engine_recipe, engine_runner
    recipe = engine_recipe.load_recipe(engine, platform)
    envs = recipe.environments
    primary = next(e for e in envs if e.name == recipe.primary_environment)
    major, minor = primary.python_version.split(".")[:2]
    inputs = {
        "schema": SCHEMA, "engine": recipe.engine, "platform": recipe.platform,
        "recipe_sha256": engine_recipe.recipe_digest(recipe),
        "helper_sha256": engine_runner._sha(engine_runner.helper_manifest(recipe)),
        "requirements_locks": {e.name: e.requirements.packaged_lock.sha256
                               if e.requirements.packaged_lock else None for e in envs},
        "python_versions": {e.name: e.python_version for e in envs},
        "python_abi": f"cp{major}{minor}", "machine": _platform.machine(),
    }
    pins = {s.name: s.pin.revision for s in recipe.sources if s.name in ("comfyui", "vibecomfy")}
    pins["node_lock_sha256"] = primary.node_lock.sha256 if primary.node_lock else None
    return {"inputs": inputs, "pins": pins, "venv": primary.relative_path}


def identity_of(inputs):
    canonical = json.dumps({k: inputs[k] for k in INPUT_KEYS}, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---- small filesystem helpers ---------------------------------------------------------------

class _Clock:
    def __init__(self, seconds):
        self.end = time.monotonic() + seconds
        self.durations = {}
        self._start = time.monotonic()

    def check(self):
        if time.monotonic() >= self.end:
            raise Skip("deadline_exceeded")

    def phase(self, name, started):
        self.durations[name] = round(time.monotonic() - started, 3)

    def total(self):
        self.durations["total"] = round(time.monotonic() - self._start, 3)
        return self.durations


def _within(path, base):
    return path == base or path.startswith(base.rstrip("/") + "/")


def _lexical(link_dir, target):
    return os.path.normpath(target if target.startswith("/") else os.path.join(link_dir, target))


def _open_dir(base_fd, parts):
    fd = os.dup(base_fd)
    try:
        for part in parts:
            child = os.open(part, _DIR, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _fsync_dir(path):
    fd = os.open(path, _DIR)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_json(path, limit=_MANIFEST_LIMIT):
    try:
        fd = os.open(path, _READ)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        with os.fdopen(os.dup(fd), "rb") as stream:
            value = json.loads(stream.read(limit + 1).decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None
    finally:
        os.close(fd)


def _unlink(*paths):
    for path in paths:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


# ---- manifests ------------------------------------------------------------------------------

def _archive_ok(directory, name, kind, entry):
    if not isinstance(entry, dict) or not all(isinstance(entry.get(k), int) and not isinstance(entry.get(k), bool)
                                              for k in ("size", "mtime_ns", "members", "bytes")):
        return False
    if not (isinstance(entry.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            and isinstance(entry.get("destination"), str) and entry["destination"].startswith("/")):
        return False
    try:
        info = os.lstat(os.path.join(directory, f"{name}.{kind}.tar"))
    except OSError:
        return False
    # st_dev and st_ino are not stable across pods on a network mount; size and mtime are.
    return stat.S_ISREG(info.st_mode) and (info.st_size, info.st_mtime_ns) == (entry["size"], entry["mtime_ns"])


def _valid_manifests(directory, identity, root=None):
    """(name, manifest) pairs whose identity, schema and recorded tar stats all still hold."""
    found = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return found
    for entry in names:
        match = re.fullmatch(r"([0-9a-f]{32})\.manifest\.json", entry)
        manifest = _read_json(os.path.join(directory, entry)) if match else None
        if manifest is None or manifest.get("schema") != SCHEMA or manifest.get("identity") != identity:
            continue
        try:
            if identity_of(manifest) != identity:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        archives = manifest.get("archives")
        if not isinstance(archives, dict) or not isinstance(manifest.get("created_at"), str):
            continue
        if not _archive_ok(directory, match[1], "root", archives.get("root")):
            continue
        if archives.get("python") is not None and not _archive_ok(directory, match[1], "python", archives["python"]):
            continue
        if root is not None and archives["root"]["destination"] != root:
            continue
        found.append((match[1], manifest))
    return found


# ---- publish --------------------------------------------------------------------------------

def _root_included(rel):
    """True to include, None to descend without a member (``.astrid-engine``), False to skip."""
    parts = rel.split("/")
    if rel == ".astrid-engine":
        return None
    if parts[0] == "runtime":
        return len(parts) == 1 or parts[1] != "uv-cache"
    if parts[0] != ".astrid-engine" or len(parts) < 2 or parts[1] not in ("environments", "pycache"):
        return False
    # Locks under environments/ are hash-bearing content; elsewhere they are flocks or owner markers.
    return parts[1] == "environments" or not _LOCKISH.search(parts[-1])


def _walk(base, include, allowed, clock):
    """Stat-walk ``base`` without following symlinks: (rel, stat, linkname) in tar order."""
    entries = []

    def visit(fd, prefix):
        with os.scandir(fd) as scan:
            children = sorted(scan, key=lambda e: e.name)
        for entry in children:
            clock.check()
            rel = prefix + entry.name
            wanted = include(rel)
            if wanted is False:
                continue
            info = entry.stat(follow_symlinks=False)
            mode = info.st_mode
            if stat.S_ISLNK(mode):
                target = os.readlink(entry.name, dir_fd=fd)
                resolved = _lexical(os.path.dirname(os.path.join(base, rel)), target)
                if not any(_within(resolved, a) for a in allowed):
                    raise Skip("unsafe_tree")
                entries.append((rel, info, target))
            elif stat.S_ISDIR(mode):
                if wanted:
                    entries.append((rel, info, None))
                child = os.open(entry.name, _DIR, dir_fd=fd)
                try:
                    visit(child, rel + "/")
                finally:
                    os.close(child)
            elif stat.S_ISREG(mode):
                entries.append((rel, info, None))
            else:
                raise Skip("unsafe_tree")

    fd = os.open(base, _DIR)
    try:
        visit(fd, "")
    finally:
        os.close(fd)
    return entries


def _estimate(entries):
    return sum(512 + (-(-e[1].st_size // 512) * 512 if stat.S_ISREG(e[1].st_mode) else 0) for e in entries)


class _HashingWriter:
    def __init__(self, stream):
        self.stream, self.sha, self.size = stream, hashlib.sha256(), 0

    def write(self, data):
        self.sha.update(data)
        self.size += len(data)
        return self.stream.write(data)


def _write_tar(base, entries, path, clock):
    """Write an uncompressed tar of ``entries``; every path is an independent REG/DIR/SYM member."""
    partial = path + ".partial"
    base_fd = os.open(base, _DIR)
    members = total = 0
    try:
        with open(partial, "xb") as raw:
            writer = _HashingWriter(raw)
            with tarfile.open(fileobj=writer, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                for rel, info, link in entries:
                    clock.check()
                    member = tarfile.TarInfo(rel)
                    member.uid = member.gid = 0
                    member.uname = member.gname = ""
                    member.mode = (stat.S_IMODE(info.st_mode) & 0o755) | (info.st_mode & 0o700)
                    member.mtime = int(info.st_mtime)
                    if link is not None:
                        member.type, member.linkname = tarfile.SYMTYPE, link
                        tar.addfile(member)
                    elif stat.S_ISDIR(info.st_mode):
                        member.type = tarfile.DIRTYPE
                        tar.addfile(member)
                    else:
                        member.type, member.size = tarfile.REGTYPE, info.st_size
                        parts = rel.split("/")
                        parent = _open_dir(base_fd, parts[:-1])
                        try:
                            fd = os.open(parts[-1], _READ, dir_fd=parent)
                        finally:
                            os.close(parent)
                        with os.fdopen(fd, "rb") as source:
                            now = os.fstat(source.fileno())
                            if (now.st_ino, now.st_size, now.st_mtime_ns) != (info.st_ino, info.st_size, info.st_mtime_ns):
                                raise Skip("tree_changed")
                            try:
                                tar.addfile(member, source)
                            except OSError as exc:
                                if "end of data" in str(exc):
                                    raise Skip("tree_changed") from None
                                raise
                        total += info.st_size
                    members += 1
            raw.flush()
            os.fsync(raw.fileno())
        os.rename(partial, path)
        _fsync_dir(os.path.dirname(path))
        final = os.lstat(path)
        return {"sha256": writer.sha.hexdigest(), "size": final.st_size, "mtime_ns": final.st_mtime_ns,
                "members": members, "bytes": total, "destination": base}
    finally:
        os.close(base_fd)
        _unlink(partial)


def _python_dir(root, venv):
    cfg = os.path.join(root, venv, "pyvenv.cfg")
    try:
        fd = os.open(cfg, _READ)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            text = stream.read(65536)
    except (OSError, UnicodeError):
        return None
    home = next((line.split("=", 1)[1].strip() for line in text.splitlines()
                 if line.split("=", 1)[0].strip() == "home" and "=" in line), None)
    if not home or not home.startswith("/"):
        return None
    home = os.path.normpath(home)
    directory = os.path.dirname(home) if os.path.basename(home) == "bin" else home
    if _within(directory, root) or not os.path.isdir(directory):
        return None
    if os.path.realpath(directory) != directory:
        raise Skip("unsafe_tree")  # a symlinked managed Python would not restore at its recorded path
    return directory


def _free_bytes(cache, mount, size_gb, clock):
    probe = cache
    while not os.path.exists(probe):
        probe = os.path.dirname(probe)
    vfs = os.statvfs(probe)
    free = vfs.f_bavail * vfs.f_frsize
    if mount is not None and size_gb is not None:
        from astrid.core.generation import model_storage
        measured = model_storage.measure_declared_volume(
            model_storage.DeclaredVolume(Path(mount), size_gb), Path(cache),
            deadline=clock.end, check=clock.check)
        if measured is not None:
            free = min(free, measured.free_bytes)
    return free


def publish(args, described, identity, clock):
    root, cache = args.root, args.cache
    receipt = _read_json(os.path.join(root, ".astrid-engine", "install.json"), 4 * 1024 * 1024)
    if receipt is None or receipt.get("status") != "installed":
        raise Skip("not_installed")
    directory = os.path.join(cache, identity)
    if _valid_manifests(directory, identity, root):
        raise Skip("already_published")
    started = time.monotonic()
    python = _python_dir(root, described["venv"])
    allowed = [root] + ([python] if python else [])
    root_entries = _walk(root, _root_included, allowed, clock)
    python_entries = _walk(python, lambda rel: True, allowed, clock) if python else []
    estimate = _estimate(root_entries) + _estimate(python_entries) + 2 * 10240 + _MANIFEST_LIMIT
    free = _free_bytes(cache, args.mount, args.volume_size_gb, clock)
    clock.phase("estimate", started)
    if free < estimate * SPACE_MARGIN:
        raise Skip("insufficient_space", estimate_bytes=estimate, free_bytes=free)
    os.makedirs(directory, exist_ok=True)
    name = uuid.uuid4().hex
    written = []
    try:
        started = time.monotonic()
        written.append(os.path.join(directory, f"{name}.root.tar"))
        archives = {"root": _write_tar(root, root_entries, written[-1], clock), "python": None}
        clock.phase("write_root", started)
        started = time.monotonic()
        if python:
            written.append(os.path.join(directory, f"{name}.python.tar"))
            archives["python"] = _write_tar(python, python_entries, written[-1], clock)
        clock.phase("write_python", started)
        started = time.monotonic()
        if _valid_manifests(directory, identity, root):
            raise Skip("lost_race")
        manifest = {**described["inputs"], "schema": SCHEMA, "identity": identity, "pins": described["pins"],
                    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    "archives": archives}
        final = os.path.join(directory, f"{name}.manifest.json")
        written.append(final + ".partial")
        with open(written[-1], "x", encoding="utf-8") as stream:
            json.dump(manifest, stream, sort_keys=True, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(written[-1], final)
        _fsync_dir(directory)
        clock.phase("manifest", started)
    except BaseException:
        _unlink(*written)
        raise
    return {"action": "published", "identity": identity, "estimate_bytes": estimate,
            "archive_bytes": sum(a["size"] for a in archives.values() if a),
            "members": sum(a["members"] for a in archives.values() if a)}


# ---- restore --------------------------------------------------------------------------------

class _HashingReader:
    def __init__(self, stream):
        self.stream, self.sha, self.size = stream, hashlib.sha256(), 0

    def read(self, n=-1):
        data = self.stream.read(n)
        self.sha.update(data)
        self.size += len(data)
        return data


def _extract(tar_path, staging, destination, allowed, entry, include, clock):
    """Stream-extract one archive into ``staging`` with this module's own member filter."""
    staging_fd = os.open(staging, _DIR)
    directories, members, total = [], 0, 0
    try:
        with open(tar_path, "rb") as raw:
            reader = _HashingReader(raw)
            try:
                with tarfile.open(fileobj=reader, mode="r|") as tar:
                    for member in tar:
                        clock.check()
                        members += 1
                        total += _member(tar, member, staging_fd, destination, allowed, include, directories)
                while reader.read(1 << 20):
                    pass
            except (tarfile.TarError, EOFError, UnicodeError):
                raise Skip("corrupt_archive") from None
        if reader.sha.hexdigest() != entry["sha256"] or reader.size != entry["size"]:
            raise Skip("digest_mismatch")
        if (members, total) != (entry["members"], entry["bytes"]):
            raise Skip("count_mismatch")
        for parts, mode in sorted(directories, key=lambda d: -len(d[0])):
            fd = _open_dir(staging_fd, parts)
            try:
                os.fchmod(fd, mode)
            finally:
                os.close(fd)
    finally:
        os.close(staging_fd)
    _require_single_links(staging)
    return members, reader.size


def _member(tar, member, staging_fd, destination, allowed, include, directories):
    name = member.name.rstrip("/") if member.isdir() else member.name
    parts = name.split("/")
    if (not name or name.startswith("/") or "\x00" in name or any(p in ("", ".", "..") for p in parts)
            or not include(name) or not (member.isreg() or member.isdir() or member.issym())
            or member.type == tarfile.LNKTYPE):
        raise Skip("unsafe_member")
    if member.issym():
        resolved = _lexical(os.path.dirname(os.path.join(destination, name)), member.linkname)
        if not member.linkname or not any(_within(resolved, a) for a in allowed):
            raise Skip("unsafe_member")
    try:
        parent = _open_dir(staging_fd, parts[:-1])
    except OSError:
        raise Skip("unsafe_member") from None  # missing parent, or a parent that is a symlink
    try:
        if member.isdir():
            try:
                os.mkdir(parts[-1], 0o700, dir_fd=parent)
            except FileExistsError:
                if not stat.S_ISDIR(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False).st_mode):
                    raise Skip("unsafe_member") from None
            directories.append((parts, (stat.S_IMODE(member.mode) & 0o755) | 0o700))
            return 0
        if member.issym():
            try:
                os.symlink(member.linkname, parts[-1], dir_fd=parent)
            except FileExistsError:
                raise Skip("unsafe_member") from None
            return 0
        try:
            fd = os.open(parts[-1], _CREATE, 0o600, dir_fd=parent)
        except OSError:
            raise Skip("unsafe_member") from None
        with os.fdopen(fd, "wb") as out:
            source = tar.extractfile(member)
            shutil.copyfileobj(source, out, 1 << 20)
            out.flush()
            os.fchmod(out.fileno(), (stat.S_IMODE(member.mode) & 0o755) | 0o600)
            os.utime(out.fileno(), (member.mtime, member.mtime))
            return out.tell()
    finally:
        os.close(parent)


def _require_single_links(base):
    for current, dirs, names in os.walk(base):
        for name in names:
            info = os.lstat(os.path.join(current, name))
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise Skip("unsafe_member")


def _only_empty_dirs(path):
    for current, dirs, names in os.walk(path):
        if names or any(os.path.islink(os.path.join(current, d)) for d in dirs):
            return False
    return True


def _place_root(staging, root):
    if os.path.lexists(root):
        if os.path.islink(root) or not os.path.isdir(root) or not _only_empty_dirs(root):
            raise Skip("partial_content")
        for current, dirs, names in os.walk(root, topdown=False):
            os.rmdir(current)
    os.rename(staging, root)
    for path in (root, os.path.join(root, ".astrid-engine")):
        _chmod_dir(path)
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise Skip("unsafe_root")


def _chmod_dir(path):
    fd = os.open(path, _DIR)
    try:
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def restore(args, described, identity, clock):
    root = args.root
    started = time.monotonic()
    if os.path.lexists(os.path.join(root, ".astrid-engine", "install.json")) or \
            os.path.lexists(os.path.join(root, "runtime")):
        raise Skip("partial_content")
    if os.path.lexists(root) and (os.path.islink(root) or not _only_empty_dirs(root)):
        raise Skip("partial_content")
    directory = os.path.join(args.cache, identity)
    found = _valid_manifests(directory, identity, root)
    clock.phase("manifest_check", started)
    if not found:
        raise Skip("no_snapshot")
    name, manifest = max(found, key=lambda item: (item[1]["created_at"], item[0]))
    archives = manifest["archives"]
    python = archives.get("python")
    allowed = [root] + ([python["destination"]] if python else [])
    token = uuid.uuid4().hex
    staging = f"{root}.restore-{token}"
    python_staging = None
    try:
        started = time.monotonic()
        os.makedirs(os.path.dirname(root), exist_ok=True)
        os.mkdir(staging, 0o700)
        os.mkdir(os.path.join(staging, ".astrid-engine"), 0o700)
        members, size = _extract(os.path.join(directory, f"{name}.root.tar"), staging, root, allowed,
                                 archives["root"], _root_included, clock)
        clock.phase("restore_root", started)
        started = time.monotonic()
        python_action = "absent"
        if python:
            python_action = "present"
            if not os.path.lexists(python["destination"]):
                python_staging = f"{python['destination']}.restore-{token}"
                os.makedirs(os.path.dirname(python_staging), 0o700, exist_ok=True)
                os.mkdir(python_staging, 0o700)
                count, length = _extract(os.path.join(directory, f"{name}.python.tar"), python_staging,
                                         python["destination"], allowed, python, lambda rel: True, clock)
                members, size = members + count, size + length
                os.rename(python_staging, python["destination"])
                python_staging, python_action = None, "restored"
        clock.phase("restore_python", started)
        _place_root(staging, root)
    except BaseException:
        for path in (staging, python_staging):
            if path and os.path.lexists(path):
                shutil.rmtree(path, ignore_errors=True)
        raise
    return {"action": "restored", "identity": identity, "archive_bytes": size, "members": members,
            "python": python_action}


# ---- report and prune -----------------------------------------------------------------------

def report(args, described, identity, clock):
    rows, partials, cleaned, total = [], 0, 0, 0
    now = time.time()
    try:
        names = sorted(os.listdir(args.cache))
    except OSError:
        names = []
    for entry in names:
        directory = os.path.join(args.cache, entry)
        if not re.fullmatch(r"[0-9a-f]{64}", entry) or os.path.islink(directory) or not os.path.isdir(directory):
            continue
        clock.check()
        valid = _valid_manifests(directory, entry)
        referenced = set()
        for manifest_name in os.listdir(directory):
            match = re.fullmatch(r"([0-9a-f]{32})\.manifest\.json", manifest_name)
            if match:
                referenced.update({f"{match[1]}.root.tar", f"{match[1]}.python.tar"})
        size = 0
        for file_name in sorted(os.listdir(directory)):
            path = os.path.join(directory, file_name)
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                continue
            orphan = file_name.endswith(".tar") and file_name not in referenced
            if file_name.endswith(".partial") or orphan:
                partials += 1
                if now - info.st_mtime > PARTIAL_GRACE_SECONDS:
                    _unlink(path)
                    cleaned += 1
            elif file_name.endswith(".tar"):
                size += info.st_size
        total += size
        rows.append({"identity": entry, "current": entry == identity, "valid": len(valid), "bytes": size,
                     "created_at": max((m["created_at"] for _, m in valid), default=None)})
    return {"action": "reported", "identity": identity, "identities": rows, "total_bytes": total,
            "partials": partials, "cleaned": cleaned}


def prune(args, described, identity, clock):
    directory = os.path.join(args.cache, args.identity)
    if os.path.islink(directory) or not os.path.isdir(directory):
        return {"action": "skipped", "reason": "no_snapshot", "files": 0, "bytes": 0}
    files = [os.path.join(c, n) for c, _, ns in os.walk(directory) for n in ns]
    size = sum(os.lstat(p).st_size for p in files)
    if not args.confirm:
        return {"action": "dry_run", "files": len(files), "bytes": size}
    shutil.rmtree(directory)
    return {"action": "pruned", "files": len(files), "bytes": size}


# ---- CLI ------------------------------------------------------------------------------------

def _parser():
    parser = argparse.ArgumentParser(prog="engine_snapshot")
    parser.add_argument("command", choices=("restore", "publish", "report", "prune"))
    for flag in ("--root", "--cache", "--engine", "--platform"):
        parser.add_argument(flag, required=True)
    parser.add_argument("--deadline", type=float, default=1800.0)
    parser.add_argument("--volume-size-gb", type=int)
    parser.add_argument("--mount")
    parser.add_argument("--identity")
    parser.add_argument("--confirm", action="store_true")
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    for flag in ("root", "cache", "mount"):
        value = getattr(args, flag)
        if value is not None:
            if not value.startswith("/"):
                parser.error(f"--{flag} must be absolute")
            setattr(args, flag, os.path.normpath(value))
    if (args.identity is not None or args.confirm) and args.command != "prune":
        parser.error("--identity and --confirm are only for prune")
    if args.command == "prune" and not re.fullmatch(r"[0-9a-f]{64}", args.identity or ""):
        parser.error("prune requires --identity with a 64-hex identity")
    if (args.mount is None) != (args.volume_size_gb is None) or (args.volume_size_gb is not None and args.volume_size_gb <= 0):
        parser.error("--volume-size-gb and --mount go together")
    if not args.deadline > 0:
        parser.error("--deadline must be positive")
    clock = _Clock(args.deadline)
    result = {"command": args.command}
    try:
        try:
            described = describe(args.engine, args.platform)
            identity = identity_of(described["inputs"])
        except Exception:  # noqa: BLE001 - any recipe failure is one fixed reason, never its text
            if args.command in ("restore", "publish"):
                raise Skip("identity_unavailable") from None
            described, identity = None, None  # housekeeping never needs the current identity
        result["identity"] = identity
        handler = {"restore": restore, "publish": publish, "report": report, "prune": prune}[args.command]
        result.update(handler(args, described, identity, clock))
    except Skip as skip:
        result.update(skip.facts, action="skipped", reason=skip.reason)
    except OSError:
        result.update(action="skipped", reason="io_error")
    except Exception:  # noqa: BLE001 - the one-line JSON contract carries no exception text
        result.update(action="skipped", reason="internal_error")
    result["durations"] = clock.total()
    sys.stdout.write(PREFIX + json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
