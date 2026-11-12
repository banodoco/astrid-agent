"""Focused managed preparation contention behavior with a real process lock."""
from __future__ import annotations

import multiprocessing
import json
import time
from pathlib import Path

import pytest

from astrid.core.generation.engine_contracts import EngineBusy
from astrid.core.generation.engine_platforms import platform_adapter
from astrid.core.generation.managed_preparation import ManagedEnginePreparation


def _hold_install_lock(root: str, acquired, release):
    adapter = platform_adapter("linux-x86_64-cu130")
    with adapter.local_install_lock(root, deadline=time.monotonic() + 10):
        acquired.set()
        release.wait(10)


class _InstallingReceiptContext:
    def __init__(self, receipt_path):
        self.receipt_path = receipt_path
        self.reads = []

    def file_fact(self, path, *, hash_bytes, deadline):
        assert path == self.receipt_path
        return type("Fact", (), {"exists": Path(path).exists()})()

    def read_json(self, path, *, deadline):
        self.reads.append((path, deadline))
        assert path == self.receipt_path
        return json.loads(Path(path).read_text())


def test_managed_prepare_refuses_when_install_lock_is_held_for_install(tmp_path):
    root = tmp_path / "engine"
    workflow = tmp_path / "workflow.json"
    workflow.write_text("{}\n")
    receipt_path = root / ".astrid-engine/install.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text('{"status":"installing"}')
    context = _InstallingReceiptContext(str(receipt_path))
    preparation = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    preparation.root = str(root)
    preparation.deadline = time.monotonic() + 5
    preparation.cancelled = lambda: False
    preparation.adapter = platform_adapter("linux-x86_64-cu130")
    preparation.context = context

    process_context = multiprocessing.get_context("fork")
    acquired = process_context.Event()
    release = process_context.Event()
    owner = process_context.Process(target=_hold_install_lock, args=(preparation.root, acquired, release))
    owner.start()
    try:
        assert acquired.wait(5), "separate process did not acquire install lock"
        with pytest.raises(EngineBusy, match="the local engine is being updated; try again when astrid engines install finishes"):
            preparation._prepare(workflow, execution_identity="unused", inputs=None,
                                 canonical_workflow=None, input_digests=None)
        assert len(context.reads) == 1
        assert context.reads[0][0] == str(root / ".astrid-engine/install.json")
    finally:
        release.set()
        owner.join(5)
        if owner.is_alive():
            owner.terminate()
            owner.join()
    assert owner.exitcode == 0


def test_job_job_lock_contention_waits_for_managed_preparation(tmp_path):
    import threading

    root = str(tmp_path / "engine")
    workflow = tmp_path / "workflow.json"
    workflow.write_text("{}\n")
    process_context = multiprocessing.get_context("fork")
    acquired = process_context.Event()
    release = process_context.Event()
    owner = process_context.Process(target=_hold_install_lock,
                                    args=(root, acquired, release))
    owner.start()
    assert acquired.wait(5), "first job did not acquire the real install lock"

    reached_receipt = threading.Event()
    class AfterLock(Exception):
        pass
    class Context:
        def file_fact(self, path, *, hash_bytes, deadline):
            return type("Fact", (), {"exists": False})()
        def read_json(self, path, *, deadline):
            reached_receipt.set()
            raise AfterLock

    preparation = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    preparation.root = root
    preparation.deadline = time.monotonic() + 8
    preparation.cancelled = lambda: False
    preparation.check = lambda: None
    preparation.adapter = platform_adapter("linux-x86_64-cu130")
    preparation.context = Context()
    errors = []
    def prepare():
        try:
            preparation._prepare(workflow, execution_identity="unused", inputs=None,
                                 canonical_workflow=None, input_digests=None)
        except BaseException as exc:
            errors.append(exc)
    waiter = threading.Thread(target=prepare, daemon=True)
    try:
        waiter.start()
        assert not reached_receipt.wait(0.2), "preparation passed the held job lock"
        assert owner.is_alive(), "first job ended before releasing its lock"
    finally:
        release.set()
        owner.join(5)
    try:
        assert reached_receipt.wait(5), "preparation did not continue after the first job released"
        waiter.join(5)
        assert len(errors) == 1 and isinstance(errors[0], AfterLock)
        assert preparation.lock is not None
    finally:
        if preparation.lock is not None:
            preparation.lock.__exit__(None, None, None)
        if waiter.is_alive():
            waiter.join(5)
    assert owner.exitcode == 0


def test_install_refuses_live_job_and_reports_real_queue_state(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    from astrid.core.generation.engine_execution import create_local_backend
    from astrid.core.generation.engine_recipe import load_recipe
    from astrid.core.generation.engine_runner import install_engine, plan_install

    observed = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append(self.path)
            body = json.dumps({"queue_running": [[0, "running-job"]],
                               "queue_pending": [[1, "pending-job"]]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    root = tmp_path / "engine"
    receipt = root / ".astrid-engine" / "install.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"status":"installed"}')
    config = root / "runtime" / "out" / "sessions" / "comfyui" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"port": server.server_port}))
    context, _adapter = create_local_backend(engine="comfyui", platform="linux-x86_64-cu130",
                                              placement="local", root=str(root),
                                              deadline=time.monotonic() + 5)
    process_context = multiprocessing.get_context("fork")
    acquired = process_context.Event()
    release = process_context.Event()
    owner = process_context.Process(target=_hold_install_lock,
                                    args=(str(root), acquired, release))
    owner.start()
    try:
        assert acquired.wait(5), "separate job did not acquire the real install lock"
        thread.start()
        plan = plan_install(load_recipe("comfyui", "linux-x86_64-cu130"),
                            root=str(root), placement="local")
        with pytest.raises(EngineBusy, match="running-job.*pending-job"):
            install_engine(plan, context=context, adapter=context.adapter,
                           deadline=time.monotonic() + 2)
        assert observed == ["/queue"]
        assert owner.is_alive(), "install refusal disturbed the lock-owning job"
    finally:
        release.set()
        owner.join(5)
        if owner.is_alive():
            owner.terminate()
            owner.join()
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert owner.exitcode == 0



