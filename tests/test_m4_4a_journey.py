"""Focused provisional M4-4a adapter, schema, and aggregate checks."""

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import ci_journey, check_journey_evidence, journey_matrix


def _candidate():
    return {name: letter * 40
            for name, letter in zip(("app", "agent", "runtime", "pinokio"), "abcd")}


def _write_sidecar(receipt_dir, name, value):
    path = receipt_dir / "pinokio-backend" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, str):
        path.write_text(value, encoding="utf-8")
    else:
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return {"path": path.relative_to(receipt_dir).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _hosted_receipts(tmp_path, *, first_click="refused"):
    candidate = _candidate()
    events = (
        "action_built", "launcher_ref_advanced", "first_click_begin",
        "launcher_pull_begin", "launcher_pull_end", "installer_begin",
        "ready_sentinel", "installer_end", "action_end", "health_probe_begin",
        "health_probe_pass", "stop_begin", "stop_end", "retry_begin", "retry_end",
        "start_begin", "start_end",
    )
    for contract in journey_matrix.M4_4A_BACKEND_MATRIX:
        receipt_dir = tmp_path / contract["job_id"]
        sidecars = {}
        negative_controls = {
            "busy_stop": {"status": "pass", "outcome": "refused",
                          "same_session_retry": "stopped"},
            "unknown_stop": {"status": "pass", "outcome": "refused",
                             "same_session_retry": "stopped"},
            "identity_mismatch": {"status": "pass", "mismatch_observed": True,
                                  "readiness_suppressed": True},
            "instance_mismatch": {"status": "pass", "mismatch_observed": True,
                                  "readiness_suppressed": True},
        }
        first = {
            "schema_version": 1, "os": contract["os"], "mode": contract["mode"],
            "built_before_ref_advance": True,
            "launcher": {"ref": journey_matrix.M4_4A_OLD_LAUNCHER_REF},
            "ordered_steps": [
                {"kind": "git-shell", "bootstrap_action": None},
                {"kind": "shell", "bootstrap_action": "update-app"},
            ],
        }
        retry = {
            "schema_version": 1, "os": contract["os"], "mode": contract["mode"],
            "launcher": {"sha": candidate["pinokio"]}, "ordered_steps": [],
        }
        sidecars["action-array-first.json"] = _write_sidecar(
            receipt_dir, "action-array-first.json", first)
        sidecars["action-array-retry.json"] = _write_sidecar(
            receipt_dir, "action-array-retry.json", retry)
        event_path = receipt_dir / "pinokio-backend" / "events.jsonl"
        event_path.write_text("".join(
            json.dumps({"schema_version": 1, "seq": sequence, "timestamp": sequence,
                        "event": event, "os": contract["os"], "mode": contract["mode"]}) + "\n"
            for sequence, event in enumerate(events, 1)), encoding="utf-8")
        sidecars["events.jsonl"] = {
            "path": event_path.relative_to(receipt_dir).as_posix(),
            "sha256": hashlib.sha256(event_path.read_bytes()).hexdigest(),
        }
        for name in journey_matrix.M4_4A_BACKEND_SIDECARS:
            if name in sidecars:
                continue
            if name.endswith(".log"):
                value = "captured\n"
            else:
                value = {"schema_version": 1, "os": contract["os"], "mode": contract["mode"]}
                if name == "negative-controls.json":
                    value["controls"] = negative_controls
            sidecars[name] = _write_sidecar(receipt_dir, name, value)
        backend = {
            "schema_version": 1, "os": contract["os"], "mode": contract["mode"],
            "hosted_lifecycle": {"status": "pass"}, "sidecars": sidecars,
            "old_launcher": {"ref": journey_matrix.M4_4A_OLD_LAUNCHER_REF,
                             "sha256": journey_matrix.M4_4A_OLD_UPDATE_SHA256,
                             "validated": True},
            "stopped_path_agent": {
                "ref": candidate["agent"],
                "required_ancestors": list(journey_matrix.M4_4A_REQUIRED_AGENT_ANCESTORS),
                "validated": True,
            },
            "first_click": {"outcome": first_click},
            "readiness_after_update": {"status": "pass", "manual_start": False,
                                       "first_probe_http_status": 200},
            "stop_start": {"stop": {"result": "stopped", "next_action": "Start"},
                           "start": {"result": "ready", "next_action": "Stop"}},
            "negative_controls": negative_controls,
            "historical_compatibility": {"status": "pass", "vectors": ["H1", "H2", "H3"]},
            "not_applicable": [],
        }
        if first_click == "refused":
            backend["stop_after_refusal"] = {"result": "stopped"}
            backend["retry_after_stop"] = {"result": "succeeded"}
        else:
            backend["not_applicable"] = ["A3"]
        row = {
            "schema_version": 2, "receipt_kind": "release1-installed-lifecycle",
            "job_id": contract["job_id"], "mode": contract["mode"],
            "scenario": contract["leg"], "status": "pass",
            "runner_image": contract["runner"], "shell": contract["shell"],
            "evidence_class": "hosted-lifecycle", "source_override_test_mode": False,
            "final_cleanup": "pass",
            "runner_provenance": {
                "RUNNER_OS": {"macos": "macOS", "linux": "Linux", "windows": "Windows"}[contract["os"]],
                "ImageOS": {"macos": "macos14", "linux": "ubuntu24", "windows": "win22"}[contract["os"]],
                "ImageVersion": "test",
            },
            "candidate_checkouts": {name: {"sha": value} for name, value in candidate.items()},
            "service_manager_prerequisite": {"outcome": "explicit-owner", "managed_by": "pinokio",
                                             "foreground_fallback": False},
            "source_unit_evidence": {"status": "pass",
                                     "kind": "pinokio-action-array-source-check",
                                     "substitute_for_hosted": False},
            "pinokio_backend": backend,
        }
        (receipt_dir / "journey-result.json").write_text(json.dumps(row), encoding="utf-8")
    return candidate


def test_backend_matrix_is_explicit_and_cannot_admit_other_modes():
    assert journey_matrix.M4_4A_BACKEND_JOB_IDS == (
        "pinokio-backend-macos", "pinokio-backend-linux", "pinokio-backend-windows")
    assert journey_matrix.validate_m4_4a_backend_job(
        "pinokio-backend-windows", "pinokio-backend")["shell"] == "powershell.exe"
    with pytest.raises(ValueError, match="requires mode"):
        journey_matrix.validate_m4_4a_backend_job("pinokio-backend-linux", "direct")


def test_workflow_has_a_focused_backend_scope_and_explicit_provenance_inputs():
    workflow = (Path(__file__).parents[1] / ".github/workflows/journey.yml").read_text()
    assert "old_launcher_ref:" in workflow
    assert "stopped_path_agent_ref:" in workflow
    assert "m4_4a_backend_only:" in workflow
    assert "if backend_only:" in workflow
    assert "--aggregate-m4-4a" in workflow


def test_old_launcher_input_fails_closed_before_git_execution(tmp_path):
    with pytest.raises(ValueError, match="full 40-character"):
        ci_journey.validate_old_launcher_provenance(tmp_path, "missing")


def test_known_provisional_agent_ref_is_refused_before_checkout_access():
    args = argparse.Namespace(stopped_path_agent_ref=journey_matrix.M4_4A_PROVISIONAL_AGENT_REF)
    with pytest.raises(ValueError, match="provisional Agent base"):
        ci_journey.validate_m4_4a_product_ref(args)


def test_candidate_without_paired_stopped_path_ancestry_fails_closed(tmp_path, monkeypatch):
    ref = "e" * 40
    args = argparse.Namespace(stopped_path_agent_ref=ref, candidate_agent_checkout=tmp_path)
    monkeypatch.setattr(ci_journey, "git_source", lambda *_: {"sha": ref})
    monkeypatch.setattr(ci_journey.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=1))
    with pytest.raises(ValueError, match="lacks the paired stopped-path commits"):
        ci_journey.validate_m4_4a_product_ref(args)


