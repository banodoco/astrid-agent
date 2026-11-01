"""Forced receipts keep their result while completion evidence changes."""

import json
import threading

import pytest

from astrid.core.execution import generic_host
from tests.test_generic_host_result_json import _terminal_state, host_case


@pytest.mark.parametrize("forced", ["timeout", "cancelled"])
@pytest.mark.parametrize("outcome", ["unknown", "success", "failure"])
def test_finalized_receipt_refreshes_completion_evidence(host_case, monkeypatch, forced, outcome):
    case = host_case
    terminal = _terminal_state(case)
    terminal.update(forced=forced, status=forced, minimal=True, completion_state="open")
    case.host._write_task_result(terminal)
    path = case.root / "result.json"
    original = json.loads(path.read_text())
    assert terminal["receipt_final"] is True
    assert original["publication"]["status"] == "not_settled"

    def unexpected_collection(*args, **kwargs):
        pytest.fail("a finalized forced receipt must not collect late output")

    monkeypatch.setattr(case.host, "_typed_outputs", unexpected_collection)
    monkeypatch.setattr(generic_host, "serialize_terminal_result", unexpected_collection)
    # Completion can be reserved after the lifecycle observer sets forced and
    # a concurrent writer finalizes the receipt, for example during revocation.
    terminal.update(completion_state="in_flight", status="succeeded", payload={"late": True})
    case.host._write_task_result(terminal)
    pending = json.loads(path.read_text())
    assert pending == {
        **original,
        "publication": {"status": "unconfirmed", "receipt": None, "effect": None},
        "completion_diagnostic": {"status": "unconfirmed"},
    }

    with terminal["completion_lock"]:
        terminal["completion_state"] = "unknown" if outcome == "unknown" else "acknowledged"
        if outcome == "success":
            terminal.update(settled=True, settlement={"accepted": True}, effect={"type": "fixture"})
    case.host._write_task_result(terminal)
    final = json.loads(path.read_text())
    if outcome == "unknown":
        assert final == pending
    else:
        success = outcome == "success"
        assert final == {
            **original,
            "publication": {
                "status": "settled" if success else "not_settled",
                "receipt": {"accepted": True} if success else None,
                "effect": {"type": "fixture"} if success else None,
            },
        }
    assert case.runtime.failures == case.runtime.settlements == []


@pytest.mark.parametrize("reply", ["pending", "lost"])
def test_cancellation_revocation_race_keeps_finalized_publication_unconfirmed(host_case, monkeypatch, reply):
    case = host_case
    revoking, resume = threading.Event(), threading.Event()
    calls, originals = [], []

    class Bridge:
        def revoke(self):
            revoking.set()
            assert resume.wait(5)

    bridge = Bridge()

    def cancel(task_id, **options):
        calls.append((task_id, options))
        if reply == "lost":
            raise OSError("cancellation reply lost")
        return {"task": {"state": "cancel_requested"}}

    def run(*args, _terminal, **kwargs):
        with case.host._process_lock:
            case.host._child_bridges.add(bridge)
        guard = threading.Thread(target=_terminal["force_abort"], args=("cancelled",))
        guard.start()
        try:
            assert revoking.wait(5)
            case.host._write_task_result(_terminal)
            originals.append(json.loads((case.root / "result.json").read_text()))
            assert _terminal["receipt_final"] is True
            assert originals[0]["publication"]["status"] == "not_settled"
        finally:
            resume.set()
            guard.join(5)
            with case.host._process_lock:
                case.host._child_bridges.discard(bridge)
        assert not guard.is_alive()
        assert _terminal["completion_state"] == "unknown"
        return {"cancelled": True}

    monkeypatch.setattr(case.runtime, "cancel", cancel, raising=False)
    monkeypatch.setattr(case.host, "_run_task_impl", run)
    assert case.host.run_task(case.task, lease_token="lease-result")["cancelled"]
    assert len(calls) == 1
    assert calls[0][1]["stop_observed"] is False
    assert json.loads((case.root / "result.json").read_text()) == {
        **originals[0],
        "publication": {"status": "unconfirmed", "receipt": None, "effect": None},
        "completion_diagnostic": {"status": "unconfirmed"},
    }
    assert not case.host._handoff_observation_known
    assert "Runtime completion unconfirmed; attempt retained" in case.host._unresolved_task_scopes
    assert case.runtime.failures == case.runtime.settlements == []
