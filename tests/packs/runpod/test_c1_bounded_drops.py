"""Structured, text-free refusals at bootstrap and bounded transport boundaries."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import remote_placement as rp
from astrid.packs.runpod.failure_receipt import build_receipt, render

SECRET = "SECRET_SENTINEL_private/path_https://private.invalid"


def _receipt(error):
    receipt = build_receipt({"failure": error, "stage": "boot"}, job=SimpleNamespace(), machine=None, timing=None)
    rendered = render(receipt)
    assert SECRET not in json.dumps(receipt) + rendered
    return receipt, rendered


def test_missing_bootstrap_marker_is_structured_and_text_free(monkeypatch, tmp_path):
    placement = object.__new__(rp.RemotePlacement)
    placement.source_root = None
    monkeypatch.setattr(placement, "_exec", lambda *args, **kwargs: (0, "", SECRET))
    state = rp.RemoteRun("r", "/remote/run", tmp_path)

    with pytest.raises(rp.PlacementReasonError) as caught:
        placement._bootstrap(state)

    receipt, rendered = _receipt(caught.value)
    expected = {"stage": "boot", "step": "bootstrap", "gate": "provider", "code": "provider_malformed"}
    assert caught.value.reason.as_dict() == expected
    assert receipt["cause"]["reason"] == expected
    assert "provider_malformed" in rendered


class _Receive:
    def recv_bytes(self, maxlength=None):
        return json.dumps({"ok": False, "value": SECRET}).encode()

    def close(self):
        pass


class _Send:
    def close(self):
        pass


class _Process:
    daemon = False

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False

    def close(self):
        pass


class _Context:
    def Pipe(self, duplex=False):
        return _Receive(), _Send()

    def Process(self, *args, **kwargs):
        return _Process()


def _ops(monkeypatch, *, deadline=None, abort_event=None):
    monkeypatch.setattr(rp.multiprocessing, "get_context", lambda method: _Context())
    return rp._HttpsOps("pod", "credential", deadline=deadline or time.monotonic() + 5,
                        abort_event=abort_event)


def _assert_bounded_reason(error, code):
    receipt, rendered = _receipt(error)
    reason = receipt["cause"]["reason"]
    assert reason == {"stage": "boot", "step": "provider_contract", "gate": "provider", "code": code}
    assert code in rendered


def test_bounded_operation_exception_is_structured(monkeypatch):
    ops = _ops(monkeypatch)
    with pytest.raises(rp.PlacementReasonError) as caught:
        ops._bounded_call("execute_command", "safe command")
    _assert_bounded_reason(caught.value, "transport_failed")


def test_bounded_operation_timeout_is_structured(monkeypatch):
    ops = _ops(monkeypatch, deadline=time.monotonic() - 1)
    with pytest.raises(rp.PlacementReasonError) as caught:
        ops._bounded_call("execute_command", "safe command")
    _assert_bounded_reason(caught.value, "deadline_exceeded")


def test_bounded_operation_interruption_is_structured(monkeypatch):
    abort = threading.Event()
    abort.set()
    ops = _ops(monkeypatch, abort_event=abort)
    with pytest.raises(rp.PlacementReasonError) as caught:
        ops._bounded_call("execute_command", "safe command")
    _assert_bounded_reason(caught.value, "custody_unresolved")
