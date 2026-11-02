import base64
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import ci_runner_restart as helper


def test_workflow_embeds_exact_helper_and_is_manual_only():
    root = Path(__file__).parents[1]
    workflow = (root / ".github/workflows/journey.yml").read_text()
    match = re.search(r'base64\.b64decode\("([A-Za-z0-9+/=]+)", validate=True\)', workflow)
    assert match and base64.b64decode(match.group(1)) == (root / "scripts/ci_runner_restart.py").read_bytes()
    assert "workflow_dispatch:" in workflow and "sleep 120" in workflow
    assert "actions/checkout" not in workflow


def test_restart_waits_for_existing_worker_then_checks_identity_again(monkeypatch):
    calls = []
    monkeypatch.setattr(helper, "identity", lambda home: calls.append("identity") or "gui/503/pinned")
    states = iter([True, True, False, False, False])
    monkeypatch.setattr(helper, "workers_exist", lambda: next(states))
    ticks = iter([0, 1, 2, 3, 20, 21, 22, 23, 24])
    monkeypatch.setattr(helper.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(helper.time, "sleep", lambda _: None)
    listeners = iter([100, 101])
    monkeypatch.setattr(helper, "listener_pid", lambda home: next(listeners))
    monkeypatch.setattr(helper.subprocess, "run", lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0))
    result = helper.restart(Path("/unused"))
    assert calls[:2] == ["identity", "identity"]
    assert calls[2] == ["/bin/launchctl", "kickstart", "-k", "gui/503/pinned"]
    assert calls[3] == "identity"
    assert result["status"] == "restart-verified"
    assert (result["old_listener_pid"], result["new_listener_pid"]) == (100, 101)


def test_restart_refuses_when_scheduling_worker_not_observed(monkeypatch):
    monkeypatch.setattr(helper, "identity", lambda home: "gui/503/pinned")
    monkeypatch.setattr(helper, "workers_exist", lambda: False)
    monkeypatch.setattr(helper.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not restart"))
    with pytest.raises(RuntimeError, match="not observed"):
        helper.restart(Path("/unused"))


def test_restart_refuses_if_worker_never_finishes(monkeypatch):
    monkeypatch.setattr(helper, "identity", lambda home: "gui/503/pinned")
    monkeypatch.setattr(helper, "workers_exist", lambda: True)
    ticks = iter([0, 1, 20])
    monkeypatch.setattr(helper.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(helper.time, "sleep", lambda _: None)
    monkeypatch.setattr(helper.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not restart"))
    with pytest.raises(RuntimeError, match="Timed out"):
        helper.restart(Path("/unused"), timeout=10)


def test_restart_failure_attempts_pinned_service_recovery(monkeypatch):
    monkeypatch.setattr(helper, "identity", lambda home: "gui/503/pinned")
    states = iter([True, False, False, False])
    monkeypatch.setattr(helper, "workers_exist", lambda: next(states))
    ticks = iter([0, 1, 2, 3, 20, 21])
    monkeypatch.setattr(helper.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(helper.time, "sleep", lambda _: None)
    monkeypatch.setattr(helper, "listener_pid", lambda home: 100)
    recovered = []
    monkeypatch.setattr(helper, "recover", lambda home: recovered.append(home))
    monkeypatch.setattr(helper.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("launchctl failed")))
    with pytest.raises(OSError, match="launchctl failed"):
        helper.restart(Path("/unused"))
    assert recovered == [Path("/unused")]


def test_helper_cleanup_refuses_mismatched_or_active_label(monkeypatch):
    run = Path("/owned/123-1")
    calls = []
    def fake(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="/other/helper.py\npid = 123\n", stderr="")
    monkeypatch.setattr(helper.subprocess, "run", fake)
    with pytest.raises(RuntimeError, match="mismatched or still running"):
        helper.remove_own_helper(run)
    assert len(calls) == 1


def test_helper_cleanup_removes_only_matching_finished_label(monkeypatch):
    run = Path("/owned/123-1")
    calls = []
    def fake(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="/usr/bin/python3\n" + str(run / "helper.py") + "\nstate = exited\n", stderr="")
    monkeypatch.setattr(helper.subprocess, "run", fake)
    assert helper.remove_own_helper(run) == "removed"
    assert calls[-1] == ["/bin/launchctl", "remove", "haus.astrid.runner-restart.123-1"]


@pytest.mark.parametrize("code,text,expected", [
    (113, 'Could not find service "runner.label" in domain for user gui: 503', True),
    (113, 'Bad request.\nCould not find service "runner.label" in domain for user gui: 503', True),
    (1, 'Could not find service "runner.label" in domain for user gui: 503', False),
    (113, 'Could not find service "other" in domain for user gui: 503', False),
    (113, 'error: Could not find service "runner.label" in domain for user gui: 503', False),
])
def test_missing_service_requires_exact_exit_and_label(code, text, expected):
    assert helper.missing_service(SimpleNamespace(returncode=code, stderr=text), "runner.label") == expected


def test_service_identity_rejects_changed_plist_before_launchctl(tmp_path, monkeypatch):
    import hashlib
    import os
    import plistlib
    monkeypatch.setattr(helper, "UID", os.getuid())
    home = tmp_path / "home"
    runner = home / "actions-runner"
    launch = home / "Library/LaunchAgents"
    runner.mkdir(parents=True)
    launch.mkdir(parents=True)
    label = "runner.example"
    plist = launch / (label + ".plist")
    data = plistlib.dumps({"Label": label, "ProgramArguments": [str(runner / "runsvc.sh")]})
    plist.write_bytes(data)
    (runner / ".service").write_text(str(plist))
    monkeypatch.setattr(helper, "PLIST_SHA", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(helper, "LABEL_SHA", hashlib.sha256(label.encode()).hexdigest())
    calls = []
    monkeypatch.setattr(helper.subprocess, "run", lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0, stdout=str(runner / "runsvc.sh")))
    assert helper.identity(home) == "gui/503/runner.example"
    plist.write_bytes(data + b" ")
    with pytest.raises(RuntimeError, match="plist changed"):
        helper.identity(home)
    assert len(calls) == 1
