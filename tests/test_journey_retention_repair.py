"""Focused regressions for journey snapshot retention and seed limits."""
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from scripts import ci_journey as journey
from scripts import journey_artifact_boundary as boundary


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_nested_bootstraps_and_truthful_phase_observations(tmp_path, monkeypatch, platform):
    root = tmp_path / "run"
    evidence = root / "evidence" / "release0-to-release1"
    data = root / "data"
    monkeypatch.setattr(journey.sys, "platform", platform)
    # Exercise independent producer paths, including collisions in basename.
    sources = {"release0/release0-bootstrap.log": b"release zero",
               "release1/update-bootstrap.log": b"release one",
               "busy/update-bootstrap.log": b"busy refusal",
               "initial/install-bootstrap.log": b"initial"}
    for relative, content in sources.items():
        path = evidence / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    journey.write_json(evidence / "journey-result.json", {"status": "fail", "scenario": "release0-to-release1"})
    def manager(ev, _data, phase):
        journey.write_json(ev / f"manager-state-{phase}.json", {"unit_file": "real" if phase == "before-cleanup" else None})
        (ev / f"unit-file-{phase}.txt").write_text("unit" if phase == "before-cleanup" else "Unit file unavailable")
    monkeypatch.setattr(journey, "capture_manager_state", manager)
    monkeypatch.setattr(journey, "capture_runtime_service_logs", lambda *args: {
        "install.log": "not-produced-at-snapshot",
        "runtime-service.log": "not-produced-at-snapshot",
    })
    journey.snapshot_failure_evidence(evidence, root, data, "before-cleanup")
    shutil.rmtree(evidence / "busy")
    (evidence / "final-uninstall.log").write_text("cleanup result")
    journey.snapshot_failure_evidence(evidence, root, data, "after-cleanup")
    before = root / "evidence-upload" / "before-cleanup"
    after = root / "evidence-upload" / "after-cleanup"
    for relative, content in sources.items():
        name = ("release0-bootstrap.log" if platform == "win32" and relative.startswith("release0/") else
                "runtime-service-service-bootstrap-" + hashlib.sha256(relative.encode()).hexdigest() + ".log")
        assert (before / name).read_bytes() == content
        assert (after / name).read_bytes() == content
    b = journey.read_json(before / "journey-result.json")["failure_diagnostics"]["before-cleanup"]
    a = journey.read_json(after / "journey-result.json")["failure_diagnostics"]["after-cleanup"]
    assert b["uninstall"]["final_uninstall"] == "not-yet-run"
    assert a["uninstall"]["final_uninstall"] == "captured"
    assert b["unit_file"]["observation_status"] == "captured"
    assert a["unit_file"]["observation_status"] == "unavailable"
    assert b["install_log"]["source_observation"] == "not-produced-at-snapshot"
    assert b["runtime_service"]["status"] == "unavailable"
    assert b["service_manager"]["observation_status"] == "observed"
    assert "busy/update-bootstrap.log" in b["bootstrap"]["source_paths"]
    assert "busy/update-bootstrap.log" not in a["bootstrap"]["source_paths"]
    assert b["receipt"]["status"] == a["receipt"]["status"] == "captured"


@pytest.mark.parametrize("platform, relative", [
    ("linux", "home/.local/state/astrid-runtime/logs"),
    ("darwin", "home/Library/Logs/Astrid Runtime"),
    ("win32", "localappdata/Astrid Runtime/logs"),
])
def test_runtime_service_capture_uses_installer_service_log_path(tmp_path, monkeypatch, platform, relative):
    run_root = tmp_path / "run"
    log_dir = run_root / relative
    log_dir.mkdir(parents=True)
    (log_dir / "install.log").write_text("installer output\n")
    (log_dir / "runtime-service.log").write_text("service output\n")
    # A Runtime-process log is not a substitute for the service log and is not
    # an R-01 lifecycle member.
    (log_dir / "runtime.log").write_text("runtime process output\n")
    evidence = run_root / "evidence"
    monkeypatch.setattr(journey.sys, "platform", platform)

    observations = journey.capture_runtime_service_logs(evidence, run_root, "before-cleanup")

    assert observations == {"install.log": "captured", "runtime-service.log": "captured"}
    assert (evidence / "install.log").read_text() == "installer output\n"
    assert (evidence / "runtime-service.log").read_text() == "service output\n"
    assert not (evidence / "runtime-service-runtime.log").exists()


