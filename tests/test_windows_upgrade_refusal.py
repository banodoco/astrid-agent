"""The Windows refusal is a separate, deliberately narrow lifecycle."""
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import journey_version_jumps as jumps
from scripts import check_journey_evidence as gate
from tests.test_journey_evidence import release1_receipts


@pytest.mark.parametrize("exit_code,message,change,ok", [
    (1, jumps.WINDOWS_REFUSAL, None, True),
    (0, jumps.WINDOWS_REFUSAL, None, False),
    (1, "unrelated error", None, False),
    (1, "prefix " + jumps.WINDOWS_REFUSAL, None, False),
    (1, jumps.WINDOWS_REFUSAL, "marker", False),
    (1, jumps.WINDOWS_REFUSAL, "missing", False),
    (1, jumps.WINDOWS_REFUSAL, "task", False),
    (1, jumps.WINDOWS_REFUSAL, "selection", False),
])
def test_narrow_refusal_requires_message_and_preserved_evidence(tmp_path, monkeypatch, exit_code, message, change, ok):
    root, data, evidence = tmp_path / "program", tmp_path / "data", tmp_path / "evidence"
    args = SimpleNamespace(job_id="direct-windows", leg="windows-upgrade-refusal", mode="direct", shell="powershell.exe",
                           candidate_checkouts={}, service_manager_prerequisite={}, app_checkout=tmp_path, candidate=tmp_path / "candidate")
    manifest = {"release": 1}
    monkeypatch.setattr(jumps, "release0_admission", lambda args: (tmp_path, tmp_path / "r0", {}))
    monkeypatch.setattr(jumps.journey, "selected_manifest", lambda *a, **kw: (tmp_path / "manifest", manifest))
    monkeypatch.setattr(jumps.journey, "read_json", lambda path: {})
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *a: manifest)
    monkeypatch.setattr(jumps.journey, "runner_identity", lambda: {})
    monkeypatch.setattr(jumps.platform, "platform", lambda: "Windows-test")
    monkeypatch.setattr(jumps, "bootstrap_release0", lambda *a: None)
    calls = []
    def installation(*args):
        calls.append(True)
        path = root / ("other" if change == "selection" and len(calls) > 1 else "old")
        return path, data, {"release": 0}
    monkeypatch.setattr(jumps.journey, "installation", installation)
    monkeypatch.setattr(jumps, "installed_release", lambda v: v["release"])
    monkeypatch.setattr(jumps.journey, "selected_tuple", lambda *a: {})
    monkeypatch.setattr(jumps, "windows_task_definition", lambda evidence, phase: "b" * 64 if change == "task" and phase == "after" else "a" * 64)
    def forbidden(*args, **kwargs):
        pytest.fail("Refusal must not start, seed, snapshot, wait for, or inspect the R0 owner")
    for name in ("start_direct_runtime", "live_workspace", "task_after_update"):
        monkeypatch.setattr(jumps, name, forbidden)
    for name in ("wait_ready", "assert_direct_service_ownership", "verify", "sentinel_observation"):
        monkeypatch.setattr(jumps.journey, name, forbidden)
    def run(*args, **kwargs):
        marker = data / "tst-windows-refusal-marker.txt"
        if change == "marker": marker.write_text("changed")
        if change == "missing": marker.unlink()
        kwargs["stdout"].write(message)
        return SimpleNamespace(returncode=exit_code)
    monkeypatch.setattr(jumps.subprocess, "run", run)
    if ok:
        result = jumps.execute_windows_refusal(args, root, data, evidence, None, None)
        assert result["status"] == "pass"
        assert result["scheduled_task"]["before_sha256"] == result["scheduled_task"]["after_sha256"]
    else:
        with pytest.raises(RuntimeError):
            jumps.execute_windows_refusal(args, root, data, evidence, None, None)
        import json
        assert json.loads((evidence / "journey-result.json").read_text())["status"] == "fail"


def test_task_evidence_exports_definition_only(tmp_path, monkeypatch):
    calls = []
    def powershell(command, *args, **kwargs):
        calls.append(command)
        return "<Task/>"
    monkeypatch.setattr(jumps.journey.windows, "powershell", powershell)
    assert jumps.windows_task_definition(tmp_path, "before") == jumps.windows_task_definition(tmp_path, "after")
    assert all("Export-ScheduledTask" in command and "Start-ScheduledTask" not in command for command in calls)


