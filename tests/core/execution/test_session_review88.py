"""M2-11a review 88 findings S-1, S-3 and N-7, tested through the real broker.

S-1: the worker never chooses native inputs; mismatched or worker-shaped inputs are
refused before any compile or invoke. S-3: native identifiers stay on the host.
N-7: a cancel reports the requested reason separately from the verified outcome.
"""
from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from astrid.core.execution import generic_host as gh
from astrid.core.execution.session_channel import SESSION_CHANNEL_VERSION, SessionChannelBroker
from astrid.packs.wan2gp.src.driver import compile_host_settings
from tests.core.execution.test_session_identity_settings import FIXTURE, _Stub

RUN_ID = "run-review88"
SENTINELS = ("TOKEN-SENTINEL-r88", "INV-SENTINEL-r88", "SESS-SENTINEL-r88", "BINDING-SENTINEL-r88")


def _adapter(stub: _Stub, tmp_path: Path, *, cancel_session=None):
    inputs = json.loads(FIXTURE.read_text(encoding="utf-8"))["inputs"]
    profile = gh.ManagedToolProfile(compile_settings=compile_host_settings, invoke=stub.invoke_wan_session)
    adapter = gh._HostManagedSessionAdapter(
        profile=profile, managed_token=object(), inputs=inputs,
        progress_path=tmp_path / "progress.json", timeout_cap=30.0, cancelled=lambda: False,
        cancel_session=cancel_session or (lambda token, outcome: None))
    return adapter, inputs


def _submit_through_broker(adapter, args: dict[str, Any]) -> dict[str, Any]:
    from astrid.core.execution.generic_host import _send_activation_frame

    parent, child = socket.socketpair()
    try:
        broker = SessionChannelBroker(RUN_ID, adapter, parent, admission_token=object())
        _send_activation_frame(child, {
            "version": SESSION_CHANNEL_VERSION, "run_id": RUN_ID, "seq": 1, "op": "submit", "args": args,
        })
        return broker.serve_one()
    finally:
        parent.close()
        child.close()


def test_changed_valid_prompt_is_refused_before_invoke_through_broker(tmp_path: Path) -> None:
    stub = _Stub()
    adapter, inputs = _adapter(stub, tmp_path)
    changed = {**inputs, "prompt": "worker-chosen prompt"}
    response = _submit_through_broker(adapter, {"inputs": changed, "timeout_seconds": 5.0})
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_settings"
    assert stub.invocations == []


def test_native_only_field_in_worker_inputs_is_refused_before_invoke_through_broker(tmp_path: Path) -> None:
    stub = _Stub()
    adapter, inputs = _adapter(stub, tmp_path)
    smuggled = {**inputs, "model_type": "worker-native", "arbitrary_native": {"x": 1}}
    response = _submit_through_broker(adapter, {"inputs": smuggled, "timeout_seconds": 5.0})
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_settings"
    assert stub.invocations == []


def test_identical_inputs_invoke_once_with_host_compiled_settings_through_broker(tmp_path: Path) -> None:
    stub = _Stub()
    adapter, inputs = _adapter(stub, tmp_path)
    response = _submit_through_broker(adapter, {"inputs": dict(inputs), "timeout_seconds": 5.0})
    assert response["ok"] is True
    assert len(stub.invocations) == 1
    assert stub.invocations[0]["settings"] == compile_host_settings(inputs)


def test_success_evidence_contains_no_native_identifiers(tmp_path: Path) -> None:
    stub = _Stub()

    def invoke_with_identity(token, settings, *, timeout, cancelled, progress_path):
        stub.invocations.append({"settings": settings, "timeout": timeout})
        return {
            "result": {"success": True},
            "token_id": SENTINELS[0],
            "invocation_id": SENTINELS[1],
            "session_id": SENTINELS[2],
            "binding": {"identity": SENTINELS[3]},
        }

    stub.invoke_wan_session = invoke_with_identity
    adapter, inputs = _adapter(stub, tmp_path)
    adapter._profile = gh.ManagedToolProfile(compile_settings=compile_host_settings, invoke=invoke_with_identity)
    response = _submit_through_broker(adapter, {"inputs": dict(inputs), "timeout_seconds": 5.0})
    assert response["ok"] is True
    text = json.dumps(response)
    for sentinel in SENTINELS:
        assert sentinel not in text
    assert response["result"]["result"] == {"success": True} or response["result"].get("result") == {"success": True}


def test_cancel_reports_requested_reason_and_confirmed_outcome(tmp_path: Path) -> None:
    calls: list[tuple[Any, str]] = []
    adapter, _ = _adapter(_Stub(), tmp_path, cancel_session=lambda token, outcome: calls.append((token, outcome)))
    result = adapter.cancel("operator asked")
    assert calls and calls[0][1] == "confirmed"
    assert result == {"state": "cancel_requested", "requested_reason": "operator asked", "outcome": "confirmed"}


def test_cancel_failure_is_reported_uncertain_not_confirmed(tmp_path: Path) -> None:
    def failing(token, outcome):
        raise RuntimeError("native cancel failed")

    adapter, _ = _adapter(_Stub(), tmp_path, cancel_session=failing)
    result = adapter.cancel("operator asked")
    assert result["outcome"] == "uncertain"
    assert result["requested_reason"] == "operator asked"
