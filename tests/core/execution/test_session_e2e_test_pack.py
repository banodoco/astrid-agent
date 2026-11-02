"""M2-11a Luna E: test-pack end-to-end session wiring.

Real: GenericPackHost discover/admit/run_task, the admitted worker subprocess
(generic_host_worker in a child interpreter), the inherited socketpair, the
worker-side SessionClient, SessionChannelBroker, and _HostManagedSessionAdapter
(including its host-side settings recompile).

Stubbed: only the host edge ``GenericPackHost.invoke_wan_session`` and the
runtime client (``FakeRuntime``), exactly as the existing v3 tests do.
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
from tests.test_generic_host import FakeRuntime as _CurrentFakeRuntime


PACK_ID = "sessionprobe"
CAPABILITY_ID = "sessionprobe.probe"
TAMPER = "TAMPER"
# Run deadline kept inside the worker's 900s channel cap. Under the default
# policy (3600s) every managed_tool action is refused at worker start; see the
# report for that production finding. Not xfailed: the repo forbids xfail.
RUN_DEADLINE_SECONDS = 600.0

ACTION_SOURCE = '''
# Public-surface only: the pack may not deep-import astrid.core or wan2gp
# (import_violation). The settings literal mirrors the host's wan2gp compile
# for inputs {message, prompt}; the host recompiles and compares exactly.
SETTINGS_MIRROR = {
    "force_fps": "8", "image_refs_strengths": [], "model": "wan-2.2",
    "model_type": "vace_fun_14B_2_2", "num_inference_steps": 4,
    "resolution": "512x512", "seed": 0, "video_length": 9,
}


def session_probe(*, message, prompt, output_root, timeout_seconds, session):
    progress = session.progress()
    settings = {**SETTINGS_MIRROR, "message": message, "prompt": prompt}
    if message == "TAMPER":
        settings = {**settings, "prompt": "worker-tampered"}
    report = {
        "session_type": type(session).__name__,
        "output_root": str(output_root),
        "timeout_seconds": timeout_seconds,
        "progress": progress,
        "refused_code": None,
        "evidence": None,
    }
    try:
        report["evidence"] = session.submit(settings, timeout_seconds=timeout_seconds)
    except Exception as exc:
        if type(exc).__name__ != "SessionChannelError":
            raise
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
    result = _payload(host.run_task(_task(runtime, "probe-ok", {"message": "hello", "prompt": "a red fox"}),
                                    lease_token="lease"))
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
    assert len(calls) == 1
    from astrid.packs.wan2gp.src.driver import compile_host_settings
    assert calls[0]["settings"] == compile_host_settings({"message": "hello", "prompt": "a red fox"})
    assert calls[0]["settings"]["prompt"] == "a red fox"
    assert report["evidence"] == {"result": {"success": True}, "process_id": 4242, "returncode": 0}
    assert 0 < calls[0]["timeout"] <= timeout

    # 4. Channel closed after run_task returns: broker closed, both sockets closed.
    assert len(brokers) == 1 and brokers[0].closed is True
    assert brokers[0]._sock.fileno() == -1
    assert len(children) == 1 and children[0].fileno() == -1


def test_worker_submit_with_differing_settings_is_refused_before_invocation(tmp_path, monkeypatch, wiring):
    brokers, children = wiring
    requests = []
    host, runtime, calls = _host_and_definition(tmp_path, monkeypatch)
    _spy_invoke_capability(host, monkeypatch, children, requests)
    result = _payload(host.run_task(_task(runtime, "probe-tamper", {"message": TAMPER, "prompt": "a red fox"}),
                                    lease_token="lease"))

    report = runtime.settlements[-1][2]["result"]["action_result"]
    # 3. Worker's copy is refused by the host; the action sees the error code.
    assert report["refused_code"] == "invalid_settings"
    assert report["evidence"] is None
    assert calls == []
    assert result["task"]["status"] == "completed"

    # 4. Refusal closes the channel as well.
    assert len(brokers) == 1 and brokers[0].closed is True
    assert brokers[0]._sock.fileno() == -1
    assert len(children) == 1 and children[0].fileno() == -1
