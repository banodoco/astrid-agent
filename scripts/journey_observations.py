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
class ParsedEvidence:
    """One selected receipt plus the index entries that its contract requires."""

    selector: str
    candidate: dict[str, object] | None
    receipt: dict[str, object]
    files: dict[str, dict[str, object]]


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
    allowed = {selector_name, "files"}
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
    return ParsedEvidence(selector, candidate, receipt, files), errors


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
    return parsed, errors