def test_backend_source_plan_uses_schema_compatible_sidecar_refs(tmp_path, monkeypatch):
    candidate = _candidate()
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    args = argparse.Namespace(
        old_launcher_ref=journey_matrix.M4_4A_OLD_LAUNCHER_REF,
        stopped_path_agent_ref=candidate["agent"], pinokio_checkout=tmp_path,
        candidate_agent_checkout=tmp_path, candidate=candidate_path,
    )
    monkeypatch.setattr(ci_journey, "validate_old_launcher_provenance", lambda *_: {
        "ref": journey_matrix.M4_4A_OLD_LAUNCHER_REF,
        "sha256": journey_matrix.M4_4A_OLD_UPDATE_SHA256, "validated": True})
    monkeypatch.setattr(ci_journey, "validate_m4_4a_product_ref", lambda *_: {
        "ref": candidate["agent"], "validated": True})
    monkeypatch.setattr(ci_journey, "git_source", lambda *_: {"sha": candidate["pinokio"]})
    actions = iter((
        {"run": [{"kind": "git-shell", "bootstrap_action": None},
                 {"kind": "shell", "bootstrap_action": "update-app"}]},
        {"run": [{"kind": "custom", "name": "preflightStop"}]},
    ))
    monkeypatch.setattr(ci_journey, "build_pinokio_action", lambda *_args, **_kwargs: next(actions))
    receipt = ci_journey.prepare_pinokio_backend(args, tmp_path / "evidence")
    assert receipt["evidence_kind"] == "source-unit"
    assert receipt["hosted_lifecycle"]["status"] == "not-run"
    assert receipt["first_click"]["status"] == "blocked"
    assert receipt["retry_after_stop"]["status"] == "blocked"
    assert receipt["readiness_after_update"]["status"] == "blocked"
    assert receipt["stop_start"]["status"] == "blocked"
    assert set(receipt["negative_controls"]) == {
        "busy_stop", "unknown_stop", "identity_mismatch", "instance_mismatch"}
    assert all(row["os"] == ci_journey.journey_os_label()
               and row["mode"] == "pinokio-backend"
               for row in receipt["negative_controls"].values())
    for sidecar in receipt["sidecars"].values():
        assert set(sidecar) == {"path", "sha256"}
        assert (tmp_path / "evidence" / sidecar["path"]).is_file()


