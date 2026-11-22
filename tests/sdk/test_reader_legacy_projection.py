"""Reader compatibility for historical SDK authoring shapes."""

import copy

import pytest

from astrid.core.receipts.canonical import canonical_bytes
from astrid.core.timeline.reader_projection import (
    READER_PROJECTION_APP_KEY,
    ReaderProjectionError,
    check_reader_projection,
    reader_view,
)
from astrid.packs.rendering.actions.render.managed_timeline import _render_compatible_projection


@pytest.mark.parametrize("project", [reader_view, _render_compatible_projection])
def test_reader_projects_only_its_detached_copy(project):
    ratio = {"numerator": 30000, "denominator": 1001}
    extension = {"values": [None, False, 0, 0.0, {}, []]}
    config = {
        "output": {"resolution": "640x360", "file": "film.mp4", "fps": ratio},
        "theme_overrides": {"visual": {"canvas": {"fps": ratio}}, "opaque_theme": extension},
        "tracks": [{"id": "title", "kind": "visual", "label": "Title", "opaque": extension}],
        "clips": [{
            "id": "title", "track": "title", "clipType": "text", "at": 0, "hold": 1,
            "text": {"content": "Title"}, "app": {"opaque": "already namespaced"},
            "opaque": extension, "org.example/data": extension, "nullable": None, "sourceURL": None,
        }],
        "x_f2_opaque": extension,
    }
    registry = {"assets": {}}
    authored = canonical_bytes(config)

    projected, assets = project(config, registry)
    check_reader_projection(projected, assets)
    assert projected["output"]["fps"] == projected["theme_overrides"]["visual"]["canvas"]["fps"] == 30000 / 1001
    assert canonical_bytes(config) == authored
    assert registry == {"assets": {}}

    clip = projected["clips"][0]
    assert clip["app"]["opaque"] == "already namespaced"
    clip_extensions = clip["app"][READER_PROJECTION_APP_KEY]["extensions"]["clip"]
    assert canonical_bytes(clip_extensions) == canonical_bytes(
        {"opaque": extension, "org.example/data": extension, "nullable": None, "sourceURL": None}
    )
    assert not clip_extensions.keys() & clip.keys()
    assert projected["tracks"][0]["app"][READER_PROJECTION_APP_KEY]["extensions"] == {
        "track": {"opaque": extension},
    }
    assert projected["app"][READER_PROJECTION_APP_KEY]["extensions"] == {
        "config": {"x_f2_opaque": extension},
        "theme_overrides": {"opaque_theme": extension},
    }
    clip_extensions["opaque"]["values"].append("reader mutation")
    assert canonical_bytes(config) == authored


@pytest.mark.parametrize("fps", [
    {"numerator": 24},
    {"numerator": 24, "denominator": True},
    {"numerator": 24, "denominator": 0},
    {"numerator": 24, "denominator": 1, "unknown": 1},
    {"numerator": 10**400, "denominator": 1},
    {"numerator": 1, "denominator": 10**400},
])
def test_reader_refuses_invalid_canvas_ratio(fps):
    config = {"theme_overrides": {"visual": {"canvas": {"fps": fps}}}}
    with pytest.raises(ReaderProjectionError, match=r"theme_overrides\.visual\.canvas\.fps"):
        reader_view(config, {"assets": {}})


@pytest.mark.parametrize("annotation", [None, {"extensions": {"track": {"opaque": "keep"}}}])
def test_reader_refuses_extension_annotation_collision(annotation):
    track = {"id": "title", "kind": "visual", "label": "Title", "opaque": {"keep": True},
             "app": {READER_PROJECTION_APP_KEY: annotation}}
    before = copy.deepcopy(track)
    with pytest.raises(ReaderProjectionError, match="track extensions collide"):
        reader_view({"tracks": [track]}, {"assets": {}})
    assert track == before


def test_reader_keeps_non_null_source_url_subject_to_strict_validation():
    config = {"clips": [{
        "id": "title", "track": "title", "clipType": "text", "at": 0, "hold": 1,
        "text": {"content": "Title"}, "sourceURL": "https://example.invalid/clip",
    }]}
    with pytest.raises(ReaderProjectionError, match="unknown key 'sourceURL'"):
        check_reader_projection(*reader_view(config, {"assets": {}}))


def test_reader_projects_r13_passthrough_extensions_but_keeps_reader_bindings():
    config = {
        "_phase2_canary": "preserve me",
        "clips": [{
            "id": "title", "track": "title", "clipType": "text", "at": 0, "hold": 1,
            "text": {"content": "Title"}, "org.example.notes": {"ordered": [2, 1], "nullable": None},
            "asset_id": "missing", "sourceURL": "https://example.invalid/clip",
        }],
        "tracks": [{"id": "title", "kind": "visual", "label": "Title", "role": "source",
                    "org.example.notes": {"foreign_id": "not-a-reference"}}],
        "theme_overrides": {"opaque_theme": [None, False]},
    }
    projected, _ = reader_view(config, {"assets": {}})
    assert projected["app"][READER_PROJECTION_APP_KEY]["extensions"]["config"] == {
        "_phase2_canary": "preserve me",
    }
    clip = projected["clips"][0]
    assert clip["app"][READER_PROJECTION_APP_KEY]["extensions"]["clip"] == {
        "org.example.notes": {"ordered": [2, 1], "nullable": None},
    }
    assert clip["asset_id"] == "missing"
    assert clip["sourceURL"] == "https://example.invalid/clip"
    assert projected["tracks"][0]["role"] == "source"
    assert projected["tracks"][0]["app"][READER_PROJECTION_APP_KEY]["extensions"]["track"] == {
        "org.example.notes": {"foreign_id": "not-a-reference"},
    }
    assert projected["app"][READER_PROJECTION_APP_KEY]["extensions"]["theme_overrides"] == {
        "opaque_theme": [None, False],
    }


