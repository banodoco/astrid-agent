"""Exercise diagnostic callbacks at the direct-workspace lifecycle boundaries."""
import json

import pytest

from scripts import ci_journey as journey
from scripts import ci_journey_turnover as observer
from scripts import journey_version_jumps as jumps


def harness(monkeypatch, tmp_path, failure=None, manager="task-scheduler"):
    events = []
    state = {"running": True, "checks": 0}

    def ownership(*_):
        state["checks"] += 1
        events.append("ownership")
        if failure == "initial-ownership" and state["checks"] == 1:
            raise ValueError("initial ownership")
        if failure == "final-ownership" and state["checks"] == 2:
            raise ValueError("final ownership")
        return {"manager": manager}

    def powershell(command, *_args, **_kwargs):
        operation = "stop" if "Stop-ScheduledTask" in command else "start"
        events.append(operation)
        if failure == operation:
            raise ValueError(operation)
        state["running"] = operation == "start"

    def census(_data):
        events.append("census")
        if failure == "census":
            raise ValueError("census")
        return [1] if state["running"] else []

    def workspace(*_args, **_kwargs):
        events.append("workspace")
        assert not state["running"]
        if failure == "workspace":
            raise ValueError("workspace")
        return {"result": "unchanged"}

    def ready(_data):
        events.append("ready")
        if failure == "ready":
            raise ValueError("ready")

    monkeypatch.setattr(journey, "assert_direct_service_ownership", ownership)
    monkeypatch.setattr(journey, "assert_one_runtime", lambda *_: events.append("one-runtime"))
    monkeypatch.setattr(journey, "workspace_root", lambda *_: events.append("realm") or tmp_path / "realm")
    monkeypatch.setattr(journey.windows, "powershell", powershell)
    monkeypatch.setattr(journey, "runtime_processes", census)
    monkeypatch.setattr(journey, "wait_ready", ready)
    monkeypatch.setattr(jumps, "workspace", workspace)
    monkeypatch.setattr(journey, "run", lambda argv, *_args, **_kwargs: events.append(argv[2]))
    return events, state


@pytest.mark.parametrize("action", ["seed", "snapshot"])
def test_hook_order_and_opaque_reference(monkeypatch, tmp_path, action):
    events, _ = harness(monkeypatch, tmp_path)
    token = object()
    data, evidence = tmp_path / "data", tmp_path / "evidence"
    def callback(**kwargs):
        events.append(kwargs["phase"])
        assert kwargs["action"] == action and kwargs["data"] == data and kwargs["evidence"] == evidence
        if kwargs["phase"] == "before-stop":
            assert kwargs["reference"] is None
            return token
        assert kwargs["reference"] is token
        return object()  # Later return values must not replace the original reference.
    assert jumps.direct_workspace(tmp_path, {}, data, evidence, action, {}, callback) == {"result": "unchanged"}
    assert events == ["ownership", "one-runtime", "realm", "before-stop", "stop", "census",
                      "owner-absent", "workspace", "start", "after-start-request", "ready",
                      "ownership", "one-runtime"]


@pytest.mark.parametrize("failure", [None, "initial-ownership", "stop", "census", "workspace", "start", "ready", "final-ownership"])
def test_callback_errors_preserve_lifecycle_outcomes(monkeypatch, tmp_path, failure):
    def exercise(callback):
        events, state = harness(monkeypatch, tmp_path, failure)
        try:
            result = jumps.direct_workspace(tmp_path, {}, tmp_path / "data", tmp_path / "evidence",
                                            "seed", {}, callback)
            outcome = ("return", result)
        except Exception as exc:
            outcome = (type(exc), str(exc))
        return outcome, events, state
    expected = exercise(None)
    def broken(**_kwargs):
        raise RuntimeError("diagnostic failure")
    assert exercise(broken) == expected
    if failure in ("stop", "census", "workspace"):
        assert "start" in expected[1]
    if failure == "initial-ownership":
        assert "stop" not in expected[1]


@pytest.mark.parametrize(("failure", "phases"), [
    ("initial-ownership", []),
    ("stop", ["before-stop", "after-start-request"]),
    ("census", ["before-stop", "after-start-request"]),
    ("workspace", ["before-stop", "owner-absent", "after-start-request"]),
    ("start", ["before-stop", "owner-absent"]),
])
def test_failed_lifecycle_only_observes_reached_boundaries(monkeypatch, tmp_path, failure, phases):
    harness(monkeypatch, tmp_path, failure)
    seen = []
    with pytest.raises(ValueError, match=failure.replace("-", " ")):
        jumps.direct_workspace(tmp_path, {}, tmp_path, tmp_path / "evidence", "seed", {},
                               lambda **kw: seen.append(kw["phase"]))
    assert seen == phases


def test_census_timeout_preserves_refusal_and_restart(monkeypatch, tmp_path):
    events, _ = harness(monkeypatch, tmp_path)
    clock = iter((0, 121))
    monkeypatch.setattr(jumps.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(journey, "runtime_processes", lambda *_: [1])
    seen = []
    with pytest.raises(RuntimeError, match="Installed Runtime did not stop"):
        jumps.direct_workspace(tmp_path, {}, tmp_path, tmp_path / "evidence", "snapshot", {},
                               lambda **kw: seen.append(kw["phase"]))
    assert seen == ["before-stop", "after-start-request"]
    assert "workspace" not in events and "start" in events


def test_hook_is_not_called_for_other_managers(monkeypatch, tmp_path):
    events, _ = harness(monkeypatch, tmp_path, manager="systemd-user")
    # The simulated POSIX manager updates the process state as the real command would.
    monkeypatch.setattr(journey, "runtime_processes", lambda *_: [])
    monkeypatch.setattr(jumps, "workspace", lambda *_args, **_kwargs: {})
    jumps.direct_workspace(tmp_path, {}, tmp_path, tmp_path / "evidence", "seed", {},
                           lambda **_kwargs: pytest.fail("unexpected callback"))
    assert "stop" in events and "start" in events


def test_real_observer_receives_reference_and_stages_phase_files(monkeypatch, tmp_path):
    events, _ = harness(monkeypatch, tmp_path)
    data, evidence = tmp_path / "data", tmp_path / "evidence" / "release0-api-seed"
    support = data / "runtime"
    support.mkdir(parents=True)
    (support / "generic-host.json").write_text(json.dumps({"runtime_service_launch_id": "a" * 32, "secret": "private"}))
    (support / "pack-host-job-cleanup.json").write_text(json.dumps({"receipts": [{"runtime_service_launch_id": "a" * 32}]}))
    for action in ("seed", "snapshot"):
        jumps.direct_workspace(tmp_path, {}, data, evidence, action, {}, observer.observe_turnover)
        for phase in observer.PHASES:
            path = evidence / observer._name(action, phase)
            record = json.loads(path.read_text())
            assert record["status"] == "observed"
            assert record["reference"]["launch_id"] == "a" * 32
            assert "private" not in path.read_text()
    retained = journey.retain_named_evidence(evidence.parent, "before-cleanup")
    assert all(observer._name(action, phase) in retained for action in ("seed", "snapshot") for phase in observer.PHASES)
    assert events.count("stop") == events.count("start") == 2
