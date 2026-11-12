"""Design-level decisions; owner, provider and writer-guard wiring comes later."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from astrid.packs.runpod.lease_states import (
    CLEANUP_STATES,
    IDLE_STATES,
    LEASE_TRANSITIONS,
    RELEASED_STATES,
    RELEASING_STATES,
    UNKNOWN_CUSTODY,
    ZERO_CUSTODY,
    BillingAction,
    Data,
    Decision,
    InvalidLeaseTransition,
    LeaseEvent,
    LeaseState,
    Record,
    Rule,
    UndefinedTransition,
    transition,
)


def record(state: LeaseState, **changes: object) -> Record[LeaseState]:
    data: dict[str, object] = {
        "operation_id": "operation-1", "pod_id": "pod-1", "now": 100.0,
        "paid_expires_at": 300.0, "keep_deadline": 200.0, "idle_seconds": 50.0,
        "create_inflight": False,
    }
    if state in ZERO_CUSTODY | UNKNOWN_CUSTODY:
        data["pod_id"] = None
    if state in UNKNOWN_CUSTODY:
        data["create_inflight"] = True
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
            decision = journal.step(LeaseEvent.RECONCILIATION_FOUND, pod_id="pod-1")
        elif decision.record.state in CLEANUP_STATES:
            decision = journal.step(LeaseEvent.QUIESCED, drain_confirmed=True)
        elif decision.record.state in RELEASING_STATES:
            decision = journal.step(LeaseEvent.RELEASE_SUCCEEDED, absence_confirmed=True)
        else:
            pytest.fail(f"no internal completion chain for {decision}")
    pytest.fail("cleanup did not settle in its bounded internal completion chain")


@pytest.mark.parametrize("state", list(LeaseState))
@pytest.mark.parametrize("event", [
    LeaseEvent.RESTART, LeaseEvent.HOST_DEATH, LeaseEvent.PAID_EXPIRY,
    LeaseEvent.IDLE_EXPIRY, LeaseEvent.JOB_FAILED, LeaseEvent.RECIPE_CHANGED,
    LeaseEvent.MODEL_CHANGED, LeaseEvent.SOURCE_CHANGED, LeaseEvent.CANCEL,
])
def test_every_state_has_a_bounded_safety_outcome(state: LeaseState, event: LeaseEvent) -> None:
    journal = Journal(record(state))
    decision = journal.step(event, jobs_stopped=True, custody_verified=True)
    finish_cleanup(journal, decision)


@pytest.mark.parametrize("state", [LeaseState.CLEANUP_READY, LeaseState.RELEASING_READY])
def test_b1_restart_pending_reconciles_then_releases_exact_pod(state: LeaseState) -> None:
    journal = Journal(record(state))
    decision = journal.step(LeaseEvent.RESTART)
    assert_decision(decision, LeaseState.CLEANUP_READY, BillingAction.RECONCILE_THEN_RELEASE)
    finish_cleanup(journal, decision)
    assert {row.data["pod_id"] for row, _ in journal.entries} == {"pod-1"}
    assert not any("write_ahead_create_attempt" in effects for _, effects in journal.entries)


@pytest.mark.parametrize("event", [
    LeaseEvent.RECIPE_CHANGED, LeaseEvent.MODEL_CHANGED, LeaseEvent.SOURCE_CHANGED,
])
def test_b2_changed_idle_claim_is_released_on_restart(event: LeaseEvent) -> None:
    journal = Journal(record(LeaseState.IDLE_READY))
    assert_decision(journal.step(event), LeaseState.CLEANUP_READY, BillingAction.RELEASE_EXACT_POD)
    restarted = Journal(journal.current)
    decision = restarted.step(LeaseEvent.RESTART)
    assert_decision(decision, LeaseState.CLEANUP_READY, BillingAction.RECONCILE_THEN_RELEASE)
    finish_cleanup(restarted, decision)


def test_b3_preparation_finishing_after_paid_expiry_cannot_execute() -> None:
    journal = Journal(record(LeaseState.PREPARING, now=300.0))
    with pytest.raises(InvalidLeaseTransition, match="expired"):
        journal.step(LeaseEvent.PREPARATION_SUCCEEDED)
    decision = journal.step(LeaseEvent.PAID_EXPIRY)
    assert_decision(decision, LeaseState.CLEANUP_UNRESOLVED, BillingAction.RELEASE_EXACT_POD)
    with pytest.raises(UndefinedTransition):
        journal.step(LeaseEvent.PREPARATION_SUCCEEDED)
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
    assert_decision(idle.step(LeaseEvent.IDLE_EXPIRY), LeaseState.CLEANUP_READY,
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
    journal = Journal(record(LeaseState.CLEANUP_READY))

    def unavailable(value: Record[LeaseState], effects: tuple[str, ...]) -> None:
        assert "release_annotation_in_retry_boundary" in effects
        raise OSError("journal temporarily unavailable")

    with pytest.raises(OSError):
        transition(LEASE_TRANSITIONS, journal.current, LeaseEvent.RELEASE_RETRY,
                   persist=unavailable, retry=journal.retry)
    assert journal.retries == [(journal.current, LeaseEvent.RELEASE_RETRY, {})]
    decision = journal.step(LeaseEvent.RELEASE_RETRY)
    assert_decision(decision, LeaseState.CLEANUP_READY, BillingAction.RECONCILE_THEN_RELEASE)
    finish_cleanup(journal, decision)


def test_b8_cancel_requested_exact_owner_is_recovered_by_generic_table() -> None:
    # Publication owns task attempt/fence state. It must not be a lease enum.
    table = {("cancel_requested", "restart"): Rule(
        "reconcile_pending", "reconcile_exact_owner", ("write_ahead_exact_owner_recovery",))}
    saved = []
    decision = transition(table, Record("cancel_requested", {"attempt": "a", "fence": "f"}),
                          "restart", persist=lambda value, effects: saved.append((value, effects)),
                          retry=lambda value, event, changes: pytest.fail("unexpected write failure"))
    assert (decision.record.state, decision.action) == ("reconcile_pending", "reconcile_exact_owner")
    assert dict(decision.record.data) == {"attempt": "a", "fence": "f"}
    assert len(saved) == 1
    lease = Journal(record(LeaseState.CLEANUP_READY, cancel_requested=True))
    assert_decision(lease.step(LeaseEvent.RESTART), LeaseState.CLEANUP_READY,
                    BillingAction.RECONCILE_THEN_RELEASE)


def test_b9_generic_table_journals_every_cede_attempt_before_post() -> None:
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
        LeaseEvent.LATE_CREATE_SUCCEEDED, pod_id="late-pod")))
    callback.start()
    callback.join(timeout=1)
    assert not callback.is_alive(), "late-result journalling must not wait for owner close"
    assert_decision(result[0], LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD)
    assert journal.current.data["pod_id"] == "late-pod"


@pytest.mark.parametrize("event", [LeaseEvent.CANCEL, LeaseEvent.PAID_EXPIRY])
def test_b11_no_post_admission_is_settled_without_unknown_custody(event: LeaseEvent) -> None:
    journal = Journal(record(LeaseState.NO_POST))
    assert_decision(journal.step(event), LeaseState.RELEASED, BillingAction.NO_BILLABLE_RESOURCE)
    assert journal.current.data["safe_to_retry"] is True
    pending = Journal(record(LeaseState.CREATE_PENDING))
    assert_decision(pending.step(LeaseEvent.CREATE_NOT_SENT, no_allocation_proof=True),
                    LeaseState.RELEASED, BillingAction.NO_BILLABLE_RESOURCE)


def test_b12_release_cancel_and_retry_never_wait_for_abandoned_observation() -> None:
    observation_started = threading.Event()
    observation_done = threading.Event()

    def observe() -> None:
        observation_started.set()
        observation_done.wait(timeout=3)

    observer = threading.Thread(target=observe)
    observer.start()
    try:
        assert observation_started.wait(timeout=1)
        journal = Journal(record(LeaseState.ACQUIRING))
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
            journal.step(LeaseEvent.CLAIM_CONFIRMED)
    finally:
        observation_done.set()
        observer.join(timeout=1)


def test_b13_late_create_is_durable_before_close_and_recovery_needs_no_inventory(tmp_path: Path) -> None:
    path = tmp_path / "lease.json"
    owner = Journal(record(LeaseState.CREATE_PENDING), path)
    owner.step(LeaseEvent.CANCEL)
    late = owner.step(LeaseEvent.LATE_CREATE_SUCCEEDED, pod_id="X")
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
                   changes={"pod_id": "X"}, persist=unavailable, retry=journal.retry)
    before, event, changes = journal.retries[0]
    assert before == journal.current
    assert event == LeaseEvent.LATE_CREATE_SUCCEEDED
    assert changes == {"pod_id": "X"}
    assert_decision(journal.step(event, **changes), LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD)


def test_undefined_pair_cannot_persist_or_grant_an_action() -> None:
    journal = Journal(record(LeaseState.RELEASED))
    with pytest.raises(UndefinedTransition):
        journal.step(LeaseEvent.JOB_ADMITTED)
    assert journal.entries == journal.retries == []


@pytest.mark.parametrize("event", [LeaseEvent.JOB_ADMITTED, LeaseEvent.KEEP])
def test_expired_idle_claim_cannot_be_revived(event: LeaseEvent) -> None:
    journal = Journal(record(LeaseState.IDLE_READY, now=201.0))
    with pytest.raises(InvalidLeaseTransition, match="idle authority expired"):
        journal.step(event, fresh_paid_consent=True)


def test_idle_deadline_is_clipped_and_paid_authority_never_changes_implicitly() -> None:
    journal = Journal(record(LeaseState.ACTIVE_READY, now=290.0))
    settled = journal.step(LeaseEvent.JOB_SUCCEEDED)
    assert settled.record.data["keep_deadline"] == 300.0
    with pytest.raises(InvalidLeaseTransition, match="only an explicitly consented keep"):
        journal.step(LeaseEvent.CHECKPOINT, paid_expires_at=400.0)
    with pytest.raises(InvalidLeaseTransition, match="fresh paid consent"):
        journal.step(LeaseEvent.KEEP)
    kept = journal.step(LeaseEvent.KEEP, fresh_paid_consent=True, paid_expires_at=400.0)
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
        journal.step(LeaseEvent.RECONCILIATION_EMPTY, absence_confirmed=True)
    with pytest.raises(UndefinedTransition):
        journal.step(LeaseEvent.CREATE_STARTED)
    assert journal.current.state == LeaseState.CREATE_UNKNOWN


def test_create_authority_is_one_post_and_journalled_before_return() -> None:
    journal = Journal(record(LeaseState.NO_POST))
    decision = journal.step(LeaseEvent.CREATE_STARTED)
    assert_decision(decision, LeaseState.CREATE_PENDING, BillingAction.CREATE_ONCE)
    assert journal.entries[-1][1] == ("write_ahead_create_attempt",)
    with pytest.raises(UndefinedTransition):
        journal.step(LeaseEvent.CREATE_STARTED)
    assert_decision(journal.step(LeaseEvent.CREATE_SUCCEEDED, pod_id="new-pod"),
                    LeaseState.ACQUIRING, BillingAction.CHECK_PAID_DEADLINE)
    assert_decision(journal.step(LeaseEvent.ACQUISITION_CONTINUE),
                    LeaseState.ACQUIRING, BillingAction.KEEP_WITH_DEADLINE)
    assert_decision(journal.step(LeaseEvent.CLAIM_CONFIRMED), LeaseState.IDLE,
                    BillingAction.KEEP_WITH_DEADLINE)


def test_create_returning_after_expiry_journals_exact_id_before_cleanup() -> None:
    journal = Journal(record(LeaseState.CREATE_PENDING))
    decision = journal.step(LeaseEvent.CREATE_SUCCEEDED, pod_id="X", now=301.0)
    assert_decision(decision, LeaseState.ACQUIRING, BillingAction.CHECK_PAID_DEADLINE)
    assert journal.entries[-1][0].data["pod_id"] == "X"
    with pytest.raises(InvalidLeaseTransition, match="expired"):
        journal.step(LeaseEvent.ACQUISITION_CONTINUE)
    # This is the immediate internal completion of CHECK_PAID_DEADLINE.
    expired = journal.step(LeaseEvent.PAID_EXPIRY)
    assert_decision(expired, LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD)
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
            journal.step(LeaseEvent.LATE_CREATE_SUCCEEDED, **changes)
    assert journal.entries == []


def test_named_combinations_are_unique_and_exclude_generate_allocation() -> None:
    assert len({state.value for state in LeaseState}) == len(LeaseState)
    assert LEASE_TRANSITIONS[LeaseState.ACQUIRING, LeaseEvent.CLAIM_CONFIRMED].next_state == LeaseState.IDLE
    for state in LeaseState:
        if (state, LeaseEvent.JOB_ADMITTED) in LEASE_TRANSITIONS:
            assert state in IDLE_STATES
    assert all(rule.action != BillingAction.CREATE_ONCE
               for (state, event), rule in LEASE_TRANSITIONS.items()
               if event == LeaseEvent.JOB_ADMITTED)
