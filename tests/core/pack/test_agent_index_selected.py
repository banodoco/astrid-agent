"""Agent index follows the same admitted selected-source inventory as loading."""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from astrid.pack_api import VERSION
from astrid.core.pack.agent_index import build_agent_index


def _pack(collection: Path, name: str, *, api: str | None = None) -> Path:
    root = collection / name
    root.mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "docs/SKILL.md").write_text(f"---\nname: {name}\ndescription: Selected fixture.\n---\n# Fixture\n")
    (root / "pack.yaml").write_text(yaml.safe_dump({
        "schema_version": 3, "id": name, "name": name,
        "version": "1.0.0", "pack_api": api or f"{VERSION[0]}.{VERSION[1]}",
        "documentation": {"kind": "skill", "path": "docs/SKILL.md"},
        "agent": {"purpose": "Selected fixture.", "normal_entrypoints": []},
    }))
    return root


def test_index_uses_selected_admitted_roots_and_never_fetches(tmp_path, monkeypatch):
    selected = tmp_path / "selected"
    inactive = tmp_path / "inactive"
    root = _pack(selected, "indexdemo")
    _pack(selected, "invaliddemo", api=f"{VERSION[0] + 1}.0")
    _pack(inactive, "inactivedemo")
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(selected))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ASTRID_SOURCE_STATE", str(tmp_path / "absent-sources.json"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected fetch")))
    result = build_agent_index(pack_id="indexdemo")
    assert result is not None
    assert result["source_type"] == "env"
    assert result["trust_tier"] == "unknown"
    assert result["docs_paths"]["documentation"] == str(root / "docs/SKILL.md")
    assert build_agent_index(pack_id="invaliddemo") is None
    assert build_agent_index(pack_id="inactivedemo") is None
