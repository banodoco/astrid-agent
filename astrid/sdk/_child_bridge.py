"""Private, attempt-scoped SDK channel. Runtime authority stays in the host.

F05 installs an inherited socket before calling pack code. There is deliberately
no environment discovery, credential access, generic RPC, or admission fallback.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import stat
import threading
import time
from collections.abc import Mapping
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .exceptions import CapabilityInvocationError, CapabilityValidationError

if TYPE_CHECKING:
    from .results import InvocationResult, MaterializedChildOutput

_MAX_FRAME = 1024 * 1024
# Mirror the accepted B01 prototype schema; Runtime retains binding/authority.
_DISCOVERY_GRANT_CEILINGS = {
    "max_discovery_rows": 750,
    "max_discovery_metadata_bytes": 64 * 1024 * 1024,
    "max_selected_output_objects": 2,
    "max_selected_output_bytes": 16 * 1024 * 1024,
    "max_child_media_bindings": 1,
    "max_child_media_bytes": 16 * 1024 * 1024,
}
_bridge: ChildBridge | None = None
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z")
_OUTPUT_FIELDS = {"association_id", "run_id", "task_id", "attempt_id", "object_id",
                  "digest", "size", "filename", "media_type", "output_port", "ordinal"}

_PUBLICATION_OUTPUT_FIELDS = _OUTPUT_FIELDS | {"project_id", "generation_id", "group_key", "variant_key", "provenance"}
_PUBLICATION_TYPED_FIELDS = frozenset({"kind", "name", "digest", "object_id", "size", "filename",
    "media_type", "output_port", "ordinal", "group_key", "variant_key", "durability", "role", "is_primary"})


_BRIDGE_OPERATIONS = frozenset({"submit", "status", "outputs", "publish_snapshot", "materialize_output"})
_BRIDGE_PHASES = frozenset({"acquire", "send", "receive", "decode", "envelope", "data", "dispatch", "encode"})


def _bridge_io_failure_frame(*, endpoint: str, phase: str, operation: Any,
                            exception: BaseException | None, request_sequence: int,
                            frame_bytes: int, elapsed_seconds: float,
                            budget_seconds: float | None,
                            dispatch_elapsed_seconds: float | None = None) -> bytes | None:
    """Project only bounded fixed metadata, never exception text or identity."""
    try:
        exception_class = "unknown"
        for kind, label in ((TimeoutError, "TimeoutError"), (OSError, "OSError"),
                            (ValueError, "ValueError"), (TypeError, "TypeError")):
            if isinstance(exception, kind):
                exception_class = label
                break
        errno = getattr(exception, "errno", None) if isinstance(exception, OSError) else None

        def measured(value: Any) -> float | None:
            return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None

        row = {
            "diagnostic": "astrid.child_bridge_io_failure",
            "endpoint": endpoint if endpoint in ("sdk", "host") else "unknown",
            "phase": phase if phase in _BRIDGE_PHASES else "unknown",
            "operation": operation if type(operation) is str and operation in _BRIDGE_OPERATIONS else "unknown",
            "exception_class": exception_class,
            "errno": errno if type(errno) is int else None,
            "request_sequence": request_sequence if type(request_sequence) is int and request_sequence >= 0 else None,
            "frame_bytes": frame_bytes if type(frame_bytes) is int and frame_bytes >= 0 else None,
            "elapsed_seconds": measured(elapsed_seconds),
            "budget_seconds": measured(budget_seconds),
            "dispatch_elapsed_seconds": measured(dispatch_elapsed_seconds),
        }
        encoded = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        # 512 is the POSIX minimum PIPE_BUF. One atomic pipe row, or skip.
        return encoded if len(encoded) <= 512 else None
    except Exception:
        return None


def _write_bridge_io_failure(frame: bytes | None) -> None:
    """One best-effort write AFTER mandatory local bridge cleanup.

    Pipe backpressure must not hold cleanup. The brief O_NONBLOCK change is
    shared with concurrent writers; they may observe EAGAIN, and restoration
    can overwrite a concurrent flag change. No exclusive-FD claim is made.
    Only the blocking flag is restored; other descriptor flags are preserved.
    Regular-file writes occur after cleanup; filesystem latency is not bounded.
    Never close fd2, retry, create a sink or leave a diagnostic thread behind.
    """
    if type(frame) is not bytes or not frame or len(frame) > 512:
        return
    was_blocking = None
    try:
        mode = os.fstat(2).st_mode
        if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
            was_blocking = os.get_blocking(2)
            os.set_blocking(2, False)
            os.write(2, frame)
        elif stat.S_ISREG(mode):
            os.write(2, frame)
    except Exception:
        pass
    finally:
        if was_blocking is not None:
            try:
                os.set_blocking(2, was_blocking)
            except Exception:
                pass


def _emit_bridge_io_failure(**metadata: Any) -> None:
    _write_bridge_io_failure(_bridge_io_failure_frame(**metadata))


def _valid_id(value: Any) -> bool:
    return type(value) is str and _ID.fullmatch(value) is not None


def _relative_filename(value: Any) -> bool:
    return (type(value) is str and bool(value) and not PurePosixPath(value).is_absolute()
            and PurePosixPath(value) != PurePosixPath(".") and ".." not in value.split("/")
            and "\\" not in value and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate reply JSON field")
        value[key] = item
    return value


def _validate_output(value: Any, *, task_id: str, run_id: str | None = None,
                     attempt_id: str | None = None) -> dict[str, Any]:
    """Validate the exact public host projection, never authority or bytes."""
    if (type(value) is not dict or not _OUTPUT_FIELDS <= set(value) or set(value) - _OUTPUT_FIELDS - {"group_key", "variant_key", "producer"}
            or ("group_key" in value) != ("variant_key" in value)
            or any(not _valid_id(value[name]) for name in
                   ("association_id", "run_id", "task_id", "attempt_id"))
            or value["task_id"] != task_id
            or (run_id is not None and value["run_id"] != run_id)
            or (attempt_id is not None and value["attempt_id"] != attempt_id)
            or type(value["digest"]) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", value["digest"]) is None
            or type(value["object_id"]) is not str or value["object_id"] != value["digest"]
            or type(value["size"]) is not int or not 0 <= value["size"] <= 5 * 1024 ** 3
            or type(value["ordinal"]) is not int or value["ordinal"] < 0
            or any(type(value[k]) is not str or not value[k] for k in ("group_key", "variant_key") if k in value)
            or ("producer" in value and (type(value["producer"]) is not dict
                or not value["producer"] or set(value["producer"]) - {"output_port", "producer_output_id"}
                or any(type(v) is not str or not v or len(v) > 255 for v in value["producer"].values())))
            or not _relative_filename(value["filename"])
            or any(type(value[name]) is not str or not value[name]
                   or any(ord(char) < 32 or ord(char) == 127 for char in value[name])
                   for name in ("media_type", "output_port"))):
        raise ValueError("invalid child output descriptor")
    return dict(value)


def _validate_output_page(value: Any, *, task_id: str, run_id: str | None = None,
                          attempt_id: str | None = None) -> list[dict[str, Any]]:
    if type(value) is not list or len(value) > 256:
        raise ValueError("invalid child output page")
    rows = [_validate_output(row, task_id=task_id, run_id=run_id, attempt_id=attempt_id)
            for row in value]
    if len({row["association_id"] for row in rows}) != len(rows):
        raise ValueError("duplicate child output association")
    return rows


def _validate_publication_readback(value: Any, *, task_id: str, run_id: str,
                                   attempt_id: str,
                                   managed_outputs: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate the named-publication supplement; ordinary output ABI stays exact.

    The supplement is carried only by an authenticated staged host reply. It is
    receipt evidence, never an input binding or a materialization credential.
    """
    if (type(value) is not dict or set(value) != {"version", "task", "result", "managed_outputs"}
            or type(value["version"]) is not int or value["version"] != 1):
        raise ValueError("invalid publication readback version or shape")
    task, settled, rows = value["task"], value["result"], value["managed_outputs"]
    if (type(task) is not dict or set(task) != {"task_id", "run_id", "attempt_id", "state",
            "project_id", "generation_intent", "expected_effect", "result"}
            or (task["task_id"], task["run_id"], task["attempt_id"]) != (task_id, run_id, attempt_id)
            or task["state"] not in {"completed", "succeeded"}
            or not _valid_id(task["project_id"])
            or type(task["generation_intent"]) is not dict or type(task["expected_effect"]) is not dict
            or task["expected_effect"].get("effect_type") != "generation.publish_v1"
            or task["expected_effect"].get("target_id") != task["project_id"]
            or task["result"] != settled):
        raise ValueError("invalid publication task readback")
    if (type(settled) is not dict or set(settled) != {"payload", "outputs", "generation_publish_v1"}
            or type(settled["payload"]) is not dict or set(settled["payload"]) - {"action_result"}
            or type(settled["outputs"]) is not list or len(settled["outputs"]) > 256
            or any(type(row) is not dict or set(row) - _PUBLICATION_TYPED_FIELDS
                   for row in settled["outputs"])):
        raise ValueError("invalid publication settlement readback")
    applied = settled["generation_publish_v1"]
    if (type(applied) is not dict or set(applied) != {"effect_type", "publications"}
            or applied["effect_type"] != "generation.publish_v1"
            or type(applied["publications"]) is not list or len(applied["publications"]) > 32):
        raise ValueError("invalid publication effect readback")
    for publication in applied["publications"]:
        if (type(publication) is not dict
                or set(publication) != {"group_key", "generation_id", "variants", "missing_selectors"}
                or type(publication["variants"]) is not list or len(publication["variants"]) > 256
                or publication["missing_selectors"] != []):
            raise ValueError("invalid publication group readback")
        for variant in publication["variants"]:
            if (type(variant) is not dict or set(variant) != {"variant_id", "generation_id", "object_id",
                    "variant_key", "ordinal", "output_port"}):
                raise ValueError("invalid publication variant readback")
    if type(rows) is not list or len(rows) > 256:
        raise ValueError("invalid publication managed output readback")
    base = []
    for row in rows:
        if (type(row) is not dict or set(row) - {"producer"} != _PUBLICATION_OUTPUT_FIELDS
                or row["project_id"] != task["project_id"] or type(row["provenance"]) is not dict):
            raise ValueError("invalid publication managed output fields")
        provenance = row["provenance"]
        if set(provenance) - {"task_id", "attempt_id", "capability_id", "executor_id", "fence", "runtime_epoch", "delegated_publication"}:
            raise ValueError("unsupported publication provenance")
        delegated = provenance.get("delegated_publication")
        if delegated is not None:
            if (type(delegated) is not dict or set(delegated) - {"name", "parent_task_id", "parent_attempt_id",
                    "parent_fence", "runtime_epoch", "source_association_id", "source_task_id", "source_attempt_id",
                    "candidate", "verification", "raw", "raw_publication"}):
                raise ValueError("unsupported delegated publication provenance")
            for name in ("candidate", "verification", "raw"):
                if name in delegated and (type(delegated[name]) is not dict or set(delegated[name]) - {
                        "producer_task_id", "producer_attempt_id", "association_id", "object_id", "output_port"}):
                    raise ValueError("unsupported publication source provenance")
            raw = delegated.get("raw_publication")
            if raw is not None and (type(raw) is not dict or set(raw) != {"effect_type", "publications"}
                    or raw["effect_type"] != "generation.publish_v1" or type(raw["publications"]) is not list):
                raise ValueError("invalid raw publication provenance")
            # Validate raw lineage with the same finite publication group and
            # variant schema as the current canonical effect result.
            if raw is not None:
                for group in raw["publications"]:
                    if (type(group) is not dict or set(group) != {"group_key", "generation_id", "variants", "missing_selectors"}
                            or group["missing_selectors"] != [] or type(group["variants"]) is not list
                            or any(type(variant) is not dict or set(variant) != {"variant_id", "generation_id", "object_id",
                                "variant_key", "ordinal", "output_port"} for variant in group["variants"])):
                        raise ValueError("invalid raw publication provenance group")
        base.append({key: row[key] for key in _OUTPUT_FIELDS})
    if (_validate_output_page(base, task_id=task_id, run_id=run_id, attempt_id=attempt_id)
            != _validate_output_page([{key: row[key] for key in _OUTPUT_FIELDS} for row in managed_outputs], task_id=task_id, run_id=run_id, attempt_id=attempt_id)):
        raise ValueError("publication readback disagrees with verified managed outputs")
    return json.loads(_strict_json(value))


