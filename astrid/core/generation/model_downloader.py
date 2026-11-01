"""Verified, resumable downloads for immutable engine model locks.

The target owns ``models_root``. This module performs no controller-to-target
path translation and accepts only a pinned model identity from engine_contracts.
"""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
import urllib.request
import uuid
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

from .engine_contracts import (  # canonical shared types/errors; supplied by A1
    Deadline,
    DownloadError,
    DownloadResult,
    DeadlineExceeded,
    IntegrityError,
    ModelLockValidationError,
    ModelSpec,
    TargetPath,
    UnsafePathError,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_RANGE_SIZE = 64 << 20
_READ_SIZE = 1 << 20


def _safe_relative(value: str) -> PurePosixPath:
    if (not isinstance(value, str) or not value or "\\" in value or "\0" in value
            or value.startswith("/") or re.match(r"^[A-Za-z]:", value)):
        raise UnsafePathError("model destination must be a normalized relative POSIX path")
    parts = value.split("/")
    if any(part in ("", ".", "..") or re.match(r"^[A-Za-z]:", part) for part in parts):
        raise UnsafePathError("model destination contains an unsafe path component")
    return PurePosixPath(*parts)


def _validate_model(model: ModelSpec) -> tuple[PurePosixPath, str]:
    if not isinstance(model.name, str) or not model.name:
        raise ModelLockValidationError("model name must be a nonempty string")
    rel = _safe_relative(model.destination)
    if type(model.bytes) is not int or model.bytes <= 0:
        raise ModelLockValidationError("model byte count must be a positive integer")
    if not isinstance(model.sha256, str) or not _SHA256.fullmatch(model.sha256):
        raise ModelLockValidationError("model SHA-256 must be 64 lowercase hexadecimal characters")
    hf = (model.repository is not None, model.revision is not None, model.path is not None)
    direct = model.url is not None
    if direct:
        if any(hf):
            raise ModelLockValidationError("model must use exactly one source form")
        if not isinstance(model.url, str):
            raise ModelLockValidationError("model URL must be credential-free HTTPS")
        parsed = urlsplit(model.url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment):
            raise ModelLockValidationError("model URL must be credential-free HTTPS")
        return rel, model.url
    if not all(hf):
        raise ModelLockValidationError("model must use exactly one source form")
    if (not isinstance(model.repository, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", model.repository)
            or not isinstance(model.revision, str) or not _REVISION.fullmatch(model.revision)):
        raise ModelLockValidationError("Hugging Face model source must use a repository and full immutable revision")
    source_path = _safe_relative(model.path)
    url = (f"https://huggingface.co/{model.repository}/resolve/{model.revision}/"
           f"{quote(source_path.as_posix(), safe='/')}")
    return rel, url


def _root_and_destination(models_root: TargetPath, relative: PurePosixPath) -> tuple[Path, Path]:
    if not isinstance(models_root, str) or not models_root or "\0" in models_root:
        raise UnsafePathError("models_root must be a nonempty target path")
    root = Path(models_root)
    if not root.is_absolute():
        raise UnsafePathError("models_root must be absolute")
    if ".." in root.parts:
        raise UnsafePathError("models_root must not contain parent traversal")
    if os.name == "nt" and any(":" in part for part in relative.parts):
        raise UnsafePathError("model destination must not name a Windows alternate data stream")
    # Check ancestors of the root too: /safe/link/models must not silently
    # redirect model writes outside the selected target tree.
    for directory in (*reversed(root.parents), root):
        if directory.is_symlink():
            raise UnsafePathError("models_root must not traverse a symlink")
        if directory.exists() and not directory.is_dir():
            raise UnsafePathError("models_root must be a directory")
    destination = root.joinpath(*relative.parts)
    cursor = root
    for component in relative.parts[:-1]:
        cursor = cursor / component
        if cursor.is_symlink():
            raise UnsafePathError("model destination traverses a symlink")
        if cursor.exists() and not cursor.is_dir():
            raise UnsafePathError("model destination parent must be a directory")
    if destination.is_symlink():
        raise UnsafePathError("model destination must not be a symlink")
    if destination.exists() and not destination.is_file():
        raise UnsafePathError("model destination must be a regular file")
    return root, destination


def _digest(path: Path, *, deadline: Deadline) -> tuple[int, str]:
    value = hashlib.sha256()
    count = 0
    with path.open("rb") as stream:
        while True:
            _check_deadline(deadline)
            block = stream.read(8 << 20)
            _check_deadline(deadline)
            if not block:
                break
            count += len(block)
            value.update(block)
    _check_deadline(deadline)
    return count, value.hexdigest()


def _matches(path: Path, model: ModelSpec, *, deadline: Deadline) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    size, sha = _digest(path, deadline=deadline)
    return size == model.bytes and sha == model.sha256


def _check_deadline(deadline: Deadline) -> float:
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise DeadlineExceeded("model download deadline exceeded")
    return remaining


def _quarantine(path: Path) -> None:
    quarantine = path.with_name(path.name + ".quarantine")
    if quarantine.exists() or quarantine.is_symlink():
        quarantine.unlink()
    path.replace(quarantine)


def _looks_like_legacy_redirect(path: Path, expected_bytes: int) -> bool:
    # Old H3 chunks are safetensors. Xet/HTTP redirect text was occasionally
    # left in .part; recognize that legacy signature and restart from byte 0.
    if not path.exists() or path.stat().st_size >= 1 << 20 or expected_bytes <= 1 << 20:
        return False
    with path.open("rb") as stream:
        prefix = stream.read(512).lstrip().lower()
    return (prefix.startswith((b"<!doctype html", b"<html", b"redirecting", b"unauthorized",
                               b"forbidden", b"{\"error\"", b"{\n  \"error\"")))


def _cache_root(model_cache_root: TargetPath) -> Path:
    if (not isinstance(model_cache_root, str) or not model_cache_root
            or "\0" in model_cache_root):
        raise UnsafePathError("model_cache_root must be a nonempty absolute path")
    root = Path(model_cache_root)
    if not root.is_absolute() or ".." in root.parts:
        raise UnsafePathError("model_cache_root must be absolute without parent traversal")
    return root


def _cache_relative(model: ModelSpec) -> PurePosixPath:
    """Namespace by the entire immutable lock entry, never a basename/size hint."""
    _validate_model(model)
    identity = json.dumps(asdict(model), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return PurePosixPath(hashlib.sha256(identity.encode("ascii")).hexdigest(), *_safe_relative(model.destination).parts)


def _read_mountinfo():
    try:
        return Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _mount_identity(root: Path) -> tuple:
    """Snapshot the unique live containing mount; no source strings escape here."""
    existing = root
    while not existing.exists():
        existing = existing.parent
    device = existing.stat().st_dev
    table = _read_mountinfo()
    if table is None:
        # Direct downloader CPU tests can use a non-Linux host. Authenticated
        # mount-ID bindings below fail closed unless Linux evidence exists.
        return (None, device, str(existing.anchor))
    selected = []
    nested = []
    for line in table.splitlines():
        fields = line.split()
        if (len(fields) < 10 or "-" not in fields or not fields[0].isdigit()
                or int(fields[0]) <= 0 or not fields[4].startswith("/")):
            raise UnsafePathError("model cache mount table is malformed")
        point = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4]))
        if point == root or point in root.parents:
            selected.append((len(point.parts), int(fields[0]), fields[2], str(point)))
        elif root in point.parents:
            nested.append(point)
    if not selected or nested:
        raise UnsafePathError("model cache requires one mount without nested mounts")
    depth = max(row[0] for row in selected)
    matches = [row for row in selected if row[0] == depth]
    if len(matches) != 1:
        raise UnsafePathError("model cache containing mount is ambiguous")
    _, mount_id, major_minor, point = matches[0]
    if major_minor != f"{os.major(device)}:{os.minor(device)}":
        raise UnsafePathError("model cache mount device differs")
    return (mount_id, device, point)


