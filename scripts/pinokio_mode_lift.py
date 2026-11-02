"""Lift one authenticated Pinokio filesystem record into a journey receipt.

The Pinokio native collector emits one filesystem record for each action.  The
record intentionally carries a compact action origin, while its independently
authenticated artifact context carries the complete Actions origin.  This
module is the only bridge into ``journey-result.json``: it parses the installed
``install-mode.json`` receipt from the record, never derives a mode from a
declared slot, and keeps the record/context sidecars hash-bound.

The caller that supplies the authenticated context is the trusted evidence
finalizer.  This module verifies the structural and hash joins; it cannot turn
an arbitrary local file into authenticated hosted evidence on its own.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, MutableMapping


RECORD_SCHEMA = "astrid.r25.pinokio-filesystem-record.v1"
AUTHENTICATED_CONTEXT_SCHEMA = "astrid.r25.pinokio-authenticated-record-context.v1"
LIFT_SCHEMA = "astrid.r25.pinokio-mode-lift.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_RECORD_FIELDS = {
    "schema", "action", "action_id", "action_sha256", "slot_id", "tuple", "origin",
    "pinokio_install_mode_receipt", "before", "after", "engine", "status",
}
_CONTEXT_FIELDS = {"schema", "record", "slot_id", "tuple", "origin"}
_ORIGIN_FIELDS = {
    "repository_id", "repository_full_name", "workflow_id", "workflow_ref", "workflow_sha",
    "run_id", "run_attempt", "head_sha", "producer_job_id", "producer_job_name",
    "artifact_id", "artifact_name",
}
_ACTION_ORIGIN_FIELDS = {"run_id", "run_attempt", "producer_job_id"}


class ModeLiftError(ValueError):
    """A producer record cannot be safely lifted to a final journey result."""


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ModeLiftError(f"{label} must be a positive integer")
    return value


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_closed_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    if path.is_symlink() or not path.is_file():
        raise ModeLiftError(f"{label} must be a regular file")
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ModeLiftError(f"{label} exceeds the 1 MiB bound")

    def closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ModeLiftError(f"{label} has duplicate field {key!r}")
            value[key] = item
        return value

    try:
        value = json.loads(
            raw, object_pairs_hook=closed_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ModeLiftError(f"{label} has invalid JSON")),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ModeLiftError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ModeLiftError(f"{label} must contain an object")
    return value, _sha(raw)


def _relative(root: Path, path: Path, label: str) -> tuple[Path, str]:
    selected_root = root.resolve(strict=True)
    if path.is_symlink():
        raise ModeLiftError(f"{label} must not be a symlink")
    selected_path = path.resolve(strict=True)
    if not selected_path.is_relative_to(selected_root):
        raise ModeLiftError(f"{label} must be retained below the journey receipt directory")
    relative = selected_path.relative_to(selected_root).as_posix()
    if relative in {"", "."}:
        raise ModeLiftError(f"{label} has no safe relative path")
    return selected_path, relative


def _safe_sidecar(root: Path, value: Any, label: str) -> tuple[Path, str, str]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise ModeLiftError(f"{label} must contain only path and sha256")
    relative = value.get("path")
    expected_digest = value.get("sha256")
    if (not isinstance(relative, str) or not relative or Path(relative).is_absolute()
            or ".." in Path(relative).parts or not _SHA256.fullmatch(str(expected_digest))):
        raise ModeLiftError(f"{label} is unsafe or unhashed")
    path = root / relative
    selected, actual_relative = _relative(root, path, label)
    raw = selected.read_bytes() if selected.is_file() and not selected.is_symlink() else None
    if raw is None or _sha(raw) != expected_digest:
        raise ModeLiftError(f"{label} digest differs from the retained file")
    return selected, actual_relative, str(expected_digest)


def _validate_mode(value: Any, expected_mode: str) -> dict[str, Any]:
    if (not isinstance(value, Mapping) or set(value) != {"schema_version", "mode"}
            or type(value.get("schema_version")) is not int
            or value.get("schema_version") != 1
            or value.get("mode") not in {"backend", "local"}
            or value.get("mode") != expected_mode):
        raise ModeLiftError("installed Pinokio mode receipt is absent, malformed, or conflicts with the slot")
    return {"schema_version": value["schema_version"], "mode": value["mode"]}


def _validate_tuple(value: Any, candidate: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ModeLiftError("producer record tuple is absent or malformed")
    for name in ("app", "agent", "runtime", "pinokio"):
        if not isinstance(value.get(name), str) or not _GIT_SHA.fullmatch(value[name]):
            raise ModeLiftError(f"producer record tuple {name} is not a full commit SHA")
        if value[name] != candidate.get(name):
            raise ModeLiftError(f"producer record tuple {name} differs from the frozen candidate")
    return copy.deepcopy(dict(value))


def _validate_origin(value: Any, *, slot_id: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _ORIGIN_FIELDS:
        raise ModeLiftError("authenticated producer origin has missing, extra, or ambiguous fields")
    origin = dict(value)
    for name in ("repository_id", "workflow_id", "run_id", "run_attempt", "producer_job_id"):
        _positive_int(origin[name], f"origin.{name}")
    if origin["repository_full_name"] != "banodoco/astrid-pinokio":
        raise ModeLiftError("authenticated producer origin has the wrong repository")
    for name in ("workflow_ref", "producer_job_name"):
        if not isinstance(origin[name], str) or not origin[name].strip():
            raise ModeLiftError(f"origin.{name} is absent")
    for name in ("workflow_sha", "head_sha"):
        if not isinstance(origin[name], str) or not _GIT_SHA.fullmatch(origin[name]):
            raise ModeLiftError(f"origin.{name} is not a full commit SHA")
    if origin["artifact_id"] is not None:
        _positive_int(origin["artifact_id"], "origin.artifact_id")
    if (not isinstance(origin["artifact_name"], str)
            or not origin["artifact_name"].startswith(f"r25-snapshots-{slot_id}-")):
        raise ModeLiftError("authenticated producer artifact is not bound to the slot")
    return copy.deepcopy(origin)


def _validate_record(record: Any, *, expected_slot: str, expected_mode: str,
                     candidate: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not isinstance(record, Mapping) or set(record) != _RECORD_FIELDS:
        raise ModeLiftError("producer filesystem record has missing, extra, or ambiguous fields")
    if record.get("schema") != RECORD_SCHEMA:
        raise ModeLiftError("producer filesystem record schema differs")
    if record.get("slot_id") != expected_slot:
        raise ModeLiftError("producer filesystem record slot differs from the final journey slot")
    if record.get("status") != "completed":
        raise ModeLiftError("producer filesystem record did not complete")
    for name in ("action", "action_id"):
        if not isinstance(record.get(name), str) or not record[name].strip():
            raise ModeLiftError(f"producer filesystem record {name} is absent")
    if not isinstance(record.get("action_sha256"), str) or not _SHA256.fullmatch(record["action_sha256"]):
        raise ModeLiftError("producer filesystem record action digest is invalid")
    if not isinstance(record.get("before"), Mapping) or not isinstance(record.get("after"), Mapping):
        raise ModeLiftError("producer filesystem record filesystem observations are absent")
    if not isinstance(record.get("engine"), Mapping):
        raise ModeLiftError("producer filesystem record engine observation is absent")
    origin = record.get("origin")
    if not isinstance(origin, Mapping) or set(origin) != _ACTION_ORIGIN_FIELDS:
        raise ModeLiftError("producer filesystem record action origin is absent or ambiguous")
    for name in _ACTION_ORIGIN_FIELDS:
        _positive_int(origin[name], f"record.origin.{name}")
    return (
        _validate_mode(record.get("pinokio_install_mode_receipt"), expected_mode),
        _validate_tuple(record.get("tuple"), candidate),
        copy.deepcopy(dict(origin)),
    )


def _validate_context(context: Any, *, record_path: str, record_digest: str,
                      expected_slot: str, record_tuple: Mapping[str, Any],
                      record_origin: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(context, Mapping) or set(context) != _CONTEXT_FIELDS:
        raise ModeLiftError("authenticated record context has missing, extra, or ambiguous fields")
    if context.get("schema") != AUTHENTICATED_CONTEXT_SCHEMA:
        raise ModeLiftError("authenticated record context schema differs")
    if context.get("slot_id") != expected_slot:
        raise ModeLiftError("authenticated record context slot differs")
    record = context.get("record")
    if record != {"path": record_path, "sha256": record_digest}:
        raise ModeLiftError("authenticated record context does not bind the retained record bytes")
    if context.get("tuple") != record_tuple:
        raise ModeLiftError("authenticated record context tuple differs from the producer record")
    origin = _validate_origin(context.get("origin"), slot_id=expected_slot)
    for name in _ACTION_ORIGIN_FIELDS:
        if origin[name] != record_origin[name]:
            raise ModeLiftError("authenticated record context origin differs from the producer record")
    return origin


def lift(record_root: Path, result: MutableMapping[str, Any], *, record_path: Path,
         authenticated_context_path: Path, candidate: Mapping[str, Any], expected_slot: str,
         expected_mode: str) -> dict[str, Any]:
    """Attach a verified producer record to one final journey result in-place."""
    root = record_root.resolve(strict=True)
    selected_record, record_relative = _relative(root, record_path, "producer filesystem record")
    selected_context, context_relative = _relative(
        root, authenticated_context_path, "authenticated producer context")
    record, record_digest = _read_closed_json(selected_record, "producer filesystem record")
    context, context_digest = _read_closed_json(selected_context, "authenticated producer context")
    mode, tuple_value, action_origin = _validate_record(
        record, expected_slot=expected_slot, expected_mode=expected_mode, candidate=candidate)
    origin = _validate_context(
        context, record_path=record_relative, record_digest=record_digest,
        expected_slot=expected_slot, record_tuple=tuple_value, record_origin=action_origin)
    binding = {
        "schema": LIFT_SCHEMA,
        "record": {"path": record_relative, "sha256": record_digest},
        "authenticated_context": {"path": context_relative, "sha256": context_digest},
        "slot_id": expected_slot,
        "origin": origin,
        "tuple": tuple_value,
        "pinokio_install_mode_receipt": mode,
    }
    result["pinokio_install_mode_receipt"] = copy.deepcopy(mode)
    result["pinokio_install_mode_binding"] = binding
    return binding


def validate_lift(result: Mapping[str, Any], receipt_root: Path, *, candidate: Mapping[str, Any],
                  expected_slot: str, expected_mode: str) -> list[str]:
    """Return fail-closed diagnostics for a lifted final journey result."""
    try:
        if not isinstance(result, Mapping):
            raise ModeLiftError("final journey result is not an object")
        binding = result.get("pinokio_install_mode_binding")
        if not isinstance(binding, Mapping) or set(binding) != {
            "schema", "record", "authenticated_context", "slot_id", "origin", "tuple",
            "pinokio_install_mode_receipt",
        }:
            raise ModeLiftError("final journey result has no unambiguous Pinokio mode binding")
        if binding.get("schema") != LIFT_SCHEMA:
            raise ModeLiftError("final journey result Pinokio mode binding schema differs")
        if binding.get("slot_id") != expected_slot:
            raise ModeLiftError("final journey result Pinokio mode binding slot differs")
        root = receipt_root.resolve(strict=True)
        record_path, record_relative, record_digest = _safe_sidecar(root, binding.get("record"), "record sidecar")
        context_path, context_relative, context_digest = _safe_sidecar(
            root, binding.get("authenticated_context"), "authenticated context sidecar")
        record, actual_record_digest = _read_closed_json(record_path, "producer filesystem record")
        context, actual_context_digest = _read_closed_json(context_path, "authenticated producer context")
        if record_digest != actual_record_digest or context_digest != actual_context_digest:
            raise ModeLiftError("lift sidecar digest changed while validating")
        mode, tuple_value, action_origin = _validate_record(
            record, expected_slot=expected_slot, expected_mode=expected_mode, candidate=candidate)
        origin = _validate_context(
            context, record_path=record_relative, record_digest=record_digest,
            expected_slot=expected_slot, record_tuple=tuple_value, record_origin=action_origin)
        expected = {
            "slot_id": expected_slot,
            "origin": origin,
            "tuple": tuple_value,
            "pinokio_install_mode_receipt": mode,
        }
        if any(binding.get(name) != value for name, value in expected.items()):
            raise ModeLiftError("final journey result differs from the authenticated producer record")
        if result.get("pinokio_install_mode_receipt") != mode:
            raise ModeLiftError("final journey result installed mode was not lifted from the producer record")
    except (ModeLiftError, OSError, ValueError) as exc:
        return [str(exc)]
    return []
