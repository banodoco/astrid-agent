"""Strict, engine-neutral profile for managed generation result manifests.

The wire shape deliberately remains a universal ``manifest.json``: the
profile adds correlation, stable producer output identity, phase outcomes, and
opaque evidence namespaces without making ``GenerationResult`` or any engine
adapter part of the contract.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from astrid.core._shared.result_manifest import (
    ResultManifestError,
    ValidatedResultManifest,
    validate_result_manifest,
)

MANAGED_GENERATION_RESULT_KIND = "managed-generation-result.v1"
MANAGED_GENERATION_RESULT_SCHEMA_VERSION = 1
PHASE_NAMES = ("execution", "retrieval", "verification", "publication")
PHASE_STATUSES = frozenset(
    {"not_started", "pending", "succeeded", "failed", "cancelled", "interrupted"}
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MIME_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+\-]*/[a-z0-9][a-z0-9!#$&^_.+\-]*$"
)

_PROFILE_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "inputs",
        "outputs",
        "created",
        "warnings",
        "task_id",
        "attempt_id",
        "producer_run_id",
        "outcomes",
        "evidence",
    }
)
_OUTPUT_FIELDS = frozenset(
    {
        "producer_output_id",
        "output_port",
        "ordinal",
        "path",
        "media_type",
        "bytes",
        "sha256",
        "hash_check",
    }
)
_OUTCOME_FIELDS = frozenset({"status", "code", "message"})


class ManagedGenerationResultError(ResultManifestError):
    """Raised when a managed-generation result profile is invalid."""


@dataclass(frozen=True, slots=True)
class ManagedGenerationPhaseOutcome:
    """One lifecycle outcome in the execution-to-publication chain."""

    status: str
    code: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, str]:
        result = {"status": self.status}
        if self.code is not None:
            result["code"] = self.code
        if self.message is not None:
            result["message"] = self.message
        return result


@dataclass(frozen=True, slots=True)
class ManagedGenerationOutput:
    """A custody-checked output and its optional native hash comparison."""

    producer_output_id: str
    output_port: str
    ordinal: int
    path: str
    media_type: str
    bytes: int
    sha256: str
    hash_check: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "producer_output_id": self.producer_output_id,
            "output_port": self.output_port,
            "ordinal": self.ordinal,
            "path": self.path,
            "media_type": self.media_type,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }
        if self.hash_check is not None:
            result["hash_check"] = dict(self.hash_check)
        return result


@dataclass(frozen=True, slots=True)
class ManagedGenerationResult:
    """Validated ``managed-generation-result.v1`` wire data.

    ``staging_root`` is validation context, not serialized contract state.
    ``GenerationResult`` remains a separate SDK projection.
    """

    task_id: str
    attempt_id: str
    producer_run_id: str
    created: str
    inputs: Mapping[str, Any]
    outputs: tuple[ManagedGenerationOutput, ...]
    outcomes: Mapping[str, ManagedGenerationPhaseOutcome]
    evidence: Mapping[str, Mapping[str, Any]]
    warnings: tuple[str, ...] = ()
    staging_root: Path | None = None
    schema_version: int = MANAGED_GENERATION_RESULT_SCHEMA_VERSION
    kind: str = MANAGED_GENERATION_RESULT_KIND

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON-shaped profile, excluding validation context."""
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "inputs": dict(self.inputs),
            "outputs": [output.to_dict() for output in self.outputs],
            "created": self.created,
            "warnings": list(self.warnings),
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "producer_run_id": self.producer_run_id,
            "outcomes": {
                phase: self.outcomes[phase].to_dict() for phase in PHASE_NAMES
            },
            "evidence": {
                namespace: dict(self.evidence[namespace])
                for namespace in ("producer", "transport")
            },
        }

    def to_json(self) -> str:
        """Serialize using canonical sorted-key, compact JSON."""
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        staging_root: str | Path,
        expected_task_id: str | None = None,
        expected_attempt_id: str | None = None,
    ) -> "ManagedGenerationResult":
        return validate_managed_generation_result(
            payload,
            staging_root=staging_root,
            expected_task_id=expected_task_id,
            expected_attempt_id=expected_attempt_id,
        )

    @classmethod
    def from_json(
        cls,
        raw: str,
        *,
        staging_root: str | Path,
        expected_task_id: str | None = None,
        expected_attempt_id: str | None = None,
    ) -> "ManagedGenerationResult":
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ManagedGenerationResultError(
                f"managed-generation result is not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ManagedGenerationResultError(
                "managed-generation result JSON must be an object"
            )
        return cls.from_dict(
            payload,
            staging_root=staging_root,
            expected_task_id=expected_task_id,
            expected_attempt_id=expected_attempt_id,
        )


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManagedGenerationResultError(f"{field} must be an object")
    return value


