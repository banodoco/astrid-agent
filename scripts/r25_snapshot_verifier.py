"""Fail-closed structural verifier for the R-25 Pinokio snapshot artifact.

This is a predicate helper, not an artifact downloader or an aggregate.  Its
``authenticated_context`` argument must be constructed only after the existing
trusted downloader/finalizer has independently selected the exact hosted run,
attempt, producer job, immutable artifact and archive bytes.  A producer
receipt, an artifact name, a hash declared by a producer, or a synthetic test
fixture never supplies that authority.

The helper verifies the closed R-25 request/action/receipt relation, including
the external artifact-ID join used when the request was made before GitHub had
an artifact ID.  It verifies that action and filesystem records are separately
retained and hashed.  It intentionally does *not* turn those opaque action
records into a release verdict: the existing M4-6 assertions still decide the
journey row.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Mapping

from . import pinokio_snapshot_collector as collector


REQUEST_SCHEMA = "astrid.r25.pinokio-snapshot-request.v1"
ACTION_SCHEMA = "astrid.r25.pinokio-action-snapshot.v1"
RECEIPT_SCHEMA = "astrid.r25.pinokio-snapshot-receipt.v1"
CONTEXT_SCHEMA = "astrid.r36.pinokio-snapshot-authenticated-context.v1"
RECEIPT_PATH = "snapshot-receipt.json"
REQUEST_PATH = "request.json"
REQUIRED_ACTIONS = collector.REQUIRED_ACTIONS
SUPPORTED_SLOTS = frozenset(
    {
        "pinokio-backend-macos",
        "pinokio-fully-local-macos",
        "pinokio-backend-windows",
        "pinokio-fully-local-windows",
    }
)
R25_AGENT_REF = "a6b68d5b5478326a4db1971966d33a2cfe61d091"
R25_PINOKIO_REF = "95aafb5a029c3f53cb2268d8596909eab0deb3c4"
R25_SOURCES = {
    "scripts/pinokio_snapshot_collector.py": "a94ef58088702aba984c920a7d718ee6b03713a52d93b41298ccdd958a230a99",
    "scripts/pinokio_snapshot_macos.py": "086faf864e821ef53e7502179cdd322a7b86b462e4403e0f7af6abf789b2db6d",
    "scripts/pinokio_snapshot_windows.py": "6ab995a1829ddfeaf78cf738985f9a03c77a22b1c88ef36c462a0f34b3a83611",
    "pinokio/providers/r25-snapshot-macos.cjs": "eceba872257c310f1ad638e475ee1954facef43866ee95f443170fda408631da",
    "pinokio/providers/r25-snapshot-windows.cjs": "2c3cbce7597f3440ded89c89eaf1b74f7eae6c1ba11e71e9d3d64fb2e70e67d8",
}
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_ORIGIN_FIELDS = {
    "repository_id", "repository_full_name", "workflow_id", "workflow_ref",
    "workflow_sha", "run_id", "run_attempt", "head_sha", "producer_job_id",
    "producer_job_name", "artifact_id", "artifact_name",
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _raw_sha(value: object) -> str:
    return hashlib.sha256(_canonical(value) + b"\n").hexdigest()


def _is_digest(value: object, pattern: re.Pattern[str] = _HEX64) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _is_positive(value: object) -> bool:
    return type(value) is int and value > 0


def _safe_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and str(path) == value


def _exact(value: object, fields: set[str], label: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{label}: object required")
        return {}
    missing, unknown = fields - set(value), set(value) - fields
    if missing:
        errors.append(f"{label}: missing fields {', '.join(sorted(missing))}")
    if unknown:
        errors.append(f"{label}: unknown fields {', '.join(sorted(unknown))}")
    return value


def _utc(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def _index(files_value: object, errors: list[str]) -> dict[str, dict[str, Any]]:
    if not isinstance(files_value, list):
        errors.append("files: authenticated index list is required")
        return {}
    result: dict[str, dict[str, Any]] = {}
    for number, row in enumerate(files_value):
        item = _exact(row, {"path", "sha256", "json"}, f"files[{number}]", errors)
        path = item.get("path")
        if not _safe_path(path):
            errors.append(f"files[{number}].path: safe artifact-relative path required")
            continue
        if path in result:
            errors.append(f"files[{number}].path: duplicate evidence path {path!r}")
            continue
        result[path] = item
        if not _is_digest(item.get("sha256")):
            errors.append(f"files[{number}].sha256: lowercase SHA-256 required")
        if not isinstance(item.get("json"), dict):
            errors.append(f"files[{number}].json: parsed JSON object required")
    return result


def _entry(index: Mapping[str, dict[str, Any]], path: str, errors: list[str]) -> dict[str, Any]:
    entry = index.get(path)
    if entry is None:
        errors.append(f"files: missing {path!r}")
        return {}
    value = entry.get("json")
    if not isinstance(value, dict):
        return {}
    if entry.get("sha256") != _raw_sha(value):
        errors.append(f"{path}: raw SHA-256 does not bind canonical producer bytes")
    return entry


def _origin(value: object, label: str, errors: list[str], *, artifact_required: bool) -> dict[str, Any]:
    result = _exact(value, _ORIGIN_FIELDS, label, errors)
    for key in ("repository_id", "workflow_id", "run_id", "run_attempt", "producer_job_id"):
        if not _is_positive(result.get(key)):
            errors.append(f"{label}.{key}: positive integer required")
    for key in ("repository_full_name", "workflow_ref", "producer_job_name"):
        if not isinstance(result.get(key), str) or not result[key].strip() or "\x00" in result[key]:
            errors.append(f"{label}.{key}: non-empty text required")
    if isinstance(result.get("repository_full_name"), str) and result["repository_full_name"].count("/") != 1:
        errors.append(f"{label}.repository_full_name: owner/name required")
    for key in ("workflow_sha", "head_sha"):
        if not _is_digest(result.get(key), _HEX40):
            errors.append(f"{label}.{key}: 40-hex SHA required")
    if artifact_required and not _is_positive(result.get("artifact_id")):
        errors.append(f"{label}.artifact_id: authenticated immutable artifact ID required")
    if not artifact_required and result.get("artifact_id") is not None and not _is_positive(result.get("artifact_id")):
        errors.append(f"{label}.artifact_id: positive integer or null required")
    if not isinstance(result.get("artifact_name"), str) or _SAFE_LABEL.fullmatch(result["artifact_name"]) is None:
        errors.append(f"{label}.artifact_name: safe artifact label required")
    return result


def _same(left: object, right: object) -> bool:
    try:
        return _canonical(left) == _canonical(right)
    except (TypeError, ValueError):
        return False


def _collector(value: object, label: str, errors: list[str]) -> dict[str, Any]:
    result = _exact(value, {"agent_ref", "pinokio_ref", "sources"}, label, errors)
    if result.get("agent_ref") != R25_AGENT_REF:
        errors.append(f"{label}.agent_ref: supplied R-25 Agent ref required")
    if result.get("pinokio_ref") != R25_PINOKIO_REF:
        errors.append(f"{label}.pinokio_ref: supplied R-25 Pinokio ref required")
    if not _same(result.get("sources"), R25_SOURCES):
        errors.append(f"{label}.sources: exact supplied collector source hashes required")
    return result


def _actions(value: object, label: str, errors: list[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(REQUIRED_ACTIONS):
        errors.append(f"{label}: exact six action bindings required")
        return []
    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    for number, expected in enumerate(REQUIRED_ACTIONS):
        row = _exact(value[number], {"action", "action_id", "action_sha256"}, f"{label}[{number}]", errors)
        if row.get("action") != expected:
            errors.append(f"{label}[{number}].action: expected {expected!r}")
        action_id = row.get("action_id")
        if not isinstance(action_id, str) or _SAFE_LABEL.fullmatch(action_id) is None:
            errors.append(f"{label}[{number}].action_id: safe unique label required")
        elif action_id in ids:
            errors.append(f"{label}[{number}].action_id: duplicate action ID")
        else:
            ids.add(action_id)
        if not _is_digest(row.get("action_sha256")):
            errors.append(f"{label}[{number}].action_sha256: SHA-256 required")
        result.append(row)
    return result


def _receipt_actions(value: object, expected: list[dict[str, Any]], errors: list[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != len(REQUIRED_ACTIONS):
        errors.append("receipt.actions: exact six finalized action rows required")
        return []
    result: list[dict[str, Any]] = []
    for number, action in enumerate(REQUIRED_ACTIONS):
        row = _exact(
            value[number],
            {"action", "binding", "index", "poll_interval_seconds", "max_poll_samples", "poll_samples", "status", "errors"},
            f"receipt.actions[{number}]",
            errors,
        )
        if row.get("action") != action or row.get("index") != number or row.get("binding") != (expected[number] if number < len(expected) else None):
            errors.append(f"receipt.actions[{number}]: action inventory differs from request")
        if (type(row.get("poll_interval_seconds")) not in {int, float}
                or isinstance(row.get("poll_interval_seconds"), bool)
                or not 0.01 <= row.get("poll_interval_seconds", 0) <= 30
                or not _is_positive(row.get("max_poll_samples"))
                or not _is_positive(row.get("poll_samples"))
                or row.get("status") != "observed"
                or row.get("errors") != []):
            errors.append(f"receipt.actions[{number}]: completed bounded poll/action state required")
        result.append(row)
    return result


def _platform(observation: object, slot: str, targets: object, label: str, errors: list[str]) -> None:
    if not isinstance(observation, dict):
        errors.append(f"{label}.observation: platform object required")
        return
    unsigned = dict(observation)
    digest = unsigned.pop("structural_sha256", None)
    if not _is_digest(digest) or digest != hashlib.sha256(_canonical(unsigned)).hexdigest():
        errors.append(f"{label}.observation.structural_sha256: structural digest differs")
    if slot.endswith("-macos"):
        required = {
            "capture_finished_monotonic_ns", "capture_started_monotonic_ns", "captured_at_utc",
            "collector_pid", "launchd", "limits", "processes", "schema", "source_profile",
            "structural_sha256",
        }
        _exact(observation, required, f"{label}.observation", errors)
        if observation.get("schema") != "astrid.r25.snapshot.macos.v1":
            errors.append(f"{label}.observation.schema: macOS snapshot schema required")
        if observation.get("source_profile") != "astrid.r25.macos-15.bash.ps-libproc-launchctl.v1":
            errors.append(f"{label}.observation.source_profile: approved macOS profile required")
        if not _utc(observation.get("captured_at_utc")) or not _is_positive(observation.get("collector_pid")):
            errors.append(f"{label}.observation: capture timestamp or collector PID differs")
        rows = observation.get("processes")
        if not isinstance(rows, list) or not rows:
            errors.append(f"{label}.observation.processes: non-empty process census required")
        elif any(not isinstance(row, dict) or set(row) != {"birth_text", "collection_state", "command_line", "executable_path", "parent_pid", "pid"} for row in rows):
            errors.append(f"{label}.observation.processes: macOS process rows differ")
        launchd = observation.get("launchd")
        wanted = targets.get("launchd_targets") if isinstance(targets, dict) else None
        if not isinstance(launchd, list) or [row.get("target") for row in launchd if isinstance(row, dict)] != wanted:
            errors.append(f"{label}.observation.launchd: requested service census differs")
        elif any(row.get("state") == "error" for row in launchd if isinstance(row, dict)):
            errors.append(f"{label}.observation.launchd: collection error cannot prove a service fact")
    else:
        required = {"schema", "source", "capture", "limits", "targets", "processes", "scheduled_tasks", "services", "structural_sha256"}
        _exact(observation, required, f"{label}.observation", errors)
        if observation.get("schema") != "astrid.r25.snapshot.windows.v1":
            errors.append(f"{label}.observation.schema: Windows snapshot schema required")
        source = observation.get("source")
        if not isinstance(source, dict) or source.get("profile") != "windows-cim-task-service-snapshot-v1" or source.get("continuity") != "bounded-snapshot":
            errors.append(f"{label}.observation.source: approved Windows profile required")
        target_value = observation.get("targets")
        expected_targets = ({"scheduled_task_paths": targets.get("scheduled_task_paths"), "service_names": targets.get("service_names")} if isinstance(targets, dict) else None)
        if not _same(target_value, expected_targets):
            errors.append(f"{label}.observation.targets: requested Windows target census differs")
        for key in ("scheduled_tasks", "services"):
            rows = observation.get(key)
            if not isinstance(rows, list) or any(isinstance(row, dict) and row.get("status") == "collection-error" for row in rows):
                errors.append(f"{label}.observation.{key}: explicit non-error service facts required")


def _context(value: object, candidate: Mapping[str, Any], slot: str, errors: list[str]) -> dict[str, Any]:
    fields = {"schema", "slot_id", "candidate", "origin", "artifact", "collector", "actions", "finalization", "action_evidence"}
    result = _exact(value, fields, "authenticated_context", errors)
    if result.get("schema") != CONTEXT_SCHEMA:
        errors.append("authenticated_context.schema: R-36 authenticated context schema required")
    if result.get("slot_id") != slot:
        errors.append("authenticated_context.slot_id: differs from selected slot")
    if not _same(result.get("candidate"), candidate):
        errors.append("authenticated_context.candidate: differs from selected tuple")
    origin = _origin(result.get("origin"), "authenticated_context.origin", errors, artifact_required=True)
    artifact = _exact(result.get("artifact"), {"id", "name", "archive_sha256", "archive_size_bytes"}, "authenticated_context.artifact", errors)
    if (not _is_positive(artifact.get("id"))
            or artifact.get("id") != origin.get("artifact_id")
            or artifact.get("name") != origin.get("artifact_name")):
        errors.append("authenticated_context.artifact: immutable ID/name differs from origin join")
    if not _is_digest(artifact.get("archive_sha256")) or not _is_positive(artifact.get("archive_size_bytes")):
        errors.append("authenticated_context.artifact: independently downloaded archive hash/size required")
    _collector(result.get("collector"), "authenticated_context.collector", errors)
    _actions(result.get("actions"), "authenticated_context.actions", errors)
    return result


def check(evidence: dict) -> tuple[bool, list[str]]:
    """Verify one R-25 binding supplied by the existing authenticated index.

    A true result proves this *binding predicate* only.  The caller must still
    run the normal M4-6 semantic/action/filesystem assertions before claiming a
    journey row or platform qualification.
    """
    errors: list[str] = []
    if not isinstance(evidence, dict):
        return False, ["evidence: authenticated index object required"]
    _exact(evidence, {"slot_id", "candidate", "authenticated_context", "files"}, "evidence", errors)
    slot = evidence.get("slot_id")
    if slot not in SUPPORTED_SLOTS:
        errors.append("slot_id: R-25 macOS/Windows Pinokio slot required")
        return False, errors
    candidate = evidence.get("candidate")
    if not isinstance(candidate, dict):
        errors.append("candidate: selected tuple object required")
        candidate = {}
    try:
        collector._validate_tuple(candidate)  # type: ignore[attr-defined]
    except (AttributeError, collector.SnapshotCollectorError, TypeError, ValueError) as exc:
        errors.append(f"candidate: tuple differs from CURRENT-TUPLE shape ({exc})")
    context = _context(evidence.get("authenticated_context"), candidate, slot, errors)
    index = _index(evidence.get("files"), errors)
    receipt_entry = _entry(index, RECEIPT_PATH, errors)
    request_entry = _entry(index, REQUEST_PATH, errors)
    receipt = receipt_entry.get("json") if isinstance(receipt_entry.get("json"), dict) else {}
    request = request_entry.get("json") if isinstance(request_entry.get("json"), dict) else {}
    receipt_fields = {"schema", "status", "qualification", "m4_6_acceptance", "binding_sha256", "slot_id", "tuple", "origin", "collector", "artifact_binding", "request_file", "required_actions", "actions", "files"}
    _exact(receipt, receipt_fields, "receipt", errors)
    if receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("status") != "collector-complete":
        errors.append("receipt: completed R-25 receipt required")
    if receipt.get("qualification") != "requires-r36-verifier" or receipt.get("m4_6_acceptance") is not False:
        errors.append("receipt: producer may not self-qualify M4-6")
    try:
        collector.validate_request(request)
    except (collector.SnapshotCollectorError, TypeError, ValueError) as exc:
        errors.append(f"request: invalid R-25 request ({exc})")
    if request.get("schema") != REQUEST_SCHEMA or request.get("slot_id") != slot:
        errors.append("request: schema or selected slot differs")
    if not _same(request.get("tuple"), candidate) or not _same(receipt.get("tuple"), candidate):
        errors.append("tuple: request/receipt differs from selected tuple")
    _collector(request.get("collector"), "request.collector", errors)
    _collector(receipt.get("collector"), "receipt.collector", errors)
    action_rows = _actions(request.get("actions"), "request.actions", errors)
    _receipt_actions(receipt.get("actions"), action_rows, errors)
    origin = _origin(request.get("origin"), "request.origin", errors, artifact_required=False)
    receipt_origin = _origin(receipt.get("origin"), "receipt.origin", errors, artifact_required=False)
    context_origin = _origin(context.get("origin"), "authenticated_context.origin", errors, artifact_required=True)
    if not _same(origin, receipt_origin):
        errors.append("origin: request and receipt differ")
    if not _same(context.get("collector"), request.get("collector")):
        errors.append("authenticated_context.collector: differs from producer request")
    if not _same(context.get("actions"), action_rows):
        errors.append("authenticated_context.actions: differs from producer request")
    for name in _ORIGIN_FIELDS - {"artifact_id"}:
        if origin.get(name) != context_origin.get(name):
            errors.append(f"origin.{name}: differs from independent authenticated join")
    if origin.get("artifact_id") is not None and origin.get("artifact_id") != context_origin.get("artifact_id"):
        errors.append("origin.artifact_id: producer and authenticated artifact join differ")
    artifact_binding = _exact(receipt.get("artifact_binding"), {"artifact_id", "artifact_name", "artifact_id_authority"}, "receipt.artifact_binding", errors)
    if artifact_binding.get("artifact_name") != context_origin.get("artifact_name") or artifact_binding.get("artifact_id") != origin.get("artifact_id"):
        errors.append("receipt.artifact_binding: artifact fields differ")
    expected_authority = "request" if origin.get("artifact_id") is not None else "r36-external-origin-join-required"
    if artifact_binding.get("artifact_id_authority") != expected_authority:
        errors.append("receipt.artifact_binding: external artifact-ID join state differs")
    binding_sha = hashlib.sha256(_canonical(request)).hexdigest() if request else ""
    if receipt.get("binding_sha256") != binding_sha:
        errors.append("receipt.binding_sha256: request binding differs")
    request_file = _exact(receipt.get("request_file"), {"path", "sha256", "size_bytes"}, "receipt.request_file", errors)
    if request_file.get("path") != REQUEST_PATH or request_file.get("sha256") != request_entry.get("sha256"):
        errors.append("receipt.request_file: indexed request binding differs")
    if request_file.get("sha256") != _raw_sha(request) or request_file.get("size_bytes") != len(_canonical(request) + b"\n"):
        errors.append("receipt.request_file: canonical request bytes differ")
    declared_files = receipt.get("files")
    if not isinstance(declared_files, list):
        errors.append("receipt.files: finalized observation inventory required")
        declared_files = []
    declared_paths: list[str] = []
    previous_monotonic = -1
    for number, row_value in enumerate(declared_files):
        row = _exact(row_value, {"path", "sha256", "size_bytes", "action", "phase", "sample"}, f"receipt.files[{number}]", errors)
        path = row.get("path")
        if not _safe_path(path):
            errors.append(f"receipt.files[{number}].path: safe path required")
            continue
        declared_paths.append(path)
        entry = _entry(index, path, errors)
        payload = entry.get("json") if isinstance(entry.get("json"), dict) else {}
        if row.get("sha256") != entry.get("sha256") or row.get("size_bytes") != len(_canonical(payload) + b"\n"):
            errors.append(f"receipt.files[{number}]: indexed raw bytes differ")
        expected_index = REQUIRED_ACTIONS.index(row["action"]) if row.get("action") in REQUIRED_ACTIONS else -1
        if row.get("phase") not in {"before", "during", "boundary"} or type(row.get("sample")) is not int or row["sample"] < 0:
            errors.append(f"receipt.files[{number}]: action phase/sample differs")
        expected_observation = {"schema", "binding_sha256", "slot_id", "tuple", "origin", "collector", "action", "action_binding", "action_index", "phase", "sample", "session_monotonic_ns", "captured_at_utc", "observation"}
        _exact(payload, expected_observation, f"{path}", errors)
        if payload.get("schema") != ACTION_SCHEMA or payload.get("binding_sha256") != binding_sha or payload.get("slot_id") != slot:
            errors.append(f"{path}: action snapshot binding differs")
        if not _same(payload.get("tuple"), candidate) or not _same(payload.get("origin"), origin) or not _same(payload.get("collector"), request.get("collector")):
            errors.append(f"{path}: tuple, origin, or collector differs")
        if payload.get("action") != row.get("action") or payload.get("phase") != row.get("phase") or payload.get("sample") != row.get("sample") or payload.get("action_index") != expected_index:
            errors.append(f"{path}: action declaration differs")
        if expected_index >= 0 and payload.get("action_binding") != action_rows[expected_index]:
            errors.append(f"{path}: action inventory binding differs")
        monotonic = payload.get("session_monotonic_ns")
        if type(monotonic) is not int or monotonic < previous_monotonic or not _utc(payload.get("captured_at_utc")):
            errors.append(f"{path}: capture ordering/timestamp differs")
        elif type(monotonic) is int:
            previous_monotonic = monotonic
        _platform(payload.get("observation"), slot, request.get("targets"), path, errors)
    if len(declared_paths) != len(set(declared_paths)):
        errors.append("receipt.files: duplicate observation path")
    expected_paths = {f"observations/{index:02d}-{action}-{phase}-{sample:04d}.json" for index, action in enumerate(REQUIRED_ACTIONS) for phase, sample in (("before", 0), ("during", 0), ("boundary", 0))}
    if set(declared_paths) != expected_paths:
        errors.append("receipt.files: exact before/during/boundary action inventory required")
    finalization = _exact(context.get("finalization"), {"path", "sha256", "request_path", "request_sha256", "actions", "files", "complete"}, "authenticated_context.finalization", errors)
    if finalization.get("path") != RECEIPT_PATH or finalization.get("sha256") != receipt_entry.get("sha256"):
        errors.append("authenticated_context.finalization: final receipt path/hash differs")
    if finalization.get("request_path") != REQUEST_PATH or finalization.get("request_sha256") != request_entry.get("sha256"):
        errors.append("authenticated_context.finalization: request path/hash differs")
    if finalization.get("complete") is not True or finalization.get("actions") != [row.get("action_id") for row in action_rows] or finalization.get("files") != [REQUEST_PATH, *declared_paths]:
        errors.append("authenticated_context.finalization: complete action/file closure differs")
    action_evidence = context.get("action_evidence")
    if not isinstance(action_evidence, list) or len(action_evidence) != len(REQUIRED_ACTIONS):
        errors.append("authenticated_context.action_evidence: exact six separate action/filesystem records required")
        action_evidence = []
    retained: set[str] = {REQUEST_PATH, RECEIPT_PATH, *declared_paths}
    for number, action in enumerate(REQUIRED_ACTIONS):
        row = _exact(action_evidence[number] if number < len(action_evidence) else None, {"action", "action_id", "action_sha256", "action_path", "filesystem_path", "filesystem_sha256"}, f"authenticated_context.action_evidence[{number}]", errors)
        expected = action_rows[number] if number < len(action_rows) else {}
        if row.get("action") != action or row.get("action_id") != expected.get("action_id") or row.get("action_sha256") != expected.get("action_sha256"):
            errors.append(f"authenticated_context.action_evidence[{number}]: action inventory differs")
        for path_name, digest_name in (("action_path", "action_sha256"), ("filesystem_path", "filesystem_sha256")):
            path, digest = row.get(path_name), row.get(digest_name)
            if not _safe_path(path) or path in retained:
                errors.append(f"authenticated_context.action_evidence[{number}].{path_name}: distinct safe retained path required")
                continue
            retained.add(path)
            entry = _entry(index, path, errors)
            if entry.get("sha256") != digest or not isinstance(entry.get("json"), dict):
                errors.append(f"authenticated_context.action_evidence[{number}].{path_name}: indexed separately attributable evidence differs")
    if set(index) != retained:
        errors.append("files: unexpected or missing producer/action/filesystem evidence")
    return not errors, errors
