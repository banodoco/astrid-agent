"""A stale terminal request must not monopolize host admission."""
from __future__ import annotations

# Pytest fixtures are deliberately imported and consumed by argument name.
# ruff: noqa: F811
import json
import os
import threading
import time

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, HostError
from astrid.sdk.storage_ownership import reclaim
from astrid_runtime_client.generated import ApiError
from tests.core.execution.test_bug14_settlement_liveness import settlement_case  # noqa: F401
from tests.core.execution.test_terminal_publication_replay import _saved_failure
from tests.test_generic_host_result_json import _success_boundary, host_case  # noqa: F401


def _bounded_run(host, call):
    errors = []
    finished = threading.Event()

    def run():
        try:
            call()
        except Exception as exc:  # noqa: BLE001 - relay worker failures to pytest
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert finished.wait(2), "stale recovery blocked generation admission"
        if errors:
            raise errors[0]
    finally:
        host._shutdown.set()
        thread.join(timeout=2)
        assert not thread.is_alive()


def _unknown_request(root, executor):
    root.mkdir(parents=True)
    request = {"operation": "fail_attempt", "identifier": "unknown-attempt",
               "task_id": "unknown-task", "executor_id": executor,
               "idempotency_key": "stale-failure", "body": {
                   "lease_id": "old-lease", "fence": 1, "runtime_epoch": 1,
                   "error": {"code": "old_failure"}, "outputs": [], "result": {}}}
    path = root / "fail-request.json"
    path.write_text(json.dumps(request))
    return path


@pytest.mark.parametrize("configured", [False, True], ids=["shared-tmp", "configured-root"])
def test_foreign_stale_request_does_not_block_generation(host_case, tmp_path, monkeypatch, capsys, configured):
    case = host_case
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(generic_host.tempfile, "gettempdir", lambda: str(temporary))
    case.host.attempt_root = None
    if configured:
        case.host.attempt_base = temporary / "configured"
        stale = case.host.attempt_base / "stale"
    else:
        stale = temporary / "astrid-attempt-stale"
    path = _unknown_request(stale, case.host.executor_id)
    before = path.read_bytes()
    reads = []
    task = case.runtime.task

    def read(task_id):
        reads.append(task_id)
        if task_id == "unknown-task":
            raise ApiError(404, "not_found", "task belongs to another Runtime")
        return task(task_id)

    monkeypatch.setattr(case.runtime, "task", read)
    _success_boundary(case, monkeypatch)
    monkeypatch.setattr(case.host, "claim_once", lambda: case.host.run_task(case.task, lease_token="lease-result"))
    _bounded_run(case.host, lambda: case.host.run(once=True))
    assert len(case.runtime.settlements) == 1
    assert path.read_bytes() == before
    assert sorted(p.name for p in stale.iterdir()) == ["fail-request.json"]
    if configured:
        assert reads.count("unknown-task") == 1
        assert capsys.readouterr().err.count("generic host recovery skipped") == 1
    else:
        assert "unknown-task" not in reads


def test_default_attempt_namespaces_are_isolated_and_reclaimable(tmp_path, monkeypatch):
    monkeypatch.setattr(generic_host.tempfile, "gettempdir", lambda: str(tmp_path))
    first = GenericPackHost(pack_roots=[])
    second = GenericPackHost(pack_roots=[])
    left = first._allocate_attempt_root("task", "attempt")
    right = second._allocate_attempt_root("task", "attempt")
    assert left.parent != right.parent
    assert left.name.startswith("astrid-attempt-")
    assert right.name.startswith("astrid-attempt-")
    assert left.parent.parent == right.parent.parent == tmp_path
    (left / "fail-request.json").write_text("foreign, unreadable evidence")
    reads = []

    class Runtime:
        def task(self, task_id):
            reads.append(task_id)
            raise AssertionError("cross-host read")

    second.client = Runtime()
    second._recover_terminal_requests()
    assert not reads
    assert (left / "fail-request.json").read_text() == "foreign, unreadable evidence"
    then = time.time() - 3 * 86400
    os.utime(left.parent, (then, then))
    os.utime(right.parent, (then, then))
    os.utime(left, (then, then))
    report = reclaim(tmp_path / "data", temporary_root=tmp_path, cache_root=tmp_path / "cache")
    attempts = next(row for row in report["roots"] if row["name"] == "attempt-roots")
    assert attempts["expired_count"] == 1
    assert not left.exists() and right.exists()


@pytest.mark.parametrize("foreign", ["executor", "epoch"])
def test_positive_foreign_identity_preserves_all_files(settlement_case, tmp_path, monkeypatch, capsys, foreign):
    case = settlement_case
    root, request = _saved_failure(case, tmp_path)
    if foreign == "executor":
        request["executor_id"] = "another-worker"
    else:
        request["body"]["runtime_epoch"] += 100
    path = root / "fail-request.json"
    path.write_text(json.dumps(request))
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    _bounded_run(case.host, case.host._recover_terminal_requests)
    assert not case.failures
    assert case.service.task(case.task_id)["task"]["status"] == "running"
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    assert capsys.readouterr().err.count("generic host recovery skipped") == 1


