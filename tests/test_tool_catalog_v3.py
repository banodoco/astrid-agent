from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from astrid.core.pack import PackValidationError, discover_packs
from scripts import gen_tool_catalog as generator

ENTRY = "ui/video-editor/entry.json"
RESOURCE = "ui/video-editor/services.json"


def _fixture_repo(tmp_path: Path, *, host_entry: str = "video-editor") -> Path:
    repo = tmp_path / "source"
    root = repo / "astrid/packs/video_editing"
    (root / "ui/video-editor").mkdir(parents=True)
    (root / ENTRY).write_text(
        json.dumps(
            {"schema_version": 1, "tool_id": "video-editor", "host_entry": host_entry},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / RESOURCE).write_text('{"services":["timeline","shots"]}\n', encoding="utf-8")
    (root / "ui/editor-extension.tsx").write_text(
        "export default extension;\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": 3,
        "pack_api": "2.3",
        "id": "video_editing",
        "name": "Video Editing",
        "version": "1.0.0",
        "dependencies": {"npm": ["react@18.3.1"]},
        "ui": {
            "video-editor": {
                "type": "tool",
                "entry": ENTRY,
                "target": "astrid",
                "compatibility": {"host": "1"},
                "resources": [{"kind": "support", "path": RESOURCE}],
            },
            "inside-editor": {"type": "editor", "entry": "ui/editor-extension.tsx"},
        },
    }
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return repo


def _generate_from(monkeypatch: pytest.MonkeyPatch, repo: Path) -> str:
    monkeypatch.setattr(
        generator,
        "discover_packs",
        lambda: discover_packs(repo / "astrid/packs"),
    )
    return generator.generate()


def test_tool_catalog_projects_only_whole_tools_and_binds_declared_resource_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _fixture_repo(tmp_path)
    result = json.loads(_generate_from(monkeypatch, repo))

    assert result["schema_version"] == 1
    assert len(result["tools"]) == 1
    (tool,) = result["tools"]
    assert tool["id"] == "video-editor"
    assert tool["canonical_id"] == "video_editing.video-editor"
    assert tool["entry"]["path"] == ENTRY
    assert tool["entry"]["host_entry"] == "video-editor"
    assert tool["manifest"]["sha256"] == hashlib.sha256(
        (repo / "astrid/packs/video_editing/pack.yaml").read_bytes()
    ).hexdigest()
    assert tool["resources"] == [
        {
            "path": RESOURCE,
            "kind": "support",
            "sha256": hashlib.sha256(
                (repo / "astrid/packs/video_editing" / RESOURCE).read_bytes()
            ).hexdigest(),
            "size": (repo / "astrid/packs/video_editing" / RESOURCE).stat().st_size,
        }
    ]
    assert tool["dependencies"] == {
        "npm": ["react@18.3.1"],
        "python": [],
        "system": [],
    }
    assert len(tool["release_sha256"]) == 64


def test_catalog_check_rejects_stale_resource_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _fixture_repo(tmp_path)
    monkeypatch.setattr(
        generator,
        "discover_packs",
        lambda: discover_packs(repo / "astrid/packs"),
    )
    output = tmp_path / "catalog.json"
    assert generator.main([str(output)]) == 0
    (repo / "astrid/packs/video_editing" / RESOURCE).write_text(
        '{"services":["timeline"]}\n', encoding="utf-8"
    )

    assert generator.main([str(output), "--check"]) == 1
    assert "is stale" in capsys.readouterr().err


def test_committed_catalog_matches_the_packs(capsys: pytest.CaptureFixture[str]) -> None:
    # The App build refuses a catalog whose resource hashes are stale, so a pack edit must regenerate it.
    assert generator.main(["--check"]) == 0, capsys.readouterr().err


def test_catalog_rejects_entry_that_retargets_the_host_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _fixture_repo(tmp_path, host_entry="travel-between-images")
    with pytest.raises(PackValidationError, match="Tool entry must be exactly"):
        _generate_from(monkeypatch, repo)


def test_release_digest_uses_utf8_sorted_json_parity() -> None:
    descriptor = {"label": "café 🎬", "nested": {"ключ": "значение"}, "count": 1}
    expected_bytes = '{"count":1,"label":"café 🎬","nested":{"ключ":"значение"}}'.encode(
        "utf-8"
    )
    assert generator.canonical_json_bytes(descriptor) == expected_bytes
    assert hashlib.sha256(generator.canonical_json_bytes(descriptor)).hexdigest() == (
        "2e150a3104c437812d83eac4a0fb401c350eeb7a5e0df3e794906bac4d63fae1"
    )