class _ChildOutputBinding:
    """Private snapshot of one result; copying public fields cannot mint it."""

    def __init__(self, result: InvocationResult, bridge: ChildBridge, child_key: str):
        run_id, task_id, attempt_id = result.kernel_run_id, result.kernel_task_id, result.kernel_attempt_id
        if (not isinstance(run_id, str) or not isinstance(task_id, str) or not isinstance(attempt_id, str)
                or not all(_valid_id(item) for item in (run_id, task_id, attempt_id, child_key))):
            raise ValueError("invalid child result identity")
        self._result = result
        self._bridge = bridge
        self._child_key = child_key
        self._identity = (run_id, task_id, attempt_id)
        self._outputs = _validate_output_page(
            result.outputs.get("managed_outputs"), task_id=task_id,
            run_id=run_id, attempt_id=attempt_id,
        )

    def materialize(self, result: InvocationResult, association_id: str) -> MaterializedChildOutput:
        try:
            current = _validate_output_page(result.outputs.get("managed_outputs"),
                                            task_id=self._identity[1], run_id=self._identity[0],
                                            attempt_id=self._identity[2])
        except ValueError as exc:
            raise CapabilityValidationError("child result contains invalid output descriptors") from exc
        if (result is not self._result or result.ok is not True
                or (result.kernel_run_id, result.kernel_task_id, result.kernel_attempt_id) != self._identity
                or current != self._outputs):
            raise CapabilityValidationError("child result no longer matches its verified output binding")
        if not _valid_id(association_id):
            raise CapabilityValidationError("association_id must be an exact child output ID")
        selected = [row for row in self._outputs if row["association_id"] == association_id]
        if len(selected) != 1:
            raise CapabilityValidationError("association_id must occur exactly once in this child result")
        return self._bridge.materialize_output(
            child_key=self._child_key, task_id=self._identity[1], output=selected[0],
        )


