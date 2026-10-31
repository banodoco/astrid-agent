from pathlib import Path

from astrid.core.generation import listing
from scripts import gen_capability_index


def test_generation_section_uses_shared_catalog(monkeypatch):
    monkeypatch.setattr(listing, "catalog_rows", lambda: [{
        "id": "example-model", "kind": "image", "mode": "t2i",
        "backend": "local", "availability": "fixture-only", "reason": "test | reason",
    }])
    section = "\n".join(gen_capability_index._generation_section())
    assert "| example-model | image | t2i | local | fixture-only | test \\| reason |" in section
    assert "astrid generate list" in section


def test_committed_capability_index_is_generated_from_registry():
    path = Path(__file__).resolve().parents[3] / "astrid/packs/_core/docs/references/capabilities.md"
    text = path.read_text(encoding="utf-8")
    assert gen_capability_index.render_block() in text
