"""Synthetic checks for the journey's bounded, status-only SDK observations."""
import json
import subprocess
import sys

import pytest

from scripts import ci_journey as journey


STATUS = "sdk-capture-status.json"


def receipt(evidence, name=STATUS):
    return journey.SDK_CAPTURE_RECEIPTS[evidence.absolute()][name]


def call_capture(monkeypatch, evidence, code, timeout=5):
    def cli(_version, _argv, evidence, name, **_kwargs):
        return journey.run([sys.executable, "-c", code], evidence / name, timeout=timeout)
    monkeypatch.setattr(journey, "installed_cli", cli)
    return journey.installed_cli_with_capture({}, [], evidence, "sdk-invocation.json", status_name=STATUS)


def test_success_classifies_without_publishing_response(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence" / "fresh"
    secret = "private-response-value"
    payload = {"ok": True, "details": {"astridTaskRefs": [{"task_id": secret}]}}
    output = call_capture(monkeypatch, evidence, f"print({json.dumps(json.dumps(payload))})")
    assert json.loads(output) == payload
    status = receipt(evidence)
    assert status == {"schema": "astrid.sdk-capture-status.v1", "exit_condition": "success",
                      "response_size": "within_limit", "json_object": True,
                      "ok": True, "task_refs": "one"}
    journey.write_json(evidence / "journey-result.json", {"status": "pass", "job_id": "example"})
    journey.retain_named_evidence(evidence, "after-cleanup")
    staged = tmp_path / "evidence-upload" / "after-cleanup"
    assert secret not in (staged / STATUS).read_text()
    assert not (staged / "sdk-invocation.json").exists()
    assert journey.read_json(staged / "journey-result.json") == {"status": "pass", "job_id": "example"}


def test_over_limit_does_not_change_success_or_parse_prefix(monkeypatch, tmp_path):
    output = call_capture(monkeypatch, tmp_path, f"print('x' * {journey.SDK_CAPTURE_LIMIT + 10})")
    assert len(output) > journey.SDK_CAPTURE_LIMIT
    status = receipt(tmp_path)
    assert status["response_size"] == "over_limit"
    assert status["exit_condition"] == "success"
    assert status["json_object"] is None
    assert status["task_refs"] == "unknown"


@pytest.mark.parametrize("code,timeout,exception,condition", [
    ("import sys; print('private'); sys.exit(4)", 5, RuntimeError, "failure"),
    ("import time; print('private', flush=True); time.sleep(5)", 0.1, subprocess.TimeoutExpired, "timeout"),
])
def test_failures_keep_original_exception_and_hide_partial_output(monkeypatch, tmp_path, code, timeout, exception, condition):
    with pytest.raises(exception):
        call_capture(monkeypatch, tmp_path, code, timeout)
    status = receipt(tmp_path)
    assert status["exit_condition"] == condition
    assert status["response_size"] == "unavailable"
    assert "private" not in json.dumps(status)


def test_diagnostic_write_failure_does_not_replace_cli_outcome(monkeypatch, tmp_path):
    class FailingReceipts(dict):
        def setdefault(self, *_args):
            raise OSError("diagnostic unavailable")
    monkeypatch.setattr(journey, "SDK_CAPTURE_RECEIPTS", FailingReceipts())
    assert call_capture(monkeypatch, tmp_path, "print('ok')") == "ok\n"
    with pytest.raises(subprocess.TimeoutExpired):
        call_capture(monkeypatch, tmp_path, "import time; time.sleep(5)", 0.1)


def test_staging_rejects_injected_and_stale_receipts(tmp_path):
    evidence = tmp_path / "evidence" / "fresh"
    evidence.mkdir(parents=True)
    staged = tmp_path / "evidence-upload" / "before-cleanup"
    staged.mkdir(parents=True)
    (staged / STATUS).write_text("stale")
    journey.retain_named_evidence(evidence, "before-cleanup")
    assert not (staged / STATUS).exists()
    journey.write_json(evidence / STATUS, {"schema": "astrid.sdk-capture-status.v1", "private": "secret"})
    journey.retain_named_evidence(evidence, "before-cleanup")
    assert not (staged / STATUS).exists()


def test_task_clears_previous_capture_before_failed_start(monkeypatch, tmp_path):
    evidence = tmp_path / "evidence" / "fresh"
    evidence.mkdir(parents=True)
    for phase in ("before-cleanup", "after-cleanup"):
        staged = tmp_path / "evidence-upload" / phase
        staged.mkdir(parents=True)
        for name in journey.SDK_CAPTURE_STATUS_NAMES:
            (staged / name).write_text("stale")
    for name in journey.SDK_CAPTURE_STATUS_NAMES:
        (evidence / name).write_text("stale")
    def failed_start(*_args, **_kwargs):
        raise FileNotFoundError("synthetic start failure")
    monkeypatch.setattr(journey, "installed_cli", failed_start)
    with pytest.raises(FileNotFoundError, match="synthetic start failure"):
        journey.task({}, evidence, "project")
    assert receipt(evidence, "project-create-capture-status.json")["exit_condition"] == "failure"
    assert STATUS not in journey.SDK_CAPTURE_RECEIPTS[evidence.absolute()]
    for phase in ("before-cleanup", "after-cleanup"):
        for name in journey.SDK_CAPTURE_STATUS_NAMES:
            assert not (tmp_path / "evidence-upload" / phase / name).exists()
