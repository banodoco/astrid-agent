"""Host publication keeps the exact wire request through transient failures."""
from __future__ import annotations

import json
import multiprocessing
import threading
from types import SimpleNamespace
import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import HostError, RuntimeProtocolClient
from astrid_runtime_client import WorkspaceClient
from astrid_runtime_client.generated import ApiError
from tests.test_generic_host_result_json import host_case  # noqa: F401
from tests.core.execution.test_bug14_settlement_liveness import (  # noqa: F401
    settlement_case, _run, _assert_resolved,
)


@pytest.fixture(autouse=True)
def _isolated_ephemeral_attempt_namespace(tmp_path, monkeypatch):
    # Each test has an independent Runtime with the same fake executor name.
    # Its durable attempt registry must therefore be independent too.
    monkeypatch.setattr(generic_host.tempfile, "tempdir", str(tmp_path))


def _wire(case, monkeypatch, statuses, *, unavailable=False):
    calls = []

    def transport(method, path, headers, body):
        payload = json.loads(body)
        calls.append((method, path, bytes(body), headers["Idempotency-Key"]))
        status = statuses.pop(0) if statuses else None
        if status:
            raise ApiError(status, "publication_test", "publication unavailable")
        if unavailable:
            raise OSError("publication unavailable")
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


@pytest.mark.parametrize("failed", [False, True], ids=["settlement", "failure"])
def test_host_total_replay_bound_is_visibly_unresolved(settlement_case, monkeypatch, failed):
    case = settlement_case
    case.mode = "replay"
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_MAX_ROUNDS", 3, raising=False)
    calls = _wire(case, monkeypatch, [], unavailable=True)
    if failed:
        _fail_after_outputs(case, monkeypatch)
    with pytest.raises(OSError, match="publication unavailable"):
        _run(case)
    _join(case)
    assert len(calls) == 8  # two synchronous writes, then three rounds of two
    assert calls == [calls[0]] * 8
    assert case.service.task(case.task_id)["task"]["status"] == "running"
    marker = json.loads((case.root / "publication-unresolved.json").read_text())
    assert marker["status"] == "unresolved" and marker["rounds"] == 3
    receipt = json.loads((case.root / "result.json").read_text())
    assert receipt["status"] != "succeeded"
    assert receipt["publication"]["status"] == "unresolved"
    assert case.host._shutdown.is_set()
    assert (case.root / ("fail-request.json" if failed else "settle-request.json")).exists()
    assert (case.root / "outputs").is_dir()
    # A restarted host must surface unresolved custody before claiming work.
    case.host._shutdown.clear()
    case.host.attempt_root = case.root
    with pytest.raises(HostError, match="unresolved terminal publication"):
        case.host.run(once=True)


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


