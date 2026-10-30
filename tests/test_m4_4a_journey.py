"""Focused provisional M4-4a adapter and aggregate checks."""

import argparse
import json
from pathlib import Path

import pytest

from scripts import ci_journey, check_journey_evidence, journey_matrix


def test_backend_matrix_is_explicit_and_cannot_admit_other_modes():
    assert journey_matrix.M4_4A_BACKEND_JOB_IDS == (
        "pinokio-backend-macos", "pinokio-backend-linux", "pinokio-backend-windows")
    assert journey_matrix.validate_m4_4a_backend_job(
        "pinokio-backend-windows", "pinokio-backend")["shell"] == "powershell.exe"
    with pytest.raises(ValueError, match="requires mode"):
        journey_matrix.validate_m4_4a_backend_job("pinokio-backend-linux", "direct")


def test_old_launcher_input_fails_closed_before_git_execution(tmp_path):
    with pytest.raises(ValueError, match="full 40-character"):
        ci_journey.validate_old_launcher_provenance(tmp_path, "missing")


def test_backend_plan_requires_explicit_old_launcher_input(tmp_path):
    args = argparse.Namespace(pinokio_checkout=tmp_path)
    with pytest.raises(ValueError, match="--old-launcher-ref"):
        ci_journey.prepare_pinokio_backend(args, tmp_path / "evidence")


def test_m4_4a_aggregate_requires_every_hosted_row_and_never_accepts_missing_input(tmp_path):
    candidate = {name: letter * 40 for name, letter in zip(("app", "agent", "runtime", "pinokio"), "abcd")}
    errors = check_journey_evidence.evaluate_m4_4a_backend_matrix(candidate, tmp_path)
    assert {job for job in journey_matrix.M4_4A_BACKEND_JOB_IDS
            if any(job in error for error in errors)} == set(journey_matrix.M4_4A_BACKEND_JOB_IDS)


def test_source_only_receipt_is_not_hosted_lifecycle_evidence(tmp_path):
    candidate = {name: letter * 40 for name, letter in zip(("app", "agent", "runtime", "pinokio"), "abcd")}
    row = {
        "job_id": "pinokio-backend-linux", "mode": "pinokio-backend", "scenario": "fresh-install",
        "status": "pass", "runner_image": "ubuntu-24.04", "shell": "sh",
        "evidence_class": "source-unit", "source_override_test_mode": False,
        "final_cleanup": "pass", "runner_provenance": {"RUNNER_OS": "Linux", "ImageOS": "ubuntu24",
        "ImageVersion": "test"}, "candidate_checkouts": {name: {"sha": value} for name, value in candidate.items()},
        "service_manager_prerequisite": {"outcome": "explicit-owner", "managed_by": "pinokio",
        "foreground_fallback": False}, "source_unit_evidence": {"status": "pass", "substitute_for_hosted": False},
        "pinokio_backend": {"schema_version": 1, "os": "linux", "mode": "pinokio-backend",
        "hosted_lifecycle": {"status": "not-run"}},
    }
    path = tmp_path / "pinokio-backend-linux" / "journey-result.json"
    path.parent.mkdir()
    path.write_text(json.dumps(row), encoding="utf-8")
    errors = check_journey_evidence.evaluate_m4_4a_backend_matrix(candidate, tmp_path)
    assert any("hosted lifecycle evidence" in error for error in errors)
    assert any("evidence_class" in error for error in errors)