def _require_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ManagedGenerationResultError(
            f"{field} must be a non-empty stable identifier"
        )
    return value


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManagedGenerationResultError(f"{field} must be a non-empty string")
    return value


def _validate_outcomes(
    raw: Any, *, output_count: int
) -> dict[str, ManagedGenerationPhaseOutcome]:
    outcomes = _require_mapping(raw, "outcomes")
    if set(outcomes) != set(PHASE_NAMES):
        raise ManagedGenerationResultError(
            "outcomes must contain exactly execution, retrieval, verification, publication"
        )

    normalized: dict[str, ManagedGenerationPhaseOutcome] = {}
    for phase in PHASE_NAMES:
        item = _require_mapping(outcomes[phase], f"outcomes.{phase}")
        unknown = sorted(set(item) - _OUTCOME_FIELDS)
        if unknown:
            raise ManagedGenerationResultError(
                f"outcomes.{phase} contains unknown fields: {', '.join(unknown)}"
            )
        status = item.get("status")
        if not isinstance(status, str) or status not in PHASE_STATUSES:
            raise ManagedGenerationResultError(
                f"outcomes.{phase}.status must be one of {sorted(PHASE_STATUSES)}"
            )
        code = item.get("code")
        message = item.get("message")
        if code is not None:
            _require_text(code, f"outcomes.{phase}.code")
        if message is not None:
            _require_text(message, f"outcomes.{phase}.message")
        normalized[phase] = ManagedGenerationPhaseOutcome(status, code, message)

    execution = normalized["execution"].status
    retrieval = normalized["retrieval"].status
    verification = normalized["verification"].status
    publication = normalized["publication"].status
    if execution in {"not_started", "pending"}:
        raise ManagedGenerationResultError(
            "execution outcome must be terminal (succeeded, failed, cancelled, or interrupted)"
        )
    if execution == "succeeded" and output_count == 0:
        raise ManagedGenerationResultError(
            "successful execution requires at least one output"
        )
    if execution != "succeeded" and publication != "not_started":
        raise ManagedGenerationResultError(
            "partial outputs or execution failure require no publication"
        )
    if (retrieval != "succeeded" and verification != "not_started"
            and not (execution != "succeeded"
                     and ((retrieval == "failed" and verification == "failed")
                          or (output_count and retrieval in {"failed", "not_started"}
                              and verification in {"failed", "succeeded"})))):
        raise ManagedGenerationResultError(
            "verification must be not_started until retrieval succeeds"
        )
    if verification != "succeeded" and publication != "not_started":
        raise ManagedGenerationResultError(
            "publication must be not_started until verification succeeds"
        )
    if publication == "succeeded" and verification != "succeeded":
        raise ManagedGenerationResultError(
            "publication succeeded without successful verification"
        )
    return normalized


def validate_evidence_reference(
    value: Any, *, field: str, staging_root: str | Path,
) -> dict[str, Any]:
    """Validate a portable CAS ref and, when supplied, its attempt-local bytes.

    A local ``path`` is staging context only; publishers remove it after copying
    the verified bytes. An object ref without a path is checked by Runtime in
    the task's project during the fenced publication transaction.
    """
    reference = dict(_require_mapping(value, field))
    digest = reference.get("sha256")
    if digest is None:
        identity = reference.get("digest", reference.get("object_id"))
        if isinstance(identity, str):
            digest = identity.removeprefix("sha256:")
            reference["sha256"] = digest
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise ManagedGenerationResultError(f"{field}.sha256 must be 64 lowercase hexadecimal characters")
    size = reference.get("bytes")
    if type(size) is not int or size < 0:
        raise ManagedGenerationResultError(f"{field}.bytes must be a non-negative integer")
    filename = _require_text(reference.get("filename"), f"{field}.filename")
    if Path(filename).name != filename or filename in {".", ".."} or "\\" in filename:
        raise ManagedGenerationResultError(f"{field}.filename must be a basename")
    for key in ("object_id", "digest"):
        if key in reference and reference[key] not in (digest, f"sha256:{digest}"):
            raise ManagedGenerationResultError(f"{field}.{key} disagrees with sha256")
    if "size" in reference and (type(reference["size"]) is not int or reference["size"] != size):
        raise ManagedGenerationResultError(f"{field}.size disagrees with bytes")
    if "path" in reference:
        path = reference["path"]
        if (not isinstance(path, str) or not path or Path(path).is_absolute()
                or ".." in Path(path).parts):
            raise ManagedGenerationResultError(f"{field}.path must be a contained relative path")
        try:
            validate_result_manifest({
                "schema_version": 1, "kind": "managed-evidence", "created": "staged",
                "inputs": {}, "warnings": [], "outputs": [{
                    "path": path, "ordinal": 0, "content_hash": f"sha256:{digest}",
                    "bytes": size,
                }],
            }, staging_root=staging_root)
        except ResultManifestError as exc:
            raise ManagedGenerationResultError(f"{field}: {exc}") from exc
    return reference


