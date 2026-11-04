"""Run Runtime's advisory busy inspector on detached SQLite bytes only."""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path

from .core import absolute, write_json


def _capture(path, destination=None):
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("The previous SQLite file is not a regular file.")
    keys = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
            "st_size", "st_mtime_ns", "st_ctime_ns")
    identity = lambda value: tuple(getattr(value, key) for key in keys)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    output = None
    try:
        if identity(os.fstat(descriptor)) != identity(before):
            raise ValueError("Astrid changed during the busy check.")
        if destination is not None:
            output = destination.open("xb")
            os.chmod(destination, 0o600)
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            if output is not None:
                output.write(chunk)
        if (identity(before) != identity(os.fstat(descriptor))
                or identity(before) != identity(path.lstat())):
            raise ValueError("Astrid changed during the busy check.")
        return identity(before), digest.hexdigest()
    finally:
        if output is not None:
            output.close()
        os.close(descriptor)


def inspect(backend, data):
    """Copy DB/sidecar bytes through read-only FDs, then use preflight_stop.

    Runtime's current inspector opens SQLite read-only but may change source
    WAL/SHM bookkeeping. Never call it on the retained source. These repeated
    scans detect observed changes; this remains an advisory check, not an
    atomic task-admission fence. Uncertain captures stay private and retained.
    """
    from astrid_runtime.bootstrap import preflight_stop
    from astrid_runtime.paths import RuntimePaths

    realm = backend.source_workspace(data)
    realm_root = absolute(realm["realm_root"])
    names = ["realm.sqlite3" + suffix for suffix in ("", "-wal", "-shm", "-journal")]
    parent = absolute(Path(tempfile.gettempdir()).resolve())
    if parent == data or parent.is_relative_to(data):
        raise ValueError("The busy-check scratch directory overlaps the previous workspace.")
    scratch = Path(tempfile.mkdtemp(prefix="astrid-copy-preflight-", dir=parent))
    private_realm = scratch / "runtime" / "realms" / realm["realm_id"]
    certain = False
    try:
        private_realm.mkdir(parents=True, mode=0o700)
        before = {name: _capture(absolute(realm_root / name)) for name in names}
        if before[names[0]] is None:
            raise ValueError("The previous SQLite database is missing.")
        captured = {name: _capture(absolute(realm_root / name), private_realm / name) for name in names}
        after = {name: _capture(absolute(realm_root / name)) for name in names}
        if before != captured or before != after:
            raise ValueError(f"Astrid changed during the busy check; the uncertain capture is kept at {scratch}.")
        certain = True
        write_json(scratch / "runtime" / "catalog.json", {
            "version": 1, "selected_realm_id": realm["realm_id"], "source_profiles": {},
            "realms": [{"realm_id": realm["realm_id"], "display_name": "Astrid Workspace",
                        "data_root": str(private_realm)}]})
        preflight_stop(RuntimePaths.current(data_root=scratch))
    finally:
        if certain:
            shutil.rmtree(scratch)
