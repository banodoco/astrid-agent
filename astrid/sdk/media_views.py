"""Copy-on-write views of authenticated Runtime CAS objects."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping


def verified_cas_path(client: Any, project: str, digest: str, *, size: int | None = None) -> Path | None:
    """Resolve the existing Runtime location seam and independently verify it."""
    resolver = getattr(client, "get_project_object_location", None)
    if not callable(resolver):
        return None
    try:
        result = resolver(project, digest if digest.startswith("sha256:") else "sha256:" + digest)
        location = result.data if hasattr(result, "data") else result
        if isinstance(location, Mapping) and isinstance(location.get("data"), Mapping):
            location = location["data"]
        if not isinstance(location, Mapping) or location.get("storage") != "runtime_cas" or location.get("verified") is not True:
            return None
        raw_path = location.get("local_path")
        actual_digest = str(location.get("digest") or "").removeprefix("sha256:").lower()
        expected_digest = digest.removeprefix("sha256:").lower()
        if actual_digest != expected_digest or not isinstance(raw_path, str):
            return None
        source = Path(raw_path)
        if not source.is_absolute() or source.is_symlink() or not source.is_file():
            return None
        current = source
        while current != current.parent:
            if current.is_symlink():
                return None
            current = current.parent
        if (
            source.name != expected_digest[2:]
            or source.parent.name != expected_digest[:2]
            or source.parent.parent.name != "sha256"
            or source.parent.parent.parent.name != "cas"
        ):
            return None
        actual_size = source.stat().st_size
        if size is not None and actual_size != size:
            return None
        if hashlib.sha256(source.read_bytes()).hexdigest() != expected_digest:
            return None
        return source
    except (OSError, ValueError, TypeError):
        return None


def clone_or_copy(source: Path, destination: Path, *, digest: str) -> str:
    """Atomically create an independent view; never expose the CAS inode."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(fd)
    temporary = Path(raw)
    strategy = "copy"
    try:
        if sys.platform == "darwin":
            libc = ctypes.CDLL(None, use_errno=True)
            clonefile = getattr(libc, "clonefile", None)
            if clonefile is None:
                raise OSError(errno.ENOTSUP, "APFS clonefile is unavailable")
            clonefile.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int)
            clonefile.restype = ctypes.c_int
            temporary.unlink()
            if clonefile(os.fsencode(source), os.fsencode(temporary), 0) != 0:
                code = ctypes.get_errno()
                if code != errno.EXDEV:
                    raise OSError(code, os.strerror(code), str(source))
                shutil.copyfile(source, temporary)
            else:
                strategy = "apfs-clone"
        else:
            shutil.copyfile(source, temporary)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest.removeprefix("sha256:"):
            raise ValueError("view bytes do not match the Runtime CAS digest")
        os.replace(temporary, destination)
        return strategy
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


__all__ = ["clone_or_copy", "verified_cas_path"]
