"""Host-enforced settings identity (M2-11a): the worker's settings are never trusted.

(a) A worker request carrying a "settings" field, or any key outside the inputs
contract, is refused before any invoke. (b) invoke receives exactly the host compile
of the inputs. (c) Inputs that fail host compile or validation are refused before
invoke. The earlier settings-comparison guarantee was replaced by these (M-GPU, 09:34Z).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host as gh
from astrid.packs.wan2gp.src.driver import compile_host_settings

FIXTURE = Path(__file__).resolve().parents[2] / "packs" / "wan2gp" / "fixtures" / "one_frame_engine_result.json"


class _Stub:
    def __init__(self) -> None:
        self.invocations: list[dict] = []
        self.managed_tool_session = SimpleNamespace(cancel=lambda token, outcome=None: None)

    def invoke_wan_session(self, token, settings, *, timeout, cancelled, progress_path):
        self.invocations.append({"settings": settings, "timeout": timeout})
        return {"result": {"success": True}}


def _adapter(stub: _Stub, tmp_path: Path):
    inputs = json.loads(FIXTURE.read_text(encoding="utf-8"))["inputs"]
    profile = gh.ManagedToolProfile(compile_settings=compile_host_settings, invoke=stub.invoke_wan_session)
    adapter = gh._HostManagedSessionAdapter(
        profile=profile, managed_token=object(), inputs=inputs,
        progress_path=tmp_path / "progress.json", timeout_cap=30.0, cancelled=lambda: False,
        cancel_session=lambda token, reason: None)
    return adapter, inputs


def test_mismatched_worker_settings_are_refused_before_any_invoke_inputs(tmp_path: Path) -> None:
    """(a) A worker request carrying a "settings" field, or any key outside the
    inputs contract, is refused by the broker before the adapter or invoke runs."""
    from astrid.core.execution.session_channel import SESSION_CHANNEL_VERSION, SessionChannelBroker
    from astrid.core.execution.generic_host import _send_activation_frame
    import socket

    stub = _Stub()
    adapter, inputs = _adapter(stub, tmp_path)
    forged_settings = dict(compile_host_settings(inputs))
    forged_settings["prompt"] = "worker-chosen prompt"
    parent, child = socket.socketpair()
    try:
        broker = SessionChannelBroker("run-identity-1", adapter, parent, admission_token=object())
        _send_activation_frame(child, {
            "version": SESSION_CHANNEL_VERSION, "run_id": "run-identity-1", "seq": 1, "op": "submit",
            "args": {"inputs": dict(inputs), "settings": forged_settings, "timeout_seconds": 5.0},
        })
        response = broker.serve_one()
    finally:
        parent.close()
        child.close()
    assert response["ok"] is False
    assert response["error"]["code"] == "protocol"
    assert stub.invocations == []


def test_invoke_receives_exactly_host_compiled_inputs(tmp_path: Path) -> None:
    """(b) invoke gets the host compile of the inputs, never a worker-shaped object."""
    stub = _Stub()
    adapter, inputs = _adapter(stub, tmp_path)
    worker_inputs = dict(inputs)
    adapter.submit(worker_inputs, timeout_seconds=5.0)
    assert len(stub.invocations) == 1
    invoked = stub.invocations[0]["settings"]
    assert invoked == compile_host_settings(inputs)
    assert invoked is not worker_inputs


def test_inputs_failing_host_compile_are_refused_before_invoke(tmp_path: Path) -> None:
    """(c) A worker copy that differs from the admitted inputs is refused before compile or invoke.

    Review S-1: the host compiles only its own admitted inputs. Any worker-shaped
    difference (including one that would fail compile) is refused at validation."""
    stub = _Stub()
    adapter, inputs = _adapter(stub, tmp_path)
    bad = {**inputs, "resolution": "bogus"}
    with pytest.raises(ValueError, match="host-admitted"):
        adapter.validate_settings(bad)
    assert stub.invocations == []


def test_matching_settings_reach_invoke_once_with_host_compiled_settings(tmp_path: Path) -> None:
    stub = _Stub()
    adapter, inputs = _adapter(stub, tmp_path)
    worker_copy = dict(inputs)
    adapter.validate_settings(worker_copy)
    adapter.submit(worker_copy, timeout_seconds=5.0)
    assert len(stub.invocations) == 1
    assert stub.invocations[0]["settings"] == compile_host_settings(inputs)


def test_submit_is_allowed_once_per_run(tmp_path: Path) -> None:
    stub = _Stub()
    adapter, inputs = _adapter(stub, tmp_path)
    adapter.submit(dict(inputs), timeout_seconds=5.0)
    with pytest.raises(RuntimeError, match="once per run"):
        adapter.submit(dict(inputs), timeout_seconds=5.0)
    assert len(stub.invocations) == 1
