"""R-30 admission tests for the API-bound self-hosted direct-macOS row."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "journey_evidence", ROOT / "scripts" / "check_journey_evidence.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def candidate():
    return {key: letter * 40 for key, letter in zip(("app", "agent", "runtime", "pinokio"), "abcd")}


def release1_row(contract, frozen):
    runner_os, image_os = gate.RUNNER_PROVENANCE[contract["runner"]]
    row = {
        "receipt_kind": "release1-installed-lifecycle",
        "job_id": contract["job_id"],
        "mode": contract["mode"],
        "scenario": contract.get("leg", "fresh-install"),
        "status": "pass",
        "runner_image": contract["runner"],
        "shell": contract["shell"],
        "source_override_test_mode": False,
        "final_cleanup": "pass",
        "os_build": runner_os + "-test",
        "runner_provenance": {
            "RUNNER_OS": runner_os,
            "ImageOS": image_os,
            "ImageVersion": "test",
        },
        "candidate_checkouts": {name: {"sha": sha} for name, sha in frozen.items()},
        "sources": {name: {"sha": frozen[name]} for name in ("app", "agent", "runtime")},
        "installed_tuple": {name: frozen[name] for name in ("app", "agent", "runtime")},
        "import_origins": {"runtime": "/owned/runtime"},
        "checks": {
            "semantic_data": "pass", "release": "pass", "service": "pass", "health": "pass",
            "task_settled": "pass", "one_runtime": "pass", "unrelated_sentinel": "pass",
        },
    }
    if contract["mode"] == "direct":
        row.update(
            service_manager_prerequisite={
                "required": True, "outcome": "pass", "manager": "native", "version": "1",
                "foreground_fallback": False,
            },
            service_manager={"manager": "native", "foreground_fallback": False},
            release_transition={"from": 0, "to": 1,
                                "harness_override": "frozen-release0-direct-to-release1"},
            release0_fixture={
                "fixture_sums_sha256": gate.RELEASE0_SUMS_SHA256,
                "manifest_sha256": "f" * 64,
                "provenance_sha256": "a" * 64,
                "bootstrap_sha256": gate.RELEASE0_BOOTSTRAP_SHA256,
                "sources": {
                    "agent": "3204bb00135bec8ee2d2d3fb21eed5abfceca649",
                    "runtime": "6d53a19070faf63fc15ae1f81417722edc2c8a5f",
                },
            },
            semantic_data={"identical": True, "before_sha256": "b" * 64,
                           "after_sha256": "b" * 64},
        )
    else:
        row["service_manager_prerequisite"] = {
            "required": False, "outcome": "explicit-owner", "managed_by": "pinokio",
            "foreground_fallback": False,
        }
    return row


def authenticated_row(frozen):
    direct = next(row for row in gate.RELEASE1_MATRIX if row["job_id"] == "direct-macos")
    row = release1_row(direct, frozen)
    context = {
        "schema_version": gate.SELF_HOSTED_MACOS_TRUSTED_CONTEXT_SCHEMA_VERSION,
        "source": "github-actions-api",
        "repository": "banodoco/astrid-agent",
        "workflow_path": ".github/workflows/journey.yml",
        "workflow_id": 789,
        "workflow_sha": "e" * 40,
        "ref": "refs/heads/lane/M-CI/C-MAC-R30-TRUSTED-CONTEXT-R1",
        "head_sha": "e" * 40,
        "run_id": 123,
        "run_attempt": 2,
        "job_id": 456,
        "matrix_job_id": "direct-macos",
        "runner_name": "hannah-mac",
        "runner_labels": ["self-hosted", "macOS", "ARM64", "astrid-mac"],
        "runner_os": "macOS",
        "runner_arch": "ARM64",
        "conclusion": "success",
        "candidate": copy.deepcopy(frozen),
        "receipt_sha256": "f" * 64,
        "collector_profile": gate.SELF_HOSTED_MACOS_COLLECTOR_PROFILE,
        "collector_version": gate.SELF_HOSTED_MACOS_COLLECTOR_VERSION,
        "collector_sha256": "c" * 64,
        "job_started_at": "2026-11-02T20:00:00Z",
        "job_completed_at": "2026-11-02T20:10:00Z",
        "receipt_ended_at": "2026-11-02T20:01:00Z",
        "freshness_seconds": 540,
    }
    row.update(
        venue="self-hosted",
        runner_image="self-hosted",
        execution_identity={
            key: copy.deepcopy(context[key])
            for key in gate.SELF_HOSTED_MACOS_RECEIPT_IDENTITY_FIELDS
        },
        self_hosted_collection={
            "schema_version": gate.SELF_HOSTED_MACOS_COLLECTION_SCHEMA_VERSION,
            "prepared_only": False,
            "started_at": "2026-11-02T20:00:05Z",
            "ended_at": "2026-11-02T20:01:00Z",
            "initial_census": {"source": "independent", "sha256": "1" * 64, "count": 4},
            "final_census": {"source": "independent", "sha256": "2" * 64, "count": 4},
            "event_sequence": {"first": 1, "last": 3, "count": 3, "sha256": "3" * 64},
            "loss_counters": {"emitted": 3, "persisted": 3, "dropped": 0},
            "inner_exit": 0,
        },
    )
    return row, context


def write_bound(tmp_path, receipt, context):
    receipt_path = tmp_path / "self-hosted" / "after-cleanup" / "journey-result.json"
    context_path = tmp_path / "trusted-actions-context.json"
    raw_dir = receipt_path.parent / "self-hosted-macos-raw"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir()
    raw_bytes = {
        "initial_census": b'{"source":"independent","phase":"initial"}\n',
        "final_census": b'{"source":"independent","phase":"final"}\n',
        "event_sequence": b'{"seq":1}\n{"seq":2}\n{"seq":3}\n',
    }
    for field, filename in gate.SELF_HOSTED_MACOS_RAW_FILES.items():
        path = raw_dir / filename
        path.write_bytes(raw_bytes[field])
        item = receipt["self_hosted_collection"].get(field)
        if isinstance(item, dict):
            item["sha256"] = hashlib.sha256(raw_bytes[field]).hexdigest()
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    context["receipt_sha256"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    context_path.write_text(json.dumps(context, sort_keys=True), encoding="utf-8")
    return receipt_path, context_path, raw_dir


def evaluate(tmp_path, receipt=None, context=None):
    frozen = candidate()
    good_receipt, good_context = authenticated_row(frozen)
    receipt = good_receipt if receipt is None else receipt
    context = good_context if context is None else context
    receipt_path, context_path, raw_dir = write_bound(tmp_path, receipt, context)
    return frozen, receipt_path, context_path, raw_dir


def test_self_hosted_macos_accepts_only_the_complete_api_bound_record(tmp_path):
    frozen, receipt_path, context_path, raw_dir = evaluate(tmp_path)
    assert not gate.evaluate_self_hosted_macos_receipt(frozen, receipt_path, context_path, raw_dir)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("wrong-runner", "runner_name differs"),
        ("wrong-label", "runner labels are missing astrid-mac"),
        ("wrong-head", "head_sha differs"),
        ("wrong-tuple", "trusted tuple differs"),
        ("missing-loss", "event loss counters"),
        ("nonzero-loss", "event loss counters"),
        ("missing-census", "initial census"),
        ("stale", "trusted freshness"),
        ("runner-freshness", "collector evidence has unknown fields freshness_seconds"),
        ("prepared", "prepared-only"),
        ("inner-exit", "inner exit"),
        ("hosted-relabel", "relabelled hosted evidence"),
    ),
)
def test_self_hosted_macos_required_negative_controls_fail_closed(tmp_path, mutation, expected):
    frozen = candidate()
    receipt, context = authenticated_row(frozen)
    if mutation == "wrong-runner":
        receipt["execution_identity"]["runner_name"] = "other-mac"
    elif mutation == "wrong-label":
        receipt["execution_identity"]["runner_labels"] = ["self-hosted", "macOS", "ARM64"]
    elif mutation == "wrong-head":
        receipt["execution_identity"]["head_sha"] = "9" * 40
    elif mutation == "wrong-tuple":
        context["candidate"]["runtime"] = "9" * 40
    elif mutation == "missing-loss":
        del receipt["self_hosted_collection"]["loss_counters"]
    elif mutation == "nonzero-loss":
        receipt["self_hosted_collection"]["loss_counters"]["dropped"] = 1
    elif mutation == "missing-census":
        del receipt["self_hosted_collection"]["initial_census"]
    elif mutation == "stale":
        context["freshness_seconds"] = 901
    elif mutation == "runner-freshness":
        receipt["self_hosted_collection"]["freshness_seconds"] = 540
    elif mutation == "prepared":
        receipt["self_hosted_collection"]["prepared_only"] = True
    elif mutation == "inner-exit":
        receipt["self_hosted_collection"]["inner_exit"] = 1
    elif mutation == "hosted-relabel":
        receipt["runner_image"] = "macos-15"
    receipt_path, context_path, raw_dir = write_bound(tmp_path, receipt, context)
    assert any(expected in error for error in gate.evaluate_self_hosted_macos_receipt(
        frozen, receipt_path, context_path, raw_dir
    )), mutation


def test_self_hosted_macos_rejects_a_wrong_artifact_after_api_binding(tmp_path):
    frozen, receipt_path, context_path, raw_dir = evaluate(tmp_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["execution_identity"]["runner_name"] = "forged-after-hash"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert any("trusted receipt digest differs" in error for error in gate.evaluate_self_hosted_macos_receipt(
        frozen, receipt_path, context_path, raw_dir
    ))


def test_self_hosted_macos_derives_freshness_only_after_job_completion(tmp_path):
    frozen, receipt_path, context_path, raw_dir = evaluate(tmp_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert "freshness_seconds" not in receipt["self_hosted_collection"]
    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert context["receipt_ended_at"] == receipt["self_hosted_collection"]["ended_at"]
    assert context["freshness_seconds"] == 540
    assert not gate.evaluate_self_hosted_macos_receipt(frozen, receipt_path, context_path, raw_dir)


def test_self_hosted_macos_rejects_missing_or_negative_trusted_freshness(tmp_path):
    frozen = candidate()
    receipt, context = authenticated_row(frozen)
    del context["freshness_seconds"]
    receipt_path, context_path, raw_dir = write_bound(tmp_path, receipt, context)
    assert any("missing freshness_seconds" in error for error in gate.evaluate_self_hosted_macos_receipt(
        frozen, receipt_path, context_path, raw_dir
    ))

    receipt, context = authenticated_row(frozen)
    context["freshness_seconds"] = -1
    receipt_path, context_path, raw_dir = write_bound(tmp_path / "negative", receipt, context)
    assert any("trusted freshness" in error for error in gate.evaluate_self_hosted_macos_receipt(
        frozen, receipt_path, context_path, raw_dir
    ))


def test_self_hosted_macos_rejects_stale_post_job_freshness(tmp_path):
    frozen = candidate()
    receipt, context = authenticated_row(frozen)
    context["job_completed_at"] = "2026-11-02T20:16:02Z"
    context["freshness_seconds"] = 902
    receipt_path, context_path, raw_dir = write_bound(tmp_path, receipt, context)
    assert any("trusted freshness" in error for error in gate.evaluate_self_hosted_macos_receipt(
        frozen, receipt_path, context_path, raw_dir
    ))


def test_self_hosted_macos_rejects_wrong_job_or_forged_context_timestamp(tmp_path):
    frozen = candidate()
    receipt, context = authenticated_row(frozen)
    context["job_id"] = 987654321
    receipt_path, context_path, raw_dir = write_bound(tmp_path, receipt, context)
    assert any("receipt job_id differs" in error for error in gate.evaluate_self_hosted_macos_receipt(
        frozen, receipt_path, context_path, raw_dir
    ))

    receipt, context = authenticated_row(frozen)
    context["receipt_ended_at"] = "2026-11-02T20:01:01Z"
    receipt_path, context_path, raw_dir = write_bound(tmp_path / "timestamp", receipt, context)
    assert any("trusted receipt end time differs" in error for error in gate.evaluate_self_hosted_macos_receipt(
        frozen, receipt_path, context_path, raw_dir
    ))


def test_self_hosted_macos_rejects_tampered_raw_receipt_timestamp(tmp_path):
    frozen, receipt_path, context_path, raw_dir = evaluate(tmp_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["self_hosted_collection"]["ended_at"] = "2026-11-02T20:01:01Z"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    assert any("trusted receipt digest differs" in error for error in gate.evaluate_self_hosted_macos_receipt(
        frozen, receipt_path, context_path, raw_dir
    ))


def test_self_hosted_macos_rehashes_retained_raw_bytes_and_rejects_tampering(tmp_path):
    frozen, receipt_path, context_path, raw_dir = evaluate(tmp_path)
    (raw_dir / gate.SELF_HOSTED_MACOS_RAW_FILES["event_sequence"]).write_bytes(b"tampered\n")
    assert any("event sequence raw bytes digest differs" in error
               for error in gate.evaluate_self_hosted_macos_receipt(
                   frozen, receipt_path, context_path, raw_dir
               ))


def test_self_hosted_macos_rejects_missing_or_lost_retained_raw_sources(tmp_path):
    frozen, receipt_path, context_path, raw_dir = evaluate(tmp_path)
    (raw_dir / gate.SELF_HOSTED_MACOS_RAW_FILES["initial_census"]).unlink()
    assert any("raw collection artifact is absent" in error
               for error in gate.evaluate_self_hosted_macos_receipt(
                   frozen, receipt_path, context_path, raw_dir
               ))

    frozen, receipt_path, context_path, raw_dir = evaluate(tmp_path / "lost")
    for path in raw_dir.iterdir():
        path.unlink()
    raw_dir.rmdir()
    assert any("raw collection artifact directory is absent" in error
               for error in gate.evaluate_self_hosted_macos_receipt(
                   frozen, receipt_path, context_path, raw_dir
               ))


def test_self_hosted_macos_rejects_raw_sources_that_exceed_the_bound(tmp_path, monkeypatch):
    frozen, receipt_path, context_path, raw_dir = evaluate(tmp_path)
    monkeypatch.setitem(gate.SELF_HOSTED_MACOS_RAW_MAX_BYTES, "event_sequence", 1)
    assert any("event sequence raw bytes are empty or exceed the bound" in error
               for error in gate.evaluate_self_hosted_macos_receipt(
                   frozen, receipt_path, context_path, raw_dir
               ))


def test_self_hosted_macos_refuses_to_admit_a_receipt_without_raw_sources(tmp_path):
    frozen, receipt_path, context_path, _ = evaluate(tmp_path)
    assert any("raw collection artifact directory is absent" in error
               for error in gate.evaluate_self_hosted_macos_receipt(
                   frozen, receipt_path, context_path, None
               ))


def test_combined_aggregate_preserves_eight_hosted_rows_and_rejects_relabelling(tmp_path):
    frozen, receipt_path, context_path, raw_dir = evaluate(tmp_path)
    hosted = tmp_path / "hosted"
    for contract in gate.RELEASE1_MATRIX:
        if contract["job_id"] == "direct-macos":
            continue
        target = hosted / contract["job_id"] / "after-cleanup" / "journey-result.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(release1_row(contract, frozen)), encoding="utf-8")
    assert not gate.evaluate_release1_with_self_hosted_macos(
        frozen, hosted, receipt_path, context_path, raw_dir
    )

    pinokio = next(hosted.rglob("journey-result.json"))
    row = json.loads(pinokio.read_text(encoding="utf-8"))
    row["venue"] = "self-hosted"
    pinokio.write_text(json.dumps(row), encoding="utf-8")
    assert any("hosted row is relabelled" in error for error in gate.evaluate_release1_with_self_hosted_macos(
        frozen, hosted, receipt_path, context_path, raw_dir
    ))


def test_workflow_builds_context_from_gate_owned_actions_api_and_keeps_hosted_route():
    workflow = (ROOT / ".github" / "workflows" / "journey.yml").read_text(encoding="utf-8")
    assert "actions: read" in workflow
    assert "name: Journey ${{ matrix.job_id }}" in workflow
    assert "journey-self-hosted-direct-macos-astrid-mac" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "actions/runs/{run_id}" in workflow
    assert "actions/runs/{run_id}/jobs?per_page=100" in workflow
    assert "github-actions-api" in workflow
    assert "--aggregate-release1-with-self-hosted-macos" in workflow
    assert "--aggregate-release1" in workflow
    assert "self-hosted receipt must not be included" not in workflow
