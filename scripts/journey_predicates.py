"""Journey checks for the orchestrator's authenticated evidence index.

``check_m4_6`` accepts one selected slot, not an inventory or a verdict.  The
caller must authenticate provenance, select the frozen slot and candidate, hash
raw artifact bytes, parse JSON with duplicate-key rejection, and supply:

    {"slot_id": str, "candidate": dict,
     "files": [{"path": relative_artifact_path, "sha256": str, "json": dict}]}

The list contains the recorded receipt and its four machine-observation files.
The candidate includes the four repository SHAs and ``manifest_sha256``;
the caller must bind all five to independently selected inputs, not to the
receipt.  M-CI's feature inventory must use scenario ``m4_6`` and select
these exact artifact-relative paths for each slot.
Paths are artifact-relative, not paths to read from disk.  All values must come
from the caller's authenticated index; fields in an artifact cannot confer that
status.  These checks do not select attempts or establish complete coverage.

For direct slots, the M-INT rule-36 P13 checkpoint class can be structurally
accepted only when M-CI supplies an externally recorded run/job/artifact join.
That join is caller input, not a claim made by the five producer files.  This
module validates its binding but remains neither a provenance authenticator nor
a release evaluator.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath

from . import journey_failure_modes as failure
from . import native_service_adapter as native


def _same_json(left: object, right: object) -> bool:
    try:
        return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
            right, sort_keys=True, allow_nan=False
        )
    except (TypeError, ValueError):
        return False


def _index_files(value: object) -> tuple[dict[str, dict], list[str]]:
    if not isinstance(value, list):
        return {}, ["files: evidence index list is required"]
    indexed = {}
    errors = []
    for number, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"files[{number}]: index entry must be an object")
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
            errors.append(f"files[{number}].path: safe relative path required")
            continue
        parts = PurePosixPath(path)
        if parts.is_absolute() or ".." in parts.parts or str(parts) != path:
            errors.append(f"files[{number}].path: safe relative path required")
            continue
        if path in indexed:
            errors.append(f"files[{number}].path: duplicate evidence path {path!r}")
            continue
        indexed[path] = item
        if not failure._digest(item.get("sha256")):
            errors.append(f"files[{number}].sha256: SHA-256 required")
        if not isinstance(item.get("json"), dict):
            errors.append(f"files[{number}].json: parsed JSON object required")
    return indexed, errors


_DEFERRED_DIRECT_CLAIM_KEYS = frozenset(
    {
        "containment",
        "custody_continuity",
        "gap_free_custody",
        "continuous_history",
        "continuous-history",
        "removal_authorization_continuity",
    }
)


def _deferred_claim_errors(value: object, *, prefix: str = "receipt") -> list[str]:
    """A checkpoint record must never smuggle in a deferred R1.x success claim."""
    errors: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key)
            if name in _DEFERRED_DIRECT_CLAIM_KEYS:
                errors.append(f"{prefix}: direct checkpoint must not assert deferred {name}")
            errors.extend(_deferred_claim_errors(item, prefix=f"{prefix}.{name}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(_deferred_claim_errors(item, prefix=f"{prefix}[{index}]"))
    return errors


def _external_direct_checkpoint_join_errors(
    value: object,
    *,
    receipt: dict,
    slot: str,
    candidate: dict,
    files: dict[str, dict],
) -> list[str]:
    """Validate the M-CI-owned external join without treating it as self-proof."""
    if not isinstance(value, dict):
        return ["external_checkpoint_join: M-CI external run/job/artifact join is required"]
    required = {
        "schema_version",
        "kind",
        "checkpoint_obligation_class",
        "slot_id",
        "candidate",
        "producer",
        "artifact",
        "harness_commit",
    }
    if set(value) != required:
        return ["external_checkpoint_join: exact external join fields are required"]
    errors: list[str] = []
    if value.get("schema_version") != 1 or value.get("kind") != "m-ci-external-artifact-join/v1":
        errors.append("external_checkpoint_join: exact M-CI join schema is required")
    if value.get("checkpoint_obligation_class") != "direct-r1-checkpoint":
        errors.append("external_checkpoint_join: direct R1 checkpoint class is required")
    if value.get("slot_id") != slot or value.get("candidate") != candidate:
        errors.append("external_checkpoint_join: selected slot/candidate differs")
    harness = value.get("harness_commit")
    if not isinstance(harness, str) or len(harness) != 40 or any(char not in "0123456789abcdef" for char in harness):
        errors.append("external_checkpoint_join.harness_commit: exact harness SHA is required")
    producer = value.get("producer")
    if not isinstance(producer, dict) or set(producer) != {
        "repository", "workflow", "run_id", "run_attempt", "job", "conclusion"
    }:
        errors.append("external_checkpoint_join.producer: exact run/job fields are required")
    else:
        hosted = receipt.get("hosted_authentication")
        expected = {
            "repository": hosted.get("GITHUB_REPOSITORY") if isinstance(hosted, dict) else None,
            "workflow": hosted.get("GITHUB_WORKFLOW") if isinstance(hosted, dict) else None,
            "run_id": hosted.get("GITHUB_RUN_ID") if isinstance(hosted, dict) else None,
            "run_attempt": hosted.get("GITHUB_RUN_ATTEMPT") if isinstance(hosted, dict) else None,
            "job": hosted.get("GITHUB_JOB") if isinstance(hosted, dict) else None,
        }
        if producer.get("conclusion") != "success" or any(
            not isinstance(producer.get(key), str) or not producer.get(key)
            for key in expected
        ):
            errors.append("external_checkpoint_join.producer: successful exact producer identity is required")
        elif any(producer.get(key) != expected[key] for key in expected):
            errors.append("external_checkpoint_join.producer: producer identity differs from receipt")
    artifact = value.get("artifact")
    if not isinstance(artifact, dict) or set(artifact) != {"id", "name", "sha256", "files"}:
        errors.append("external_checkpoint_join.artifact: exact fetched artifact fields are required")
    else:
        artifact_files = artifact.get("files")
        expected_files = {path: item.get("sha256") for path, item in files.items()}
        if (
            not isinstance(artifact.get("id"), str)
            or not artifact["id"]
            or not isinstance(artifact.get("name"), str)
            or not artifact["name"]
            or not failure._digest(artifact.get("sha256"))
            or artifact_files != expected_files
        ):
            errors.append("external_checkpoint_join.artifact: fetched file identities differ")
    return errors


def check_m4_6(evidence: dict) -> tuple[bool, list[str]]:
    """Check the M4-6 slot's evidence; never authenticate caller claims."""
    reasons = []
    if not isinstance(evidence, dict):
        return False, ["evidence: index object required"]
    slot = evidence.get("slot_id")
    candidate = evidence.get("candidate")
    if not isinstance(slot, str) or slot not in failure.SLOT_CONTRACTS:
        reasons.append("slot_id: selected M4-6 slot is missing or unknown")
    if not isinstance(candidate, dict):
        reasons.append("candidate: frozen tuple binding is required")
        candidate = {}
    reasons.extend(failure.candidate_errors(candidate))
    files, index_errors = _index_files(evidence.get("files"))
    reasons.extend(index_errors)
    required_paths = {failure.RECEIPT_NAME} | {
        f"evidence/{scenario}.json" for scenario in failure.SCENARIOS
    }
    for path in sorted(required_paths - files.keys()):
        reasons.append(f"files: missing evidence {path!r}")
    for path in sorted(files.keys() - required_paths):
        reasons.append(f"files: unexpected evidence {path!r}")
    receipt = files.get(failure.RECEIPT_NAME, {}).get("json")
    if isinstance(receipt, dict) and isinstance(slot, str) and slot in failure.SLOT_CONTRACTS:
        try:
            reasons.extend(failure.validate_receipt(receipt, candidate, slot))
            # The native adapter is not an engine/custody proof for Pinokio.
            if failure.SLOT_CONTRACTS[slot]["mode"] == "direct":
                reasons.extend(native._native_structural_errors(receipt))
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
            reasons.append("receipt: malformed native observation")
        declared = receipt.get("evidence_files")
        if not isinstance(declared, list):
            declared = []
        seen_paths = set()
        seen_scenarios = set()
        for row in declared:
            if not isinstance(row, dict):
                reasons.append("evidence_files: malformed declaration")
                continue
            path, scenario = row.get("path"), row.get("scenario")
            if not isinstance(path, str) or not isinstance(scenario, str):
                reasons.append("evidence_files: path and scenario required")
                continue
            if path in seen_paths or scenario in seen_scenarios:
                reasons.append("evidence_files: duplicate path or scenario")
            seen_paths.add(path)
            seen_scenarios.add(scenario)
            if scenario not in failure.SCENARIOS or path != f"evidence/{scenario}.json":
                reasons.append("evidence_files: scenario/path binding differs")
                continue
            entry = files.get(path, {})
            if row.get("sha256") != entry.get("sha256"):
                reasons.append(f"{path}: index SHA-256 differs from receipt")
            payload = entry.get("json")
            if not isinstance(payload, dict):
                continue
            scenarios = receipt.get("scenarios")
            scenario_row = scenarios.get(scenario) if isinstance(scenarios, dict) else None
            if (payload.get("slot_id") != slot or payload.get("scenario") != scenario
                    or payload.get("execution_scope") != receipt.get("execution_scope")
                    or not _same_json(payload.get("observation"), scenario_row)
                    or not _same_json(payload.get("collector_provenance"), receipt.get("collector_provenance"))
                    or payload.get("collector_structural_sha256") != receipt.get("collector_structural_sha256")):
                reasons.append(f"{path}: parsed observation differs from receipt or selected slot")
            service = receipt.get("native_service")
            if isinstance(service, dict):
                instances = service.get("instances")
                expected = [item for item in instances if isinstance(item, dict)
                            and str(item.get("scenario", "")).split(".", 1)[0] == scenario] if isinstance(instances, list) else None
                if (not _same_json(payload.get("native_instances"), expected)
                        or not _same_json(payload.get("sanitized_native_manager_observations"), service.get("raw_observations"))):
                    reasons.append(f"{path}: native observations differ from receipt")
        if seen_scenarios != set(failure.SCENARIOS):
            reasons.append("evidence_files: all four distinct scenarios are required")
        if (failure.SLOT_CONTRACTS[slot]["mode"] == "direct"
                and receipt.get("execution_scope") != native.HOSTED_SCOPE):
            reasons.append("execution_scope: hosted native-service evidence required")
    else:
        reasons.append("receipt: parsed failure-mode receipt is required")
    if isinstance(receipt, dict) and isinstance(slot, str) and slot in failure.SLOT_CONTRACTS:
        if failure.SLOT_CONTRACTS[slot]["mode"] == "direct":
            reasons.extend(_deferred_claim_errors(receipt))
            reasons.extend(
                _external_direct_checkpoint_join_errors(
                    evidence.get("external_checkpoint_join"),
                    receipt=receipt,
                    slot=slot,
                    candidate=candidate,
                    files=files,
                )
            )
        else:
            reasons.append("M4-6: Pinokio custody remains outside this direct checkpoint predicate")
    else:
        reasons.append("M4-6: parsed receipt is unavailable")
    return not reasons, reasons
