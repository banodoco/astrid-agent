from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from astrid.core.pack import discover_packs
from scripts import gen_editor_extension_catalog as generator


def _fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    root = repo / "astrid/packs/rendering"
    files = {
        "ui/threejs-live-editor/extension.tsx": "export default { activate() {} };\n",
        "ui/video-editor/entry.json": '{"schema_version":1,"tool_id":"video-editor","host_entry":"video-editor"}\n',
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / "pack.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 3,
                "pack_api": "2.3",
                "id": "rendering",
                "name": "Rendering",
                "version": "1.0.0",
                "ui": {
                    "threejs-live-editor": {
                        "type": "editor",
                        "entry": "ui/threejs-live-editor/extension.tsx",
                    },
                    "video-editor": {
                        "type": "tool",
                        "entry": "ui/video-editor/entry.json",
                        "target": "astrid",
                        "compatibility": {"host": "1"},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return repo


def test_v3_editor_projection_excludes_whole_tool_declarations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _fixture_repo(tmp_path)
    monkeypatch.setattr(
        generator,
        "discover_packs",
        lambda: discover_packs(repo / "astrid/packs"),
    )

    generated = generator.generate(repo / "astrid/packs/rendering/editor/catalog.ts")

    assert generated.count("import editorExtension") == 1
    assert "ui/threejs-live-editor/extension" in generated
    assert "video-editor" not in generated
    assert "ui/video-editor/entry.json" not in generated
