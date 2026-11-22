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
