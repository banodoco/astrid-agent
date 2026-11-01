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


def index_files(value: object) -> tuple[dict[str, dict[str, object]], list[str]]:
    """Validate the index fragment accepted by the decided predicate API."""
    if not isinstance(value, list):
        return {}, ["files: evidence index list is required"]
    indexed: dict[str, dict[str, object]] = {}
    errors: list[str] = []
    for number, item in enumerate(value):
        prefix = f"files[{number}]"
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
    allowed = {selector_name, "files", "producer"}
    if require_candidate:
        allowed.add("candidate")
    errors = _unknown_fields(evidence, allowed, "evidence")
    selector = evidence.get(selector_name)
    if not isinstance(selector, str) or selector not in accepted_selectors:
        errors.append(f"{selector_name}: selected obligation is missing or unknown")
        selector = ""
    candidate: dict[str, object] | None = None
    if require_candidate:
        candidate_value = evidence.get("candidate")
        errors.extend(_candidate_errors(candidate_value))
        if isinstance(candidate_value, dict):
            candidate = candidate_value
    producer_supplied = "producer" in evidence
    producer: ParsedProducerEnvelope | None = None
    if producer_supplied:
        producer, producer_errors = parse_producer_envelope(evidence.get("producer"))
        errors.extend(producer_errors)
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
    return ParsedEvidence(selector, candidate, receipt, files, producer, producer_supplied), errors


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
            or not isinstance(owner.get("version"), str) or not owner["version"].strip()
            or owner.get("foreground_fallback") is not False
        ):
            errors.append("receipt: native service-manager prerequisite did not pass")
        manager = receipt.get("service_manager")
        if not isinstance(manager, dict) or manager.get("foreground_fallback") is not False:
            errors.append("receipt: direct service ownership is absent")
        transition = receipt.get("release_transition")
        if not isinstance(transition, dict) or transition.get("from") != 0 or transition.get("to") != 1:
            errors.append("receipt.release_transition: authentic release-0 transition is absent")
        fixture = receipt.get("release0_fixture")
        if not isinstance(fixture, dict) or fixture.get("fixture_sums_sha256") != failure.RELEASE0_SUMS_SHA256:
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
        expected_owner = "native" if release2.scenario_definition(parsed.selector)["mode"] == "direct" else "pinokio"
    except ValueError:  # selector error is already reported above
        pass
    else:
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
    try:
        errors.extend(failure.validate_receipt(receipt, parsed.candidate, parsed.selector))
        # This function internally dispatches to the direct or Pinokio native
        # structural contract.  It remains structural evidence only.
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
