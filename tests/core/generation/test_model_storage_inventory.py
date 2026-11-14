"""Inventory interruption drains the owned process group, including orphaned children."""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager

import pytest

from astrid.core.generation import model_storage


@contextmanager
def _subreaper():
    # The helper's orphan becomes this test's child, so even the failing
    # pre-fix control can kill and reap it without leaving a PID 1 zombie.
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0  # PR_GET_CHILD_SUBREAPER
    assert libc.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    try:
        yield
    finally:
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0


def _reap(pid, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        found, status = os.waitpid(pid, os.WNOHANG)
        if found:
            return status
        time.sleep(.01)
    return None


@pytest.mark.parametrize('interrupt', ['deadline', 'cancel'])
def test_inventory_kills_child_after_command_leader_exits(tmp_path, monkeypatch, interrupt):
    pid_path = tmp_path / 'child.pid'
    script = '''import os, pathlib, sys, time
pid_path = pathlib.Path(sys.argv[1])
if os.fork() == 0:
    pid_path.write_text(str(os.getpid()))
    print("child holds inventory stdout open", flush=True)
    time.sleep(30)
    os._exit(0)
while not pid_path.exists() or not pid_path.stat().st_size:
    time.sleep(.001)
os._exit(0)
'''
    popen = subprocess.Popen
    owned = []

    def command(*args, **kwargs):
        process = popen(*args, **kwargs)
        owned.append(process)
        return process

    monkeypatch.setattr(model_storage.subprocess, 'Popen', command)
    with _subreaper():
        child = None
        status = None
        try:
            output = model_storage._inventory_command(
                [sys.executable, '-c', script, str(pid_path)],
                deadline=time.monotonic() + (.4 if interrupt == 'deadline' else 2),
                cancelled=lambda: interrupt == 'cancel' and bool(owned) and owned[0].poll() is not None,
            )
            assert owned[0].returncode == 0, 'the leader must exit before interruption'
            child = int(pid_path.read_text())
            status = _reap(child, .5)
            assert status is not None, 'inventory descendant survived its deadline/cancellation'
            assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
            assert 'could not gather complete listing' in output
            assert 'cleanup unconfirmed' in output or not _group_exists(owned[0].pid)
            if 'cleanup unconfirmed' in output:
                assert owned[0].pid in model_storage._INVENTORY_CUSTODY
        finally:
            if owned:
                try:
                    os.killpg(owned[0].pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                owned[0].wait(timeout=1)
            if child is None and pid_path.exists():
                child = int(pid_path.read_text())
            if child is not None and status is None:
                status = _reap(child, 1)
                assert status is not None, 'test harness could not reap its inventory child'
            if owned and not _group_exists(owned[0].pid):
                getattr(model_storage, '_INVENTORY_CUSTODY', {}).pop(owned[0].pid, None)


def _group_exists(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def test_inventory_reports_an_unreapable_command(monkeypatch):
    read_fd, write_fd = os.pipe()
    os.close(write_fd)

    class Unreapable:
        pid = 1 << 30
        returncode = 0

        def __init__(self):
            self.stdout = os.fdopen(read_fd, 'rb')

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            raise subprocess.TimeoutExpired('inventory', timeout)

    process = Unreapable()
    monkeypatch.setattr(model_storage.subprocess, 'Popen', lambda *args, **kwargs: process)
    try:
        output = model_storage._inventory_command(['fake-inventory'], deadline=time.monotonic() + 1,
                                                  cancelled=lambda: False)
        assert 'could not reap inventory command' in output
        assert 'cleanup unconfirmed' in output
        assert model_storage._INVENTORY_CUSTODY[process.pid] is process
    finally:
        getattr(model_storage, '_INVENTORY_CUSTODY', {}).pop(process.pid, None)
