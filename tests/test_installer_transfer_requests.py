"""Request bookkeeping is not native transfer authority or a wire protocol."""

from dataclasses import replace

import pytest

from astrid.installer.transfer_requests import RequestEnvelope, RequestLedger, RequestRefused


def test_replay_returns_exact_recorded_result_after_phase_changes():
    envelope = RequestEnvelope("op", "generation", "request", "HELD_PROVISIONAL", "digest")
    ledger = RequestLedger("op", "generation", "digest")
    pending, result = ledger.begin(envelope, b"install-suppression", phase="HELD_PROVISIONAL")
    assert result is None
    assert ledger.records == ()  # Intent must be persisted by the transaction owner.
    finished = pending.finish(envelope, b"install-suppression", b"result including owned deltas")
    replay, result = finished.begin(envelope, b"install-suppression", phase="RESTART_FENCED")
    assert replay is finished
    assert result == b"result including owned deltas"
    assert "result including owned deltas" not in repr(finished)
    assert finished.finish(envelope, b"install-suppression", result) is finished


@pytest.mark.parametrize("change", [
    {"operation_id": "other"}, {"generation": "other"}, {"binding_digest": "other"},
    {"expected_phase": "other"},
])
def test_same_request_with_changed_envelope_is_refused(change):
    envelope = RequestEnvelope("op", "generation", "request", "phase", "digest")
    ledger, _ = RequestLedger("op", "generation", "digest").begin(envelope, b"request", phase="phase")
    with pytest.raises(RequestRefused, match="binding_changed"):
        ledger.begin(replace(envelope, **change), b"request", phase="phase")


def test_changed_bytes_or_result_refused():
    envelope = RequestEnvelope("op", "generation", "request", "phase", "digest")
    ledger, _ = RequestLedger("op", "generation", "digest").begin(envelope, b"first", phase="phase")
    with pytest.raises(RequestRefused, match="binding_changed"):
        ledger.begin(envelope, b"second", phase="phase")
    finished = ledger.finish(envelope, b"first", b"receipt")
    with pytest.raises(RequestRefused, match="binding_changed"):
        finished.finish(envelope, b"first", b"different receipt")


def test_unfinished_or_out_of_phase_requests_require_recovery():
    envelope = RequestEnvelope("op", "generation", "request", "phase", "digest")
    ledger = RequestLedger("op", "generation", "digest")
    with pytest.raises(RequestRefused, match="recovery_required"):
        ledger.begin(envelope, b"request", phase="other")
    with pytest.raises(RequestRefused, match="recovery_required"):
        ledger.finish(envelope, b"request", b"receipt")
    pending, _ = ledger.begin(envelope, b"request", phase="phase")
    with pytest.raises(RequestRefused, match="recovery_required"):
        pending.begin(envelope, b"request", phase="phase")


def test_pending_intent_blocks_different_request_until_reconciled():
    envelope = RequestEnvelope("op", "generation", "request", "phase", "digest")
    ledger, _ = RequestLedger("op", "generation", "digest").begin(envelope, b"first", phase="phase")
    with pytest.raises(RequestRefused, match="recovery_required"):
        ledger.begin(replace(envelope, request_id="next"), b"next", phase="phase")
