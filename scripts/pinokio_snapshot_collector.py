"""Run-bound Pinokio action snapshot sessions for macOS and Windows.

The collector is deliberately a producer, not a verdict checker.  It binds
every retained observation to one exact candidate tuple, workflow attempt,
producer job, slot, collector source set, action and artifact name.  R-36 owns
the independent artifact-origin join and qualification decision.

Platform modules perform the direct OS reads.  This module owns action order,
bounded in-action polling, exclusive receipt files, file hashing and closed-
world finalization.  A session cannot be resumed or rehydrated in another
process; an existing output directory is refused.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Final, Mapping

REQUEST_SCHEMA: Final = "astrid.r25.pinokio-snapshot-request.v1"
OBSERVATION_SCHEMA: Final = "astrid.r25.pinokio-action-snapshot.v1"
RECEIPT_SCHEMA: Final = "astrid.r25.pinokio-snapshot-receipt.v1"
REQUIRED_ACTIONS: Final = ("start", "ready", "stop", "update", "rollback", "uninstall")
SUPPORTED_SLOTS: Final = frozenset(
    {
        "pinokio-backend-macos",
        "pinokio-fully-local-macos",
        "pinokio-backend-windows",
        "pinokio-fully-local-windows",
    }
)
_HEX40: Final = re.compile(r"^[0-9a-f]{40}$")
_HEX64: Final = re.compile(r"^[0-9a-f]{64}$")
_SAFE_LABEL: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_TUPLE_FIELDS: Final = {
    "app",
    "agent",
    "runtime",
    "pinokio",
    "manifest_sha256",
    "artifacts",
    "published_at",
}
_ARTIFACT_FIELDS: Final = {"public/install", "public/install.sh", "public/install.ps1"}
_ORIGIN_FIELDS: Final = {
    "repository_id",
    "repository_full_name",
    "workflow_id",
    "workflow_ref",
    "workflow_sha",
    "run_id",
    "run_attempt",
    "head_sha",
    "producer_job_id",
    "producer_job_name",
    "artifact_id",
    "artifact_name",
}
_COLLECTOR_FIELDS: Final = {"agent_ref", "pinokio_ref", "sources"}
_MAX_OBSERVATION_BYTES: Final = 16 * 1024 * 1024


class SnapshotCollectorError(RuntimeError):
    """A missing, duplicate, malformed or unbound observation stopped collection."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise SnapshotCollectorError("value is not canonical JSON") from exc


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in rows:
        if key in result:
            raise SnapshotCollectorError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_request(path: Path) -> dict[str, Any]:
    """Load one bounded, absolute, non-symlink request with duplicate rejection."""
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise SnapshotCollectorError("request must be an absolute regular non-symlink file")
    try:
        size = path.stat().st_size
        if size < 2 or size > 1024 * 1024:
            raise SnapshotCollectorError("request size is outside the bounded range")
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda item: (_ for _ in ()).throw(
                SnapshotCollectorError(f"invalid JSON constant: {item}")
            ),
        )
    except SnapshotCollectorError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise SnapshotCollectorError("request is unreadable or invalid JSON") from exc
    if not isinstance(value, dict):
        raise SnapshotCollectorError("request must be a JSON object")
    validate_request(value)
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise SnapshotCollectorError(f"{label} must be a positive integer")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise SnapshotCollectorError(f"{label} must be a non-empty string")
    return value


def _digest(value: object, label: str, pattern: re.Pattern[str] = _HEX64) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise SnapshotCollectorError(f"{label} has an invalid immutable digest")
    return value


def _safe_label(value: object, label: str) -> str:
    value = _text(value, label)
    if _SAFE_LABEL.fullmatch(value) is None:
        raise SnapshotCollectorError(f"{label} is not a safe bounded label")
    return value


