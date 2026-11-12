"""Design-level decisions; owner, provider and writer-guard wiring comes later."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from astrid.core.execution.transitions import (
    Data,
    Decision,
    Record,
    Rule,
    UndefinedTransition,
    transition,
)
from astrid.packs.runpod.lease_states import (
    ATTACHMENT_STATES,
    CLEANUP_STATES,
    CUSTODY_CONTROL_FIELDS,
    EXCLUDED_LEASE_ACTIONS,
    IDLE_STATES,
    LEASE_TRANSITIONS,
    RELEASED_STATES,
    RELEASING_STATES,
    UNKNOWN_CUSTODY,
    ZERO_CUSTODY,
    BillingAction,
    InvalidLeaseTransition,
    LeaseEvent,
    LeaseState,
    dispatch_ticket,
    startup_recovery_eligible,
)


def record(state: LeaseState, **changes: object) -> Record[LeaseState]:
    data: dict[str, object] = {
        "operation_id": "operation-1", "pod_id": "pod-1", "now": 100.0,
        "paid_expires_at": 300.0, "keep_deadline": 200.0, "idle_seconds": 50.0,
        "create_inflight": False, "create_attempt": "create-1",
        "stop_requested": False, "cancellation_fence": 0, "owner_epoch": "epoch-1",
        "release_authorized": True, "dispatch_attempt": "observe-1",
        "dispatch_kind": "observe", "dispatch_fence": 0, "dispatch_deadline": 300.0,
        "dispatch_owner": "epoch-1", "dispatch_admitted": True,
        "attempt_ids": ("create-1", "observe-1"),
    }
    if state in ZERO_CUSTODY | UNKNOWN_CUSTODY:
        data["pod_id"] = None
    if state in UNKNOWN_CUSTODY:
        data.update(create_inflight=True, dispatch_attempt="create-1", dispatch_kind="create")
    if state in ATTACHMENT_STATES:
        data.update(pod_id=None if state == LeaseState.ATTACHMENT_PENDING else "pod-1",
                    requested_pod_id="pod-1")
    if state in CLEANUP_STATES | RELEASING_STATES:
        data.update(stop_requested=True, observations_abandoned=True, cancellation_fence=1)
    if state in ZERO_CUSTODY:
        data.update(create_attempt=None, dispatch_attempt=None)
    return Record(state, {**data, **changes})


class Journal:
    def __init__(self, initial: Record[LeaseState], path: Path | None = None) -> None:
        self.current = initial
        self.entries: list[tuple[Record[LeaseState], tuple[str, ...]]] = []
        self.retries: list[tuple[Record[LeaseState], LeaseEvent, Data]] = []
        self.path = path
        self.lock = threading.Lock()

    def persist(self, value: Record[LeaseState], effects: tuple[str, ...]) -> None:
        assert value.revision == self.current.revision + 1
        if self.path is not None:
            # A real durable sink for the late-result/reopen contract, no provider.
            with self.path.open("w") as stream:
                json.dump({"state": value.state.name, "data": dict(value.data),
                           "revision": value.revision, "effects": effects}, stream)
                stream.flush()
                os.fsync(stream.fileno())
        self.current = value
        self.entries.append((value, effects))

    def retry(self, value: Record[LeaseState], event: LeaseEvent, changes: Data) -> None:
        self.retries.append((value, event, changes))

    def step(self, event: LeaseEvent, **changes: object) -> Decision[LeaseState, BillingAction]:
        with self.lock:
            return transition(LEASE_TRANSITIONS, self.current, event, changes=changes,
                              persist=self.persist, retry=self.retry)


def assert_decision(
    decision: Decision[LeaseState, BillingAction], state: LeaseState, action: BillingAction,
) -> None:
    assert (decision.record.state, decision.action) == (state, action)


def finish_cleanup(journal: Journal, decision: Decision[LeaseState, BillingAction]) -> None:
    """Successful fake action completions; never inject another user/timer event.

    Release is only confirmed after the fake's exact drain and absence result.
    Failures/outages are tested separately; no table can manufacture absence.
    """
    for _ in range(4):
        if decision.action == BillingAction.KEEP_WITH_DEADLINE:
            assert decision.record.data["keep_deadline"] <= decision.record.data["paid_expires_at"]
            return
        if decision.record.state in RELEASED_STATES:
            assert decision.action == BillingAction.NO_BILLABLE_RESOURCE
            return
        if decision.record.state in UNKNOWN_CUSTODY:
            decision = journal.step(LeaseEvent.LATE_CREATE_SUCCEEDED, attempt_id="create-1", pod_id="pod-1")
        elif decision.record.state in CLEANUP_STATES:
            decision = journal.step(LeaseEvent.QUIESCED, drain_confirmed=True)
        elif decision.record.state in RELEASING_STATES:
            decision = journal.step(LeaseEvent.RELEASE_SUCCEEDED, absence_proof=True)
        else:
            pytest.fail(f"no internal completion chain for {decision}")
    pytest.fail("cleanup did not settle in its bounded internal completion chain")


@pytest.mark.parametrize("state", list(LeaseState))
@pytest.mark.parametrize("event", [
    LeaseEvent.RESTART, LeaseEvent.HOST_DEATH, LeaseEvent.PAID_EXPIRY,
    LeaseEvent.IDLE_EXPIRY, LeaseEvent.JOB_FAILED, LeaseEvent.RECIPE_CHANGED,
    LeaseEvent.MODEL_CHANGED, LeaseEvent.SOURCE_CHANGED, LeaseEvent.CANCEL,
])
@pytest.mark.parametrize("now", [100.0, 350.0])
def test_every_state_has_a_bounded_safety_outcome(state: LeaseState, event: LeaseEvent, now: float) -> None:
    journal = Journal(record(state, now=now))
    decision = journal.step(event, jobs_stopped=True, custody_verified=True)
    finish_cleanup(journal, decision)


@pytest.mark.parametrize("state", [LeaseState.CLEANUP, LeaseState.RELEASING])
def test_b1_restart_pending_reconciles_then_releases_exact_pod(state: LeaseState) -> None:
    journal = Journal(record(state))
    decision = journal.step(LeaseEvent.RESTART)
    assert_decision(decision, LeaseState.CLEANUP, BillingAction.RECONCILE_THEN_RELEASE)
    finish_cleanup(journal, decision)
    assert {row.data["pod_id"] for row, _ in journal.entries} == {"pod-1"}
    assert not any("write_ahead_create_attempt" in effects for _, effects in journal.entries)


@pytest.mark.parametrize("event", [
    LeaseEvent.RECIPE_CHANGED, LeaseEvent.MODEL_CHANGED, LeaseEvent.SOURCE_CHANGED,
])
def test_b2_changed_idle_claim_is_released_on_restart(event: LeaseEvent) -> None:
    journal = Journal(record(LeaseState.IDLE_READY))
    assert_decision(journal.step(event), LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD)
    restarted = Journal(journal.current)
    decision = restarted.step(LeaseEvent.RESTART)
    assert_decision(decision, LeaseState.CLEANUP, BillingAction.RECONCILE_THEN_RELEASE)
    finish_cleanup(restarted, decision)


def test_b3_preparation_finishing_after_paid_expiry_cannot_execute() -> None:
    journal = Journal(record(LeaseState.PREPARING, now=300.0))
    decision = journal.step(LeaseEvent.PREPARATION_SUCCEEDED)
    assert_decision(decision, LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD)
    repeated = journal.step(LeaseEvent.PREPARATION_SUCCEEDED)
    assert_decision(repeated, LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD)
    assert repeated.record.data["archived_preparation"] == "prepared"
    finish_cleanup(journal, decision)


def test_b4_acquisition_paid_expiry_releases_without_another_create() -> None:
    journal = Journal(record(LeaseState.ACQUIRING, now=300.0))
    decision = journal.step(LeaseEvent.PAID_EXPIRY)
    assert_decision(decision, LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD)
    assert decision.record.data["observations_abandoned"] is True
    finish_cleanup(journal, decision)
    assert not any("write_ahead_create_attempt" in effects for _, effects in journal.entries)


def test_b5_idle_expiry_counts_only_idle_time() -> None:
    idle = Journal(record(LeaseState.IDLE_READY))
    assert_decision(idle.step(LeaseEvent.IDLE_EXPIRY), LeaseState.CLEANUP,
                    BillingAction.RELEASE_EXACT_POD)
    active = Journal(record(LeaseState.ACTIVE_READY))
    decision = active.step(LeaseEvent.IDLE_EXPIRY)
    assert_decision(decision, LeaseState.ACTIVE_READY, BillingAction.KEEP_WITH_DEADLINE)
    assert decision.record.data["keep_deadline"] == 300.0
    settled = active.step(LeaseEvent.JOB_SUCCEEDED, now=180.0)
    assert settled.record.data["keep_deadline"] == 230.0


@pytest.mark.parametrize("state", list(IDLE_STATES))
def test_b6_host_death_preserves_previously_durable_retention_deadline(state: LeaseState) -> None:
    journal = Journal(record(state))
    before = dict(journal.current.data)
    decision = journal.step(LeaseEvent.HOST_DEATH)
    assert_decision(decision, state, BillingAction.KEEP_WITH_DEADLINE)
    assert decision.effects == ()
    assert dict(decision.record.data) == before
    restarted = Journal(journal.current)
    finish_cleanup(restarted, restarted.step(LeaseEvent.RESTART, now=350.0))
    assert restarted.current.state in RELEASED_STATES


def test_b7_release_annotation_write_failure_stays_inside_retry_boundary() -> None:
    journal = Journal(record(LeaseState.CLEANUP))

    def unavailable(value: Record[LeaseState], effects: tuple[str, ...]) -> None:
        assert "release_annotation_in_retry_boundary" in effects
        raise OSError("journal temporarily unavailable")

    with pytest.raises(OSError):
        transition(LEASE_TRANSITIONS, journal.current, LeaseEvent.RELEASE_RETRY,
                   persist=unavailable, retry=journal.retry)
    assert journal.retries == [(journal.current, LeaseEvent.RELEASE_RETRY, {})]
    decision = journal.step(LeaseEvent.RELEASE_RETRY)
    assert_decision(decision, LeaseState.CLEANUP, BillingAction.RECONCILE_THEN_RELEASE)
    finish_cleanup(journal, decision)


@pytest.mark.parametrize("state", ["running", "cancel_requested"])
def test_b8_cancel_requested_exact_owner_is_recovered_by_production_rule(state: str) -> None:
    identity = {"attempt_id": "a", "expected_attempt_id": "a", "fence": "f",
                "expected_fence": "f", "executor_id": "e", "expected_executor_id": "e"}
    assert startup_recovery_eligible(state, **identity)
    assert not startup_recovery_eligible("completed", **identity)
    for key in ("attempt_id", "fence", "executor_id"):
        assert not startup_recovery_eligible(state, **{**identity, key: "foreign"})
    lease = Journal(record(LeaseState.CLEANUP, cancel_requested=True))
    assert_decision(lease.step(LeaseEvent.RESTART), LeaseState.CLEANUP,
                    BillingAction.RECONCILE_THEN_RELEASE)


def test_b9_generic_table_journals_every_cede_attempt_before_post() -> None:
    # Deferred contract fixture; not a production cede policy or Release guarantee.
    table = {("cede_pending", "cede_attempt"): Rule(
        "cede_pending", "post_exact_cede", ("write_ahead_cede_attempt",))}
    saved = []
    posts = []
    current = Record("cede_pending", {"attempt": "a", "fence": "f", "body": "unchanged"})
    for _ in range(3):
        decision = transition(
            table, current, "cede_attempt",
            persist=lambda value, effects: saved.append((value, effects)),
            retry=lambda value, event, changes: pytest.fail("unexpected write failure"),
        )
        assert len(saved) == len(posts) + 1
        assert saved[-1] == (decision.record, ("write_ahead_cede_attempt",))
        assert (decision.record.state, decision.action) == ("cede_pending", "post_exact_cede")
        posts.append(dict(decision.record.data))
        current = decision.record
    assert len(saved) == len(posts) == 3
    assert posts == [dict(current.data)] * 3
    assert [value.revision for value, _ in saved] == [1, 2, 3]


def test_b10_transition_lock_is_released_before_owner_action_or_late_callback() -> None:
    journal = Journal(record(LeaseState.CREATE_PENDING))
    journal.step(LeaseEvent.CANCEL)
    result = []
    callback = threading.Thread(target=lambda: result.append(journal.step(
        LeaseEvent.LATE_CREATE_SUCCEEDED, attempt_id="create-1", pod_id="late-pod")))
    callback.start()
    callback.join(timeout=1)
    assert not callback.is_alive(), "late-result journalling must not wait for owner close"
    assert_decision(result[0], LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD)
    assert journal.current.data["pod_id"] == "late-pod"


@pytest.mark.parametrize("event", [LeaseEvent.CANCEL, LeaseEvent.PAID_EXPIRY])
def test_b11_no_post_admission_is_settled_without_unknown_custody(event: LeaseEvent) -> None:
    journal = Journal(record(LeaseState.CAPACITY_WAIT))
    assert_decision(journal.step(event), LeaseState.RELEASED, BillingAction.NO_BILLABLE_RESOURCE)
    assert journal.current.data["safe_to_retry"] is True
    pending = Journal(record(LeaseState.CREATE_PENDING))
    assert_decision(pending.step(LeaseEvent.CREATE_NOT_SENT, attempt_id="create-1", no_allocation_proof=True),
                    LeaseState.RELEASED, BillingAction.NO_BILLABLE_RESOURCE)


def test_b12_release_cancel_and_retry_never_wait_for_abandoned_observation() -> None:
    observation_started = threading.Event()
    observation_done = threading.Event()
    journal = Journal(record(LeaseState.ACQUIRING))
    intent = journal.step(LeaseEvent.ACQUISITION_CONTINUE, attempt_id="blocked-observation")
    ticket = dispatch_ticket(intent.record)
    provider_entries = []
    late_results = []

    def observe() -> None:
        admission = journal.step(LeaseEvent.PROVIDER_ENTRY, ticket=ticket)
        assert admission.action == BillingAction.OBSERVE_ONCE
        provider_entries.append(ticket)
        observation_started.set()
        observation_done.wait(timeout=3)  # Uncancellable fake provider operation.
        try:
            journal.step(LeaseEvent.CLAIM_CONFIRMED, ticket=ticket)
        except UndefinedTransition:
            late_results.append("discarded")
        else:
            late_results.append("incorrectly adopted")

    observer = threading.Thread(target=observe)
    observer.start()
    try:
        assert observation_started.wait(timeout=1)
        decision = journal.step(LeaseEvent.CANCEL)
        assert_decision(decision, LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD)
        failed = journal.step(LeaseEvent.RELEASE_FAILED)
        assert_decision(failed, LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD)
        retry = journal.step(LeaseEvent.RELEASE_RETRY)
        assert_decision(retry, LeaseState.CLEANUP, BillingAction.RECONCILE_THEN_RELEASE)
        finish_cleanup(journal, retry)
        assert observer.is_alive()
        assert journal.current.data["observations_abandoned"] is True
        with pytest.raises(UndefinedTransition):
            journal.step(LeaseEvent.ACQUISITION_CONTINUE, attempt_id="abandoned-next-call")
    finally:
        observation_done.set()
        observer.join(timeout=1)
    assert not observer.is_alive()
    assert late_results == ["discarded"]
    assert provider_entries == [ticket]


def test_b13_late_create_is_durable_before_close_and_recovery_needs_no_inventory(tmp_path: Path) -> None:
    path = tmp_path / "lease.json"
    owner = Journal(record(LeaseState.CREATE_PENDING), path)
    owner.step(LeaseEvent.CANCEL)
    late = owner.step(LeaseEvent.LATE_CREATE_SUCCEEDED, attempt_id="create-1", pod_id="X")
    assert_decision(late, LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD)
    assert path.exists()
    # Simulate losing all process memory. Recovery only reads the durable id.
    saved = json.loads(path.read_text())
    recovered = Journal(Record(LeaseState[saved["state"]], saved["data"], saved["revision"]))
    decision = recovered.step(LeaseEvent.RESTART)
    assert_decision(decision, LeaseState.CLEANUP, BillingAction.RECONCILE_THEN_RELEASE)
    finish_cleanup(recovered, decision)
    assert {value.data["pod_id"] for value, _ in recovered.entries} == {"X"}
    assert recovered.current.state == LeaseState.RELEASED


def test_failed_late_result_write_retains_the_exact_id_in_retry_payload() -> None:
    journal = Journal(record(LeaseState.CREATE_UNKNOWN))

    def unavailable(value: Record[LeaseState], effects: tuple[str, ...]) -> None:
        raise OSError("journal temporarily unavailable")

    with pytest.raises(OSError):
        transition(LEASE_TRANSITIONS, journal.current, LeaseEvent.LATE_CREATE_SUCCEEDED,
                   changes={"pod_id": "X", "attempt_id": "create-1"}, persist=unavailable, retry=journal.retry)
    before, event, changes = journal.retries[0]
    assert before == journal.current
    assert event == LeaseEvent.LATE_CREATE_SUCCEEDED
    assert changes == {"pod_id": "X", "attempt_id": "create-1"}
    assert_decision(journal.step(event, **changes), LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD)


def test_undefined_pair_cannot_persist_or_grant_an_action() -> None:
    journal = Journal(record(LeaseState.RELEASED))
    with pytest.raises(UndefinedTransition):
        journal.step(LeaseEvent.JOB_ADMITTED)
    assert journal.entries == journal.retries == []


@pytest.mark.parametrize("event", [LeaseEvent.JOB_ADMITTED, LeaseEvent.KEEP])
def test_expired_idle_admission_or_keep_gets_persisted_cleanup(event: LeaseEvent) -> None:
    journal = Journal(record(LeaseState.IDLE_READY, now=200.0))
    decision = journal.step(event, paid_authority_proof=True, paid_deadline=400.0,
                            idle_duration=50.0)
    assert_decision(decision, LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD)
    assert journal.entries[-1][0] == decision.record


def test_idle_deadline_is_clipped_and_paid_authority_never_changes_implicitly() -> None:
    journal = Journal(record(LeaseState.ACTIVE_READY, now=290.0))
    settled = journal.step(LeaseEvent.JOB_SUCCEEDED)
    assert settled.record.data["keep_deadline"] == 300.0
    with pytest.raises(InvalidLeaseTransition, match="transition-owned"):
        journal.step(LeaseEvent.CHECKPOINT, paid_expires_at=400.0)
    with pytest.raises(InvalidLeaseTransition, match="explicit paid authority"):
        journal.step(LeaseEvent.KEEP)
    kept = journal.step(LeaseEvent.KEEP, paid_authority_proof=True, paid_deadline=400.0,
                        idle_duration=50.0)
    assert kept.record.data["keep_deadline"] == 340.0


def test_failure_retention_requires_stopped_jobs_and_verified_custody() -> None:
    journal = Journal(record(LeaseState.ACTIVE_READY))
    with pytest.raises(InvalidLeaseTransition, match="stopped jobs"):
        journal.step(LeaseEvent.JOB_FAILED)
    decision = journal.step(LeaseEvent.JOB_FAILED, jobs_stopped=True, custody_verified=True)
    assert_decision(decision, LeaseState.IDLE_READY, BillingAction.KEEP_WITH_DEADLINE)
    assert decision.record.data["keep_deadline"] == 150.0
    stale = Journal(record(LeaseState.PREPARING))
    assert_decision(stale.step(LeaseEvent.PREPARATION_FAILED_SAFE, jobs_stopped=True,
                               custody_verified=True), LeaseState.ACTIVE_STALE,
                    BillingAction.KEEP_WITH_DEADLINE)
    assert_decision(stale.step(LeaseEvent.JOB_FAILED, jobs_stopped=True, custody_verified=True),
                    LeaseState.IDLE_STALE, BillingAction.KEEP_WITH_DEADLINE)


def test_inflight_create_cannot_be_settled_by_empty_inventory_or_recreated() -> None:
    journal = Journal(record(LeaseState.CREATE_PENDING))
    assert_decision(journal.step(LeaseEvent.CANCEL), LeaseState.CREATE_UNKNOWN,
                    BillingAction.RECONCILE_THEN_RELEASE)
    with pytest.raises(InvalidLeaseTransition, match="in-flight create"):
        journal.step(LeaseEvent.RECONCILIATION_EMPTY, absence_proof=True)
    with pytest.raises(UndefinedTransition):
        journal.step(LeaseEvent.CREATE_STARTED)
    assert journal.current.state == LeaseState.CREATE_UNKNOWN


def test_create_authority_is_one_post_and_journalled_before_return() -> None:
    journal = Journal(record(LeaseState.CAPACITY_WAIT))
    intent = journal.step(LeaseEvent.CREATE_STARTED, attempt_id="create-2")
    assert_decision(intent, LeaseState.CREATE_PENDING, BillingAction.AWAIT_PROVIDER_ENTRY)
    assert journal.entries[-1][1] == ("write_ahead_create_attempt",)
    assert intent.record.data["create_inflight"] is False
    admitted = journal.step(LeaseEvent.PROVIDER_ENTRY, ticket=dispatch_ticket(intent.record))
    assert_decision(admitted, LeaseState.CREATE_PENDING, BillingAction.CREATE_ONCE)
    assert admitted.record.data["create_inflight"] is True
    with pytest.raises(InvalidLeaseTransition, match="already consumed"):
        journal.step(LeaseEvent.PROVIDER_ENTRY, ticket=dispatch_ticket(intent.record))
    with pytest.raises(UndefinedTransition):
        journal.step(LeaseEvent.CREATE_STARTED, attempt_id="another")
    assert_decision(journal.step(LeaseEvent.CREATE_SUCCEEDED, pod_id="new-pod", attempt_id="create-2"),
                    LeaseState.ACQUIRING, BillingAction.KEEP_WITH_DEADLINE)
    observing = journal.step(LeaseEvent.ACQUISITION_CONTINUE, attempt_id="observe-2")
    assert_decision(observing, LeaseState.ACQUIRING, BillingAction.AWAIT_PROVIDER_ENTRY)
    ticket = dispatch_ticket(observing.record)
    assert_decision(journal.step(LeaseEvent.PROVIDER_ENTRY, ticket=ticket),
                    LeaseState.ACQUIRING, BillingAction.OBSERVE_ONCE)
    assert_decision(journal.step(LeaseEvent.CLAIM_CONFIRMED, ticket=ticket), LeaseState.IDLE,
                    BillingAction.KEEP_WITH_DEADLINE)


def test_create_returning_after_expiry_journals_exact_id_with_cleanup() -> None:
    journal = Journal(record(LeaseState.CREATE_PENDING))
    expired = journal.step(LeaseEvent.CREATE_SUCCEEDED, attempt_id="create-1", pod_id="X", now=301.0)
    assert_decision(expired, LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD)
    assert journal.entries[-1][0].data["pod_id"] == "X"
    finish_cleanup(journal, expired)


def test_release_never_claims_success_without_exact_completion_proofs() -> None:
    journal = Journal(record(LeaseState.CLEANUP))
    with pytest.raises(InvalidLeaseTransition, match="drain"):
        journal.step(LeaseEvent.QUIESCED)
    journal.step(LeaseEvent.QUIESCED, drain_confirmed=True)
    with pytest.raises(InvalidLeaseTransition, match="absence"):
        journal.step(LeaseEvent.RELEASE_SUCCEEDED)
    assert journal.current.state == LeaseState.RELEASING
    assert_decision(journal.step(LeaseEvent.RELEASE_FAILED), LeaseState.CLEANUP,
                    BillingAction.RELEASE_EXACT_POD)


def test_old_or_foreign_result_cannot_replace_current_exact_identity() -> None:
    journal = Journal(record(LeaseState.CREATE_UNKNOWN, pod_id="owned-pod"))
    for changes in ({"pod_id": "foreign-pod"}, {"operation_id": "foreign-operation", "pod_id": "owned-pod"}):
        with pytest.raises(InvalidLeaseTransition, match="changed under an existing owner"):
            journal.step(LeaseEvent.LATE_CREATE_SUCCEEDED, attempt_id="create-1", **changes)
    assert journal.entries == []


def test_named_combinations_are_unique_and_exclude_generate_allocation() -> None:
    assert len({state.value for state in LeaseState}) == len(LeaseState)
    assert LEASE_TRANSITIONS[LeaseState.ACQUIRING, LeaseEvent.CLAIM_CONFIRMED].next_state == LeaseState.IDLE
    for state in LeaseState:
        if (state, LeaseEvent.JOB_ADMITTED) in LEASE_TRANSITIONS:
            assert state in IDLE_STATES | {LeaseState.WARM_IDLE}
    assert all(rule.action != BillingAction.CREATE_ONCE
               for (state, event), rule in LEASE_TRANSITIONS.items()
               if event == LeaseEvent.JOB_ADMITTED)


@pytest.mark.parametrize("field", sorted(CUSTODY_CONTROL_FIELDS))
@pytest.mark.parametrize("event", [LeaseEvent.RECONCILIATION_EMPTY, LeaseEvent.CANCEL])
def test_caller_cannot_manufacture_or_erase_custody_control(field: str, event: LeaseEvent) -> None:
    journal = Journal(record(LeaseState.CREATE_PENDING))
    before = journal.current
    with pytest.raises(InvalidLeaseTransition, match="transition-owned"):
        journal.step(event, **{field: False, "absence_proof": True})
    assert journal.current == before
    assert journal.entries == journal.retries == []


def test_reconciliation_cannot_clear_admitted_create_even_after_finding_exact_pod() -> None:
    journal = Journal(record(LeaseState.CREATE_PENDING))
    found = journal.step(LeaseEvent.RECONCILIATION_FOUND, pod_id="pod-1")
    assert found.record.data["create_inflight"] is True
    journal.step(LeaseEvent.QUIESCED, drain_confirmed=True)
    with pytest.raises(InvalidLeaseTransition, match="completed create"):
        journal.step(LeaseEvent.RELEASE_SUCCEEDED, absence_proof=True)
    late = journal.step(LeaseEvent.LATE_CREATE_SUCCEEDED, pod_id="pod-1", attempt_id="create-1")
    assert late.record.data["create_inflight"] is False
    finish_cleanup(journal, late)


@pytest.mark.parametrize("kind", ["create", "observe"])
@pytest.mark.parametrize("stop", [LeaseEvent.CANCEL, LeaseEvent.PAID_EXPIRY])
def test_dispatch_is_revoked_between_persistence_and_provider_entry(kind: str, stop: LeaseEvent) -> None:
    journal = Journal(record(LeaseState.CAPACITY_WAIT if kind == "create" else LeaseState.ACQUIRING))
    # Persistence happened, then an executor/provider-gate wait gave cancellation
    # the transition lock. Only provider entry after that wait could allow I/O.
    intent = journal.step(LeaseEvent.CREATE_STARTED if kind == "create" else LeaseEvent.ACQUISITION_CONTINUE,
                          attempt_id="delayed-entry")
    ticket = dispatch_ticket(intent.record)
    journal.step(stop, now=300.0 if stop == LeaseEvent.PAID_EXPIRY else 100.0)
    denied = journal.step(LeaseEvent.PROVIDER_ENTRY, ticket=ticket)
    assert denied.action == (BillingAction.RECONCILE_THEN_RELEASE if kind == "create"
                             else BillingAction.RELEASE_EXACT_POD)
    assert denied.record.data["dispatch_admitted"] is False
    assert denied.record.data["create_inflight"] is False
    assert not any("write_ahead_provider_entry" in effects for _, effects in journal.entries)


def test_dispatch_rechecks_absolute_deadline_without_timer_callback() -> None:
    journal = Journal(record(LeaseState.CAPACITY_WAIT))
    intent = journal.step(LeaseEvent.CREATE_STARTED, attempt_id="delayed-entry")
    denied = journal.step(LeaseEvent.PROVIDER_ENTRY, ticket=dispatch_ticket(intent.record), now=300.0)
    assert denied.action == BillingAction.RECONCILE_THEN_RELEASE
    assert denied.record.data["create_inflight"] is False


def test_admitted_create_keeps_exact_attempt_until_completion_after_cancel() -> None:
    journal = Journal(record(LeaseState.CAPACITY_WAIT))
    intent = journal.step(LeaseEvent.CREATE_STARTED, attempt_id="create-2")
    journal.step(LeaseEvent.PROVIDER_ENTRY, ticket=dispatch_ticket(intent.record))
    journal.step(LeaseEvent.CANCEL)
    with pytest.raises(InvalidLeaseTransition, match="in-flight create"):
        journal.step(LeaseEvent.RECONCILIATION_EMPTY, absence_proof=True)
    with pytest.raises(InvalidLeaseTransition, match="foreign attempt"):
        journal.step(LeaseEvent.LATE_CREATE_SUCCEEDED, attempt_id="foreign", pod_id="X")
    late = journal.step(LeaseEvent.CREATE_SUCCEEDED, attempt_id="create-2", pod_id="X")
    assert_decision(late, LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD)
    assert late.record.data["create_inflight"] is False
    finish_cleanup(journal, late)


def test_foreign_dispatch_ticket_and_reused_observation_cannot_start_provider_work() -> None:
    from dataclasses import replace

    journal = Journal(record(LeaseState.ACQUIRING))
    intent = journal.step(LeaseEvent.ACQUISITION_CONTINUE, attempt_id="observe-2")
    ticket = dispatch_ticket(intent.record)
    for changes in ({"attempt_id": "foreign"}, {"owner_epoch": "foreign"},
                    {"cancellation_fence": 999}, {"deadline": 301.0}, {"operation_id": "foreign"}):
        with pytest.raises(InvalidLeaseTransition, match="foreign attempt"):
            journal.step(LeaseEvent.PROVIDER_ENTRY, ticket=replace(ticket, **changes))
    journal.step(LeaseEvent.PROVIDER_ENTRY, ticket=ticket)
    journal.step(LeaseEvent.ACQUISITION_CONTINUE, attempt_id="observe-3")
    with pytest.raises(InvalidLeaseTransition, match="admitted exact ticket"):
        journal.step(LeaseEvent.CLAIM_CONFIRMED, ticket=ticket)


@pytest.mark.parametrize("state", list(IDLE_STATES))
def test_verified_idle_restart_preserves_both_original_deadlines_and_replaces_epoch(state: LeaseState) -> None:
    journal = Journal(record(state))
    before = journal.current
    recovered = journal.step(LeaseEvent.RESTART, now=180.0, compatible=True,
                             custody_verified=True, new_owner_epoch="epoch-2")
    assert_decision(recovered, state, BillingAction.KEEP_WITH_DEADLINE)
    for key in ("paid_expires_at", "keep_deadline", "idle_seconds", "operation_id", "pod_id"):
        assert recovered.record.data[key] == before.data[key]
    assert recovered.record.data["owner_epoch"] == "epoch-2"
    assert recovered.effects == ("adopt_verified_idle_owner", "rearm_original_deadlines")
    assert recovered.record.data["dispatch_attempt"] is None


@pytest.mark.parametrize("state", list(IDLE_STATES))
@pytest.mark.parametrize("facts", [
    {"now": 200.0}, {"now": 300.0}, {"compatible": False},
    {"custody_verified": False}, {"create_inflight": True}, {"stop_requested": True},
    {"keep_deadline": 400.0},
])
def test_idle_restart_cleans_expired_incompatible_or_uncertain_custody(state: LeaseState, facts: dict) -> None:
    # Custody facts are read from the journal, not caller changes.
    data = {key: value for key, value in facts.items() if key in CUSTODY_CONTROL_FIELDS}
    journal = Journal(record(state, **data))
    evidence = {"compatible": True, "custody_verified": True,
                "new_owner_epoch": "epoch-2", **{key: value for key, value in facts.items() if key not in data}}
    recovered = journal.step(LeaseEvent.RESTART, **evidence)
    assert_decision(recovered, LeaseState.CLEANUP, BillingAction.RECONCILE_THEN_RELEASE)
    assert recovered.record.data["owner_epoch"] == "epoch-1"


def test_preparation_invalidation_keeps_active_owner_until_final_failure_settlement() -> None:
    journal = Journal(record(LeaseState.ACTIVE_READY, invocation_id="invocation-1"))
    stale = journal.step(LeaseEvent.PREPARATION_INVALIDATED)
    assert_decision(stale, LeaseState.ACTIVE_STALE, BillingAction.KEEP_WITH_DEADLINE)
    assert stale.record.data["invocation_id"] == "invocation-1"
    with pytest.raises(UndefinedTransition):
        journal.step(LeaseEvent.JOB_ADMITTED)
    settled = journal.step(LeaseEvent.JOB_FAILED, jobs_stopped=True, custody_verified=True)
    assert_decision(settled, LeaseState.IDLE_STALE, BillingAction.KEEP_WITH_DEADLINE)
    assert "checkpoint_stopped_job" in settled.effects


# Enumerate every retention/dispatch-intent row on BOTH sides of the deadline.
# Valid evidence must yield persisted cleanup after expiry, not a guard raise.
@pytest.mark.parametrize("state,event", [
    pair for pair, rule in LEASE_TRANSITIONS.items()
    if rule.action in {BillingAction.KEEP_WITH_DEADLINE, BillingAction.AWAIT_PROVIDER_ENTRY}
    and pair[1] != LeaseEvent.HOST_DEATH
])
@pytest.mark.parametrize("now", [100.0, 350.0])
def test_all_retention_rows_persist_cleanup_when_expired(state: LeaseState, event: LeaseEvent, now: float) -> None:
    journal = Journal(record(state, now=now))
    evidence: dict[str, object] = {"jobs_stopped": True, "custody_verified": True,
                                  "paid_authority_proof": True, "paid_deadline": 400.0,
                                  "idle_duration": 50.0, "attempt_id": "new-attempt"}
    if event == LeaseEvent.PROVIDER_ENTRY:
        # Fixture models an unconsumed durable intent, including revoked states.
        journal = Journal(record(state, now=now, dispatch_admitted=False))
        evidence["ticket"] = dispatch_ticket(journal.current)
    elif event in {LeaseEvent.CLAIM_CONFIRMED, LeaseEvent.ATTACHMENT_BOUND}:
        evidence.update(ticket=dispatch_ticket(journal.current), pod_id="pod-1")
        if event == LeaseEvent.CLAIM_CONFIRMED:
            evidence.pop("pod_id")
    elif event == LeaseEvent.CREATE_SUCCEEDED:
        evidence.update(attempt_id="create-1", pod_id="pod-1")
    elif event == LeaseEvent.WARM_SESSION_BOUND:
        evidence.update(pod_id="pod-1", exclusive_release_proof=True)
    decision = journal.step(event, **evidence)
    assert journal.entries[-1][0] == decision.record
    if now >= 300.0:
        assert decision.record.state in CLEANUP_STATES | UNKNOWN_CUSTODY | RELEASED_STATES
        assert decision.action not in {BillingAction.KEEP_WITH_DEADLINE, BillingAction.CREATE_ONCE,
                                       BillingAction.OBSERVE_ONCE, BillingAction.AWAIT_PROVIDER_ENTRY}


@pytest.mark.parametrize("authorized", [False, True])
def test_exact_attachment_requested_id_does_not_imply_release_authority(authorized: bool) -> None:
    journal = Journal(record(LeaseState.RELEASED, pod_id=None))
    requested = journal.step(LeaseEvent.ATTACHMENT_REQUESTED, operation_id="attach-1",
                             requested_pod_id="exact-attachment", paid_deadline=300.0,
                             idle_duration=50.0, new_owner_epoch="attach-epoch",
                             exclusive_release_proof=authorized)
    assert requested.record.state == LeaseState.ATTACHMENT_PENDING
    assert requested.record.data["pod_id"] is None
    assert requested.record.data["release_authorized"] is authorized
    stopped = journal.step(LeaseEvent.CANCEL)
    assert stopped.record.data["requested_pod_id"] == "exact-attachment"
    assert stopped.record.data["release_authorized"] is authorized
    if authorized:
        assert stopped.action == BillingAction.RELEASE_EXACT_POD
        finish_cleanup(journal, stopped)
    else:
        assert stopped.action == BillingAction.RECONCILE_THEN_RELEASE
        with pytest.raises(InvalidLeaseTransition, match="exclusive release authority"):
            journal.step(LeaseEvent.QUIESCED, drain_confirmed=True)
        assert journal.current.state == LeaseState.CLEANUP
        assert journal.current.data.get("absence_confirmed") is False


def test_exact_attachment_binding_requires_exact_requested_id_and_dispatch() -> None:
    journal = Journal(record(LeaseState.ATTACHMENT_PENDING))
    observing = journal.step(LeaseEvent.ACQUISITION_CONTINUE, attempt_id="attach-observation")
    ticket = dispatch_ticket(observing.record)
    journal.step(LeaseEvent.PROVIDER_ENTRY, ticket=ticket)
    with pytest.raises(InvalidLeaseTransition, match="exact requested id"):
        journal.step(LeaseEvent.ATTACHMENT_BOUND, pod_id="other", ticket=ticket)
    bound = journal.step(LeaseEvent.ATTACHMENT_BOUND, pod_id="pod-1", ticket=ticket)
    assert_decision(bound, LeaseState.ATTACHED_UNVERIFIED, BillingAction.KEEP_WITH_DEADLINE)
    claimed = journal.step(LeaseEvent.CLAIM_CONFIRMED, ticket=ticket)
    assert_decision(claimed, LeaseState.IDLE, BillingAction.KEEP_WITH_DEADLINE)


@pytest.mark.parametrize("state", [LeaseState.WARM_IDLE, LeaseState.WARM_ACTIVE])
def test_warm_session_expiry_has_canonical_exact_cleanup_route(state: LeaseState) -> None:
    journal = Journal(record(state))
    cleanup = journal.step(LeaseEvent.WARM_SESSION_EXPIRED)
    assert_decision(cleanup, LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD)
    assert cleanup.record.data["archived_warm_session"] == state.value[3]
    finish_cleanup(journal, cleanup)


def test_cleanup_collapses_preparation_variants_and_archives_evidence() -> None:
    for state in (LeaseState.PREPARING, LeaseState.ACTIVE_READY, LeaseState.ACTIVE_STALE):
        journal = Journal(record(state))
        cleanup = journal.step(LeaseEvent.CANCEL)
        assert cleanup.record.state == LeaseState.CLEANUP
        assert cleanup.record.data.get("archived_preparation") == state.value[2]
        finish_cleanup(journal, cleanup)
    assert EXCLUDED_LEASE_ACTIONS == {"runpod.provision", "runpod.session"}
    assert not {"NO_POST", "CAPACITY_EXHAUSTED", "CLEANUP_REQUIRED"} & set(LeaseState.__members__)


def test_attachment_reconciliation_preserves_exact_request_and_release_authority() -> None:
    journal = Journal(record(LeaseState.ATTACHMENT_PENDING, release_authorized=False))
    with pytest.raises(InvalidLeaseTransition, match="exact requested id"):
        journal.step(LeaseEvent.RECONCILIATION_FOUND, pod_id="other")
    reconciled = journal.step(LeaseEvent.RECONCILIATION_FOUND, pod_id="pod-1")
    assert_decision(reconciled, LeaseState.CLEANUP, BillingAction.RECONCILE_THEN_RELEASE)
    assert reconciled.record.data["release_authorized"] is False
    with pytest.raises(InvalidLeaseTransition, match="exclusive release authority"):
        journal.step(LeaseEvent.QUIESCED, drain_confirmed=True)


def test_attachment_acquisition_failure_cleans_exact_requested_id_without_allocation() -> None:
    journal = Journal(record(LeaseState.ATTACHMENT_PENDING))
    failed = journal.step(LeaseEvent.ACQUISITION_FAILED)
    assert_decision(failed, LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD)
    assert failed.record.data["requested_pod_id"] == "pod-1"
    assert failed.record.data["pod_id"] is None
    finish_cleanup(journal, failed)


def test_zero_custody_contradictory_inventory_enters_reconciliation_and_cannot_create() -> None:
    journal = Journal(record(LeaseState.CAPACITY_WAIT))
    uncertain = journal.step(LeaseEvent.RECONCILIATION_UNKNOWN)
    assert_decision(uncertain, LeaseState.CREATE_UNKNOWN, BillingAction.RECONCILE_THEN_RELEASE)
    assert uncertain.record.data["safe_to_retry"] is False
    with pytest.raises(UndefinedTransition):
        journal.step(LeaseEvent.CREATE_STARTED, attempt_id="unsafe-retry")


@pytest.mark.parametrize("state", [LeaseState.ACTIVE_READY, LeaseState.ACTIVE_STALE])
@pytest.mark.parametrize("restart_at", [231.0, 350.0])
def test_b6_settlement_is_durable_before_timer_arming_and_overdue_restart_cleans(
    state: LeaseState, restart_at: float,
) -> None:
    journal = Journal(record(state))
    settled = journal.step(LeaseEvent.JOB_FAILED, now=180.0, jobs_stopped=True, custody_verified=True)
    assert settled.record.data["keep_deadline"] == 230.0
    assert settled.record.data["paid_expires_at"] == 300.0
    # No timer has been constructed; the saved settlement is sufficient.
    before_death = journal.current
    journal.step(LeaseEvent.HOST_DEATH)
    assert journal.current.data == before_death.data
    restarted = Journal(journal.current)
    cleanup = restarted.step(LeaseEvent.RESTART, now=restart_at, compatible=True,
                             custody_verified=True, new_owner_epoch="epoch-2")
    assert_decision(cleanup, LeaseState.CLEANUP, BillingAction.RECONCILE_THEN_RELEASE)
    assert cleanup.record.data["keep_deadline"] == 230.0
    assert cleanup.record.data["paid_expires_at"] == 300.0
    finish_cleanup(restarted, cleanup)


def test_capacity_rejection_evidence_survives_merged_zero_custody_state_and_next_attempt() -> None:
    journal = Journal(record(LeaseState.CREATE_PENDING))
    rejected = journal.step(LeaseEvent.CAPACITY_REJECTED, attempt_id="create-1", no_allocation_proof=True)
    assert_decision(rejected, LeaseState.CAPACITY_WAIT, BillingAction.NO_BILLABLE_RESOURCE)
    assert rejected.record.data["capacity_rejection"] == "definite_no_allocation"
    assert rejected.record.data["capacity_rejected_attempt"] == "create-1"
    assert rejected.record.data["absence_confirmed"] is True
    next_attempt = journal.step(LeaseEvent.CREATE_STARTED, attempt_id="create-2")
    assert next_attempt.record.data["absence_confirmed"] is False
    assert next_attempt.record.data["capacity_rejected_attempt"] == "create-1"
    assert next_attempt.record.data["create_attempt"] == "create-2"


@pytest.mark.parametrize("stopped", [False, True])
def test_expired_or_stopped_definite_rejection_settles_zero_custody(stopped: bool) -> None:
    journal = Journal(record(LeaseState.CREATE_PENDING, now=300.0, stop_requested=stopped))
    rejected = journal.step(LeaseEvent.CAPACITY_REJECTED, attempt_id="create-1", no_allocation_proof=True)
    assert_decision(rejected, LeaseState.RELEASED, BillingAction.NO_BILLABLE_RESOURCE)
    assert rejected.record.data["safe_to_retry"] is True
    assert rejected.record.data["create_inflight"] is False


@pytest.mark.parametrize("state", [LeaseState.CLEANUP, LeaseState.RELEASING])
@pytest.mark.parametrize("event", [LeaseEvent.JOB_SUCCEEDED, LeaseEvent.JOB_FAILED,
                                 LeaseEvent.PREPARATION_SUCCEEDED, LeaseEvent.PREPARATION_FAILED_SAFE,
                                 LeaseEvent.PREPARATION_INVALIDATED])
def test_exact_holder_can_finish_during_cleanup_without_reopening_admission(
    state: LeaseState, event: LeaseEvent,
) -> None:
    journal = Journal(record(state, now=350.0, invocation_id="exact-holder"))
    completed = journal.step(event, last_job={"invocation_id": "exact-holder", "settled": True})
    assert_decision(completed, LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD)
    assert "record_late_completion_without_admission" in completed.effects
    assert completed.record.data["last_job"] == {"invocation_id": "exact-holder", "settled": True}
    with pytest.raises(UndefinedTransition):
        journal.step(LeaseEvent.JOB_ADMITTED)
    finish_cleanup(journal, completed)