class _BridgeRejected(CapabilityInvocationError):
    def __init__(self, error: dict[str, Any]):
        super().__init__("child bridge rejected request", details=error)
        self.error = error


def _strict_json(value: Any) -> bytes:
    def check(item: Any, depth: int = 0) -> None:
        if depth > 32:
            raise ValueError("JSON nesting exceeds 32")
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for nested in item:
                check(nested, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for nested in item.values():
                check(nested, depth + 1)
            return
        raise ValueError("requires finite strict JSON")
    try:
        check(value)
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > _MAX_FRAME - 1024:
            raise ValueError("JSON exceeds bridge frame bound")
        return encoded
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise CapabilityValidationError(f"child bridge request {exc}") from exc


def _validate_child_policy(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise CapabilityValidationError("child_delegation must be an object")
    copied = dict(value)
    required = {"capabilities", "targets", "input_object_ids"}
    if not required <= copied.keys() or copied.keys() - required - {"limits", "stages", "final_publication", "publications", "recoverable_outputs", "discovery_grant"}:
        raise CapabilityValidationError("child_delegation requires a finite policy shape")
    for name, maximum, nonempty in (("capabilities", 32, True), ("targets", 32, True),
                                    ("input_object_ids", 256, False), ("stages", 32, True)):
        if name not in copied:
            continue
        items = copied[name]
        if type(items) is not list or len(items) > maximum or (nonempty and not items):
            raise CapabilityValidationError(f"child_delegation.{name} must be a bounded list")
    limits = copied.get("limits", {})
    if type(limits) is not dict or any(type(n) is not int or n <= 0 for n in limits.values()):
        raise CapabilityValidationError("child_delegation.limits must contain positive finite integers")
    if "publications" in copied:
        publications = copied["publications"]
        if ("final_publication" in copied or "stages" not in copied
                or type(publications) is not dict or set(publications) != {"raw", "final"}
                or type(publications["raw"]) is not dict
                or set(publications["raw"]) != {"stage", "output_port", "effect"}
                or type(publications["final"]) is not dict
                or set(publications["final"]) != {"stage", "output_port", "candidate_stage",
                    "candidate_output_port", "verify_stage", "verify_output_port", "effect"}):
            raise CapabilityValidationError("publications requires exact raw/final declarations and stages, without final_publication")
        # Runtime validates stage order, effect selectors, project authority and
        # actual publication lineage. This is only the finite transport envelope.
    if "discovery_grant" in copied:
        grant = copied["discovery_grant"]
        identities = {"project_id", "run_id", "task_id", "attempt_id", "capability_id"}
        if (type(grant) is not dict or set(grant) != identities | {"capability_digest", "limits"}
                or any(type(grant[key]) is not str or not grant[key] or "*" in grant[key]
                       for key in identities)
                or type(grant["capability_digest"]) is not str
                or re.fullmatch(r"sha256:[0-9a-f]{64}", grant["capability_digest"]) is None):
            raise CapabilityValidationError("discovery_grant requires one exact project/run/task/attempt and root capability")
        bounds = grant["limits"]
        if (type(bounds) is not dict or set(bounds) != _DISCOVERY_GRANT_CEILINGS.keys()
                or any(type(n) is not int or n < 1 or n > _DISCOVERY_GRANT_CEILINGS[key]
                       for key, n in bounds.items())):
            raise CapabilityValidationError("discovery_grant.limits requires every finite prototype bound")
        # Do not compare the root pin to permitted child capabilities or fetch
        # historical selections here. Runtime validates both exact bindings.
    if "recoverable_outputs" in copied:
        grants = copied["recoverable_outputs"]
        if type(grants) is not list or len(grants) != 1:
            raise CapabilityValidationError("recoverable_outputs requires one exact pinned Human Review grant")
        grant = grants[0]
        if (type(grant) is not dict
                or set(grant) != {"capability_id", "capability_digest", "output_ports"}
                or grant.get("capability_id") != "editorial.human_review"
                or grant.get("output_ports") != ["state_result"]
                or type(grant.get("capability_digest")) is not str
                or re.fullmatch(r"sha256:[0-9a-f]{64}", grant["capability_digest"]) is None
                or {key: grant[key] for key in ("capability_id", "capability_digest")} not in copied["capabilities"]):
            raise CapabilityValidationError("recoverable_outputs must pin editorial.human_review/state_result in capabilities")
    # Runtime resolves defaults, permitted names, numeric ceilings and policy authority.
    return json.loads(_strict_json(copied))


class _BridgeTimeout(CapabilityInvocationError):
    """An I/O budget expired, observed before cleanup or diagnostic work."""

    def __init__(self, message: str, *, observed_at: float):
        super().__init__(message)
        self.observed_at = observed_at


class _BridgeReplyTimeout(TimeoutError):
    """A complete validated reply arrived too late for this exchange."""

    def __init__(self, data: Any, *, observed_at: float):
        super().__init__("child bridge reply arrived after its deadline")
        self.data = data
        self.observed_at = observed_at


class ChildBridge:
    """Five fixed operations over an inherited private stream socket.

    A complete request/reply holds the lock for at most io_timeout_seconds.
    Waiting for task progress happens outside this lock. Any channel failure
    revokes this client permanently; outstanding calls cannot fall back.
    """

    def __init__(self, channel: socket.socket, *, io_timeout_seconds: float = 5.0):
        if not math.isfinite(io_timeout_seconds) or not 0 < io_timeout_seconds <= 5:
            raise ValueError("bridge I/O timeout must be in (0, 5]")
        self._channel = channel
        self._timeout = float(io_timeout_seconds)
        self._lock = threading.Lock()
        self._sequence = 0
        self._closed = False

    def close(self) -> None:
        self._closed = True
        try:
            self._channel.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._channel.close()

    def _exchange(self, op: str, body: dict[str, Any], *, timeout_seconds: float | None = None,
                  expected_output: dict[str, Any] | None = None) -> Any:
        budget = min(self._timeout, timeout_seconds) if timeout_seconds is not None else self._timeout
        started = time.monotonic()
        deadline = started + budget
        if budget <= 0 or not self._lock.acquire(timeout=budget):
            raise _BridgeTimeout("child bridge request timed out", observed_at=time.monotonic()) from TimeoutError(
                "child bridge lock acquisition timed out")
        phase = "send"
        frame = bytearray()
        try:
            if self._closed:
                raise CapabilityInvocationError("child bridge is closed")
            self._sequence += 1
            request = {"v": 1, "request_id": self._sequence, "op": op, **body}
            encoded = _strict_json(request) + b"\n"
            self._channel.settimeout(max(0.000001, deadline - time.monotonic()))
            self._channel.sendall(encoded)
            phase = "receive"
            while not frame.endswith(b"\n"):
                self._channel.settimeout(max(0.000001, deadline - time.monotonic()))
                chunk = self._channel.recv(min(65536, _MAX_FRAME + 1 - len(frame)))
                received_at = time.monotonic()
                if not chunk or len(frame) >= _MAX_FRAME:
                    raise ValueError("closed or oversized reply")
                frame.extend(chunk)
                if b"\n" in frame[:-1] or len(frame) > _MAX_FRAME:
                    raise ValueError("invalid frame boundary")
                if not frame.endswith(b"\n") and received_at >= deadline:
                    raise TimeoutError("child bridge reply timed out")
            phase = "decode"
            reply = json.loads(frame, object_pairs_hook=_unique_json_object)
            try:
                _strict_json(reply)
            except CapabilityValidationError as exc:
                raise ValueError("invalid reply JSON") from exc
            phase = "envelope"
            if (type(reply) is not dict or type(reply.get("v")) is not int or reply.get("v") != 1
                    or type(reply.get("request_id")) is not int
                    or reply.get("request_id") != self._sequence or type(reply.get("ok")) is not bool
                    or set(reply) != {"v", "request_id", "ok", "data" if reply["ok"] else "error"}):
                raise ValueError("invalid reply envelope")
            if not reply["ok"]:
                # Host errors carry only public SDK data, never authority/credentials.
                error = reply["error"]
                if type(error) is not dict or set(error) != {"code", "message", "details"}:
                    raise ValueError("invalid error envelope")
                if type(error["code"]) is not str or type(error["message"]) is not str or type(error["details"]) is not dict:
                    raise ValueError("invalid error fields")
                raise _BridgeRejected(error)
            phase = "data"
            data = reply["data"]
            if op == "outputs":
                data = _validate_output_page(data, task_id=body["task_id"])
            if op == "materialize_output":
                if (type(data) is not dict or set(data) != {"output", "filename"}
                        or not _relative_filename(data["filename"])
                        or _validate_output(data["output"], task_id=body["task_id"]) != expected_output):
                    raise ValueError("invalid materialized child output")
            if op == "publish_snapshot":
                if (type(data) is not dict
                        or set(data) != {"association_id", "receipt_id", "revision", "output_port",
                                         "digest", "size", "durability"}
                        or any(type(data[name]) is not str or not data[name]
                               for name in ("association_id", "receipt_id"))
                        or type(data["revision"]) is not int or data["revision"] != body["revision"]
                        or type(data["output_port"]) is not str or data["output_port"] != body["output_port"]
                        or type(data["digest"]) is not str
                        or re.fullmatch(r"sha256:[0-9a-f]{64}", data["digest"]) is None
                        or type(data["size"]) is not int or data["size"] < 0
                        or data["durability"] != "durable"):
                    raise ValueError("invalid durable snapshot receipt")
            if received_at >= deadline:
                # Validate complete replies before classifying lateness; a
                # caller must still reject malformed or foreign resources.
                raise _BridgeReplyTimeout(data, observed_at=received_at)
            return data
        except CapabilityValidationError:
            raise
        except CapabilityInvocationError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            observed_at = exc.observed_at if isinstance(exc, _BridgeReplyTimeout) else time.monotonic()
            self.close()
            _emit_bridge_io_failure(endpoint="sdk", phase=phase, operation=op,
                                    exception=exc, request_sequence=self._sequence,
                                    frame_bytes=len(frame), elapsed_seconds=time.monotonic() - started,
                                    budget_seconds=budget)
            if isinstance(exc, TimeoutError):
                raise _BridgeTimeout("child bridge is unavailable or returned an invalid reply",
                                     observed_at=observed_at) from exc
            raise CapabilityInvocationError("child bridge is unavailable or returned an invalid reply") from exc
        finally:
            self._lock.release()

    def submit(self, request: dict[str, Any]) -> Any:
        return self._exchange("submit", request)

    def status(self, child_key: str, task_id: str, *, timeout_seconds: float | None = None) -> Any:
        return self._exchange("status", {"child_key": child_key, "task_id": task_id}, timeout_seconds=timeout_seconds)

    def outputs(self, child_key: str, task_id: str, *, timeout_seconds: float | None = None) -> Any:
        return self._exchange("outputs", {"child_key": child_key, "task_id": task_id}, timeout_seconds=timeout_seconds)

    def materialize_output(self, *, child_key: str, task_id: str,
                           output: dict[str, Any]) -> MaterializedChildOutput:
        """Private exact-association operation; the host owns the destination."""
        from .results import MaterializedChildOutput

        if not _valid_id(child_key) or not _valid_id(task_id):
            raise CapabilityValidationError("materialization requires exact child key and task IDs")
        try:
            descriptor = _validate_output(output, task_id=task_id)
        except ValueError as exc:
            raise CapabilityValidationError(str(exc)) from exc
        data = self._exchange("materialize_output", {
            "child_key": child_key, "task_id": task_id, "association_id": descriptor["association_id"],
        }, expected_output=descriptor)
        return MaterializedChildOutput(output=MappingProxyType(dict(data["output"])), filename=data["filename"])

    def publish_snapshot(self, *, filename: str, output_port: str, revision: int) -> dict[str, Any]:
        """Request host-owned durable publication of an attempt-relative file."""
        if (type(filename) is not str or not filename or PurePosixPath(filename).is_absolute()
                or PurePosixPath(filename) == PurePosixPath(".") or ".." in filename.split("/")
                or "\\" in filename or "\x00" in filename):
            raise CapabilityValidationError("snapshot filename must be contained and attempt-relative")
        if type(output_port) is not str or not output_port:
            raise CapabilityValidationError("snapshot output_port must be a nonempty string")
        if type(revision) is not int or revision <= 0:
            raise CapabilityValidationError("snapshot revision must be a positive integer")
        return self._exchange("publish_snapshot", {
            "filename": filename, "output_port": output_port, "revision": revision,
        })


def _install_child_bridge(channel: socket.socket) -> ChildBridge:
    """Host bootstrap only; channel arrives via inherited launch controls."""
    global _bridge
    if _bridge is not None:
        raise CapabilityInvocationError("child bridge already installed")
    _bridge = ChildBridge(channel)
    return _bridge


def _child_request(capability: Any, inputs: dict[str, Any], *, digest: str,
                   child_key: str | None, wait: bool, timeout_seconds: float,
                   poll_seconds: float, stage: str | None = None,
                   input_refs: list[Mapping[str, Any]] | None = None,
                   generation_intent: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from astrid.core.execution.managed_inputs import managed_file_digest
    descriptors = []
    values = dict(inputs)
    for port in capability.inputs:
        if str(port.type).lower() != "file" or values.get(port.name) is None:
            continue
        value = values[port.name]
        if isinstance(value, Mapping) and "filename" in value and not ({"digest", "object_id"} & value.keys()):
            if (set(value) != {"filename", "media_type", "output_port"}
                    or any(type(item) is not str or not item for item in value.values())):
                raise CapabilityValidationError("producer file requires filename/media_type/output_port strings")
            filename = value["filename"]
            if (PurePosixPath(filename).is_absolute() or ".." in filename.split("/")
                    or "\\" in filename or "\x00" in filename or filename in {".", ""}):
                raise CapabilityValidationError("producer filename must be contained and attempt-relative")
            descriptor = {"name": port.name, "kind": "producer_file", **value}
        else:
            if isinstance(value, Mapping) and set(value) - {"object_id", "digest", "filename"}:
                raise CapabilityValidationError("object file binding contains unsupported fields")
            try:
                descriptor = {"name": port.name, "kind": "object", "object_id": managed_file_digest(value, port.name)}
            except ValueError as exc:
                raise CapabilityValidationError(str(exc)) from exc
        descriptors.append(descriptor)
    exact = {"child": {"capability_id": capability.id, "capability_digest": digest},
             "inputs": values, "input_descriptors": descriptors}
    if stage is not None or input_refs is not None:
        if not isinstance(stage, str) or type(input_refs) is not list:
            raise CapabilityValidationError("staged child requires stage and input_refs")
        exact.update(stage=stage, input_refs=input_refs)
    if generation_intent is not None:
        if stage is None or not isinstance(generation_intent, Mapping):
            raise CapabilityValidationError("generation intent requires a staged child")
        exact["generation_intent"] = dict(generation_intent)
    encoded = _strict_json(exact)
    if child_key is None:
        child_key = "sdk-child-" + hashlib.sha256(encoded).hexdigest()
    if type(child_key) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}", child_key):
        raise CapabilityValidationError("child_key must start alphanumeric and contain at most 128 ASCII key characters")
    if type(wait) is not bool:
        raise CapabilityValidationError("child wait must be boolean")
    for name, number in (("timeout_seconds", timeout_seconds), ("poll_seconds", poll_seconds)):
        if type(number) not in (int, float) or not math.isfinite(number) or number <= 0:
            raise CapabilityValidationError(f"child {name} must be positive and finite")
    return {**exact, "child_key": child_key, "wait": wait,
            "timeout_seconds": float(timeout_seconds), "poll_seconds": float(poll_seconds)}
