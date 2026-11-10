"""Setup skill packaging, mirror and generic harness naming."""
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

from astrid.core.pack import load_pack_manifest
from astrid.skills import default_pack_ids
from astrid.skills.discovery import SkillDescriptor, list_extras, list_skills
from astrid.skills.harnesses import ClaudeAdapter, CodexAdapter, HermesAdapter

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / 'astrid/packs/setup'
HEADER = '<!-- Generated from astrid/packs/setup/docs/SKILL.md; regenerate with python scripts/generate_setup_skill_mirror.py. -->\n\n'


def test_setup_pack_is_default_and_mirror_is_current():
    descriptor = next(d for d in list_skills() if d.pack_id == 'setup')
    assert descriptor.skill_md == PACK / 'docs/SKILL.md'
    assert descriptor.sync_name == 'astrid-setup'
    assert 'setup' in default_pack_ids()
    mirror = (ROOT / 'docs/setup/SKILL.md').read_text()
    source = descriptor.skill_md.read_text()
    assert mirror.startswith('---\n')
    assert mirror.replace(HEADER, '', 1) == source
    from astrid.skills.discovery import _parse_frontmatter
    assert _parse_frontmatter(mirror)[0] == _parse_frontmatter(source)[0]
    command = [sys.executable, str(ROOT / 'scripts/generate_setup_skill_mirror.py')]
    result = subprocess.run([*command, '--stdout'], capture_output=True, text=True, check=True)
    assert result.stdout == mirror
    subprocess.run([*command, '--check'], check=True)
    assert load_pack_manifest(PACK / 'pack.yaml').documentation['sync_name'] == 'astrid-setup'


