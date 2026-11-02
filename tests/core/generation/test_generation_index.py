from pathlib import Path
from types import SimpleNamespace

from scripts import gen_capability_index


def test_capability_index_excludes_installed_external_pack_rows(monkeypatch, tmp_path):
    rows = [
        SimpleNamespace(
            id="bundled.render",
            short_description="Bundled executor",
            description="",
            metadata={"pack_root": gen_capability_index.PACKS_ROOT / "rendering"},
        ),
        SimpleNamespace(
            id="personal.render",
            short_description="Personal executor",
            description="",
            metadata={"pack_root": tmp_path / "personal"},
        ),
    ]
    monkeypatch.setattr(
        gen_capability_index,
        "load_executors",
        lambda: SimpleNamespace(list=lambda: rows),
    )

    assert gen_capability_index._executor_rows() == [("bundled.render", "Bundled executor")]


def test_committed_capability_index_is_generated_from_registry():
    path = Path(__file__).resolve().parents[3] / "astrid/packs/_core/docs/references/capabilities.md"
    text = path.read_text(encoding="utf-8")
    assert gen_capability_index.render_block() in text