def test_reader_projection_uses_arbitrary_namespaced_and_structured_supplemental_fields():
    config = {
        "clips": [{"id": "c", "track": "t", "at": 0, "clipType": "image",
                   "vendor.media-hint": "plate", "opaquePayload": {"revision": 4}}],
        "tracks": [{"id": "t", "kind": "visual", "label": "T",
                    "vendor.lane-hint": "foreground", "opaqueSettings": [1, 2]}],
        "theme_overrides": {"vendor.palette": {"tone": "warm"}},
        "vendor.root-hint": "preserved",
    }
    projected, _ = reader_view(config, {"assets": {}})
    annotations = projected["app"][READER_PROJECTION_APP_KEY]["extensions"]
    assert annotations["config"] == {"vendor.root-hint": "preserved"}
    assert annotations["theme_overrides"] == {"vendor.palette": {"tone": "warm"}}
    assert projected["clips"][0]["app"][READER_PROJECTION_APP_KEY]["extensions"]["clip"] == {
        "vendor.media-hint": "plate", "opaquePayload": {"revision": 4},
    }
    assert projected["tracks"][0]["app"][READER_PROJECTION_APP_KEY]["extensions"]["track"] == {
        "vendor.lane-hint": "foreground", "opaqueSettings": [1, 2],
    }


def test_reader_preserves_actual_schema_clip_fields_as_structural():
    clip = {
        "id": "title", "track": "title", "clipType": "text", "at": 0, "hold": 1,
        "text": {"content": "Title"}, "params": {"keyframes": []},
        "generation": {"seed": 3}, "effects": [], "fit": "cover",
        "keyframes": {}, "elementRef": {"id": "registered"},
    }
    config = {"clips": [clip], "tracks": [{"id": "title", "kind": "visual", "label": "Title"}]}
    projected, registry = reader_view(config, {"assets": {}})
    check_reader_projection(projected, registry)
    assert projected["clips"][0] == clip


@pytest.mark.parametrize(("owner", "field"), [
    ("clip", "opaque2"), ("track", "opaque2"), ("theme_overrides", "opaque2"),
])
def test_reader_keeps_unqualified_scalar_unknowns_for_strict_refusal(owner, field):
    config = {"clips": [], "tracks": []}
    if owner == "clip":
        config["clips"] = [{"id": "c", "track": "t", "at": 0, field: "not metadata"}]
    elif owner == "track":
        config["tracks"] = [{"id": "t", "kind": "visual", "label": "T", field: "not metadata"}]
    else:
        config["theme_overrides"] = {field: "not metadata"}
    projected, registry = reader_view(config, {"assets": {}})
    with pytest.raises(ReaderProjectionError, match="unknown key|unexpected"):
        check_reader_projection(projected, registry)


def test_reader_refuses_javascript_reserved_extension_keys():
    with pytest.raises(ReaderProjectionError, match="JavaScript reserved key"):
        reader_view({"constructor": {"polluted": True}}, {"assets": {}})


@pytest.mark.parametrize("clip_type", ["shot", "future-clip-kind"])
def test_reader_does_not_hide_extensions_on_unsupported_clip_kinds(clip_type):
    config = {"clips": [{"id": "c", "track": "t", "at": 0, "clipType": clip_type,
                          "opaque_future_field": {"keep": True}}]}
    projected, _ = reader_view(config, {"assets": {}})
    assert projected["clips"][0]["opaque_future_field"] == {"keep": True}


def test_reader_refuses_nonfinite_detached_extension_value():
    config = {"future_extension": float("nan")}
    with pytest.raises(ReaderProjectionError, match="bounded finite JSON"):
        reader_view(config, {"assets": {}})


@pytest.mark.parametrize(("section", "field", "value"), [
    ("clip", "at", "soon"), ("clip", "params", "opaque"),
    ("track", "opacity", "high"), ("theme_overrides", "visual", "blue"),
])
def test_reader_does_not_project_malformed_structural_values(section, field, value):
    config = {"clips": [], "tracks": []}
    if section == "clip":
        config["clips"] = [{"id": "c", "track": "t", "at": 0, "clipType": "image", field: value}]
    elif section == "track":
        config["tracks"] = [{"id": "t", "kind": "visual", "label": "T", field: value}]
    else:
        config["theme_overrides"] = {field: value}
    with pytest.raises(ReaderProjectionError):
        check_reader_projection(*reader_view(config, {"assets": {}}))


@pytest.mark.parametrize("clip_type", ["image", "video", "audio"])
def test_known_media_clip_kinds_project_opaque_metadata(clip_type):
    config = {"clips": [{"id": "c", "track": "t", "at": 0, "clipType": clip_type,
                          "vendor.clip": {"opaque": True}}]}
    projected, _ = reader_view(config, {"assets": {}})
    assert projected["clips"][0]["app"][READER_PROJECTION_APP_KEY]["extensions"]["clip"] == {
        "vendor.clip": {"opaque": True},
    }
