#!/usr/bin/env python3
"""Durable, sealed-result coordinator for C-06 tuple batches.

This module is deliberately a control-plane boundary.  It records immutable
tuple/plan inputs and authenticated source identities, but it never dispatches
or executes a producer.  A deployment owner calls :class:`Coordinator` only
after its local-socket/peer-credential boundary has authenticated the caller.
It does not create or publish a GitHub Check Run: the persisted host verdict is
authoritative in this bounded implementation; C-06-PUBLISH is a follow-up.
"""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import uuid


VERSION = "c06-tuple-coordinator/v2"
TARGET_REPOSITORY = "banodoco/astrid-agent"
CHECK_NAME = "int/regression-green"

REGISTERED = "REGISTERED"
CHECK_OPEN = "CHECK_OPEN"
COLLECTING = "COLLECTING"
SEALED = "SEALED"
FINALIZE_READY = "FINALIZE_READY"
FINALIZING = "FINALIZING"
VERDICT_READY = "VERDICT_READY"
TERMINAL = "TERMINAL"
SUPERSEDED = "SUPERSEDED"

TERMINAL_OUTCOMES = frozenset((
    "completed", "infrastructure_failed", "unstartable", "skipped",
    "cancelled", "timed_out", "missing_at_deadline", "collection_failed",
))
FORCED_OUTCOMES = frozenset((
    "unstartable", "skipped", "cancelled", "timed_out", "missing_at_deadline",
    "infrastructure_failed", "collection_failed",
))

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WORKFLOW = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]+\.(?:yml|yaml)\Z")
_ARTIFACT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class CoordinatorError(ValueError):
    """A caller supplied a value that cannot cross the coordinator boundary."""


class StaleGeneration(CoordinatorError):
    """A late generation attempted to influence the active tuple batch."""


class DuplicateConflict(CoordinatorError):
    """A duplicate was not byte-for-byte identical to the prior event."""


