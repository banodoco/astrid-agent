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


def test_rendering_pack_declares_one_resolvable_editor_entry() -> None:
    pack = load_pack_manifest(PACK_ROOT / "pack.yaml")

    assert pack.ui["live-scenes"]["entry"] == "ui/live-scenes/extension.tsx"
    entries = _editor_entries(pack)
    assert entries == (PACK_ROOT / "ui/live-scenes/extension.tsx",)
    assert entries[0].is_file()


def test_generated_catalog_is_current_and_pack_derived() -> None:
    generated = generate(DEFAULT_OUTPUT)
    actual = DEFAULT_OUTPUT.read_text(encoding="utf-8")

    assert actual == generated
    assert "../ui/live-scenes/extension" in actual
    assert "ui/live-scenes/extension.tsx" in actual


def test_editor_entry_discovery_rejects_paths_outside_pack() -> None:
    pack = load_pack_manifest(PACK_ROOT / "pack.yaml")
    escaped = replace(
        pack,
        extensions={"editor": {"entries": ["../outside/extension.tsx"]}},
    )

    with pytest.raises(PackValidationError, match="must stay within the pack root"):
        pack_editor_entry_paths(escaped)