def test_runtime_service_absence_is_not_produced_not_collector_error(tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    (run_root / "home/.local/state/astrid-runtime/logs").mkdir(parents=True)
    monkeypatch.setattr(journey.sys, "platform", "linux")

    assert journey.capture_runtime_service_logs(run_root / "evidence", run_root, "before-cleanup") == {
        "install.log": "not-produced-at-snapshot",
        "runtime-service.log": "not-produced-at-snapshot",
    }


def test_seed_streams_are_bounded_and_oversized_stdout_fails_explicitly(tmp_path):
    request = tmp_path / "request.json"
    request.write_text("{}")
    script = tmp_path / "seed.py"
    script.write_text("import sys\nsys.stdout.buffer.write(b'a' * 400000)\nsys.stderr.buffer.write(b'b' * 400000)\n")
    log = tmp_path / "seed.json"
    with pytest.raises(RuntimeError, match="stdout exceeded"):
        journey.run_seed([sys.executable, str(script), "--request", str(request)], log, env=None, timeout=30)
    directory = tmp_path / "seed-diagnostics" / "0001"
    status = json.loads((directory / "exit.json").read_text())
    for name, value in (("stdout", b"a"), ("stderr", b"b")):
        stream = status["streams"][name]
        assert stream == {"captured_bytes": 400000, "retained_bytes": journey.SEED_STREAM_LIMIT,
                          "sha256": hashlib.sha256(value * 400000).hexdigest(), "truncated": True}
        assert (directory / f"{name}.txt").stat().st_size == journey.SEED_STREAM_LIMIT


def test_many_oversized_seed_diagnostics_are_bounded_and_disclosed(tmp_path):
    root = tmp_path / "run"
    evidence = root / "evidence" / "fresh"
    journey.write_json(evidence / "journey-result.json", {"status": "fail"})
    for i in range(1, journey.SEED_INVOCATION_LIMIT + 4):
        path = evidence / "seed-diagnostics" / f"{i:04d}" / "stderr.txt"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x" * (journey.SEED_FILE_LIMIT + 100))
    journey.retain_named_evidence(evidence, "before-cleanup")
    staged = root / "evidence-upload" / "before-cleanup"
    summary = journey.read_json(staged / "runtime-service-service-seed-summary.log")
    assert summary["omitted_invocations"] == 3
    assert len(summary["files"]) == journey.SEED_INVOCATION_LIMIT
    assert all(item["truncated"] and item["retained_bytes"] == journey.SEED_FILE_LIMIT for item in summary["files"])
    assert boundary.prepare(root / "evidence-upload") < boundary.MAX_ARTIFACT_BYTES
    shutil.rmtree(evidence / "seed-diagnostics")
    journey.retain_named_evidence(evidence, "after-cleanup")
    assert (root / "evidence-upload" / "after-cleanup" / "runtime-service-service-seed-summary.log").read_bytes() == (staged / "runtime-service-service-seed-summary.log").read_bytes()


def test_early_snapshot_reports_absence_without_fabricating_files(tmp_path, monkeypatch):
    root = tmp_path / "run"
    evidence = root / "evidence" / "preflight"
    journey.write_json(evidence / "journey-result.json", {"status": "fail"})
    monkeypatch.setattr(journey, "capture_manager_state", lambda *args: None)
    monkeypatch.setattr(journey, "capture_runtime_service_logs", lambda *args: {})
    journey.snapshot_failure_evidence(evidence, root, root / "data", "before-cleanup")
    before = root / "evidence-upload" / "before-cleanup"
    diagnostics = journey.read_json(before / "journey-result.json")["failure_diagnostics"]["before-cleanup"]
    for name in ("install_log", "service_manager", "unit_file", "runtime_service", "bootstrap", "uninstall", "seed"):
        assert diagnostics[name]["status"] == "unavailable"
    assert diagnostics["uninstall"]["final_uninstall"] == "not-yet-run"
    assert not (root / "evidence-upload" / "after-cleanup").exists()


def test_receipt_copy_failure_and_seed_truncation_are_reported(tmp_path, monkeypatch):
    root = tmp_path / "run"
    evidence = root / "evidence" / "fresh"
    journey.write_json(evidence / "journey-result.json", {"status": "fail"})
    seed = evidence / "seed-diagnostics" / "0001" / "stderr.txt"
    seed.parent.mkdir(parents=True)
    seed.write_bytes(b"x" * (journey.SEED_FILE_LIMIT + 1))
    monkeypatch.setattr(journey, "capture_manager_state", lambda *args: None)
    monkeypatch.setattr(journey, "capture_runtime_service_logs", lambda *args: {})
    original_copy = shutil.copyfile
    def copy_with_receipt_failure(source, target, *args, **kwargs):
        if Path(source).name == "journey-result.json":
            raise OSError("receipt copy failed")
        return original_copy(source, target, *args, **kwargs)
    monkeypatch.setattr(journey.shutil, "copyfile", copy_with_receipt_failure)
    journey.snapshot_failure_evidence(evidence, root, root / "data", "before-cleanup")
    diagnostic = journey.read_json(evidence / "journey-result.json")["failure_diagnostics"]["before-cleanup"]
    assert diagnostic["receipt"] == {"status": "unavailable", "path": None}
    assert diagnostic["seed"]["observation_status"] == "incomplete"
    assert not (root / "evidence-upload" / "before-cleanup" / "journey-result.json").exists()
