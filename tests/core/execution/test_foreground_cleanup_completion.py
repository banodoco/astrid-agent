"""The installed foreground callback must let its cleanup owner publish proof.

CLI, host shutdown, task scope and pinned Runtime cleanup are real. The Runtime
client/bootstrap and Windows kernel calls are explicit fixture boundaries. A
real signal interrupts the foreground thread after native close, before Runtime
can mark its retained tree drained. This is not a native Windows receipt.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("boundary", ["close-return", "drained-record", "native-error"])
def test_foreground_cli_keeps_first_owner_through_completion(
    boundary, tmp_path, owned_process_groups,
):
    output = tmp_path / "completion.json"
    root = str(Path(__file__).resolve().parents[3])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH"))))
    child = owned_process_groups.popen(
        [sys.executable, "-u", str(Path(__file__).resolve()), boundary, str(output)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=sys.platform != "win32",
    )
    try:
        stdout, stderr = child.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        child.kill()
        stdout, stderr = child.communicate(timeout=5)
        pytest.fail(f"foreground completion did not finish\n{stdout}\n{stderr}")
    assert child.returncode == 0, (stdout, stderr)
    result = json.loads(output.read_text())
    print(json.dumps(result, sort_keys=True))
    if boundary == "native-error":
        assert result["first_status"] == "unresolved" and "WinError 6" in result["first_detail"], result
        # CleanupUncertainError is the public alias of CustodyError.
        assert result["cli_exception"] == "CustodyError" and result["cli_cleanup_uncertain"], result
        assert result["journal"] and result["handle"] == 101 and not result["drained"], result
        assert result["close_count"] == 0, result
    else:
        assert result["first_status"] == "settled" and result["first_detail"] == "", result
        assert result["cli_exit"] == 0 and result["cli_exception"] is None, result
        assert result["drained"] and result["handle"] is None and result["journal"] == [], result
        assert result["close_count"] == 1, result
        names = [event["event"] for event in result["events"]]
        assert names.index("native_closed") < names.index("drained_published") < names.index("owner_journal_updated"), result
        assert names.index("owner_journal_updated") < names.index("shutdown_finished") < names.index("cli_finished"), result
        assert result["completion_threads"] == [result["owner_thread"], result["owner_thread"]], result
    assert result["signals_sent"] == 2 and result["shutdown_calls"] == 1, result
    assert result["signal_seconds"] < 1.0, result
    assert result["retained_owner"] and result["shutdown_finished_before_cli_exit"], result
    assert result["shutdown_thread"] != result["owner_thread"], result


def _probe(boundary: str, output: Path) -> None:
    from runtime_protocol import platform
    from astrid.core.execution import generic_host, task_scope
    from astrid.core._shared.boot_manifest import build_manifest, load_boot_manifest_hash
    from tests.core.execution.test_generic_host_actions_v3 import pack
    from tests.core.execution.test_task_scope_stop_serialization import _ControlledKernel

    events = []
    origin = time.monotonic()
    owner_thread = threading.get_ident()
    completion_threads = []

    def event(name):
        events.append({"event": name, "thread": threading.get_ident(),
                       "elapsed_seconds": time.monotonic() - origin})

    class Owner(SimpleNamespace):
        __hash__ = object.__hash__

        def __setattr__(self, name, value):
            super().__setattr__(name, value)
            if name == "_runtime_tree_drained" and value:
                completion_threads.append(threading.get_ident())
                event("drained_published")

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("ASTRID_SOURCE_STATE", str(output.parent / "absent-state.json"))
        for name in ("ASTRID_PACKS_PATH", "ASTRID_EXECUTION_TARGET_JSON", "ASTRID_OUTPUT_ROOT",
                     "ASTRID_HOST_READINESS_PROFILE_PATH", "ASTRID_PACK_HOST_SERVICE_MODE"):
            patch.delenv(name, raising=False)
        root, host, runtime, _, _ = pack(output.parent)
        boot = output.parent / "boot-manifest.json"
        boot.write_text(json.dumps(build_manifest(registry={}, fixtures=(), profile_order=())))
        digest = "sha256:" + load_boot_manifest_hash(boot, support_root=output.parent)
        credential = output.parent / "worker.token"
        credential.write_text("fixture-worker")
        credential.chmod(0o600)
        patch.setattr(sys, "argv", ["host", "run", "--pack-root", str(root),
            "--runtime-endpoint", "http://127.0.0.1:1", "--credential-file", str(credential), "--once"])
        patch.setattr(generic_host, "_compose_cli_boot_manifest", lambda *a: (boot, digest))
        patch.setattr(generic_host, "_startup_identity_attestation", lambda **k: {"source": {"closure_digest": "3" * 64}})
        patch.setattr(generic_host, "RuntimeProtocolClient", lambda *a, credential_file=None: runtime)
        patch.setattr(generic_host, "GenericPackHost", lambda **k: host)

        kernel = _ControlledKernel(None)
        kernel.deny_termination = boundary == "native-error"
        job = object.__new__(platform._Job)
        job.handle = 101
        process = Owner(pid=123, args=["worker"], _runtime_job=job, _astrid_task_scope=True,
                        poll=lambda: 1, wait=lambda **k: 1)
        host._track_process(process)
        entries = [{"pgid": process.pid, "owner": "retained"}]
        first = None
        signals_sent = 0
        signal_seconds = 0.0
        shutdown_calls = 0
        shutdown_thread = None
        shutdown_finished = threading.Event()
        original_shutdown = host.shutdown
        original_call = kernel.call
        original_drained = platform.tree_drained

        def shutdown():
            nonlocal shutdown_calls, shutdown_thread
            shutdown_calls += 1
            shutdown_thread = threading.get_ident()
            event("shutdown_entered")
            try:
                original_shutdown()
            finally:
                event("shutdown_finished")
                shutdown_finished.set()

        def request_signals():
            nonlocal signals_sent, signal_seconds
            if signals_sent:
                return
            started = time.monotonic()
            for _ in range(2):
                signals_sent += 1
                event("signal_requested")
                signal.raise_signal(signal.SIGTERM)
                event("signal_returned")
            signal_seconds = time.monotonic() - started

        def native_call(name, handle, *args):
            result = original_call(name, handle, *args)
            if name == "CloseHandle":
                event("native_closed")
                if boundary == "close-return":
                    request_signals()
            if name == "TerminateJobObject" and boundary == "native-error":
                request_signals()
            return result

        def tree_drained(owned):
            result = original_drained(owned)
            if boundary == "drained-record":
                request_signals()
            return result

        def edit_journal(_path, edit):
            entries[:] = edit(entries)
            if threading.get_ident() == owner_thread:
                completion_threads.append(owner_thread)
                event("owner_journal_updated")

        def run(**_kwargs):
            nonlocal first
            # Keep the model installed until _cli has joined requested cleanup.
            patch.setattr(task_scope, "sys", SimpleNamespace(platform="win32"))
            patch.setattr(platform, "sys", SimpleNamespace(platform="win32"))
            patch.setattr(platform, "_kernel", lambda: kernel)
            patch.setattr(kernel, "call", native_call)
            patch.setattr(platform, "tree_drained", tree_drained)
            patch.setattr(task_scope, "_journal", "retained-scope-journal")
            patch.setattr(task_scope, "_journal_edit", edit_journal)
            patch.setattr(ctypes, "get_last_error", lambda: 6, raising=False)
            patch.setattr(ctypes, "WinError", lambda code: OSError(f"WinError {code}"), raising=False)
            first = task_scope.stop(process, grace=0)
            event("first_owner_returned")
            return {"cleanup": first.status}

        patch.setattr(host, "shutdown", shutdown)
        patch.setattr(host, "run", run)
        cli_exit, cli_exception = None, None
        cli_cleanup_uncertain = False
        try:
            cli_exit = generic_host._cli()
        except BaseException as exc:
            cli_exception = type(exc).__name__
            cli_cleanup_uncertain = type(exc) is generic_host.CleanupUncertainError
        event("cli_finished")
        result = {
            "boundary": boundary, "model": "actual foreground CLI/signal; modeled Windows kernel/journal",
            "owner_thread": owner_thread, "shutdown_thread": shutdown_thread,
            "first_status": first.status if first is not None else None,
            "first_detail": first.detail if first is not None else None,
            "cli_exit": cli_exit, "cli_exception": cli_exception,
            "cli_cleanup_uncertain": cli_cleanup_uncertain,
            "signals_sent": signals_sent, "signal_seconds": signal_seconds,
            "shutdown_calls": shutdown_calls, "shutdown_finished_before_cli_exit": shutdown_finished.is_set(),
            "retained_owner": process in host._active_processes,
            "handle": job.handle, "drained": getattr(process, "_runtime_tree_drained", False),
            "journal": list(entries), "completion_threads": completion_threads,
            "close_count": sum(e[:2] == ["CloseHandle", "return"] and e[3] for e in kernel.events),
            "events": list(events), "kernel_events": list(kernel.events),
        }
        output.write_text(json.dumps(result, indent=2))
        # Record any retry after the original result; it cannot repair the receipt.
        if first is not None and first.status != "settled":
            retry = task_scope.stop(process, grace=0)
            result["separate_retry_status"] = retry.status
            result["separate_retry_detail"] = retry.detail
            result["separate_retry_kernel_events"] = kernel.events[len(result["kernel_events"]):]
            output.write_text(json.dumps(result, indent=2))
        if job.handle is not None:
            job.close()


if __name__ == "__main__":
    _probe(sys.argv[1], Path(sys.argv[2]))
