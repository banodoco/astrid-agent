"""Strict, non-authoritative parsers for the journey evidence index.

The M-CI evaluator owns artifact retrieval, raw-byte provenance, selected
attempts, and closed-inventory decisions.  This module only parses the raw
JSON form that M-CI hashes and validates the checked-in journey receipt
contracts.  A structurally valid result is deliberately *not* an independent
R-30 observation or a qualification decision.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Mapping

if __package__:
    from . import journey_failure_modes as failure
    from . import journey_matrix as matrix
    from . import journey_release2 as release2
    from . import native_service_adapter as native
else:
    try:
        import journey_failure_modes as failure
        import journey_matrix as matrix
        import journey_release2 as release2
        import native_service_adapter as native
    except ModuleNotFoundError:
        from scripts import journey_failure_modes as failure
        from scripts import journey_matrix as matrix
        from scripts import journey_release2 as release2
        from scripts import native_service_adapter as native


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_SAFE_INTEGER = (1 << 53) - 1
_RECEIPT_PATH = "journey-result.json"
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5",
    "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4",
    "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


class ObservationParseError(ValueError):
    """Raised when raw artifact bytes are not an R-30-safe JSON object."""


@dataclass(frozen=True)
class ParsedProducerEnvelope:
    """A structurally linked R-30 export and collector seal.

    This is deliberately not an authentication result.  M-CI must establish
    the export/seal's provenance, signature roles, selected attempt, and
    immutable transport before this object can be considered by a predicate.
    """

    export_sha256: str
    export: dict[str, object]
    seal_sha256: str
    seal: dict[str, object]


@dataclass(frozen=True)
class ParsedEvidence:
    """One selected receipt plus the index entries that its contract requires."""

    selector: str
    candidate: dict[str, object] | None
    receipt: dict[str, object]
    files: dict[str, dict[str, object]]
    producer: ParsedProducerEnvelope | None
    producer_supplied: bool
    authenticated_context: dict[str, object] | None
    producer_files: dict[str, dict[str, object]]


def _reject_constant(value: str) -> object:
    raise ObservationParseError(f"JSON constant {value!r} is not allowed")


def _safe_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:  # pragma: no cover - json only supplies valid integer syntax
        raise ObservationParseError("invalid JSON integer") from exc
    if not -_MAX_SAFE_INTEGER <= parsed <= _MAX_SAFE_INTEGER:
        raise ObservationParseError("JSON integer exceeds the R-30 safe range")
    return parsed


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ObservationParseError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _validate_json_values(value: object) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ObservationParseError("JSON contains an unsafe surrogate string")
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int):
        if not -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
            raise ObservationParseError("JSON integer exceeds the R-30 safe range")
        return
    if isinstance(value, float):
        raise ObservationParseError("JSON floats are not allowed")
    if isinstance(value, list):
        for item in value:
            _validate_json_values(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):  # json cannot produce this, retained for defensive callers
                raise ObservationParseError("JSON object key is not a string")
            _validate_json_values(key)
            _validate_json_values(item)
        return
    raise ObservationParseError("JSON contains an unsupported value")


def parse_json_object(raw: bytes, expected_sha256: str) -> dict[str, object]:
    """Hash, decode, and strictly parse one raw evidence object.

    This is the boundary M-CI should use while building its authenticated
    index.  It proves only byte-to-object correspondence; caller ownership of
    the bytes remains M-CI's responsibility.
    """
    if not isinstance(raw, bytes):
        raise ObservationParseError("raw artifact must be bytes")
    if not isinstance(expected_sha256, str) or _DIGEST.fullmatch(expected_sha256) is None:
        raise ObservationParseError("expected SHA-256 must be lowercase hexadecimal")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ObservationParseError("raw artifact SHA-256 differs from the evidence index")
    try:
        text = raw.decode("utf-8", "strict")
        value = json.loads(
            text,
            object_pairs_hook=_no_duplicate_object,
            parse_constant=_reject_constant,
            parse_float=lambda _value: (_ for _ in ()).throw(
                ObservationParseError("JSON floats are not allowed")
            ),
            parse_int=_safe_integer,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObservationParseError("raw artifact is not UTF-8 JSON") from exc
    _validate_json_values(value)
    if not isinstance(value, dict):
        raise ObservationParseError("raw artifact must contain a JSON object")
    return value


def canonical_json_sha256(value: object) -> str:
    """Return the R30-CJSON-1 digest for an already decoded JSON value.

    This is used only for R-30 relational checks such as ``files_sha256``.
    It deliberately does not turn decoded JSON into a substitute for the raw
    artifact digest, which M-CI must compute before parsing.
    """
    _validate_json_values(value)
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:  # defensive for non-JSON callers
        raise ObservationParseError("value is not R-30 canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


_R30_BINDING_FIELDS = {
    "qualification_id", "tuple_sha256", "inventory_sha256", "obligation_id",
    "attempt_id", "nonce", "origin_sha256", "execution_sha256",
}
_R30_EXPORT_FIELDS = {
    "kind", "version", "binding", "contract_sha256", "owner_kind", "session_id",
    "status", "checks", "observations", "continuity", "files",
}
_R30_SEAL_FIELDS = {
    "kind", "version", "binding", "grant_sha256", "collector_profile_sha256",
    "provider_contract_sha256", "export_sha256", "claims_sha256", "files_sha256",
    "result", "context_state",
}
_R30_FILE_FIELDS = {"path", "role", "sha256", "size"}
_R30_OBSERVATION_FIELDS = {"id", "kind", "file", "sha256", "sequence"}
_R30_CHECK_FIELDS = {"id", "result", "observations"}
_R30_CONTINUITY_FIELDS = {
    "source_profile_sha256", "first_sequence", "last_sequence", "event_count",
    "gaps", "dropped", "terminal",
}


def _r30_id(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 256 and bool(value.strip())


def _r30_exact_object(
    value: object, required: set[str], label: str, errors: list[str]
) -> dict[str, object]:
    if not isinstance(value, dict):
        errors.append(f"{label}: object required")
        return {}
    unknown = set(value) - required
    missing = required - set(value)
    if unknown:
        errors.append(f"{label}: unknown field(s): {', '.join(sorted(map(str, unknown)))}")
    if missing:
        errors.append(f"{label}: missing field(s): {', '.join(sorted(missing))}")
    return value


def _r30_digest(value: object, label: str, errors: list[str]) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        errors.append(f"{label}: lowercase SHA-256 required")


def _r30_id_error(value: object, label: str, errors: list[str]) -> None:
    if not _r30_id(value):
        errors.append(f"{label}: nonempty bounded ID required")


def _r30_positive_integer(value: object, label: str, errors: list[str]) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_SAFE_INTEGER:
        errors.append(f"{label}: positive R-30 safe integer required")


def _parse_r30_binding(value: object, label: str, errors: list[str]) -> dict[str, object]:
    binding = _r30_exact_object(value, _R30_BINDING_FIELDS, label, errors)
    for name in ("qualification_id", "obligation_id", "attempt_id"):
        _r30_id_error(binding.get(name), f"{label}.{name}", errors)
    for name in ("tuple_sha256", "inventory_sha256", "nonce", "origin_sha256", "execution_sha256"):
        _r30_digest(binding.get(name), f"{label}.{name}", errors)
    return binding


def _parse_r30_files(value: object, errors: list[str]) -> dict[str, dict[str, object]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 1024:
        errors.append("producer.export.files: 1..1024 declared files required")
        return {}
    files: dict[str, dict[str, object]] = {}
    total_size = 0
    paths: list[str] = []
    for number, item in enumerate(value):
        prefix = f"producer.export.files[{number}]"
        row = _r30_exact_object(item, _R30_FILE_FIELDS, prefix, errors)
        path = row.get("path")
        if not safe_relative_path(path):
            errors.append(f"{prefix}.path: safe relative path required")
        elif path in files:
            errors.append(f"{prefix}.path: duplicate declared path {path!r}")
        else:
            files[path] = row
            paths.append(path)
        _r30_id_error(row.get("role"), f"{prefix}.role", errors)
        _r30_digest(row.get("sha256"), f"{prefix}.sha256", errors)
        size = row.get("size")
        if type(size) is not int or not 0 <= size <= 52_428_800:
            errors.append(f"{prefix}.size: 0..52428800 integer required")
        else:
            total_size += size
    if paths != sorted(paths):
        errors.append("producer.export.files: paths must be sorted")
    if total_size > 52_428_800:
        errors.append("producer.export.files: declared payload exceeds 52428800 bytes")
    return files


def _parse_r30_observations(
    value: object, files: Mapping[str, Mapping[str, object]], errors: list[str]
) -> set[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 4096:
        errors.append("producer.export.observations: 1..4096 observations required")
        return set()
    identities: set[str] = set()
    sequences: list[int] = []
    for number, item in enumerate(value):
        prefix = f"producer.export.observations[{number}]"
        row = _r30_exact_object(item, _R30_OBSERVATION_FIELDS, prefix, errors)
        observation_id = row.get("id")
        _r30_id_error(observation_id, f"{prefix}.id", errors)
        if isinstance(observation_id, str):
            if observation_id in identities:
                errors.append(f"{prefix}.id: duplicate observation ID {observation_id!r}")
            identities.add(observation_id)
        _r30_id_error(row.get("kind"), f"{prefix}.kind", errors)
        file_path = row.get("file")
        if not isinstance(file_path, str) or file_path not in files:
            errors.append(f"{prefix}.file: declared export file required")
        _r30_digest(row.get("sha256"), f"{prefix}.sha256", errors)
        if isinstance(file_path, str) and file_path in files and row.get("sha256") != files[file_path].get("sha256"):
            errors.append(f"{prefix}.sha256: differs from declared file")
        sequence = row.get("sequence")
        _r30_positive_integer(sequence, f"{prefix}.sequence", errors)
        if type(sequence) is int and 1 <= sequence <= _MAX_SAFE_INTEGER:
            sequences.append(sequence)
    if sequences != sorted(sequences):
        errors.append("producer.export.observations: observations must be sorted by sequence")
    if len(set(sequences)) != len(sequences):
        errors.append("producer.export.observations: duplicate sequence")
    return identities


def _parse_r30_checks(value: object, observation_ids: set[str], errors: list[str]) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 256:
        errors.append("producer.export.checks: 1..256 checks required")
        return
    check_ids: set[str] = set()
    for number, item in enumerate(value):
        prefix = f"producer.export.checks[{number}]"
        row = _r30_exact_object(item, _R30_CHECK_FIELDS, prefix, errors)
        check_id = row.get("id")
        _r30_id_error(check_id, f"{prefix}.id", errors)
        if isinstance(check_id, str):
            if check_id in check_ids:
                errors.append(f"{prefix}.id: duplicate check ID {check_id!r}")
            check_ids.add(check_id)
        if row.get("result") != "pass":
            errors.append(f"{prefix}.result: pass required")
        referenced = row.get("observations")
        if not isinstance(referenced, list) or not 1 <= len(referenced) <= 256:
            errors.append(f"{prefix}.observations: 1..256 observation IDs required")
            continue
        if any(not _r30_id(item) for item in referenced):
            errors.append(f"{prefix}.observations: nonempty bounded IDs required")
            continue
        observation_references = [item for item in referenced if isinstance(item, str)]
        if len(set(observation_references)) != len(observation_references):
            errors.append(f"{prefix}.observations: duplicate observation ID")
        for observation_id in observation_references:
            if observation_id not in observation_ids:
                errors.append(f"{prefix}.observations: dangling observation {observation_id!r}")


def _parse_r30_continuity(value: object, observations: object, errors: list[str]) -> None:
    continuity = _r30_exact_object(value, _R30_CONTINUITY_FIELDS, "producer.export.continuity", errors)
    _r30_digest(continuity.get("source_profile_sha256"), "producer.export.continuity.source_profile_sha256", errors)
    for name in ("first_sequence", "last_sequence", "event_count"):
        _r30_positive_integer(continuity.get(name), f"producer.export.continuity.{name}", errors)
    for name in ("gaps", "dropped"):
        value = continuity.get(name)
        if type(value) is not int or not 0 <= value <= _MAX_SAFE_INTEGER:
            errors.append(f"producer.export.continuity.{name}: nonnegative R-30 safe integer required")
        elif value != 0:
            errors.append(f"producer.export.continuity.{name}: zero required")
    if continuity.get("terminal") is not True:
        errors.append("producer.export.continuity.terminal: true required")
    sequence_values = [
        row.get("sequence") for row in observations if isinstance(row, dict) and type(row.get("sequence")) is int
    ] if isinstance(observations, list) else []
    if sequence_values and all(
        type(continuity.get(name)) is int for name in ("first_sequence", "last_sequence", "event_count")
    ):
        first, last, count = (
            continuity["first_sequence"], continuity["last_sequence"], continuity["event_count"]
        )
        if first != sequence_values[0] or last != sequence_values[-1] or count != len(sequence_values):
            errors.append("producer.export.continuity: sequence bounds or event count differs")
        elif last - first + 1 != count:
            errors.append("producer.export.continuity: observation sequence is not contiguous")


def _parse_r30_export(value: object, errors: list[str]) -> dict[str, object]:
    export = _r30_exact_object(value, _R30_EXPORT_FIELDS, "producer.export", errors)
    if export.get("kind") != "r23-export":
        errors.append("producer.export.kind: r23-export required")
    if export.get("version") != 1:
        errors.append("producer.export.version: version 1 required")
    _parse_r30_binding(export.get("binding"), "producer.export.binding", errors)
    _r30_digest(export.get("contract_sha256"), "producer.export.contract_sha256", errors)
    if export.get("owner_kind") not in {"native", "pinokio"}:
        errors.append("producer.export.owner_kind: native or pinokio required")
    _r30_id_error(export.get("session_id"), "producer.export.session_id", errors)
    if export.get("status") != "pass":
        errors.append("producer.export.status: pass required")
    files = _parse_r30_files(export.get("files"), errors)
    observation_ids = _parse_r30_observations(export.get("observations"), files, errors)
    _parse_r30_checks(export.get("checks"), observation_ids, errors)
    _parse_r30_continuity(export.get("continuity"), export.get("observations"), errors)
    return export


def _parse_r30_seal(
    value: object, export: Mapping[str, object], export_sha256: str, errors: list[str]
) -> dict[str, object]:
    seal = _r30_exact_object(value, _R30_SEAL_FIELDS, "producer.seal", errors)
    if seal.get("kind") != "collector-seal":
        errors.append("producer.seal.kind: collector-seal required")
    if seal.get("version") != 1:
        errors.append("producer.seal.version: version 1 required")
    binding = _parse_r30_binding(seal.get("binding"), "producer.seal.binding", errors)
    if binding != export.get("binding"):
        errors.append("producer.seal.binding: differs from export binding")
    for name in (
        "grant_sha256", "collector_profile_sha256", "provider_contract_sha256", "export_sha256",
        "claims_sha256", "files_sha256",
    ):
        _r30_digest(seal.get(name), f"producer.seal.{name}", errors)
    if seal.get("export_sha256") != export_sha256:
        errors.append("producer.seal.export_sha256: differs from indexed export")
    try:
        expected_files_sha256 = canonical_json_sha256(export.get("files"))
    except ObservationParseError as exc:
        errors.append(f"producer.export.files: cannot canonicalize ({exc})")
    else:
        if seal.get("files_sha256") != expected_files_sha256:
            errors.append("producer.seal.files_sha256: differs from R30-CJSON-1 export files")
    if seal.get("result") != "pass":
        errors.append("producer.seal.result: pass required")
    if seal.get("context_state") != "finalized":
        errors.append("producer.seal.context_state: finalized required")
    return seal


def _producer_entry(value: object, label: str, errors: list[str]) -> tuple[str, dict[str, object]]:
    entry = _r30_exact_object(value, {"sha256", "json"}, label, errors)
    digest = entry.get("sha256")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        errors.append(f"{label}.sha256: lowercase SHA-256 required")
        digest = ""
    payload = entry.get("json")
    if not isinstance(payload, dict):
        errors.append(f"{label}.json: parsed JSON object required")
        payload = {}
    else:
        try:
            _validate_json_values(payload)
        except ObservationParseError as exc:
            errors.append(f"{label}.json: {exc}")
    return digest, payload


def parse_producer_envelope(value: object) -> tuple[ParsedProducerEnvelope | None, list[str]]:
    """Parse the R-30 export/seal relation without authenticating it.

    ``value`` is M-CI's already-indexed ``{"export": ..., "seal": ...}``
    envelope.  The raw-byte digest in each member must have been checked by
    :func:`parse_json_object` when M-CI built that index.  This parser verifies
    only the approved structural and cross-record rules, never signatures,
    trust roots, selection, replay, or finalization authority.
    """
    errors: list[str] = []
    envelope = _r30_exact_object(value, {"export", "seal"}, "producer", errors)
    export_sha256, export_value = _producer_entry(envelope.get("export"), "producer.export", errors)
    seal_sha256, seal_value = _producer_entry(envelope.get("seal"), "producer.seal", errors)
    export = _parse_r30_export(export_value, errors)
    seal = _parse_r30_seal(seal_value, export, export_sha256, errors)
    if errors:
        return None, errors
    return ParsedProducerEnvelope(export_sha256, export, seal_sha256, seal), errors


def _producer_owner_errors(
    producer: ParsedProducerEnvelope | None, expected_owner: str, label: str
) -> list[str]:
    if producer is None:
        return []
    if producer.export.get("owner_kind") != expected_owner:
        return [f"producer.export.owner_kind: {label} requires {expected_owner!r} ownership"]
    return []


def safe_relative_path(value: object) -> bool:
    """Apply the portable R-30 artifact-member path restrictions."""
    if not isinstance(value, str) or not value or len(value) > 512:
        return False
    if any(not (0x21 <= ord(character) <= 0x7E) for character in value):
        return False
    if "\\" in value or ":" in value or "%" in value:
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value:
        return False
    for part in path.parts:
        if part in {"", ".", ".."} or part.endswith((".", " ")):
            return False
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
            return False
    return True


def index_files(
    value: object, *, label: str = "files"
) -> tuple[dict[str, dict[str, object]], list[str]]:
    """Validate the index fragment accepted by the decided predicate API."""
    if not isinstance(value, list):
        return {}, [f"{label}: evidence index list is required"]
    indexed: dict[str, dict[str, object]] = {}
    errors: list[str] = []
    for number, item in enumerate(value):
        prefix = f"{label}[{number}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}: index entry must be an object")
            continue
        extra = set(item) - {"path", "sha256", "json"}
        if extra:
            errors.append(f"{prefix}: unknown index field(s): {', '.join(sorted(extra))}")
        path = item.get("path")
        if not safe_relative_path(path):
            errors.append(f"{prefix}.path: safe relative path required")
            continue
        if path in indexed:
            errors.append(f"{prefix}.path: duplicate evidence path {path!r}")
            continue
        indexed[path] = item
        if not isinstance(item.get("sha256"), str) or _DIGEST.fullmatch(item["sha256"]) is None:
            errors.append(f"{prefix}.sha256: SHA-256 required")
        if not isinstance(item.get("json"), dict):
            errors.append(f"{prefix}.json: parsed JSON object required")
    return indexed, errors


def _unknown_fields(value: Mapping[str, object], allowed: set[str], label: str) -> list[str]:
    unknown = set(value) - allowed
    return [f"{label}: unknown field(s): {', '.join(sorted(unknown))}"] if unknown else []


def _candidate_errors(candidate: object) -> list[str]:
    if not isinstance(candidate, dict):
        return ["candidate: frozen tuple binding is required"]
    return failure.candidate_errors(candidate)


def _parse_selected(
    evidence: object,
    *,
    selector_name: str,
    accepted_selectors: set[str],
    require_candidate: bool,
    receipt_path: str,
    required_paths: set[str],
) -> tuple[ParsedEvidence | None, list[str]]:
    if not isinstance(evidence, dict):
        return None, ["evidence: index object required"]
    # ``authenticated_context`` and ``producer_files`` are supplied only by
    # M-CI after it has authenticated the selected immutable bytes.  They are
    # deliberately optional at this structural boundary: legacy callers keep
    # parsing, while semantic predicates reject their absence.
    allowed = {
        selector_name,
        "candidate",
        "files",
        "producer",
        "authenticated_context",
        "producer_files",
    }
    errors = _unknown_fields(evidence, allowed, "evidence")
    selector = evidence.get(selector_name)
    if not isinstance(selector, str) or selector not in accepted_selectors:
        errors.append(f"{selector_name}: selected obligation is missing or unknown")
        selector = ""
    candidate: dict[str, object] | None = None
    if require_candidate or "candidate" in evidence:
        candidate_value = evidence.get("candidate")
        errors.extend(_candidate_errors(candidate_value))
        if isinstance(candidate_value, dict):
            candidate = candidate_value
    producer_supplied = "producer" in evidence
    producer: ParsedProducerEnvelope | None = None
    if producer_supplied:
        producer, producer_errors = parse_producer_envelope(evidence.get("producer"))
        errors.extend(producer_errors)
    context_value = evidence.get("authenticated_context")
    authenticated_context = context_value if isinstance(context_value, dict) else None
    if "authenticated_context" in evidence and authenticated_context is None:
        errors.append("authenticated_context: object required")
    producer_files: dict[str, dict[str, object]] = {}
    if "producer_files" in evidence:
        producer_files, producer_file_errors = index_files(
            evidence.get("producer_files"), label="producer_files"
        )
        errors.extend(producer_file_errors)
    files, file_errors = index_files(evidence.get("files"))
    errors.extend(file_errors)
    for path in sorted(required_paths - files.keys()):
        errors.append(f"files: missing evidence {path!r}")
    for path in sorted(files.keys() - required_paths):
        errors.append(f"files: unexpected evidence {path!r}")
    receipt = files.get(receipt_path, {}).get("json")
    if not isinstance(receipt, dict):
        errors.append("receipt: parsed journey receipt is required")
        return None, errors
    return ParsedEvidence(
        selector,
        candidate,
        receipt,
        files,
        producer,
        producer_supplied,
        authenticated_context,
        producer_files,
    ), errors


_RELEASE1_FIELDS = {
    "schema_version", "receipt_kind", "job_id", "mode", "scenario", "status",
    "runner_image", "runner_provenance", "os_build", "shell",
    "source_override_test_mode", "candidate_checkouts", "service_manager_prerequisite",
    "service_manager", "release_transition", "release0_fixture", "semantic_data",
    "sources", "installed_tuple", "import_origins", "checks", "final_cleanup",
    "evidence_class", "pinokio_backend", "source_unit_evidence", "run_roots", "sentinel",
}


def _release1_errors(receipt: Mapping[str, object], candidate: Mapping[str, object], job_id: str) -> list[str]:
    errors = _unknown_fields(receipt, _RELEASE1_FIELDS, "receipt")
    try:
        contract = matrix.release1_job(job_id)
    except ValueError as exc:  # selector is checked before this point
        return [*errors, str(exc)]
    expected_os, expected_image = failure.RUNNER_PROVENANCE[contract["runner"]][1:]
    for field, expected in (
        ("schema_version", 2),
        ("receipt_kind", "release1-installed-lifecycle"),
        ("job_id", job_id),
        ("mode", contract["mode"]),
        ("scenario", contract.get("leg", "fresh-install")),
        ("status", "pass"),
        ("runner_image", contract["runner"]),
        ("shell", contract["shell"]),
        ("source_override_test_mode", False),
        ("final_cleanup", "pass"),
    ):
        if receipt.get(field) != expected:
            errors.append(f"receipt.{field}: expected {expected!r}")
    provenance = receipt.get("runner_provenance")
    if (
        not isinstance(provenance, dict)
        or provenance.get("RUNNER_OS") != expected_os
        or provenance.get("ImageOS") != expected_image
        or not isinstance(provenance.get("ImageVersion"), str)
        or not provenance["ImageVersion"].strip()
    ):
        errors.append("receipt.runner_provenance: hosted runner identity differs")
    os_build = receipt.get("os_build")
    if not isinstance(os_build, str) or not os_build.startswith(expected_os + "-"):
        errors.append("receipt.os_build: hosted OS build differs")
    checkouts = receipt.get("candidate_checkouts")
    if not isinstance(checkouts, dict):
        errors.append("receipt.candidate_checkouts: exact candidate checkout receipt is absent")
    else:
        for name in ("app", "agent", "runtime", "pinokio"):
            if (checkouts.get(name) or {}).get("sha") != candidate.get(name):
                errors.append(f"receipt.candidate_checkouts.{name}: differs from selected tuple")
    sources = receipt.get("sources")
    installed = receipt.get("installed_tuple")
    if not isinstance(sources, dict) or not isinstance(installed, dict):
        errors.append("receipt: installed/source identity is absent")
    else:
        for name in ("app", "agent", "runtime"):
            if (sources.get(name) or {}).get("sha") != candidate.get(name) or installed.get(name) != candidate.get(name):
                errors.append(f"receipt.{name}: installed/source identity differs from selected tuple")
    if not receipt.get("import_origins"):
        errors.append("receipt.import_origins: installed origins are absent")
    owner = receipt.get("service_manager_prerequisite")
    if not isinstance(owner, dict):
        errors.append("receipt.service_manager_prerequisite: receipt is absent")
        return errors
    if contract["mode"] == "direct":
        if (
            owner.get("required") is not True or owner.get("outcome") != "pass"
            or owner.get("manager") != native.MANAGERS[contract["os"]]
            or not isinstance(owner.get("version"), str) or not owner["version"].strip()
            or owner.get("foreground_fallback") is not False
        ):
            errors.append("receipt: native service-manager prerequisite did not pass")
        manager = receipt.get("service_manager")
        expected_manager = native.MANAGERS[contract["os"]]
        if (
            not isinstance(manager, dict)
            or manager.get("manager") != expected_manager
            or not isinstance(manager.get("version"), str)
            or not manager["version"].strip()
            or manager.get("foreground_fallback") is not False
        ):
            errors.append("receipt: direct service ownership is absent")
        transition = receipt.get("release_transition")
        if not isinstance(transition, dict) or transition.get("from") != 0 or transition.get("to") != 1:
            errors.append("receipt.release_transition: authentic release-0 transition is absent")
        fixture = receipt.get("release0_fixture")
        if (
            not isinstance(fixture, dict)
            or fixture.get("fixture_sums_sha256") != failure.RELEASE0_SUMS_SHA256
            or fixture.get("bootstrap_sha256") != failure.RELEASE0_BOOTSTRAP_SHA256
            or fixture.get("sources") != failure.RELEASE0_REFS
            or any(_DIGEST.fullmatch(str(fixture.get(name, ""))) is None for name in ("manifest_sha256", "provenance_sha256"))
        ):
            errors.append("receipt.release0_fixture: frozen release-0 provenance is absent")
        semantic = receipt.get("semantic_data")
        if (
            not isinstance(semantic, dict) or semantic.get("identical") is not True
            or not _DIGEST.fullmatch(str(semantic.get("before_sha256", "")))
            or semantic.get("before_sha256") != semantic.get("after_sha256")
        ):
            errors.append("receipt.semantic_data: preservation is absent or unequal")
        checks = receipt.get("checks")
        for name in ("release", "service", "health", "semantic_data", "task_settled", "one_runtime", "unrelated_sentinel"):
            if not isinstance(checks, dict) or checks.get(name) != "pass":
                errors.append(f"receipt.checks.{name}: pass required")
    elif (
        owner.get("required") is not False or owner.get("outcome") != "explicit-owner"
        or owner.get("managed_by") != "pinokio" or owner.get("foreground_fallback") is not False
    ):
        errors.append("receipt: Pinokio ownership prerequisite is not explicit")
    return errors


_M4_5_FIELDS = {
    "schema_version", "receipt_kind", "scenario_id", "scenario", "job_id", "channel",
    "mode", "os", "runner", "shell", "status", "evidence_kind",
    "source_override_test_mode", "candidate", "candidate_checkouts", "stand_in",
    "release_transition", "installed", "runner_provenance", "os_build",
    "service_manager_prerequisite", "semantic_data", "checks", "controls", "trigger",
    "actions", "execution_boundary", "final_cleanup", "failure",
}


def _exact_legacy_object(
    value: object, expected_fields: set[str], label: str, errors: list[str]
) -> dict[str, object]:
    if not isinstance(value, dict):
        errors.append(f"{label}: exact object required")
        return {}
    if set(value) != expected_fields:
        missing = expected_fields - set(value)
        unknown = set(value) - expected_fields
        if missing:
            errors.append(f"{label}: missing field(s): {', '.join(sorted(missing))}")
        if unknown:
            errors.append(f"{label}: unknown field(s): {', '.join(sorted(unknown))}")
    return value


def _m4_5_nested_errors(receipt: Mapping[str, object], scenario: Mapping[str, object]) -> list[str]:
    """Close the legacy M4-5 nested shape without treating it as authority."""
    errors: list[str] = []
    _exact_legacy_object(
        receipt.get("runner_provenance"), {"RUNNER_OS", "ImageOS", "ImageVersion"},
        "receipt.runner_provenance", errors,
    )
    candidate = _exact_legacy_object(
        receipt.get("candidate"), {"app", "agent", "runtime", "pinokio"}, "receipt.candidate", errors,
    )
    checkouts = _exact_legacy_object(
        receipt.get("candidate_checkouts"), {"app", "agent", "runtime", "pinokio"},
        "receipt.candidate_checkouts", errors,
    )
    for name in ("app", "agent", "runtime", "pinokio"):
        _exact_legacy_object(checkouts.get(name), {"sha"}, f"receipt.candidate_checkouts.{name}", errors)
    _exact_legacy_object(
        receipt.get("installed"),
        {"release", "qualification_marker", "manifest_sha256", "agent", "runtime", "schema_digest"},
        "receipt.installed", errors,
    )
    _exact_legacy_object(
        receipt.get("semantic_data"), {"identical", "before_sha256", "after_sha256"},
        "receipt.semantic_data", errors,
    )
    mode = scenario["mode"]
    owner_fields = (
        {"required", "outcome", "manager", "version", "foreground_fallback"}
        if mode == "direct" else
        {"required", "outcome", "managed_by", "foreground_fallback"}
    )
    _exact_legacy_object(receipt.get("service_manager_prerequisite"), owner_fields, "receipt.service_manager_prerequisite", errors)
    required_checks = {
        "release", "marker", "service", "health", "semantic_data", "task_settled", "one_runtime",
        "unrelated_sentinel",
    }
    channel = scenario["channel"]
    if channel == "button":
        required_checks.update({"native_one_shot", "lost_ack_controls"})
        if scenario["os"] == "linux":
            required_checks.add("draft_controls")
    elif channel == "install-line":
        required_checks.add("install_line")
    else:
        required_checks.add("pinokio_update")
    _exact_legacy_object(receipt.get("checks"), required_checks, "receipt.checks", errors)
    trigger = receipt.get("trigger")
    if channel == "install-line":
        _exact_legacy_object(
            trigger,
            {"channel", "entrypoint", "update_flag", "manifest_sha256", "root_bound", "data_root_bound"},
            "receipt.trigger", errors,
        )
    elif channel == "pinokio-update":
        trigger_row = _exact_legacy_object(
            trigger,
            {"channel", "entrypoint", "managed_by", "mode", "route", "foreground_fallback", "action_provenance"},
            "receipt.trigger", errors,
        )
        _exact_legacy_object(
            trigger_row.get("action_provenance"), {"pinokio_sha", "action_sha256"},
            "receipt.trigger.action_provenance", errors,
        )
    else:
        trigger_row = _exact_legacy_object(
            trigger, {"channel", "entrypoint", "request", "draft_flush", "native_job"}, "receipt.trigger", errors,
        )
        _exact_legacy_object(
            trigger_row.get("native_job"),
            {"format", "system", "one_shot", "owned", "acknowledged", "operation_id"},
            "receipt.trigger.native_job", errors,
        )
    if not isinstance(receipt.get("controls"), list):
        errors.append("receipt.controls: list required")
    if not isinstance(receipt.get("actions"), list):
        errors.append("receipt.actions: list required")
    if not isinstance(receipt.get("execution_boundary"), dict):
        errors.append("receipt.execution_boundary: object required")
    return errors


def parse_release1_evidence(evidence: object) -> tuple[ParsedEvidence | None, list[str]]:
    """Parse one selected Release-1 receipt; do not discover the nine-row set."""
    parsed, errors = _parse_selected(
        evidence,
        selector_name="job_id",
        accepted_selectors=set(matrix.RELEASE1_JOB_IDS),
        require_candidate=True,
        receipt_path=_RECEIPT_PATH,
        required_paths={_RECEIPT_PATH},
    )
    if parsed is not None and parsed.candidate is not None:
        errors.extend(_release1_errors(parsed.receipt, parsed.candidate, parsed.selector))
        try:
            expected_owner = "native" if matrix.release1_job(parsed.selector)["mode"] == "direct" else "pinokio"
        except ValueError:  # selector error is already reported above
            pass
        else:
            errors.extend(_producer_owner_errors(parsed.producer, expected_owner, "Release-1"))
    return parsed, errors


def parse_m4_5_evidence(evidence: object) -> tuple[ParsedEvidence | None, list[str]]:
    """Parse one selected M4-5 receipt with its established trigger semantics."""
    parsed, errors = _parse_selected(
        evidence,
        selector_name="scenario_id",
        accepted_selectors=set(release2.RELEASE2_SCENARIO_IDS),
        require_candidate=False,
        receipt_path=_RECEIPT_PATH,
        required_paths={_RECEIPT_PATH},
    )
    if parsed is None:
        return parsed, errors
    errors.extend(_unknown_fields(parsed.receipt, _M4_5_FIELDS, "receipt"))
    if parsed.receipt.get("scenario_id") != parsed.selector:
        errors.append("receipt.scenario_id: differs from selected obligation")
    try:
        release2.validate_receipt(parsed.receipt)
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        errors.append(f"receipt: malformed M4-5 observation: {exc}")
    try:
        definition = release2.scenario_definition(parsed.selector)
    except ValueError:  # selector error is already reported above
        pass
    else:
        errors.extend(_m4_5_nested_errors(parsed.receipt, definition))
        expected_owner = "native" if definition["mode"] == "direct" else "pinokio"
        errors.extend(_producer_owner_errors(parsed.producer, expected_owner, "M4-5"))
    return parsed, errors


_M4_6_FIELDS = {
    "schema_version", "receipt_kind", "receipt_state", "status", "verification", "checks",
    "slot_id", "job_id", "mode", "os", "runner_image", "runner_provenance", "os_build",
    "shell", "source_override_test_mode", "candidate_checkouts", "manifest_sha256",
    "release0_fixture", "evidence_files", "scenarios", "execution_scope",
    "hosted_authentication", "collector_provenance", "collector_structural_sha256", "owner",
    "native_service",
}
_M4_6_SIDECAR_FIELDS = {
    "execution_scope", "slot_id", "scenario", "collector_provenance",
    "collector_structural_sha256", "observation", "native_instances",
    "sanitized_native_manager_observations",
}


def _m4_6_receipt_state_errors(
    receipt: Mapping[str, object], candidate: Mapping[str, object], slot_id: str
) -> list[str]:
    """Keep the receipt/sidecar refusal boundary separate from raw semantics.

    The historic native structural object is producer-supplied diagnostic data.
    Once an M-CI-bound R-30 semantic observation is present, its custody and
    scenario facts are recomputed by :func:`semantic_observation_errors`
    instead.  Receipt state, all four named sidecars, and their selected-slot
    bindings remain mandatory and fail closed here.
    """
    errors: list[str] = []
    if receipt.get("schema_version") != failure.SCHEMA_VERSION:
        errors.append("receipt.schema_version: unsupported failure receipt schema")
    if receipt.get("receipt_kind") != failure.RECEIPT_KIND:
        errors.append("receipt.receipt_kind: expected failure-mode receipt")
    if receipt.get("receipt_state") != "recorded":
        errors.append("receipt.receipt_state: recorded required")
    if receipt.get("slot_id") != slot_id or receipt.get("job_id") != slot_id:
        errors.append("receipt.slot_id: differs from selected obligation")
    if receipt.get("status") != "pass":
        errors.append(f"receipt.status: {receipt.get('status')!r} is not pass")
    verification = receipt.get("verification")
    if (
        not isinstance(verification, dict)
        or verification.get("state") != "verified-pass"
        or verification.get("failed_assertions") != []
    ):
        errors.append("receipt.verification: clean verified-pass required")
    checks = receipt.get("checks")
    if (
        not isinstance(checks, dict)
        or set(checks) != set(failure.SCENARIOS)
        or any(checks.get(name) != "pass" for name in failure.SCENARIOS)
    ):
        errors.append("receipt.checks: every named scenario must pass")
    checkouts = receipt.get("candidate_checkouts")
    if not isinstance(checkouts, dict):
        errors.append("receipt.candidate_checkouts: frozen tuple bindings required")
    else:
        for name in ("app", "agent", "runtime", "pinokio"):
            if (checkouts.get(name) or {}).get("sha") != candidate.get(name):
                errors.append(f"receipt.candidate_checkouts.{name}: differs from selected tuple")
    if receipt.get("manifest_sha256") != candidate.get("manifest_sha256"):
        errors.append("receipt.manifest_sha256: differs from selected tuple")
    return errors


def _same_json(left: object, right: object) -> bool:
    try:
        return json.dumps(left, sort_keys=True, separators=(",", ":"), allow_nan=False) == json.dumps(
            right, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError):
        return False


def parse_m4_6_evidence(evidence: object) -> tuple[ParsedEvidence | None, list[str]]:
    """Parse one M4-6 receipt and all four declared machine-observation sidecars."""
    required_paths = {failure.RECEIPT_NAME} | {f"evidence/{name}.json" for name in failure.SCENARIOS}
    parsed, errors = _parse_selected(
        evidence,
        selector_name="slot_id",
        accepted_selectors=set(failure.REQUIRED_SLOT_IDS),
        require_candidate=True,
        receipt_path=failure.RECEIPT_NAME,
        required_paths=required_paths,
    )
    if parsed is None or parsed.candidate is None:
        return parsed, errors
    receipt = parsed.receipt
    errors.extend(_unknown_fields(receipt, _M4_6_FIELDS, "receipt"))
    errors.extend(_m4_6_receipt_state_errors(receipt, parsed.candidate, parsed.selector))
    # Preserve the legacy structural refusal for the old receipt-only route.
    # A complete M-CI context with a producer envelope instead has its native
    # custody and four-scenario semantics recomputed from the linked raw
    # producer observation below; those producer claims are not duplicated as
    # a second authority inside the legacy receipt.
    if parsed.authenticated_context is None or parsed.producer is None:
        try:
            errors.extend(failure.validate_receipt(receipt, parsed.candidate, parsed.selector))
            errors.extend(native._native_structural_errors(receipt))
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError) as exc:
            errors.append(f"receipt: malformed native observation: {exc}")
    declarations = receipt.get("evidence_files")
    if not isinstance(declarations, list):
        declarations = []
        errors.append("receipt.evidence_files: declaration list is required")
    seen_paths: set[str] = set()
    seen_scenarios: set[str] = set()
    scenarios = receipt.get("scenarios")
    for number, declaration in enumerate(declarations):
        prefix = f"receipt.evidence_files[{number}]"
        if not isinstance(declaration, dict):
            errors.append(f"{prefix}: malformed declaration")
            continue
        path, scenario = declaration.get("path"), declaration.get("scenario")
        if not isinstance(path, str) or not isinstance(scenario, str):
            errors.append(f"{prefix}: path and scenario required")
            continue
        if path in seen_paths or scenario in seen_scenarios:
            errors.append(f"{prefix}: duplicate path or scenario")
        seen_paths.add(path)
        seen_scenarios.add(scenario)
        if scenario not in failure.SCENARIOS or path != f"evidence/{scenario}.json":
            errors.append(f"{prefix}: scenario/path binding differs")
            continue
        entry = parsed.files.get(path, {})
        if declaration.get("sha256") != entry.get("sha256"):
            errors.append(f"{path}: index SHA-256 differs from receipt")
        payload = entry.get("json")
        if not isinstance(payload, dict):
            continue
        errors.extend(_unknown_fields(payload, _M4_6_SIDECAR_FIELDS, path))
        expected_observation = scenarios.get(scenario) if isinstance(scenarios, dict) else None
        if (
            payload.get("slot_id") != parsed.selector
            or payload.get("scenario") != scenario
            or payload.get("execution_scope") != receipt.get("execution_scope")
            or not _same_json(payload.get("observation"), expected_observation)
            or not _same_json(payload.get("collector_provenance"), receipt.get("collector_provenance"))
            or payload.get("collector_structural_sha256") != receipt.get("collector_structural_sha256")
        ):
            errors.append(f"{path}: parsed observation differs from receipt or selected slot")
        service = receipt.get("native_service")
        instances = service.get("instances") if isinstance(service, dict) else None
        expected_instances = (
            [
                item for item in instances
                if isinstance(item, dict) and str(item.get("scenario", "")).split(".", 1)[0] == scenario
            ]
            if isinstance(instances, list) else None
        )
        raw_observations = service.get("raw_observations") if isinstance(service, dict) else None
        if (
            not _same_json(payload.get("native_instances"), expected_instances)
            or not _same_json(payload.get("sanitized_native_manager_observations"), raw_observations)
        ):
            errors.append(f"{path}: native observations differ from receipt")
    if seen_scenarios != set(failure.SCENARIOS):
        errors.append("receipt.evidence_files: all four distinct scenarios are required")
    try:
        expected_owner = "native" if failure.SLOT_CONTRACTS[parsed.selector]["mode"] == "direct" else "pinokio"
    except KeyError:  # selector error is already reported above
        pass
    else:
        errors.extend(_producer_owner_errors(parsed.producer, expected_owner, "M4-6"))
    return parsed, errors


# The following profile is intentionally a consumer of M-CI's authenticated
# index, not an authenticator.  ``authenticated_context`` is constructed by
# M-CI only after its frozen-tuple, selected-attempt, immutable-byte, and
# transport/finalization checks.  These checks recompute the journey-specific
# relations between that context, the R-30 producer envelope, and the parsed
# machine observations.
_AUTHENTICATED_CONTEXT_FIELDS = {
    "kind",
    "version",
    "family",
    "selector_name",
    "selector_value",
    "candidate",
    "binding",
    "producer_export_sha256",
    "collector_seal_sha256",
    "observation_ids",
    "state",
}
_DIRECT_OBSERVATION_ID = "direct-custody"
_DIRECT_OBSERVATION_PATH = "observations/direct-custody.json"
_PINOKIO_OBSERVATION_ID = "pinokio-settlement"
_PINOKIO_OBSERVATION_PATH = "observations/pinokio-settlement.json"


def _semantic_owner_and_selector(
    family: str, selector: str
) -> tuple[str, str] | None:
    """Return the required owner and selector field for one family member."""
    try:
        if family == "release1":
            return (
                "native" if matrix.release1_job(selector)["mode"] == "direct" else "pinokio",
                "job_id",
            )
        if family == "m4_5":
            return (
                "native" if release2.scenario_definition(selector)["mode"] == "direct" else "pinokio",
                "scenario_id",
            )
        if family == "m4_6":
            return (
                "native" if failure.SLOT_CONTRACTS[selector]["mode"] == "direct" else "pinokio",
                "slot_id",
            )
    except (KeyError, ValueError):
        return None
    return None


def _context_and_producer_errors(
    parsed: ParsedEvidence, family: str
) -> tuple[dict[str, object] | None, str | None, list[str]]:
    """Cross-bind the M-CI context to one finalized R-30 producer pair."""
    errors: list[str] = []
    expected = _semantic_owner_and_selector(family, parsed.selector)
    if expected is None:
        return None, None, [f"authenticated_context: unknown {family!r} selector"]
    owner_kind, selector_name = expected
    producer = parsed.producer
    if producer is None:
        return None, owner_kind, [
            "authenticated_context: producer export and collector seal are absent"
        ]
    if parsed.authenticated_context is None:
        return None, owner_kind, [
            "authenticated_context: M-CI finalized authenticated context is absent"
        ]
    context = _r30_exact_object(
        parsed.authenticated_context,
        _AUTHENTICATED_CONTEXT_FIELDS,
        "authenticated_context",
        errors,
    )
    for name, value in (
        ("kind", "mci-authenticated-context"),
        ("version", 1),
        ("family", family),
        ("selector_name", selector_name),
        ("selector_value", parsed.selector),
        ("state", "finalized"),
    ):
        if context.get(name) != value:
            errors.append(f"authenticated_context.{name}: expected {value!r}")
    context_candidate = context.get("candidate")
    if not isinstance(context_candidate, dict):
        errors.append("authenticated_context.candidate: frozen candidate object required")
    else:
        errors.extend(_candidate_errors(context_candidate))
    if parsed.candidate is None:
        errors.append("authenticated_context: selected top-level candidate is required")
    elif not _same_json(context_candidate, parsed.candidate):
        errors.append("authenticated_context.candidate: differs from selected candidate")
    context_binding = _parse_r30_binding(
        context.get("binding"), "authenticated_context.binding", errors
    )
    if not _same_json(context_binding, producer.export.get("binding")):
        errors.append("authenticated_context.binding: differs from producer export")
    if not _same_json(context_binding, producer.seal.get("binding")):
        errors.append("authenticated_context.binding: differs from collector seal")
    for field, actual in (
        ("producer_export_sha256", producer.export_sha256),
        ("collector_seal_sha256", producer.seal_sha256),
    ):
        _r30_digest(context.get(field), f"authenticated_context.{field}", errors)
        if context.get(field) != actual:
            errors.append(f"authenticated_context.{field}: differs from selected producer record")
    ids = context.get("observation_ids")
    export_ids = [
        item.get("id") for item in producer.export.get("observations", [])
        if isinstance(item, dict)
    ]
    if (
        not isinstance(ids, list)
        or any(not _r30_id(item) for item in ids)
        or ids != sorted(ids)
        or len(ids) != len(set(ids))
        or ids != sorted(export_ids)
    ):
        errors.append(
            "authenticated_context.observation_ids: exact sorted producer observation IDs required"
        )
    if producer.export.get("owner_kind") != owner_kind:
        errors.append(
            f"authenticated_context: {family} selector requires {owner_kind!r} producer ownership"
        )
    return context, owner_kind, errors


def _producer_file_errors(parsed: ParsedEvidence) -> list[str]:
    """Bind indexed parsed producer bytes to the R-30 export file list."""
    errors: list[str] = []
    if parsed.producer is None:
        return errors
    declared = parsed.producer.export.get("files")
    observations = parsed.producer.export.get("observations")
    if not isinstance(declared, list) or not isinstance(observations, list):
        return ["producer: malformed export file or observation list"]
    declared_by_path = {
        item.get("path"): item for item in declared
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    if set(parsed.producer_files) != set(declared_by_path):
        errors.append("producer_files: must contain exactly the R-30 export file set")
    for path, declaration in declared_by_path.items():
        indexed = parsed.producer_files.get(path)
        if not isinstance(indexed, dict):
            errors.append(f"producer_files: missing indexed producer file {path!r}")
            continue
        if indexed.get("sha256") != declaration.get("sha256"):
            errors.append(f"producer_files[{path!r}]: SHA-256 differs from R-30 file declaration")
        if not isinstance(indexed.get("json"), dict):
            errors.append(f"producer_files[{path!r}]: parsed observation object required")
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        path = observation.get("file")
        indexed = parsed.producer_files.get(path)
        if not isinstance(indexed, dict):
            continue
        if indexed.get("sha256") != observation.get("sha256"):
            errors.append(
                f"producer observation {observation.get('id')!r}: indexed SHA-256 differs"
            )
    return errors


def _require_digest(value: object, label: str, errors: list[str]) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        errors.append(f"{label}: lowercase SHA-256 required")


def _require_id(value: object, label: str, errors: list[str]) -> None:
    if not _r30_id(value):
        errors.append(f"{label}: nonempty bounded ID required")


def _exact_fields(
    value: object, fields: set[str], label: str, errors: list[str]
) -> dict[str, object]:
    return _r30_exact_object(value, fields, label, errors)


def _custody_errors(
    value: object, principal_id: object, label: str
) -> list[str]:
    """Validate loss-detecting process/removal/final-census semantics."""
    errors: list[str] = []
    custody = _exact_fields(
        value,
        {
            "principal_id", "controller", "descendants", "listeners", "roots", "locks",
            "authorized_removal", "removed", "final_census",
        },
        label,
        errors,
    )
    if custody.get("principal_id") != principal_id:
        errors.append(f"{label}.principal_id: differs from owner principal")
    controller = _exact_fields(
        custody.get("controller"),
        {"incarnation", "executable_sha256", "birth_sequence", "reaped"},
        f"{label}.controller",
        errors,
    )
    controller_id = controller.get("incarnation")
    _require_id(controller_id, f"{label}.controller.incarnation", errors)
    _require_digest(controller.get("executable_sha256"), f"{label}.controller.executable_sha256", errors)
    _r30_positive_integer(controller.get("birth_sequence"), f"{label}.controller.birth_sequence", errors)
    if controller.get("reaped") is not True:
        errors.append(f"{label}.controller.reaped: true required")
    descendants = custody.get("descendants")
    if not isinstance(descendants, list) or not descendants:
        errors.append(f"{label}.descendants: nonempty list required")
        descendants = []
    seen = {controller_id} if isinstance(controller_id, str) else set()
    identities = [controller_id] if isinstance(controller_id, str) else []
    births = [controller.get("birth_sequence")] if type(controller.get("birth_sequence")) is int else []
    for number, value in enumerate(descendants):
        row = _exact_fields(
            value,
            {"incarnation", "parent_incarnation", "executable_sha256", "birth_sequence", "reaped"},
            f"{label}.descendants[{number}]",
            errors,
        )
        incarnation = row.get("incarnation")
        _require_id(incarnation, f"{label}.descendants[{number}].incarnation", errors)
        _require_digest(row.get("executable_sha256"), f"{label}.descendants[{number}].executable_sha256", errors)
        _r30_positive_integer(row.get("birth_sequence"), f"{label}.descendants[{number}].birth_sequence", errors)
        if row.get("parent_incarnation") not in seen:
            errors.append(f"{label}.descendants[{number}].parent_incarnation: known earlier lineage required")
        if incarnation in seen:
            errors.append(f"{label}.descendants[{number}].incarnation: duplicate/PID-reused identity")
        if births and type(row.get("birth_sequence")) is int and row["birth_sequence"] <= max(births):
            errors.append(f"{label}.descendants[{number}].birth_sequence: strict lineage order required")
        if row.get("reaped") is not True:
            errors.append(f"{label}.descendants[{number}].reaped: true required")
        if isinstance(incarnation, str):
            seen.add(incarnation)
            identities.append(incarnation)
        if type(row.get("birth_sequence")) is int:
            births.append(row["birth_sequence"])
    for name in ("listeners", "roots", "locks"):
        rows = custody.get(name)
        if not isinstance(rows, list) or not rows:
            errors.append(f"{label}.{name}: nonempty observed-owner list required")
            continue
        observed: set[object] = set()
        for number, row_value in enumerate(rows):
            row = _exact_fields(
                row_value,
                {"owner_incarnation", "identity_sha256"},
                f"{label}.{name}[{number}]",
                errors,
            )
            owner = row.get("owner_incarnation")
            if owner not in seen:
                errors.append(f"{label}.{name}[{number}].owner_incarnation: owned lineage required")
            _require_digest(row.get("identity_sha256"), f"{label}.{name}[{number}].identity_sha256", errors)
            key = (owner, row.get("identity_sha256"))
            if key in observed:
                errors.append(f"{label}.{name}[{number}]: duplicate owner observation")
            observed.add(key)
    for name in ("authorized_removal", "removed"):
        if custody.get(name) != identities:
            errors.append(f"{label}.{name}: exact controller-and-descendant set required")
    census = _exact_fields(
        custody.get("final_census"),
        {
            "absent_incarnations", "manager_absent", "no_owned_listeners",
            "no_owned_roots", "no_owned_locks", "observer_loss", "fork_or_reparent_loss",
        },
        f"{label}.final_census",
        errors,
    )
    if census.get("absent_incarnations") != identities:
        errors.append(f"{label}.final_census.absent_incarnations: exact removal set required")
    for name in (
        "manager_absent", "no_owned_listeners", "no_owned_roots", "no_owned_locks",
    ):
        if census.get(name) is not True:
            errors.append(f"{label}.final_census.{name}: true required")
    for name in ("observer_loss", "fork_or_reparent_loss"):
        if census.get(name) != 0:
            errors.append(f"{label}.final_census.{name}: zero required")
    return errors


def _semantic_header_errors(
    payload: object,
    *,
    fields: set[str],
    kind: str,
    owner_kind: str,
    selector_name: str,
    selector: str,
    candidate: Mapping[str, object],
    label: str,
) -> tuple[dict[str, object], list[str]]:
    errors: list[str] = []
    row = _exact_fields(payload, fields, label, errors)
    for name, value in (
        ("kind", kind),
        ("version", 1),
        ("owner_kind", owner_kind),
        ("selector_name", selector_name),
        ("selector_value", selector),
    ):
        if row.get(name) != value:
            errors.append(f"{label}.{name}: expected {value!r}")
    expected_candidate = canonical_json_sha256(dict(candidate))
    if row.get("candidate_sha256") != expected_candidate:
        errors.append(f"{label}.candidate_sha256: differs from selected frozen candidate")
    return row, errors


def _direct_semantic_errors(
    payload: object, parsed: ParsedEvidence, selector_name: str
) -> list[str]:
    if parsed.candidate is None:
        return ["direct observation: selected candidate is absent"]
    fields = {
        "kind", "version", "owner_kind", "selector_name", "selector_value", "candidate_sha256",
        "manager", "custody", "lifecycle", "semantic_data", "m4_6",
    }
    row, errors = _semantic_header_errors(
        payload,
        fields=fields,
        kind="direct-custody/v1",
        owner_kind="native",
        selector_name=selector_name,
        selector=parsed.selector,
        candidate=parsed.candidate,
        label="direct observation",
    )
    try:
        if selector_name == "job_id":
            os_name = matrix.release1_job(parsed.selector)["os"]
        elif selector_name == "scenario_id":
            os_name = release2.scenario_definition(parsed.selector)["os"]
        else:
            os_name = failure.SLOT_CONTRACTS[parsed.selector]["os"]
    except (KeyError, ValueError):
        return [*errors, "direct observation: selected OS is unavailable"]
    manager = _exact_fields(
        row.get("manager"),
        {"name", "version", "definition_sha256", "action_sha256", "principal_id", "foreground_fallback"},
        "direct observation.manager",
        errors,
    )
    if manager.get("name") != native.MANAGERS[os_name]:
        errors.append("direct observation.manager.name: differs from selected OS manager")
    _require_id(manager.get("version"), "direct observation.manager.version", errors)
    _require_digest(manager.get("definition_sha256"), "direct observation.manager.definition_sha256", errors)
    _require_digest(manager.get("action_sha256"), "direct observation.manager.action_sha256", errors)
    _require_id(manager.get("principal_id"), "direct observation.manager.principal_id", errors)
    if manager.get("foreground_fallback") is not False:
        errors.append("direct observation.manager.foreground_fallback: false required")
    errors.extend(_custody_errors(row.get("custody"), manager.get("principal_id"), "direct observation.custody"))
    lifecycle = _exact_fields(
        row.get("lifecycle"),
        {"operation_id", "journal_sha256", "terminal", "health", "cleanup"},
        "direct observation.lifecycle",
        errors,
    )
    _require_id(lifecycle.get("operation_id"), "direct observation.lifecycle.operation_id", errors)
    _require_digest(lifecycle.get("journal_sha256"), "direct observation.lifecycle.journal_sha256", errors)
    for name in ("terminal", "health", "cleanup"):
        if lifecycle.get(name) != "pass":
            errors.append(f"direct observation.lifecycle.{name}: pass required")
    semantic = _exact_fields(
        row.get("semantic_data"),
        {"before_sha256", "after_sha256", "entities", "unrelated_sentinel_sha256"},
        "direct observation.semantic_data",
        errors,
    )
    _require_digest(semantic.get("before_sha256"), "direct observation.semantic_data.before_sha256", errors)
    if semantic.get("after_sha256") != semantic.get("before_sha256"):
        errors.append("direct observation.semantic_data: aggregate preservation differs")
    entities = semantic.get("entities")
    if not isinstance(entities, dict) or set(entities) != {"project", "document", "preferences", "cas"}:
        errors.append("direct observation.semantic_data.entities: exact protected entity set required")
    else:
        for name, entity_value in entities.items():
            entity = _exact_fields(
                entity_value,
                {"before_sha256", "after_sha256"},
                f"direct observation.semantic_data.entities.{name}",
                errors,
            )
            _require_digest(entity.get("before_sha256"), f"direct observation.semantic_data.entities.{name}.before_sha256", errors)
            if entity.get("after_sha256") != entity.get("before_sha256"):
                errors.append(f"direct observation.semantic_data.entities.{name}: preservation differs")
    _require_digest(
        semantic.get("unrelated_sentinel_sha256"),
        "direct observation.semantic_data.unrelated_sentinel_sha256",
        errors,
    )
    errors.extend(_m4_6_semantic_errors(row.get("m4_6"), "direct observation.m4_6"))
    return errors


def _pinokio_stop_case_errors(value: object, outcome: str, label: str) -> list[str]:
    errors: list[str] = []
    row = _exact_fields(
        value,
        {
            "precondition_sha256", "injection_profile_sha256", "controller_incarnation",
            "outcome", "no_write", "no_download", "no_admission", "no_retry",
            "protected_state_sha256",
        },
        label,
        errors,
    )
    for name in ("precondition_sha256", "injection_profile_sha256", "protected_state_sha256"):
        _require_digest(row.get(name), f"{label}.{name}", errors)
    _require_id(row.get("controller_incarnation"), f"{label}.controller_incarnation", errors)
    if row.get("outcome") != outcome:
        errors.append(f"{label}.outcome: expected {outcome!r}")
    for name in ("no_write", "no_download", "no_admission", "no_retry"):
        if row.get(name) is not True:
            errors.append(f"{label}.{name}: true required")
    return errors


def _m4_6_semantic_errors(value: object, label: str) -> list[str]:
    """Require the four independent M4-6 groups from raw producer facts."""
    errors: list[str] = []
    row = _exact_fields(
        value,
        {
            "interruption_recovery", "failed_candidate_rollback", "keep_data_uninstall",
            "stop_safety",
        },
        label,
        errors,
    )
    interruption = _exact_fields(
        row.get("interruption_recovery"),
        {"operation_id", "terminal", "journal_sha256", "same_operation"},
        f"{label}.interruption_recovery",
        errors,
    )
    _require_id(interruption.get("operation_id"), f"{label}.interruption_recovery.operation_id", errors)
    _require_digest(interruption.get("journal_sha256"), f"{label}.interruption_recovery.journal_sha256", errors)
    if interruption.get("terminal") != "recovered" or interruption.get("same_operation") is not True:
        errors.append(f"{label}.interruption_recovery: exact same-operation recovery required")
    rollback = _exact_fields(
        row.get("failed_candidate_rollback"),
        {"release0_fixture_sha256", "rollback", "data_sha256"},
        f"{label}.failed_candidate_rollback",
        errors,
    )
    for name in ("release0_fixture_sha256", "data_sha256"):
        _require_digest(rollback.get(name), f"{label}.failed_candidate_rollback.{name}", errors)
    if rollback.get("rollback") != "pass":
        errors.append(f"{label}.failed_candidate_rollback.rollback: pass required")
    uninstall = _exact_fields(
        row.get("keep_data_uninstall"),
        {"cleanup", "data_sha256", "no_owned_residue"},
        f"{label}.keep_data_uninstall",
        errors,
    )
    _require_digest(uninstall.get("data_sha256"), f"{label}.keep_data_uninstall.data_sha256", errors)
    if uninstall.get("cleanup") != "pass" or uninstall.get("no_owned_residue") is not True:
        errors.append(f"{label}.keep_data_uninstall: cleanup and no-residue pass required")
    stop = _exact_fields(row.get("stop_safety"), {"refused", "unknown"}, f"{label}.stop_safety", errors)
    errors.extend(_pinokio_stop_case_errors(stop.get("refused"), "refused", f"{label}.stop_safety.refused"))
    errors.extend(_pinokio_stop_case_errors(stop.get("unknown"), "unknown", f"{label}.stop_safety.unknown"))
    return errors


def _pinokio_semantic_errors(
    payload: object, parsed: ParsedEvidence, selector_name: str
) -> list[str]:
    if parsed.candidate is None:
        return ["Pinokio observation: selected candidate is absent"]
    fields = {
        "kind", "version", "owner_kind", "selector_name", "selector_value", "candidate_sha256",
        "engine", "settlement", "stop_safety", "restoration", "custody", "m4_6",
    }
    row, errors = _semantic_header_errors(
        payload,
        fields=fields,
        kind="pinokio-settlement/v1",
        owner_kind="pinokio",
        selector_name=selector_name,
        selector=parsed.selector,
        candidate=parsed.candidate,
        label="Pinokio observation",
    )
    engine = _exact_fields(
        row.get("engine"),
        {
            "daemon_sha256", "pterm_sha256", "instance_id", "boot_id", "app_id", "resource_id",
            "root_sha256", "launcher_sha256", "action_sha256", "helper_graph_sha256",
            "principal_id", "pinokio_sha",
        },
        "Pinokio observation.engine",
        errors,
    )
    for name in (
        "daemon_sha256", "pterm_sha256", "root_sha256", "launcher_sha256", "action_sha256",
        "helper_graph_sha256",
    ):
        _require_digest(engine.get(name), f"Pinokio observation.engine.{name}", errors)
    for name in ("instance_id", "boot_id", "app_id", "resource_id", "principal_id"):
        _require_id(engine.get(name), f"Pinokio observation.engine.{name}", errors)
    if engine.get("pinokio_sha") != parsed.candidate.get("pinokio"):
        errors.append("Pinokio observation.engine.pinokio_sha: differs from selected candidate")
    settlement = _exact_fields(
        row.get("settlement"),
        {
            "request_id", "execution_id", "operation_id", "journal_sha256", "events", "terminal",
            "readiness_sha256", "process_census_sha256",
        },
        "Pinokio observation.settlement",
        errors,
    )
    for name in ("request_id", "execution_id", "operation_id"):
        _require_id(settlement.get(name), f"Pinokio observation.settlement.{name}", errors)
    for name in ("journal_sha256", "readiness_sha256", "process_census_sha256"):
        _require_digest(settlement.get(name), f"Pinokio observation.settlement.{name}", errors)
    events = settlement.get("events")
    expected_kinds = ["accepted", "running", "terminal"]
    if not isinstance(events, list) or len(events) != len(expected_kinds):
        errors.append("Pinokio observation.settlement.events: one ordered accepted/running/terminal chain required")
        events = []
    terminal_event: dict[str, object] = {}
    for number, expected_kind in enumerate(expected_kinds):
        item = events[number] if number < len(events) else None
        event = _exact_fields(
            item,
            {"sequence", "kind", "execution_id", "outcome", "source_sha256"},
            f"Pinokio observation.settlement.events[{number}]",
            errors,
        )
        if event.get("sequence") != number + 1:
            errors.append(f"Pinokio observation.settlement.events[{number}].sequence: contiguous order required")
        if event.get("kind") != expected_kind or event.get("execution_id") != settlement.get("execution_id"):
            errors.append(f"Pinokio observation.settlement.events[{number}]: wrong kind or execution binding")
        if expected_kind == "terminal":
            if event.get("outcome") != "success":
                errors.append("Pinokio observation.settlement terminal outcome: success required")
            _require_digest(event.get("source_sha256"), "Pinokio observation.settlement terminal source", errors)
            terminal_event = event
        elif event.get("outcome") is not None or event.get("source_sha256") is not None:
            errors.append(f"Pinokio observation.settlement.events[{number}]: only terminal may carry outcome/source")
    terminal = _exact_fields(
        settlement.get("terminal"),
        {"execution_id", "outcome", "source_sha256", "sequence"},
        "Pinokio observation.settlement.terminal",
        errors,
    )
    if (
        terminal.get("execution_id") != settlement.get("execution_id")
        or terminal.get("outcome") != "success"
        or terminal.get("sequence") != 3
        or terminal.get("source_sha256") != terminal_event.get("source_sha256")
    ):
        errors.append("Pinokio observation.settlement.terminal: must exactly bind the one engine terminal")
    stop_safety = _exact_fields(
        row.get("stop_safety"), {"refused", "unknown"}, "Pinokio observation.stop_safety", errors
    )
    errors.extend(_pinokio_stop_case_errors(stop_safety.get("refused"), "refused", "Pinokio observation.stop_safety.refused"))
    errors.extend(_pinokio_stop_case_errors(stop_safety.get("unknown"), "unknown", "Pinokio observation.stop_safety.unknown"))
    restoration = _exact_fields(
        row.get("restoration"),
        {
            "seam_removed", "callbacks_fenced", "controller_incarnation", "transport_sha256",
            "checkout_sha", "runtime_sha", "readiness_sha256", "data_sha256", "sentinel_sha256", "cleanup",
        },
        "Pinokio observation.restoration",
        errors,
    )
    if restoration.get("seam_removed") is not True or restoration.get("callbacks_fenced") is not True:
        errors.append("Pinokio observation.restoration: seam removal and callback fencing required")
    _require_id(restoration.get("controller_incarnation"), "Pinokio observation.restoration.controller_incarnation", errors)
    for name in ("transport_sha256", "data_sha256", "sentinel_sha256"):
        _require_digest(restoration.get(name), f"Pinokio observation.restoration.{name}", errors)
    if restoration.get("checkout_sha") != parsed.candidate.get("pinokio"):
        errors.append("Pinokio observation.restoration.checkout_sha: selected Pinokio checkout required")
    if restoration.get("runtime_sha") != parsed.candidate.get("runtime"):
        errors.append("Pinokio observation.restoration.runtime_sha: selected Runtime checkout required")
    if restoration.get("readiness_sha256") != settlement.get("readiness_sha256"):
        errors.append("Pinokio observation.restoration.readiness_sha256: differs from settled readiness")
    if restoration.get("cleanup") != "pass":
        errors.append("Pinokio observation.restoration.cleanup: pass required")
    errors.extend(_custody_errors(row.get("custody"), engine.get("principal_id"), "Pinokio observation.custody"))
    errors.extend(_m4_6_semantic_errors(row.get("m4_6"), "Pinokio observation.m4_6"))
    m4_6 = row.get("m4_6")
    if not isinstance(m4_6, dict) or not _same_json(m4_6.get("stop_safety"), row.get("stop_safety")):
        errors.append("Pinokio observation.m4_6.stop_safety: differs from settlement stop safety")
    return errors


def semantic_observation_errors(parsed: ParsedEvidence | None, family: str) -> list[str]:
    """Recompute R-30 §5 direct/Pinokio semantics for one selected input.

    The caller must be M-CI's already-authenticated evaluator.  This function
    only rejects mismatched or incomplete facts; it never reads files, chooses
    an attempt, verifies signatures, or treats a producer claim as authority.
    """
    if parsed is None:
        return ["authenticated_context: selected evidence could not be parsed"]
    context, owner_kind, errors = _context_and_producer_errors(parsed, family)
    if context is None or owner_kind is None:
        return errors
    errors.extend(_producer_file_errors(parsed))
    if parsed.producer is None:
        return errors
    expected_id = _DIRECT_OBSERVATION_ID if owner_kind == "native" else _PINOKIO_OBSERVATION_ID
    expected_path = _DIRECT_OBSERVATION_PATH if owner_kind == "native" else _PINOKIO_OBSERVATION_PATH
    expected_kind = "direct-custody-v1" if owner_kind == "native" else "pinokio-settlement-v1"
    observations = parsed.producer.export.get("observations")
    checks = parsed.producer.export.get("checks")
    if not isinstance(observations, list) or len(observations) != 1:
        errors.append("producer export: exactly one selected semantic observation required")
        observations = []
    observation = observations[0] if observations else {}
    if (
        not isinstance(observation, dict)
        or observation.get("id") != expected_id
        or observation.get("kind") != expected_kind
        or observation.get("file") != expected_path
        or observation.get("sequence") != 1
    ):
        errors.append("producer export: semantic observation ID/kind/path/sequence differs")
    if (
        not isinstance(checks, list)
        or len(checks) != 1
        or not isinstance(checks[0], dict)
        or checks[0].get("id") != expected_id
        or checks[0].get("result") != "pass"
        or checks[0].get("observations") != [expected_id]
    ):
        errors.append("producer export: exact semantic pass-check binding required")
    payload = parsed.producer_files.get(expected_path, {}).get("json")
    expected = _semantic_owner_and_selector(family, parsed.selector)
    if expected is None:
        return [*errors, "semantic observation: selected family/selector is unavailable"]
    _owner, selector_name = expected
    if owner_kind == "native":
        errors.extend(_direct_semantic_errors(payload, parsed, selector_name))
    else:
        errors.extend(_pinokio_semantic_errors(payload, parsed, selector_name))
    return errors
