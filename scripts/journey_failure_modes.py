#!/usr/bin/env python3
"""Admit machine observations for the Release-1 failure-mode journey.

This module does not pretend to run a hosted platform from a unit test.  The
platform adapter supplies observations; this module turns them into a receipt
only when every M4-6 invariant is explicit and machine-checkable.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Final, Mapping

if __package__:
    from .journey_matrix import RELEASE1_MATRIX
else:
    try:
        from journey_matrix import RELEASE1_MATRIX
    except ModuleNotFoundError:  # importlib-loaded tests run from the repository root
        from scripts.journey_matrix import RELEASE1_MATRIX


SCHEMA_VERSION: Final = 1
RECEIPT_KIND: Final = "release1-failure-modes"
RECEIPT_NAME: Final = "journey-failure-result.json"
SCENARIOS: Final = (
    "interruption_recovery",
    "failed_candidate_rollback",
    "keep_data_uninstall",
    "stop_safety",
)
STOP_OUTCOMES: Final = ("refused", "unknown")
DATA_ENTITIES: Final = ("project", "document", "preferences", "cas")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")

RELEASE0_REFS: Final = {
    "agent": "3204bb00135bec8ee2d2d3fb21eed5abfceca649",
    "runtime": "6d53a19070faf63fc15ae1f81417722edc2c8a5f",
}
RELEASE0_LAUNCHERS: Final = {
    "main": "bbba620e3d89eeff8021417f365e6ea584203105",
    "stop_first": "c3b4cd5d32d8bd37e76cec292d81f18320eda9c2",
}
RELEASE0_SUMS_SHA256: Final = "58221a50aa7940e3121d6239ddeb92d6409df3711096aac1161c7ec130b755c0"
RELEASE0_BOOTSTRAP_SHA256: Final = {
    "install": "100bab1deb15d620abd910c8fb0c9a6d77f4945428a3229dac6b506705e3fd0d",
    "install.ps1": "bba3f2edc41c047cb51475ceeac4489a04610af73a3af44bdc05287964d28258",
}
RUNNER_PROVENANCE: Final = {
    "macos-14": ("macos", "macOS", "macos14"),
    "ubuntu-24.04": ("linux", "Linux", "ubuntu24"),
    "windows-2022": ("windows", "Windows", "win22"),
}
SLOT_CONTRACTS: Final = {row["job_id"]: dict(row) for row in RELEASE1_MATRIX}
REQUIRED_SLOT_IDS: Final = tuple(SLOT_CONTRACTS)


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def write_json(path: Path, value: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _digest(value: object) -> bool:
    return isinstance(value, str) and DIGEST.fullmatch(value) is not None


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _candidate_errors(candidate: Mapping, prefix: str = "candidate") -> list[str]:
    errors = []
    for name in ("app", "agent", "runtime", "pinokio"):
        if not isinstance(candidate.get(name), str) or SHA.fullmatch(candidate[name]) is None:
            errors.append(f"{prefix}.{name}: full commit SHA is required")
    if not _digest(candidate.get("manifest_sha256")):
        errors.append(f"{prefix}.manifest_sha256: SHA-256 is required")
    return errors


def candidate_errors(candidate: Mapping, prefix: str = "candidate") -> list[str]:
    """Public candidate-shape check shared by the aggregate gate."""
    return _candidate_errors(candidate, prefix)


def _semantic_errors(value: object, prefix: str) -> list[str]:
    row = _mapping(value)
    errors = []
    before = row.get("before_sha256")
    after = row.get("after_sha256")
    if not _digest(before) or not _digest(after):
        errors.append(f"{prefix}: before/after semantic SHA-256 values are required")
    elif before != after or row.get("identical") is not True:
        errors.append(f"{prefix}: semantic data is not identical")
    entities = _mapping(row.get("entities"))
    if set(entities) != set(DATA_ENTITIES):
        errors.append(f"{prefix}.entities: project, document, preferences and cas are required")
    for name in DATA_ENTITIES:
        entity = _mapping(entities.get(name))
        entity_before = entity.get("before_sha256")
        entity_after = entity.get("after_sha256")
        if not _digest(entity_before) or not _digest(entity_after):
            errors.append(f"{prefix}.entities.{name}: before/after SHA-256 values are required")
        elif entity_before != entity_after:
            errors.append(f"{prefix}.entities.{name}: semantic data changed")
    return errors


def _identity_errors(value: object, expected_sources: Mapping, prefix: str) -> list[str]:
    row = _mapping(value)
    errors = []
    if row.get("selected_release") not in ("release0", "candidate"):
        errors.append(f"{prefix}.selected_release: release0 or candidate is required")
    if not _nonempty(row.get("executable_path")):
        errors.append(f"{prefix}.executable_path: real installed executable path is required")
    for name in ("executable_sha256", "version_manifest_sha256", "process_identity_sha256"):
        if not _digest(row.get(name)):
            errors.append(f"{prefix}.{name}: SHA-256 is required")
    if row.get("source_refs") != dict(expected_sources):
        errors.append(f"{prefix}.source_refs: installed Agent/Runtime identity is wrong")
    if row.get("running") is not True or row.get("health") != "pass":
        errors.append(f"{prefix}: executable was not observed running and healthy")
    return errors


def _identity_pair_errors(value: object, prefix: str) -> list[str]:
    pair = _mapping(value)
    before = _mapping(pair.get("before"))
    after = _mapping(pair.get("after"))
    errors = []
    errors.extend(_identity_errors(before, RELEASE0_REFS, f"{prefix}.before"))
    errors.extend(_identity_errors(after, RELEASE0_REFS, f"{prefix}.after"))
    for name in (
        "executable_path",
        "executable_sha256",
        "version_manifest_sha256",
        "source_refs",
    ):
        if before.get(name) != after.get(name):
            errors.append(f"{prefix}: release-0 {name} changed")
    if pair.get("match") is not True:
        errors.append(f"{prefix}.match: real release-0 identity was not affirmed")
    return errors


def _release0_fixture_errors(value: object) -> list[str]:
    fixture = _mapping(value)
    errors = []
    if fixture.get("fixture_sums_sha256") != RELEASE0_SUMS_SHA256:
        errors.append("release0_fixture.fixture_sums_sha256: frozen fixture digest is wrong")
    if fixture.get("bootstrap_sha256") != RELEASE0_BOOTSTRAP_SHA256:
        errors.append("release0_fixture.bootstrap_sha256: frozen bootstrap digests are wrong")
    if fixture.get("sources") != RELEASE0_REFS:
        errors.append("release0_fixture.sources: frozen release-0 refs are wrong")
    if fixture.get("launcher_refs") != RELEASE0_LAUNCHERS:
        errors.append("release0_fixture.launcher_refs: frozen launcher refs are wrong")
    for name in ("manifest_sha256", "provenance_sha256"):
        if not _digest(fixture.get(name)):
            errors.append(f"release0_fixture.{name}: SHA-256 is required")
    return errors


def _runner_errors(observation: Mapping, contract: Mapping) -> list[str]:
    errors = []
    runner = contract["runner"]
    expected_os, expected_runner_os, expected_image = RUNNER_PROVENANCE[runner]
    for name, expected in (
        ("slot_id", contract["job_id"]),
        ("job_id", contract["job_id"]),
        ("mode", contract["mode"]),
        ("os", expected_os),
        ("runner_image", runner),
        ("shell", contract["shell"]),
    ):
        if observation.get(name) != expected:
            errors.append(f"{name}: expected {expected!r}")
    provenance = _mapping(observation.get("runner_provenance"))
    if (
        provenance.get("RUNNER_OS") != expected_runner_os
        or provenance.get("ImageOS") != expected_image
        or not _nonempty(provenance.get("ImageVersion"))
    ):
        errors.append("runner_provenance: hosted runner identity does not match the slot")
    os_build = observation.get("os_build")
    if not isinstance(os_build, str) or not os_build.startswith(expected_runner_os + "-"):
        errors.append("os_build: hosted OS build does not match the slot")
    if observation.get("source_override_test_mode") is not False:
        errors.append("source_override_test_mode: source overrides are not admissible")
    return errors


def _candidate_observation_errors(observation: Mapping, candidate: Mapping) -> list[str]:
    errors = _candidate_errors(candidate)
    observed = _mapping(observation.get("candidate_checkouts"))
    for name in ("app", "agent", "runtime", "pinokio"):
        if _mapping(observed.get(name)).get("sha") != candidate.get(name):
            errors.append(f"candidate_checkouts.{name}: checkout differs from frozen candidate")
    if observation.get("manifest_sha256") != candidate.get("manifest_sha256"):
        errors.append("manifest_sha256: observation differs from frozen candidate")
    return errors


def _evidence_errors(value: object) -> list[str]:
    if not isinstance(value, list):
        return ["evidence_files: machine evidence list is required"]
    errors = []
    covered = set()
    for index, item in enumerate(value):
        row = _mapping(item)
        prefix = f"evidence_files[{index}]"
        raw_path = row.get("path")
        if not _nonempty(raw_path):
            errors.append(f"{prefix}.path: run-owned relative path is required")
        else:
            evidence_path = PurePosixPath(raw_path)
            if evidence_path.is_absolute() or ".." in evidence_path.parts:
                errors.append(f"{prefix}.path: path must be run-owned and relative")
        if not _digest(row.get("sha256")):
            errors.append(f"{prefix}.sha256: SHA-256 is required")
        if row.get("kind") != "machine-observation":
            errors.append(f"{prefix}.kind: prose/source evidence is not admissible")
        if row.get("scenario") not in SCENARIOS:
            errors.append(f"{prefix}.scenario: unknown failure scenario")
        else:
            covered.add(row["scenario"])
    for scenario in SCENARIOS:
        if scenario not in covered:
            errors.append(f"evidence_files: no machine observation for {scenario}")
    return errors


def _interruption_errors(value: object, candidate: Mapping) -> list[str]:
    row = _mapping(value)
    errors = []
    operation_id = row.get("operation_id")
    if row.get("status") != "pass":
        errors.append("scenarios.interruption_recovery.status: pass is required")
    if not _nonempty(operation_id) or not _nonempty(row.get("phase")):
        errors.append(
            "scenarios.interruption_recovery: operation id and durable phase are required"
        )
    if row.get("injected") is not True:
        errors.append(
            "scenarios.interruption_recovery.injected: controlled interruption is required"
        )
    first = _mapping(row.get("first_request"))
    if first.get("interrupted") is not True or not _nonempty(first.get("journal_state")):
        errors.append(
            "scenarios.interruption_recovery.first_request: interruption journal is absent"
        )
    recovery = _mapping(row.get("recovery_request"))
    if recovery.get("explicit") is not True or recovery.get("operation_id") != operation_id:
        errors.append(
            "scenarios.interruption_recovery.recovery_request: explicit same-operation recovery is required"
        )
    if row.get("terminal_state") not in ("complete", "rolled_back"):
        errors.append("scenarios.interruption_recovery.terminal_state: recovery is not terminal")
    if row.get("duplicate_operation") is not False or row.get("recovery_verified") is not True:
        errors.append("scenarios.interruption_recovery: recovery duplicated or was not verified")
    terminal = _mapping(row.get("terminal_identity"))
    selected = terminal.get("selected_release")
    expected_sources = (
        RELEASE0_REFS
        if selected == "release0"
        else {"agent": candidate.get("agent"), "runtime": candidate.get("runtime")}
    )
    errors.extend(
        _identity_errors(
            terminal, expected_sources, "scenarios.interruption_recovery.terminal_identity"
        )
    )
    errors.extend(
        _semantic_errors(row.get("semantic_data"), "scenarios.interruption_recovery.semantic_data")
    )
    return errors


def _rollback_errors(value: object) -> list[str]:
    row = _mapping(value)
    errors = []
    if row.get("status") != "pass":
        errors.append("scenarios.failed_candidate_rollback.status: pass is required")
    required_true = (
        "failure_injected",
        "rollback_selected",
        "real_release0_started",
        "real_release0_identity_match",
        "data_intact",
    )
    required_false = ("candidate_started", "candidate_activated")
    for name in required_true:
        if row.get(name) is not True:
            errors.append(f"scenarios.failed_candidate_rollback.{name}: true is required")
    for name in required_false:
        if row.get(name) is not False:
            errors.append(f"scenarios.failed_candidate_rollback.{name}: false is required")
    if not _nonempty(row.get("failure_phase")):
        errors.append("scenarios.failed_candidate_rollback.failure_phase: named phase is required")
    if row.get("journal_terminal_state") not in ("rolled_back", "failed"):
        errors.append(
            "scenarios.failed_candidate_rollback.journal_terminal_state: terminal rollback is required"
        )
    errors.extend(
        _identity_pair_errors(
            row.get("executable_identity"),
            "scenarios.failed_candidate_rollback.executable_identity",
        )
    )
    errors.extend(
        _semantic_errors(
            row.get("semantic_data"), "scenarios.failed_candidate_rollback.semantic_data"
        )
    )
    return errors


def _uninstall_errors(value: object) -> list[str]:
    row = _mapping(value)
    errors = []
    if row.get("status") != "pass":
        errors.append("scenarios.keep_data_uninstall.status: pass is required")
    for name in (
        "requested",
        "keep_data",
        "program_root_removed",
        "selection_removed",
        "native_service_removed",
        "data_root_exists",
        "workspace_and_cas_present",
        "unrelated_sentinel_survived",
    ):
        if row.get(name) is not True:
            errors.append(f"scenarios.keep_data_uninstall.{name}: true is required")
    if row.get("exit_code") != 0:
        errors.append("scenarios.keep_data_uninstall.exit_code: zero is required")
    if row.get("runtime_processes_after") != 0:
        errors.append(
            "scenarios.keep_data_uninstall.runtime_processes_after: owned Runtime remains"
        )
    errors.extend(
        _semantic_errors(row.get("semantic_data"), "scenarios.keep_data_uninstall.semantic_data")
    )
    return errors


def _stop_safety_errors(value: object) -> list[str]:
    row = _mapping(value)
    errors = []
    if row.get("status") != "pass":
        errors.append("scenarios.stop_safety.status: pass is required")
    cases = _mapping(row.get("cases"))
    if set(cases) != set(STOP_OUTCOMES):
        errors.append("scenarios.stop_safety.cases: refused and unknown cases are required")
    for outcome in STOP_OUTCOMES:
        case = _mapping(cases.get(outcome))
        prefix = f"scenarios.stop_safety.cases.{outcome}"
        if case.get("outcome") != outcome or not _nonempty(case.get("reason")):
            errors.append(f"{prefix}: explicit {outcome} outcome and reason are required")
        for name in (
            "update_admitted",
            "candidate_downloaded_or_written",
            "selection_changed",
            "journal_changed_to_update",
            "candidate_activated",
            "data_changed",
            "automatic_retry",
        ):
            if case.get(name) is not False:
                errors.append(f"{prefix}.{name}: false is required")
        errors.extend(
            _identity_pair_errors(case.get("executable_identity"), f"{prefix}.executable_identity")
        )
        errors.extend(_semantic_errors(case.get("semantic_data"), f"{prefix}.semantic_data"))
    return errors


def scenario_errors(name: str, value: object, candidate: Mapping) -> list[str]:
    if name == "interruption_recovery":
        return _interruption_errors(value, candidate)
    if name == "failed_candidate_rollback":
        return _rollback_errors(value)
    if name == "keep_data_uninstall":
        return _uninstall_errors(value)
    if name == "stop_safety":
        return _stop_safety_errors(value)
    return [f"scenarios.{name}: unknown scenario"]


def evaluate_observation(slot_id: str, candidate: Mapping, observation: Mapping) -> list[str]:
    contract = SLOT_CONTRACTS.get(slot_id)
    if contract is None:
        return [f"slot_id: unknown Release-1 slot {slot_id!r}"]
    errors = []
    errors.extend(_runner_errors(observation, contract))
    errors.extend(_candidate_observation_errors(observation, candidate))
    errors.extend(_release0_fixture_errors(observation.get("release0_fixture")))
    errors.extend(_evidence_errors(observation.get("evidence_files")))
    scenarios = _mapping(observation.get("scenarios"))
    if set(scenarios) != set(SCENARIOS):
        errors.append(
            "scenarios: exact interruption, rollback, uninstall and stop controls are required"
        )
    for name in SCENARIOS:
        errors.extend(scenario_errors(name, scenarios.get(name), candidate))
    return errors


def create_receipt(slot_id: str, candidate: Mapping, observation: Mapping) -> dict:
    """Create a recorded receipt; only validated observations receive pass."""
    receipt = copy.deepcopy(dict(observation))
    receipt.update(
        schema_version=SCHEMA_VERSION,
        receipt_kind=RECEIPT_KIND,
        receipt_state="recorded",
    )
    per_scenario = {
        name: scenario_errors(name, _mapping(observation.get("scenarios")).get(name), candidate)
        for name in SCENARIOS
    }
    errors = evaluate_observation(slot_id, candidate, receipt)
    receipt["checks"] = {
        name: "pass" if not scenario_error else "fail"
        for name, scenario_error in per_scenario.items()
    }
    receipt["status"] = "pass" if not errors else "fail"
    receipt["verification"] = {
        "state": "verified-pass" if not errors else "verified-fail",
        "verifier": f"{RECEIPT_KIND}/v{SCHEMA_VERSION}",
        "failed_assertions": errors,
    }
    return receipt


def validate_receipt(receipt: Mapping, candidate: Mapping, expected_slot: str) -> list[str]:
    errors = []
    if receipt.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version: unsupported failure receipt schema")
    if receipt.get("receipt_kind") != RECEIPT_KIND:
        errors.append("receipt_kind: not a Release-1 failure-mode receipt")
    if receipt.get("receipt_state") != "recorded":
        errors.append("receipt_state: receipt was not durably recorded")
    if receipt.get("status") != "pass":
        errors.append(f"status: {receipt.get('status')!r} is not pass")
    verification = _mapping(receipt.get("verification"))
    if verification.get("state") != "verified-pass" or verification.get("failed_assertions") != []:
        errors.append("verification: receipt is not a clean verified pass")
    checks = _mapping(receipt.get("checks"))
    if set(checks) != set(SCENARIOS) or any(checks.get(name) != "pass" for name in SCENARIOS):
        errors.append("checks: every failure-mode assertion must pass")
    errors.extend(evaluate_observation(expected_slot, candidate, receipt))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot-id", required=True, choices=REQUIRED_SLOT_IDS)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--observation", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        candidate = read_json(args.candidate)
        observation = read_json(args.observation)
        receipt = create_receipt(args.slot_id, candidate, observation)
        write_json(args.output, receipt)
        errors = _mapping(receipt.get("verification")).get("failed_assertions", [])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors = [str(exc)]
    if errors:
        print("Failure-mode admission: FAIL", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Failure-mode admission: PASS ({args.slot_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
