"""Private descriptor-relative filesystem building blocks for Linux contexts.

This is an incomplete A2 slice, not an ExecutionContext or install authority.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import stat
import time
from contextlib import contextmanager
from collections.abc import Mapping

from .engine_contracts import DeadlineExceeded, FileFact, RecipeValidationError, UnsafePathError

_JSON_LIMIT = 4 * 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _check(deadline):
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise RecipeValidationError("deadline must be finite")
    if time.monotonic() >= deadline:
        raise DeadlineExceeded("target filesystem deadline exceeded")


def _parts(path):
    if (not isinstance(path, str) or not path.startswith('/') or path.startswith('//')
            or '\\' in path or ':' in path or any(ord(c) < 32 for c in path)
            or len(os.fsencode(path)) > 4096):
        raise UnsafePathError("unsafe target path")
    parts = path[1:].split('/')
    if not parts or any(p in ('', '.', '..') or len(os.fsencode(p)) > 255 for p in parts):
        raise UnsafePathError("unsafe target path")
    return parts


class _TargetFiles:
    def __init__(self, root):
        self._root = tuple(_parts(root))

    def _relative(self, path):
        parts = _parts(path)
        if tuple(parts[:len(self._root)]) != self._root:
            raise UnsafePathError("target path outside install root")
        return parts

    @contextmanager
    def _directory(self, parts, deadline, *, create=False):
        _check(deadline)
        fd = os.open('/', _DIR_FLAGS)
        try:
            for part in parts:
                _check(deadline)
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                        os.fsync(fd)
                    except FileExistsError:
                        pass
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
            _check(deadline)
            yield fd
        except FileNotFoundError:
            raise
        except OSError:
            raise UnsafePathError("unsafe or inaccessible target path") from None
        finally:
            os.close(fd)

    def ensure_dir(self, path, *, deadline):
        parts = self._relative(path)
        with self._directory(parts, deadline, create=True):
            pass

    @staticmethod
    def _regular(fd):
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UnsafePathError("target is not a singly linked regular file")
        return info

    def file_fact(self, path, *, hash_bytes, deadline):
        parts = self._relative(path)
        if type(hash_bytes) is not bool:
            raise RecipeValidationError("hash selection must be boolean")
        try:
            with self._directory(parts[:-1], deadline) as parent:
                info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    return FileFact(True, False, True, None, None)
                if not stat.S_ISREG(info.st_mode):
                    return FileFact(True, False, False, None, None)
                fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent)
                try:
                    before = self._regular(fd)
                    if (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
                        raise UnsafePathError("target changed during inspection")
                    digest = None
                    if hash_bytes:
                        h = hashlib.sha256()
                        count = 0
                        while True:
                            _check(deadline)
                            block = os.read(fd, 1024 * 1024)
                            if not block:
                                break
                            count += len(block)
                            h.update(block)
                        after = self._regular(fd)
                        if (count != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                            raise UnsafePathError("target changed during inspection")
                        digest = h.hexdigest()
                    _check(deadline)
                    return FileFact(True, True, False, before.st_size, digest)
                finally:
                    os.close(fd)
        except FileNotFoundError:
            return FileFact(False, False, False, None, None)

    def read_json(self, path, *, deadline):
        parts = self._relative(path)
        try:
            with self._directory(parts[:-1], deadline) as parent:
                fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent)
                try:
                    info = self._regular(fd)
                    if info.st_size > _JSON_LIMIT:
                        raise RecipeValidationError("target JSON exceeds limit")
                    data = bytearray()
                    while True:
                        _check(deadline)
                        block = os.read(fd, min(65536, _JSON_LIMIT + 1 - len(data)))
                        if not block:
                            break
                        data.extend(block)
                        if len(data) > _JSON_LIMIT:
                            raise RecipeValidationError("target JSON exceeds limit")
                    after = self._regular(fd)
                    if (len(data) != info.st_size or (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                        raise UnsafePathError("target changed during read")
                finally:
                    os.close(fd)
        except FileNotFoundError:
            raise RecipeValidationError("target JSON is missing") from None
        try:
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError("duplicate key")
                    result[key] = value
                return result
            value = json.loads(data.decode('utf-8'), object_pairs_hook=pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (UnicodeError, ValueError, RecursionError):
            raise RecipeValidationError("invalid target JSON") from None
        if not isinstance(value, dict):
            raise RecipeValidationError("target JSON must be an object")
        _check(deadline)
        return value

    def write_json_atomic(self, path, value, *, deadline):
        parts = self._relative(path)
        if not isinstance(value, Mapping):
            raise RecipeValidationError("target JSON must be an object")
        try:
            data = json.dumps(dict(value), sort_keys=True, separators=(',', ':'),
                              ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise RecipeValidationError("invalid target JSON") from None
        if len(data) > _JSON_LIMIT:
            raise RecipeValidationError("target JSON exceeds limit")
        try:
            with self._directory(parts[:-1], deadline) as parent:
                try:
                    info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise UnsafePathError("unsafe target JSON leaf")
                except FileNotFoundError:
                    pass
                temp = '.a2-' + secrets.token_hex(16) + '.tmp'
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=parent)
                try:
                    view = memoryview(data)
                    while view:
                        _check(deadline)
                        written = os.write(fd, view)
                        if written <= 0:
                            raise UnsafePathError("target write made no progress")
                        view = view[written:]
                    os.fsync(fd)
                    _check(deadline)
                    os.replace(temp, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                    os.fsync(parent)
                    _check(deadline)
                finally:
                    os.close(fd)
                    try:
                        os.unlink(temp, dir_fd=parent)
                    except FileNotFoundError:
                        pass
        except FileNotFoundError:
            raise UnsafePathError("target parent is missing") from None