class _CacheBinding:
    """Bind this operation to unchanged no-follow root directories and mount."""
    def __init__(self, root, *, device=None, mount_id=None):
        self.root = _cache_root(root)
        if ((device is None) != (mount_id is None)
                or device is not None and (type(device) is not int or device < 0
                                           or type(mount_id) is not int or mount_id <= 0)):
            raise UnsafePathError("model cache requires valid device and mount ID")
        self.chain = {}
        self.mount = None
        self.expected = (device, mount_id)
        self.check()

    def check(self):
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise UnsafePathError("model cache requires descriptor-anchored no-follow access")
        descriptor = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        current = Path(self.root.anchor)
        observed = {}
        try:
            observed[str(current)] = (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino)
            for part in self.root.parts[1:]:
                try:
                    following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                except FileNotFoundError:
                    break
                os.close(descriptor)
                descriptor = following
                current /= part
                metadata = os.fstat(descriptor)
                observed[str(current)] = (metadata.st_dev, metadata.st_ino)
            if any(observed.get(path) != identity for path, identity in self.chain.items()):
                raise UnsafePathError("model cache directory binding changed")
            self.chain.update(observed)
            mount = _mount_identity(self.root)
            if self.mount is not None and mount != self.mount:
                raise UnsafePathError("model cache mount/device binding changed")
            device, mount_id = self.expected
            if device is not None and (mount[0] != mount_id or mount[1] != device):
                raise UnsafePathError("model cache differs from authenticated mount/device")
            self.mount = mount
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise UnsafePathError("model cache path changed to an unsafe entry") from exc
            raise
        finally:
            os.close(descriptor)


