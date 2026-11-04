"""CPU-only scheduling proof using H3 declarations and the paired Runtime.

No host subprocess, engine, media, network service or provider is started.
Only the clock advances in the expiry case; Runtime lease policy is unchanged.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from astrid.core.execution.generic_host import GenericPackHost
from tests.helpers.runtime import use_runtime

ROOT = Path(__file__).resolve().parents[3]
PARENT = "h3_av.transform"
CHILD = "h3_av.prepare"
WORKER = {"actor": "worker", "scopes": ["worker:execute"]}
pytestmark = pytest.mark.integration


@pytest.fixture(params=[False, True], ids=["declaration", "matrix"])
def scheduling(tmp_path, request):
    runtime_root = use_runtime()
    import runtime_protocol
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore

    assert Path(runtime_protocol.__file__).resolve().is_relative_to(runtime_root)
    host = GenericPackHost(
        pack_roots=[ROOT / "astrid/packs/h3_av"],
        capability_matrix=ROOT / "config/astrid-beta-capabilities.json" if request.param else None,
        max_concurrency=3,
        credential_source={},
    )
    records = {record.id: record for record in host.discover()}
    assert records[PARENT].definition.metadata["tracked_h3_parent"] is True
    assert records[PARENT].definition.metadata["adapter_family"] == "orchestration"
    assert records[PARENT].adapter.family == "orchestration"
    assert not records[PARENT].adapter.requires_network
    assert records[PARENT].resource_keys == ()
    for capability in (CHILD, "h3_av.compile", "h3_av.compose", "h3_av.verify", "h3_av.publication_finalizer"):
        assert records[capability].adapter.family == "cpu"
        assert records[capability].resource_keys == ("cpu",)

    realm = tmp_path / "realm"
    RealmStore.initialize(realm).close()
    service = RuntimeService(realm)
    try:
        # The existing host resource projection is registered unchanged in
        # Runtime; the scheduler itself is real and receives no exemptions.
        for record in records.values():
            service.register_capability({
                "capability_id": record.id,
                "definition_digest": record.capability_digest,
                "required_resource_keys": list(record.resource_keys),
            })
        service.register_executor({
            "executor_id": "worker", "capabilities": list(records),
            "max_concurrency": host.max_concurrency,
            "resource_keys": sorted({key for record in records.values() for key in record.resource_keys}),
        }, idempotency_key="register")
        project = service.create_project({"slug": "h3-scheduling", "name": "H3 scheduling"})["id"]
        service.create_task({
            "project": project, "capability_id": PARENT,
            "capability_digest": records[PARENT].capability_digest,
            "input_object_ids": [], "spec": {}, "idempotency_key": "parent",
            "child_delegation": {
                "capabilities": [{"capability_id": CHILD, "capability_digest": records[CHILD].capability_digest}],
                "targets": [{"kind": "default"}], "input_object_ids": [],
            },
        }, enforce_readiness=True)
        yield service, records
    finally:
        service.close()


def _lease(attempt):
    return {key: attempt[key] for key in ("lease_id", "fence", "runtime_epoch")}


def _claim(service, capability, key):
    return service.claim_next({
        "executor_id": "worker", "capability_ids": [capability],
        "runtime_epoch": service.health()["runtime_epoch"],
    }, idempotency_key=key, identity=WORKER)


def _children(service, records, parent):
    authority = service.issue_child_authority(parent["attempt_id"], _lease(parent), identity=WORKER)["authority"]
    children = []
    for key in ("prepare", "next-prepare"):
        child = service.admit_delegated_child({
            "authority": authority,
            "task": {
                "capability_id": CHILD, "capability_digest": records[CHILD].capability_digest,
                "input_object_ids": [], "spec": {},
            },
        }, idempotency_key=key, identity=WORKER)
        assert child["task"]["spec"]["delegated_parent"]["parent_attempt_id"] == parent["attempt_id"]
        children.append(child["task"]["id"])
    return children, authority


def _reservations(service):
    return [tuple(row) for row in service.store.conn.execute(
        "SELECT task_id, resource_key, lease_token, fence FROM reservations WHERE released_at IS NULL"
    )]


def test_h3_parent_and_cpu_children_progress_with_attempt_fences(scheduling):
    from runtime_protocol.errors import AuthorizationError, ConflictError, LeaseError

    service, records = scheduling
    parent = _claim(service, PARENT, "claim-parent")
    assert parent["attempt_id"] and parent["fence"] == 1
    assert _reservations(service) == []
    children, authority = _children(service, records, parent)
    child = _claim(service, CHILD, "claim-prepare")
    assert child["task_id"] == children[0] and child["fence"] == 1
    assert _reservations(service) == [(child["task_id"], "cpu", child["lease_id"], child["fence"])]
    blocked = _claim(service, CHILD, "claim-blocked-cpu")
    assert blocked["waiting_reason"] == "waiting_for_cpu"
    assert service.task(children[1])["task"]["attempt"] == 0

    # The parent keeps its attempt/owner and can publish semantic progress
    # while prepare holds the only CPU reservation. No lease override.
    heartbeat = service.heartbeat_attempt(parent["attempt_id"], {
        **_lease(parent), "progress": {"phase": "prepare", "current": 1, "total": 7},
    }, idempotency_key="parent-progress")
    assert heartbeat["data"]["attempt_id"] == parent["attempt_id"]
    current = service.task(parent["task_id"])["task"]
    assert current["status"] == "running" and current["executor_id"] == "worker"
    assert current["attempt_id"] == parent["attempt_id"] and current["attempt"] == 1
    assert any(event["kind"] == "task.progress" for event in service.store.list_events(parent["run_id"]))
    with pytest.raises(ConflictError, match="every accepted child"):
        service.settle_attempt(parent["attempt_id"], _lease(parent), idempotency_key="early-parent")
    with pytest.raises(LeaseError):
        service.heartbeat_attempt(child["attempt_id"], {**_lease(child), "fence": 0}, idempotency_key="stale-fence")
    with pytest.raises(AuthorizationError):
        service.heartbeat_attempt(child["attempt_id"], _lease(child), idempotency_key="foreign-owner",
                                  identity={"actor": "foreign", "scopes": ["worker:execute"]})
    assert _reservations(service) == [(child["task_id"], "cpu", child["lease_id"], child["fence"])]

    service.settle_attempt(child["attempt_id"], _lease(child), idempotency_key="settle-prepare")
    assert _reservations(service) == []
    with pytest.raises(LeaseError):
        service.heartbeat_attempt(child["attempt_id"], _lease(child), idempotency_key="settled-child")
    following = _claim(service, CHILD, "claim-next-prepare")
    assert following["task_id"] == children[1]
    service.settle_attempt(following["attempt_id"], _lease(following), idempotency_key="settle-next-prepare")
    service.settle_attempt(parent["attempt_id"], _lease(parent), idempotency_key="settle-parent")
    assert service.task(parent["task_id"])["task"]["status"] == "completed"
    assert _reservations(service) == []


@pytest.mark.parametrize("termination", ["cancel", "expire"])
def test_h3_parent_without_resources_still_contains_and_fences_children(scheduling, monkeypatch, termination):
    from runtime_protocol import lifecycle as runtime_lifecycle, service as runtime_service, store as runtime_store
    from runtime_protocol.errors import ConflictError, LeaseError
    from runtime_protocol.lifecycle import _interruption_report

    service, records = scheduling
    parent = _claim(service, PARENT, "claim-parent")
    children, authority = _children(service, records, parent)
    child = _claim(service, CHILD, "claim-prepare")
    assert child["task_id"] == children[0]
    if termination == "cancel":
        service.cancel(parent["task_id"])
        expected = "cancelled"
    else:
        # Move the Runtime clock past the real parent deadline, without
        # sleeping or modifying either lease duration or persisted leases.
        future = (datetime.fromisoformat(parent["lease_expires_at"]) + timedelta(seconds=1)).astimezone(timezone.utc)
        real_datetime = runtime_store.datetime

        class AfterDeadline(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return future.astimezone(tz) if tz else future.replace(tzinfo=None)

        monkeypatch.setattr(runtime_store, "datetime", AfterDeadline)
        monkeypatch.setattr(runtime_service, "datetime", AfterDeadline)
        monkeypatch.setattr(runtime_lifecycle, "datetime", AfterDeadline)
        expected = "running"
    assert service.task(parent["task_id"])["task"]["status"] == expected
    assert all(service.task(task_id)["task"]["status"] == "cancelled" for task_id in children)
    assert _reservations(service) == []
    for attempt in (parent, child):
        with pytest.raises(LeaseError):
            service.heartbeat_attempt(attempt["attempt_id"], _lease(attempt), idempotency_key="late-" + attempt["task_id"])
        with pytest.raises(LeaseError):
            service.settle_attempt(attempt["attempt_id"], _lease(attempt), idempotency_key="late-settle-" + attempt["task_id"])
    if termination == "expire":
        # Expiry fences the old authority, but does not establish that its
        # executor stopped. The exact parent custody remains a stop blocker.
        retained = service.task(parent["task_id"])["task"]
        assert retained["attempt_id"] == parent["attempt_id"]
        assert retained["attempt"] == 1 and retained["executor_id"] == "worker"
        attempt = service.store.conn.execute(
            "SELECT lease_id, fence, runtime_epoch, settled FROM attempts WHERE id=?",
            (parent["attempt_id"],),
        ).fetchone()
        assert tuple(attempt) == (parent["lease_id"], parent["fence"], parent["runtime_epoch"], 0)
        report = _interruption_report(service.store.conn)
        assert report["safe"] is False
        assert {row["blocker_kind"] for row in report["blockers"] if row["task_id"] == parent["task_id"]} == {
            "running_task", "unsettled_attempt",
        }
        assert any(row["attempt_id"] == parent["attempt_id"] and row["lease_expired"] is True
                   for row in report["unreconciled_attempts"])
        with pytest.raises(LeaseError):
            service.issue_child_authority(parent["attempt_id"], _lease(parent), identity=WORKER)
        with pytest.raises(LeaseError):
            service.admit_delegated_child({
                "authority": authority,
                "task": {"capability_id": CHILD, "capability_digest": records[CHILD].capability_digest,
                         "input_object_ids": [], "spec": {}},
            }, idempotency_key="late-child", identity=WORKER)
        assert _claim(service, PARENT, "claim-before-stop") is None
        with pytest.raises(ConflictError, match="not retryable"):
            service.retry_task(parent["task_id"], {}, idempotency_key="retry-before-stop")

        # The fixture has no real subprocess: the owner explicitly reports
        # that it has stopped and cedes its exact expired attempt.
        service.fail_attempt(parent["attempt_id"], {**_lease(parent), "error": {"code": "lease_lost"}},
                             idempotency_key="stopped-parent", identity=WORKER)
        assert service.task(parent["task_id"])["task"]["status"] == "failed"
        assert service.store.conn.execute("SELECT settled FROM attempts WHERE id=?", (parent["attempt_id"],)).fetchone()[0] == 1
        assert _interruption_report(service.store.conn)["safe"] is True
        monkeypatch.setattr(runtime_store, "datetime", real_datetime)
        monkeypatch.setattr(runtime_service, "datetime", real_datetime)
        monkeypatch.setattr(runtime_lifecycle, "datetime", real_datetime)
        service.retry_task(parent["task_id"], {}, idempotency_key="retry-after-stop")
        assert service.task(parent["task_id"])["task"]["status"] == "queued"
        retried = _claim(service, PARENT, "retry-parent")
        assert retried["task_id"] == parent["task_id"]
        assert retried["attempt_id"] != parent["attempt_id"] and retried["fence"] > parent["fence"]
        assert service.task(parent["task_id"])["task"]["attempt"] == 2
