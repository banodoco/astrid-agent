"""Bound repeated foreground cleanup without abandoning its retained owner.

The child runs the actual host signal handler and shutdown implementation on
one thread. Windows Job calls are modeled as in the five serialization controls;
these tests are not a native Windows receipt.
"""
from __future__ import annotations

import ctypes
import faulthandler
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from runtime_protocol import platform
from astrid.core.execution import task_scope
from tests.core.execution.test_task_scope_stop_serialization import (
    _ControlledKernel,
    windows_cleanup_boundary,
)


@pytest.mark.parametrize("boundary", ["native", "journal"])
def test_foreground_shutdown_reentry_preserves_owner_and_returns(
    boundary, tmp_path, owned_process_groups,
):
    """A stuck before-version is confined to a disposable, retained child."""
    output = tmp_path / "foreground-result.json"
    root = str(Path(__file__).resolve().parents[3])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH"))))
    child = owned_process_groups.popen(
        [sys.executable, "-u", str(Path(__file__).resolve()), boundary, str(output)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=sys.platform != "win32",
    )
    try:
        stdout, stderr = child.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        child.kill()
        stdout, stderr = child.communicate(timeout=5)
        pytest.fail(f"foreground cleanup did not return within 8s\n{stdout}\n{stderr}")
    assert child.returncode == 0, (stdout, stderr)
    result = json.loads(output.read_text())
    print(json.dumps(result, sort_keys=True))
    assert result["callback_threads"] == [result["foreground_thread"]]
    assert result["elapsed"] < 1.0, result
    assert result["retained_before_retry"] and result["journal_before_retry"]
    assert result["retry_status"] == "settled" and result["journal_after_retry"] == []
    assert result["close_count"] == 1 and result["shutdown_observed"]


@pytest.mark.parametrize("pause_operation", ["TerminateJobObject", "QueryInformationJobObject"])
def test_contended_stop_returns_unresolved_without_taking_custody(
    windows_cleanup_boundary, monkeypatch, pause_operation,
):
    kernel, owner = windows_cleanup_boundary(pause_operation)
    process = owner(101)
    entry = {"pgid": process.pid, "owner": "retained"}
    entries, journal_calls = [entry], []

    def edit_journal(path, edit):
        journal_calls.append(path)
        entries[:] = edit(entries)

    monkeypatch.setattr(task_scope, "_journal", "retained-scope-journal")
    monkeypatch.setattr(task_scope, "_journal_edit", edit_journal)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(task_scope.stop, process, grace=0)
        try:
            assert kernel.paused.wait(5), kernel.events
            before = list(kernel.events)
            started = time.monotonic()
            second = pool.submit(task_scope.stop, process, grace=0, kill_wait=0.05)
            try:
                report = second.result(timeout=1)
            except TimeoutError:
                pytest.fail("contending cleanup exceeded its bounded wait")
            elapsed = time.monotonic() - started
            assert report.status == "unresolved" and "in progress" in report.detail
            assert elapsed < 1 and not first.done()
            assert kernel.events == before and process._runtime_job.handle == 101
            assert entries == [entry] and journal_calls == []
        finally:
            kernel.resume.set()
        settled = first.result(timeout=5)
    assert settled.status == "settled"
    assert entries == [] and journal_calls == ["retained-scope-journal"]
    assert process._runtime_tree_drained and process._runtime_job.handle is None


def test_lock_installation_contention_has_a_bounded_wait(windows_cleanup_boundary):
    kernel, owner = windows_cleanup_boundary()
    process = owner(101)
    guard = task_scope._stop_lock_guard
    with ThreadPoolExecutor(max_workers=1) as pool:
        with guard:
            result = pool.submit(task_scope.stop, process, grace=0, kill_wait=0.05)
            try:
                report = result.result(timeout=1)
            except TimeoutError:
                pytest.fail("cleanup lock installation exceeded its bounded wait")
            assert report.status == "unresolved" and "in progress" in report.detail
            assert kernel.events == [] and process._runtime_job.handle == 101
        assert task_scope.stop(process, grace=0).status == "settled"


class _RetainedOwner(SimpleNamespace):
    __hash__ = object.__hash__


def _foreground_probe(boundary: str, output: Path) -> None:
    from astrid.core.execution import generic_host

    host = generic_host.GenericPackHost(pack_roots=[], client=SimpleNamespace())
    foreground_thread = threading.get_ident()
    callback_threads = []

    def foreground_shutdown(signum, _frame):
        callback_threads.append(threading.get_ident())
        generic_host._handle_host_shutdown_signal(host, signum)

    restore_handlers = platform.install_stop_handlers(foreground_shutdown)
    faulthandler.dump_traceback_later(3)
    job = object.__new__(platform._Job)
    job.handle = 101
    process = _RetainedOwner(
        pid=123, args=["worker"], _runtime_job=job, _astrid_task_scope=True,
        poll=lambda: 1, wait=lambda **_: 1,
    )
    host._track_process(process)
    entries = [{"pgid": process.pid, "owner": "retained"}]
    kernel = _ControlledKernel(None)
    original_call = kernel.call
    requested = False

    def request_once():
        nonlocal requested
        if not requested:
            requested = True
            signal.raise_signal(signal.SIGTERM)

    def native_call(name, handle, *args):
        if boundary == "native":
            request_once()
        return original_call(name, handle, *args)

    def edit_journal(_path, edit):
        if boundary == "journal":
            request_once()
        entries[:] = edit(entries)

    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(task_scope, "sys", SimpleNamespace(platform="win32"))
            patch.setattr(platform, "sys", SimpleNamespace(platform="win32"))
            patch.setattr(platform, "_kernel", lambda: kernel)
            patch.setattr(kernel, "call", native_call)
            patch.setattr(task_scope, "_journal", "retained-scope-journal")
            patch.setattr(task_scope, "_journal_edit", edit_journal)
            patch.setattr(ctypes, "get_last_error", lambda: 6, raising=False)
            patch.setattr(ctypes, "WinError", lambda code: OSError(f"WinError {code}"), raising=False)
            started = time.monotonic()
            if boundary == "native":
                first = task_scope.stop(process, grace=0)
                assert first.status == "unresolved", first
                assert job.handle == 101 and not getattr(process, "_runtime_tree_drained", False)
                assert kernel.events == []
            else:
                with pytest.raises(generic_host.CleanupUncertainError):
                    task_scope.stop(process, grace=0)
                assert process._runtime_tree_drained and job.handle is None
            elapsed = time.monotonic() - started
            retained = process in host._active_processes
            journal_before = list(entries)
            assert retained and journal_before and host._unresolved_task_scopes
            assert not getattr(process, "_astrid_scope_stopping", False)
            retry = task_scope.stop(process, grace=0)
            close_count = sum(event[:2] == ["CloseHandle", "return"] for event in kernel.events)
            assert retry.status == "settled" and entries == [] and close_count == 1
            host.shutdown()
            output.write_text(json.dumps({
                "boundary": boundary,
                "model": "real foreground signal and host shutdown; modeled Windows Job calls",
                "foreground_thread": foreground_thread, "callback_threads": callback_threads,
                "elapsed": elapsed, "retained_before_retry": retained,
                "journal_before_retry": journal_before, "journal_after_retry": entries,
                "retry_status": retry.status, "close_count": close_count,
                "shutdown_observed": host._shutdown.is_set(), "native_events": kernel.events,
            }, sort_keys=True))
    finally:
        faulthandler.cancel_dump_traceback_later()
        restore_handlers()


if __name__ == "__main__":
    _foreground_probe(sys.argv[1], Path(sys.argv[2]))
