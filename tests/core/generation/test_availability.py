"""The shared generation availability data marks previews as fixture-only."""

from __future__ import annotations

from pathlib import Path

import yaml

from astrid.core.generation.availability import availability_declaration


def test_preview_routes_are_fixture_only_and_pack_resources_are_labeled() -> None:
    declaration = availability_declaration()
    fixture_routes = declaration["fixture_only_routes"]
    pack = yaml.safe_load(Path("astrid/packs/astrid_motion/pack.yaml").read_text(encoding="utf-8"))
    resources = {resource["path"]: resource for resource in pack["resources"]}

    assert fixture_routes
    for route, fixture_only in fixture_routes.items():
        pack_id, path = route.split(":", 1)
        assert pack_id == "astrid_motion"
        assert fixture_only is True
        assert resources[path]["kind"] == "fixture"
    assert {f"astrid_motion:{path}" for path, item in resources.items() if item["kind"] == "fixture"} == set(fixture_routes)


def test_model_qualification_is_coded_and_qualified_fake_is_allowed(monkeypatch):
    import pytest
    from astrid.core.contracts.errors import AstridError
    from astrid.core.generation import availability

    monkeypatch.setattr(availability, 'availability_declaration', lambda: {'models': {
        'blocked': {'available': False, 'error': {'code': 'generation_route_unqualified', 'message': 'not qualified'}},
        'qualified': {'available': True},
    }})
    with pytest.raises(AstridError, match='not qualified') as exc:
        availability.require_model_available('blocked', 'i2v', 'local')
    assert exc.value.code == 'generation_route_unqualified' and exc.value.fix
    availability.require_model_available('qualified', 'i2v', 'local')


def test_template_family_uses_same_qualification_record(monkeypatch):
    from astrid.core.generation import availability

    record = {'available': False, 'error': {'code': 'generation_route_unqualified', 'message': 'unqualified'}}
    monkeypatch.setattr(availability, 'availability_declaration', lambda: {
        'models': {'fake-family': record}, 'template_models': {'video/fake': 'fake-family'},
    })
    assert availability.template_availability('video/fake_animation', 'i2v', 'local') is record
    record['available'] = True
    assert availability.template_availability('video/fake_animation', 'i2v', 'local')['available'] is True


def test_executor_documentation_uses_action_decision_and_retains_qualified(monkeypatch):
    from types import SimpleNamespace
    from astrid.core.generation import availability
    from scripts import gen_capability_index as generator

    ids = ['wan2gp.generate_video', 'video_editing.animate_image',
           'vibecomfy.character_animation', 'synthetic.qualified']
    declaration = {**availability.availability_declaration()}
    declaration['actions'] = {**declaration['actions'], 'synthetic.qualified': {'available': True}}
    monkeypatch.setattr(availability, 'availability_declaration', lambda: declaration)
    executors = [SimpleNamespace(id=name, short_description='executor description', description='',
        metadata={'pack_root': generator.PACKS_ROOT / 'fake'}) for name in ids]
    monkeypatch.setattr(generator, 'load_executors', lambda: SimpleNamespace(list=lambda: executors))
    rows = dict(generator._executor_rows())
    assert rows['synthetic.qualified'] == 'executor description'
    for name in ids[:-1]:
        projection = availability.availability_projection(availability.action_availability(name))
        assert f"Unavailable: {projection['reason']}" in rows[name]
        assert f"Fix: {projection['fix']}" in rows[name]