def redact_evidence(value: Any) -> Any:
    """Remove secrets from observations while retaining public correlation."""
    if isinstance(value, Mapping):
        return {key: redact_evidence(item) for key, item in value.items()
                if key in {"credential_actor", "token_id"} or not re.search(
                    r"password|secret|token|authorization|api[_-]?key|credential",
                    key, re.I,
                )}
    if isinstance(value, list):
        return [redact_evidence(item) for item in value]
    if isinstance(value, str):
        def endpoint(match: re.Match[str]) -> str:
            parsed = urlsplit(match.group())
            return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))
        return re.sub(r"https?://[^\s\"<>]+", endpoint, value)
    return value


def validate_workflow_hook_evidence(value: Any) -> dict[str, Any]:
    """Validate the bounded, path-free H3 reference-view evidence projection."""
    field_sets = {
        "evidence": {"reference_view"},
        "view": {"references", "target_frame_cap", "audio"},
        "reference": {"occurrence", "modality", "trim", "input_binding", "tag",
                      "retained_picture_frames_estimate", "effective_size", "retained_picture_frames_derived"},
        "input_binding": {"binding", "port", "start_time", "frame_load_cap", "audio", "soundtrack", "resolved_range"},
        "soundtrack": {"port", "tag"},
        "estimate": {"value", "kind", "observed"},
        "effective_size": {"width", "height"},
        "derived": {"value", "status", "kind", "reason", "input_frame_count", "input_sha256", "basis",
                    "conformance", "target_frame_cap", "alignment", "rule_source", "loader_source", "decoder_source"},
        "audio": {"requested_grants", "effective_40hz_mask", "decoded_frame_mask",
                  "expected_delivered_sample_spans", "actual_delivered_sample_spans"},
        "grant": {"samples", "strength", "use", "occurrences"},
        "effective_40hz_mask": {"values", "basis", "precision"},
        "span": {"samples", "source"},
    }
    child_context = {
        ("evidence", "reference_view"): "view", ("view", "references"): "reference",
        ("reference", "input_binding"): "input_binding", ("input_binding", "soundtrack"): "soundtrack",
        ("reference", "retained_picture_frames_estimate"): "estimate",
        ("reference", "effective_size"): "effective_size",
        ("reference", "retained_picture_frames_derived"): "derived",
        ("view", "audio"): "audio", ("audio", "requested_grants"): "grant",
        ("audio", "effective_40hz_mask"): "effective_40hz_mask",
        ("audio", "expected_delivered_sample_spans"): "span",
        ("audio", "actual_delivered_sample_spans"): "span",
    }
    list_context = {("view", "references"): "reference", ("audio", "requested_grants"): "grant",
                    ("audio", "expected_delivered_sample_spans"): "span",
                    ("audio", "actual_delivered_sample_spans"): "span"}
    typed_fields = {
        ("evidence", "reference_view"): (dict, type(None)),
        ("view", "references"): list, ("view", "target_frame_cap"): int,
        ("reference", "occurrence"): str, ("reference", "modality"): str,
        ("reference", "trim"): (list, type(None)), ("reference", "tag"): str,
        ("reference", "input_binding"): dict,
        ("reference", "retained_picture_frames_estimate"): dict,
        ("reference", "effective_size"): dict,
        ("reference", "retained_picture_frames_derived"): dict,
        ("input_binding", "binding"): str, ("input_binding", "port"): str,
        ("input_binding", "start_time"): (int, float), ("input_binding", "frame_load_cap"): int,
        ("input_binding", "audio"): (str, bool), ("input_binding", "soundtrack"): (dict, bool, type(None)),
        ("input_binding", "resolved_range"): (list, type(None)), ("soundtrack", "port"): str,
        ("soundtrack", "tag"): str, ("estimate", "value"): (int, float),
        ("estimate", "kind"): str, ("estimate", "observed"): bool,
        ("effective_size", "width"): int, ("effective_size", "height"): int,
        ("derived", "value"): int, ("derived", "status"): str, ("derived", "kind"): str,
        ("derived", "reason"): str, ("derived", "input_frame_count"): int,
        ("derived", "input_sha256"): str, ("derived", "basis"): str,
        ("derived", "conformance"): str, ("derived", "target_frame_cap"): int,
        ("derived", "alignment"): str, ("derived", "rule_source"): str,
        ("derived", "loader_source"): str, ("derived", "decoder_source"): str,
        ("audio", "requested_grants"): list, ("audio", "effective_40hz_mask"): dict,
        ("view", "audio"): dict,
        ("audio", "decoded_frame_mask"): list, ("audio", "expected_delivered_sample_spans"): list,
        ("audio", "actual_delivered_sample_spans"): list, ("grant", "samples"): list,
        ("grant", "strength"): (int, float), ("grant", "use"): str,
        ("grant", "occurrences"): list, ("effective_40hz_mask", "values"): list,
        ("effective_40hz_mask", "basis"): str, ("effective_40hz_mask", "precision"): (str, int, float),
        ("span", "samples"): list, ("span", "source"): str,
    }
    required_fields = {
        "view": {"references"},
        "reference": {"occurrence", "modality"},
        "soundtrack": {"port", "tag"},
        "estimate": {"value", "kind", "observed"},
        "effective_size": {"width", "height"},
        "grant": {"samples", "strength", "use", "occurrences"},
        "effective_40hz_mask": {"values", "basis", "precision"},
        "span": {"samples", "source"},
    }
    citations = {
        "rule_source": "ComfyUI@6bfaacc67c2103481e5f0c84d75257cd0581d86a:comfy_extras/nodes_minimax_h3.py:324-332",
        "loader_source": "ComfyUI-VideoHelperSuite@4ee72c065db22c9d96c2427954dc69e7b908444b:videohelpersuite/load_video_nodes.py:216-246",
        "decoder_source": "ComfyUI-VideoHelperSuite@4ee72c065db22c9d96c2427954dc69e7b908444b:videohelpersuite/load_video_nodes.py:170-193",
    }
    locator_patterns = (
        r"(?i)file://|https?://",
        r"(?i)(?:^|[\s\"'=])(?:[a-z]:[\\/]|\\\\)",
        r"(?i)(?:^|[\s\"'=])(?:\.{1,2}[\\/]|[a-z0-9_.-]+\\[a-z0-9_.-]+(?:\\|$))",
        r"(?<![A-Za-z0-9])/(?:[^/\s]+/)*[^/\s]+",
        r"(?<![A-Za-z0-9])(?:[A-Za-z_.-]+/)+[A-Za-z0-9_.-]+(?:\b|$)",
        r"(?i)\b[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.(?:mp4|mkv|mov|webm|wav|flac|png|jpe?g|json|zip|py)(?:\b|:)",
    )

    def check(item: Any, context: str, where: str) -> None:
        if isinstance(item, dict):
            if set(item) - field_sets[context]:
                raise ManagedGenerationResultError(f"workflow hook evidence has an unsupported field at {where}")
            required = required_fields.get(context, set())
            if required - set(item):
                raise ManagedGenerationResultError(f"workflow hook evidence is missing a field at {where}")
            for key, child in item.items():
                expected_type = typed_fields.get((context, key))
                if expected_type is not None and (type(child) not in expected_type if isinstance(expected_type, tuple)
                                                   else type(child) is not expected_type):
                    raise ManagedGenerationResultError(f"workflow hook evidence has an invalid value at {where}.{key}")
                entry_context = list_context.get((context, key))
                if (type(child) is list and entry_context is not None
                        and any(type(entry) is not dict for entry in child)):
                    raise ManagedGenerationResultError(f"workflow hook evidence has an invalid list entry at {where}.{key}")
                if key in citations:
                    if child != citations[key]:
                        raise ManagedGenerationResultError(f"workflow hook evidence has an unpinned citation at {where}.{key}")
                    continue
                next_context = child_context.get((context, key))
                if isinstance(child, dict) and next_context is None:
                    raise ManagedGenerationResultError(f"workflow hook evidence has an unsupported object at {where}.{key}")
                check(child, next_context or context, f"{where}.{key}")
        elif isinstance(item, list):
            next_context = list_context.get((context, where.rsplit(".", 1)[-1]))
            list_field = where.rsplit(".", 1)[-1]
            if ((context, list_field) in {("reference", "trim"), ("input_binding", "resolved_range"),
                                          ("grant", "samples"), ("span", "samples")}
                    and len(item) != 2):
                raise ManagedGenerationResultError(f"workflow hook evidence has an invalid boundary pair at {where}")
            if next_context is not None and any(type(child) is not dict for child in item):
                raise ManagedGenerationResultError(f"workflow hook evidence has an invalid list entry at {where}")
            primitive_types = {
                ("reference", "trim"): (int, float, str), ("input_binding", "resolved_range"): (int, float, str),
                ("grant", "samples"): int, ("grant", "occurrences"): str,
                ("effective_40hz_mask", "values"): (int, float), ("audio", "decoded_frame_mask"): (int, float, bool),
                ("span", "samples"): int,
            }.get((context, list_field))
            if primitive_types is not None and any(type(child) not in (primitive_types if isinstance(primitive_types, tuple)
                                                                        else (primitive_types,)) for child in item):
                raise ManagedGenerationResultError(f"workflow hook evidence has invalid list values at {where}")
            if ((context, list_field) in {("reference", "trim"), ("input_binding", "resolved_range")}
                    and any(isinstance(child, str) and re.fullmatch(
                        r"(?:0|[1-9][0-9]*)/[1-9][0-9]*", child) is None for child in item)):
                raise ManagedGenerationResultError(f"workflow hook evidence has an invalid rational boundary at {where}")
            for index, child in enumerate(item):
                check(child, next_context or context, f"{where}[{index}]")
        elif isinstance(item, str):
            rational_trim = (context in {"reference", "input_binding"}
                and where.rsplit(".", 1)[-1].startswith(("trim[", "resolved_range["))
                and re.fullmatch(r"(?:0|[1-9][0-9]*)/[1-9][0-9]*", item) is not None)
            if not rational_trim and any(re.search(pattern, item) for pattern in locator_patterns):
                raise ManagedGenerationResultError(f"workflow hook evidence contains a path or URL at {where}")
        elif item is not None and type(item) not in (bool, int, float):
            raise ManagedGenerationResultError(f"workflow hook evidence is not JSON-safe at {where}")

    if type(value) is not dict:
        raise ManagedGenerationResultError("workflow hook evidence must be an object")
    check(value, "evidence", "evidence")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ManagedGenerationResultError("workflow hook evidence must be finite JSON") from exc
    if len(encoded.encode("utf-8")) > 2_000_000:
        raise ManagedGenerationResultError("workflow hook evidence exceeds the 2 MB limit")
    return json.loads(encoded)