def test_aggregate_requires_both_windows_proofs(tmp_path):
    import json
    candidate = release1_receipts(tmp_path)
    path = tmp_path / "direct-windows" / "journey-result.json"
    valid = json.loads(path.read_text())
    assert gate.evaluate_release1_matrix(candidate, tmp_path) == []
    mutations = [
        lambda r: r.pop("windows_upgrade_refusal"),
        lambda r: r["checks"].update(workspace_content="not-run"),
        lambda r: r["windows_upgrade_refusal"].update(status="fail"),
        lambda r: r["windows_upgrade_refusal"]["refusal"].update(exit_code=0),
        lambda r: r["windows_upgrade_refusal"]["refusal"].update(message="other failure"),
        lambda r: r["windows_upgrade_refusal"]["checks"].update(data_marker="not-run"),
        lambda r: r["windows_upgrade_refusal"]["scheduled_task"].update(after_sha256="d" * 64),
        lambda r: r["windows_upgrade_refusal"].pop("data_marker"),
        lambda r: r["windows_upgrade_refusal"]["checks"].update(scheduled_task="not-run"),
        lambda r: r["windows_upgrade_refusal"].update(final_cleanup="fail"),
        lambda r: r["windows_upgrade_refusal"].update(candidate_checkouts={}),
        lambda r: r["windows_upgrade_refusal"].update(receipt_kind="diagnostic-only"),
    ]
    for mutate in mutations:
        bad = copy.deepcopy(valid)
        mutate(bad)
        path.write_text(json.dumps(bad))
        assert gate.evaluate_release1_matrix(candidate, tmp_path)


def test_aggregate_uses_final_receipt_from_distinct_artifact_directories(tmp_path):
    import json
    import shutil
    (tmp_path / "source").mkdir()
    candidate = release1_receipts(tmp_path / "source")
    artifacts = tmp_path / "artifacts"
    for contract in gate.RELEASE1_MATRIX:
        job = contract["job_id"]
        artifact = artifacts / ("journey-receipt-" + job)
        for phase in ("before-cleanup", "after-cleanup"):
            dest = artifact / phase
            dest.mkdir(parents=True)
            shutil.copyfile(tmp_path / "source" / job / "journey-result.json", dest / "journey-result.json")
        before = artifact / "before-cleanup" / "journey-result.json"
        before.write_text(json.dumps({"job_id": job, "status": "not-run"}))
    assert not gate.evaluate_release1_matrix(candidate, artifacts)
    final = artifacts / "journey-receipt-direct-windows" / "after-cleanup" / "journey-result.json"
    final.unlink()
    assert any("direct-windows: missing receipt" in e for e in gate.evaluate_release1_matrix(candidate, artifacts))
    shutil.copyfile(tmp_path / "source" / "direct-windows" / "journey-result.json", final)
    duplicate = artifacts / "duplicate" / "after-cleanup"
    duplicate.mkdir(parents=True)
    shutil.copyfile(final, duplicate / "journey-result.json")
    assert any("duplicate receipt for direct-windows" in e for e in gate.evaluate_release1_matrix(candidate, artifacts))


def test_workflow_runs_both_windows_checks_and_keeps_artifacts_separate():
    text = (Path(__file__).parents[1] / ".github/workflows/journey.yml").read_text()
    assert '--leg windows-upgrade-refusal' in text
    assert '$freshExit -eq 0' in text
    assert 'if ($refusalExit -ne 0) { exit $refusalExit }' in text
    assert 'windows_upgrade_refusal -NotePropertyValue $refusal' in text
    download = text.split('- name: Download every per-job receipt', 1)[1].split('- name: Aggregate the exact Release-1', 1)[0]
    assert 'merge-multiple: false' in download


def test_refusal_driver_avoids_process_checks_even_during_cleanup(tmp_path, monkeypatch):
    import argparse
    import json
    from scripts import ci_journey as journey

    run_root = tmp_path / "run"
    root, data = run_root / "program", run_root / "data"
    args = argparse.Namespace(run_root=run_root, root=root, data_root=data,
                              leg="windows-upgrade-refusal", mode="direct", shell="powershell.exe")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(journey, "runner_identity", lambda: {})
    monkeypatch.setattr(journey, "assert_runner_identity", lambda *_: None)
    calls = []
    monkeypatch.setattr(journey, "assert_host_available", lambda leg, *a, **kw: calls.append((leg, kw)))
    def forbidden(*args, **kwargs):
        pytest.fail("Refusal wrapper must not inspect runtime processes or take diagnostics snapshots")
    for name in ("sentinel_process", "sentinel_observation", "runtime_processes", "snapshot_failure_evidence"):
        monkeypatch.setattr(journey, name, forbidden)
    def execute(args, root, data, evidence, sentinel, birth):
        assert sentinel is None and birth is None
        root.mkdir()
        (root / "current").write_text("r0")
        journey.write_json(evidence / "journey-result.json", {"status": "pass"})
        return {"status": "pass"}
    monkeypatch.setattr(journey, "_execute_owned", execute)
    monkeypatch.setattr(journey, "installation", lambda *a: (root / "version.json", data, {}))
    monkeypatch.setattr(journey, "installed_cli", lambda *a, **kw: (root / "current").unlink())
    assert journey.execute(args)["final_cleanup"] == "pass"
    final = run_root / "evidence-upload" / "after-cleanup" / "journey-result.json"
    assert json.loads(final.read_text())["final_cleanup"] == "pass"
    assert calls == [("fresh-install", {"skip_process_check": True}),
                     ("cleanup", {"skip_process_check": True})]
