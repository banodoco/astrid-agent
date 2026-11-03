"""The Actions index is diagnostic until a platform origin join exists."""
import copy
import base64
import hashlib
import json

import pytest

from scripts import check_journey_evidence as checker
from scripts import journey_predicates
from tests.test_journey_predicates import index_for
from tests.failure_mode_fixtures import PROVISIONAL_CANDIDATE


def api_case():
    archive = b"synthetic archive"
    artifact = {"id": 41, "name": "m4-6-direct-linux", "expired": False,
                "workflow_run": {"id": 21}, "size_in_bytes": len(archive),
                "digest": "sha256:" + hashlib.sha256(archive).hexdigest()}
    return dict(
        candidate=copy.deepcopy(PROVISIONAL_CANDIDATE), slot_id="direct-linux",
        run_id=21, attempt=2, job_id=31, artifact_id=41, harness_sha="a" * 40,
        run={"id": 21, "run_attempt": 2, "head_sha": "a" * 40,
             "repository": {"full_name": "banodoco/astrid-agent"},
             "path": ".github/workflows/journey.yml@refs/heads/lane/example", "workflow_id": 51},
        jobs=[{"id": 31, "run_id": 21, "run_attempt": 2, "status": "completed",
               "conclusion": "success", "name": "produce / direct-linux",
               "runner_name": "GitHub Actions 123", "labels": ["ubuntu-24.04"]}],
        artifact=artifact, artifacts=[copy.deepcopy(artifact)], archive=archive,
        workflow=b"jobs: {}\n", workflow_ref="a" * 40,
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


def test_attempt_scoped_api_response_need_not_repeat_attempt():
    case = api_case()
    del case["jobs"][0]["run_attempt"]
    assert checker.m4_6_actions_index(**case)["admitted"] is False


def test_collector_fetches_pinned_workflow_and_all_api_observations(monkeypatch):
    case = api_case()
    workflow = case["workflow"]
    blob = hashlib.sha1(b"blob " + str(len(workflow)).encode() + b"\0" + workflow).hexdigest()
    seen = []
    root = "https://api.github.com/repos/banodoco/astrid-agent"
    responses = {
        f"/contents/.github/workflows/journey.yml?ref={'a' * 40}": {
            "type": "file", "path": ".github/workflows/journey.yml", "encoding": "base64",
            "content": base64.b64encode(workflow).decode(), "size": len(workflow), "sha": blob},
        "/actions/runs/21": case["run"],
        "/actions/runs/21/attempts/2/jobs?per_page=100&page=1": {"jobs": case["jobs"]},
        "/actions/runs/21/artifacts?per_page=100&page=1": {"artifacts": case["artifacts"]},
        "/actions/artifacts/41": case["artifact"],
        "/actions/artifacts/41/zip": case["archive"],
    }

    class Response:
        def __init__(self, body):
            self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return self.body[:limit]

    class Opener:
        def open(self, request, timeout):
            url = request.full_url
            assert url.startswith(root)
            path = url[len(root):]
            seen.append(path)
            return Response(responses[path])

    monkeypatch.setattr(checker.urllib.request, "build_opener", lambda *args: Opener())
    result = checker.collect_m4_6_actions_index(
        case["candidate"], case["slot_id"], 21, 2, 31, 41, "a" * 40, "token")
    assert result["admitted"] is False
    assert result["workflow_sha256"] == hashlib.sha256(workflow).hexdigest()
    assert result["artifact_size_bytes"] == len(case["archive"])
    assert set(seen) == set(responses)

    responses[f"/contents/.github/workflows/journey.yml?ref={'a' * 40}"]["sha"] = "b" * 40
    with pytest.raises(ValueError, match="blob identity"):
        checker.collect_m4_6_actions_index(
            case["candidate"], case["slot_id"], 21, 2, 31, 41, "a" * 40, "token")


@pytest.mark.parametrize("change", [
    lambda c: c["run"].update(id=22),
    lambda c: c["run"].update(run_attempt=1),
    lambda c: c["jobs"][0].update(id=32),
    lambda c: c["jobs"][0].update(run_attempt=1),
    lambda c: c["artifact"].update(id=42),
    lambda c: c["artifact"]["workflow_run"].update(id=22),
    lambda c: c["artifact"].update(size_in_bytes=1),
    lambda c: c["artifacts"].append(copy.deepcopy(c["artifacts"][0])),
    lambda c: c.update(workflow_ref="b" * 40),
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
