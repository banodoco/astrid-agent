"""Evidence contract tests; these observations are fixtures, not hosted receipts."""
import copy
import hashlib
import json

import pytest

from scripts import make_standin_release
from scripts import windows_pinokio_evidence as evidence


def observations(standin):
    digest = "a" * 64
    return {
        "install": {"status": "pass", "launcher_install": True, "managed_by_pinokio": True, "release": 1, "install_id": "one"},
        "start_task": {"status": "pass", "launcher_start": True, "healthy": True, "task_succeeded": True, "materialized_output": True, "runtime_instance_id": "first", "task_id": "task-one", "data_digest": digest},
        "stop_start": {"status": "pass", "launcher_stop": True, "owned_runtime_stopped": True, "unrelated_sentinel_alive": True, "launcher_start": True, "healthy": True, "before_instance_id": "first", "after_instance_id": "second"},
        "busy_stop": {"status": "pass", "task_observed_busy": True, "launcher_stop_attempted": True, "data_intact": True, "unrelated_sentinel_alive": True, "stop_outcome": "refused", "task_state": "failed", "task_clean_failure": True, "task_terminal_observed": True, "task_id": "task-busy", "data_digest": digest},
        "update": {"status": "pass", "launcher_update": True, "healthy": True, "managed_by_pinokio": True, "data_intact": True, "release": 2, "standin_manifest_sha256": standin, "installed_manifest_sha256": standin, "runtime_instance_id": "third", "data_digest": digest},
        "updated_task": {"status": "pass", "healthy": True, "task_succeeded": True, "materialized_output": True, "release": 2, "runtime_instance_id": "third", "task_id": "task-two"},
        "uninstall": {"status": "pass", "launcher_uninstall": True, "program_removed": True, "owned_runtime_stopped": True, "unrelated_sentinel_alive": True, "data_intact": True, "data_digest": digest},
    }


def test_standin_is_canonical_and_preserves_candidate(tmp_path):
    candidate = {"release": 1, "agent": {"sha": "1" * 40}, "runtime": {"sha": "2" * 40}}
    before = copy.deepcopy(candidate)
    path = tmp_path / "standin.json"
    sha = evidence.standin_manifest(candidate, path)
    raw = make_standin_release.canonical_manifest_bytes(make_standin_release.make_standin_release(candidate))
    assert path.read_bytes() == raw
    assert sha == hashlib.sha256(raw).hexdigest()
    assert candidate == before
    with pytest.raises(evidence.EvidenceError, match="already exists"):
        evidence.standin_manifest(candidate, path)
    with pytest.raises(evidence.EvidenceError, match="release 1"):
        evidence.standin_manifest({"release": 2}, tmp_path / "bad.json")


def test_incremental_ledger_and_complete_validation(tmp_path):
    digest = "f" * 64
    ledger = evidence.WindowsPinokioEvidence(tmp_path / "stages.json", digest)
    with pytest.raises(evidence.EvidenceError, match="order"):
        ledger.record("start_task", observations(digest)["start_task"])
    for name, row in observations(digest).items():
        ledger.record(name, row)
        value = json.loads(ledger.path.read_text())
        if name != "uninstall":
            assert value["status"] == "incomplete"
            with pytest.raises(evidence.EvidenceError, match="incomplete"):
                evidence.validate_ledger(value, standin_sha256=digest)
    evidence.validate_ledger(value, standin_sha256=digest)
    with pytest.raises(evidence.EvidenceError, match="digest"):
        evidence.validate_ledger(value, standin_sha256="e" * 64)


@pytest.mark.parametrize("stage,key,value", [
    ("install", "launcher_install", False),
    ("start_task", "task_succeeded", False),
    ("stop_start", "after_instance_id", "first"),
    ("busy_stop", "task_observed_busy", False),
    ("busy_stop", "task_clean_failure", False),
    ("busy_stop", "task_state", "running"),
    ("busy_stop", "data_digest", "b" * 64),
    ("update", "installed_manifest_sha256", "b" * 64),
    ("update", "release", 1),
    ("updated_task", "runtime_instance_id", "second"),
    ("uninstall", "program_removed", False),
])
def test_rejects_bad_or_missing_evidence_without_advancing(tmp_path, stage, key, value):
    digest = "f" * 64
    ledger = evidence.WindowsPinokioEvidence(tmp_path / "stages.json", digest)
    rows = observations(digest)
    for name in evidence.STAGES:
        row = rows[name]
        if name == stage:
            row[key] = value
            with pytest.raises(evidence.EvidenceError):
                ledger.record(name, row)
            saved = json.loads(ledger.path.read_text())
            assert saved["status"] == "incomplete"
            assert [entry["stage"] for entry in saved["stages"]] == list(evidence.STAGES[:evidence.STAGES.index(stage)])
            break
        ledger.record(name, row)


def test_gate_rejects_deleted_reordered_or_skipped_stage(tmp_path):
    digest = "f" * 64
    ledger = evidence.WindowsPinokioEvidence(tmp_path / "stages.json", digest)
    for name, row in observations(digest).items():
        ledger.record(name, row)
    value = json.loads(ledger.path.read_text())
    for mutate in (
        lambda v: v["stages"].pop(2),
        lambda v: v["stages"].reverse(),
        lambda v: v["stages"][3]["observation"].update(status="skipped"),
        lambda v: v["stages"][4]["observation"].update(standin_manifest_sha256="e" * 64, installed_manifest_sha256="e" * 64),
    ):
        broken = copy.deepcopy(value)
        mutate(broken)
        with pytest.raises(evidence.EvidenceError):
            evidence.validate_ledger(broken, standin_sha256=digest)
