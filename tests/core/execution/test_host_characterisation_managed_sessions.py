"""HM1 R04/R11: the current host's Wan session and owner-control seams.

The host and ManagedToolSession are real. Only the Wan child adapter is a
double: these cases pin coordination, not native release or LS-8 residency.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, HostCancelled
from astrid.core.execution.managed_tool_session import (
    SessionCapacityError,
    StaleAdmissionError,
)


@pytest.fixture
def wan_host(tmp_path, monkeypatch):
    events, adapters = [], []

    class WanAdapter:
        def __init__(self, *, spec, binding, **kwargs):
            self.spec, self.binding = spec, binding
            self.index = len(adapters)
            self.invocations = []
            self.failure = None
            self.on_invoke = None
            adapters.append(self)
            events.append((self.index, "construct"))

        def start(self):
            events.append((self.index, "start"))

        def observe(self):
            events.append((self.index, "observe"))
            return {"ok": True}

        def fence(self, *, reason):
            events.append((self.index, "fence", reason))
            return {"ok": True}

        def release(self, *, reason):
            events.append((self.index, "release", reason))
            return {"ok": True, "released": True, "owner": self.index}

        def invoke(self, token, settings, **kwargs):
            self.invocations.append((token, settings, kwargs))
            if self.on_invoke is not None:
                self.on_invoke()
            if self.failure is not None:
                raise self.failure
            return {
                "generation": token.generation,
                "binding_identity": list(token.binding_identity),
                "token_id": token.token_id,
                "invocation_id": token.invocation_id,
                "outputs": ["native-output"],
            }

    monkeypatch.setattr(generic_host, "_ManagedWanChildAdapter", WanAdapter)
    root = tmp_path / "wan"
    root.mkdir()
    interpreter = tmp_path / "venv" / "python"
    interpreter.parent.mkdir()
    interpreter.write_text("the adapter double never executes this file\n")
    host = GenericPackHost(pack_roots=[tmp_path], executor_id="hm2-owner")
    params = {
        "owner_dir": tmp_path / "owner", "root": root, "python": interpreter,
        "source_digest": "source-1", "config_digest": "config-1",
        "readiness_timeout": 3, "release_timeout": 2,
    }
    try:
        yield SimpleNamespace(host=host, params=params, events=events, adapters=adapters)
    finally:
        host.shutdown()


@pytest.mark.parametrize("runtime_instance", [None, "runtime-1"])
def test_r04_r11_wan_prepare_snapshots_reuses_and_replaces_in_owner_order(wan_host, runtime_instance):
    case, host = wan_host, wan_host.host
    if runtime_instance is not None:
        host.runtime_state["runtime_instance_id"] = runtime_instance
    options = {"cli_args": ["--profile", "small"], "console_isatty": False}
    first = host.prepare_wan_session(**case.params, init_options=options)
    manager = host.managed_tool_session
    generation = manager.generation
    adapter = case.adapters[0]
    assert adapter.spec == {
        "owner_dir": str(case.params["owner_dir"].resolve()),
        "root": str(case.params["root"].resolve()),
        "python": str(case.params["python"].absolute()),
        "source_digest": "source-1", "config_digest": "config-1",
        "runtime_instance_id": runtime_instance or "hm2-owner",
        "init": {
            "cli_args": ["--profile", "small"], "console_isatty": False,
            "root": str(case.params["root"].resolve()),
            "output_dir": str(case.params["owner_dir"].resolve() / "spool"),
            "console_output": False,
        },
    }
    options["cli_args"].append("--caller-mutated")
    assert adapter.spec["init"]["cli_args"] == ["--profile", "small"]
    replay_options = {"cli_args": ["--profile", "small"], "console_isatty": False}
    assert host.prepare_wan_session(**case.params, init_options=replay_options) is first
    assert manager.generation == generation and manager.current_adapter is adapter
    assert case.events == [(0, "construct"), (0, "start"), (0, "observe"), (0, "observe")]

    replacement = host.prepare_wan_session(
        **{**case.params, "config_digest": "config-2"}, init_options=replay_options,
    )
    assert replacement.execution_identity != first.execution_identity
    assert replacement.process_birth_id != first.process_birth_id
    assert manager.current_binding == replacement and manager.generation > generation
    assert case.events[4:] == [
        (1, "construct"), (0, "fence", "capacity_replacement"),
        (0, "release", "capacity_replacement"), (1, "start"), (1, "observe"),
    ]


def test_r04_r11_wan_result_keeps_admission_until_caller_settles(wan_host, tmp_path):
    case, host = wan_host, wan_host.host
    binding = host.prepare_wan_session(**case.params)
    manager = host.managed_tool_session
    token = manager.admit(capability_id="wan2gp.generate_video", invocation_id="invocation-1")
    settings = {"prompt": "one frame"}
    def cancelled():
        return False

    progress_path = tmp_path / "progress.json"
    evidence = host.invoke_wan_session(
        token, settings, timeout=7, cancelled=cancelled, progress_path=progress_path,
    )
    assert case.adapters[0].invocations == [(
        token, settings, {"timeout": 7, "cancelled": cancelled, "progress_path": progress_path},
    )]
    assert evidence == {
        "generation": token.generation, "binding_identity": list(token.binding_identity),
        "token_id": token.token_id, "invocation_id": "invocation-1", "outputs": ["native-output"],
    }
    assert manager.active and manager.generation == token.generation
    with pytest.raises(SessionCapacityError, match="^serial managed session already has an admission$"):
        manager.admit(capability_id="wan2gp.generate_video", invocation_id="invocation-2")
    assert host.release_managed_session(
        expected_binding=binding, expected_generation=token.generation,
    ) == {"ok": False, "released": False, "release_pending": True}
    assert case.events == [(0, "construct"), (0, "start"), (0, "observe")]
    assert manager.settle(token, result_evidence=evidence).state == "settled"
    assert host.release_managed_session(
        expected_binding=binding, expected_generation=token.generation, reason="owner-complete",
    ) == {"ok": True, "released": True, "native": {"ok": True, "released": True, "owner": 0}}
    assert not manager.occupied
    assert case.events[-2:] == [(0, "fence", "owner-complete"), (0, "release", "owner-complete")]


@pytest.mark.parametrize("failure_type", [RuntimeError, HostCancelled])
def test_r04_r11_wan_invocation_failure_fences_but_retains_original_owner(wan_host, failure_type):
    case, host = wan_host, wan_host.host
    binding = host.prepare_wan_session(**case.params)
    manager = host.managed_tool_session
    token = manager.admit(capability_id="wan2gp.generate_video", invocation_id="invocation-1")
    adapter = case.adapters[0]
    adapter.failure = failure_type("native invocation stopped")
    with pytest.raises(failure_type) as raised:
        host.invoke_wan_session(token, {}, timeout=7, cancelled=lambda: False)
    assert raised.value is adapter.failure
    assert manager.occupied and not manager.active and manager.current_adapter is adapter
    assert manager.current_binding == binding and manager.generation == token.generation + 1
    assert case.events[-1] == (0, "fence", "wan_invocation_failed_or_cancelled")
    assert not any(event[1] == "release" for event in case.events)
    with pytest.raises(StaleAdmissionError):
        manager.settle(token, result_evidence={"outputs": ["late-output"]})
    with pytest.raises(StaleAdmissionError):
        host.invoke_wan_session(token, {}, timeout=7, cancelled=lambda: False)
    assert len(adapter.invocations) == 1


def test_r04_r11_late_wan_failure_cannot_fence_the_replacement(wan_host):
    case, host = wan_host, wan_host.host
    first = host.prepare_wan_session(**case.params)
    manager = host.managed_tool_session
    token = manager.admit(capability_id="wan2gp.generate_video", invocation_id="invocation-1")
    old = case.adapters[0]
    replacement = []
    old.on_invoke = lambda: replacement.append(host.prepare_wan_session(
        **{**case.params, "config_digest": "config-2"},
    ))
    old.failure = RuntimeError("old generation failed after replacement")
    with pytest.raises(RuntimeError) as raised:
        host.invoke_wan_session(token, {}, timeout=7, cancelled=lambda: False)
    assert raised.value is old.failure
    assert replacement[0] != first and manager.current_binding == replacement[0]
    assert manager.active and manager.current_adapter is case.adapters[1]
    assert case.events == [
        (0, "construct"), (0, "start"), (0, "observe"), (1, "construct"),
        (0, "fence", "capacity_replacement"), (0, "release", "capacity_replacement"),
        (1, "start"), (1, "observe"),
    ]


def test_r11_release_control_waits_for_task_owner_and_refuses_stale_binding(wan_host):
    case, host = wan_host, wan_host.host
    first = host.prepare_wan_session(**case.params)
    manager = host.managed_tool_session
    generation = manager.generation
    with host._session_task_lock:
        assert host.release_managed_session(
            expected_binding=first, expected_generation=generation,
        ) == {"ok": False, "released": False, "release_pending": True}
        assert host._session_task_lock.locked()
    assert manager.active and manager.generation == generation
    assert case.events == [(0, "construct"), (0, "start"), (0, "observe")]
    replacement = host.prepare_wan_session(**{**case.params, "config_digest": "config-2"})
    before = list(case.events)
    with pytest.raises(StaleAdmissionError, match="authorization is stale"):
        host.release_managed_session(expected_binding=first, expected_generation=generation)
    assert manager.current_binding == replacement and manager.active
    assert case.events == before and not host._session_task_lock.locked()
