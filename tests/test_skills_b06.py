from __future__ import annotations

from pathlib import Path

import yaml

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


def test_creative_work_resolves_v3_action_guides_from_pack_manifest() -> None:
    route = (discovery.PACKS_DIR / "_core/docs/creative-work/SKILL.md").read_text(encoding="utf-8")
    assert "component in that pack's `pack.yaml`" in route
    assert "actions/<slug>/STAGE.md" in route
    assert "astrid/packs/generation/actions/generate_image/STAGE.md" in route
    assert "before recommending a" in route and "reinstall" in route

    generation_root = discovery.PACKS_DIR / "generation"
    manifest = yaml.safe_load((generation_root / "pack.yaml").read_text(encoding="utf-8"))
    resources = manifest["actions"]["generate_image"]["resources"]
    stage_path = next(
        resource["path"] for resource in resources
        if resource["kind"] == "documentation" and resource["path"].endswith("STAGE.md")
    )
    assert stage_path == "actions/generate_image/STAGE.md"
    assert (generation_root / stage_path).is_file()


def test_generation_readiness_is_in_agent_and_creative_routes() -> None:
    root = Path(__file__).resolve().parents[1]
    profile = (root / "astrid/agent_profiles/astrid.md").read_text(encoding="utf-8")
    route = (discovery.PACKS_DIR / "_core/docs/creative-work/SKILL.md").read_text(encoding="utf-8")
    assert "astrid generate list --json" in profile
    assert "astrid generate list --json" in route
    assert "--kind" in profile and "--kind" in route
