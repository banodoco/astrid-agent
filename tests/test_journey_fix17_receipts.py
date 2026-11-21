"""Release 14 qualification boundary controls."""

from pathlib import Path
import json
import sys

import yaml


def test_windows_journey_drivers_and_artifacts_use_the_same_short_root():
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "journey.yml"
    journey = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]["journey"]
    assert "matrix.os == 'windows' && 'f17'" in journey["env"]["JOURNEY_ROOT_NAME"]
    steps = {step.get("name"): step for step in journey["steps"]}
    assert '$runRoot = Join-Path $env:RUNNER_TEMP $env:JOURNEY_ROOT_NAME' in steps[
        "Run the matrix-owned lifecycle driver"]["run"]
    assert "Join-Path $env:RUNNER_TEMP 'f17r'" in steps["Run the matrix-owned lifecycle driver"]["run"]
    assert 'run_root="$RUNNER_TEMP/$JOURNEY_ROOT_NAME"' in steps["Run the Pinokio launcher row driver"]["run"]
    assert "$env:JOURNEY_ROOT_NAME/evidence-upload" in steps[
        "Validate the named lifecycle evidence artifact boundary"]["run"]
    assert steps["Retain only the named lifecycle evidence after a lifecycle failure"]["with"]["path"] == (
        "${{ runner.temp }}/${{ env.JOURNEY_ROOT_NAME }}/evidence-upload"
    )


def test_native_update_gate_requires_windows_public_installer_receipt():
    from scripts import check_journey_evidence as gate

    errors = gate._release13_row_errors("direct-windows-r13-update", {
        "update": {"command": "install --update", "exit_code": 0},
    }, 14, "windows")
    assert any("candidate install --update receipt" in error for error in errors)


def test_windows_receipt_is_tied_to_the_actual_install_invocation():
    from scripts import check_journey_evidence as gate

    invocation = {"started_at": 10, "finished_at": 20, "previous_operation_id": "a" * 32}
    receipt = {"format": "astrid-last-run/v1", "mode": "update", "operation_id": "b" * 32,
               "started_at": 11, "finished_at": 15}
    assert gate._windows_receipt_matches_invocation(receipt, invocation)
    assert not gate._windows_receipt_matches_invocation({**receipt, "operation_id": "a" * 32}, invocation)
    assert not gate._windows_receipt_matches_invocation({**receipt, "started_at": 9}, invocation)
    assert not gate._windows_receipt_matches_invocation({**receipt, "finished_at": 21}, invocation)


def test_native_update_gate_rejects_metadata_only_reopen_without_source_ids():
    from scripts import check_journey_evidence as gate

    errors = gate._release13_row_errors("direct-linux-r13-update", {
        "timeline_save": {"saved": True, "saved_name": "same"},
        "timeline_reopen": {"reopened": True, "saved_name": "same"},
    }, 14)
    assert any("timeline edit/save evidence" in error for error in errors)
    assert any("timeline reopen" in error for error in errors)


def test_native_update_gate_binds_fresh_composition_to_its_source():
    from scripts import check_journey_evidence as gate

    saved = {
        "project_id": "r13-project", "timeline_id": "r13-timeline", "base_revision_id": "r13-revision",
        "accepted_revision_id": "r14-revision", "publication_id": "r14-publication",
        "document_sha256": "1" * 64, "track_id": "V1", "edit": "Edited track", "saved": True,
    }
    row = {
        "timeline_seed": {"project": "r13-project", "timeline": "r13-timeline", "track_id": "V1",
                          "initial_label": "Original track", "revision_id": "r13-revision",
                          "publication_id": "r13-publication"},
        "timeline_save": saved,
        "timeline_reopen": {**saved, "reopened": True},
    }
    def save_errors():
        return [error for error in gate._release13_row_errors("direct-linux-r13-update", row, 14)
                if "timeline edit/save evidence" in error or "timeline reopen" in error]

    assert save_errors() == []
    row["timeline_reopen"]["project_id"] = "different-project"
    assert any("timeline reopen" in error for error in save_errors())
    row["timeline_reopen"]["project_id"] = saved["project_id"]
    saved["base_revision_id"] = saved["accepted_revision_id"]
    assert any("timeline edit/save evidence" in error for error in save_errors())


