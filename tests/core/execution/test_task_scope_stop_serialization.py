"""Exercise Runtime's Windows cleanup with controlled native-call boundaries.

The kernel below models handle lifetime; it is not a native Windows receipt.
Runtime's stop, signal, Job accounting and Job close implementations remain real.
"""

from __future__ import annotations

import ctypes
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from runtime_protocol import platform
from astrid.core.execution import task_scope


class _NativeCall:
    def __init__(self, kernel, name):
        self.kernel, self.name = kernel, name

    def __call__(self, handle, *args):
        return self.kernel.call(self.name, handle, *args)


class _ControlledKernel:
    def __init__(self, pause_operation):
        self.pause_operation = pause_operation
        self.paused = threading.Event()
        self.resume = threading.Event()
        self.lock = threading.Lock()
        self.live = {101, 102}
        self.events = []
        self.deny_termination = False
        for name in ("TerminateJobObject", "QueryInformationJobObject", "CloseHandle"):
            setattr(self, name, _NativeCall(self, name))

    def call(self, name, handle, *args):
        with self.lock:
            self.events.append([name, "enter", handle])
            pause = name == self.pause_operation
            if pause:
                self.pause_operation = None
        if pause:
            self.paused.set()
            if not self.resume.wait(5):
                raise RuntimeError("test coordinator did not release native boundary")
        with self.lock:
            valid = handle in self.live and not (
                name == "TerminateJobObject" and self.deny_termination
            )
            if valid and name == "CloseHandle":
                self.live.remove(handle)
            if valid and name == "QueryInformationJobObject":
                args[1]._obj.ActiveProcesses = 0
            self.events.append([name, "return", handle, bool(valid)])
            return int(valid)


@pytest.fixture
def windows_cleanup_boundary(monkeypatch):
    kernels, jobs = [], []
    monkeypatch.setattr(task_scope, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(task_scope, "_journal", None)

    def invalid_handle(code):
        error = OSError(f"[WinError {code}] The handle is invalid.")
        error.winerror = code
        return error

    monkeypatch.setattr(ctypes, "WinError", invalid_handle, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 6, raising=False)

    def create(pause_operation=None):
        kernel = _ControlledKernel(pause_operation)
        kernels.append(kernel)
        monkeypatch.setattr(platform, "_kernel", lambda: kernel)

        def owner(handle):
            job = object.__new__(platform._Job)
            job.handle = handle
            jobs.append(job)
            # Deliberately equal numeric PIDs: only the retained owner is custody.
            return SimpleNamespace(pid=123, args=["worker"], _runtime_job=job,
                                   poll=lambda: 1, wait=lambda **_: 1)

        return kernel, owner

    yield create
    for kernel in kernels:
        kernel.resume.set()
    for job in jobs:
        job.close()


@pytest.mark.parametrize("pause_operation", ["TerminateJobObject", "QueryInformationJobObject"])
@pytest.mark.parametrize("same_owner", [True, False], ids=["same-owner", "distinct-owners"])
def test_overlapping_windows_stops_preserve_retained_job(
    windows_cleanup_boundary, pause_operation, same_owner,
):
    kernel, owner = windows_cleanup_boundary(pause_operation)
    first = owner(101)
    second = first if same_owner else owner(102)
    second_started, second_done = threading.Event(), threading.Event()

    def stop_second():
        second_started.set()
        try:
            return task_scope.stop(second, grace=0)
        finally:
            second_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(task_scope.stop, first, grace=0)
        try:
            assert kernel.paused.wait(5), kernel.events
            second_result = pool.submit(stop_second)
            assert second_started.wait(5)
            completed_while_first_paused = second_done.wait(1)
        finally:
            kernel.resume.set()
        reports = [first_result.result(timeout=5), second_result.result(timeout=5)]

    observation = {
        "model": "controlled native boundary; not hosted Windows",
        "same_owner": same_owner,
        "paused_operation": pause_operation,
        "second_completed_while_first_paused": completed_while_first_paused,
        "reports": [{"status": report.status, "detail": report.detail} for report in reports],
        "native_boundary_events": kernel.events,
    }
    print(json.dumps(observation, sort_keys=True))
    assert [report.status for report in reports] == ["settled", "settled"], observation
    assert completed_while_first_paused is not same_owner, observation
    assert first._runtime_job.handle is None and second._runtime_job.handle is None
    closed = [event[2] for event in kernel.events if event[:2] == ["CloseHandle", "return"]]
    assert sorted(closed) == ([101] if same_owner else [101, 102]), observation


def test_windows_stop_retains_failure_and_retries_native_proof(windows_cleanup_boundary, monkeypatch):
    kernel, owner = windows_cleanup_boundary()
    process = owner(101)
    entry = {"pid": process.pid, "pgid": process.pid, "start": "original", "task": "attempt"}
    entries, journal_calls = [entry], []

    def edit_journal(path, edit):
        journal_calls.append(path)
        entries[:] = edit(entries)

    monkeypatch.setattr(task_scope, "_journal", "retained-scope-journal")
    monkeypatch.setattr(task_scope, "_journal_edit", edit_journal)
    kernel.deny_termination = True
    failed = task_scope.stop(process, grace=0)
    assert failed.status == "unresolved" and "[WinError 6]" in failed.detail
    assert entries == [entry] and journal_calls == []
    assert process._runtime_job.handle == 101
    assert not getattr(process, "_runtime_tree_drained", False)

    kernel.deny_termination = False
    recovered = task_scope.stop(process, grace=0)
    assert recovered.status == "settled" and recovered.detail == ""
    assert entries == [] and journal_calls == ["retained-scope-journal"]
    assert process._runtime_tree_drained and process._runtime_job.handle is None