@pytest.mark.parametrize("damage", ["unreadable", "missing-owner"])
def test_uncertain_own_identity_surfaces_bounded_error(settlement_case, tmp_path, monkeypatch, capsys, damage):
    case = settlement_case
    root, request = _saved_failure(case, tmp_path)
    if damage == "unreadable":
        content = "{broken JSON"
    else:
        request.pop("executor_id")
        content = json.dumps(request)
    (root / "fail-request.json").write_text(content)
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_POLL_SECONDS", 0)
    with pytest.raises(HostError, match="recovery"):
        _bounded_run(case.host, case.host._recover_terminal_requests)
    assert (root / "fail-request.json").read_text() == content
    assert "generic host recovery could not read" in capsys.readouterr().err
    assert not case.failures


def test_known_unrecoverable_evidence_cedes_to_runtime_failure(settlement_case, tmp_path, monkeypatch, capsys):
    case = settlement_case
    root, request = _saved_failure(case, tmp_path)
    original = case.host.client.generated.fail_attempt
    calls = []

    def refuse_evidence(attempt_id, **body):
        calls.append(body)
        if "result" in body:
            raise OSError("evidence endpoint unavailable")
        return original(attempt_id, **body)

    monkeypatch.setattr(case.host.client.generated, "fail_attempt", refuse_evidence)
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_POLL_SECONDS", 0)
    _bounded_run(case.host, case.host._recover_terminal_requests)
    stored = case.service.task(case.task_id)["task"]
    assert stored["status"] == "failed"
    assert stored["result"]["error"]["code"] == "reconciliation_required"
    assert "recovery exhausted" in stored["result"]["error"]["message"]
    assert len(calls) == generic_host._TERMINAL_RECOVERY_MAX_ATTEMPTS + 1
    assert json.loads((root / "fail-request.json").read_text()) == request
    assert "generic host recovery exhausted" in capsys.readouterr().err
    case.host._shutdown.clear()
    case.host._recover_terminal_requests()
    assert len(calls) == generic_host._TERMINAL_RECOVERY_MAX_ATTEMPTS + 1


def test_recovery_lost_ack_reads_durable_evidence_before_any_retry(settlement_case, tmp_path, monkeypatch):
    case = settlement_case
    _, request = _saved_failure(case, tmp_path)
    generated = case.host.client.generated
    original = generated.fail_attempt
    calls = []
    readbacks = []
    verify = case.host.client._verify_terminal_evidence_readback

    def lost_ack(attempt_id, **body):
        calls.append(body)
        original(attempt_id, **body)
        raise OSError("committed but ACK lost")

    def readback(*args, **kwargs):
        readbacks.append(args)
        return verify(*args, **kwargs)

    monkeypatch.setattr(generated, "fail_attempt", lost_ack)
    monkeypatch.setattr(case.host.client, "_verify_terminal_evidence_readback", readback)
    _bounded_run(case.host, case.host._recover_terminal_requests)
    assert len(calls) == 1
    assert readbacks
    assert readbacks[0][1]["error"] == request["body"]["error"]
    assert case.service.task(case.task_id)["task"]["status"] == "failed"


def test_exhausted_cede_is_visible_and_replayed_exactly_after_restart(settlement_case, tmp_path, monkeypatch):
    case = settlement_case
    root, request = _saved_failure(case, tmp_path)
    generated = case.host.client.generated
    original = generated.fail_attempt
    calls = []

    def outage(attempt_id, **body):
        calls.append(body)
        raise OSError("Runtime unavailable")

    monkeypatch.setattr(generated, "fail_attempt", outage)
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_POLL_SECONDS", 0)
    with pytest.raises(HostError, match="recovery could not reconcile"):
        _bounded_run(case.host, case.host._recover_terminal_requests)
    assert len(calls) == 2 * generic_host._TERMINAL_RECOVERY_MAX_ATTEMPTS
    assert case.service.task(case.task_id)["task"]["status"] == "running"
    assert json.loads((root / "fail-request.json").read_text()) == request
    cede = calls[-1]
    assert "result" not in cede and cede["error"]["code"] == "reconciliation_required"
    saved = root / "fail-request.json.reconciliation.json"
    persisted = saved.read_bytes()

    def restored(attempt_id, **body):
        calls.append(body)
        return original(attempt_id, **body)

    monkeypatch.setattr(generated, "fail_attempt", restored)
    restarted = GenericPackHost(pack_roots=[], client=case.host.client,
                                executor_id=case.host.executor_id, attempt_root=root)
    _bounded_run(restarted, restarted._recover_terminal_requests)
    assert calls[-1] == cede
    assert len(calls) == 2 * generic_host._TERMINAL_RECOVERY_MAX_ATTEMPTS + 1
    assert saved.read_bytes() == persisted
    assert case.service.task(case.task_id)["task"]["status"] == "failed"


def test_success_response_with_unrelated_result_requires_real_failure_readback(settlement_case, tmp_path, monkeypatch):
    case = settlement_case
    _, request = _saved_failure(case, tmp_path)
    generated = case.host.client.generated
    original = generated.fail_attempt
    calls = []

    def uncertain_response(attempt_id, **body):
        calls.append(body)
        if len(calls) == 1:
            return {"task": {**generated.get_task(case.task_id), "result": {"unrelated": True}}}
        return original(attempt_id, **body)

    monkeypatch.setattr(generated, "fail_attempt", uncertain_response)
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_POLL_SECONDS", 0)
    _bounded_run(case.host, case.host._recover_terminal_requests)
    assert len(calls) == 2 and calls[0] == calls[1]
    stored = case.service.task(case.task_id)["task"]
    assert stored["status"] == "failed"
    assert stored["result"]["error"] == request["body"]["error"]
