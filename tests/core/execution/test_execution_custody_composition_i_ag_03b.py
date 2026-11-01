"""Focused terminal arbitration checks against the real run_task wrapper."""
import threading

import pytest

from astrid.core.execution.generic_host import HostError
from tests.test_generic_host_result_json import host_case, _success_boundary


def test_concurrent_failures_share_one_completion(host_case, monkeypatch):
    case = host_case
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        entered.set()
        assert release.wait(5)
        return {"accepted": True}

    monkeypatch.setattr(case.runtime, "fail", fail)

    def exercise(*args, _terminal, **kwargs):
        call = _terminal["completion_call"]
        errors = []
        thread = threading.Thread(target=lambda: call(case.runtime.fail, "task-result", "lease-result", "error"))
        thread.start()
        try:
            assert entered.wait(5)
            with pytest.raises(HostError, match="reserved or unconfirmed"):
                call(case.runtime.fail, "task-result", "lease-result", "other")
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive()
        assert _terminal["completion_state"] == "acknowledged"
        return {"ok": True}

    monkeypatch.setattr(case.host, "_run_task_impl", exercise)
    case.host.run_task(case.task, lease_token="lease-result")
    assert len(calls) == 1


def test_lost_success_reply_does_not_issue_failure(host_case, monkeypatch):
    case = host_case
    _success_boundary(case, monkeypatch)
    calls = []

    def lost_reply(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError("reply lost")

    monkeypatch.setattr(case.runtime, "settle", lost_reply)
    with pytest.raises(OSError, match="reply lost"):
        case.host.run_task(case.task, lease_token="lease-result")
    assert len(calls) == 1
    assert case.runtime.failures == []
    import json
    receipt = json.loads((case.root / "result.json").read_text())
    assert receipt["completion_diagnostic"] == {"status": "unconfirmed"}
    assert not receipt["ok"]

@pytest.mark.parametrize("success_first", [True, False])
def test_success_abort_race_has_one_completion_decision(host_case, monkeypatch, success_first):
    case = host_case
    entered, release = threading.Event(), threading.Event()
    calls, errors = [], []

    def blocked(kind):
        def operation(*args, **kwargs):
            calls.append(kind)
            entered.set()
            assert release.wait(5)
            return {"kind": kind}
        return operation

    monkeypatch.setattr(case.runtime, "settle", blocked("settle"))
    monkeypatch.setattr(case.runtime, "fail", blocked("fail"))

    def exercise(*args, _terminal, **kwargs):
        call = _terminal["completion_call"]
        first = case.runtime.settle if success_first else case.runtime.fail
        second = case.runtime.fail if success_first else case.runtime.settle
        def submit():
            try:
                call(first, "task-result", "lease-result")
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=submit)
        thread.start()
        try:
            assert entered.wait(5)
            with pytest.raises(HostError, match="reserved or unconfirmed"):
                call(second, "task-result", "lease-result")
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive()
        assert errors == []
        assert _terminal["completion_state"] == "acknowledged"
        return {"ok": True}

    monkeypatch.setattr(case.host, "_run_task_impl", exercise)
    case.host.run_task(case.task, lease_token="lease-result")
    assert calls == ["settle" if success_first else "fail"]
