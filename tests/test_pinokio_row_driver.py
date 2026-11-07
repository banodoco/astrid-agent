"""Unit tests for the pinokio-row-v1 driver: receipt shape, stage ordering and busy stop."""

import json
from pathlib import Path

import pytest

from scripts import ci_journey
from scripts import pinokio_row_driver as driver

BUSY = "Astrid is busy (2 tasks running). Try again when they finish."


class FakeRow:
    """Stands in for Row: every stage method succeeds unless told to fail."""

    def __init__(self, fail_at=None):
        self.fail_at = fail_at
        self.calls = []

    def _call(self, name):
        self.calls.append(name)
        if name == self.fail_at:
            raise driver.RowError(f"{name} went wrong")

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self._call(name)


def test_all_stages_pass_writes_six_named_steps():
    steps = driver.run_row(FakeRow())
    assert [step["name"] for step in steps] == list(driver.STEP_NAMES)
    assert all(step["status"] == "pass" for step in steps)
    assert all(set(step) == {"name", "status", "detail"} for step in steps)


def test_failing_stage_stops_later_stages_and_records_them_as_not_run():
    steps = driver.run_row(FakeRow(fail_at="start"))
    by_name = {step["name"]: step for step in steps}
    assert by_name["install"]["status"] == "pass"
    assert by_name["start_task"]["status"] == "fail"
    assert "start went wrong" in by_name["start_task"]["detail"]
    for later in ("stop_start", "busy_stop_data", "update_ready_task", "uninstall_data"):
        assert by_name[later]["status"] == "fail"
        assert by_name[later]["detail"] == "not run: start_task failed"


def test_receipt_carries_schema_job_os_sha_and_steps(tmp_path):
    sha = "a" * 40
    value = driver.receipt("pinokio-linux", "linux", sha, driver.run_row(FakeRow()))
    assert set(value) == {"schema", "job_id", "os", "pinokio_sha", "steps"}
    assert value["schema"] == "pinokio-row-v1"
    assert (value["job_id"], value["os"], value["pinokio_sha"]) == ("pinokio-linux", "linux", sha)


def test_step_detail_is_single_line_and_bounded():
    step = driver._step("install", "fail", "line one\nline two " + "x" * 2000)
    assert "\n" not in step["detail"] and len(step["detail"]) <= 500


def _busy_row(monkeypatch, stop_request):
    """A Row with the busy-stop collaborators replaced by recorders."""
    base = Path("/nonexistent-pinokio-row")
    row = driver.Row(job_id="pinokio-linux", os_name="linux", run_root=base / "run", pinokio=base / "p",
                     agent=base / "a", app_checkout=base / "app", candidate_sha="a" * 40)
    events = []
    monkeypatch.setattr(ci_journey, "submit_task", lambda *a, **k: events.append(("submit", k["duration_s"])) or "task-1")
    monkeypatch.setattr(row, "write_marker", lambda: events.append("marker"))
    monkeypatch.setattr(row, "wait_for_state", lambda task_id, wanted, timeout: events.append(("wait", task_id, sorted(wanted))) or "running")
    monkeypatch.setattr(row, "stop_request", stop_request(events))
    monkeypatch.setattr(row, "assert_same_runtime", lambda: events.append("same-runtime"))
    monkeypatch.setattr(row, "finish_stop", lambda: events.append("finish-stop"))
    monkeypatch.setattr(row, "stop", lambda: events.append("retry-stop"))
    monkeypatch.setattr(row, "start", lambda: events.append("start"))
    monkeypatch.setattr(row, "verify_marker", lambda: events.append("verify-marker"))
    return row, events


def test_busy_refusal_waits_for_terminal_then_retried_stop_succeeds(monkeypatch):
    def stop_request(events):
        def refuse():
            events.append("refused")
            raise driver.RowError(f"command failed (exit 1): node: {BUSY}")
        return refuse

    row, events = _busy_row(monkeypatch, stop_request)
    outcome = row.busy_stop()
    assert "refused once" in outcome
    assert events.index("same-runtime") > events.index("refused")
    assert ("wait", "task-1", sorted(ci_journey.TERMINAL)) in events
    assert events.index(("wait", "task-1", sorted(ci_journey.TERMINAL))) < events.index("retry-stop")
    assert events.index("retry-stop") < events.index("start") < events.index("verify-marker")
    assert events[0] == "marker" and ("submit", driver.BUSY_DURATIONS_S[0]) in events


