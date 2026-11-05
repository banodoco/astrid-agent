"""PK-12 (new): pack resources resolve from the pack's selected root.

A pack admitted from an extra root resolves each declared resource beneath
that root, wherever the root lives and whatever the working directory is; the
same bytes under two roots resolve to two roots with equal digests. Core pack
resources resolve beneath the selected ``astrid`` package, not the cwd.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from astrid.core.foundation.pack_resources import astrid_root, pack_root
from astrid.core.pack.canonical import read_normalize_validate

ROOT = Path(__file__).resolve().parents[2]


def _external_pack(parent: Path) -> Path:
    pack = parent / "selected_res"
    (pack / "rendering").mkdir(parents=True)
    (pack / "data").mkdir()
    (pack / "data" / "table.json").write_text('{"rows": [1, 2, 3]}\n', encoding="utf-8")
    (pack / "rendering" / "a.yaml").write_text(json.dumps({
        "schema_version": 1, "id": "selected_res.renderer_0", "name": "a", "version": "1.0.0",
        "protocol_version": 1, "command": ["python3", "backend.py"], "operations": ["render", "support"],
    }), encoding="utf-8")
    (pack / "pack.yaml").write_text(json.dumps({
        "schema_version": 3, "id": "selected_res", "name": "Selected", "version": "1.0.0",
        "pack_api": "1.0", "documentation": {"kind": "none", "reason": "Test fixture."},
        "rendering": {"renderer_0": {"type": "renderer", "path": "rendering/a.yaml"}},
        "resources": [{"path": "data/table.json", "kind": "example"}],
    }), encoding="utf-8")
    return pack


def test_declared_resources_resolve_under_each_selected_root(tmp_path: Path, monkeypatch) -> None:
    first = _external_pack(tmp_path / "root-a")
    second = tmp_path / "root-b" / "selected_res"
    shutil.copytree(first, second)
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    handles = {}
    for pack in (first, second):
        entry = read_normalize_validate(pack / "pack.yaml", source="extra")
        resolved = {handle.path: handle for handle in entry.resource_handles}
        assert "data/table.json" in resolved
        handle = resolved["data/table.json"]
        assert handle.resolved.resolve().is_relative_to(pack.resolve())
        assert not handle.resolved.resolve().is_relative_to(ROOT)
        assert not handle.resolved.resolve().is_relative_to(elsewhere)
        handles[pack] = handle
    assert handles[first].sha256 == handles[second].sha256
    assert handles[first].resolved != handles[second].resolved


def test_core_pack_resources_resolve_under_the_selected_package(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert pack_root("rendering") == astrid_root() / "packs" / "rendering"
    assert (pack_root("rendering") / "pack.yaml").is_file()
    assert not pack_root("rendering").is_relative_to(tmp_path)
