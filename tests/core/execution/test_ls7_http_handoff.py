"""Exercise the negotiated Agent/Runtime handoff over authenticated HTTP.

Only the external command child is replaced. Discovery, preparation, descriptor
registration, admission, claim, lease observation, settlement and SDK following
use the production paths. Native sync attribution has its own Runtime fixture.
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.sdk import invocation
from astrid.sdk.remote import RemoteTasks
from astrid.sdk.workspace_client import WorkspaceClient
from astrid_runtime_client import ApiError, WorkspaceClient as GeneratedClient
from tests.test_generic_host import _write_manifest
from tests.test_generic_host_result_json import _success_boundary


@pytest.mark.parametrize("lose_result_response", [False, True])
def test_registered_thin_request_wakes_host_and_sdk_without_extra_commit(tmp_path, monkeypatch, real_runtime, lose_result_response):
    path = _write_manifest(tmp_path / "pack")
    definition = json.loads(path.read_text())
    ports = [{"name": "mode", "type": "string", "required": True, "aliases": ["text"]},
             {"name": "seed", "type": "integer", "required": False, "default": 17}]
    definition["inputs"] = ports
    definition["metadata"].update(output_result_manifest=True, runtime_resolution={
        "entry": {"id": "echo", "kind": "text", "inputs": ports},
        "admission": {"spec": {}},
        "bindings": {p["name"]: ["spec", "inputs", p["name"]] for p in ports},
        "placement_generation": "echo-placement-1", "project_id": None,
    })
    path.write_text(json.dumps(definition))
    daemon = real_runtime()
    worker_id = "ls7-http-host"
    token, _ = daemon.credentials.provision(worker_id, [
        "handshake", "worker:register", "worker:execute", "tasks:read", "objects:read", "objects:write",
    ])
    transport = RuntimeProtocolClient(daemon.endpoint, token)
    host = GenericPackHost(pack_roots=[tmp_path / "pack"], client=transport,
                           executor_id=worker_id, attempt_root=tmp_path / "run")
    case = SimpleNamespace(host=host, root=tmp_path / "run", legacy_bytes=None)
    _success_boundary(case, monkeypatch)
    assert [record.id for record in host.discover()] == ["test.echo"]
    outcome = []
    thread = None
    statements = []
    counts = {"claim": 0, "heartbeat": 0}
    try:
        host.register()
        assert transport.supports_task_waits()
        registered = daemon.service.store.conn.execute(
            "SELECT COUNT(*) FROM capabilities").fetchone()[0]
        assert registered == 1
        for name, key in (("claim_next", "claim"), ("heartbeat", "heartbeat")):
            original = getattr(transport, name)
            def counted(*args, _call=original, _key=key, **kwargs):
                counts[_key] += 1
                return _call(*args, **kwargs)
            monkeypatch.setattr(transport, name, counted)

        entered = threading.Event()
        changes = daemon.service._changes()
        wait = changes.condition.wait
        def observed_wait(timeout):
            entered.set()
            return wait(timeout)
        monkeypatch.setattr(changes.condition, "wait", observed_wait)

        def serve():
            try:
                outcome.append(host.run(max_tasks=1, poll_seconds=10))
            except BaseException as exc:
                outcome.append(exc)
        thread = threading.Thread(target=serve)
        thread.start()
        assert entered.wait(3), "registered host never entered the Runtime work wait"
        assert counts == {"claim": 0, "heartbeat": 0}
        daemon.service.store.conn.set_trace_callback(statements.append)

        def forbidden(*args, **kwargs):
            pytest.fail("negotiated caller/worker success path polled or slept")
        reconnect_delays = []
        def record_sleep(seconds):
            if not lose_result_response:
                forbidden()
            reconnect_delays.append(seconds)
        monkeypatch.setattr(generic_host.time, "sleep", record_sleep)
        caller = GeneratedClient(daemon.endpoint, daemon.token)
        thin = {"format": "runtime.resolution/v1", "entry_id": "echo", "values": {"text": "precise"}}
        admitted = caller.admit_thin_task(thin, idempotency_key="http-thin-once")
        facade = WorkspaceClient(daemon.endpoint, daemon.token)
        tasks = RemoteTasks(facade)
        handshakes = []
        handshake = facade._generated.handshake
        def record_handshake(*args, **kwargs):
            handshakes.append(args)
            return handshake(*args, **kwargs)
        monkeypatch.setattr(facade._generated, "handshake", record_handshake)
        if lose_result_response:
            wait_for_task = facade._generated.wait_for_task
            delivered = []
            def lose_once(*args, **kwargs):
                snapshot = wait_for_task(*args, **kwargs)
                if not delivered:
                    delivered.append(snapshot)
                    raise ApiError(0, "transport_error", "fixture lost a completed read response")
                return snapshot
            monkeypatch.setattr(facade._generated, "wait_for_task", lose_once)
        monkeypatch.setattr(tasks, "show", forbidden)
        monkeypatch.setattr(tasks, "list_managed_outputs", forbidden)
        result, ok, attempt = invocation._wait_for_kernel_task_loop(
            SimpleNamespace(tasks=tasks), task_id=admitted["task_id"], run_id=admitted["run_id"],
            timeout_seconds=8, poll_seconds=10, read_managed_outputs=True, expects_outputs=True,
        )
        thread.join(3)
        assert not thread.is_alive() and outcome and isinstance(outcome[0], list), outcome
        assert ok and attempt and result["ok"], result
        assert counts == {"claim": 1, "heartbeat": 0}
        assert len(handshakes) == (2 if lose_result_response else 1)
        assert reconnect_delays == ([.05] if lose_result_response else [])
        assert sum(sql == "COMMIT" for sql in statements) == 3
        assert len(result["managed_outputs"]) == 1
        task = daemon.service.store.get_task(admitted["task_id"])["task"]
        assert task["spec"]["spec"]["inputs"] == {"mode": "precise", "seed": 17}
        assert caller.admit_thin_task(thin, idempotency_key="http-thin-once") == admitted
        assert sum(sql == "COMMIT" for sql in statements) == 3
        assert daemon.service.store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        host.shutdown()
        if thread is not None:
            thread.join(3)
        daemon.stop()
    assert not transport._task_observers