def test_generic_sync_name_preserves_existing_targets(tmp_path):
    descriptor = SkillDescriptor('sample', 'sample', '', '', tmp_path, tmp_path / 'SKILL.md')
    adapters = [ClaudeAdapter(tmp_path / 'claude'), CodexAdapter(tmp_path / 'codex'), HermesAdapter(tmp_path / 'hermes')]
    mains = list_skills()
    units = [d for d in [*mains, *list_extras(mains)] if d.pack_id != "setup"]
    expected_ids = {"_core", "developer", "developer-fast-iteration",
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


def test_setup_is_selected_and_visible_in_default_sync(tmp_path):
    from unittest import mock
    from astrid import skills
    fx_home = tmp_path / 'home'
    (fx_home / '.claude').mkdir(parents=True)
    state_path = tmp_path / 'state.json'
    with mock.patch.dict('os.environ', {'HOME': str(fx_home), 'ASTRID_STATE_HOME': str(tmp_path / 'state')}), \
            mock.patch.object(Path, 'home', return_value=fx_home):
        skills.sync(harness_names=['claude'], state_path=state_path)
        adapter = ClaudeAdapter()
        view = tmp_path / 'skills/claude/packs/setup/SKILL.md'
        assert view.is_file()


def test_preinstalled_managed_setup_copy_is_adopted_by_sync(tmp_path):
    import json
    import shutil
    from unittest import mock
    from astrid import skills
    from astrid.skills.harnesses.base import _directory_digest
    from astrid.skills.discovery import list_skills
    fx_home = tmp_path / 'home'
    skills_dir = fx_home / '.claude/skills'
    skills_dir.mkdir(parents=True)
    source = next(d.skill_dir for d in list_skills() if d.pack_id == 'setup').resolve()
    target = skills_dir / 'astrid-setup'
    shutil.copytree(source, target)
    (target / '.astrid-managed-skill.json').write_text(json.dumps({
        'source': str(source), 'digest': _directory_digest(source), 'mechanism': 'copy'
    }) + '\n')
    with mock.patch.dict('os.environ', {'HOME': str(fx_home), 'ASTRID_STATE_HOME': str(tmp_path / 'state')}), \
            mock.patch.object(Path, 'home', return_value=fx_home):
        report = skills.sync(harness_names=['claude'], mechanism='copy', state_path=tmp_path / 'state.json')
        assert target.is_dir() and not target.is_symlink()
        assert (target / '.astrid-managed-skill.json').is_file()
        assert len(list(skills_dir.glob('astrid-setup'))) == 1
        assert not any('conflict' in str(row).lower() for row in report.get('conflicts', []))


def test_preinstall_helper_writes_durable_marker_and_default_sync_preserves_it(tmp_path, monkeypatch):
    import importlib.util
    import json
    from astrid import skills
    from astrid.skills.harnesses.base import is_managed_copy
    helper = ROOT / 'scripts/preinstall_setup_skill.py'
    spec = importlib.util.spec_from_file_location('preinstall_setup_skill', helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source_file = tmp_path / 'fixture.md'
    source_file.write_text('---\nname: astrid-setup\ndescription: Fixture.\n---\nFixture body.\n')
    home = tmp_path / 'home'
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setenv('ASTRID_SETUP_SKILL_SOURCE', str(source_file))
    monkeypatch.setenv('ASTRID_STATE_HOME', str(tmp_path / 'state'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(home / '.claude'))
    monkeypatch.setattr('builtins.input', lambda _prompt: 'claude')
    assert module.main() == 0
    target = home / '.claude/skills/astrid-setup'
    marker = json.loads((target / '.astrid-managed-skill.json').read_text())
    durable = tmp_path / 'data/astrid-preinstall/skills/astrid-setup'
    assert marker == {'source': str(durable.resolve()), 'digest': module.directory_digest(durable), 'mechanism': 'copy'}
    assert is_managed_copy(target)

    with monkeypatch.context() as patch:
        patch.setenv('HOME', str(home))
        patch.setattr(Path, 'home', lambda: home)
        skills.sync(harness_names=['claude'], mechanism='copy', state_path=tmp_path / 'state.json')
    assert is_managed_copy(target)
    assert json.loads((target / '.astrid-managed-skill.json').read_text()) == marker
    assert (tmp_path / 'skills/claude/packs/setup/SKILL.md').is_file()
    assert len(list((home / '.claude/skills').glob('astrid-setup'))) == 1


def test_preinstall_helper_preserves_unmanaged_harness_entry(tmp_path, monkeypatch):
    import importlib.util
    helper = ROOT / 'scripts/preinstall_setup_skill.py'
    spec = importlib.util.spec_from_file_location('preinstall_setup_skill_foreign', helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source_file = tmp_path / 'fixture.md'
    source_file.write_text('---\nname: astrid-setup\ndescription: Fixture.\n---\n')
    home = tmp_path / 'home'
    target = home / '.codex/skills/astrid-setup'
    target.mkdir(parents=True)
    (target / 'SKILL.md').write_text('user content')
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setenv('ASTRID_SETUP_SKILL_SOURCE', str(source_file))
    monkeypatch.setenv('CODEX_HOME', str(home / '.codex'))
    monkeypatch.setattr('builtins.input', lambda _prompt: 'codex')
    assert module.main() == 2
    assert (target / 'SKILL.md').read_text() == 'user content'


def test_documented_preinstall_launcher_form_runs_with_local_helper(tmp_path):
    import os
    import subprocess
    import sys
    source = tmp_path / 'fixture.md'
    source.write_text('---\nname: astrid-setup\ndescription: Fixture.\n---\n')
    home = tmp_path / 'home'
    env = {
        **os.environ,
        'HOME': str(home),
        'XDG_DATA_HOME': str(tmp_path / 'data'),
        'ASTRID_SETUP_SKILL_SOURCE': str(source),
        'CLAUDE_CONFIG_DIR': str(home / '.claude'),
    }
    code = f"import urllib.request; exec(urllib.request.urlopen({(ROOT / 'scripts/preinstall_setup_skill.py').as_uri()!r}).read())"
    result = subprocess.run([sys.executable, '-c', code], input='claude\n', text=True,
                            capture_output=True, env=env, check=True)
    assert 'This installs the skill only' in result.stdout
    assert (home / '.claude/skills/astrid-setup/.astrid-managed-skill.json').is_file()


def test_sync_rejects_colliding_names_before_mutation(tmp_path, monkeypatch):
    import pytest
    from astrid import skills
    descriptors = list_skills()
    descriptors = [replace(d, sync_name='astrid') if d.pack_id == 'setup' else d for d in descriptors]
    home = tmp_path / 'home'
    (home / '.claude').mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setattr(skills, 'list_skills', lambda: descriptors)
    for _ in range(2):
        with pytest.raises(ValueError, match='collid'):
            skills.sync(harness_names=['claude'], deep=True, state_path=tmp_path / 'state.json')
        assert not (home / '.claude/skills/astrid').exists()
        assert not (tmp_path / 'skills').exists()


def test_prune_recorded_custom_name_only_with_matching_source(tmp_path):
    from astrid.skills.harnesses.base import prune_orphan_skill_links
    skills_dir = tmp_path / 'skills'
    skills_dir.mkdir()
    source = tmp_path / 'source'
    source.mkdir()
    target = skills_dir / 'custom-name'
    target.symlink_to(source, target_is_directory=True)
    assert prune_orphan_skill_links(skills_dir, set(), known_names=set(),
                                   owned_sources={target: {source.resolve()}},
                                   declared_sources=[source.resolve()]) == [target]
    assert not target.is_symlink()


def test_prune_custom_name_preserves_retargeted_link(tmp_path):
    from astrid.skills.harnesses.base import prune_orphan_skill_links
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    target = skills_dir / "custom-name"
    target.symlink_to(foreign, target_is_directory=True)
    conflicts = []
    assert prune_orphan_skill_links(skills_dir, set(), owned_sources={target: {source.resolve()}},
                                   declared_sources=[foreign.resolve()], conflicts=conflicts) == []
    assert target.resolve() == foreign.resolve()
    assert conflicts and conflicts[0].extras.get("conflict")