def _require_cache_capacity(binding, required_bytes, *, deadline):
    """Advisory per-model staging check on the bound cache filesystem.

    A cache hit needs no new cache bytes. A miss or corrupt replacement needs
    one full verified temporary while any old cache entry remains untouched.
    This observes available space; it cannot reserve space against other writers.
    """
    _check_deadline(deadline)
    binding.check()
    directory = os.open(binding.root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in binding.root.parts[1:]:
            try:
                following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            except FileNotFoundError:
                break
            os.close(directory)
            directory = following
        if os.fstat(directory).st_dev != binding.mount[1]:
            raise UnsafePathError("model cache capacity device differs from bound filesystem")
        space = os.fstatvfs(directory)
        binding.check()
        _check_deadline(deadline)
        if space.f_bavail * space.f_frsize < required_bytes:
            raise DownloadError("model cache capacity cannot stage the exact locked model")
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePathError("model cache capacity path changed to an unsafe entry") from exc
        raise
    finally:
        os.close(directory)


def _cache_directory(binding: _CacheBinding, relative: PurePosixPath, *, create=False) -> int | None:
    binding.check()
    directory = os.open(binding.root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        root_parts = binding.root.parts[1:]
        for index, component in enumerate((*root_parts, *relative.parts[:-1])):
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
            # Ancestors can belong to another device; cache root and children cannot.
            if index >= len(root_parts) - 1 and os.fstat(directory).st_dev != binding.mount[1]:
                raise UnsafePathError("model cache directory device changed")
        binding.check()
        result, directory = directory, None
        return result
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePathError("model cache path changed to an unsafe entry") from exc
        raise
    finally:
        if directory is not None:
            os.close(directory)


def _cache_entry(directory, name, *, missing=True):
    try:
        metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        if missing:
            return
        raise
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise UnsafePathError("model cache entry must be regular without hard links or symlinks")


def _check_cache_directory(binding, relative, directory):
    """Rewalk before publication so renamed directories cannot receive writes."""
    current = _cache_directory(binding, relative)
    if current is None:
        raise UnsafePathError("model cache directory disappeared")
    try:
        before, after = os.fstat(directory), os.fstat(current)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise UnsafePathError("model cache directory binding changed")
    finally:
        os.close(current)


def _open_cache_source(binding, relative):
    directory = _cache_directory(binding, relative)
    if directory is None:
        return None
    try:
        _cache_entry(directory, relative.name)
        source = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            metadata = os.fstat(source)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_dev != binding.mount[1]:
                raise UnsafePathError("model cache entry must be regular on the bound device")
            _check_cache_directory(binding, relative, directory)
        except BaseException:
            os.close(source)
            raise
        return source
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePathError("model cache path changed to an unsafe entry") from exc
        raise
    finally:
        os.close(directory)


def _copy_cached_model(model, *, binding, relative, destination, deadline):
    source_fd = _open_cache_source(binding, relative)
    if source_fd is None:
        return False
    temporary = None
    try:
        with os.fdopen(source_fd, "rb") as source:
            if os.fstat(source.fileno()).st_size != model.bytes:
                return False
            temporary_fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.cache-", dir=destination.parent)
            temporary = Path(temporary_name)
            digest, count = hashlib.sha256(), 0
            with os.fdopen(temporary_fd, "wb") as output:
                while True:
                    _check_deadline(deadline)
                    binding.check()
                    block = source.read(min(_READ_SIZE, model.bytes - count + 1))
                    _check_deadline(deadline)
                    binding.check()
                    if not block:
                        break
                    count += len(block)
                    if count > model.bytes:
                        return False
                    digest.update(block)
                    output.write(block)
                metadata = os.fstat(source.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise UnsafePathError("model cache entry acquired an unsafe hard link")
                if metadata.st_size != model.bytes or count != model.bytes or digest.hexdigest() != model.sha256:
                    return False
                output.flush()
                os.fsync(output.fileno())
            _check_deadline(deadline)
            binding.check()
            current = _open_cache_source(binding, relative)
            if current is None:
                raise UnsafePathError("model cache source disappeared")
            try:
                before, after = os.fstat(source.fileno()), os.fstat(current)
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    raise UnsafePathError("model cache source path binding changed")
            finally:
                os.close(current)
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
            _check_deadline(deadline)
            return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_cached_model(model, *, binding, relative, destination, deadline):
    """Rehash verified local bytes into a same-filesystem temporary, then publish."""
    _require_cache_capacity(binding, model.bytes, deadline=deadline)
    directory = _cache_directory(binding, relative, create=True)
    temporary = f".{relative.name}.cache-{uuid.uuid4().hex}"
    try:
        _cache_entry(directory, relative.name)
        source_fd = _open_local_cache_source(destination)
        with os.fdopen(source_fd, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size != model.bytes:
                raise UnsafePathError("verified local cache source is no longer a regular locked file")
            output_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            digest, count = hashlib.sha256(), 0
            with os.fdopen(output_fd, "wb") as output:
                while True:
                    _check_deadline(deadline)
                    binding.check()
                    block = source.read(min(_READ_SIZE, model.bytes - count + 1))
                    _check_deadline(deadline)
                    binding.check()
                    if not block:
                        break
                    count += len(block)
                    if count > model.bytes:
                        raise IntegrityError("local cache source exceeds locked size")
                    digest.update(block)
                    output.write(block)
                metadata = os.fstat(source.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                        or metadata.st_size != model.bytes or count != model.bytes or digest.hexdigest() != model.sha256):
                    raise IntegrityError("local cache source differs from locked size or SHA-256")
                output.flush()
                os.fsync(output.fileno())
                published = os.fstat(output.fileno())
                if (not stat.S_ISREG(published.st_mode) or published.st_nlink != 1
                        or published.st_dev != binding.mount[1] or published.st_size != model.bytes):
                    raise UnsafePathError("model cache temporary acquired an unsafe identity")
            _check_deadline(deadline)
            _check_cache_directory(binding, relative, directory)
            _cache_entry(directory, relative.name)
            _cache_entry(directory, temporary, missing=False)
            observed = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
            if (observed.st_dev, observed.st_ino) != (published.st_dev, published.st_ino):
                raise UnsafePathError("model cache temporary path binding changed")
            os.replace(temporary, relative.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
            _cache_entry(directory, relative.name, missing=False)
            _check_cache_directory(binding, relative, directory)
            _check_deadline(deadline)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def _open_local_cache_source(path):
    """Read verified local bytes without following a raced-in ancestor link."""
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePathError("local cache source path must not traverse a symlink") from exc
        raise
    finally:
        os.close(directory)


def download_model(
    model: ModelSpec,
    *,
    models_root: TargetPath,
    deadline: Deadline,
    opener=None,
    model_cache_root: TargetPath | None = None,
    model_cache_device: int | None = None,
    model_cache_mount_id: int | None = None,
) -> DownloadResult:
    """Verify local/cache bytes or fetch one locked model with exact ranges.

    The optional persistent cache is keyed by exact immutable lock entry. Both
    reuse and atomic writeback verify full bytes; all engine custody stays local.
    """
    return _download_model(model, models_root=models_root, deadline=deadline,
                           opener=opener, range_bytes=_RANGE_SIZE,
                           model_cache_root=model_cache_root, model_cache_device=model_cache_device,
                           model_cache_mount_id=model_cache_mount_id)


def _download_model(
    model: ModelSpec,
    *,
    models_root: TargetPath,
    deadline: Deadline,
    opener=None,
    range_bytes: int,
    model_cache_root: TargetPath | None = None,
    model_cache_device: int | None = None,
    model_cache_mount_id: int | None = None,
) -> DownloadResult:
    relative, url = _validate_model(model)
    if type(range_bytes) is not int or range_bytes <= 0 or range_bytes > _RANGE_SIZE:
        raise ModelLockValidationError("model range size must be within the strict 64 MiB bound")
    _check_deadline(deadline)
    root, destination = _root_and_destination(models_root, relative)
    if model_cache_root is not None:
        cache = _cache_root(model_cache_root)
        if root == cache or cache in root.parents or root in cache.parents:
            raise UnsafePathError("model cache and local models_root must not overlap")
    elif model_cache_device is not None or model_cache_mount_id is not None:
        raise UnsafePathError("model cache binding requires a cache root")
    root.mkdir(parents=True, exist_ok=True)
    # Recheck after mkdir to catch a symlinked parent introduced before use.
    _, destination = _root_and_destination(models_root, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _, destination = _root_and_destination(models_root, relative)
    if destination.exists():
        if _matches(destination, model, deadline=deadline):
            _check_deadline(deadline)
            return DownloadResult(model=model.name, path=str(destination), action="skipped",
                                  bytes=model.bytes, sha256=model.sha256, verified=True,
                                  resumed_from_bytes=0)
        raise IntegrityError("existing model integrity check failed its locked size or SHA-256")

    binding = None if model_cache_root is None else _CacheBinding(
        model_cache_root, device=model_cache_device, mount_id=model_cache_mount_id)
    cache_relative = None if binding is None else _cache_relative(model)
    part = destination.with_name(destination.name + ".part")
    chunk = destination.with_name(destination.name + ".chunk")
    for temporary in (part, chunk):
        if temporary.is_symlink():
            raise UnsafePathError("model temporary path must not be a symlink")
        if temporary.exists() and not temporary.is_file():
            raise UnsafePathError("model temporary path must be a regular file")
        if temporary.exists() and temporary.stat().st_nlink != 1:
            raise UnsafePathError("model temporary path must not have multiple hard links")
    if binding is not None and _copy_cached_model(
        model, binding=binding, relative=cache_relative,
        destination=destination, deadline=deadline,
    ):
        return DownloadResult(model=model.name, path=str(destination), action="downloaded",
                              bytes=model.bytes, sha256=model.sha256, verified=True,
                              resumed_from_bytes=0)
    if binding is not None:
        _require_cache_capacity(binding, model.bytes, deadline=deadline)
    if chunk.exists():
        # .chunk is never a committed prefix; it can be discarded safely.
        chunk.unlink()
    if part.exists() and part.stat().st_size > model.bytes:
        _quarantine(part)
    if part.exists() and _looks_like_legacy_redirect(part, model.bytes):
        _quarantine(part)
    resumed_from = part.stat().st_size if part.exists() else 0
    if resumed_from == model.bytes:
        if _matches(part, model, deadline=deadline):
            _fsync_file(part)
            _check_deadline(deadline)
            os.replace(part, destination)
            _fsync_directory(destination.parent)
            _check_deadline(deadline)
            if binding is not None:
                _write_cached_model(model, binding=binding, relative=cache_relative, destination=destination, deadline=deadline)
            return DownloadResult(model=model.name, path=str(destination), action="resumed",
                                  bytes=model.bytes, sha256=model.sha256, verified=True,
                                  resumed_from_bytes=resumed_from)
        part.unlink()
        raise IntegrityError("model SHA-256 mismatch; corrupt .part removed")
    if part.exists() and part.stat().st_size > model.bytes:
        raise IntegrityError("model partial exceeds its locked byte count")

    opener = urllib.request.urlopen if opener is None else opener
    failures = 0
    while not part.exists() or part.stat().st_size < model.bytes:
        remaining = _check_deadline(deadline)
        offset = part.stat().st_size if part.exists() else 0
        end = min(model.bytes, offset + range_bytes) - 1
        request = urllib.request.Request(url, headers={
            "Range": f"bytes={offset}-{end}", "Accept-Encoding": "identity",
        })
        try:
            with opener(request, timeout=min(60.0, remaining)) as response:
                expected_range = f"bytes {offset}-{end}/{model.bytes}"
                if (getattr(response, "status", None) != 206
                        or response.headers.get("Content-Range") != expected_range):
                    raise DownloadError("server did not honor the exact model byte range")
                expected_count = end - offset + 1
                count = 0
                with chunk.open("wb") as output:
                    while True:
                        _check_deadline(deadline)
                        block = response.read(min(_READ_SIZE, expected_count - count + 1))
                        _check_deadline(deadline)
                        if not block:
                            break
                        count += len(block)
                        if count > expected_count:
                            raise DownloadError("server returned an oversized model range")
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if count != expected_count:
                    raise DownloadError("server returned a truncated model range")
            # Append only after a complete response with the exact range.
            with part.open("ab") as output, chunk.open("rb") as source:
                while True:
                    _check_deadline(deadline)
                    block = source.read(_READ_SIZE)
                    if not block:
                        break
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            failures = 0
        except DeadlineExceeded:
            # A1 makes DeadlineExceeded a DownloadError subtype. It must never
            # be retried or wrapped as a generic failed transfer.
            raise
        except (OSError, DownloadError) as exc:
            failures += 1
            chunk.unlink(missing_ok=True)
            remaining = _check_deadline(deadline)
            if failures >= 3:
                raise DownloadError("model range failed; verified .part prefix retained for resume") from exc
            time.sleep(min(2.0, remaining))
        finally:
            chunk.unlink(missing_ok=True)

    if not _matches(part, model, deadline=deadline):
        part.unlink(missing_ok=True)
        raise IntegrityError("model SHA-256 mismatch; corrupt .part removed")
    _check_deadline(deadline)
    os.replace(part, destination)
    _fsync_file(destination)
    _fsync_directory(destination.parent)
    _check_deadline(deadline)
    if binding is not None:
        _write_cached_model(model, binding=binding, relative=cache_relative, destination=destination, deadline=deadline)
    return DownloadResult(model=model.name, path=str(destination),
                          action="resumed" if resumed_from else "downloaded",
                          bytes=model.bytes, sha256=model.sha256, verified=True,
                          resumed_from_bytes=resumed_from)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
