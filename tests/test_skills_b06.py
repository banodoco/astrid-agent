from __future__ import annotations

from pathlib import Path

from astrid.skills import discovery
from astrid.skills.view import compose_view


def test_video_editing_canonical_doc_route_survives_view_composition(tmp_path: Path) -> None:
    descriptors = discovery.list_skills(discovery.PACKS_DIR)
    core = next(descriptor for descriptor in descriptors if descriptor.pack_id == "_core")
    video = next(descriptor for descriptor in descriptors if descriptor.pack_id == "video_editing")

    view_root = tmp_path / "view"
    compose_view(view_root, core, [video])

    installed_route = (view_root / "creative-work" / "SKILL.md").read_text(encoding="utf-8")
    assert "[video editing](../packs/video_editing/SKILL.md)" in installed_route
    assert "../../../video_editing/docs/SKILL.md" not in installed_route
    installed = (view_root / "packs" / "video_editing" / "SKILL.md").read_text(encoding="utf-8")
    assert str(discovery.PACKS_DIR / "video_editing/actions/event_talks/run.py") in installed
    assert str(discovery.PACKS_DIR / "rendering/docs/SKILL.md") in installed
