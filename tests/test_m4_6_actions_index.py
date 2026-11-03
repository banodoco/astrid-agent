"""The Actions index is diagnostic until a platform origin join exists."""
import copy
import hashlib
import json

import pytest

from scripts import check_journey_evidence as checker
from scripts import journey_predicates
from tests.test_journey_predicates import index_for
from tests.failure_mode_fixtures import PROVISIONAL_CANDIDATE


def api_case():
    archive = b"synthetic archive"
    return dict(
        candidate=copy.deepcopy(PROVISIONAL_CANDIDATE), slot_id="direct-linux",
        run_id=21, attempt=2, job_id=31, artifact_id=41, harness_sha="a" * 40,
        run={"id": 21, "run_attempt": 2, "head_sha": "a" * 40,
             "repository": {"full_name": "banodoco/astrid-agent"},
             "path": ".github/workflows/journey.yml@refs/heads/lane/example", "workflow_id": 51},
        jobs=[{"id": 31, "run_id": 21, "run_attempt": 2, "status": "completed",
               "conclusion": "success", "name": "produce / direct-linux",
               "runner_name": "GitHub Actions 123", "labels": ["ubuntu-24.04"]}],
        artifact={"id": 41, "name": "m4-6-direct-linux", "expired": False,
                  "workflow_run": {"id": 21}, "digest": "sha256:" + hashlib.sha256(archive).hexdigest()},
        archive=archive,
    )


def test_api_metadata_index_is_explicitly_not_admitted():
    result = checker.m4_6_actions_index(**api_case())
    assert result["admitted"] is False
    assert result["checkpoint_obligation_class"] == "direct-r1-checkpoint"
    assert {k: result[k] for k in ("repository", "workflow", "run_id", "run_attempt", "job_id",
                                    "runner_name", "runner_os", "artifact_id", "harness_sha")} == {
        "repository": "banodoco/astrid-agent", "workflow": ".github/workflows/journey.yml",
        "run_id": 21, "run_attempt": 2, "job_id": 31, "runner_name": "GitHub Actions 123",
        "runner_os": "Linux", "artifact_id": 41, "harness_sha": "a" * 40,
    }
    assert result["artifact_sha256"] == hashlib.sha256(b"synthetic archive").hexdigest()
    assert "external_checkpoint_join" not in result


@pytest.mark.parametrize("change", [
    lambda c: c["run"].update(id=22),
    lambda c: c["run"].update(run_attempt=1),
    lambda c: c["jobs"][0].update(id=32),
    lambda c: c["jobs"][0].update(run_attempt=1),
    lambda c: c["artifact"].update(id=42),
    lambda c: c["artifact"]["workflow_run"].update(id=22),
    lambda c: c.update(archive=b"altered"),
    lambda c: c.update(slot_id="direct-windows"),
    lambda c: c.update(harness_sha="b" * 40),
    lambda c: c["candidate"].update(agent="bad"),
])
def test_api_metadata_mismatches_fail_closed(change):
    case = api_case()
    change(case)
    with pytest.raises(ValueError):
        checker.m4_6_actions_index(**case)


def test_bundle_cannot_supply_its_own_external_authority():
    index = index_for()
    index["files"][0]["json"]["external_checkpoint_join"] = {"source": "github-actions-api", "admitted": True}
    ok, reasons = journey_predicates.check_m4_6(index)
    assert not ok
    assert any("external run/job/artifact join is required" in reason for reason in reasons)


def test_wrong_tuple_rejected_by_existing_predicate():
    index = index_for()
    index["candidate"]["agent"] = "f" * 40
    ok, reasons = journey_predicates.check_m4_6(index)
    assert not ok
    assert any("candidate_checkouts.agent" in reason for reason in reasons)


def test_continuity_relabel_rejected_by_existing_predicate():
    index = index_for()
    index["files"][0]["json"]["native_service"]["checkpoint_evidence"] = {"continuous_history": "pass"}
    index["external_checkpoint_join"] = {"checkpoint_obligation_class": "direct-r1-checkpoint"}
    ok, reasons = journey_predicates.check_m4_6(index)
    assert not ok
    assert any("deferred continuous_history" in reason for reason in reasons)


def test_cli_vetoes_even_well_formed_metadata(monkeypatch, tmp_path, capsys):
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps(PROVISIONAL_CANDIDATE))
    monkeypatch.setattr(checker, "collect_m4_6_actions_index", lambda *args: checker.m4_6_actions_index(**api_case()))
    monkeypatch.setattr("sys.argv", ["check_journey_evidence.py", "--m4-6-api-index", "--candidate", str(candidate)])
    assert checker.main() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["admitted"] is False
    assert checker.M4_6_ORIGIN_NEEDS in captured.err
