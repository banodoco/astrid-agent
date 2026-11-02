"""Generic `session: managed_tool` action field (M2-11a increment 1).

The field only declares that a python action runs in-process with the host's admitted
ManagedToolSession. It is not wan-specific, and it is valid only on python invocations.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from astrid.core.pack.canonical import CanonicalPackValidationError, validate_canonical_pack


def _action(**extra) -> dict:
    action = {
        "description": "Echo an input.",
        "invocation": {"kind": "python", "path": "actions/echo.py", "function": "echo"},
        "inputs": [{"name": "message", "type": "string", "required": True}],
        "outputs": [{"name": "result", "type": "json", "mode": "create", "artifact_type": "test/result"}],
    }
    action.update(extra)
    return action


def _pack(tmp_path: Path, action: dict) -> Path:
    root = tmp_path / "demo"
    (root / "actions").mkdir(parents=True, exist_ok=True)
    (root / "actions" / "echo.py").write_text("raise RuntimeError('must not import pack code')\n", encoding="utf-8")
    (root / "pack.yaml").write_text(yaml.safe_dump({
        "schema_version": 3, "id": "demo", "name": "Demo", "version": "1.0.0", "actions": {"echo": action},
    }), encoding="utf-8")
    return root


def test_python_action_may_declare_managed_tool_session(tmp_path: Path) -> None:
    root = _pack(tmp_path, _action(session="managed_tool"))
    normalized = validate_canonical_pack(root).definition.to_dict()["actions"]["echo"]
    assert normalized["session"] == "managed_tool"


def test_absent_session_is_unchanged(tmp_path: Path) -> None:
    root = _pack(tmp_path, _action())
    assert "session" not in validate_canonical_pack(root).definition.to_dict()["actions"]["echo"]


@pytest.mark.parametrize("value", ["other", "", "MANAGED_TOOL", None])
def test_unknown_session_values_are_rejected(tmp_path: Path, value) -> None:
    with pytest.raises(CanonicalPackValidationError):
        validate_canonical_pack(_pack(tmp_path, _action(session=value)))


def test_session_requires_a_python_invocation(tmp_path: Path) -> None:
    action = _action(session="managed_tool")
    action["invocation"] = {"kind": "command", "command": {"argv": ["{python_exec}", "-m", "tool"]}}
    with pytest.raises(CanonicalPackValidationError, match="requires a python invocation"):
        validate_canonical_pack(_pack(tmp_path, action))
