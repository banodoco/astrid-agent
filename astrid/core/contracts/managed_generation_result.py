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
from pathlib import Path
from typing import Any, Mapping

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
    """A verified output with producer identity and portable location."""

    producer_output_id: str
    output_port: str
    ordinal: int
    path: str
    media_type: str
    bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "producer_output_id": self.producer_output_id,
            "output_port": self.output_port,
            "ordinal": self.ordinal,
            "path": self.path,
            "media_type": self.media_type,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }


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
    if execution != "succeeded" and output_count:
        raise ManagedGenerationResultError(
            "outputs are only allowed when execution outcome succeeded"
        )
    if execution != "succeeded" and any(
        status != "not_started"
        for status in (retrieval, verification, publication)
    ):
        raise ManagedGenerationResultError(
            "retrieval, verification, and publication must be not_started after execution failure"
        )
    if retrieval != "succeeded" and verification != "not_started":
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

    inputs = _require_mapping(payload["inputs"], "inputs")
    created = _require_text(payload["created"], "created")
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
        missing = sorted(_OUTPUT_FIELDS - set(output))
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
        profile_outputs.append(
            ManagedGenerationOutput(
                producer_output_id=output_id,
                output_port=output_port,
                ordinal=ordinal,
                path=path,
                media_type=media_type,
                bytes=size,
                sha256=sha256,
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
]
