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


@pytest.mark.parametrize("slot_id", ("direct-macos", "pinokio-backend-macos", "pinokio-fully-local-macos"))
def test_macos15_exact_hosted_provenance(slot_id: str) -> None:
    observation = valid_observation(slot_id)
    observation.update(os="macos", runner_image="macos-15", os_build="macOS-15.8.2-arm64")
    observation["runner_provenance"] = {
        "RUNNER_OS": "macOS", "ImageOS": "macos15", "ImageVersion": "20261028.0489.1"
    }
    assert failure.create_receipt(slot_id, PROVISIONAL_CANDIDATE, observation)["status"] == "pass"


@pytest.mark.parametrize("slot_id", ("direct-macos", "pinokio-backend-macos", "pinokio-fully-local-macos"))
@pytest.mark.parametrize(
    "path, value, message",
    (
        (("runner_image",), "macos-14", "runner_image"),
        (("os",), "linux", "os:"),
        (("os_build",), "Linux-foreign", "os_build"),
        (("runner_provenance", "RUNNER_OS"), "Linux", "runner_provenance"),
        (("runner_provenance", "ImageOS"), "macos14", "runner_provenance"),
        (("runner_provenance", "ImageVersion"), "", "runner_provenance"),
    ),
)
def test_macos15_wrong_runner_or_image_is_rejected(
    slot_id: str, path: tuple[str, ...], value: str, message: str
) -> None:
    observation = valid_observation(slot_id)
    observation["runner_provenance"] = {
        "RUNNER_OS": "macOS", "ImageOS": "macos15", "ImageVersion": "20261028.0489.1"
    }
    target = observation
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    receipt = failure.create_receipt(slot_id, PROVISIONAL_CANDIDATE, observation)
    assert receipt["status"] == "fail"
    assert any(message in error for error in receipt["verification"]["failed_assertions"])


def test_legacy_macos14_runner_provenance_remains_exact() -> None:
    contract = dict(failure.SLOT_CONTRACTS["direct-macos"], runner="macos-14")
    observation = valid_observation("direct-macos")
    observation["runner_image"] = "macos-14"
    observation["runner_provenance"] = {
        "RUNNER_OS": "macOS", "ImageOS": "macos14", "ImageVersion": "20261028.0489.1"
    }
    assert failure._runner_errors(observation, contract) == []
    observation["runner_provenance"]["ImageOS"] = "macos15"
    assert failure._runner_errors(observation, contract)


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
