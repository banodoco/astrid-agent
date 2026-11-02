from __future__ import annotations

import copy

import pytest

from scripts import journey_failure_modes as failure
from tests.failure_mode_fixtures import PROVISIONAL_CANDIDATE, valid_observation


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


@pytest.mark.parametrize(
    "slot_id", ("direct-macos", "pinokio-backend-macos", "pinokio-fully-local-macos")
)
def test_macos15_slots_require_matching_hosted_identity(slot_id: str) -> None:
    observation = valid_observation(slot_id)
    assert failure.SLOT_CONTRACTS[slot_id]["runner"] == "macos-15"
    observation.update(os="macos", runner_image="macos-15", os_build="macOS-15.7-arm64")
    observation["runner_provenance"] = {
        "RUNNER_OS": "macOS", "ImageOS": "macos15", "ImageVersion": "20261030.1"
    }
    receipt = failure.create_receipt(slot_id, PROVISIONAL_CANDIDATE, observation)
    assert receipt["status"] == "pass"
    assert not failure.validate_receipt(receipt, PROVISIONAL_CANDIDATE, slot_id)

    for field, value, message in (
        ("os", "linux", "os:"),
        ("runner_image", "macos-14", "runner_image:"),
        ("os_build", "Linux-6.8", "os_build:"),
        ("RUNNER_OS", "Linux", "runner_provenance:"),
        ("ImageOS", "macos14", "runner_provenance:"),
        ("ImageVersion", "", "runner_provenance:"),
    ):
        bad = copy.deepcopy(observation)
        if field in ("RUNNER_OS", "ImageOS", "ImageVersion"):
            bad["runner_provenance"][field] = value
        else:
            bad[field] = value
        rejected = failure.create_receipt(slot_id, PROVISIONAL_CANDIDATE, bad)
        assert rejected["status"] == "fail", field
        assert any(message in error for error in rejected["verification"]["failed_assertions"]), field


def test_macos14_runner_identity_remains_available_for_legacy_contracts() -> None:
    contract = dict(failure.SLOT_CONTRACTS["direct-macos"], runner="macos-14")
    observation = valid_observation("direct-macos")
    observation["runner_image"] = "macos-14"
    observation["runner_provenance"]["ImageOS"] = "macos14"

    assert not failure._runner_errors(observation, contract)


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
