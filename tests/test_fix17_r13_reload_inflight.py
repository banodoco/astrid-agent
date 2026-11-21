"""Targeted controls for the FIX-17 R13 in-flight reload rehearsal row."""

from io import StringIO
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import yaml

from scripts import journey_matrix as matrix
from scripts import check_journey_evidence as gate
from scripts import journey_hosted_update as hosted
from tests.test_fix17_required_update_harness import CANDIDATE, row_fixture


ROOT = Path(__file__).resolve().parents[1]
JOB_ID = "direct-linux-r13-reload-inflight"
LEG = "r13-reload-inflight"


def test_reload_inflight_row_is_registered_without_changing_the_existing_fix17_set():
    contract = matrix.validate_job(JOB_ID, "direct")
    assert contract == {
        "job_id": JOB_ID,
        "mode": "direct",
        "runner": "ubuntu-24.04",
        "shell": "sh",
        "os": "linux",
        "leg": LEG,
    }
    assert JOB_ID not in matrix.FIX17_JOB_IDS
    assert len(matrix.FIX17_JOB_IDS) == 9

    workflow = yaml.safe_load((ROOT / ".github/workflows/journey.yml").read_text(encoding="utf-8"))
    dispatch = workflow[True]["workflow_dispatch"]
    assert JOB_ID in dispatch["inputs"]["row"]["options"]
    assert dispatch["inputs"]["rehearsal"]["default"] is False
    assert "FIX-17 round 4 REHEARSAL" in workflow["run-name"]


def test_reload_inflight_receipt_cannot_pass_without_a_reload_receipt():
    row = reload_receipt_row()
    row["timelines"][0]["reload_inflight"] = {}
    errors = gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE)
    assert errors, "the evidence gate accepted a missing held-save/reload/R14-restoration receipt"


def reload_receipt_row():
    row = row_fixture(prompt=False)
    row["steps"] = [{"stage": stage, "state": "pass"} for stage in (
        "pinned_sources", "native_r13_install", "r13_authoring", "native_r14_update", "r14_reopen", "native_end")]
    row["timelines"] = row["timelines"][:1]
    timeline = row["timelines"][0]
    config, author = timeline["config"], timeline["author"]
    origin = "https://astrid.haus"
    candidate_build_sha256 = "f" * 64
    candidate_index_sha256 = "7" * 64
    build_sha = "d" * 64
    index_sha = "9" * 64
    timeline["reload_inflight"] = {
        "mode": LEG,
        "status": "pass",
        "origin": origin,
        "intercept_reverted": True,
        "https": {"initial_build_sha256": author["build_sha256"],
                  "candidate_build_sha256": candidate_build_sha256,
                  "candidate_index_sha256": candidate_index_sha256,
                  "candidate_manifest_sha256": CANDIDATE["manifest_sha256"],
                  "candidate_build_served": True, "candidate_page_served": True},
        "held_save": {"method": "POST", "path": "/v1/projects/p0/timelines/t0/commit", "held": True,
                      "release_after_reload": False, "body_sha256": author["draft"]["requestSha256"],
                      "key_sha256": author["draft"]["keySha256"]},
        "transition": {"origin": origin, "url": origin + "/build.json", "app_sha": CANDIDATE["app"],
                        "initial_app_sha": gate.RELEASE13_APP_COMMIT,
                        "build_sha256": candidate_build_sha256},
        "notice": {"text": "Editor updated · Reload", "visible": True, "original_document": True},
        "reload": {"clicked": True, "result": "reloaded"},
        "restore": {"app_sha": CANDIDATE["app"], "document_replaced": True,
                    "index_sha256": candidate_index_sha256, "project_id": config["projectId"],
                    "timeline_id": config["timelineId"], "persisted_label": config["draftLabel"],
                    "draft_digest": author["draft"]["payloadSha256"]},
        "r14_audit": {"requests": [
            {"method": "GET", "path": "/build.json", "source": "R13", "kind": "build-watcher"},
            {"method": "GET", "path": "/tools/video-editor", "source": "R13", "kind": "navigation"},
        ],
                       "r13_save_requests_after_switch": []},
    }
    return row


def test_reload_inflight_receipt_requires_guarded_reload_and_zero_post_switch_r13_saves():
    row = reload_receipt_row()
    assert gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE) == []

    receipt = row["timelines"][0]["reload_inflight"]
    receipt["reload"]["result"] = "unsafe-reload-fallback"
    assert any("guarded Reload" in error for error in gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE))

    receipt["reload"]["result"] = "reloaded"
    receipt["r14_audit"]["r13_save_requests_after_switch"] = [{"method": "POST"}]
    assert any("post-switch" in error for error in gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE))

    row = reload_receipt_row()
    row["timelines"][0]["reload_inflight"]["held_save"]["release_after_reload"] = True
    assert any("held across the reload" in error
               for error in gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE))

    row = reload_receipt_row()
    row["timelines"][0]["reload_inflight"]["notice"]["text"] = "Updated; reload"
    assert any("exact Reload notice" in error
               for error in gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE))

    row = reload_receipt_row()
    row["timelines"][0]["reload_inflight"]["restore"]["document_replaced"] = False
    assert any("same original" in error for error in gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE))


def test_hosted_intercept_wires_the_second_build_behind_an_activation_file(tmp_path, monkeypatch):
    hosts = tmp_path / "hosts"
    hosts.write_bytes(b"original hosts\\n")
    certs = tuple(tmp_path / name for name in ("ca.pem", "leaf.pem", "leaf.key"))
    for path in certs:
        path.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(hosted, "hosts_path", lambda: hosts)
    monkeypatch.setattr(hosted, "make_certs", lambda _directory: certs)
    monkeypatch.setattr(hosted, "spki_pin", lambda _leaf: "test-pin")
    monkeypatch.setattr(hosted, "_helper", lambda *args: list(args))

    launched = {}

    def fake_popen(argv, **kwargs):
        launched["argv"] = list(argv)
        launched["kwargs"] = kwargs
        ready = Path(argv[argv.index("--ready") + 1])
        ready.write_text("ready\\n", encoding="utf-8")
        return SimpleNamespace(poll=lambda: None, stdout=StringIO())

    monkeypatch.setattr(hosted.subprocess, "Popen", fake_popen)
    intercept = hosted.Intercept(
        {"/build.json": b"r13"},
        secondary_routes={"/build.json": b"r14"},
        activate=tmp_path / "r14-published",
    )
    intercept._must = lambda *_args: None
    try:
        intercept.start()
        argv = launched["argv"]
        assert argv[argv.index("--secondary-routes") + 1] == str(intercept.directory / "secondary-routes.json")
        assert argv[argv.index("--activate") + 1] == str(tmp_path / "r14-published")
        routes = json.loads((intercept.directory / "secondary-routes.json").read_text())
        assert "/build.json" in routes
        assert Path(routes["/build.json"]).read_bytes() == b"r14"
    finally:
        if intercept.directory is not None:
            shutil.rmtree(intercept.directory, ignore_errors=True)
