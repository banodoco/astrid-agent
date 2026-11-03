"""D09 rows 11/12: static semantic roles come from element declarations."""

from __future__ import annotations

from astrid.core.timeline import scene_roles


def test_builtin_picture_roles_match_the_real_element_manifests():
    """The helper's normal loader reads the three active element.yaml files."""
    declarations = scene_roles.element_coverage(
        ["am-snap-plate", "am-footage", "am-presenter"]
    )

    assert declarations == {
        "am-snap-plate": {
            "coverage": "picture",
            "background_unless": ["asset", "params.src"],
        },
        "am-footage": {"coverage": "picture"},
        "am-presenter": {"coverage": "picture"},
    }
    assert scene_roles.role_of(
        {"clipType": "am-snap-plate", "asset": "plate.png"}, "plate", declarations
    ) == "picture"
    assert scene_roles.role_of({"clipType": "am-footage"}, "fx", declarations) == "picture"
    assert scene_roles.role_of({"clipType": "am-presenter"}, "sprite", declarations) == "picture"


def test_bare_and_missing_manifests_are_empty_and_use_track_fallback(tmp_path, monkeypatch):
    """A manifest without metadata and an absent manifest both stay unclaimed."""
    pack = tmp_path / "packs" / "fixture" / "elements" / "effects"
    bare = pack / "fixture-bare"
    bare.mkdir(parents=True)
    (bare / "element.yaml").write_text(
        "id: fixture-bare\nkind: effect\n", encoding="utf-8"
    )

    monkeypatch.setattr(scene_roles, "_PACKS_ROOT", tmp_path / "packs")
    monkeypatch.setattr(scene_roles, "_COVERAGE_CACHE", {})
    declarations = scene_roles.element_coverage(["fixture-bare", "fixture-missing"])

    assert declarations == {"fixture-bare": {}, "fixture-missing": {}}
    assert scene_roles.role_of({"clipType": "fixture-bare"}, "plate", declarations) == "picture"
    assert scene_roles.role_of({"clipType": "fixture-missing"}, "fx", declarations) == "content"