def test_clean_stop_while_busy_is_accepted_and_task_checked_after_restart(monkeypatch):
    def stop_request(events):
        return lambda: events.append("stopped")

    row, events = _busy_row(monkeypatch, stop_request)
    outcome = row.busy_stop()
    assert outcome.startswith("busy stop completed cleanly (") and "task seen running" in outcome
    assert events.index("finish-stop") < events.index("start")
    assert events[-1] == "verify-marker"
    assert events.count(("wait", "task-1", sorted(ci_journey.TERMINAL))) == 1


def test_stop_failure_other_than_busy_refusal_fails_closed(monkeypatch):
    def stop_request(events):
        def fail():
            raise driver.RowError("command failed (exit 1): node: lock timeout")
        return fail

    row, events = _busy_row(monkeypatch, stop_request)
    with pytest.raises(driver.RowError, match="lock timeout"):
        row.busy_stop()
    assert "retry-stop" not in events and "start" not in events


def test_main_writes_receipt_and_exits_nonzero_when_a_step_fails(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({"pinokio": "b" * 40}), encoding="utf-8")
    receipt = tmp_path / "out" / "journey-result.json"
    failing = [dict(name=name, status="fail", detail="boom") if name == "install" else
               dict(name=name, status="pass", detail="ok") for name in driver.STEP_NAMES]
    monkeypatch.setattr(driver, "run_row", lambda row: failing)
    argv = ["--job-id", "pinokio-linux", "--os", "linux", "--candidate", str(candidate),
            "--pinokio-checkout", str(tmp_path / "p"), "--agent-checkout", str(tmp_path / "a"),
            "--app-checkout", str(tmp_path / "app"), "--run-root", str(tmp_path / "run"),
            "--receipt", str(receipt)]
    assert driver.main(argv) == 1
    value = json.loads(receipt.read_text(encoding="utf-8"))
    assert value["pinokio_sha"] == "b" * 40 and value["steps"] == failing


def test_main_rejects_non_sha_candidate(tmp_path):
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({"pinokio": "main"}), encoding="utf-8")
    argv = ["--job-id", "pinokio-linux", "--os", "linux", "--candidate", str(candidate),
            "--pinokio-checkout", str(tmp_path), "--agent-checkout", str(tmp_path),
            "--app-checkout", str(tmp_path), "--run-root", str(tmp_path / "run"),
            "--receipt", str(tmp_path / "r.json")]
    with pytest.raises(SystemExit, match="full commit SHA"):
        driver.main(argv)


def test_job_os_map_matches_the_three_hosted_rows():
    assert driver.JOB_OS == {"pinokio-linux": "linux", "pinokio-macos": "macos", "pinokio-windows": "windows"}
    assert driver.STEP_NAMES == ("install", "start_task", "stop_start", "busy_stop_data",
                                 "update_ready_task", "uninstall_data")
    assert driver.SCHEMA == "pinokio-row-v1"


def test_sdk_result_tolerates_only_leading_diagnostic_lines():
    import pytest
    from scripts import ci_journey
    diag = '{"diagnostic": "astrid.remotion_validation_phase", "phase": "owned_probe"}'
    assert ci_journey._sdk_result('{"ok": true}') == {"ok": True}
    assert ci_journey._sdk_result(f'{diag}\n{diag}\n{{"ok": true}}\n') == {"ok": True}
    with pytest.raises(RuntimeError):
        ci_journey._sdk_result('warning: something\n{"ok": true}')
    with pytest.raises(RuntimeError):
        ci_journey._sdk_result('{"other": 1}\n{"ok": true}')


def test_busy_stop_escalates_length_then_stops_right_after_admission(monkeypatch):
    row, events = _busy_row(monkeypatch, lambda events: lambda: events.append("stopped"))
    monkeypatch.setattr(row, "wait_for_state", lambda task_id, wanted, timeout: "succeeded")
    outcome = row.busy_stop()
    submits = [e[1] for e in events if isinstance(e, tuple) and e[0] == "submit"]
    assert submits == list(driver.BUSY_DURATIONS_S) + [driver.BUSY_DURATIONS_S[-1]]
    assert "stopped" in events and "Stop sent right after admission" in outcome
