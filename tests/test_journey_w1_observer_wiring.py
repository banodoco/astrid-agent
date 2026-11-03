"""Direct-workspace observer boundaries; no native service is started or stopped."""

import pytest

from scripts import ci_journey as journey
from scripts import journey_version_jumps as jumps


@pytest.mark.parametrize("failed_phase", [None, "before-stop", "owner-exited-before-helper-and-restart", "both"])
def test_windows_snapshots_surround_stop_and_census_without_changing_work(failed_phase, tmp_path, monkeypatch):
    root, data, evidence, realm = (tmp_path / name for name in ("root", "data", "evidence", "realm"))
    version, request = {"version": "r0"}, {"request": "seed"}
    events = []
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *a: events.append("ownership") or {"manager": "task-scheduler"})
    monkeypatch.setattr(journey, "assert_one_runtime", lambda *a: events.append("one-runtime"))
    monkeypatch.setattr(journey, "workspace_root", lambda *a: realm)
    monkeypatch.setattr(journey, "wait_ready", lambda *a: events.append("ready"))
    monkeypatch.setattr(journey.windows, "powershell", lambda script, *a, **kw: events.append("stop" if "Stop-ScheduledTask" in script else "start"))
    polls = iter([[10], []])
    monkeypatch.setattr(journey, "runtime_processes", lambda *a: events.append("census") or next(polls))
    monkeypatch.setattr(jumps.time, "sleep", lambda *a: events.append("sleep"))

    def capture(got_version, got_data, got_evidence, phase):
        assert (got_version, got_data, got_evidence) == (version, data, evidence)
        events.append(phase)
        if failed_phase in (phase, "both"):
            raise OSError("capture failed")

    def workspace(got_version, got_data, got_evidence, action, got_request, *, realm):
        assert (got_version, got_data, got_evidence, action, got_request, realm) == (version, data, evidence, "seed", request, tmp_path / "realm")
        events.append("workspace")
        return {"ok": True}

    monkeypatch.setattr(journey, "capture_windows_receipt", capture)
    monkeypatch.setattr(jumps, "workspace", workspace)
    assert jumps.direct_workspace(root, version, data, evidence, "seed", request) == {"ok": True}
    assert events == ["ownership", "one-runtime", "before-stop", "stop", "census", "sleep", "census",
                      "owner-exited-before-helper-and-restart", "workspace", "start", "ready", "ownership", "one-runtime"]


@pytest.mark.parametrize("failure", ["stop", "census", "workspace"])
def test_capture_failure_does_not_mask_existing_failure_or_restart(failure, tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *a: {"manager": "task-scheduler"})
    monkeypatch.setattr(journey, "assert_one_runtime", lambda *a: None)
    monkeypatch.setattr(journey, "workspace_root", lambda *a: tmp_path / "realm")
    monkeypatch.setattr(journey, "wait_ready", lambda *a: events.append("ready"))

    def capture(*args):
        events.append(args[-1])
        raise OSError("capture failed")

    def powershell(script, *args, **kwargs):
        action = "stop" if "Stop-ScheduledTask" in script else "start"
        events.append(action)
        if action == failure:
            raise RuntimeError("original stop failure")

    def census(*args):
        events.append("census")
        if failure == "census":
            raise RuntimeError("original census failure")
        return []

    def workspace(*args, **kwargs):
        events.append("workspace")
        raise RuntimeError("original workspace failure")

    monkeypatch.setattr(journey, "capture_windows_receipt", capture)
    monkeypatch.setattr(journey.windows, "powershell", powershell)
    monkeypatch.setattr(journey, "runtime_processes", census)
    monkeypatch.setattr(jumps, "workspace", workspace)
    with pytest.raises(RuntimeError, match=f"original {failure} failure"):
        jumps.direct_workspace(tmp_path, {}, tmp_path / "data", tmp_path / "evidence", "seed", {})
    assert events[-2:] == ["start", "ready"]
    assert ("owner-exited-before-helper-and-restart" in events) == (failure == "workspace")


def test_ownership_refusal_precedes_capture_and_stop(tmp_path, monkeypatch):
    def refuse(*args):
        raise RuntimeError("ownership refused")

    monkeypatch.setattr(journey, "assert_direct_service_ownership", refuse)
    monkeypatch.setattr(journey, "capture_windows_receipt", lambda *args: pytest.fail("capture after refusal"))
    monkeypatch.setattr(journey.windows, "powershell", lambda *args, **kwargs: pytest.fail("stop after refusal"))
    with pytest.raises(RuntimeError, match="ownership refused"):
        jumps.direct_workspace(tmp_path, {}, tmp_path / "data", tmp_path / "evidence", "seed", {})


def test_non_windows_manager_does_not_capture(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *a: {"manager": "systemd-user"})
    monkeypatch.setattr(journey, "assert_one_runtime", lambda *a: None)
    monkeypatch.setattr(journey, "workspace_root", lambda *a: tmp_path / "realm")
    monkeypatch.setattr(journey, "capture_windows_receipt", lambda *a: pytest.fail("non-Windows capture"))
    monkeypatch.setattr(journey, "run", lambda cmd, *a, **kw: events.append(cmd[2]))
    monkeypatch.setattr(journey, "runtime_processes", lambda *a: [])
    monkeypatch.setattr(journey, "wait_ready", lambda *a: None)
    monkeypatch.setattr(jumps, "workspace", lambda *a, **kw: {"ok": True})
    assert jumps.direct_workspace(tmp_path, {}, tmp_path / "data", tmp_path / "evidence", "seed", {}) == {"ok": True}
    assert events == ["stop", "start"]
