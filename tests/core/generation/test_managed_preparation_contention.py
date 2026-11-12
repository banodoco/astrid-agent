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


def _hold_install_lock(root: str, acquired, release, kind="job", content=None):
    adapter = platform_adapter("linux-x86_64-cu130")
    with adapter.local_install_lock(root, deadline=time.monotonic() + 10, kind=kind):
        if content is not None:
            lock_path = Path(root) / ".astrid-engine" / "install.lock"
            with lock_path.open("wb") as lock_file:
                lock_file.write(content)
                lock_file.flush()
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
    owner = process_context.Process(target=_hold_install_lock,
                                    args=(preparation.root, acquired, release, "install"))
    owner.start()
    try:
        assert acquired.wait(5), "separate process did not acquire install lock"
        with pytest.raises(EngineBusy, match="the local engine is being updated; try again when astrid engines install finishes"):
            preparation._prepare(workflow, execution_identity="unused", inputs=None,
                                 canonical_workflow=None, input_digests=None)
        assert context.reads == [], "holder classification must not read the receipt"
    finally:
        release.set()
        owner.join(5)
        if owner.is_alive():
            owner.terminate()
            owner.join()
    assert owner.exitcode == 0


def test_install_lock_kind_refuses_prepare_and_queues_second_installer(tmp_path, monkeypatch):
    import threading

    from astrid.core.generation.engine_execution import create_local_backend
    from astrid.core.generation.engine_recipe import load_recipe
    from astrid.core.generation.engine_runner import install_engine, plan_install

    root = tmp_path / "engine"
    workflow = tmp_path / "workflow.json"
    workflow.write_text("{}\n")
    receipt = root / ".astrid-engine" / "install.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"status":"installed"}')
    deadline = time.monotonic() + 10
    context, adapter = create_local_backend(engine="comfyui", platform="linux-x86_64-cu130",
                                             placement="local", root=str(root), deadline=deadline)
    plan = plan_install(load_recipe("comfyui", "linux-x86_64-cu130"),
                        root=str(root), placement="local")
    owner_barrier = threading.Event()
    release_owner = threading.Event()
    second_stopped = threading.Event()
    stop_calls = []

    class StopAtOwnerBarrier(RuntimeError):
        pass

    def stop_at_barrier(*_args, **_kwargs):
        stop_calls.append(1)
        if len(stop_calls) == 1:
            owner_barrier.set()
            assert release_owner.wait(5), "test did not release installer owner barrier"
        else:
            second_stopped.set()
        raise StopAtOwnerBarrier

    monkeypatch.setattr("astrid.core.generation.engine_custody.stop_engine_for_converge",
                        stop_at_barrier)
    install_errors = []

    def install():
        try:
            install_engine(plan, context=context, adapter=adapter, deadline=time.monotonic() + 8)
        except StopAtOwnerBarrier as exc:
            install_errors.append(exc)

    first = threading.Thread(target=install, daemon=True)
    second = threading.Thread(target=install, daemon=True)
    first.start()
    try:
        assert owner_barrier.wait(5), "installer did not reach owner barrier"
        assert json.loads(receipt.read_text())["status"] == "installed"
        lock_path = root / ".astrid-engine" / "install.lock"
        assert lock_path.read_text() == "install"

        read_receipts = []
        original_read_json = context.read_json
        def track_read_json(path, *, deadline):
            if path == str(receipt):
                read_receipts.append(path)
            return original_read_json(path, deadline=deadline)
        monkeypatch.setattr(context, "read_json", track_read_json)

        preparation = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
        preparation.root = str(root)
        preparation.deadline = time.monotonic() + 5
        preparation.cancelled = lambda: False
        preparation.check = lambda: None
        preparation.adapter = adapter
        preparation.context = context
        preparation.recipe = load_recipe("comfyui", "linux-x86_64-cu130")
        started = time.monotonic()
        with pytest.raises(EngineBusy, match="the local engine is being updated"):
            preparation._prepare(workflow, execution_identity="unused", inputs=None,
                                 canonical_workflow=None, input_digests=None)
        assert time.monotonic() - started < 0.5
        assert read_receipts == [], "contention classification must use lock kind, not the receipt"

        second.start()
        assert not second_stopped.wait(0.2), "second installer passed the held installer lock"
        assert first.is_alive()
    finally:
        release_owner.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert second_stopped.is_set(), "second installer did not acquire after the first released"
    assert len(install_errors) == 2


@pytest.mark.parametrize("holder_content", [b"job", b"", b"\xff"])
def test_job_job_lock_contention_waits_for_managed_preparation(tmp_path, holder_content):
    import threading

    root = str(tmp_path / "engine")
    workflow = tmp_path / "workflow.json"
    workflow.write_text("{}\n")
    process_context = multiprocessing.get_context("fork")
    acquired = process_context.Event()
    release = process_context.Event()
    owner = process_context.Process(target=_hold_install_lock,
                                    args=(root, acquired, release, "job", holder_content))
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


@pytest.mark.parametrize("holder_content", [b"", b"\xff"])
def test_installer_waits_through_empty_or_unreadable_held_kind(tmp_path, holder_content):
    import threading

    from astrid.core.generation.engine_execution import create_local_backend

    root = str(tmp_path / "engine")
    deadline = time.monotonic() + 8
    context, _adapter = create_local_backend(engine="comfyui", platform="linux-x86_64-cu130",
                                              placement="local", root=root, deadline=deadline)
    process_context = multiprocessing.get_context("fork")
    owner_acquired = process_context.Event()
    release_owner = process_context.Event()
    owner = process_context.Process(target=_hold_install_lock,
                                    args=(root, owner_acquired, release_owner,
                                          "install", holder_content))
    owner.start()
    waiter_entered = threading.Event()
    waiter_acquired = threading.Event()
    release_waiter = threading.Event()
    errors = []

    def wait_to_install():
        waiter_entered.set()
        try:
            with context.acquire_install_lock(root, deadline=time.monotonic() + 6,
                                              kind="install"):
                waiter_acquired.set()
                release_waiter.wait(5)
        except BaseException as exc:
            errors.append(exc)

    waiter = threading.Thread(target=wait_to_install, daemon=True)
    try:
        assert owner_acquired.wait(5), "separate installer did not acquire the lock"
        waiter.start()
        assert waiter_entered.wait(2), "second installer did not start lock acquisition"
        assert not waiter_acquired.wait(0.2), "second installer passed the held lock"
        assert owner.is_alive(), "first installer ended before releasing its lock"
    finally:
        release_owner.set()
        owner.join(5)
    try:
        assert waiter_acquired.wait(5), "second installer did not wait through the kind transition"
        assert errors == []
    finally:
        release_waiter.set()
        waiter.join(5)
        if owner.is_alive():
            owner.terminate()
            owner.join()
    assert not waiter.is_alive()
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
