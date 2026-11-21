"""Offline controls for the installed R13 to required-R14 rehearsal."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import check_journey_evidence as gate
from scripts import journey_live_workspace as live
from scripts import journey_matrix as matrix
from scripts import journey_required_update as required
from scripts import journey_version_jumps as jumps

ROOT = Path(__file__).resolve().parents[1]
R13_MANIFEST = "a" * 64
R14_MANIFEST = "b" * 64
R14_APP = "c" * 40
CANDIDATE = {
    "app": R14_APP, "release": 14, "required": {"release": 14, "reason": "Required test release."},
    "manifest_sha256": R14_MANIFEST, "schema_digest": "sha256:" + "e" * 64,
}


def test_workflow_selects_nine_exact_rows_and_preserves_historical_scope():
    assert len(matrix.FIX17_JOB_IDS) == len(set(matrix.FIX17_JOB_IDS)) == 9
    assert set(matrix.RELEASE1_JOB_IDS) == {
        "direct-macos", "direct-linux", "direct-windows", "direct-macos-fresh", "direct-linux-fresh",
    }
    workflow = yaml.safe_load((ROOT / ".github/workflows/journey.yml").read_text())
    choices = workflow[True]["workflow_dispatch"]["inputs"]["row"]["options"]
    assert set(matrix.FIX17_JOB_IDS) <= set(choices)
    assert not any("mixed-" in choice for choice in choices)
    driver = next(step for step in workflow["jobs"]["journey"]["steps"]
                  if step.get("name") == "Run the matrix-owned lifecycle driver")
    for contract in matrix.FIX17_REQUIRED_UPDATE_MATRIX:
        assert matrix.validate_job(contract["job_id"], "direct") == contract
        assert contract["job_id"] not in matrix.RELEASE1_JOB_IDS
        assert contract["leg"] in driver["run"]
        assert (contract["os"], contract["shell"]) == ("linux", "sh")
    assert "--release13-browser-dist" in driver["run"] and "--candidate-browser-dist" in driver["run"]
    assert jumps.RELEASE13_APP_COMMIT == gate.RELEASE13_APP_COMMIT == "de7dd1e668aa6dcf3cbce013c701fa7c76615f99"


def test_hosted_build_must_be_in_exact_pinned_checkout(tmp_path):
    app = tmp_path / "app"
    dist = app / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>")
    (dist / "build.json").write_text("{}")
    with pytest.raises(RuntimeError, match="exact pinned"):
        required._dist(app, tmp_path / "absent")
    with pytest.raises(RuntimeError, match="does not belong"):
        required._dist(app, tmp_path)
    assert required._dist(app, dist) == dist
    (dist / "build.json").unlink()
    with pytest.raises(RuntimeError, match="index and build"):
        required._dist(app, dist)


def browser_step(mode, app, manifest, **details):
    return {"status": "pass", "mode": mode, "app": app, "manifest_sha256": manifest,
            "build_sha256": "f" * 64, "intercept_reverted": True, **details}


def row_fixture(prompt):
    row = {
        "release13_source": {"app_commit": gate.RELEASE13_APP_COMMIT, "manifest_sha256": R13_MANIFEST,
                             "identity": gate.RELEASE13_IDENTITY},
        "installed_r13": gate.RELEASE13_IDENTITY,
        "release_transition": {"from": 13, "to": 14, "source": "candidate-install-update"},
        "update": {"command": "install --update", "exit_code": 0},
        "update_receipt": {"kind": "updated", "exit_code": 0, "still": 14, "target": 14},
        "installed_tuple": {"schema_digest": CANDIDATE["schema_digest"]},
        "browser_profile_removed": True,
        "service_manager": {"manager": "systemd-user"},
        "run_roots": {"data": "/test/data"},
        "live_runtimes": [{"data_root": "/test/data", "runtime_instance_id": "running"}],
        "sentinel": {"at_settlement": {"alive": True}},
        "steps": [{"stage": stage, "state": "pass"} for stage in (
            "pinned_sources", "native_r13_install", "r13_authoring", *(("required_prompt",) if prompt else ()),
            "native_r14_update", "r14_reopen", "native_end")],
        "timelines": [],
    }
    projects = {}
    for i in range(1 if prompt else 2):
        project, timeline = f"p{i}", f"t{i}"
        draft = {"realmId": "realm", "actorId": "actor", "projectId": project, "timelineId": timeline,
                 "ownerId": f"owner{i}", "intentId": f"intent{i}", "payloadSha256": "1" * 64}
        config = {"projectId": project, "timelineId": timeline, "savedLabel": "Old edit", "draftLabel": "Pending"}
        accepted = {"revision": "rev-1", "publication": "pub-1"}
        row["timelines"].append({
            "config": config,
            "author": browser_step("author", gate.RELEASE13_APP_COMMIT, R13_MANIFEST, draft=draft,
                                   blockedPosts=1, accepted=accepted),
            "reopen": browser_step("reopen", R14_APP, R14_MANIFEST, draft=draft.copy(),
                                   reopenedLabel="Old edit", draftVisible=True),
        })
        projects[project] = {"timelines": {timeline: {
            "revision_id": "rev-1", "publication_id": "pub-1",
            "history": [{"revision_sha256": value * 64} for value in ("2", "3")],
        }}}
    if prompt:
        row["prompt"] = browser_step(
            "prompt", gate.RELEASE13_APP_COMMIT, R14_MANIFEST, draft=draft.copy(),
            manifest_reads=2, browserManifestSha256=R14_MANIFEST,
            prompt={"title": "Update Astrid Required Show how",
                    "reason": "This update is required: Required test release.", "action": "Update now"},
        )
    else:
        row["realm_before"] = {"realm_id": "realm", "projects": projects}
        row["realm_after"] = deepcopy(row["realm_before"])
    return row


@pytest.mark.parametrize("contract", matrix.FIX17_REQUIRED_UPDATE_MATRIX)
def test_gate_accepts_exact_evidence_and_rejects_a_replaced_queue(contract):
    job_id = contract["job_id"]
    row = row_fixture(contract["leg"] == matrix.FIX17_PROMPT_LEG)
    assert gate._fix17_browser_errors(job_id, contract, row, CANDIDATE) == []
    row["timelines"][0]["reopen"]["draft"]["ownerId"] = "different-owner"
    assert any("save-queue" in e for e in gate._fix17_browser_errors(job_id, contract, row, CANDIDATE))


def test_gate_rejects_old_publication_floor_wrong_served_bytes_and_hidden_recovery():
    contract = matrix.FIX17_REQUIRED_UPDATE_MATRIX[0]
    row = row_fixture(True)
    row["prompt"]["browserManifestSha256"] = R13_MANIFEST
    row["timelines"][0]["reopen"]["draftVisible"] = False
    candidate = {**CANDIDATE, "required": {"release": 13, "reason": "Old reason"}}
    errors = gate._fix17_browser_errors(contract["job_id"], contract, row, candidate)
    assert any("does not require" in e for e in errors)
    assert any("prompt" in e for e in errors)
    assert any("save-queue" in e for e in errors)


def test_gate_rejects_missing_old_realm_history():
    contract = matrix.FIX17_REQUIRED_UPDATE_MATRIX[1]
    row = row_fixture(False)
    row["realm_after"]["projects"]["p1"]["timelines"]["t1"]["history"].pop()
    assert any("history" in e for e in gate._fix17_browser_errors(contract["job_id"], contract, row, CANDIDATE))


def test_inventory_follows_all_pages_and_reads_each_revision():
    timeline = "/v1/projects/p/timelines/t"
    head = {"revision_id": "r2", "publication_id": "u2"}
    calls = []
    replies = {
        "/v1/realm": {"realm_id": "our-realm"},
        "/v1/projects?limit=50": {"items": [], "next_cursor": "p/2"},
        "/v1/projects?limit=50&cursor=p%2F2": {
            "items": [{"project_id": "p", "realm_id": "our-realm", "name": "Project"}], "next_cursor": None},
        "/v1/projects/p/timelines?limit=50": {"items": [{"timeline_id": "t"}], "next_cursor": None},
        timeline: {"name": "Timeline", "head_revision_id": "r2", "head_publication_id": "u2"},
        timeline + "/commits?limit=50": {"head": head, "next_cursor": "older", "items": [{
            **head, "intent": "recorded", "previous": {"revision_id": "r1", "publication_id": "u1"}}]},
        timeline + "/commits?limit=50&cursor=older": {"head": head, "next_cursor": None, "items": [{
            "revision_id": "r1", "publication_id": "u1", "intent": "bootstrap", "previous": None}]},
        timeline + "/composition-revisions/r1": {
            "revision_id": "r1", "project_id": "p", "timeline_id": "t", "payload": {"config": {"name": "old"}}},
        timeline + "/composition-revisions/r2": {
            "revision_id": "r2", "project_id": "p", "timeline_id": "t", "payload": {"config": {"name": "new"}}},
    }

    def request(method, route):
        assert method == "GET"
        calls.append(route)
        return replies[route]

    client = SimpleNamespace(request=request)
    result = live.realm_inventory(client)
    observed = result["projects"]["p"]["timelines"]["t"]["history"]
    assert set(calls) == set(replies)
    assert result["realm_id"] == "our-realm"
    assert [entry["revision_id"] for entry in observed] == ["r2", "r1"]
    assert observed[0]["revision_sha256"] != observed[1]["revision_sha256"]
    replies[timeline + "/commits?limit=50&cursor=older"]["next_cursor"] = "older"
    with pytest.raises(RuntimeError, match="repeated page cursor"):
        live.realm_inventory(client)
