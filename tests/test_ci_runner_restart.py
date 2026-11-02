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
    ticks = iter([0, 1, 2, 3, 20, 21])
    monkeypatch.setattr(helper.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(helper.time, "sleep", lambda _: None)
    monkeypatch.setattr(helper.subprocess, "run", lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0))
    result = helper.restart(Path("/unused"))
    assert calls[:2] == ["identity", "identity"]
    assert calls[2] == ["/bin/launchctl", "kickstart", "-k", "gui/503/pinned"]
    assert result["status"] == "restart-requested"


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
