from __future__ import annotations

from pathlib import Path

import yaml

from astrid.skills import discovery, registry
from astrid.skills.view import compose_view


def test_generation_canonical_doc_route_survives_view_composition(tmp_path: Path) -> None:
    descriptors = discovery.list_skills(discovery.PACKS_DIR)
    core = next(descriptor for descriptor in descriptors if descriptor.pack_id == "_core")
    video = next(descriptor for descriptor in descriptors if descriptor.pack_id == "generation")

    view_root = tmp_path / "view"
    _, _, view_packs = compose_view(view_root, core, [video])
    registry.regenerate(skill_md_path=view_root / "creative-work/references/packs.md", descriptors=view_packs, view_root=view_root)

    installed_route = (view_root / "creative-work" / "SKILL.md").read_text(encoding="utf-8")
    assert "[selected pack directory](references/packs.md)" in installed_route
    assert "packs/generation/SKILL.md" in (view_root / "creative-work/references/packs.md").read_text()
    assert "../../../generation/docs/SKILL.md" not in installed_route
    installed = (view_root / "packs" / "generation" / "SKILL.md").read_text(encoding="utf-8")
    # Cross-pack doc links are rewritten from pack-relative to absolute paths in the view.
    assert str(discovery.PACKS_DIR / "vibecomfy/docs/SKILL.md") in installed
    assert "../../vibecomfy/docs/SKILL.md" not in installed


def test_creative_work_resolves_v3_action_guides_from_pack_manifest() -> None:
    route = (discovery.CORE_SKILLS_DIR / "creative-work/SKILL.md").read_text(encoding="utf-8")
    assert "component in that pack's `pack.yaml`" in route
    assert "actions/<slug>/STAGE.md" in route
    assert "astrid/packs/generation/actions/generate_image/STAGE.md" not in route
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
    route = (discovery.CORE_SKILLS_DIR / "creative-work/SKILL.md").read_text(encoding="utf-8")
    assert "astrid generate list --json" in profile
    assert "astrid generate list --json" in route
    assert "--kind" in profile and "--kind" in route
