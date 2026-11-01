from __future__ import annotations

import copy

import pytest

from scripts import journey_failure_modes as failure
from scripts import journey_matrix as matrix
from tests.failure_mode_fixtures import PROVISIONAL_CANDIDATE, valid_observation


def test_m4_6_slots_exclude_canonical_fault_scenarios() -> None:
    original_ids = tuple(
        f"{mode}-{os_name}"
        for mode in ("direct", "pinokio-backend", "pinokio-fully-local")
        for os_name in ("macos", "linux", "windows")
    )
    fault_ids = {
        f"direct-{os_name}-{scenario}"
        for os_name in ("macos", "linux", "windows")
        for scenario in ("interrupted-update", "rollback")
    }

    assert matrix.M4_6_SLOT_IDS == failure.REQUIRED_SLOT_IDS == original_ids
    assert tuple(row["job_id"] for row in matrix.M4_6_MATRIX) == original_ids
    assert tuple(failure.SLOT_CONTRACTS) == original_ids
    for slot_id in original_ids:
        assert matrix.validate_m4_6_slot(slot_id) == matrix.release1_job(slot_id)
        assert failure.SLOT_CONTRACTS[slot_id] == matrix.release1_job(slot_id)

    for fault_id in fault_ids:
        assert sum(row["job_id"] == fault_id for row in matrix.RELEASE1_MATRIX) == 1
        assert matrix.release1_job(fault_id)["scenario_id"] == fault_id
        with pytest.raises(ValueError, match="Unknown M4-6 failure-mode slot"):
            matrix.validate_m4_6_slot(fault_id)
        receipt = failure.create_receipt(
            fault_id, PROVISIONAL_CANDIDATE, valid_observation("direct-linux")
        )
        assert receipt["status"] == "fail"
        assert any(
            "unknown Release-1 slot" in error
            for error in receipt["verification"]["failed_assertions"]
        )
        with pytest.raises(SystemExit) as exc:
            failure.main([
                "--slot-id", fault_id, "--candidate", "unused",
                "--observation", "unused", "--output", "unused",
            ])
        assert exc.value.code == 2

    assert matrix.APP_UPDATE_GUARD_SCENARIO["scenario_id"] == "installed-app-update-guard-linux"


@pytest.mark.parametrize("slot_id", failure.REQUIRED_SLOT_IDS)
def test_all_nine_os_mode_slots_admit_complete_machine_observations(slot_id: str) -> None:
    receipt = failure.create_receipt(slot_id, PROVISIONAL_CANDIDATE, valid_observation(slot_id))

    assert receipt["status"] == "pass"
    assert receipt["verification"] == {
        "state": "verified-pass",
        "verifier": "release1-failure-modes/v1",
        "failed_assertions": [],
    }
    assert not failure.validate_receipt(receipt, PROVISIONAL_CANDIDATE, slot_id)


@pytest.mark.parametrize("slot_id", ("direct-macos", "direct-linux", "direct-windows"))
def test_interruption_recovery_is_explicit_per_os(slot_id: str) -> None:
    observation = valid_observation(slot_id)
    observation["scenarios"]["interruption_recovery"]["duplicate_operation"] = True

    receipt = failure.create_receipt(slot_id, PROVISIONAL_CANDIDATE, observation)

    assert receipt["status"] == "fail"
    assert receipt["checks"]["interruption_recovery"] == "fail"
    assert any("duplicated" in error for error in receipt["verification"]["failed_assertions"])


@pytest.mark.parametrize(
    "mutation, message",
    (
        (("executable_identity", "after", "executable_sha256"), "executable_sha256 changed"),
        (
            ("semantic_data", "entities", "project", "after_sha256"),
            "project: semantic data changed",
        ),
    ),
)
def test_rollback_requires_real_release0_executable_and_intact_data(
    mutation: tuple[str, ...], message: str
) -> None:
    observation = valid_observation("direct-linux")
    value = observation["scenarios"]["failed_candidate_rollback"]
    target = value
    for key in mutation[:-1]:
        target = target[key]
    target[mutation[-1]] = "f" * 64

    receipt = failure.create_receipt("direct-linux", PROVISIONAL_CANDIDATE, observation)

    assert receipt["status"] == "fail"
    assert any(message in error for error in receipt["verification"]["failed_assertions"])


@pytest.mark.parametrize(
    "field, value",
    (("keep_data", False), ("data_root_exists", False), ("runtime_processes_after", 1)),
)
def test_keep_data_uninstall_fails_closed(field: str, value: object) -> None:
    observation = valid_observation("pinokio-backend-macos")
    observation["scenarios"]["keep_data_uninstall"][field] = value

    receipt = failure.create_receipt("pinokio-backend-macos", PROVISIONAL_CANDIDATE, observation)

    assert receipt["status"] == "fail"
    assert any(field in error for error in receipt["verification"]["failed_assertions"])


@pytest.mark.parametrize("outcome", failure.STOP_OUTCOMES)
@pytest.mark.parametrize(
    "field", ("update_admitted", "candidate_downloaded_or_written", "automatic_retry")
)
def test_refused_or_unknown_stop_never_updates(outcome: str, field: str) -> None:
    observation = valid_observation("pinokio-fully-local-windows")
    observation["scenarios"]["stop_safety"]["cases"][outcome][field] = True

    receipt = failure.create_receipt(
        "pinokio-fully-local-windows", PROVISIONAL_CANDIDATE, observation
    )

    assert receipt["status"] == "fail"
    assert any(
        f"cases.{outcome}.{field}" in error
        for error in receipt["verification"]["failed_assertions"]
    )


def test_prose_or_source_evidence_cannot_be_admitted() -> None:
    observation = valid_observation("direct-windows")
    observation["evidence_files"][0]["kind"] = "source-inspection"

    receipt = failure.create_receipt("direct-windows", PROVISIONAL_CANDIDATE, observation)

    assert receipt["status"] == "fail"
    assert any(
        "prose/source evidence" in error for error in receipt["verification"]["failed_assertions"]
    )


def test_stale_verified_pass_fields_cannot_hide_mutated_observations() -> None:
    receipt = failure.create_receipt(
        "direct-macos", PROVISIONAL_CANDIDATE, valid_observation("direct-macos")
    )
    stale = copy.deepcopy(receipt)
    stale["scenarios"]["failed_candidate_rollback"]["candidate_activated"] = True

    errors = failure.validate_receipt(stale, PROVISIONAL_CANDIDATE, "direct-macos")

    assert any("candidate_activated" in error for error in errors)
