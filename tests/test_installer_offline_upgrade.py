"""Offline upgrade primitives retain the Runtime's real kernel locks."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.installer.backend import Backend
from astrid.installer.core import write_json


def _probe(root, target):
    script = """
import sys
from pathlib import Path
from runtime_protocol import platform
from runtime_protocol.store import realm_admission_fence
root, target = Path(sys.argv[1]), sys.argv[2]
try:
    if target == 'fence':
        with realm_admission_fence(root, blocking=False):
            pass
    else:
        with (root / 'owner.lock').open('r+b') as lock:
            platform.lock_file(lock.fileno(), blocking=False)
            platform.unlock_file(lock.fileno())
except BlockingIOError:
    sys.exit(12)
"""
    return subprocess.run([sys.executable, "-c", script, str(root), target],
                          env=os.environ.copy(), check=False).returncode


def test_upgrade_lock_holds_and_releases_both_kernel_locks(tmp_path):
    root = tmp_path / "realm"
    root.mkdir()
    backend = Backend(tmp_path / "program", tmp_path / "log", tmp_path / "uv")
    realm = {"realm_root": str(root)}
    with backend.realm_upgrade_lock(realm, timeout=0):
        assert _probe(root, "fence") == 12
        assert _probe(root, "owner") == 12
        with pytest.raises(RuntimeError, match="already held"):
            with backend.realm_upgrade_lock(realm, timeout=0):
                pass
    assert _probe(root, "fence") == 0
    assert _probe(root, "owner") == 0


def test_busy_owner_times_out_and_releases_fence(tmp_path):
    from runtime_protocol import platform

    root = tmp_path / "realm"
    root.mkdir()
    lock = root / "owner.lock"
    lock.touch()
    backend = Backend(tmp_path / "program", tmp_path / "log", tmp_path / "uv")
    with lock.open("r+b") as held:
        platform.lock_file(held.fileno(), blocking=False)
        try:
            with pytest.raises(TimeoutError, match="still in use"):
                with backend.realm_upgrade_lock({"realm_root": str(root)}, timeout=0):
                    pytest.fail("a busy owner must not be admitted")
            assert _probe(root, "fence") == 0
        finally:
            platform.unlock_file(held.fileno())


def test_offline_backup_requires_lock_and_never_starts_runtime(tmp_path):
    data = tmp_path / "data"
    root = data / "runtime" / "realms" / "selected"
    root.mkdir(parents=True)
    (root / "realm.sqlite3").write_bytes(b"state")
    write_json(data / "runtime" / "catalog.json", {"selected_realm_id": "selected"})
    backend = Backend(tmp_path / "program", tmp_path / "log", tmp_path / "uv")
    backend.runtime = lambda *args, **kwargs: pytest.fail("offline backup started Runtime")
    realm = {"realm_root": str(root)}
    with pytest.raises(RuntimeError, match="requires the selected realm"):
        backend.offline_realm_backup(data, realm)
    with backend.realm_upgrade_lock(realm, timeout=0):
        backup = backend.offline_realm_backup(data, realm)
    assert (Path(backup["root"]) / "realm" / "realm.sqlite3").read_bytes() == b"state"
    assert (Path(backup["root"]) / "support" / "runtime" / "catalog.json").is_file()
    assert not (Path(backup["root"]) / "support" / "runtime" / "realms").exists()
    with pytest.raises(ValueError, match="replacement-directory custody"):
        backend.restore(None, data, realm, backup)


def test_manual_upgrade_lock_cannot_be_nested_with_runtime_owner_fence(tmp_path):
    """The WIP lock is not Runtime's reentrant fence and cannot wrap migration."""
    from runtime_protocol.errors import OwnerBusyError
    from runtime_protocol.upgrade import _owner_fence

    root = tmp_path / "realm"
    root.mkdir()
    backend = Backend(tmp_path / "program", tmp_path / "log", tmp_path / "uv")
    with backend.realm_upgrade_lock({"realm_root": str(root)}, timeout=0):
        with pytest.raises(OwnerBusyError, match="another runtime daemon owns this realm"):
            with _owner_fence(root):
                pytest.fail("separate lock acquisition must not be treated as reentrant")
    with _owner_fence(root):
        pass
