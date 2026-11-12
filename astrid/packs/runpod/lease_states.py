"""Lease policy only: no provider calls and no existing-writer wiring.

Values map (record.state, lease.state, preparation.state, warm_session.state).
None means absent. Cleanup archives preparation evidence in data and clears its
live field; cleanup, termination and confirmed absence each have one state.

A short transition lock covers read, decision and durable compare-and-swap only.
CREATE_STARTED / ACQUISITION_CONTINUE persist revocable dispatch INTENT, not a
provider-call permit. After every queue or provider-gate wait, the executor must
submit PROVIDER_ENTRY with that exact DispatchTicket, under the SAME lock used
by cancellation, and enter the provider immediately after unlocking. Entry is
its linearization point: no intervening queue, gate wait or reused permit. An
admitted create remains uncertain until its fenced completion, even if stopped.
An observation needs a fresh admission for each provider operation; abandoned
results cannot continue work. Cleanup has its own executor and never joins an
observation or waits for its executor shutdown. No lock spans provider I/O.

Deadlines are absolute journal data, not timers. HOST_DEATH describes existing
durable obligations (no crash-time handler); RESTART enforces them. Existing
calls install no independent provider stop: controller absence has no finite
billing bound. Successful completion chains do not prove outage liveness.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from astrid.core.execution.transitions import Data, Outcome, Record, Rule


class InvalidLeaseTransition(ValueError):
    """Invalid identity or proof; ordinary expiry instead produces cleanup."""


class LeaseState(Enum):
    CAPACITY_WAIT = ("capacity_retry_ready", "preparing", None, None)
    CREATE_PENDING = ("allocation_pending", "preparing", None, None)
    CREATE_UNKNOWN = ("allocation_unknown", "preparing", None, None)
    ACQUIRING = ("allocated_unverified", "preparing", None, None)
    ATTACHMENT_PENDING = ("attachment_pending", None, None, None)
    ATTACHED_UNVERIFIED = ("attached_unverified", None, None, None)
    IDLE = ("claimed", "idle", None, None)
    IDLE_READY = ("claimed", "idle", "prepared", None)
    IDLE_STALE = ("claimed", "idle", "stale_after_failure", None)
    ACTIVE = ("claimed", "active", None, None)
    PREPARING = ("claimed", "active", "preparing", None)
    ACTIVE_READY = ("claimed", "active", "prepared", None)
    ACTIVE_STALE = ("claimed", "active", "stale_after_failure", None)
    WARM_IDLE = ("claimed", None, None, "idle")
    WARM_ACTIVE = ("claimed", None, None, "active")
    CLEANUP = ("cleanup_pending", "releasing", None, None)
    RELEASING = ("release_pending", "releasing", None, None)
    RELEASED = ("cleanup_complete", "releasing", None, None)


class LeaseEvent(Enum):
    CLAIM = "claim"
    CREATE_STARTED = "create_started"
    PROVIDER_ENTRY = "provider_entry"
    CREATE_NOT_SENT = "create_not_sent"
    CREATE_SUCCEEDED = "create_succeeded"
    CREATE_UNKNOWN = "create_unknown"
    CREATE_FAILED_EXACT = "create_failed_exact"
    ACQUISITION_FAILED = "acquisition_failed"
    ACQUISITION_CONTINUE = "acquisition_continue"
    CAPACITY_REJECTED = "capacity_rejected"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    ATTACHMENT_REQUESTED = "attachment_requested"
    ATTACHMENT_BOUND = "attachment_bound"
    CLAIM_CONFIRMED = "claim_confirmed"
    WARM_SESSION_BOUND = "warm_session_bound"
    WARM_SESSION_EXPIRED = "warm_session_expired"
    JOB_ADMITTED = "job_admitted"
    PREPARATION_STARTED = "preparation_started"
    PREPARATION_SUCCEEDED = "preparation_succeeded"
    PREPARATION_INVALIDATED = "preparation_invalidated"
    PREPARATION_FAILED_SAFE = "preparation_failed_safe"
    PREPARATION_FAILED = "preparation_failed"
    JOB_SUCCEEDED = "job_succeeded"
    JOB_FAILED = "job_failed"
    JOB_FAILED_TERMINAL = "job_failed_terminal"
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
    # Intent alone never authorizes provider entry.
    AWAIT_PROVIDER_ENTRY = "await_provider_entry"
    CREATE_ONCE = "create_once"
    OBSERVE_ONCE = "observe_once"
    NO_BILLABLE_RESOURCE = "no_billable_resource"


# The future sole-writer guard excludes these action identities, not whole
# shared modules or persistence helpers that may also receive in-scope records.
EXCLUDED_LEASE_ACTIONS = frozenset({"runpod.provision", "runpod.session"})

# Startup terminal-request recovery picks up BOTH states, with exact identity.
# This is eligibility, not publication/cede policy (those belong to later work).
STARTUP_RECOVERY_STATES = frozenset({"running", "cancel_requested"})


def startup_recovery_eligible(
    state: str, *, attempt_id: object, expected_attempt_id: object,
    fence: object, expected_fence: object, executor_id: object, expected_executor_id: object,
) -> bool:
    return (state in STARTUP_RECOVERY_STATES and expected_attempt_id is not None
            and expected_fence is not None and attempt_id == expected_attempt_id
            and fence == expected_fence and executor_id in (None, expected_executor_id))


ZERO_CUSTODY = frozenset({LeaseState.CAPACITY_WAIT})
UNKNOWN_CUSTODY = frozenset({LeaseState.CREATE_PENDING, LeaseState.CREATE_UNKNOWN})
ATTACHMENT_STATES = frozenset({LeaseState.ATTACHMENT_PENDING, LeaseState.ATTACHED_UNVERIFIED})
IDLE_STATES = frozenset({LeaseState.IDLE, LeaseState.IDLE_READY, LeaseState.IDLE_STALE})
ACTIVE_STATES = frozenset({LeaseState.ACTIVE, LeaseState.PREPARING,
                         LeaseState.ACTIVE_READY, LeaseState.ACTIVE_STALE})
WARM_STATES = frozenset({LeaseState.WARM_IDLE, LeaseState.WARM_ACTIVE})
LIVE_STATES = IDLE_STATES | ACTIVE_STATES | WARM_STATES | {LeaseState.ACQUIRING}
CLEANUP_STATES = frozenset({LeaseState.CLEANUP})
RELEASING_STATES = frozenset({LeaseState.RELEASING})
RELEASED_STATES = frozenset({LeaseState.RELEASED})

# Never merge these from caller changes, even when the supplied value is equal.
# Defining events derive them from recorded facts and validated evidence inputs.
CUSTODY_CONTROL_FIELDS = frozenset({
    "create_inflight", "create_attempt", "stop_requested", "cancellation_fence",
    "paid_expires_at", "keep_deadline", "cleanup_due_at", "idle_seconds",
    "absence_confirmed", "safe_to_retry", "observations_abandoned", "owner_epoch",
    "release_authorized", "dispatch_attempt", "dispatch_kind", "dispatch_fence",
    "dispatch_deadline", "dispatch_owner", "dispatch_admitted", "attempt_ids",
    "archived_preparation", "archived_warm_session", "capacity_rejection",
    "capacity_rejected_attempt",
})


@dataclass(frozen=True)
class DispatchTicket:
    operation_id: object
    owner_epoch: object
    attempt_id: object
    cancellation_fence: object
    deadline: float
    kind: str


def _number(data: Data, key: str) -> float:
    value = data.get(key)
    if type(value) not in (int, float) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidLeaseTransition(f"finite {key} is required")
    return float(value)


def dispatch_ticket(record: Record[LeaseState]) -> DispatchTicket:
    """Read an intent ticket; only PROVIDER_ENTRY can admit its provider action."""
    data = record.data
    if not data.get("dispatch_attempt") or data.get("dispatch_kind") not in {"create", "observe"}:
        raise InvalidLeaseTransition("no dispatch intent")
    return DispatchTicket(data.get("operation_id"), data.get("dispatch_owner"),
                          data["dispatch_attempt"], data.get("dispatch_fence"),
                          _number(data, "dispatch_deadline"), str(data["dispatch_kind"]))


def _cleanup(data: dict[str, object], source: LeaseState, *, reconcile: bool = False,
             effects: tuple[str, ...] = ()) -> Outcome[LeaseState, BillingAction]:
    if not data.get("stop_requested"):
        data["cancellation_fence"] = int(_number(data, "cancellation_fence")) + 1
    data.update(stop_requested=True, observations_abandoned=True,
                cleanup_due_at=_number(data, "paid_expires_at"), safe_to_retry=False)
    if source in WARM_STATES:
        data["archived_warm_session"] = source.value[3]
    preparation = source.value[2]
    if preparation is not None:
        data["archived_preparation"] = preparation
    if source in RELEASED_STATES:
        return Outcome(source, BillingAction.NO_BILLABLE_RESOURCE, effects, data)
    if source in ZERO_CUSTODY and not data.get("pod_id"):
        data.update(safe_to_retry=True, absence_confirmed=True)
        return Outcome(LeaseState.RELEASED, BillingAction.NO_BILLABLE_RESOURCE,
                       (*effects, "settle_no_allocation"), data)
    if not data.get("pod_id") and not data.get("requested_pod_id"):
        return Outcome(LeaseState.CREATE_UNKNOWN, BillingAction.RECONCILE_THEN_RELEASE,
                       (*effects, "fence_admission", "retain_late_create_result",
                        "write_ahead_reconciliation"), data)
    # Exact attachment's requested id is sufficient for reconciliation; it does
    # NOT grant deletion. QUIESCED requires the recorded exclusive authority.
    action = (BillingAction.RECONCILE_THEN_RELEASE if reconcile or not data.get("release_authorized")
              else BillingAction.RELEASE_EXACT_POD)
    return Outcome(LeaseState.CLEANUP, action,
                   (*effects, "fence_admission", "abandon_observations", "write_ahead_release",
                    "release_annotation_in_retry_boundary"), data)


def _resolve(
    source: LeaseState, event: LeaseEvent, destination: LeaseState, action: BillingAction,
    effects: tuple[str, ...],
) -> Callable[[Data, Data], Outcome[LeaseState, BillingAction]]:
    def apply(old: Data, changes: Data) -> Outcome[LeaseState, BillingAction]:
        forbidden = CUSTODY_CONTROL_FIELDS.intersection(changes)
        if forbidden:
            raise InvalidLeaseTransition(f"transition-owned fields: {', '.join(sorted(forbidden))}")
        evidence_keys = {"ticket", "attempt_id", "no_allocation_proof", "absence_proof",
                         "drain_confirmed", "exclusive_release_proof", "compatible",
                         "custody_verified", "jobs_stopped", "new_owner_epoch",
                         "paid_authority_proof", "paid_deadline", "idle_duration"}
        data = {**old, **{key: value for key, value in changes.items() if key not in evidence_keys}}
        for key in ("operation_id", "pod_id", "requested_pod_id"):
            if old.get(key) and data.get(key) != old[key] and event not in {
                LeaseEvent.CLAIM, LeaseEvent.ATTACHMENT_REQUESTED,
            }:
                raise InvalidLeaseTransition(f"{key} changed under an existing owner")
        if not data.get("operation_id"):
            raise InvalidLeaseTransition("exact operation identity is required")
        if source in ZERO_CUSTODY and (old.get("pod_id") or old.get("create_inflight")):
            raise InvalidLeaseTransition("zero custody contains a pod or in-flight create")
        if "requested_pod_id" in changes and event != LeaseEvent.ATTACHMENT_REQUESTED:
            raise InvalidLeaseTransition("requested identity requires an attachment request")
        if "pod_id" in changes and event not in {
            LeaseEvent.CREATE_SUCCEEDED, LeaseEvent.LATE_CREATE_SUCCEEDED,
            LeaseEvent.CREATE_FAILED_EXACT, LeaseEvent.RECONCILIATION_FOUND,
            LeaseEvent.ATTACHMENT_BOUND, LeaseEvent.WARM_SESSION_BOUND,
        }:
            raise InvalidLeaseTransition("exact identity requires a binding event")
        now = _number(data, "now")
        if event in {LeaseEvent.CLAIM, LeaseEvent.ATTACHMENT_REQUESTED}:
            paid = _number(changes, "paid_deadline")
            if paid <= now:
                raise InvalidLeaseTransition("new authority must have a future deadline")
            if not changes.get("new_owner_epoch"):
                raise InvalidLeaseTransition("new owner epoch is required")
            if event == LeaseEvent.CLAIM:
                # Terminal evidence is archived, never inherited as new custody.
                data.update(pod_id=None, requested_pod_id=None)
            data.update(paid_expires_at=paid, keep_deadline=paid,
                        idle_seconds=_number(changes, "idle_duration"),
                        owner_epoch=changes["new_owner_epoch"], stop_requested=False,
                        cancellation_fence=0, observations_abandoned=False,
                        absence_confirmed=False, create_inflight=False,
                        create_attempt=None, dispatch_attempt=None, attempt_ids=(),
                        capacity_rejection=None, capacity_rejected_attempt=None,
                        release_authorized=event == LeaseEvent.CLAIM,
                        safe_to_retry=event == LeaseEvent.CLAIM)
            if event == LeaseEvent.ATTACHMENT_REQUESTED:
                if not changes.get("requested_pod_id"):
                    raise InvalidLeaseTransition("attachment requires an exact requested id")
                data["pod_id"] = None
                data["release_authorized"] = changes.get("exclusive_release_proof") is True
            return Outcome(destination, action, effects, data)

        if source in CLEANUP_STATES | RELEASING_STATES:
            completed_preparation = {
                LeaseEvent.PREPARATION_SUCCEEDED: "prepared",
                LeaseEvent.PREPARATION_FAILED_SAFE: "stale_after_failure",
                LeaseEvent.PREPARATION_INVALIDATED: "stale_after_failure",
            }
            if event in completed_preparation:
                data["archived_preparation"] = completed_preparation[event]

        # Exact create completions are fenced independently of revoked dispatch
        # and current epochs: cancellation cannot discard a paid late result.
        if event in {LeaseEvent.CREATE_SUCCEEDED, LeaseEvent.LATE_CREATE_SUCCEEDED,
                     LeaseEvent.CREATE_FAILED_EXACT, LeaseEvent.CREATE_NOT_SENT,
                     LeaseEvent.CAPACITY_REJECTED}:
            if not old.get("create_attempt") or changes.get("attempt_id") != old["create_attempt"]:
                raise InvalidLeaseTransition("create completion has a foreign attempt")
            if event in {LeaseEvent.CREATE_NOT_SENT, LeaseEvent.CAPACITY_REJECTED}:
                if changes.get("no_allocation_proof") is not True:
                    raise InvalidLeaseTransition("provider no-allocation proof is required")
                data.update(create_inflight=False, absence_confirmed=True)
                if event == LeaseEvent.CAPACITY_REJECTED:
                    data.update(capacity_rejection="definite_no_allocation",
                                capacity_rejected_attempt=old["create_attempt"])
            else:
                if not old.get("create_inflight") or not changes.get("pod_id"):
                    raise InvalidLeaseTransition("exact create completion requires an admitted create and pod")
                data.update(create_inflight=False, release_authorized=True)
        if event == LeaseEvent.RECONCILIATION_FOUND:
            if not changes.get("pod_id"):
                raise InvalidLeaseTransition("reconciliation requires an exact pod")
            if old.get("requested_pod_id") and changes["pod_id"] != old["requested_pod_id"]:
                raise InvalidLeaseTransition("reconciliation differs from exact requested id")
            # Inventory is NOT proof the create completed. Keep its in-flight
            # fence even after discovering an exact pod while the POST is out.
        if event == LeaseEvent.ATTACHMENT_BOUND:
            if not changes.get("pod_id") or changes["pod_id"] != old.get("requested_pod_id"):
                raise InvalidLeaseTransition("attachment result differs from exact requested id")
        if event == LeaseEvent.RECONCILIATION_EMPTY:
            if old.get("create_inflight") or changes.get("absence_proof") is not True:
                raise InvalidLeaseTransition("empty inventory cannot settle an in-flight create without absence proof")
            data["absence_confirmed"] = True
        if event == LeaseEvent.QUIESCED:
            if changes.get("drain_confirmed") is not True:
                raise InvalidLeaseTransition("exact drain confirmation is required")
            if not old.get("release_authorized"):
                raise InvalidLeaseTransition("exclusive release authority is required")
            if not (old.get("pod_id") or old.get("requested_pod_id")):
                raise InvalidLeaseTransition("exact release identity is required")
        if event == LeaseEvent.RELEASE_SUCCEEDED:
            if old.get("create_inflight") or changes.get("absence_proof") is not True:
                raise InvalidLeaseTransition("completed create and exact absence proof are required")
            data.update(absence_confirmed=True, safe_to_retry=True)
        if event in {LeaseEvent.ATTACHMENT_BOUND, LeaseEvent.CLAIM_CONFIRMED}:
            ticket = changes.get("ticket")
            if (not isinstance(ticket, DispatchTicket) or ticket.kind != "observe"
                    or ticket != dispatch_ticket(Record(source, old)) or not old.get("dispatch_admitted")):
                raise InvalidLeaseTransition("observation completion requires its admitted exact ticket")
        if event == LeaseEvent.WARM_SESSION_BOUND:
            if not changes.get("pod_id") or changes.get("exclusive_release_proof") is not True:
                raise InvalidLeaseTransition("warm session requires exact pod and exclusive release proof")
            data["release_authorized"] = True

        if event == LeaseEvent.HOST_DEATH and source in LIVE_STATES:
            # No computation can be relied upon at crash time, even overdue.
            return Outcome(source, BillingAction.KEEP_WITH_DEADLINE, (), old)

        if event == LeaseEvent.KEEP:
            if changes.get("paid_authority_proof") is not True:
                raise InvalidLeaseTransition("keep requires explicit paid authority proof")
        if event in {LeaseEvent.PREPARATION_FAILED_SAFE, LeaseEvent.JOB_FAILED} and action == BillingAction.KEEP_WITH_DEADLINE:
            if changes.get("jobs_stopped") is not True or changes.get("custody_verified") is not True:
                raise InvalidLeaseTransition("failure retention requires stopped jobs and verified custody")
        paid = _number(old, "paid_expires_at")
        expired = now >= paid or (source in IDLE_STATES | {LeaseState.WARM_IDLE}
                                 and now >= _number(old, "keep_deadline"))

        if event == LeaseEvent.PROVIDER_ENTRY:
            ticket = changes.get("ticket")
            if not isinstance(ticket, DispatchTicket) or ticket != dispatch_ticket(Record(source, old)):
                raise InvalidLeaseTransition("dispatch ticket has a foreign attempt, fence or deadline")
            if old.get("dispatch_admitted"):
                raise InvalidLeaseTransition("dispatch ticket already consumed")
            if (source in {LeaseState.CREATE_UNKNOWN, LeaseState.CLEANUP}
                    or old.get("stop_requested") or expired or ticket.owner_epoch != old.get("owner_epoch")
                    or ticket.cancellation_fence != old.get("cancellation_fence") or ticket.deadline != paid):
                return _cleanup(data, source)
            data["dispatch_admitted"] = True
            if ticket.kind == "create":
                data["create_inflight"] = True
                return Outcome(source, BillingAction.CREATE_ONCE, ("write_ahead_provider_entry",), data)
            return Outcome(source, BillingAction.OBSERVE_ONCE, ("write_ahead_provider_entry",), data)

        if event == LeaseEvent.RESTART and source in IDLE_STATES:
            if (not expired and _number(old, "keep_deadline") <= paid
                    and not old.get("stop_requested") and not old.get("create_inflight")
                    and old.get("pod_id") and changes.get("compatible") is True
                    and changes.get("custody_verified") is True):
                epoch = changes.get("new_owner_epoch")
                if not epoch or epoch == old.get("owner_epoch"):
                    raise InvalidLeaseTransition("recovery requires a new owner epoch")
                data["owner_epoch"] = epoch
                data["dispatch_attempt"] = None
                return Outcome(source, BillingAction.KEEP_WITH_DEADLINE,
                               ("adopt_verified_idle_owner", "rearm_original_deadlines"), data)
            return _cleanup(data, source, reconcile=True)

        # All valid retention/admission/completion events choose cleanup when
        # late or stopped. No caller timer/exception policy is required.
        if action in {BillingAction.KEEP_WITH_DEADLINE, BillingAction.AWAIT_PROVIDER_ENTRY}:
            if expired or old.get("stop_requested"):
                return _cleanup(data, destination if destination in LIVE_STATES else source, effects=effects)
        if event == LeaseEvent.RECONCILIATION_UNKNOWN and source in ZERO_CUSTODY:
            data.update(stop_requested=True, safe_to_retry=False, absence_confirmed=False,
                        observations_abandoned=True,
                        cancellation_fence=int(_number(old, "cancellation_fence")) + 1)
            return Outcome(LeaseState.CREATE_UNKNOWN, BillingAction.RECONCILE_THEN_RELEASE,
                           ("fence_admission", "write_ahead_reconciliation"), data)
        if event == LeaseEvent.CAPACITY_REJECTED and (expired or old.get("stop_requested")):
            data["safe_to_retry"] = True
            return Outcome(LeaseState.RELEASED, BillingAction.NO_BILLABLE_RESOURCE,
                           (*effects, "settle_no_allocation"), data)
        if action in {BillingAction.RELEASE_EXACT_POD, BillingAction.RECONCILE_THEN_RELEASE} and event != LeaseEvent.QUIESCED:
            return _cleanup(data, source, reconcile=action == BillingAction.RECONCILE_THEN_RELEASE,
                            effects=effects)

        if event in {LeaseEvent.CREATE_STARTED, LeaseEvent.ACQUISITION_CONTINUE}:
            attempt = changes.get("attempt_id")
            prior_attempts = old.get("attempt_ids", ())
            if not isinstance(prior_attempts, (tuple, list)):
                raise InvalidLeaseTransition("recorded attempts must be a sequence")
            attempts = tuple(prior_attempts)
            if not isinstance(attempt, str) or not attempt or attempt in attempts:
                raise InvalidLeaseTransition("dispatch requires a fresh exact attempt")
            if event == LeaseEvent.CREATE_STARTED and (old.get("create_inflight") or old.get("stop_requested")):
                raise InvalidLeaseTransition("stopped or uncertain create cannot be retried")
            data.update(dispatch_attempt=attempt,
                        dispatch_kind="create" if event == LeaseEvent.CREATE_STARTED else "observe",
                        dispatch_fence=old.get("cancellation_fence"), dispatch_deadline=paid,
                        dispatch_owner=old.get("owner_epoch"), dispatch_admitted=False,
                        attempt_ids=(*attempts, attempt), safe_to_retry=False)
            if event == LeaseEvent.CREATE_STARTED:
                data.update(create_attempt=attempt, absence_confirmed=False)
        if action == BillingAction.KEEP_WITH_DEADLINE:
            if not data.get("pod_id"):
                raise InvalidLeaseTransition("retention requires exact claimed custody")
            if event == LeaseEvent.KEEP:
                paid = _number(changes, "paid_deadline")
                if paid <= now:
                    raise InvalidLeaseTransition("replacement authority must have a future deadline")
                data["paid_expires_at"] = paid
                data["idle_seconds"] = _number(changes, "idle_duration")
            if destination in IDLE_STATES | {LeaseState.WARM_IDLE}:
                if source not in IDLE_STATES | {LeaseState.WARM_IDLE} or event == LeaseEvent.KEEP:
                    data["keep_deadline"] = min(now + _number(data, "idle_seconds"), paid)
                if now >= _number(data, "keep_deadline"):
                    return _cleanup(data, source, effects=effects)
            else:
                data["keep_deadline"] = paid
            if _number(data, "keep_deadline") > paid:
                raise InvalidLeaseTransition("durable idle deadline exceeds paid authority")
        if action == BillingAction.NO_BILLABLE_RESOURCE and destination == LeaseState.RELEASED:
            if source in ZERO_CUSTODY:
                return _cleanup(data, source, effects=effects)
            data["safe_to_retry"] = True
        if event in {LeaseEvent.CREATE_NOT_SENT, LeaseEvent.CAPACITY_REJECTED}:
            data["safe_to_retry"] = True
        return Outcome(destination, action, effects, data)
    return apply


def _build_table() -> Mapping[tuple[LeaseState, LeaseEvent], Rule[LeaseState, BillingAction]]:
    table: dict[tuple[LeaseState, LeaseEvent], Rule[LeaseState, BillingAction]] = {}

    def add(source: LeaseState, event: LeaseEvent, destination: LeaseState,
            action: BillingAction, *effects: str) -> None:
        table[source, event] = Rule(destination, action, effects,
                                   _resolve(source, event, destination, action, effects))

    def stop(source: LeaseState, event: LeaseEvent, *, reconcile: bool = False) -> None:
        if source in RELEASED_STATES | ZERO_CUSTODY:
            add(source, event, LeaseState.RELEASED, BillingAction.NO_BILLABLE_RESOURCE,
                "settle_no_allocation")
        else:
            add(source, event, LeaseState.CREATE_UNKNOWN if source in UNKNOWN_CUSTODY else LeaseState.CLEANUP,
                BillingAction.RECONCILE_THEN_RELEASE if reconcile or source in UNKNOWN_CUSTODY
                else BillingAction.RELEASE_EXACT_POD)

    safety = (LeaseEvent.RESTART, LeaseEvent.PAID_EXPIRY, LeaseEvent.IDLE_EXPIRY,
              LeaseEvent.JOB_FAILED, LeaseEvent.JOB_FAILED_TERMINAL, LeaseEvent.CANCEL,
              LeaseEvent.RECIPE_CHANGED, LeaseEvent.MODEL_CHANGED, LeaseEvent.SOURCE_CHANGED,
              LeaseEvent.RELEASE, LeaseEvent.HOST_DEATH)
    for state in LeaseState:
        for event in safety:
            stop(state, event, reconcile=event in {LeaseEvent.RESTART, LeaseEvent.HOST_DEATH})
        if state in LIVE_STATES:
            add(state, LeaseEvent.HOST_DEATH, state, BillingAction.KEEP_WITH_DEADLINE)
            add(state, LeaseEvent.CHECKPOINT, state, BillingAction.KEEP_WITH_DEADLINE,
                "checkpoint_exact_owner")
        if state in ACTIVE_STATES | {LeaseState.ACQUIRING, LeaseState.WARM_ACTIVE}:
            add(state, LeaseEvent.IDLE_EXPIRY, state, BillingAction.KEEP_WITH_DEADLINE)
        if state in CLEANUP_STATES | RELEASING_STATES:
            stop(state, LeaseEvent.RELEASE_FAILED)
            stop(state, LeaseEvent.RELEASE_RETRY, reconcile=True)
            # A draining holder may settle/account without reopening admission.
            # Likewise, a preparation completion after cancellation is evidence
            # for cleanup, never authority to execute the prepared worker.
            for event in (LeaseEvent.JOB_SUCCEEDED, LeaseEvent.JOB_FAILED,
                          LeaseEvent.PREPARATION_SUCCEEDED, LeaseEvent.PREPARATION_FAILED_SAFE,
                          LeaseEvent.PREPARATION_INVALIDATED):
                add(state, event, LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD,
                    "record_late_completion_without_admission")
            # An inventory result does not resolve an in-flight create. Its
            # completion can arrive after cleanup already discovered the pod.
            for event in (LeaseEvent.LATE_CREATE_SUCCEEDED, LeaseEvent.CREATE_SUCCEEDED):
                add(state, event, LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD,
                    "journal_exact_pod")
        if state in IDLE_STATES | ACTIVE_STATES:
            add(state, LeaseEvent.KEEP, state, BillingAction.KEEP_WITH_DEADLINE,
                "persist_replacement_authority", "persist_deadlines_before_timer")
    add(LeaseState.CLEANUP, LeaseEvent.QUIESCED, LeaseState.RELEASING,
        BillingAction.RELEASE_EXACT_POD, "write_ahead_terminate_exact_pod")
    add(LeaseState.RELEASING, LeaseEvent.RELEASE_SUCCEEDED, LeaseState.RELEASED,
        BillingAction.NO_BILLABLE_RESOURCE, "record_exact_absence")
    add(LeaseState.RELEASED, LeaseEvent.CLAIM, LeaseState.CAPACITY_WAIT,
        BillingAction.NO_BILLABLE_RESOURCE, "write_ahead_claim", "persist_paid_deadline")
    add(LeaseState.RELEASED, LeaseEvent.ATTACHMENT_REQUESTED, LeaseState.ATTACHMENT_PENDING,
        BillingAction.NO_BILLABLE_RESOURCE, "write_ahead_exact_attachment", "persist_paid_deadline")
    add(LeaseState.ATTACHMENT_PENDING, LeaseEvent.ATTACHMENT_BOUND, LeaseState.ATTACHED_UNVERIFIED,
        BillingAction.KEEP_WITH_DEADLINE, "journal_exact_attachment")
    for state in ATTACHMENT_STATES:
        stop(state, LeaseEvent.ACQUISITION_FAILED)
        stop(state, LeaseEvent.RECONCILIATION_UNKNOWN, reconcile=True)
        add(state, LeaseEvent.RECONCILIATION_FOUND, LeaseState.CLEANUP,
            BillingAction.RECONCILE_THEN_RELEASE, "journal_exact_attachment")
    add(LeaseState.ATTACHED_UNVERIFIED, LeaseEvent.CLAIM_CONFIRMED, LeaseState.IDLE,
        BillingAction.KEEP_WITH_DEADLINE, "persist_deadlines_before_timer")
    add(LeaseState.ACQUIRING, LeaseEvent.WARM_SESSION_BOUND, LeaseState.WARM_IDLE,
        BillingAction.KEEP_WITH_DEADLINE, "persist_warm_session_deadlines")
    for state in WARM_STATES:
        stop(state, LeaseEvent.WARM_SESSION_EXPIRED)
    add(LeaseState.WARM_IDLE, LeaseEvent.JOB_ADMITTED, LeaseState.WARM_ACTIVE,
        BillingAction.KEEP_WITH_DEADLINE, "pause_idle_clock")
    for event in (LeaseEvent.JOB_SUCCEEDED, LeaseEvent.JOB_FAILED):
        add(LeaseState.WARM_ACTIVE, event, LeaseState.WARM_IDLE,
            BillingAction.KEEP_WITH_DEADLINE, "persist_deadlines_before_timer")
    add(LeaseState.CAPACITY_WAIT, LeaseEvent.CREATE_STARTED, LeaseState.CREATE_PENDING,
        BillingAction.AWAIT_PROVIDER_ENTRY, "write_ahead_create_attempt")
    stop(LeaseState.CAPACITY_WAIT, LeaseEvent.CAPACITY_EXHAUSTED)
    add(LeaseState.CAPACITY_WAIT, LeaseEvent.RECONCILIATION_UNKNOWN, LeaseState.CREATE_UNKNOWN,
        BillingAction.RECONCILE_THEN_RELEASE, "write_ahead_reconciliation")
    for state in UNKNOWN_CUSTODY:
        add(state, LeaseEvent.CREATE_NOT_SENT, LeaseState.RELEASED,
            BillingAction.NO_BILLABLE_RESOURCE, "record_no_post_sent")
        add(state, LeaseEvent.CAPACITY_REJECTED, LeaseState.CAPACITY_WAIT,
            BillingAction.NO_BILLABLE_RESOURCE, "record_definite_capacity_rejection")
        for event in (LeaseEvent.CREATE_UNKNOWN, LeaseEvent.RECONCILIATION_UNKNOWN):
            stop(state, event, reconcile=True)
        for event in (LeaseEvent.LATE_CREATE_SUCCEEDED, LeaseEvent.RECONCILIATION_FOUND,
                      LeaseEvent.CREATE_FAILED_EXACT):
            add(state, event, LeaseState.CLEANUP, BillingAction.RELEASE_EXACT_POD, "journal_exact_pod")
        add(state, LeaseEvent.RECONCILIATION_EMPTY, LeaseState.RELEASED,
            BillingAction.NO_BILLABLE_RESOURCE, "record_create_finished_and_absent")
        add(state, LeaseEvent.CREATE_SUCCEEDED, LeaseState.ACQUIRING,
            BillingAction.KEEP_WITH_DEADLINE, "journal_exact_pod")
    for state in UNKNOWN_CUSTODY | ATTACHMENT_STATES | {LeaseState.ACQUIRING, LeaseState.CLEANUP}:
        add(state, LeaseEvent.PROVIDER_ENTRY, state, BillingAction.AWAIT_PROVIDER_ENTRY)
    for state in ATTACHMENT_STATES | {LeaseState.ACQUIRING}:
        add(state, LeaseEvent.ACQUISITION_CONTINUE, state,
            BillingAction.AWAIT_PROVIDER_ENTRY, "write_ahead_observation_attempt")
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
    add(LeaseState.ACTIVE_READY, LeaseEvent.PREPARATION_INVALIDATED, LeaseState.ACTIVE_STALE,
        BillingAction.KEEP_WITH_DEADLINE, "checkpoint_stale_preparation_keep_active_owner")
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
    return MappingProxyType(table)


LEASE_TRANSITIONS = _build_table()
