import errno
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from astrid.installer import linux_lock_capture
from astrid.installer.backend import Backend

if sys.platform == "linux":
    import fcntl


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc capture")
def test_parent_snapshot_and_independent_child_contention(tmp_path):
    lock = tmp_path / "astrid-install.lock"
    with lock.open("a+b") as parent:
        fcntl.flock(parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = linux_lock_capture.capture(lock)
        child = subprocess.run([sys.executable, "-c", """
import errno, fcntl, json, os, sys
with open(sys.argv[1], 'a+b') as f:
    st = os.fstat(f.fileno())
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        error = None
    except OSError as exc:
        error = exc.errno
    print(json.dumps(dict(pid=os.getpid(), ppid=os.getppid(), device=st.st_dev,
                          inode=st.st_ino, errno=error)))
""", str(lock)], capture_output=True, text=True, check=True)
        result = json.loads(child.stdout)
        after = linux_lock_capture.capture(lock)
        print("LOCK_CONTROL " + json.dumps({"before": before, "child": result, "after": after}, sort_keys=True))
        identity = os.fstat(parent.fileno())
        assert before["pid"] == result["ppid"] == os.getpid()
        assert (result["device"], result["inode"]) == (identity.st_dev, identity.st_ino)
        assert result["errno"] in (errno.EAGAIN, errno.EACCES)
        assert before["path"]["inode"] == after["path"]["inode"] == identity.st_ino
        assert any(item["fd"] == parent.fileno() for item in before["fds"])
        if before["locks"] != "unavailable" and not before["locks"]["truncated"]:
            assert {"mode": "WRITE", "kernel_pid": os.getpid()} in before["locks"]["matches"]
        assert str(tmp_path) not in json.dumps(before)


def test_capture_unavailable_and_record_failure_do_not_raise(tmp_path):
    assert linux_lock_capture.capture(tmp_path / "missing")["path"] == "unavailable"
    linux_lock_capture.record(tmp_path / "missing", tmp_path / "no-dir" / "log", lambda _: None)
    lock = tmp_path / "private-lock-name"
    lock.write_text("secret contents")
    log = tmp_path / "log"
    protected = []
    linux_lock_capture.record(lock, log, lambda fd: protected.append(fd))
    text = log.read_text()
    assert protected and "secret contents" not in text and str(lock) not in text
    assert json.loads(text.removeprefix("installer-lock-parent "))["path"]["inode"] == lock.stat().st_ino


def test_only_opted_in_upgrade_records_and_failure_is_nonfatal(tmp_path, monkeypatch):
    backend = Backend(tmp_path, tmp_path / "log", tmp_path / "uv")
    monkeypatch.setattr(backend, "run", lambda *args, **kwargs: '{"ok": true}')
    calls = []
    monkeypatch.setattr(linux_lock_capture, "record", lambda *args: calls.append(args))
    monkeypatch.delenv("ASTRID_LINUX_LOCK_CAPTURE", raising=False)
    backend.runtime(None, tmp_path, "upgrade")
    monkeypatch.setenv("ASTRID_LINUX_LOCK_CAPTURE", "1")
    backend.runtime(None, tmp_path, "status")
    assert not calls
    backend.runtime(None, tmp_path, "upgrade")
    assert len(calls) == (1 if sys.platform == "linux" else 0)
    monkeypatch.setattr(linux_lock_capture, "record", lambda *args: (_ for _ in ()).throw(OSError()))
    assert backend.runtime(None, tmp_path, "upgrade") == {"ok": True}