def canonical_json(value):
    """Return the one JSON representation used for all durable identities."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _require(condition, message):
    if not condition:
        raise CoordinatorError(message)


def _utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _safe_id(value, label):
    _require(isinstance(value, str) and _ID.fullmatch(value), "invalid " + label)


def _sha256(value, label):
    _require(isinstance(value, str) and _SHA256.fullmatch(value), "invalid " + label)


def _sha40(value, label):
    _require(isinstance(value, str) and _SHA40.fullmatch(value), "invalid " + label)


def tuple_digest(tup):
    """Validate the minimal P-04 tuple identity required at this boundary."""
    _require(isinstance(tup, dict), "tuple must be an object")
    body = {key: value for key, value in tup.items() if key != "digest"}
    identity = digest(body)
    supplied = tup.get("digest")
    _require(supplied is None or supplied == identity, "tuple digest does not match contents")
    repos = tup.get("repos")
    _require(isinstance(repos, dict) and isinstance(repos.get("agent"), dict),
             "tuple is missing the Agent repository")
    for name, repo in repos.items():
        _require(isinstance(name, str) and isinstance(repo, dict), "invalid tuple repository")
        _sha40(repo.get("sha"), "tuple repository SHA")
    return identity


def _checkout_map(tup):
    return {name: entry["sha"] for name, entry in tup["repos"].items()}


def _validate_deadline(value):
    _require(isinstance(value, str), "producer deadline must be an ISO timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CoordinatorError("producer deadline must be an ISO timestamp") from exc
    _require(parsed.tzinfo is not None, "producer deadline needs a timezone")


def _validate_broker_identity(identity):
    _require(isinstance(identity, dict) and set(identity) == {"job_id", "job_yaml_sha256"},
             "invalid broker source identity")
    _safe_id(identity["job_id"], "broker job id")
    _sha256(identity["job_yaml_sha256"], "broker job.yaml digest")


def _validate_hosted_identity(identity):
    fields = {
        "repository", "workflow_path", "workflow_revision", "run_id", "run_attempt",
        "job_id", "artifact_id", "artifact_name", "head_sha", "dispatch_correlation",
    }
    _require(isinstance(identity, dict) and set(identity) == fields,
             "invalid hosted source identity")
    _require(isinstance(identity["repository"], str) and
             re.fullmatch(r"banodoco/astrid-[a-z0-9-]+", identity["repository"]),
             "invalid hosted repository")
    _require(isinstance(identity["workflow_path"], str) and
             _WORKFLOW.fullmatch(identity["workflow_path"]), "invalid hosted workflow path")
    _sha40(identity["workflow_revision"], "hosted workflow revision")
    _sha40(identity["head_sha"], "hosted head SHA")
    for field in ("run_id", "run_attempt", "job_id", "artifact_id"):
        _require(type(identity[field]) is int and identity[field] > 0,
                 "invalid hosted " + field)
    _require(isinstance(identity["artifact_name"], str) and
             _ARTIFACT.fullmatch(identity["artifact_name"]), "invalid hosted artifact name")
    _safe_id(identity["dispatch_correlation"], "hosted dispatch correlation")


def validate_source_identity(kind, identity):
    _require(kind in ("broker", "hosted"), "invalid producer kind")
    if kind == "broker":
        _validate_broker_identity(identity)
    else:
        _validate_hosted_identity(identity)


def source_key(kind, identity):
    """An unambiguous durable identity, unique across every active batch."""
    validate_source_identity(kind, identity)
    if kind == "broker":
        return "broker:" + identity["job_id"]
    return "hosted:%s:%s:%s:%s" % (
        identity["repository"], identity["run_id"], identity["run_attempt"], identity["job_id"])


def validate_producer_plan(plan, tup):
    """Validate and return a closed producer plan.

    Each row names one logical producer and the complete source identity it is
    allowed to bind.  Allowing no wildcards here keeps a later downloader or
    broker result from selecting a different source.
    """
    _require(isinstance(plan, list) and plan, "producer plan must be a nonempty list")
    expected_checkouts = _checkout_map(tup)
    seen = set()
    normalized = []
    fields = {"producer_id", "kind", "source", "checkout_shas", "deadline",
              "evidence_kind", "reviewed_identity"}
    for row in plan:
        _require(isinstance(row, dict) and set(row) == fields, "invalid producer plan row")
        producer_id, kind = row["producer_id"], row["kind"]
        _safe_id(producer_id, "producer id")
        _require(producer_id not in seen, "duplicate producer id")
        seen.add(producer_id)
        validate_source_identity(kind, row["source"])
        _require(row["checkout_shas"] == expected_checkouts,
                 "producer plan checkout map differs from frozen tuple")
        _validate_deadline(row["deadline"])
        _require(isinstance(row["evidence_kind"], str) and row["evidence_kind"].strip(),
                 "invalid producer evidence kind")
        _sha256(row["reviewed_identity"], "reviewed producer identity")
        if kind == "hosted":
            repository = row["source"]["repository"].split("/", 1)[1].removeprefix("astrid-")
            _require(repository in expected_checkouts and
                     row["source"]["head_sha"] == expected_checkouts[repository],
                     "hosted source does not bind a frozen tuple checkout")
        normalized.append(row)
    return normalized


def normalize_terminal_outcome(source_state, result=None):
    """Map watchdog/broker/API terminal facts to the contract's red outcomes.

    A test command that exits nonzero is still ``completed``: transport did its
    job and the evaluator decides whether the captured JUnit failure is
    admitted.  Death, interruption and missing terminal facts are never
    converted into a skip or a success.
    """
    _require(isinstance(source_state, str), "invalid source terminal state")
    state = source_state.lower().replace("_", "-")
    if state in ("done", "completed", "success"):
        return "completed"
    if state in ("worker-death", "worker-died", "dead", "interrupted", "failed",
                 "infrastructure-failed", "collection-failed"):
        return "infrastructure_failed" if state != "collection-failed" else "collection_failed"
    if state in ("cancelled", "canceled", "cancel-requested"):
        return "cancelled"
    if state in ("timed-out", "timeout"):
        return "timed_out"
    if state in ("unstartable", "dispatch-rejected"):
        return "unstartable"
    if state == "skipped":
        return "skipped"
    if state in ("missing", "missing-at-deadline", "not-started"):
        return "missing_at_deadline"
    raise CoordinatorError("unknown source terminal state")


class Coordinator:
    """SQLite-backed state machine used by the trusted host deployment adapter."""

    def __init__(self, database):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.database), isolation_level=None,
                                           check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def close(self):
        self._connection.close()

    def _create_schema(self):
        self._connection.executescript("""
        CREATE TABLE IF NOT EXISTS batches (
          batch_id TEXT PRIMARY KEY,
          generation INTEGER NOT NULL,
          target_repository TEXT NOT NULL,
          target_sha TEXT NOT NULL,
          tuple_digest TEXT NOT NULL,
          tuple_json TEXT NOT NULL,
          plan_json TEXT NOT NULL,
          state TEXT NOT NULL,
          check_identity_json TEXT,
          sealed_results_json TEXT,
          sealed_results_digest TEXT,
          invocation_id TEXT UNIQUE,
          finalizer_input_json TEXT,
          verdict_json TEXT,
          verdict_digest TEXT,
          terminal_reason TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(target_repository, target_sha, generation)
        );
        CREATE TABLE IF NOT EXISTS source_bindings (
          batch_id TEXT NOT NULL REFERENCES batches(batch_id),
          producer_id TEXT NOT NULL,
          kind TEXT NOT NULL,
          source_key TEXT NOT NULL UNIQUE,
          identity_json TEXT NOT NULL,
          identity_digest TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(batch_id, producer_id)
        );
        CREATE TABLE IF NOT EXISTS producer_results (
          batch_id TEXT NOT NULL REFERENCES batches(batch_id),
          producer_id TEXT NOT NULL,
          outcome TEXT NOT NULL,
          source_identity_digest TEXT NOT NULL,
          result_json TEXT NOT NULL,
          result_digest TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY(batch_id, producer_id)
        );
        CREATE TABLE IF NOT EXISTS requests (
          request_id TEXT PRIMARY KEY,
          payload_digest TEXT NOT NULL,
          response_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id TEXT NOT NULL REFERENCES batches(batch_id),
          event_type TEXT NOT NULL,
          event_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    @staticmethod
    def _row(connection, batch_id):
        row = connection.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if row is None:
            raise CoordinatorError("unknown batch")
        return row

    @staticmethod
    def _event(connection, batch_id, event_type, value):
        connection.execute(
            "INSERT INTO events(batch_id, event_type, event_json, created_at) VALUES (?, ?, ?, ?)",
            (batch_id, event_type, canonical_json(value), _utc_now()),
        )

    @staticmethod
    def _plan_by_id(row):
        return {item["producer_id"]: item for item in json.loads(row["plan_json"])}

    @staticmethod
    def _assert_active_generation(connection, row):
        active = connection.execute(
            "SELECT MAX(generation) AS generation FROM batches "
            "WHERE target_repository = ? AND target_sha = ?",
            (row["target_repository"], row["target_sha"]),
        ).fetchone()["generation"]
        if row["generation"] != active or row["state"] == SUPERSEDED:
            raise StaleGeneration("batch generation is superseded")

    def register_batch(self, *, batch_id, generation, target_repository, target_sha,
                       tup, producer_plan, request_id=None):
        """Persist one closed batch before any source can bind or start."""
        _safe_id(batch_id, "batch id")
        _require(type(generation) is int and generation > 0, "invalid generation")
        _require(target_repository == TARGET_REPOSITORY, "unexpected target repository")
        _sha40(target_sha, "target SHA")
        identity = tuple_digest(tup)
        _require(tup["repos"]["agent"]["sha"] == target_sha,
                 "target SHA does not equal tuple Agent SHA")
        plan = validate_producer_plan(producer_plan, tup)
        payload = {
            "version": VERSION, "batch_id": batch_id, "generation": generation,
            "target_repository": target_repository, "target_sha": target_sha,
            "tuple_digest": identity, "tuple": tup, "producer_plan": plan,
        }
        payload_digest = digest(payload)
        if request_id is not None:
            _safe_id(request_id, "request id")
        with self._transaction() as connection:
            if request_id is not None:
                prior = connection.execute("SELECT * FROM requests WHERE request_id = ?", (request_id,)).fetchone()
                if prior is not None:
                    if prior["payload_digest"] != payload_digest:
                        raise DuplicateConflict("request id was replayed with a different payload")
                    return json.loads(prior["response_json"])
            existing = connection.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
            if existing is not None:
                if (existing["generation"] == generation and existing["tuple_digest"] == identity and
                        existing["plan_json"] == canonical_json(plan)):
                    return self._batch_value(connection, existing)
                raise DuplicateConflict("batch id was reused with a different immutable input")
            maximum = connection.execute(
                "SELECT MAX(generation) AS generation FROM batches WHERE target_repository = ? AND target_sha = ?",
                (target_repository, target_sha),
            ).fetchone()["generation"]
            if maximum is not None and generation <= maximum:
                raise StaleGeneration("generation must be higher than every prior tuple batch")
            now = _utc_now()
            # A later generation cannot accept late source events from the old batch.
            connection.execute(
                "UPDATE batches SET state = ?, terminal_reason = ?, updated_at = ? "
                "WHERE target_repository = ? AND target_sha = ? AND generation < ? "
                "AND state NOT IN (?, ?, ?)",
                (SUPERSEDED, "superseded by a higher generation", now, target_repository, target_sha,
                 generation, TERMINAL, SUPERSEDED, VERDICT_READY),
            )
            connection.execute(
                "INSERT INTO batches(batch_id, generation, target_repository, target_sha, tuple_digest, "
                "tuple_json, plan_json, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (batch_id, generation, target_repository, target_sha, identity, canonical_json(tup),
                 canonical_json(plan), REGISTERED, now, now),
            )
            response = {"batch_id": batch_id, "generation": generation, "tuple_digest": identity,
                        "state": REGISTERED}
            self._event(connection, batch_id, "registered", response)
            if request_id is not None:
                connection.execute("INSERT INTO requests(request_id, payload_digest, response_json) VALUES (?, ?, ?)",
                                   (request_id, payload_digest, canonical_json(response)))
            return response

    def confirm_check_open(self, batch_id, check_identity):
        """Compatibility seam reserved for the out-of-scope C-06-PUBLISH adapter."""
        _require(isinstance(check_identity, dict) and set(check_identity) ==
                 {"check_run_id", "app_id", "external_id"}, "invalid verified check identity")
        _require(type(check_identity["check_run_id"]) is int and check_identity["check_run_id"] > 0 and
                 type(check_identity["app_id"]) is int and check_identity["app_id"] > 0,
                 "invalid verified check identity")
        _safe_id(check_identity["external_id"], "check external id")
        with self._transaction() as connection:
            row = self._row(connection, batch_id)
            self._assert_active_generation(connection, row)
            if row["state"] == CHECK_OPEN:
                if row["check_identity_json"] == canonical_json(check_identity):
                    return self._batch_value(connection, row)
                raise DuplicateConflict("conflicting duplicate check identity")
            _require(row["state"] == REGISTERED, "check can only open a registered batch")
            now = _utc_now()
            connection.execute("UPDATE batches SET state = ?, check_identity_json = ?, updated_at = ? WHERE batch_id = ?",
                               (CHECK_OPEN, canonical_json(check_identity), now, batch_id))
            self._event(connection, batch_id, "check_open", check_identity)
            return self._batch_value(connection, self._row(connection, batch_id))

    def open_host_verdict(self, batch_id, host_identity):
        """Open the local host-verdict boundary without a GitHub publisher.

        The supplied identity is only an audited local correlation token.  It
        is deliberately not an App/check-run identity and cannot be treated as
        required-check publication evidence.
        """
        _require(isinstance(host_identity, dict) and set(host_identity) == {"host_id", "external_id"},
                 "invalid host verdict identity")
        _safe_id(host_identity["host_id"], "host id")
        _safe_id(host_identity["external_id"], "host external id")
        with self._transaction() as connection:
            row = self._row(connection, batch_id)
            self._assert_active_generation(connection, row)
            if row["state"] == CHECK_OPEN:
                if row["check_identity_json"] == canonical_json(host_identity):
                    return self._batch_value(connection, row)
                raise DuplicateConflict("conflicting duplicate host verdict identity")
            _require(row["state"] == REGISTERED, "host verdict can only open a registered batch")
            connection.execute("UPDATE batches SET state = ?, check_identity_json = ?, updated_at = ? WHERE batch_id = ?",
                               (CHECK_OPEN, canonical_json(host_identity), _utc_now(), batch_id))
            self._event(connection, batch_id, "host_verdict_open", host_identity)
            return self._batch_value(connection, self._row(connection, batch_id))

    def bind_source(self, batch_id, producer_id, kind, identity):
        """Bind one logical producer exactly once to an authenticated source."""
        _safe_id(producer_id, "producer id")
        validate_source_identity(kind, identity)
        with self._transaction() as connection:
            row = self._row(connection, batch_id)
            self._assert_active_generation(connection, row)
            _require(row["state"] in (CHECK_OPEN, COLLECTING),
                     "source binding is forbidden before check open or after sealing")
            plan = self._plan_by_id(row)
            _require(producer_id in plan, "source is not in the closed producer plan")
            expected = plan[producer_id]
            _require(expected["kind"] == kind and expected["source"] == identity,
                     "source identity differs from the immutable producer plan")
            identity_json, identity_digest = canonical_json(identity), digest(identity)
            existing = connection.execute(
                "SELECT * FROM source_bindings WHERE batch_id = ? AND producer_id = ?",
                (batch_id, producer_id),
            ).fetchone()
            if existing is not None:
                if existing["kind"] == kind and existing["identity_json"] == identity_json:
                    return {"producer_id": producer_id, "identity_digest": identity_digest}
                raise DuplicateConflict("producer source was already bound differently")
            try:
                connection.execute(
                    "INSERT INTO source_bindings(batch_id, producer_id, kind, source_key, identity_json, "
                    "identity_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (batch_id, producer_id, kind, source_key(kind, identity), identity_json, identity_digest, _utc_now()),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateConflict("authenticated source is already bound to another batch") from exc
            connection.execute("UPDATE batches SET state = ?, updated_at = ? WHERE batch_id = ?",
                               (COLLECTING, _utc_now(), batch_id))
            self._event(connection, batch_id, "source_bound", {"producer_id": producer_id,
                                                                 "kind": kind, "identity": identity})
            return {"producer_id": producer_id, "identity_digest": identity_digest}

    def bind_broker_directory(self, batch_id, producer_id, broker_root, job_id):
        """Hash the authoritative job.yaml itself, not a producer-supplied digest."""
        _safe_id(job_id, "broker job id")
        root, directory = Path(broker_root).resolve(), (Path(broker_root) / "jobs" / job_id).resolve()
        _require(directory.parent == (root / "jobs").resolve(), "unsafe broker job path")
        job_file = directory / "job.yaml"
        _require(job_file.is_file(), "broker job.yaml is missing")
        identity = {"job_id": job_id, "job_yaml_sha256": hashlib.sha256(job_file.read_bytes()).hexdigest()}
        return self.bind_source(batch_id, producer_id, "broker", identity)

    def record_terminal(self, batch_id, producer_id, source_state, result=None, *, identity=None):
        """Record one source-authenticated terminal result and seal when complete."""
        _safe_id(producer_id, "producer id")
        outcome = normalize_terminal_outcome(source_state, result)
        if result is None:
            result = {}
        _require(isinstance(result, dict), "terminal result must be an object")
        with self._transaction() as connection:
            row = self._row(connection, batch_id)
            self._assert_active_generation(connection, row)
            plan = self._plan_by_id(row)
            _require(producer_id in plan, "terminal result is not in closed producer plan")
            binding = connection.execute(
                "SELECT * FROM source_bindings WHERE batch_id = ? AND producer_id = ?", (batch_id, producer_id)
            ).fetchone()
            _require(binding is not None, "terminal result has no authenticated source binding")
            _require(identity is not None and canonical_json(identity) == binding["identity_json"],
                     "terminal result source identity does not match binding")
            result_json, result_digest = canonical_json(result), digest(result)
            existing = connection.execute(
                "SELECT * FROM producer_results WHERE batch_id = ? AND producer_id = ?", (batch_id, producer_id)
            ).fetchone()
            if existing is not None:
                if (existing["outcome"] == outcome and existing["source_identity_digest"] == binding["identity_digest"] and
                        existing["result_json"] == result_json):
                    return self._batch_value(connection, row)
                raise DuplicateConflict("duplicate terminal event conflicts with sealed event")
            _require(row["state"] == COLLECTING, "terminal result is late or batch is not collecting")
            connection.execute(
                "INSERT INTO producer_results(batch_id, producer_id, outcome, source_identity_digest, result_json, "
                "result_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (batch_id, producer_id, outcome, binding["identity_digest"], result_json, result_digest, _utc_now()),
            )
            self._event(connection, batch_id, "producer_terminal", {"producer_id": producer_id,
                                                                        "outcome": outcome, "result": result})
            self._seal_if_complete(connection, row)
            return self._batch_value(connection, self._row(connection, batch_id))

    def force_terminal(self, batch_id, producer_id, outcome, reason):
        """Trusted cancellation/deadline path for a producer that never bound."""
        _safe_id(producer_id, "producer id")
        _require(outcome in FORCED_OUTCOMES, "invalid forced terminal outcome")
        _require(isinstance(reason, str) and reason, "forced terminal result needs a reason")
        with self._transaction() as connection:
            row = self._row(connection, batch_id)
            self._assert_active_generation(connection, row)
            plan = self._plan_by_id(row)
            _require(producer_id in plan, "forced terminal result is not in closed producer plan")
            existing = connection.execute(
                "SELECT * FROM producer_results WHERE batch_id = ? AND producer_id = ?", (batch_id, producer_id)
            ).fetchone()
            result = {"reason": reason, "forced": True}
            result_json = canonical_json(result)
            if existing is not None:
                if existing["outcome"] == outcome and existing["result_json"] == result_json:
                    return self._batch_value(connection, row)
                raise DuplicateConflict("forced terminal result conflicts with prior terminal result")
            _require(row["state"] in (CHECK_OPEN, COLLECTING), "forced terminal result is late")
            source_digest = digest(plan[producer_id]["source"])
            connection.execute(
                "INSERT INTO producer_results(batch_id, producer_id, outcome, source_identity_digest, result_json, "
                "result_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (batch_id, producer_id, outcome, source_digest, result_json, digest(result), _utc_now()),
            )
            connection.execute("UPDATE batches SET state = ?, updated_at = ? WHERE batch_id = ?",
                               (COLLECTING, _utc_now(), batch_id))
            self._event(connection, batch_id, "producer_forced_terminal", {"producer_id": producer_id,
                                                                               "outcome": outcome, "reason": reason})
            self._seal_if_complete(connection, self._row(connection, batch_id))
            return self._batch_value(connection, self._row(connection, batch_id))

    def cancel_batch(self, batch_id, reason="coordinator cancellation"):
        """Cancellation seals every remaining producer as red; it never drops one."""
        _require(isinstance(reason, str) and reason, "cancellation needs a reason")
        with self._transaction() as connection:
            row = self._row(connection, batch_id)
            self._assert_active_generation(connection, row)
            _require(row["state"] in (CHECK_OPEN, COLLECTING), "batch cannot be cancelled in current state")
            plan = self._plan_by_id(row)
            completed = {item["producer_id"] for item in connection.execute(
                "SELECT producer_id FROM producer_results WHERE batch_id = ?", (batch_id,)
            )}
            self._event(connection, batch_id, "cancel_requested", {"reason": reason})
            for producer_id, planned in plan.items():
                if producer_id in completed:
                    continue
                result = {"reason": reason, "forced": True}
                connection.execute(
                    "INSERT INTO producer_results(batch_id, producer_id, outcome, source_identity_digest, result_json, "
                    "result_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (batch_id, producer_id, "cancelled", digest(planned["source"]), canonical_json(result),
                     digest(result), _utc_now()),
                )
            connection.execute("UPDATE batches SET state = ?, updated_at = ? WHERE batch_id = ?",
                               (COLLECTING, _utc_now(), batch_id))
            self._seal_if_complete(connection, self._row(connection, batch_id))
            return self._batch_value(connection, self._row(connection, batch_id))

    def expire_batch(self, batch_id, reason="producer deadline expired"):
        """Map every remaining producer to missing_at_deadline and seal it red."""
        _require(isinstance(reason, str) and reason, "expiry needs a reason")
        with self._transaction() as connection:
            row = self._row(connection, batch_id)
            self._assert_active_generation(connection, row)
            _require(row["state"] in (CHECK_OPEN, COLLECTING), "batch cannot expire in current state")
            plan = self._plan_by_id(row)
            completed = {item["producer_id"] for item in connection.execute(
                "SELECT producer_id FROM producer_results WHERE batch_id = ?", (batch_id,)
            )}
            for producer_id, planned in plan.items():
                if producer_id in completed:
                    continue
                result = {"reason": reason, "forced": True}
                connection.execute(
                    "INSERT INTO producer_results(batch_id, producer_id, outcome, source_identity_digest, result_json, "
                    "result_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (batch_id, producer_id, "missing_at_deadline", digest(planned["source"]), canonical_json(result),
                     digest(result), _utc_now()),
                )
            self._event(connection, batch_id, "deadline_expired", {"reason": reason})
            connection.execute("UPDATE batches SET state = ?, updated_at = ? WHERE batch_id = ?",
                               (COLLECTING, _utc_now(), batch_id))
            self._seal_if_complete(connection, self._row(connection, batch_id))
            return self._batch_value(connection, self._row(connection, batch_id))

    def _seal_if_complete(self, connection, row):
        if row["state"] not in (COLLECTING, CHECK_OPEN):
            return
        plan = self._plan_by_id(row)
        records = connection.execute(
            "SELECT * FROM producer_results WHERE batch_id = ? ORDER BY producer_id", (row["batch_id"],)
        ).fetchall()
        if {record["producer_id"] for record in records} != set(plan):
            return
        results = [{
            "producer_id": record["producer_id"], "outcome": record["outcome"],
            "source_identity_digest": record["source_identity_digest"],
            "result": json.loads(record["result_json"]), "result_digest": record["result_digest"],
        } for record in records]
        sealed_digest = digest(results)
        invocation_id = uuid.uuid4().hex
        now = _utc_now()
        connection.execute(
            "UPDATE batches SET state = ?, sealed_results_json = ?, sealed_results_digest = ?, invocation_id = ?, "
            "updated_at = ? WHERE batch_id = ?",
            (SEALED, canonical_json(results), sealed_digest, invocation_id, now, row["batch_id"]),
        )
        self._event(connection, row["batch_id"], "sealed", {"sealed_results_digest": sealed_digest,
                                                                 "invocation_id": invocation_id})
        connection.execute("UPDATE batches SET state = ?, updated_at = ? WHERE batch_id = ?",
                           (FINALIZE_READY, _utc_now(), row["batch_id"]))
        self._event(connection, row["batch_id"], "finalize_ready", {"invocation_id": invocation_id})

    def begin_finalization(self, batch_id):
        """Return the one immutable finalizer input; never create a second invocation."""
        with self._transaction() as connection:
            row = self._row(connection, batch_id)
            self._assert_active_generation(connection, row)
            _require(row["state"] in (FINALIZE_READY, FINALIZING), "batch is not ready to finalize")
            if row["finalizer_input_json"] is not None:
                return json.loads(row["finalizer_input_json"])
            input_value = {
                "schema_version": "c06-sealed-finalizer-input/v1",
                "check": CHECK_NAME,
                "batch_id": row["batch_id"], "generation": row["generation"],
                "invocation_id": row["invocation_id"], "tuple_digest": row["tuple_digest"],
                "target_sha": row["target_sha"], "sealed_results_digest": row["sealed_results_digest"],
                "tuple": json.loads(row["tuple_json"]),
                "producer_results": json.loads(row["sealed_results_json"]),
            }
            input_value["sealed_input_digest"] = digest(input_value)
            connection.execute("UPDATE batches SET state = ?, finalizer_input_json = ?, updated_at = ? WHERE batch_id = ?",
                               (FINALIZING, canonical_json(input_value), _utc_now(), batch_id))
            self._event(connection, batch_id, "finalizer_started", {"invocation_id": row["invocation_id"],
                                                                        "sealed_input_digest": input_value["sealed_input_digest"]})
            return input_value

    def record_verdict(self, batch_id, status):
        """Accept one fully bound finalizer status, rejecting late/stale results."""
        _require(isinstance(status, dict), "finalizer status must be an object")
        fields = {"schema_version", "check", "batch_id", "generation", "invocation_id", "tuple_digest",
                  "target_sha", "sealed_input_digest", "result", "reasons", "evaluated_at"}
        _require(set(status) == fields, "invalid finalizer status fields")
        _require(status["schema_version"] == "c06-finalizer-status/v1" and status["check"] == CHECK_NAME and
                 status["result"] in ("pass", "fail") and isinstance(status["reasons"], list) and
                 all(isinstance(item, str) and item for item in status["reasons"]),
                 "invalid finalizer status")
        with self._transaction() as connection:
            row = self._row(connection, batch_id)
            self._assert_active_generation(connection, row)
            _require(row["state"] == FINALIZING, "finalizer status is late")
            expected = {
                "batch_id": row["batch_id"], "generation": row["generation"],
                "invocation_id": row["invocation_id"], "tuple_digest": row["tuple_digest"],
                "target_sha": row["target_sha"],
                "sealed_input_digest": json.loads(row["finalizer_input_json"])["sealed_input_digest"],
            }
            _require(all(status[key] == value for key, value in expected.items()),
                     "stale or wrong tuple finalizer status")
            bindings = [json.loads(item[0]) for item in connection.execute(
                "SELECT identity_json FROM source_bindings WHERE batch_id = ? ORDER BY producer_id", (batch_id,)
            )]
            results = json.loads(row["sealed_results_json"])
            host_status = dict(status)
            host_status.update({
                "host_verdict": True,
                "sealed_results_digest": row["sealed_results_digest"],
                "source_bindings_digest": digest(bindings),
                "artifact_index_digest": digest(results),
            })
            value, value_digest = canonical_json(host_status), digest(host_status)
            connection.execute("UPDATE batches SET state = ?, verdict_json = ?, verdict_digest = ?, updated_at = ? "
                               "WHERE batch_id = ?", (VERDICT_READY, value, value_digest, _utc_now(), batch_id))
            self._event(connection, batch_id, "verdict_ready", {"result": status["result"],
                                                                   "verdict_digest": value_digest})
            return self._batch_value(connection, self._row(connection, batch_id))

    def fail_finalization(self, batch_id, reason):
        """Create the terminal red verdict for cancellation, death or malformed output."""
        _require(isinstance(reason, str) and reason, "finalizer failure needs a reason")
        with self._transaction() as connection:
            row = self._row(connection, batch_id)
            self._assert_active_generation(connection, row)
            _require(row["state"] == FINALIZING, "finalizer failure is late")
            input_value = json.loads(row["finalizer_input_json"])
            bindings = [json.loads(item[0]) for item in connection.execute(
                "SELECT identity_json FROM source_bindings WHERE batch_id = ? ORDER BY producer_id", (batch_id,)
            )]
            results = json.loads(row["sealed_results_json"])
            status = {
                "schema_version": "c06-finalizer-status/v1", "check": CHECK_NAME,
                "batch_id": row["batch_id"], "generation": row["generation"],
                "invocation_id": row["invocation_id"], "tuple_digest": row["tuple_digest"],
                "target_sha": row["target_sha"],
                "sealed_input_digest": input_value["sealed_input_digest"],
                "result": "fail", "reasons": [reason], "evaluated_at": _utc_now(),
                "host_verdict": True, "sealed_results_digest": row["sealed_results_digest"],
                "source_bindings_digest": digest(bindings), "artifact_index_digest": digest(results),
            }
            connection.execute("UPDATE batches SET state = ?, verdict_json = ?, verdict_digest = ?, updated_at = ? "
                               "WHERE batch_id = ?", (VERDICT_READY, canonical_json(status), digest(status),
                                                       _utc_now(), batch_id))
            self._event(connection, batch_id, "finalizer_failed", {"reason": reason})
            return self._batch_value(connection, self._row(connection, batch_id))

    def recover_finalization(self, batch_id, invocation_directory, status_path, *, worker_dead):
        """Supervise an interrupted finalizer without ever replaying its body.

        The supervisor passes ``worker_dead`` only after its process watchdog
        has established that no live finalizer owns the invocation.  A complete
        bound status is adopted.  A dead worker after ``started.json`` becomes
        a terminal red host verdict; it is never started a second time.  Before
        a marker exists the returned sealed input may be started once.
        """
        _require(type(worker_dead) is bool, "worker_dead must be a boolean")
        with self._transaction() as connection:
            row = self._row(connection, batch_id)
            self._assert_active_generation(connection, row)
            _require(row["state"] == FINALIZING, "batch is not finalizing")
            sealed = json.loads(row["finalizer_input_json"])
        # Keep filesystem inspection outside the DB transaction.  No producer
        # input or plan is consulted on recovery.
        try:
            import regression_finalizer as finalizer
            status = finalizer.read_complete_status(status_path, sealed)
        except Exception:
            status = None
        if status is not None:
            return self.record_verdict(batch_id, status)
        marker = Path(invocation_directory) / "started.json"
        if marker.exists():
            if not worker_dead:
                return {"batch_id": batch_id, "state": FINALIZING, "recovery": "await_live_finalizer"}
            return self.fail_finalization(batch_id, "interrupted_finalization_after_start_marker")
        return {"batch_id": batch_id, "state": FINALIZING, "recovery": "safe_to_start_once", "sealed_input": sealed}

    def mark_terminal(self, batch_id, reason="publication verified"):
        """Called only by an external publisher adapter after exact read-back."""
        _require(isinstance(reason, str) and reason, "terminal transition needs a reason")
        with self._transaction() as connection:
            row = self._row(connection, batch_id)
            self._assert_active_generation(connection, row)
            _require(row["state"] == VERDICT_READY, "only a verified verdict can become terminal")
            connection.execute("UPDATE batches SET state = ?, terminal_reason = ?, updated_at = ? WHERE batch_id = ?",
                               (TERMINAL, reason, _utc_now(), batch_id))
            self._event(connection, batch_id, "terminal", {"reason": reason})
            return self._batch_value(connection, self._row(connection, batch_id))

    def _batch_value(self, connection, row):
        return {
            "batch_id": row["batch_id"], "generation": row["generation"], "state": row["state"],
            "tuple_digest": row["tuple_digest"], "target_sha": row["target_sha"],
            "invocation_id": row["invocation_id"],
            "sealed_results_digest": row["sealed_results_digest"],
            "verdict": json.loads(row["verdict_json"]) if row["verdict_json"] else None,
            "terminal_reason": row["terminal_reason"],
        }

    def batch(self, batch_id):
        with self._lock:
            return self._batch_value(self._connection, self._row(self._connection, batch_id))

    def events(self, batch_id):
        with self._lock:
            self._row(self._connection, batch_id)
            return [{"sequence": row["sequence"], "event_type": row["event_type"],
                     "value": json.loads(row["event_json"])} for row in self._connection.execute(
                "SELECT * FROM events WHERE batch_id = ? ORDER BY sequence", (batch_id,)
            )]