def test_exhausted_slot_allows_owned_sibling_to_release_reservation(settlement_case, monkeypatch):
    case = settlement_case
    case.mode = "replay"
    coordinator = case.host
    monkeypatch.setattr(coordinator.client, "fork_for_host_slot", lambda: coordinator.client)
    exhausted, healthy = coordinator._new_claim_slot(), coordinator._new_claim_slot()
    with case.service.store._mutex:
        case.service.register_executor({"executor_id": "bug14-worker", "capabilities": ["test.echo"],
            "max_concurrency": 2, "runtime_epoch": case.attempt["runtime_epoch"]},
            idempotency_key="two-slots")
        created = case.service.create_task({"capability_id": "test.echo",
            "capability_digest": coordinator.capabilities["test.echo"].capability_digest,
            "idempotency_key": "sibling"})
        sibling = case.service.claim_next({"executor_id": "bug14-worker", "capability_ids": ["test.echo"],
            "runtime_epoch": case.attempt["runtime_epoch"]}, idempotency_key="sibling-claim")
    assert sibling["task_id"] == created["task"]["id"]
    # The sibling already owns its attempt before the other slot exhausts.
    owned, release_sibling = threading.Event(), threading.Event()
    errors = []

    def release():
        with case.service.store._mutex:
            return case.service.fail_attempt(sibling["attempt_id"], {
                **{key: sibling[key] for key in ("lease_id", "fence", "runtime_epoch")},
                "error": {"code": "sibling_stopped"}}, idempotency_key="sibling-fail")

    def sibling_owner():
        try:
            assert healthy._handoff_begin_direct_attempt()
            owned.set()
            assert release_sibling.wait(2)
            healthy._handoff_settlement_call(release)
        except BaseException as exc:
            errors.append(exc)
        finally:
            healthy._handoff_leave_attempt(threading.get_ident())

    worker = threading.Thread(target=sibling_owner)
    worker.start()
    assert owned.wait(2)
    monkeypatch.setattr(exhausted, "_allocate_attempt_root", coordinator._allocate_attempt_root)
    monkeypatch.setattr(exhausted, "_run_command_definition", coordinator._run_command_definition)
    case.host = exhausted
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_MAX_ROUNDS", 1)
    _wire(case, monkeypatch, [], unavailable=True)
    try:
        with pytest.raises(OSError, match="publication unavailable"):
            _run(case)
        _join(case)
        assert (case.root / "publication-unresolved.json").is_file()
        assert coordinator.handoff_quiescence()["observation_status"] == "unknown"
        assert not coordinator._handoff_enter_claim_rpc()

        release_sibling.set()
        worker.join(timeout=3)
        assert not worker.is_alive()
        assert not errors
        assert case.service.task(sibling["task_id"])["task"]["status"] == "failed"
        assert case.service.store.conn.execute(
            "SELECT count(*) FROM reservations WHERE task_id=? AND released_at IS NULL",
            (sibling["task_id"],)).fetchone()[0] == 0
        assert case.service.task(case.task_id)["task"]["status"] == "running"
    finally:
        release_sibling.set()
        worker.join(timeout=3)
        assert not worker.is_alive()


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


@pytest.mark.parametrize("fault", ["read", "write", "cede"])
def test_recovery_total_bound_is_visibly_unresolved(settlement_case, tmp_path, monkeypatch, fault):
    case = settlement_case
    root, request = _saved_failure(case, tmp_path)
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_MAX_ROUNDS", 3, raising=False)
    calls = _wire(case, monkeypatch, [400] if fault == "cede" else [], unavailable=True)
    if fault == "read":
        def unreadable(*args):
            raise ApiError(429, "rate_limited", "task read unavailable")
        monkeypatch.setattr(case.host.client.generated, "get_task", unreadable)
    # Baseline has no total bound; allow it to leave its loop so this test
    # can report the missing unresolved state without hanging the runner.
    real_wait = case.host._shutdown.wait
    waits = []

    def wait(delay):
        waits.append(delay)
        if len(waits) == 4:
            case.host._shutdown.set()
        return real_wait(delay)

    monkeypatch.setattr(case.host._shutdown, "wait", wait)
    with pytest.raises(HostError, match="unresolved terminal publication"):
        case.host._recover_terminal_requests()
    marker = json.loads((root / "publication-unresolved.json").read_text())
    assert marker["status"] == "unresolved" and marker["rounds"] == 3
    assert len(waits) <= 2
    assert len(calls) <= 5
    if fault == "read":
        assert not calls  # failed observations never authorize a cede
    if fault == "cede":
        assert calls[1][3].endswith("-cede")
        assert calls[1:] == [calls[1]] * (len(calls) - 1)
    assert json.loads((root / "fail-request.json").read_text()) == request
    assert case.service.task(case.task_id)["task"]["status"] == "running"


def _fresh_host(case, root):
    return generic_host.GenericPackHost(pack_roots=list(case.host.pack_roots), client=case.host.client,
        executor_id=case.host.executor_id, attempt_root=root,
        capability_matrix=case.host.capability_matrix_path, execution_policy=case.host.execution_policy)


