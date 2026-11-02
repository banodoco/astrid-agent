"""M2-11a Luna E/H: test-pack end-to-end session wiring.

Real: GenericPackHost discover/admit/run_task, the admitted worker subprocess
(generic_host_worker in a child interpreter), the inherited socketpair, the
worker-side SessionClient, SessionChannelBroker, and _HostManagedSessionAdapter
(including its host-side compile of raw inputs and the echoed compiled_settings).

Stubbed: only the host edge ``GenericPackHost.invoke_wan_session`` and the
runtime client (``FakeRuntime``), exactly as the existing v3 tests do. The
sessionprobe host profile is registered by this test, since profiles are
host-owned and keyed by managed tool; it reuses the wan2gp host compile.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from astrid.core.execution import session_channel as session_channel_module
from astrid.core.execution.guards import ExecutionGuardPolicy
from astrid.core.execution.executor.runner import SESSION_ACTION_TIMEOUT_SECONDS
from astrid.core.execution.generic_host import GenericPackHost
from astrid.packs.wan2gp.src.driver import compile_host_settings
from tests.core.execution.session_test_profiles import register_test_profile
from tests.test_generic_host import FakeRuntime as _CurrentFakeRuntime


PACK_ID = "sessionprobe"
CAPABILITY_ID = "sessionprobe.probe"
TAMPER = "TAMPER"
# Run deadline kept inside the worker's 900s channel cap. Under the default
# policy (3600s) every managed_tool action is refused at worker start; see the
# report for that production finding. Not xfailed: the repo forbids xfail.
RUN_DEADLINE_SECONDS = 600.0

# The action sees only the public surface: the error type from astrid.sdk.
# It sends RAW inputs; the host profile compiles them and echoes compiled_settings.
ACTION_SOURCE = '''
from astrid.sdk import SessionChannelError

TAMPER = "TAMPER"


def session_probe(*, message, prompt, output_root, timeout_seconds, session):
    progress = session.progress()
    inputs = {"message": message, "prompt": prompt}
    if message == TAMPER:
        # Raw inputs the host profile rejects (unsupported resolution).
        inputs = {**inputs, "resolution": "bogus"}
    report = {
        "session_type": type(session).__name__,
        "output_root": str(output_root),
        "timeout_seconds": timeout_seconds,
        "progress": progress,
        "refused_code": None,
        "evidence": None,
    }
    try:
        report["evidence"] = session.submit(inputs, timeout_seconds=timeout_seconds)
    except SessionChannelError as exc:
        report["refused_code"] = exc.code
    return report
'''


class FakeRuntime(_CurrentFakeRuntime):
    def health(self):
        return {**super().health(), "instance_id": "fixture-instance",
                "runtime_instance_id": "fixture-instance", "coordinator_epoch": "fixture-instance"}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTRID_SOURCE_STATE", str(tmp_path / "absent-state.json"))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)


def _test_pack(tmp_path):
    """A schema-v3 test pack under tmp_path; no shipped pack.yaml is touched."""
    root = tmp_path / "packs" / PACK_ID
    action_path = root / "actions" / "session_probe.py"
    action_path.parent.mkdir(parents=True, exist_ok=True)
    action_path.write_text(ACTION_SOURCE)
    declaration = {
        "description": "Probe the managed session channel from inside the worker.",
        "invocation": {"kind": "python", "path": "actions/session_probe.py", "function": "session_probe"},
        "session": "managed_tool",
        "inputs": [
            {"name": "message", "type": "string", "required": True},
            {"name": "prompt", "type": "string", "required": True},
        ],
        "outputs": {},
        "metadata": {"adapter_family": "cpu", "resource_keys": ["cpu"], "project_scope": "optional"},
    }
    (root / "pack.yaml").write_text(yaml.safe_dump({
        "schema_version": 3, "pack_api": "1.0", "id": PACK_ID, "name": "Session Probe",
        "version": "1.0.0", "actions": {"probe": declaration},
    }))
    return root


def _host_and_definition(tmp_path, monkeypatch, *, policy=None):
    root = _test_pack(tmp_path)
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[root], client=runtime,
                           execution_policy=policy or ExecutionGuardPolicy(deadline_seconds=RUN_DEADLINE_SECONDS))
    records = host.discover()
    assert [record.id for record in records] == [CAPABILITY_ID]
    definition, admission = host.admit(capability_kind="executor", capability_id=CAPABILITY_ID)
    host.register()
    calls = []

    def fake_invoke_wan_session(token, settings, *, timeout, cancelled, progress_path=None):
        calls.append({"settings": dict(settings), "timeout": timeout, "at": time.monotonic(),
                      "progress_path": str(progress_path) if progress_path is not None else None})
        return {"result": {"success": True}, "process_id": 4242, "returncode": 0}

    # Host edge only: the real adapter calls this, nothing above it is replaced.
    monkeypatch.setattr(host, "invoke_wan_session", fake_invoke_wan_session)
    # Host-owned profile for this managed tool (test wiring; packs cannot add one).
    register_test_profile(host, PACK_ID, fake_invoke_wan_session, compile_settings=compile_host_settings)
    return host, runtime, calls


def _task(runtime, task_id, inputs):
    task = {"task": {"id": task_id, "capability": CAPABILITY_ID, "project_id": "demo",
                     "attempt_id": f"{task_id}-attempt", "fence": 1,
                     "spec": {"spec": {"inputs": inputs}}}}
    runtime.tasks[task_id] = task
    return task


@pytest.fixture
def wiring(monkeypatch):
    """Record the real broker and the real child end handed to the worker."""
    brokers, children = [], []
    real_broker = session_channel_module.SessionChannelBroker

    class RecordingBroker(real_broker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            brokers.append(self)

    monkeypatch.setattr(session_channel_module, "SessionChannelBroker", RecordingBroker)
    return brokers, children


def _spy_invoke_capability(host, monkeypatch, children, requests):
    real_invoke = host.invoke_capability

    def spy(*args, **kwargs):
        if kwargs.get("run_channel") is not None:
            children.append(kwargs["run_channel"])
            requests.append(kwargs["request"])
        return real_invoke(*args, **kwargs)

    monkeypatch.setattr(host, "invoke_capability", spy)


def _payload(result):
    assert result["task"]["status"] == "completed", result
    return result


def test_worker_gets_real_session_client_with_host_deadline_and_compiled_settings(tmp_path, monkeypatch, wiring):
    brokers, children = wiring
    requests = []
    host, runtime, calls = _host_and_definition(tmp_path, monkeypatch)
    _spy_invoke_capability(host, monkeypatch, children, requests)
    started = time.monotonic()
    inputs = {"message": "hello", "prompt": "a red fox"}
    result = _payload(host.run_task(_task(runtime, "probe-ok", inputs), lease_token="lease"))
    ended = time.monotonic()

    # 1. Real worker-side client, host-owned output root, deadline-derived timeout.
    receipt = runtime.settlements[-1][2]
    report = receipt["result"]["action_result"]
    assert report["session_type"] == "SessionClient"
    assert report["refused_code"] is None
    assert len(requests) == 1 and Path(report["output_root"]) == Path(requests[0]["out"]).resolve()
    timeout = report["timeout_seconds"]
    assert 0 < timeout <= 900
    assert timeout != SESSION_ACTION_TIMEOUT_SECONDS
    # Derived from the host deadline: remaining budget at worker start, never more than the run deadline.
    assert timeout <= RUN_DEADLINE_SECONDS
    assert timeout >= RUN_DEADLINE_SECONDS - (ended - started) - 5.0
    assert result["task"]["status"] == "completed"

    # 2. Exactly one host invocation, with the host-compiled settings.
    expected = compile_host_settings(inputs)
    assert len(calls) == 1
    assert calls[0]["settings"] == expected
    assert calls[0]["settings"]["prompt"] == "a red fox"
    assert 0 < calls[0]["timeout"] <= timeout

    # 3. The evidence echoes exactly what the host compiled and invoked.
    assert report["evidence"] == {"result": {"success": True}, "process_id": 4242, "returncode": 0,
                                  "compiled_settings": expected}

    # 4. Channel closed after run_task returns: broker closed, both sockets closed.
    assert len(brokers) == 1 and brokers[0].closed is True
    assert brokers[0]._sock.fileno() == -1
    assert len(children) == 1 and children[0].fileno() == -1


def test_worker_submit_with_host_rejected_inputs_is_refused_before_invocation(tmp_path, monkeypatch, wiring):
    brokers, children = wiring
    requests = []
    host, runtime, calls = _host_and_definition(tmp_path, monkeypatch)
    _spy_invoke_capability(host, monkeypatch, children, requests)
    result = _payload(host.run_task(_task(runtime, "probe-tamper", {"message": TAMPER, "prompt": "a red fox"}),
                                    lease_token="lease"))

    report = runtime.settlements[-1][2]["result"]["action_result"]
    # 3. The worker's raw inputs are rejected by the host profile; the action sees the error code.
    assert report["refused_code"] == "invalid_settings"
    assert report["evidence"] is None
    assert calls == []
    assert result["task"]["status"] == "completed"

    # 4. Refusal closes the channel as well.
    assert len(brokers) == 1 and brokers[0].closed is True
    assert brokers[0]._sock.fileno() == -1
    assert len(children) == 1 and children[0].fileno() == -1
