"""Host publication keeps the exact wire request through transient failures."""
from __future__ import annotations

import json
import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import RuntimeProtocolClient
from astrid_runtime_client import WorkspaceClient
from astrid_runtime_client.generated import ApiError
from tests.test_generic_host_result_json import host_case  # noqa: F401
from tests.core.execution.test_bug14_settlement_liveness import (  # noqa: F401
    settlement_case, _run, _assert_resolved,
)


def _wire(case, monkeypatch, statuses):
    calls = []

    def transport(method, path, headers, body):
        payload = json.loads(body)
        calls.append((method, path, bytes(body), headers["Idempotency-Key"]))
        status = statuses.pop(0) if statuses else None
        if status:
            code = {400: "validation_error", 409: "conflict", 422: "validation_error",
                    408: "http_error", 429: "rate_limited"}[status]
            raise ApiError(status, code, "publication unavailable")
        attempt_id = case.attempt["attempt_id"]
        with case.service.store._mutex:
            if path.endswith("/settle"):
                response = case.service.settle_attempt(attempt_id, payload,
                                                       idempotency_key=headers["Idempotency-Key"])
            else:
                assert path.endswith("/fail")
                response = case.service.fail_attempt(attempt_id, payload,
                                                     idempotency_key=headers["Idempotency-Key"])
        return 200, {}, json.dumps(response).encode()

    wire = WorkspaceClient("http://offline.invalid", transport=transport)
    generated = case.host.client.generated
    monkeypatch.setattr(generated, "settle_attempt", wire.settle_attempt)

    def fail(attempt_id, **request):
        return RuntimeProtocolClient._terminal_evidence_request(wire, "fail_attempt", attempt_id, request)

    monkeypatch.setattr(generated, "fail_attempt", fail)
    return calls


def _join(case):
    owner = case.host._reconciliation_owners.get(case.attempt["attempt_id"])
    if owner is not None:
        owner.join(timeout=3)
        assert not owner.is_alive()


def _fail_after_outputs(case, monkeypatch):
    execute = case.host._run_command_definition

    def fail(*args, **kwargs):
        execute(*args, **kwargs)
        raise RuntimeError("original producer failure")

    monkeypatch.setattr(case.host, "_run_command_definition", fail)


@pytest.mark.parametrize("failed", [False, True], ids=["settlement", "failure"])
def test_host_transient_publication_replays_exact_wire(settlement_case, monkeypatch, failed):
    case = settlement_case
    case.mode = "replay"
    # Both the synchronous adapter and the host owner encounter 408 then 429.
    calls = _wire(case, monkeypatch, [408, 429, 408, 429])
    if failed:
        _fail_after_outputs(case, monkeypatch)
    with pytest.raises(ApiError):
        _run(case)
    _join(case)
    assert len(calls) == 5
    assert calls == [calls[0]] * 5
    method, path, body, key = calls[0]
    assert method == "POST"
    assert path.endswith("/fail" if failed else "/settle")
    assert key == f"{'fail' if failed else 'settle'}-{case.attempt['attempt_id']}-{case.attempt['fence']}"
    payload = json.loads(body)
    assert payload["result"]["manifest_ref"]
    stored = case.service.task(case.task_id)["task"]
    assert stored["result"]["manifest_ref"] == payload["result"]["manifest_ref"]
    assert "publication_rejected" not in json.dumps(stored)
    if failed:
        assert "original producer failure" in stored["result"]["error"]["message"]
        assert stored["result"]["host_failure"] == payload["result"]["host_failure"]
        _assert_resolved(case, "failed")
    else:
        _assert_resolved(case, "completed")
    receipt = json.loads((case.root / "result.json").read_text())
    assert receipt["status"] == ("partial" if failed else "succeeded")


@pytest.mark.parametrize("status", [400, 409, 422])
@pytest.mark.parametrize("failed", [False, True], ids=["settlement", "failure"])
def test_host_definite_rejection_remains_permanent(settlement_case, monkeypatch, status, failed):
    case = settlement_case
    case.mode = "replay"
    calls = _wire(case, monkeypatch, [status])
    if failed:
        _fail_after_outputs(case, monkeypatch)
    with pytest.raises(generic_host.TerminalPublicationRejected):
        _run(case)
    _join(case)
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0] == "POST"
    cede = json.loads(calls[1][2])
    assert "outputs" not in cede and "result" not in cede
    assert cede["error"]["code"] == ("reconciliation_required" if failed else "publication_rejected")
    assert case.service.task(case.task_id)["task"]["status"] == "failed"


def _saved_failure(case, tmp_path):
    case.mode = "replay"
    case.host.executor_id = "bug14-worker"
    root = tmp_path / "saved"
    root.mkdir()
    case.host.attempt_root = root
    body = {key: case.attempt[key] for key in ("lease_id", "fence", "runtime_epoch")}
    body.update(error={"code": "producer_failed", "message": "original failure"}, outputs=[], result={})
    request = {"operation": "fail_attempt", "identifier": case.attempt["attempt_id"],
               "task_id": case.task_id, "executor_id": case.host.executor_id,
               "idempotency_key": "saved-failure", "body": body}
    (root / "fail-request.json").write_text(json.dumps(request))
    return root, request


def test_recovery_transient_read_and_write_preserve_request(settlement_case, tmp_path, monkeypatch):
    case = settlement_case
    root, request = _saved_failure(case, tmp_path)
    calls = _wire(case, monkeypatch, [408, 429])
    generated = case.host.client.generated
    get_task = generated.get_task
    reads = []

    def observe(task_id):
        reads.append(task_id)
        if len(reads) == 1:
            raise ApiError(429, "rate_limited", "task read unavailable")
        return get_task(task_id)

    monkeypatch.setattr(generated, "get_task", observe)
    case.host._recover_terminal_requests()
    assert len(calls) == 3 and calls == [calls[0]] * 3
    assert calls[0][0] == "POST" and calls[0][3] == request["idempotency_key"]
    assert json.loads(calls[0][2]) == request["body"]
    assert json.loads((root / "fail-request.json").read_text()) == request
    stored = case.service.task(case.task_id)["task"]
    assert stored["status"] == "failed" and stored["result"]["error"]["code"] == "producer_failed"
