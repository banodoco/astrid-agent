"""M2-11a review 88 findings S-1, S-3 and N-7, tested through the real broker.

S-1: the worker never chooses native inputs; mismatched or worker-shaped inputs are
refused before any compile or invoke. S-3: native identifiers stay on the host.
N-7: a cancel reports the requested reason separately from the verified outcome.
"""
from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

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


def test_unregistered_managed_route_is_refused_before_any_session_acquisition(tmp_path: Path, monkeypatch: Any) -> None:
    """Review X-4: the profile and route are validated before any open, start, admit or dispatch."""
    import dataclasses

    import astrid.core.generation.availability as availability
    from astrid.core.execution.managed_tool_session import ManagedToolSession
    from astrid.core.pack.loader import _default_pack_roots
    from tests.packs.wan2gp import test_session_run_task_parity as parity

    monkeypatch.setattr(availability, "action_availability", lambda _route: {"available": True})
    client = parity._RuntimeClient()
    host = gh.GenericPackHost(pack_roots=_default_pack_roots(), client=client)
    host.discover()
    host.capabilities["wan2gp.generate_video"] = dataclasses.replace(
        host.capabilities["wan2gp.generate_video"], ready=True)
    # The shipped wan action declares a managed session; with no profile it must be refused.
    monkeypatch.delitem(host._managed_tool_profiles, "wan2gp")

    events: list[str] = []
    real_open = ManagedToolSession.open
    real_admit = ManagedToolSession.admit
    real_adapter_for = ManagedToolSession.adapter_for
    monkeypatch.setattr(ManagedToolSession, "open", lambda self, *a, **k: (events.append("open"), real_open(self, *a, **k))[1])
    monkeypatch.setattr(ManagedToolSession, "admit", lambda self, *a, **k: (events.append("admit"), real_admit(self, *a, **k))[1])
    monkeypatch.setattr(ManagedToolSession, "adapter_for", lambda self, *a, **k: (events.append("adapter_for"), real_adapter_for(self, *a, **k))[1])
    real_channel = gh._HostSessionChannel

    class _Recording(real_channel):
        def __init__(self, *args, **kwargs):
            events.append("channel")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(gh, "_HostSessionChannel", _Recording)
    monkeypatch.setattr(host, "invoke_wan_session", lambda *a, **k: events.append("invoke") or {})

    class _RecordingLock:
        """Records session-lock acquisition; the early route check must run before it."""

        def __init__(self, real):
            self._real = real

        def acquire(self, *args, **kwargs):
            events.append("session_lock")
            return self._real.acquire(*args, **kwargs)

        def release(self):
            return self._real.release()

    host._session_task_lock = _RecordingLock(host._session_task_lock)
    task = parity._task(tmp_path, dict(parity.EXPLICIT_INPUTS))
    client.tasks["wan-task"] = task
    with pytest.raises(gh.HostError, match="no registered host profile"):
        host.run_task(task, lease_token="lease")
    assert events == []


class _BlockingAdapter:
    """A submit that blocks in a native-like call until released or cancelled."""

    def __init__(self, cancelled, *, honour_cancel: bool) -> None:
        self._cancelled = cancelled
        self._honour = honour_cancel
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def validate_settings(self, inputs: Any) -> None:
        return None

    def submit(self, inputs: Any, timeout_seconds: float) -> dict[str, Any]:
        self.started.set()
        try:
            while not self.release.is_set():
                if self._honour and self._cancelled():
                    break
                self.release.wait(0.01)
            return {"result": {"success": False}}
        finally:
            self.finished.set()

    def progress(self) -> dict[str, Any]:
        return {}

    def cancel(self, reason: str) -> dict[str, Any]:
        return {"state": "cancel_requested"}


def _channel_with(adapter_factory, stop: threading.Event):
    parent, child = socket.socketpair()
    adapter = adapter_factory(stop.is_set)
    broker = SessionChannelBroker(RUN_ID, adapter, parent, admission_token=object())
    channel = gh._HostSessionChannel(broker, parent, child, RUN_ID, stop=stop)
    channel.start()
    from astrid.core.execution.generic_host import _send_activation_frame

    _send_activation_frame(child, {"version": SESSION_CHANNEL_VERSION, "run_id": RUN_ID, "seq": 1,
                                   "op": "submit", "args": {"inputs": {}, "timeout_seconds": 5.0}})
    return channel, adapter, child


def test_close_during_active_submit_stops_the_serving_thread(tmp_path: Path) -> None:
    """S-2: close cancels the in-flight native call and waits for the thread before settlement."""
    stop = threading.Event()
    channel, adapter, child = _channel_with(lambda c: _BlockingAdapter(c, honour_cancel=True), stop)
    assert adapter.started.wait(5)
    still_serving = channel.close(join_timeout=5)
    assert still_serving is False
    assert adapter.finished.is_set()
    child.close()


def test_worker_crash_during_active_submit_leaves_no_live_thread(tmp_path: Path) -> None:
    """S-2: a worker that dies mid-submit: the run stops the call and the thread ends."""
    stop = threading.Event()
    channel, adapter, child = _channel_with(lambda c: _BlockingAdapter(c, honour_cancel=True), stop)
    assert adapter.started.wait(5)
    child.close()
    assert channel.close(join_timeout=5) is False
    assert adapter.finished.is_set()


def test_deadline_during_active_submit_is_observed_by_the_native_call(tmp_path: Path) -> None:
    """S-2: a deadline is the cancel signal; the in-flight call ends and nothing outlives it."""
    stop = threading.Event()
    channel, adapter, child = _channel_with(lambda c: _BlockingAdapter(c, honour_cancel=True), stop)
    assert adapter.started.wait(5)
    stop.set()
    assert adapter.finished.wait(5)
    assert channel.close(join_timeout=5) is False
    child.close()


def test_a_native_call_that_ignores_cancel_is_reported_still_serving(tmp_path: Path) -> None:
    """S-2: when the native call will not stop, close reports it still serving (the run fences)."""
    stop = threading.Event()
    channel, adapter, child = _channel_with(lambda c: _BlockingAdapter(c, honour_cancel=False), stop)
    assert adapter.started.wait(5)
    assert channel.close(join_timeout=0.2) is True
    adapter.release.set()
    assert adapter.finished.wait(5)
    child.close()
