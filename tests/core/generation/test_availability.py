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