def test_interrupted_recovery_preserves_consumed_budget_for_fresh_host(
    settlement_case, tmp_path, monkeypatch,
):
    case = settlement_case
    root, request = _saved_failure(case, tmp_path)
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_MAX_ROUNDS", 3)
    calls = _wire(case, monkeypatch, [], unavailable=True)

    class Interrupted(BaseException):
        pass

    waits = []

    def crash(delay):
        waits.append(delay)
        if len(waits) == 2:
            raise Interrupted()

    monkeypatch.setattr(case.host._shutdown, "wait", crash)
    with pytest.raises(Interrupted):
        case.host._recover_terminal_requests()
    assert len(calls) == 2
    assert not (root / "publication-unresolved.json").exists()
    fresh = _fresh_host(case, root)
    with pytest.raises(HostError, match="unresolved terminal publication"):
        fresh._recover_terminal_requests()
    assert len(calls) == 3  # Only the unconsumed round survives a restart.
    assert calls == [calls[0]] * 3
    assert json.loads((root / "fail-request.json").read_text()) == request
    assert json.loads((root / "publication-unresolved.json").read_text())["rounds"] == 3
    assert case.service.task(case.task_id)["task"]["status"] == "running"


def test_live_reconciliation_and_restart_share_consumed_budget(settlement_case, monkeypatch):
    case = settlement_case
    case.mode = "replay"
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_MAX_ROUNDS", 3)
    calls = _wire(case, monkeypatch, [], unavailable=True)
    _fail_after_outputs(case, monkeypatch)
    with pytest.raises(OSError, match="publication unavailable"):
        _run(case)
    _join(case)
    assert len(calls) == 8
    # Model interruption after charging the final round but before writing
    # the unresolved marker. Only the pre-I/O budget survives this restart.
    (case.root / "publication-unresolved.json").unlink()
    fresh = _fresh_host(case, case.root)
    with pytest.raises(HostError, match="unresolved terminal publication"):
        fresh._recover_terminal_requests()
    assert len(calls) == 8
    assert json.loads((case.root / "publication-unresolved.json").read_text())["rounds"] == 3
    assert case.service.task(case.task_id)["task"]["status"] == "running"


def _recovery_process(root, executor_id, ready, start, observations, reads, errors):
    """Independent processes consume the same recovery budget through the host."""
    generic_host._TERMINAL_RECONCILE_MAX_ROUNDS = 3
    count = 0

    def unavailable(task_id):
        nonlocal count
        count += 1
        if count == 1:
            observations.wait(timeout=5)
        raise OSError("offline observation")

    host = generic_host.GenericPackHost(pack_roots=[], client=SimpleNamespace(task=unavailable),
                                       executor_id=executor_id, attempt_root=root)
    host._shutdown.wait = lambda delay: None
    ready.put(True)
    if not start.wait(5):
        errors.put("process start was not released")
        return
    try:
        host._recover_terminal_requests()
    except HostError as exc:
        if "unresolved terminal publication" not in str(exc):
            errors.put(str(exc))
    except BaseException as exc:
        errors.put(repr(exc))
    finally:
        reads.put(count)


def test_concurrent_recovery_processes_share_one_total_budget(settlement_case, tmp_path):
    case = settlement_case
    root, _ = _saved_failure(case, tmp_path)
    context = multiprocessing.get_context("spawn")
    ready, reads, errors, start = context.Queue(), context.Queue(), context.Queue(), context.Event()
    observations = context.Barrier(2)
    workers = [context.Process(target=_recovery_process,
        args=(root, case.host.executor_id, ready, start, observations, reads, errors)) for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        assert all(ready.get(timeout=10) for _ in workers)
        start.set()
        for worker in workers:
            worker.join(timeout=10)
            assert not worker.is_alive() and worker.exitcode == 0
        assert sum(reads.get(timeout=2) for _ in workers) == 3
        assert errors.empty()
        assert json.loads((root / "publication-unresolved.json").read_text())["rounds"] == 3
    finally:
        start.set()
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=3)


