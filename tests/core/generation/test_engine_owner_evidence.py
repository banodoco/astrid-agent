"""Focused evidence checks for retiring an engine owner marker."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from astrid.core.execution import process_group
from astrid.core.generation import engine_custody as custody
from astrid.core.generation.engine_contracts import CustodyUnresolved, EndpointOwnershipError


def _identity(root):
    from tests.core.generation.test_engine_custody import _identity as make_identity
    return make_identity(root)


def _marker(root, *, process=None, owner_id="a" * 32):
    control = Path(root, ".astrid-engine", "session")
    control.mkdir(parents=True, exist_ok=True)
    control.parent.chmod(0o700)
    control.chmod(0o700)
    value = {"identity": _identity(root), "owner_id": owner_id}
    if process is not None:
        value["process"] = process
    marker_path = control / "session.json"
    marker_path.write_text(json.dumps(value))
    marker_path.chmod(0o600)
    (control / "owner.lock").touch(mode=0o600)
    return control, value


def test_owner_process_state_distinguishes_live_and_reused_pid():
    current = process_group.process_birth_identity()
    pid = os.getpid()
    assert custody._owner_process_state({"process": {"pid": pid, "birth_id": current}}) == ("alive", "pid_start_ticks")
    other = current.rsplit(":", 1)[0] + ":0"
    state, evidence = custody._owner_process_state({"process": {"pid": pid, "birth_id": other}})
    assert (state, evidence) == ("dead", "pid_start_ticks")
    boot, ticks = current.split(":", 1)
    changed_boot = ("0" if boot[0] != "0" else "1") + boot[1:] + ":" + ticks
    assert custody._owner_process_state({"process": {"pid": pid, "birth_id": changed_boot}}) == ("dead", "boot_id")


def test_dead_child_process_evidence_and_different_pod(monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    birth = process_group.process_birth_identity(child.pid)
    child.terminate()
    child.wait(timeout=5)
    assert custody._owner_process_state({"process": {"pid": child.pid, "birth_id": birth}}) == ("dead", "pid_start_ticks")
    monkeypatch.setenv("RUNPOD_POD_ID", "current-pod")
    assert custody._owner_process_state({"pod_id": "recorded-pod"}) == ("dead", "different_pod")


def test_flock_reliability_uses_kernel_mount_type(tmp_path, monkeypatch):
    lockfile = tmp_path / "owner.lock"
    lockfile.touch()
    fd = os.open(lockfile, os.O_RDWR)
    try:
        assert custody._flock_reliable(fd) is True
        original_open = open

        def mountinfo(path, *args, **kwargs):
            if path == "/proc/self/mountinfo":
                info = os.fstat(fd)
                return __import__("io").StringIO(
                    f"1 0 {os.major(info.st_dev)}:{os.minor(info.st_dev)} / /mount rw - fuse.test source rw\n")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", mountinfo)
        assert custody._flock_reliable(fd) is False
    finally:
        os.close(fd)


def test_local_flock_can_retire_unknown_legacy_owner(tmp_path, caplog):
    control, _marker_value = _marker(tmp_path)
    client = custody.LinuxEngineServiceClient(**_identity(tmp_path))
    caplog.set_level(logging.WARNING, logger=custody.__name__)
    dirfd = os.open(control, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert client._retire_dead_owner(dirfd, time.monotonic() + 2) is True
    finally:
        os.close(dirfd)
    assert not (control / "session.json").exists()
    assert '"evidence":"flock"' in caplog.text


@pytest.mark.parametrize(("case", "evidence"), [
    ("boot_id", "boot_id"), ("pid_reuse", "pid_start_ticks"),
    ("reaped_child", "pid_start_ticks"), ("different_pod", "different_pod"),
])
def test_positive_process_evidence_retires_on_unreliable_mount(tmp_path, monkeypatch, caplog, case, evidence):
    process = None
    pod_id = None
    if case == "boot_id":
        boot, ticks = process_group.process_birth_identity().split(":", 1)
        changed_boot = ("0" if boot[0] != "0" else "1") + boot[1:]
        process = {"pid": os.getpid(), "birth_id": f"{changed_boot}:{ticks}"}
    elif case == "pid_reuse":
        birth = process_group.process_birth_identity()
        boot, ticks = birth.split(":", 1)
        process = {"pid": os.getpid(), "birth_id": f"{boot}:{int(ticks) + 1}"}
    elif case == "reaped_child":
        child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        birth = process_group.process_birth_identity(child.pid)
        child.terminate()
        child.wait(timeout=5)
        process = {"pid": child.pid, "birth_id": birth}
    else:
        monkeypatch.setenv("RUNPOD_POD_ID", "current-pod")
        pod_id = "recorded-pod"

    control, marker = _marker(tmp_path, process=process)
    if pod_id is not None:
        marker["pod_id"] = pod_id
        marker_path = control / "session.json"
        marker_path.write_text(json.dumps(marker))
        marker_path.chmod(0o600)
    monkeypatch.setattr(custody, "_flock_reliable", lambda _fd: False)
    client = custody.LinuxEngineServiceClient(**_identity(tmp_path))
    caplog.set_level(logging.WARNING, logger=custody.__name__)
    dirfd = os.open(control, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert client._retire_dead_owner(dirfd, time.monotonic() + 2) is True
    finally:
        os.close(dirfd)
    assert not (control / "session.json").exists()
    assert f'"evidence":"{evidence}"' in caplog.text
    assert marker["owner_id"] not in caplog.text


def test_unreadable_same_boot_pid_refuses_without_retiring_or_signalling(tmp_path, monkeypatch):
    birth = process_group.process_birth_identity()
    control, marker = _marker(tmp_path, process={"pid": os.getpid(), "birth_id": birth})
    monkeypatch.setattr(custody, "_flock_reliable", lambda _fd: False)
    original = custody.process_birth_identity

    def unreadable(pid=None):
        if pid == os.getpid():
            return ""
        return original(pid)

    monkeypatch.setattr(custody, "process_birth_identity", unreadable)
    monkeypatch.setattr(custody, "held_child_birth_identity", lambda _pid: None)
    client = custody.LinuxEngineServiceClient(**_identity(tmp_path))
    with pytest.raises(CustodyUnresolved) as err:
        client.call("probe", {}, deadline=time.monotonic() + 2)
    details = err.value.custody_details
    assert details["owner_state"] == "unknown" and details["evidence"] == "none"
    assert json.loads((control / "session.json").read_text()) == marker
    assert Path(f"/proc/{os.getpid()}").exists()


def test_live_unreachable_owner_and_free_lock_is_not_retired(tmp_path):
    control, marker = _marker(tmp_path, process={"pid": os.getpid(), "birth_id": process_group.process_birth_identity()})
    client = custody.LinuxEngineServiceClient(**_identity(tmp_path))
    with pytest.raises(CustodyUnresolved):
        client.call("probe", {}, deadline=time.monotonic() + 1)
    assert json.loads((control / "session.json").read_text()) == marker
    assert (control / "session.json").exists()


def test_unknown_legacy_owner_on_unreliable_mount_refuses_without_evidence(tmp_path, monkeypatch):
    control, marker = _marker(tmp_path)
    marker["identity"] = {**marker["identity"], "helper_sha256": "f" * 64}
    marker_path = control / "session.json"
    marker_path.write_text(json.dumps(marker))
    marker_path.chmod(0o600)
    client = custody.LinuxEngineServiceClient(**_identity(tmp_path))
    monkeypatch.setattr(custody, "_flock_reliable", lambda _fd: False)
    with pytest.raises(EndpointOwnershipError) as err:
        client.call("probe", {}, deadline=time.monotonic() + 1)
    details = err.value.custody_details
    assert details["reason"] in {"owner_unknown", "identity_mismatch"}
    assert details["evidence"] == "none"
    assert json.loads((control / "session.json").read_text()) == marker


def test_held_real_owner_lock_refuses_even_when_marker_pid_is_dead(tmp_path):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=5)
    control, marker = _marker(tmp_path, process={"pid": dead.pid, "birth_id": "00000000-0000-0000-0000-000000000000:1"})
    client = custody.LinuxEngineServiceClient(**_identity(tmp_path))
    holder_code = "import fcntl,sys,time;f=open(sys.argv[1],'r+');fcntl.flock(f,fcntl.LOCK_EX);print('locked',flush=True);time.sleep(30)"
    holder = subprocess.Popen([sys.executable, "-c", holder_code, str(control / "owner.lock")],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        dirfd = os.open(control, os.O_RDONLY | os.O_DIRECTORY)
        try:
            assert client._retire_dead_owner(dirfd, time.monotonic() + 2) is False
        finally:
            os.close(dirfd)
        assert json.loads((control / "session.json").read_text()) == marker
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_owner_marker_contains_supervisor_evidence_and_logs_no_secrets(tmp_path, caplog):
    from tests.core.generation.test_engine_custody import _request
    root = tmp_path / "install"
    root.mkdir()
    client = custody.LinuxEngineServiceClient(**_identity(root))
    caplog.set_level(logging.WARNING, logger=custody.__name__)
    assert client.run(_request(root, "print('ok')")).exit_code == 0
    marker = json.loads((Path(client.control) / "session.json").read_text())
    process = marker["process"]
    assert process == {"pid": client._spawned.pid,
                       "birth_id": process_group.process_birth_identity(client._spawned.pid)}
    changed = custody.LinuxEngineServiceClient(**{**client.identity, "helper_sha256": "f" * 64})
    with pytest.raises(EndpointOwnershipError):
        changed.call("probe", {}, deadline=time.monotonic() + 2)
    assert marker["owner_id"] not in caplog.text
    client.stop_owned(deadline=time.monotonic() + 10)