def test_action_builder_uses_windows_powershell_51_route(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(ci_journey.sys, "platform", "win32")
    monkeypatch.setattr(ci_journey.shutil, "which", lambda _name: r"C:\Program Files\nodejs\node.exe")
    monkeypatch.setattr(ci_journey, "_export_pinokio_source", lambda *_: None)

    def run(argv, **_kwargs):
        captured["argv"] = argv
        return SimpleNamespace(returncode=0, stdout='{"run": []}', stderr="")

    monkeypatch.setattr(ci_journey.subprocess, "run", run)
    assert ci_journey.build_pinokio_action(tmp_path, "a" * 40) == {"run": []}
    assert captured["argv"][:5] == ["powershell.exe", "-NoProfile", "-NonInteractive",
                                    "-ExecutionPolicy", "Bypass"]


@pytest.mark.parametrize("first_click", ["refused", "succeeded"])
def test_m4_4a_aggregate_accepts_each_complete_contract_branch(tmp_path, first_click):
    candidate = _hosted_receipts(tmp_path, first_click=first_click)
    assert not check_journey_evidence.evaluate_m4_4a_backend_matrix(candidate, tmp_path)


def test_m4_4a_aggregate_rejects_missing_skipped_and_incomplete_receipts(tmp_path):
    candidate = _hosted_receipts(tmp_path)
    missing = tmp_path / "pinokio-backend-macos" / "journey-result.json"
    missing.unlink()
    assert any("pinokio-backend-macos: missing" in error
               for error in check_journey_evidence.evaluate_m4_4a_backend_matrix(candidate, tmp_path))

    candidate = _hosted_receipts(tmp_path)
    receipt = tmp_path / "pinokio-backend-linux" / "journey-result.json"
    row = json.loads(receipt.read_text(encoding="utf-8"))
    row["status"] = "skipped"
    row["pinokio_backend"]["retry_after_stop"]["result"] = "not-run"
    receipt.write_text(json.dumps(row), encoding="utf-8")
    errors = check_journey_evidence.evaluate_m4_4a_backend_matrix(candidate, tmp_path)
    assert any("status 'skipped'" in error for error in errors)
    assert any("successful retry after Stop" in error for error in errors)


def test_source_only_receipt_is_not_hosted_lifecycle_evidence(tmp_path):
    candidate = _hosted_receipts(tmp_path)
    receipt = tmp_path / "pinokio-backend-windows" / "journey-result.json"
    row = json.loads(receipt.read_text(encoding="utf-8"))
    row["evidence_class"] = "source-unit"
    row["pinokio_backend"]["hosted_lifecycle"] = {"status": "not-run"}
    receipt.write_text(json.dumps(row), encoding="utf-8")
    errors = check_journey_evidence.evaluate_m4_4a_backend_matrix(candidate, tmp_path)
    assert any("hosted lifecycle evidence" in error for error in errors)
    assert any("evidence_class" in error for error in errors)


def test_m4_4a_aggregate_never_admits_a_source_override(tmp_path):
    candidate = _hosted_receipts(tmp_path)
    receipt = tmp_path / "pinokio-backend-macos" / "journey-result.json"
    row = json.loads(receipt.read_text(encoding="utf-8"))
    row["source_override_test_mode"] = True
    receipt.write_text(json.dumps(row), encoding="utf-8")
    assert any("source override is not admitted" in error
               for error in check_journey_evidence.evaluate_m4_4a_backend_matrix(candidate, tmp_path))
