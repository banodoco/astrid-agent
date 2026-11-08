from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from astrid.core.pack import (
    PackValidationError,
    load_pack_manifest,
    pack_editor_entry_paths,
)
from scripts.gen_editor_extension_catalog import DEFAULT_OUTPUT, _editor_entries, generate

PACK_ROOT = Path(__file__).resolve().parents[1] / "astrid" / "packs" / "rendering"


def test_rendering_pack_declares_both_resolvable_v3_editor_entries() -> None:
    pack = load_pack_manifest(PACK_ROOT / "pack.yaml")

    assert pack.schema_version == "3"
    # video-editor is the core Tool surface the App host requires (M-64); it is a
    # whole Tool declaration, not an editor entry, so the editor set is unchanged.
    assert set(pack.ui) == {"threejs-live-editor", "transcript", "video-editor"}
    entries = _editor_entries(pack)
    assert set(entries) == {
        PACK_ROOT / "ui/threejs-live-editor/extension.tsx",
        PACK_ROOT / "ui/transcript/extension.tsx",
    }
    assert all(entry.is_file() for entry in entries)
    assert pack.ui["transcript"]["target"] == "video-editor"
    assert pack.ui["transcript"]["compatibility"] == {"sdk": "1"}


def test_generated_catalog_is_current_and_pack_derived() -> None:
    generated = generate(DEFAULT_OUTPUT)
    actual = DEFAULT_OUTPUT.read_text(encoding="utf-8")

    assert actual == generated
    assert "../ui/threejs-live-editor/extension" in actual
    assert "../ui/transcript/extension" in actual
    assert "packId: 'rendering', entryPath: 'ui/transcript/extension.tsx'" in actual
    assert actual.count("import editorExtension") == 2


def test_editor_entry_discovery_rejects_paths_outside_pack() -> None:
    pack = load_pack_manifest(PACK_ROOT / "pack.yaml")
    escaped = replace(
        pack,
        extensions={"editor": {"entries": ["../outside/extension.tsx"]}},
    )

    with pytest.raises(PackValidationError, match="must stay within the pack root"):
        pack_editor_entry_paths(escaped)
