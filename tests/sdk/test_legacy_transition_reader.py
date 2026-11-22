"""R13 transition identities are adapted only in detached reader views."""

import json

import pytest

from astrid.core.element import catalog
from astrid.core.timeline import validate_timeline
from astrid.core.timeline.reader_projection import check_reader_projection, reader_view
from astrid.packs.rendering.actions.render.managed_timeline import _render_compatible_projection


_REGISTRY = {"assets": {"a": {"type": "video", "file": "a.mp4"}}}


def _r13_config(transition):
    return {
        "tracks": [{"id": "v", "kind": "visual", "label": "Video"}],
        "clips": [
            {"id": "a", "track": "v", "clipType": "media", "asset": "a", "at": 0, "hold": 2,
             "transition": transition},
            {"id": "b", "track": "v", "clipType": "media", "asset": "a", "at": 2, "hold": 2},
        ],
    }


@pytest.mark.parametrize(
    "saved_id, reader_id",
    [
        ("crossfade", "cross-fade"),
        ("wipe", "wipe"),
        ("slide-push", "slide-push"),
        ("zoom-through", "zoom-through"),
    ],
)
@pytest.mark.parametrize("reader", [reader_view, _render_compatible_projection])
def test_r13_reader_projection_opens_without_mutating_stored_bytes(saved_id, reader_id, reader):
    source = _r13_config({"type": saved_id, "duration": 0.5})
    before = json.dumps(source, separators=(",", ":")).encode()
    config, assets = reader(source, _REGISTRY)
    check_reader_projection(config, assets)
    assert config["clips"][0]["transition"] == {"type": reader_id, "duration": 0.5}
    assert json.dumps(source, separators=(",", ":")).encode() == before


@pytest.mark.parametrize(
    "transition, expected",
    [
        ("crossfade", "cross-fade"),
        ({"id": "crossfade", "durationFrames": 10}, {"id": "cross-fade", "durationFrames": 10}),
    ],
)
def test_reader_canonicalizes_other_supported_alias_shapes(transition, expected):
    source = _r13_config(transition)
    config, assets = reader_view(source, _REGISTRY)
    assert config["clips"][0]["transition"] == expected
    check_reader_projection(config, assets)
    assert source["clips"][0]["transition"] == transition


@pytest.mark.parametrize("identity", ["crossfade", "wipe", "slide-push", "zoom-through"])
def test_strict_timeline_validator_accepts_authored_r13_transition(identity):
    validate_timeline(_r13_config({"type": identity, "duration": 0.5}))


def test_legacy_transition_catalog_only_extends_reader_membership():
    legacy_ids = {"wipe", "slide-push", "zoom-through"}
    assert legacy_ids <= set(catalog.list_transition_ids(include_legacy=True))
    assert legacy_ids.isdisjoint(catalog.list_transition_ids())


@pytest.mark.parametrize("reader", [reader_view, _render_compatible_projection])
def test_unknown_transition_remains_refused(reader):
    config, registry = reader(_r13_config({"type": "crossfadee", "duration": 0.5}), _REGISTRY)
    with pytest.raises(ValueError, match="crossfadee"):
        check_reader_projection(config, registry)
