"""M2-11a Luna G: declared-session pack end-to-end under the DEFAULT execution policy.

Same pack shape as test_session_e2e_test_pack.py, but run_task gets a plain
ExecutionGuardPolicy() (3600 s run deadline, no override). Guards the regression
where the worker refused any deadline above 900 s.

Real: GenericPackHost discover/admit/run_task, the admitted worker subprocess
(generic_host_worker in a child interpreter), the inherited socketpair, the
worker-side SessionClient, SessionChannelBroker, and _HostManagedSessionAdapter.

Stubbed: only the host edge ``GenericPackHost.invoke_wan_session`` and the
runtime client (``FakeRuntime``), as in the existing v3 tests. The pack builder
and the fake invoke edge are copied here (not imported) so this file does not
depend on the file another worker is rewriting.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from astrid.core.execution import session_channel as session_channel_module
from astrid.core.execution.generic_host import GenericPackHost, HostError
from astrid.core.execution.guards import DEFAULT_DEADLINE_SECONDS, ExecutionGuardPolicy
from tests.core.execution.session_test_profiles import register_test_profile
from tests.test_generic_host import FakeRuntime as _CurrentFakeRuntime


PACK_ID = "sessionprobe"
CAPABILITY_ID = "sessionprobe.probe"
PREVIOUS_WORKER_CAP_SECONDS = 900.0
TOLERANCE_SECONDS = 5.0

ACTION_SOURCE = '''
# Public-surface only: the pack may not deep-import astrid.core or wan2gp
# (import_violation). The worker submits RAW inputs; the host compiles its own
# admitted copy and refuses any difference (review X-6, S-1).


def session_probe(*, message, prompt, output_root, timeout_seconds, session):
    progress = session.progress()
    settings = {"message": message, "prompt": prompt}
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


def _register_probe_profile(host, fake_invoke):
    """Instance-level, test-only registration via the shared helper."""
    register_test_profile(host, PACK_ID, fake_invoke)


def _host(tmp_path, monkeypatch, *, policy):
    root = _test_pack(tmp_path)
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[root], client=runtime, execution_policy=policy)
    records = host.discover()
    assert [record.id for record in records] == [CAPABILITY_ID]
    host.admit(capability_kind="executor", capability_id=CAPABILITY_ID)
    host.register()
    calls = []

    def fake_invoke_wan_session(token, settings, *, timeout, cancelled, progress_path=None):
        calls.append({"settings": dict(settings), "timeout": timeout})
        return {"result": {"success": True}, "process_id": 4242, "returncode": 0}

    # Host edge only: the real adapter calls this, nothing above it is replaced.
    monkeypatch.setattr(host, "invoke_wan_session", fake_invoke_wan_session)
    _register_probe_profile(host, fake_invoke_wan_session)
    return host, runtime, calls


def _task(runtime, task_id, inputs):
    task = {"task": {"id": task_id, "capability": CAPABILITY_ID, "project_id": "demo",
                     "attempt_id": f"{task_id}-attempt", "fence": 1,
                     "spec": {"spec": {"inputs": inputs}}}}
    runtime.tasks[task_id] = task
    return task


def test_declared_session_pack_runs_under_default_policy_with_full_deadline(tmp_path, monkeypatch):
    brokers = []
    real_broker = session_channel_module.SessionChannelBroker

    class RecordingBroker(real_broker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            brokers.append(self)

    monkeypatch.setattr(session_channel_module, "SessionChannelBroker", RecordingBroker)

    # Real: the default policy object, no deadline override.
    policy = ExecutionGuardPolicy()
    assert policy.deadline_seconds == DEFAULT_DEADLINE_SECONDS == 3600.0
    host, runtime, calls = _host(tmp_path, monkeypatch, policy=policy)

    started = time.monotonic()
    try:
        result = host.run_task(_task(runtime, "probe-default", {"message": "hello", "prompt": "a red fox"}),
                               lease_token="lease")
    except HostError as exc:  # pragma: no cover - the failure this test exists to catch
        pytest.fail(f"default-policy run raised HostError: {exc}")
    ended = time.monotonic()

    # 1. The run completes and the action returns its mapping (no refusal).
    assert result["task"]["status"] == "completed", result
    report = runtime.settlements[-1][2]["result"]["action_result"]
    assert isinstance(report, dict)
    assert report["session_type"] == "SessionClient"
    assert report["refused_code"] is None
    assert report["evidence"] == {
            "result": {"success": True}, "process_id": 4242, "returncode": 0,
            # Host-compiled settings echoed back (M2-11a P2/P3 contract).
            "compiled_settings": {"prompt": "a red fox", "message": "hello"},
        }

    # 2. The worker's timeout is positive, within the remaining run budget, and above the old 900 s cap.
    timeout = report["timeout_seconds"]
    elapsed = ended - started
    assert timeout > 0
    assert timeout <= policy.deadline_seconds
    assert timeout >= policy.deadline_seconds - elapsed - TOLERANCE_SECONDS
    assert timeout > PREVIOUS_WORKER_CAP_SECONDS

    # 3. Exactly one host invocation, with a positive timeout bounded by the worker's timeout.
    assert len(calls) == 1
    assert 0 < calls[0]["timeout"] <= timeout

    # Channel closed after run_task returns.
    assert len(brokers) == 1 and brokers[0].closed is True


def test_pack_declaring_an_unregistered_managed_tool_is_refused_before_the_channel(tmp_path, monkeypatch):
    """Nothing a pack reaches can add a profile: an unknown managed tool is refused, even with others present."""
    import astrid.core.execution.generic_host as gh

    opened = []
    real_channel = gh._HostSessionChannel

    class _Recording(real_channel):
        def __init__(self, *args, **kwargs):
            opened.append(args)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(gh, "_HostSessionChannel", _Recording)
    host, runtime, calls = _host(tmp_path, monkeypatch, policy=ExecutionGuardPolicy())
    # Other profiles exist (wan2gp and the probe); the unknown tool must still be refused.
    assert "wan2gp" in host._managed_tool_profiles and PACK_ID in host._managed_tool_profiles
    host._managed_tool_profiles.pop(PACK_ID)
    with pytest.raises(HostError, match="no registered host profile"):
        host.run_task(_task(runtime, "unknown-tool", {"message": "hi", "prompt": "a prompt"}), lease_token="lease")
    assert opened == [] and calls == []