def test_r14_pinokio_gate_requires_fresh_open_and_stopped_runtime(tmp_path):
    import hashlib
    import json

    from scripts import check_journey_evidence as gate

    candidate = {key: digit * 40 for key, digit in zip(("app", "agent", "runtime", "pinokio"), "abcd")}
    candidate.update(manifest_sha256="e" * 64, schema_digest="sha256:" + "f" * 64,
                     release=14, required={"release": 14, "reason": "Required."})
    frozen = {key: candidate[key] for key in ("app", "agent", "runtime", "pinokio", "manifest_sha256")}
    manifest = {"release": 14, "required": candidate["required"], "agent": {"sha": candidate["agent"]},
                "runtime": {"sha": candidate["runtime"], "schema_digest": candidate["schema_digest"]}}
    roots = {"selectedProgramRoot": "/test/program", "selectedDataRoot": "/test/data"}
    live = {"runtimeInstanceId": "runtime", "runtimeIdentity": {"release": 14, "managed_by": "pinokio"},
            "schemaDigest": candidate["schema_digest"]}
    steps = {
        "install": {"manifest_sha256": candidate["manifest_sha256"], "manifest": manifest, "selected_program_root": "/test/program",
                    "selected_data_root": "/test/data", "installed_manifest_sha256": hashlib.sha256(
                        (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()).hexdigest()},
        "start": {**roots, **live, "readySentinel": True},
        "open_app": {**live, "target": "https://astrid.haus/?connect=1", "connected": True,
                     "manifestVerified": True, "manifestSha256": candidate["manifest_sha256"], "appBuild": candidate["app"]},
        "stop": {**roots, "stopped": True, "runtimeInstanceId": "runtime", "sessionExitCode": 0},
    }
    row = {"schema": "pinokio-row-v2", "job_id": "pinokio-linux", "os": "linux",
           "pinokio_sha": candidate["pinokio"], "candidate": frozen,
           "stages": [{"name": name, "status": "pass", "detail": "verified", "observation": proof}
                      for name, proof in steps.items()]}
    receipt = tmp_path / "journey-result.json"
    receipt.write_text(json.dumps(row))
    assert gate.evaluate_release1_1_pinokio_matrix(candidate, tmp_path, ["pinokio-linux"]) == []
    steps["open_app"]["runtimeInstanceId"] = "other-runtime"
    receipt.write_text(json.dumps(row))
    assert any("Open Astrid" in error for error in gate.evaluate_release1_1_pinokio_matrix(candidate, tmp_path, ["pinokio-linux"]))


def test_fix17_child_failure_receipt_keeps_exit_and_sanitized_stderr(tmp_path, monkeypatch):
    from scripts import journey_version_jumps as jumps

    monkeypatch.setenv("FIX17_TEST_TOKEN", "receipt-secret-value")
    stdout = tmp_path / "child-stdout.log"
    stderr = tmp_path / "child-stderr.log"
    receipt = {"stage": "r13_authoring", "state": "running"}
    try:
        jumps._fix17_subprocess(
            [sys.executable, "-c", "import sys; print('out'); print('token=receipt-secret-value', file=sys.stderr); sys.exit(7)"],
            stdout, stderr, timeout=5)
    except RuntimeError as error:
        receipt.update(child_exit_code=error.child_exit_code, child_stderr=error.child_stderr)
    else:
        raise AssertionError("failing child unexpectedly succeeded")

    assert stdout.read_text().strip() == "out"
    assert stderr.read_text().strip() == "token=[redacted]"
    assert receipt["child_exit_code"] == 7
    assert "token=[redacted]" in receipt["child_stderr"]
    assert "receipt-secret-value" not in json.dumps(receipt)
