"""Focused parser coverage for the M-CI journey-evidence index boundary."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from scripts import journey_failure_modes as failure
from scripts import journey_observations as observations
from scripts import journey_release2 as release2
from scripts import native_service_adapter as native
from tests.failure_mode_fixtures import PROVISIONAL_CANDIDATE, valid_observation


def _entry(path: str, payload: dict) -> dict:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "json": payload}


def release1_index(slot: str = "direct-linux") -> dict:
    contract = next(row for row in failure.SLOT_CONTRACTS.values() if row["job_id"] == slot)
    _os_name, runner_os, image_os = failure.RUNNER_PROVENANCE[contract["runner"]]
    candidate = copy.deepcopy(PROVISIONAL_CANDIDATE)
    receipt = {
        "receipt_kind": "release1-installed-lifecycle",
        "job_id": slot,
        "mode": contract["mode"],
        "scenario": contract.get("leg", "fresh-install"),
        "status": "pass",
        "runner_image": contract["runner"],
        "shell": contract["shell"],
        "source_override_test_mode": False,
        "final_cleanup": "pass",
        "os_build": runner_os + "-test",
        "runner_provenance": {"RUNNER_OS": runner_os, "ImageOS": image_os, "ImageVersion": "test"},
        "candidate_checkouts": {name: {"sha": candidate[name]} for name in ("app", "agent", "runtime", "pinokio")},
        "sources": {name: {"sha": candidate[name]} for name in ("app", "agent", "runtime")},
        "installed_tuple": {name: candidate[name] for name in ("app", "agent", "runtime")},
        "import_origins": {"runtime": "/owned/runtime"},
        "service_manager_prerequisite": {"required": True, "outcome": "pass", "manager": "native", "version": "1", "foreground_fallback": False},
        "service_manager": {"manager": "native", "foreground_fallback": False},
        "release_transition": {"from": 0, "to": 1, "harness_override": "frozen-release0-direct-to-release1"},
        "release0_fixture": {
            "fixture_sums_sha256": failure.RELEASE0_SUMS_SHA256,
            "manifest_sha256": "f" * 64,
            "provenance_sha256": "a" * 64,
            "bootstrap_sha256": failure.RELEASE0_BOOTSTRAP_SHA256,
            "sources": dict(failure.RELEASE0_REFS),
        },
        "semantic_data": {"identical": True, "before_sha256": "b" * 64, "after_sha256": "b" * 64},
        "checks": {name: "pass" for name in ("release", "service", "health", "semantic_data", "task_settled", "one_runtime", "unrelated_sentinel")},
    }
    return {"job_id": slot, "candidate": candidate, "files": [_entry("journey-result.json", receipt)]}


def m4_5_index(scenario_id: str = "release2-install-line-direct-linux") -> dict:
    row = release2.scenario_definition(scenario_id)
    runner = release2.RUNNERS[row["os"]]
    receipt = {
        "schema_version": 1,
        "receipt_kind": "release2-installed-lifecycle",
        **row,
        "status": "pass",
        "evidence_kind": "hosted-lifecycle",
        "source_override_test_mode": False,
        "candidate": copy.deepcopy(release2.RELEASE1_CANDIDATE),
        "candidate_checkouts": {name: {"sha": sha} for name, sha in release2.RELEASE1_CANDIDATE.items()},
        "stand_in": release2.verified_stand_in(),
        "release_transition": {"from": 1, "to": 2, "stand_in": True},
        "installed": {"release": 2, "qualification_marker": release2.STAND_IN_MARKER, "manifest_sha256": release2.STAND_IN_MANIFEST_SHA256, "agent": release2.STAND_IN_AGENT, "runtime": release2.STAND_IN_RUNTIME, "schema_digest": release2.STAND_IN_SCHEMA},
        "runner_provenance": {"RUNNER_OS": runner["runner_os"], "ImageOS": runner["image_os"], "ImageVersion": "shape-only"},
        "os_build": runner["runner_os"] + "-shape-only",
        "service_manager_prerequisite": {"required": True, "outcome": "pass", "manager": runner["manager"], "version": "shape-only", "foreground_fallback": False},
        "semantic_data": {"identical": True, "before_sha256": "a" * 64, "after_sha256": "a" * 64},
        "checks": {name: "pass" for name in ("release", "marker", "service", "health", "semantic_data", "task_settled", "one_runtime", "unrelated_sentinel", "install_line")},
        "controls": [],
        "trigger": {"channel": "install-line", "entrypoint": "public-bootstrap", "update_flag": True, "manifest_sha256": release2.STAND_IN_MANIFEST_SHA256, "root_bound": True, "data_root_bound": True},
        "actions": [],
        "execution_boundary": {},
        "final_cleanup": "pass",
    }
    return {"scenario_id": scenario_id, "files": [_entry("journey-result.json", receipt)]}


def m4_6_index(slot: str = "direct-linux") -> dict:
    observation = valid_observation(slot)
    observation.update(
        execution_scope=native.HOSTED_SCOPE,
        collector_provenance={"kind": "synthetic"},
        collector_structural_sha256="a" * 64,
        native_service={"instances": [], "raw_observations": []},
    )
    declarations, payloads = native._evidence_bundle(observation)
    observation["evidence_files"] = declarations
    receipt = failure.create_receipt(slot, PROVISIONAL_CANDIDATE, observation)
    files = [_entry(failure.RECEIPT_NAME, receipt)]
    files.extend(
        {"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "json": json.loads(raw)}
        for path, raw in payloads.items()
    )
    return {"slot_id": slot, "candidate": copy.deepcopy(PROVISIONAL_CANDIDATE), "files": files}


@pytest.mark.parametrize(
    "raw",
    [b"{", b'{"same": 1, "same": 2}', b'{"float": 1.5}', b'{"large": 9007199254740992}', b'{"bad": "\\ud800"}'],
)
def test_raw_parser_rejects_noncanonical_or_unsafe_json(raw: bytes) -> None:
    with pytest.raises(observations.ObservationParseError):
        observations.parse_json_object(raw, hashlib.sha256(raw).hexdigest())


def test_raw_parser_binds_the_exact_raw_digest() -> None:
    raw = b'{"valid":true}'
    with pytest.raises(observations.ObservationParseError, match="SHA-256"):
        observations.parse_json_object(raw, "0" * 64)
    assert observations.parse_json_object(raw, hashlib.sha256(raw).hexdigest()) == {"valid": True}


@pytest.mark.parametrize("path", ["../escape.json", "/absolute.json", "evidence\\bad.json", "NUL.json", "evidence/%2e.json", "dot./x.json"])
def test_portable_path_parser_rejects_unsafe_paths(path: str) -> None:
    assert observations.safe_relative_path(path) is False


def test_release1_and_m4_5_parsers_accept_their_checked_in_structural_contracts() -> None:
    release1, release1_errors = observations.parse_release1_evidence(release1_index())
    m4_5, m4_5_errors = observations.parse_m4_5_evidence(m4_5_index())
    assert release1 is not None and release1_errors == []
    assert m4_5 is not None and m4_5_errors == []


@pytest.mark.parametrize(
    ("factory", "parser"),
    [
        (release1_index, observations.parse_release1_evidence),
        (m4_5_index, observations.parse_m4_5_evidence),
        (m4_6_index, observations.parse_m4_6_evidence),
    ],
)
def test_parsers_reject_undeclared_fields(factory, parser) -> None:
    index = factory()
    index["files"][0]["json"]["not_declared_by_contract"] = True
    _parsed, errors = parser(index)
    assert any("unknown field" in error for error in errors)


def test_m4_6_parser_rejects_sidecar_mismatch_and_malformed_observation() -> None:
    index = m4_6_index()
    sidecar = next(item for item in index["files"] if item["path"] == "evidence/stop_safety.json")
    sidecar["json"]["observation"] = {"status": "forged"}
    sidecar["json"]["not_declared_by_contract"] = True
    _parsed, errors = observations.parse_m4_6_evidence(index)
    assert any("unknown field" in error for error in errors)
    assert any("parsed observation differs" in error for error in errors)


def test_m4_6_parser_rejects_duplicate_missing_wrong_slot_and_wrong_tuple() -> None:
    index = m4_6_index()
    index["files"].append(copy.deepcopy(index["files"][1]))
    index["files"].pop(2)
    index["slot_id"] = "direct-macos"
    index["candidate"]["agent"] = "f" * 40
    _parsed, errors = observations.parse_m4_6_evidence(index)
    assert any("duplicate evidence path" in error for error in errors)
    assert any("missing evidence" in error for error in errors)
    assert any("differs from selected tuple" in error or "candidate_checkouts.agent" in error for error in errors)