def _validate_profile_fields(payload: Mapping[str, Any]) -> None:
    unknown = sorted(set(payload) - _PROFILE_FIELDS)
    missing = sorted(_PROFILE_FIELDS - set(payload))
    if missing:
        raise ManagedGenerationResultError(
            "managed-generation result missing required fields: " + ", ".join(missing)
        )
    if unknown:
        raise ManagedGenerationResultError(
            "managed-generation result contains unknown fields: " + ", ".join(unknown)
        )

def validate_managed_generation_result(
    payload: Any,
    *,
    staging_root: str | Path,
    expected_task_id: str | None = None,
    expected_attempt_id: str | None = None,
) -> ManagedGenerationResult:
    """Strictly validate one managed-generation result against staged bytes."""
    if not isinstance(payload, Mapping):
        raise ManagedGenerationResultError("managed-generation result must be an object")
    _validate_profile_fields(payload)
    if (
        isinstance(payload["schema_version"], bool)
        or not isinstance(payload["schema_version"], int)
        or payload["schema_version"] != MANAGED_GENERATION_RESULT_SCHEMA_VERSION
    ):
        raise ManagedGenerationResultError(
            f"schema_version must be {MANAGED_GENERATION_RESULT_SCHEMA_VERSION}"
        )
    if payload["kind"] != MANAGED_GENERATION_RESULT_KIND:
        raise ManagedGenerationResultError(
            f"kind must be {MANAGED_GENERATION_RESULT_KIND!r}"
        )

    task_id = _require_identifier(payload["task_id"], "task_id")
    attempt_id = _require_identifier(payload["attempt_id"], "attempt_id")
    producer_run_id = _require_identifier(payload["producer_run_id"], "producer_run_id")
    if expected_task_id is not None and task_id != expected_task_id:
        raise ManagedGenerationResultError(
            f"task_id {task_id!r} does not match expected task_id {expected_task_id!r}"
        )
    if expected_attempt_id is not None and attempt_id != expected_attempt_id:
        raise ManagedGenerationResultError(
            f"attempt_id {attempt_id!r} does not match expected attempt_id {expected_attempt_id!r}"
        )

    inputs = dict(_require_mapping(payload["inputs"], "inputs"))
    created = _require_text(payload["created"], "created")
    try:
        timestamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if timestamp.utcoffset() != timedelta(0):
            raise ValueError("UTC required")
    except ValueError as exc:
        raise ManagedGenerationResultError("created must be a UTC timestamp") from exc
    for key in ("source", "effective_settings"):
        if key in inputs:
            inputs[key] = validate_evidence_reference(
                inputs[key], field=f"inputs.{key}", staging_root=staging_root,
            )
    warnings = payload["warnings"]
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise ManagedGenerationResultError("warnings must be a list of strings")

    raw_outputs = payload["outputs"]
    if not isinstance(raw_outputs, list):
        raise ManagedGenerationResultError("outputs must be a list")
    profile_outputs: list[ManagedGenerationOutput] = []
    seen_ids: set[str] = set()
    universal_outputs: list[dict[str, Any]] = []
    outcomes = _validate_outcomes(payload["outcomes"], output_count=len(raw_outputs))
    for index, raw_output in enumerate(raw_outputs):
        output = _require_mapping(raw_output, f"outputs[{index}]")
        unknown = sorted(set(output) - _OUTPUT_FIELDS)
        missing = sorted((_OUTPUT_FIELDS - {"hash_check"}) - set(output))
        if missing:
            raise ManagedGenerationResultError(
                f"outputs[{index}] missing required fields: {', '.join(missing)}"
            )
        if unknown:
            raise ManagedGenerationResultError(
                f"outputs[{index}] contains unknown fields: {', '.join(unknown)}"
            )
        output_id = _require_identifier(
            output["producer_output_id"], f"outputs[{index}].producer_output_id"
        )
        if output_id in seen_ids:
            raise ManagedGenerationResultError(
                f"duplicate producer output identity {output_id!r}"
            )
        seen_ids.add(output_id)
        output_port = _require_identifier(output["output_port"], f"outputs[{index}].output_port")
        ordinal = output["ordinal"]
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise ManagedGenerationResultError(
                f"outputs[{index}].ordinal must be a non-negative integer"
            )
        path = output["path"]
        if not isinstance(path, str) or not path.strip() or Path(path).is_absolute() or ".." in Path(path).parts:
            raise ManagedGenerationResultError(
                f"outputs[{index}].path must be a contained relative path"
            )
        media_type = output["media_type"]
        if not isinstance(media_type, str) or _MIME_RE.fullmatch(media_type) is None:
            raise ManagedGenerationResultError(
                f"outputs[{index}].media_type must be a lowercase MIME type"
            )
        size = output["bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ManagedGenerationResultError(
                f"outputs[{index}].bytes must be a non-negative integer"
            )
        sha256 = output["sha256"]
        if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
            raise ManagedGenerationResultError(
                f"outputs[{index}].sha256 must be 64 lowercase hexadecimal characters"
            )
        hash_check = None
        if "hash_check" in output:
            check = _require_mapping(output["hash_check"], f"outputs[{index}].hash_check")
            if set(check) != {"status", "expected_sha256"}:
                raise ManagedGenerationResultError(f"outputs[{index}].hash_check must contain status and expected_sha256")
            expected = check["expected_sha256"]
            if expected is not None and (not isinstance(expected, str) or _SHA256_RE.fullmatch(expected) is None):
                raise ManagedGenerationResultError(f"outputs[{index}].hash_check.expected_sha256 must be a sha256 or null")
            actual_status = "not_checked" if expected is None else "verified" if expected == sha256 else "failed"
            if check["status"] != actual_status:
                raise ManagedGenerationResultError(f"outputs[{index}].hash_check.status disagrees with the hashes")
            if actual_status == "failed" and outcomes["verification"].status == "succeeded":
                raise ManagedGenerationResultError("failed output hash check cannot have successful verification")
            hash_check = dict(check)
        profile_outputs.append(
            ManagedGenerationOutput(
                producer_output_id=output_id,
                output_port=output_port,
                ordinal=ordinal,
                path=path,
                media_type=media_type,
                bytes=size,
                sha256=sha256,
                hash_check=hash_check,
            )
        )
        universal_outputs.append(
            {
                "path": path,
                "ordinal": ordinal,
                "content_hash": f"sha256:{sha256}",
                "bytes": size,
                "media_type": media_type,
                "output_port": output_port,
                "producer_output_id": output_id,
            }
        )

    evidence = _require_mapping(payload["evidence"], "evidence")
    if set(evidence) != {"producer", "transport"}:
        raise ManagedGenerationResultError(
            "evidence must contain exactly producer and transport namespaces"
        )
    evidence_namespaces: dict[str, Mapping[str, Any]] = {}
    for namespace in ("producer", "transport"):
        evidence_namespaces[namespace] = _require_mapping(
            evidence[namespace], f"evidence.{namespace}"
        )

    universal: dict[str, Any] = {
        "schema_version": MANAGED_GENERATION_RESULT_SCHEMA_VERSION,
        "kind": MANAGED_GENERATION_RESULT_KIND,
        "inputs": inputs,
        "outputs": universal_outputs,
        "created": created,
        "warnings": warnings,
    }
    if universal_outputs:
        try:
            validated: ValidatedResultManifest = validate_result_manifest(
                universal, staging_root=staging_root
            )
        except ResultManifestError as exc:
            raise ManagedGenerationResultError(str(exc)) from exc
        validated_root = validated.staging_root
    else:
        validated_root = Path(staging_root)
        if not validated_root.exists() or not validated_root.is_dir():
            raise ManagedGenerationResultError(
                f"staging root must be an existing directory, got {str(validated_root)!r}"
            )
    result = ManagedGenerationResult(
        task_id=task_id,
        attempt_id=attempt_id,
        producer_run_id=producer_run_id,
        created=created,
        inputs=dict(inputs),
        outputs=tuple(profile_outputs),
        outcomes=outcomes,
        evidence=evidence_namespaces,
        warnings=tuple(warnings),
        staging_root=validated_root,
    )
    try:
        result.to_json()
    except (TypeError, ValueError) as exc:
        raise ManagedGenerationResultError(
            f"managed-generation result contains non-JSON evidence or inputs: {exc}"
        ) from exc
    return result


def read_managed_generation_result(
    path: str | Path,
    *,
    staging_root: str | Path,
    expected_task_id: str | None = None,
    expected_attempt_id: str | None = None,
) -> ManagedGenerationResult:
    """Read JSON and apply strict profile plus staged-byte validation."""
    manifest_path = Path(path)
    try:
        raw = manifest_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedGenerationResultError(
            f"cannot read managed-generation result {manifest_path}: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise ManagedGenerationResultError(
            f"managed-generation result {manifest_path} must be a JSON object"
        )
    return validate_managed_generation_result(
        payload,
        staging_root=staging_root,
        expected_task_id=expected_task_id,
        expected_attempt_id=expected_attempt_id,
    )


__all__ = [
    "MANAGED_GENERATION_RESULT_KIND",
    "MANAGED_GENERATION_RESULT_SCHEMA_VERSION",
    "ManagedGenerationOutput",
    "ManagedGenerationPhaseOutcome",
    "ManagedGenerationResult",
    "ManagedGenerationResultError",
    "PHASE_NAMES",
    "PHASE_STATUSES",
    "read_managed_generation_result",
    "validate_managed_generation_result",
    "validate_evidence_reference",
    "redact_evidence",
    "validate_workflow_hook_evidence",
]
