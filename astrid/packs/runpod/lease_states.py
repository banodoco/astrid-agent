"""Lease custody decisions, without provider calls or existing-owner wiring.

Each enum value is (record.state, lease.state, lease.preparation.state).
Absent preparation is None. Release preserves preparation evidence, so those
reachable variants are named rather than hidden in a wildcard state. Admission
is for a manual claim; a job can only attach to an already claimed lease.

The caller serializes read/transition/durable replacement with a short lock and
an operation/revision compare-and-swap. It releases that lock before doing the
returned action. Provider results return as fenced events. In particular, an
abandoned observation has no right to write or initiate another provider call.
An in-flight create is different: its late exact id MUST still be journalled.

Deadlines are absolute journal data. A timer is only a wakeup mechanism. A dead
host runs no transition: HOST_DEATH describes the already durable obligation;
RESTART performs it. Existing provider APIs cannot schedule future termination
independently of this controller.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Generic, TypeVar

S = TypeVar("S", bound=Hashable)
E = TypeVar("E", bound=Hashable)
A = TypeVar("A")
Data = Mapping[str, object]


class UndefinedTransition(ValueError):
    """An event has no authority in this state."""


class InvalidLeaseTransition(ValueError):
    """A defined lease decision lacks its deadline, identity or completion proof."""


@dataclass(frozen=True)
class Record(Generic[S]):
    state: S
    data: Data = field(default_factory=dict)
    revision: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))


@dataclass(frozen=True)
class Rule(Generic[S, A]):
    next_state: S
    action: A
    effects: tuple[str, ...] = ()
    prepare: Callable[[Data, Data], Data] = lambda old, changes: {**old, **changes}


@dataclass(frozen=True)
class Decision(Generic[S, A]):
    record: Record[S]
    action: A
    effects: tuple[str, ...]


def transition(
    table: Mapping[tuple[S, E], Rule[S, A]],
    record: Record[S],
    event: E,
    *,
    persist: Callable[[Record[S], tuple[str, ...]], None],
    retry: Callable[[Record[S], E, Data], None],
    changes: Data | None = None,
) -> Decision[S, A]:
    """Persist the decision before returning authority to perform its action.

    ``persist`` must atomically and durably save record AND effects, checking
    the prior operation/revision under the short transition lock. Effects are
    write-ahead journal entries, never callbacks. Every invocation persists,
    including a retry of a same-state POST. The caller owns payload/receipt
    validation and retry scheduling; ``retry`` receives the original record,
    event AND changes, so an unpersisted late pod id is not lost. It is invoked
    inside this boundary on a storage failure, including release annotations.

    This function has no lease-specific signature or publication policy. A
    publication table can use its own states, actions and write-ahead effects.
    Neither a failed write nor an undefined pair returns action authority.
    """
    try:
        rule = table[record.state, event]
    except KeyError as exc:
        raise UndefinedTransition(f"undefined transition: {record.state!r}, {event!r}") from exc
    updated = Record(rule.next_state, rule.prepare(record.data, changes or {}), record.revision + 1)
    persisted = False
    try:
        persist(updated, rule.effects)
        persisted = True
    finally:
        if not persisted:
            retry(record, event, changes or {})
    return Decision(updated, rule.action, rule.effects)


class LeaseState(Enum):
    # Values map the three historical string fields in the module docstring's order.
    NO_POST = ("allocation_ready", "preparing", None)
    CAPACITY_WAIT = ("capacity_retry_ready", "preparing", None)
    CAPACITY_EXHAUSTED = ("capacity_exhausted", "preparing", None)
    CREATE_PENDING = ("allocation_pending", "preparing", None)
    CREATE_UNKNOWN = ("allocation_unknown", "preparing", None)
    ACQUIRING = ("allocated_unverified", "preparing", None)
    IDLE = ("claimed", "idle", None)
    IDLE_READY = ("claimed", "idle", "prepared")
    IDLE_STALE = ("claimed", "idle", "stale_after_failure")
    ACTIVE = ("claimed", "active", None)
    PREPARING = ("claimed", "active", "preparing")
    ACTIVE_READY = ("claimed", "active", "prepared")
    ACTIVE_STALE = ("claimed", "active", "stale_after_failure")
    # Historical preparation failure saved this before release set lease.releasing.
    CLEANUP_REQUIRED = ("cleanup_pending", "active", "unresolved")
    CLEANUP = ("cleanup_pending", "releasing", None)
    CLEANUP_READY = ("cleanup_pending", "releasing", "prepared")
    CLEANUP_STALE = ("cleanup_pending", "releasing", "stale_after_failure")
    CLEANUP_UNRESOLVED = ("cleanup_pending", "releasing", "unresolved")
    RELEASING = ("release_pending", "releasing", None)
    RELEASING_READY = ("release_pending", "releasing", "prepared")
    RELEASING_STALE = ("release_pending", "releasing", "stale_after_failure")
    RELEASING_UNRESOLVED = ("release_pending", "releasing", "unresolved")
    RELEASED = ("cleanup_complete", "releasing", None)
    RELEASED_READY = ("cleanup_complete", "releasing", "prepared")
    RELEASED_STALE = ("cleanup_complete", "releasing", "stale_after_failure")
    RELEASED_UNRESOLVED = ("cleanup_complete", "releasing", "unresolved")


class LeaseEvent(Enum):
    CLAIM = "claim"
    CREATE_STARTED = "create_started"
    CREATE_NOT_SENT = "create_not_sent"
    CREATE_SUCCEEDED = "create_succeeded"
    CREATE_UNKNOWN = "create_unknown"
    CREATE_FAILED_EXACT = "create_failed_exact"
    ACQUISITION_FAILED = "acquisition_failed"
    ACQUISITION_CONTINUE = "acquisition_continue"
    CAPACITY_REJECTED = "capacity_rejected"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    CLAIM_CONFIRMED = "claim_confirmed"
    JOB_ADMITTED = "job_admitted"
    PREPARATION_STARTED = "preparation_started"
    PREPARATION_SUCCEEDED = "preparation_succeeded"
    PREPARATION_FAILED_SAFE = "preparation_failed_safe"
    PREPARATION_FAILED = "preparation_failed"
    JOB_SUCCEEDED = "job_succeeded"
    JOB_FAILED = "job_failed"
    JOB_FAILED_TERMINAL = "job_failed_terminal"
    JOB_FAILED_STALE = "job_failed_stale"
    CANCEL = "cancel"
    RELEASE = "release"
    RELEASE_FAILED = "release_failed"
    RELEASE_RETRY = "release_retry"
    QUIESCED = "quiesced"
    RELEASE_SUCCEEDED = "release_succeeded"
    RECONCILIATION_FOUND = "reconciliation_found"
    RECONCILIATION_EMPTY = "reconciliation_empty"
    RECONCILIATION_UNKNOWN = "reconciliation_unknown"
    LATE_CREATE_SUCCEEDED = "late_create_succeeded"
    KEEP = "keep"
    RESTART = "restart"
    HOST_DEATH = "host_death"
    PAID_EXPIRY = "paid_expiry"
    IDLE_EXPIRY = "idle_expiry"
    RECIPE_CHANGED = "recipe_changed"
    MODEL_CHANGED = "model_changed"
    SOURCE_CHANGED = "source_changed"
    CHECKPOINT = "checkpoint"


class BillingAction(Enum):
    KEEP_WITH_DEADLINE = "keep_with_deadline"
    RELEASE_EXACT_POD = "release_exact_pod"
    RECONCILE_THEN_RELEASE = "reconcile_then_release"
    # A create needs distinct authority: a keep must never imply a fresh POST.
    CREATE_ONCE = "create_once"
    # Proven zero allocation or confirmed absence needs no billing operation.
    NO_BILLABLE_RESOURCE = "no_billable_resource"
    # Save a successful create even when its paid deadline just expired. This
    # grants NO provider work: immediately dispatch PAID_EXPIRY if due, otherwise
    # ACQUISITION_CONTINUE. Never wait for another timer or user event here.
    CHECK_PAID_DEADLINE = "check_paid_deadline"


ZERO_CUSTODY = frozenset({LeaseState.NO_POST, LeaseState.CAPACITY_WAIT,
                          LeaseState.CAPACITY_EXHAUSTED})
UNKNOWN_CUSTODY = frozenset({LeaseState.CREATE_PENDING, LeaseState.CREATE_UNKNOWN})
IDLE_STATES = frozenset({LeaseState.IDLE, LeaseState.IDLE_READY, LeaseState.IDLE_STALE})
ACTIVE_STATES = frozenset({LeaseState.ACTIVE, LeaseState.PREPARING,
                         LeaseState.ACTIVE_READY, LeaseState.ACTIVE_STALE})
LIVE_STATES = IDLE_STATES | ACTIVE_STATES | {LeaseState.ACQUIRING}
CLEANUP_STATES = frozenset({LeaseState.CLEANUP, LeaseState.CLEANUP_READY,
                          LeaseState.CLEANUP_STALE, LeaseState.CLEANUP_UNRESOLVED,
                          LeaseState.CLEANUP_REQUIRED})
RELEASING_STATES = frozenset({LeaseState.RELEASING, LeaseState.RELEASING_READY,
                            LeaseState.RELEASING_STALE, LeaseState.RELEASING_UNRESOLVED})
RELEASED_STATES = frozenset({LeaseState.RELEASED, LeaseState.RELEASED_READY,
                           LeaseState.RELEASED_STALE, LeaseState.RELEASED_UNRESOLVED})


def _number(data: Data, key: str) -> float:
    value = data.get(key)
    if type(value) not in (int, float) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidLeaseTransition(f"finite {key} is required")
    return float(value)


def _phase(state: LeaseState, root: str) -> LeaseState:
    preparation = state.value[2]
    if preparation == "preparing":
        preparation = "unresolved"
    return LeaseState((root, "releasing", preparation))


def _prepare(
    source: LeaseState, event: LeaseEvent, destination: LeaseState, action: BillingAction,
) -> Callable[[Data, Data], Data]:
    def apply(old: Data, changes: Data) -> Data:
        data = {**old, **changes}
        if source in ZERO_CUSTODY and old.get("pod_id"):
            raise InvalidLeaseTransition("zero-allocation state contains an exact pod")
        for key in ("operation_id", "pod_id"):
            if old.get(key) and data.get(key) != old[key] and event != LeaseEvent.CLAIM:
                raise InvalidLeaseTransition(f"{key} changed under an existing owner")
        if event != LeaseEvent.CLAIM and old.get("paid_expires_at") != data.get("paid_expires_at"):
            if event != LeaseEvent.KEEP or not changes.get("fresh_paid_consent"):
                raise InvalidLeaseTransition("only an explicitly consented keep can replace paid expiry")
        if not data.get("operation_id"):
            raise InvalidLeaseTransition("exact operation identity is required")
        if action == BillingAction.KEEP_WITH_DEADLINE and not data.get("pod_id"):
            raise InvalidLeaseTransition("retention requires the exact claimed pod")
        if event == LeaseEvent.CLAIM:
            if data.get("pod_id") or _number(data, "now") >= _number(data, "paid_expires_at"):
                raise InvalidLeaseTransition("claim requires empty custody and live paid authority")
            data.update(stop_requested=False, create_inflight=False, observations_abandoned=False)
        elif old.get("stop_requested") and not data.get("stop_requested"):
            raise InvalidLeaseTransition("only a fresh claim can clear the stop fence")
        if event == LeaseEvent.KEEP:
            if not changes.get("fresh_paid_consent"):
                raise InvalidLeaseTransition("keep requires fresh paid consent")
            if _number(data, "now") >= _number(old, "paid_expires_at"):
                raise InvalidLeaseTransition("keep cannot revive expired paid authority")
        if source in IDLE_STATES and event in {LeaseEvent.JOB_ADMITTED, LeaseEvent.KEEP}:
            if _number(data, "now") >= _number(old, "keep_deadline"):
                raise InvalidLeaseTransition("idle authority expired before admission or keep")
        if source in UNKNOWN_CUSTODY and event in {
            LeaseEvent.CANCEL, LeaseEvent.PAID_EXPIRY, LeaseEvent.RELEASE,
            LeaseEvent.RESTART, LeaseEvent.RECONCILIATION_UNKNOWN,
        }:
            data["stop_requested"] = True
        if action in {BillingAction.RELEASE_EXACT_POD, BillingAction.RECONCILE_THEN_RELEASE}:
            data["stop_requested"] = True
            data["observations_abandoned"] = True
            data["cleanup_due_at"] = _number(data, "paid_expires_at")
        if action == BillingAction.RELEASE_EXACT_POD and not data.get("pod_id"):
            raise InvalidLeaseTransition("exact pod identity is required for release")
        if event in {LeaseEvent.LATE_CREATE_SUCCEEDED, LeaseEvent.RECONCILIATION_FOUND,
                     LeaseEvent.CREATE_SUCCEEDED, LeaseEvent.CREATE_FAILED_EXACT}:
            if not changes.get("pod_id"):
                raise InvalidLeaseTransition("create/reconciliation result must contain the exact pod id")
            data["create_inflight"] = False
        if event == LeaseEvent.CREATE_STARTED:
            if old.get("stop_requested") or old.get("create_inflight"):
                raise InvalidLeaseTransition("stopped or in-flight create cannot be retried")
            if _number(data, "now") >= _number(data, "paid_expires_at"):
                raise InvalidLeaseTransition("paid authority expired before create")
            data["create_inflight"] = True
            data["safe_to_retry"] = False
        if event in {LeaseEvent.CREATE_NOT_SENT, LeaseEvent.CAPACITY_REJECTED}:
            if not changes.get("no_allocation_proof"):
                raise InvalidLeaseTransition("provider admission/rejection proof is required")
            data["create_inflight"] = False
            data["safe_to_retry"] = not data.get("stop_requested", False)
        if event == LeaseEvent.RECONCILIATION_EMPTY:
            if data.get("create_inflight") or not changes.get("absence_confirmed"):
                raise InvalidLeaseTransition("empty inventory cannot settle an in-flight create")
        if event == LeaseEvent.QUIESCED and not changes.get("drain_confirmed"):
            raise InvalidLeaseTransition("exact drain confirmation is required")
        if event == LeaseEvent.RELEASE_SUCCEEDED and not changes.get("absence_confirmed"):
            raise InvalidLeaseTransition("provider absence confirmation is required")
        if action == BillingAction.CHECK_PAID_DEADLINE:
            data["keep_deadline"] = _number(data, "paid_expires_at")
        if event in {LeaseEvent.PREPARATION_FAILED_SAFE, LeaseEvent.JOB_FAILED,
                     LeaseEvent.JOB_FAILED_STALE} and action == BillingAction.KEEP_WITH_DEADLINE:
            if not changes.get("jobs_stopped") or not changes.get("custody_verified"):
                raise InvalidLeaseTransition("failure retention requires stopped jobs and verified custody")
        if action == BillingAction.KEEP_WITH_DEADLINE:
            paid = _number(data, "paid_expires_at")
            now = _number(data, "now")
            if event == LeaseEvent.HOST_DEATH:
                pass  # A crash cannot persist a newly computed retention bound.
            elif destination in IDLE_STATES and (source not in IDLE_STATES or event == LeaseEvent.KEEP):
                data["keep_deadline"] = min(now + _number(data, "idle_seconds"), paid)
            elif destination not in IDLE_STATES:
                data["keep_deadline"] = paid
            deadline = _number(data, "keep_deadline")
            if deadline > paid or (event != LeaseEvent.HOST_DEATH and now >= deadline):
                raise InvalidLeaseTransition("retention deadline is expired or exceeds paid authority")
            if data.get("stop_requested"):
                raise InvalidLeaseTransition("cleanup custody cannot be retained or executed")
        if action == BillingAction.NO_BILLABLE_RESOURCE:
            data["safe_to_retry"] = destination in RELEASED_STATES or not data.get("stop_requested", False)
        return data
    return apply


def _build_table() -> Mapping[tuple[LeaseState, LeaseEvent], Rule[LeaseState, BillingAction]]:
    table: dict[tuple[LeaseState, LeaseEvent], Rule[LeaseState, BillingAction]] = {}

    def add(source: LeaseState, event: LeaseEvent, destination: LeaseState,
            action: BillingAction, *effects: str) -> None:
        table[source, event] = Rule(destination, action, effects,
                                    _prepare(source, event, destination, action))

    def stop(source: LeaseState, event: LeaseEvent, *, reconcile: bool = False) -> None:
        if source in RELEASED_STATES:
            add(source, event, source, BillingAction.NO_BILLABLE_RESOURCE)
        elif source in ZERO_CUSTODY:
            add(source, event, LeaseState.RELEASED, BillingAction.NO_BILLABLE_RESOURCE,
                "settle_no_allocation")
        elif source in UNKNOWN_CUSTODY:
            add(source, event, LeaseState.CREATE_UNKNOWN, BillingAction.RECONCILE_THEN_RELEASE,
                "fence_admission", "retain_late_create_result", "write_ahead_reconciliation")
        else:
            add(source, event, _phase(source, "cleanup_pending"),
                BillingAction.RECONCILE_THEN_RELEASE if reconcile else BillingAction.RELEASE_EXACT_POD,
                "fence_admission", "abandon_observations", "write_ahead_release",
                "release_annotation_in_retry_boundary")

    safety = (LeaseEvent.RESTART, LeaseEvent.PAID_EXPIRY, LeaseEvent.IDLE_EXPIRY,
              LeaseEvent.JOB_FAILED, LeaseEvent.JOB_FAILED_TERMINAL, LeaseEvent.CANCEL,
              LeaseEvent.RECIPE_CHANGED, LeaseEvent.MODEL_CHANGED, LeaseEvent.SOURCE_CHANGED,
              LeaseEvent.RELEASE)
    for state in LeaseState:
        for event in safety:
            stop(state, event, reconcile=event == LeaseEvent.RESTART)
        # This models a crash, not a handler that must run during that crash.
        if state in LIVE_STATES:
            add(state, LeaseEvent.HOST_DEATH, state, BillingAction.KEEP_WITH_DEADLINE)
        else:
            stop(state, LeaseEvent.HOST_DEATH, reconcile=True)
        if state in ACTIVE_STATES or state == LeaseState.ACQUIRING:
            add(state, LeaseEvent.IDLE_EXPIRY, state, BillingAction.KEEP_WITH_DEADLINE)
        if state in LIVE_STATES:
            add(state, LeaseEvent.CHECKPOINT, state, BillingAction.KEEP_WITH_DEADLINE,
                "checkpoint_exact_owner")
        if state in CLEANUP_STATES | RELEASING_STATES:
            stop(state, LeaseEvent.RELEASE_FAILED)
            stop(state, LeaseEvent.RELEASE_RETRY, reconcile=True)
        if state in CLEANUP_STATES:
            add(state, LeaseEvent.QUIESCED, _phase(state, "release_pending"),
                BillingAction.RELEASE_EXACT_POD, "write_ahead_terminate_exact_pod")
        if state in RELEASING_STATES:
            add(state, LeaseEvent.RELEASE_SUCCEEDED, _phase(state, "cleanup_complete"),
                BillingAction.NO_BILLABLE_RESOURCE, "record_exact_absence")
        if state in IDLE_STATES | ACTIVE_STATES:
            add(state, LeaseEvent.KEEP, state, BillingAction.KEEP_WITH_DEADLINE,
                "replace_paid_authority_with_fresh_consent", "persist_deadlines_before_timer")
    for state in RELEASED_STATES:
        add(state, LeaseEvent.CLAIM, LeaseState.NO_POST, BillingAction.NO_BILLABLE_RESOURCE,
            "write_ahead_claim", "persist_paid_deadline")
    for state in {LeaseState.NO_POST, LeaseState.CAPACITY_WAIT}:
        add(state, LeaseEvent.CREATE_STARTED, LeaseState.CREATE_PENDING,
            BillingAction.CREATE_ONCE, "write_ahead_create_attempt")
        stop(state, LeaseEvent.CAPACITY_EXHAUSTED)
    for state in ZERO_CUSTODY:
        add(state, LeaseEvent.RECONCILIATION_UNKNOWN, LeaseState.CREATE_UNKNOWN,
            BillingAction.RECONCILE_THEN_RELEASE, "fence_admission", "write_ahead_reconciliation")
    for state in UNKNOWN_CUSTODY:
        add(state, LeaseEvent.CREATE_NOT_SENT, LeaseState.RELEASED,
            BillingAction.NO_BILLABLE_RESOURCE, "record_no_post_sent", "settle_no_allocation")
        add(state, LeaseEvent.CAPACITY_REJECTED, LeaseState.CAPACITY_WAIT,
            BillingAction.NO_BILLABLE_RESOURCE, "record_definite_capacity_rejection")
        for event in (LeaseEvent.CREATE_UNKNOWN, LeaseEvent.RECONCILIATION_UNKNOWN):
            stop(state, event)
        for event in (LeaseEvent.LATE_CREATE_SUCCEEDED, LeaseEvent.RECONCILIATION_FOUND,
                      LeaseEvent.CREATE_FAILED_EXACT):
            add(state, event, LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD,
                "journal_exact_pod", "abandon_observations", "write_ahead_release")
        add(state, LeaseEvent.RECONCILIATION_EMPTY, LeaseState.RELEASED,
            BillingAction.NO_BILLABLE_RESOURCE, "record_create_finished_and_absent")
    add(LeaseState.CREATE_PENDING, LeaseEvent.CREATE_SUCCEEDED, LeaseState.ACQUIRING,
        BillingAction.CHECK_PAID_DEADLINE, "journal_exact_pod", "persist_paid_deadline")
    add(LeaseState.ACQUIRING, LeaseEvent.ACQUISITION_CONTINUE, LeaseState.ACQUIRING,
        BillingAction.KEEP_WITH_DEADLINE, "bound_observation_by_paid_deadline")
    add(LeaseState.ACQUIRING, LeaseEvent.CLAIM_CONFIRMED, LeaseState.IDLE,
        BillingAction.KEEP_WITH_DEADLINE, "persist_deadlines_before_timer")
    stop(LeaseState.ACQUIRING, LeaseEvent.ACQUISITION_FAILED)
    for idle, active in ((LeaseState.IDLE, LeaseState.ACTIVE),
                         (LeaseState.IDLE_READY, LeaseState.ACTIVE_READY),
                         (LeaseState.IDLE_STALE, LeaseState.ACTIVE_STALE)):
        add(idle, LeaseEvent.JOB_ADMITTED, active, BillingAction.KEEP_WITH_DEADLINE,
            "fence_exact_invocation", "pause_idle_clock")
    for state in ACTIVE_STATES:
        if state != LeaseState.PREPARING:
            add(state, LeaseEvent.PREPARATION_STARTED, LeaseState.PREPARING,
                BillingAction.KEEP_WITH_DEADLINE, "write_ahead_preparation")
        stop(state, LeaseEvent.PREPARATION_FAILED)
    add(LeaseState.PREPARING, LeaseEvent.PREPARATION_SUCCEEDED, LeaseState.ACTIVE_READY,
        BillingAction.KEEP_WITH_DEADLINE, "checkpoint_exact_preparation")
    add(LeaseState.PREPARING, LeaseEvent.PREPARATION_FAILED_SAFE, LeaseState.ACTIVE_STALE,
        BillingAction.KEEP_WITH_DEADLINE, "checkpoint_stopped_stale_preparation")
    add(LeaseState.ACTIVE_READY, LeaseEvent.JOB_SUCCEEDED, LeaseState.IDLE_READY,
        BillingAction.KEEP_WITH_DEADLINE, "persist_deadlines_before_timer")
    for active, idle in ((LeaseState.ACTIVE_READY, LeaseState.IDLE_READY),
                         (LeaseState.ACTIVE_STALE, LeaseState.IDLE_STALE)):
        add(active, LeaseEvent.JOB_FAILED, idle, BillingAction.KEEP_WITH_DEADLINE,
            "checkpoint_stopped_job", "persist_deadlines_before_timer")
    add(LeaseState.ACTIVE_READY, LeaseEvent.JOB_FAILED_STALE, LeaseState.IDLE_STALE,
        BillingAction.KEEP_WITH_DEADLINE, "checkpoint_stopped_stale_preparation",
        "persist_deadlines_before_timer")
    return MappingProxyType(table)


LEASE_TRANSITIONS = _build_table()