def test_cede_is_journaled_and_restart_replays_its_exact_wire(settlement_case, tmp_path, monkeypatch):
    case = settlement_case
    root, original = _saved_failure(case, tmp_path)
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_MAX_ROUNDS", 3)
    calls = _wire(case, monkeypatch, [400], unavailable=True)

    class Interrupted(BaseException):
        pass

    waits = []

    def crash(delay):
        waits.append(delay)
        if len(waits) == 2:
            raise Interrupted()

    monkeypatch.setattr(case.host._shutdown, "wait", crash)
    with pytest.raises(Interrupted):
        case.host._recover_terminal_requests()
    cede_path = root / "cede-request.json"
    assert cede_path.is_file()
    cede = json.loads(cede_path.read_text())
    assert cede["idempotency_key"].endswith("-cede")
    assert json.loads((root / "fail-request.json").read_text()) == original
    assert json.loads(calls[-1][2]) == cede["body"]
    # A fresh host replays that journal even when the lease has since expired.
    restarted = _fresh_host(case, root)
    replayed = _wire(case, monkeypatch, [])
    get_task = restarted.client.generated.get_task
    monkeypatch.setattr(restarted.client.generated, "get_task", lambda task_id: {
        **get_task(task_id), "lease_expires_at": "2000-01-01T00:00:00Z"})
    restarted._recover_terminal_requests()
    assert replayed == [calls[-1]]
    assert case.service.task(case.task_id)["task"]["status"] == "failed"
    # The acknowledgement used the final round. Another restart must preserve
    # that resolution rather than mark the retained older journals unresolved.
    _fresh_host(case, root)._recover_terminal_requests()
    assert replayed == [calls[-1]]
    assert not (root / "publication-unresolved.json").exists()


@pytest.mark.parametrize("storage", ["root", "base", "ephemeral"])
@pytest.mark.parametrize("entry", ["run_task", "supervisor", "claim", "delegated"])
def test_fresh_host_entrypoints_honor_durable_publication_latch(
    settlement_case, tmp_path, monkeypatch, storage, entry,
):
    case = settlement_case
    root, _ = _saved_failure(case, tmp_path)
    case.host._unresolved_terminal_publication(root, case.task_id, case.attempt["attempt_id"], 12)
    if storage == "ephemeral":
        renamed = tmp_path / "astrid-attempt-unresolved"
        root.rename(renamed)
        root = renamed
    before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    fresh = _fresh_host(case, root)
    if storage != "root":
        fresh.attempt_root = None
        fresh.attempt_base = tmp_path if storage == "base" else None
    assert fresh is not case.host and not fresh._shutdown.is_set()
    fresh.capabilities = dict(case.host.capabilities)
    executions, claims = [], []
    monkeypatch.setattr(fresh, "_run_task_impl", lambda *a, **kw: executions.append(a) or {"ok": True})
    monkeypatch.setattr(fresh, "_claim_once_impl", lambda *a: claims.append(a))
    frame = {"task": case.task, "task_id": case.task_id, "lease_id": case.attempt["lease_id"],
             "attempt_id": case.attempt["attempt_id"], "fence": case.attempt["fence"]}
    if entry == "supervisor":
        supervisor = fresh.persistent_supervisor(tmp_path / "supervisor.jsonl")
        response = supervisor.start(frame, run_to_terminal=True)
        assert response["status"] == "failed"
        assert "unresolved terminal publication" in json.dumps(response)
    else:
        with pytest.raises(HostError, match="unresolved terminal publication"):
            if entry == "run_task":
                fresh.run_task(case.task, lease_token=case.attempt["lease_id"])
            elif entry == "claim":
                fresh.claim_once()
            else:
                # Readiness is deliberately stopped at a sentinel. The durable
                # latch must run before any delegated admission work at all.
                monkeypatch.setattr(fresh, "_registration_admission_reason",
                                    lambda: claims.append("delegated admission") or "sentinel")
                fresh.run_delegated_child(case.task["task"], parent={}, authority_client=None,
                                          target={}, live=lambda: None)
    assert not executions and not claims
    assert fresh._shutdown.is_set()
    assert fresh.handoff_quiescence()["observation_status"] == "unknown"
    assert {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()} == before
    assert case.service.task(case.task_id)["task"]["status"] == "running"
