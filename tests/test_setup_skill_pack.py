"""Setup skill packaging, mirror and generic harness naming."""
from dataclasses import replace
from pathlib import Path

from astrid.core.pack import load_pack_manifest
from astrid.skills import default_pack_ids
from astrid.skills.discovery import SkillDescriptor, list_extras, list_skills
from astrid.skills.harnesses import ClaudeAdapter, CodexAdapter, HermesAdapter

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / 'astrid/packs/setup'
HEADER = '<!-- Generated from astrid/packs/setup/docs/SKILL.md; do not edit. -->\n\n'


def test_setup_pack_is_default_and_mirror_is_current():
    descriptor = next(d for d in list_skills() if d.pack_id == 'setup')
    assert descriptor.skill_md == PACK / 'docs/SKILL.md'
    assert descriptor.sync_name == 'astrid-setup'
    assert 'setup' in default_pack_ids()
    assert (ROOT / 'docs/setup/SKILL.md').read_text() == HEADER + descriptor.skill_md.read_text()
    assert load_pack_manifest(PACK / 'pack.yaml').documentation['sync_name'] == 'astrid-setup'


def test_generic_sync_name_preserves_existing_targets(tmp_path):
    descriptor = SkillDescriptor('sample', 'sample', '', '', tmp_path, tmp_path / 'SKILL.md')
    adapters = [ClaudeAdapter(tmp_path / 'claude'), CodexAdapter(tmp_path / 'codex'), HermesAdapter(tmp_path / 'hermes')]
    mains = list_skills()
    units = [d for d in [*mains, *list_extras(mains)] if d.pack_id != "setup"]
    expected_ids = {"_core", "comfy_wrap", "developer", "developer-fast-iteration",
                    "developer-releasing", "generation", "h3_av", "local", "rendering",
                    "runpod", "timeline", "timeline-narrated", "timeline-review",
                    "timeline-series", "timeline-shots", "training", "vibecomfy", "wan2gp"}
    assert {d.skill_id for d in units} == expected_ids
    for adapter in adapters:
        for unit in units:
            expected = "astrid" if unit.pack_id == "_core" else f"astrid-{unit.skill_id}"
            assert adapter.target_for(unit) == adapter.skills_dir / expected
    for adapter in adapters:
        assert adapter.target_for(replace(descriptor, sync_name='custom-name')).name == 'custom-name'
        assert adapter.target_for(replace(descriptor, pack_id='_core', sync_name='custom-name')).name == 'astrid'


def test_declared_name_is_generic_and_rejects_unsafe_paths(tmp_path):
    import pytest
    from astrid.core.pack import PackValidationError
    pack = tmp_path / "demo"
    (pack / "docs").mkdir(parents=True)
    (pack / "docs/SKILL.md").write_text("---\nname: demo\ndescription: Demo.\n---\nBody.\n")
    manifest = pack / "pack.yaml"
    template = "schema_version: 3\nid: demo\nname: Demo\nversion: 1.0.0\ndocumentation:\n  kind: skill\n  path: docs/SKILL.md\n  sync_name: {}\n"
    manifest.write_text(template.format("different-name"))
    assert list_skills(tmp_path)[0].sync_name == "different-name"
    manifest.write_text(template.format("../escape"))
    with pytest.raises(PackValidationError):
        load_pack_manifest(manifest)


def test_setup_is_materialised_in_view(tmp_path):
    from astrid.skills.view import compose_view
    descriptors = list_skills()
    core = next(d for d in descriptors if d.pack_id == "_core")
    setup = next(d for d in descriptors if d.pack_id == "setup")
    _, _, packs = compose_view(tmp_path / "view", core, [setup])
    assert packs[0].skill_md.is_file()
    assert "# Set up Astrid" in packs[0].skill_md.read_text()
    assert packs[0].sync_name == "astrid-setup"


def test_custom_name_is_not_pruned_as_an_orphan(tmp_path):
    from astrid.skills.harnesses.base import prune_orphan_skill_links
    source = tmp_path / "source"
    source.mkdir()
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    target = skills_dir / "astrid-custom"
    target.symlink_to(source, target_is_directory=True)
    assert prune_orphan_skill_links(skills_dir, {"demo"}, known_names={target.name},
                                    declared_sources=[source]) == []
    assert target.is_symlink()
