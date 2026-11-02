"""Digest binding for `session: managed_tool` (M2-11a increment 2, digest part).

An action without `session` keeps the exact capability digest it had before the field existed,
so existing admissions and receipts stay valid. Declaring or changing `session` changes the
digest, which forces re-admission.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from astrid.core.execution import generic_host as gh
from astrid.core.execution.executor import actions as executor_actions

# Pinned on 2351d24cb0e2 (pre-field base) for this exact fixture.
UNCHANGED_DIGEST = "sha256:043fec5299690429b4760565bebcada5712a499d0b222f573fc479f2064217e3"


def _fixture(**extra) -> dict:
    action = {
        "description": "Echo an input.",
        "invocation": {"kind": "python", "path": "actions/echo.py", "function": "echo"},
        "inputs": [{"name": "message", "type": "string", "required": True}],
        "outputs": [{"name": "result", "type": "json", "mode": "create", "artifact_type": "test/result"}],
    }
    action.update(extra)
    return action


def _digest(action: dict) -> str:
    pack = SimpleNamespace(id="demo", root=Path("/opt/fixture/demo"), version="1.0.0")
    definition = executor_actions._action_definition(SimpleNamespace(pack=pack), "echo", action)
    return gh._capability_digest(definition.to_dict())


def test_action_without_session_keeps_its_pre_field_digest():
    assert _digest(_fixture()) == UNCHANGED_DIGEST


def test_declaring_session_changes_the_digest():
    assert _digest(_fixture(session="managed_tool")) != UNCHANGED_DIGEST


def test_toggling_session_changes_the_digest_both_ways():
    without = _digest(_fixture())
    with_session = _digest(_fixture(session="managed_tool"))
    assert without != with_session
    assert _digest(_fixture(session="managed_tool")) == with_session
    assert _digest(_fixture()) == without
