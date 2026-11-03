"""Synthetic index tests; these do not establish authenticated provenance."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from scripts import journey_failure_modes as failure
from scripts import journey_predicates as predicates
from scripts import native_service_adapter as native
from tests.failure_mode_fixtures import PROVISIONAL_CANDIDATE, valid_observation


def index_for(slot="direct-linux"):
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
    raw = json.dumps(receipt, sort_keys=True).encode()
    files = [{"path": failure.RECEIPT_NAME, "sha256": hashlib.sha256(raw).hexdigest(), "json": receipt}]
    files.extend({"path": path, "sha256": hashlib.sha256(data).hexdigest(), "json": json.loads(data)}
                 for path, data in payloads.items())
    return {"slot_id": slot, "candidate": copy.deepcopy(PROVISIONAL_CANDIDATE), "files": files}


def reasons_for(index):
    ok, reasons = predicates.check_m4_6(index)
    assert ok is False
    assert reasons and all(isinstance(reason, str) for reason in reasons)
    return reasons


def receipt(index):
    return index["files"][0]["json"]


@pytest.mark.parametrize("slot", failure.REQUIRED_SLOT_IDS)
def test_m4_6_each_slot_stays_closed_without_independent_proofs(slot):
    reasons = reasons_for(index_for(slot))
    assert any("independent section 5" in reason for reason in reasons)
    assert not any("parsed observation differs" in reason for reason in reasons)
    assert not any("index SHA-256 differs" in reason for reason in reasons)


def test_m4_6_rejects_missing_evidence():
    index = index_for()
    index["files"].pop()
    assert any("missing evidence" in reason for reason in reasons_for(index))


def test_m4_6_rejects_skipped_evidence():
    index = index_for()
    receipt(index)["status"] = "skipped"
    assert any("skipped" in reason for reason in reasons_for(index))


def test_m4_6_rejects_forged_observation_despite_authority_claims():
    index = index_for()
    index.update(trusted=True, authority="orchestrator")
    entry = index["files"][1]
    entry["json"]["observation"]["status"] = "pass-forged"
    entry["sha256"] = hashlib.sha256(json.dumps(entry["json"]).encode()).hexdigest()
    reasons = reasons_for(index)
    assert any("index SHA-256 differs" in reason for reason in reasons)
    assert any("parsed observation differs" in reason for reason in reasons)


def test_m4_6_rejects_duplicate_evidence():
    index = index_for()
    index["files"].append(copy.deepcopy(index["files"][1]))
    assert any("duplicate evidence path" in reason for reason in reasons_for(index))


def test_m4_6_rejects_wrong_tuple():
    index = index_for()
    index["candidate"]["agent"] = "f" * 40
    assert any("candidate_checkouts.agent" in reason for reason in reasons_for(index))


def test_m4_6_requires_independent_manifest_binding():
    index = index_for()
    del index["candidate"]["manifest_sha256"]
    assert any("candidate.manifest_sha256" in reason for reason in reasons_for(index))


def test_m4_6_recomputes_semantics_when_matching_receipt_and_file_are_forged():
    index = index_for()
    row = receipt(index)
    scenario = "stop_safety"
    row["scenarios"][scenario]["cases"]["unknown"]["candidate_activated"] = True
    entry = next(item for item in index["files"] if item["path"] == f"evidence/{scenario}.json")
    entry["json"]["observation"] = copy.deepcopy(row["scenarios"][scenario])
    entry["sha256"] = hashlib.sha256(json.dumps(entry["json"]).encode()).hexdigest()
    next(item for item in row["evidence_files"] if item["scenario"] == scenario)["sha256"] = entry["sha256"]
    reasons = reasons_for(index)
    assert any("candidate_activated" in reason for reason in reasons)
    assert not any("parsed observation differs" in reason for reason in reasons)


def test_m4_6_rejects_wrong_slot():
    index = index_for("direct-macos")
    index["slot_id"] = "direct-linux"
    assert any("slot_id" in reason for reason in reasons_for(index))


def test_m4_6_rejects_duplicate_scenario_declarations():
    index = index_for()
    receipt(index)["evidence_files"].append(copy.deepcopy(receipt(index)["evidence_files"][0]))
    assert any("duplicate path or scenario" in reason for reason in reasons_for(index))


def test_m4_6_rejects_disposable_scope():
    index = index_for()
    receipt(index)["execution_scope"] = native.FIXTURE_SCOPE
    assert any("hosted native-service" in reason for reason in reasons_for(index))


@pytest.mark.parametrize("value", [None, {}, {"slot_id": []}, {"slot_id": "direct-linux", "candidate": [], "files": [None, {"path": []}]}])
def test_m4_6_rejects_malformed_indexes(value):
    reasons_for(value)


def test_m4_6_rejects_unsafe_and_unexpected_paths():
    index = index_for()
    index["files"].extend([{"path": "../escape", "sha256": "a" * 64, "json": {}},
                           {"path": "extra.json", "sha256": "a" * 64, "json": {}}])
    reasons = reasons_for(index)
    assert any("safe relative" in reason for reason in reasons)
    assert any("unexpected evidence" in reason for reason in reasons)


def test_m4_6_rejects_invalid_digest_and_nul_path():
    index = index_for()
    index["files"][0]["sha256"] = "A" * 64
    index["files"].append({"path": "bad\x00path", "sha256": "a" * 64, "json": {}})
    reasons = reasons_for(index)
    assert any("SHA-256 required" in reason for reason in reasons)
    assert any("safe relative" in reason for reason in reasons)