def _validate_tuple(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _TUPLE_FIELDS:
        raise SnapshotCollectorError("tuple fields differ from CURRENT-TUPLE.json")
    result = dict(value)
    for name in ("app", "agent", "runtime", "pinokio"):
        _digest(result.get(name), f"tuple.{name}", _HEX40)
    _digest(result.get("manifest_sha256"), "tuple.manifest_sha256")
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != _ARTIFACT_FIELDS:
        raise SnapshotCollectorError("tuple.artifacts fields differ")
    for name, digest in artifacts.items():
        _digest(digest, f"tuple.artifacts.{name}")
    published_at = _text(result.get("published_at"), "tuple.published_at")
    try:
        parsed = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotCollectorError("tuple.published_at is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise SnapshotCollectorError("tuple.published_at must include a timezone")
    return result


def _validate_origin(value: object, slot_id: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _ORIGIN_FIELDS:
        raise SnapshotCollectorError("origin fields differ")
    result = dict(value)
    for name in ("repository_id", "workflow_id", "run_id", "run_attempt", "producer_job_id"):
        _positive(result.get(name), f"origin.{name}")
    repository = _text(result.get("repository_full_name"), "origin.repository_full_name")
    if repository.count("/") != 1:
        raise SnapshotCollectorError("origin.repository_full_name must be owner/name")
    _text(result.get("workflow_ref"), "origin.workflow_ref")
    _digest(result.get("workflow_sha"), "origin.workflow_sha", _HEX40)
    _digest(result.get("head_sha"), "origin.head_sha", _HEX40)
    _text(result.get("producer_job_name"), "origin.producer_job_name")
    artifact_id = result.get("artifact_id")
    if artifact_id is not None:
        _positive(artifact_id, "origin.artifact_id")
    _safe_label(result.get("artifact_name"), "origin.artifact_name")
    if not result["artifact_name"].startswith(f"r25-snapshots-{slot_id}-"):
        raise SnapshotCollectorError("origin.artifact_name is not bound to the selected slot")
    return result


def _validate_collector(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _COLLECTOR_FIELDS:
        raise SnapshotCollectorError("collector fields differ")
    result = dict(value)
    _digest(result.get("agent_ref"), "collector.agent_ref", _HEX40)
    _digest(result.get("pinokio_ref"), "collector.pinokio_ref", _HEX40)
    sources = result.get("sources")
    if not isinstance(sources, Mapping) or not sources:
        raise SnapshotCollectorError("collector.sources must be a non-empty object")
    for name, digest in sources.items():
        if not isinstance(name, str) or not name or PurePosixPath(name).is_absolute():
            raise SnapshotCollectorError("collector source paths must be relative")
        parts = PurePosixPath(name)
        if ".." in parts.parts or str(parts) != name:
            raise SnapshotCollectorError("collector source paths must be normalized")
        _digest(digest, f"collector.sources.{name}")
    return result


def validate_request(value: object) -> dict[str, Any]:
    """Validate the exact request without treating it as external authority."""
    required = {"schema", "slot_id", "tuple", "origin", "collector", "targets"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise SnapshotCollectorError("request fields differ")
    result = dict(value)
    if result.get("schema") != REQUEST_SCHEMA:
        raise SnapshotCollectorError("request schema differs")
    slot_id = result.get("slot_id")
    if slot_id not in SUPPORTED_SLOTS:
        raise SnapshotCollectorError("request slot is not one of the four macOS/Windows slots")
    expected_os = str(slot_id).rsplit("-", 1)[-1]
    targets = result.get("targets")
    if not isinstance(targets, Mapping) or targets.get("os") != expected_os:
        raise SnapshotCollectorError("request targets do not match the slot OS")
    _validate_tuple(result.get("tuple"))
    _validate_origin(result.get("origin"), str(slot_id))
    _validate_collector(result.get("collector"))
    _canonical(targets)
    return result


def _output_directory(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink() or path.exists():
        raise SnapshotCollectorError("output must be an absent absolute non-symlink directory")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir() or parent.resolve(strict=True) != parent:
        raise SnapshotCollectorError("output parent must be an existing resolved directory")
    os.mkdir(path, 0o700)
    os.mkdir(path / "observations", 0o700)
    return path


def _write_exclusive(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def _observation_digest(value: Mapping[str, Any]) -> str:
    unsigned = dict(value)
    actual = unsigned.pop("structural_sha256", None)
    expected = _sha(_canonical(unsigned))
    if not isinstance(actual, str) or actual != expected:
        raise SnapshotCollectorError("platform observation structural digest differs")
    return actual


Capture = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class SnapshotSession:
    """One in-memory, closed-world action observation session."""

    def __init__(self, request: Mapping[str, Any], output: Path, *, capture: Capture) -> None:
        self.request = validate_request(request)
        self.output = _output_directory(output)
        self._capture_platform = capture
        self._binding_digest = _sha(_canonical(self.request))
        self._files: list[dict[str, Any]] = []
        self._actions: list[dict[str, Any]] = []
        self._active: _ActionContext | None = None
        self._closed = False
        self._lock = threading.RLock()
        _write_exclusive(self.output / "request.json", _canonical(self.request) + b"\n")

    @property
    def binding_digest(self) -> str:
        return self._binding_digest

    def action(
        self,
        name: str,
        *,
        poll_interval_seconds: float = 1.0,
        max_poll_samples: int = 900,
    ) -> "_ActionContext":
        """Observe one exact action before, during and at its final boundary."""
        if name not in REQUIRED_ACTIONS:
            raise SnapshotCollectorError("action is not in the fixed boundary inventory")
        if not 0.01 <= poll_interval_seconds <= 30:
            raise SnapshotCollectorError("poll interval must be in 0.01..30 seconds")
        if type(max_poll_samples) is not int or not 1 <= max_poll_samples <= 3600:
            raise SnapshotCollectorError("max poll samples must be in 1..3600")
        return _ActionContext(self, name, poll_interval_seconds, max_poll_samples)

    def _begin(self, context: "_ActionContext") -> None:
        with self._lock:
            if self._closed or self._active is not None:
                raise SnapshotCollectorError("snapshot session is closed or already observing")
            index = len(self._actions)
            expected = REQUIRED_ACTIONS[index] if index < len(REQUIRED_ACTIONS) else None
            if context.name != expected:
                raise SnapshotCollectorError(
                    f"action order differs: expected {expected!r}, received {context.name!r}"
                )
            self._active = context
            context.action_index = index

    def _capture(self, context: "_ActionContext", phase: str, sample: int) -> None:
        if phase not in {"before", "during", "boundary"}:
            raise SnapshotCollectorError("snapshot phase is invalid")
        try:
            platform_value = dict(self._capture_platform(self.request["targets"]))
        except SnapshotCollectorError:
            raise
        except Exception as exc:
            raise SnapshotCollectorError(
                f"platform snapshot failed closed: {type(exc).__name__}"
            ) from exc
        _observation_digest(platform_value)
        row = {
            "schema": OBSERVATION_SCHEMA,
            "binding_sha256": self._binding_digest,
            "slot_id": self.request["slot_id"],
            "tuple": self.request["tuple"],
            "origin": self.request["origin"],
            "collector": self.request["collector"],
            "action": context.name,
            "action_index": context.action_index,
            "phase": phase,
            "sample": sample,
            "session_monotonic_ns": time.monotonic_ns(),
            "captured_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "observation": platform_value,
        }
        payload = _canonical(row) + b"\n"
        if len(payload) > _MAX_OBSERVATION_BYTES:
            raise SnapshotCollectorError("snapshot receipt exceeds the bounded file size")
        filename = (
            f"{context.action_index:02d}-{context.name}-{phase}-{sample:04d}.json"
        )
        relative = f"observations/{filename}"
        path = self.output / relative
        _write_exclusive(path, payload)
        self._files.append(
            {
                "path": relative,
                "sha256": _sha(payload),
                "size_bytes": len(payload),
                "action": context.name,
                "phase": phase,
                "sample": sample,
            }
        )

    def _finish(self, context: "_ActionContext", action_error: BaseException | None) -> None:
        with self._lock:
            if self._active is not context:
                raise SnapshotCollectorError("action context is not the active session action")
            errors = list(context.errors)
            if action_error is not None:
                errors.append(f"action raised {type(action_error).__name__}")
            self._actions.append(
                {
                    "action": context.name,
                    "index": context.action_index,
                    "poll_interval_seconds": context.interval,
                    "max_poll_samples": context.max_samples,
                    "poll_samples": context.poll_samples,
                    "status": "observed" if not errors else "refused",
                    "errors": errors,
                }
            )
            self._active = None

    def finalize(self) -> dict[str, Any]:
        """Seal the exact six-action manifest or refuse missing/duplicate evidence."""
        with self._lock:
            if self._closed or self._active is not None:
                raise SnapshotCollectorError("snapshot session is closed or has an active action")
            observed = tuple(row["action"] for row in self._actions)
            if observed != REQUIRED_ACTIONS:
                raise SnapshotCollectorError("missing, duplicate, or out-of-order action observations")
            if any(row["status"] != "observed" for row in self._actions):
                raise SnapshotCollectorError("one or more action observations failed closed")
            paths = [row["path"] for row in self._files]
            if len(paths) != len(set(paths)):
                raise SnapshotCollectorError("duplicate snapshot receipt path")
            for action in REQUIRED_ACTIONS:
                phases = [row["phase"] for row in self._files if row["action"] == action]
                if phases.count("before") != 1 or phases.count("boundary") != 1:
                    raise SnapshotCollectorError(f"{action}: exact boundary observations are missing")
                if "during" not in phases:
                    raise SnapshotCollectorError(f"{action}: bounded in-action polling is missing")
            for row in self._files:
                path = self.output / row["path"]
                if path.is_symlink() or not path.is_file():
                    raise SnapshotCollectorError("snapshot receipt file is missing or substituted")
                payload = path.read_bytes()
                if len(payload) != row["size_bytes"] or _sha(payload) != row["sha256"]:
                    raise SnapshotCollectorError("snapshot receipt bytes differ before finalization")
                try:
                    decoded = json.loads(payload, object_pairs_hook=_pairs)
                except (ValueError, UnicodeError) as exc:
                    raise SnapshotCollectorError("snapshot receipt JSON differs before finalization") from exc
                if (
                    decoded.get("binding_sha256") != self._binding_digest
                    or decoded.get("action") != row["action"]
                    or decoded.get("phase") != row["phase"]
                    or decoded.get("sample") != row["sample"]
                ):
                    raise SnapshotCollectorError("snapshot receipt binding differs")
            origin = self.request["origin"]
            receipt = {
                "schema": RECEIPT_SCHEMA,
                "status": "collector-complete",
                "qualification": "requires-r36-verifier",
                "m4_6_acceptance": False,
                "binding_sha256": self._binding_digest,
                "slot_id": self.request["slot_id"],
                "tuple": self.request["tuple"],
                "origin": origin,
                "collector": self.request["collector"],
                "artifact_binding": {
                    "artifact_id": origin["artifact_id"],
                    "artifact_name": origin["artifact_name"],
                    "artifact_id_authority": (
                        "request" if origin["artifact_id"] is not None
                        else "r36-external-origin-join-required"
                    ),
                },
                "required_actions": list(REQUIRED_ACTIONS),
                "actions": list(self._actions),
                "files": list(self._files),
            }
            payload = _canonical(receipt) + b"\n"
            _write_exclusive(self.output / "snapshot-receipt.json", payload)
            self._closed = True
            return receipt


class _ActionContext(AbstractContextManager["_ActionContext"]):
    def __init__(
        self,
        session: SnapshotSession,
        name: str,
        interval: float,
        max_samples: int,
    ) -> None:
        self.session = session
        self.name = name
        self.interval = interval
        self.max_samples = max_samples
        self.action_index = -1
        self.poll_samples = 0
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_ActionContext":
        self.session._begin(self)
        try:
            self.session._capture(self, "before", 0)
            # An immediate in-action sample makes even a short action observable;
            # subsequent samples are interval bounded by ``max_samples``.
            self.session._capture(self, "during", 0)
            self.poll_samples = 1
        except Exception:
            self.session._active = None
            raise
        self._thread = threading.Thread(
            target=self._poll,
            name=f"r25-snapshot-{self.name}",
            daemon=True,
        )
        self._thread.start()
        return self

    def _poll(self) -> None:
        while not self._stop.wait(self.interval):
            if self.poll_samples >= self.max_samples:
                self.errors.append("bounded poll sample limit exceeded before action boundary")
                return
            try:
                self.session._capture(self, "during", self.poll_samples)
                self.poll_samples += 1
            except Exception as exc:  # retained as refusal; final boundary is still attempted
                self.errors.append(f"poll snapshot failed: {type(exc).__name__}: {exc}")
                return

    def __exit__(self, exc_type: object, exc: BaseException | None, traceback: object) -> bool:
        del exc_type, traceback
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval * 2))
            if self._thread.is_alive():
                self.errors.append("bounded polling thread did not stop")
        try:
            self.session._capture(self, "boundary", 0)
        except Exception as capture_error:
            self.errors.append(
                f"boundary snapshot failed: {type(capture_error).__name__}: {capture_error}"
            )
        self.session._finish(self, exc)
        return False


def platform_capture(request: Mapping[str, Any]) -> Capture:
    """Select the fixed platform implementation for a validated request."""
    validated = validate_request(request)
    slot = str(validated["slot_id"])
    if slot.endswith("-macos"):
        from scripts.pinokio_snapshot_macos import capture_snapshot

        return capture_snapshot
    if slot.endswith("-windows"):
        from scripts.pinokio_snapshot_windows import capture_snapshot

        return capture_snapshot
    raise SnapshotCollectorError("no fixed snapshot implementation for slot")


__all__ = [
    "OBSERVATION_SCHEMA",
    "RECEIPT_SCHEMA",
    "REQUEST_SCHEMA",
    "REQUIRED_ACTIONS",
    "SUPPORTED_SLOTS",
    "SnapshotCollectorError",
    "SnapshotSession",
    "load_request",
    "platform_capture",
    "validate_request",
]
