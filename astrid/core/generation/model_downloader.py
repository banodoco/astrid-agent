"""Verified, resumable downloads for immutable engine model locks.

The target owns ``models_root``. This module performs no controller-to-target
path translation and accepts only a pinned model identity from engine_contracts.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import time
import urllib.request
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


def download_model(
    model: ModelSpec,
    *,
    models_root: TargetPath,
    deadline: Deadline,
    opener=None,
) -> DownloadResult:
    """Fetch and verify one locked model, resuming only complete exact ranges."""
    return _download_model(model, models_root=models_root, deadline=deadline,
                           opener=opener, range_bytes=_RANGE_SIZE)


def _download_model(
    model: ModelSpec,
    *,
    models_root: TargetPath,
    deadline: Deadline,
    opener=None,
    range_bytes: int,
) -> DownloadResult:
    relative, url = _validate_model(model)
    if type(range_bytes) is not int or range_bytes <= 0 or range_bytes > _RANGE_SIZE:
        raise ModelLockValidationError("model range size must be within the strict 64 MiB bound")
    _check_deadline(deadline)
    root, destination = _root_and_destination(models_root, relative)
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

    part = destination.with_name(destination.name + ".part")
    chunk = destination.with_name(destination.name + ".chunk")
    for temporary in (part, chunk):
        if temporary.is_symlink():
            raise UnsafePathError("model temporary path must not be a symlink")
        if temporary.exists() and not temporary.is_file():
            raise UnsafePathError("model temporary path must be a regular file")
        if temporary.exists() and temporary.stat().st_nlink != 1:
            raise UnsafePathError("model temporary path must not have multiple hard links")
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