def test_lost_prompt_acceptance_keeps_job_lock_and_blocks_install(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
    from astrid.core.generation.engine_execution import create_local_backend
    from astrid.core.generation.engine_contracts import TransportError
    from astrid.core.generation.engine_recipe import load_recipe
    from astrid.core.generation.engine_runner import install_engine, plan_install

    posted = threading.Event()
    allow_acceptance = threading.Event()
    accepted = threading.Event()
    queue_reads = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/prompt"
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            posted.set()
            # The server accepted the request body but delays its reply beyond
            # the client's real socket timeout, leaving acceptance uncertain.
            assert allow_acceptance.wait(3), "test did not release the delayed acceptance"
            accepted.set()
            body = b'{"prompt_id":"accepted-after-timeout"}'
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass
        def do_GET(self):
            queue_reads.append(self.path)
            if accepted.is_set():
                body = b'{"queue_running":[[0,"accepted-after-timeout"]],"queue_pending":[]}'
            else:
                body = b'{"queue_running":[],"queue_pending":[]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr("astrid.core.generation.backends.comfy_target.HTTP_TRANSFER_SECONDS", 0.1)
    root = tmp_path / "engine"
    receipt = root / ".astrid-engine" / "install.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"status":"installed"}')
    config = root / "runtime" / "out" / "sessions" / "comfyui" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"port": server.server_port}))
    context, _adapter = create_local_backend(engine="comfyui", platform="linux-x86_64-cu130",
                                              placement="local", root=str(root),
                                              deadline=time.monotonic() + 5)
    client = ComfyTargetClient(ComfyTarget(f"http://127.0.0.1:{server.server_port}", "managed"))
    preparation = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    preparation.root = str(root)
    preparation.adapter = context.adapter
    preparation.deadline = time.monotonic() + 8
    preparation.lock = preparation.adapter.local_install_lock(
        str(root), deadline=preparation.deadline)
    preparation.lock.__enter__()
    try:
        with pytest.raises(TransportError):
            client.submit_prompt({"prompt": {"1": {"class_type": "Test", "inputs": {}}}})
        assert posted.wait(2), "loopback server did not receive the prompt"
        execution_root = tmp_path / "execution"
        journal = execution_root / ".vibecomfy-runtime/out/runs/owned/attempt.json"
        journal.parent.mkdir(parents=True)
        journal.write_text(json.dumps({"queue_acceptance": {"status": "unknown", "prompt_id": None}}))
        preparation.report = {"endpoint": client.target.url}
        preparation.uncertain = False
        preparation._preparing_engine = False
        with pytest.raises(ValueError, match="prompt acceptance remains unconfirmed"):
            preparation.close(execution_root=execution_root, worker_drained=True)
        assert preparation.uncertain is True
        assert preparation.lock is not None, "uncertain acceptance must retain the preparation lock"
        plan = plan_install(load_recipe("comfyui", "linux-x86_64-cu130"),
                            root=str(root), placement="local")
        with pytest.raises(EngineBusy, match="held by a running or preparing job"):
            install_engine(plan, context=context, adapter=context.adapter,
                           deadline=time.monotonic() + 2)
        assert queue_reads == ["/queue"]
        assert preparation.lock is not None
        allow_acceptance.set()
        assert accepted.wait(2), "server did not record the delayed prompt acceptance"
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/queue", timeout=2) as response:
            assert json.loads(response.read()) == {
                "queue_running": [[0, "accepted-after-timeout"]], "queue_pending": []}
        with pytest.raises(TransportError, match="already attempted"):
            client.submit_prompt({"prompt": {}})
    finally:
        allow_acceptance.set()
        preparation.lock.__exit__(None, None, None)
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_stale_installing_receipt_does_not_block_without_lock_contention(tmp_path):
    from astrid.core.generation.engine_contracts import IntegrityError

    root = str(tmp_path / "engine")
    workflow = tmp_path / "workflow.json"
    workflow.write_text("{}\n")
    receipt_path = str(Path(root) / ".astrid-engine/install.json")
    class Context:
        def __init__(self):
            self.reads = []
        def file_fact(self, path, *, hash_bytes, deadline):
            return type("Fact", (), {"exists": True})()
        def read_json(self, path, *, deadline):
            self.reads.append(path)
            assert path == receipt_path
            return {"status": "installing"}

    preparation = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    preparation.root = root
    preparation.deadline = time.monotonic() + 5
    preparation.cancelled = lambda: False
    preparation.check = lambda: None
    preparation.adapter = platform_adapter("linux-x86_64-cu130")
    from astrid.core.generation.engine_recipe import load_recipe
    preparation.recipe = load_recipe("comfyui", "linux-x86_64-cu130")
    preparation.context = Context()
    try:
        with pytest.raises(IntegrityError):
            preparation._prepare(workflow, execution_identity="unused", inputs=None,
                                 canonical_workflow=None, input_digests=None)
        assert preparation.context.reads == [receipt_path]
    finally:
        if getattr(preparation, "lock", None) is not None:
            preparation.lock.__exit__(None, None, None)
