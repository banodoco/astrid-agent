"""LS-8: admitted resident pack actions retain one owned, fenced worker."""
from __future__ import annotations

import json

import pytest

from astrid.core.pack.canonical import CanonicalPackValidationError, validate_canonical_pack


SESSION = {"kind": "resident", "protocol": "astrid.pack-session.v1"}


def write_pack(tmp_path, *, session=SESSION, code=None):
    root = tmp_path / "packs" / "resident_probe"
    (root / "actions").mkdir(parents=True, exist_ok=True)
    (root / "actions" / "run.py").write_text(
        code or "def run(value='default'):\n    return value\n", encoding="utf-8"
    )
    action = {
        "description": "An admitted resident test action.",
        "invocation": {"kind": "python", "path": "actions/run.py", "function": "run"},
        "inputs": [{"name": "value", "type": "string", "default": "default"}],
        "outputs": {},
        "metadata": {"adapter_family": "cpu", "resource_keys": ["cpu"], "project_scope": "optional"},
    }
    if session is not None:
        action["session"] = session
    (root / "pack.yaml").write_text(json.dumps({
        "schema_version": 3, "id": "resident_probe", "name": "Resident probe",
        "version": "1.0.0", "pack_api": "2.0", "actions": {"run": action},
    }), encoding="utf-8")
    return root


def test_manifest_declares_resident_protocol_without_importing_pack(tmp_path):
    marker = tmp_path / "imported"
    root = write_pack(tmp_path, code=f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef run(value):\n    return value\n")
    entry = validate_canonical_pack(root)
    assert dict(entry.definition.actions["run"]["session"]) == SESSION
    assert not marker.exists()


@pytest.mark.parametrize("session", [
    {"kind": "resident", "protocol": "unknown"},
    {"kind": "resident", "protocol": "astrid.pack-session.v1", "concurrent": True},
    {"kind": "resident"},
    "resident",
])
def test_manifest_refuses_unknown_resident_contract(tmp_path, session):
    with pytest.raises(CanonicalPackValidationError):
        validate_canonical_pack(write_pack(tmp_path, session=session))


def test_ordinary_manifest_has_no_resident_declaration(tmp_path):
    entry = validate_canonical_pack(write_pack(tmp_path, session=None))
    assert "session" not in entry.definition.actions["run"]
