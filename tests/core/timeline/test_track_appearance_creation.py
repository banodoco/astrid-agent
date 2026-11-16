from __future__ import annotations

import copy

import pytest

from astrid.core.timeline.events.schema.payloads.track import TrackAddedPayload
from astrid.core.timeline.projection import _apply_track_added
from astrid.core.timeline.track_appearance import (
    CLIP_PLACEMENT_VERSION,
    SCALE_APPLIES_TO_POSITIONED_CLIPS,
    stamp_new_track_appearance,
)
from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.media_add import _placement_commands
from astrid.sdk.timeline.lookup import _LookupMixin
from astrid.sdk.timeline_editing import add_track
from tests.sdk.test_timeline_sync import initial_bundle


def _assert_stamped(row):
    assert row["app"][SCALE_APPLIES_TO_POSITIONED_CLIPS] is True
    assert row["app"][CLIP_PLACEMENT_VERSION] == 1


class _Document(_LookupMixin):
    def __init__(self):
        self.bundle = {"parent": {"config": {"tracks": []}}}


def test_all_new_track_creation_paths_stamp_markers_and_keep_unrelated_app():
    direct = add_track({"tracks": []}, kind="visual", track_id="direct")
    _assert_stamped(direct)

    via_lookup = _Document()
    via_lookup.track("lookup")
    _assert_stamped(via_lookup.bundle["parent"]["config"]["tracks"][0])

    commands = _placement_commands(
        initial_bundle(), {"asset": "media-alias"}, {"media_type": "image/png"},
        on=0, duration=2, track=None, clip_id="new-clip", values={},
    )
    implicit = next(command["args"]["row"] for command in commands
                    if command["op"] == "track.create")
    _assert_stamped(implicit)

    original = {"id": "captured", "kind": "visual", "app": {"owner": "keep"}}
    captured = capture_track_create(initial_bundle(), "parent", original)
    _assert_stamped(captured["args"]["row"])
    assert captured["args"]["row"]["app"]["owner"] == "keep"
    assert original == {"id": "captured", "kind": "visual", "app": {"owner": "keep"}}

    projected = {"tracks": []}
    _apply_track_added(projected, TrackAddedPayload("legacy-event", "visual", "Legacy event"))
    _assert_stamped(projected["tracks"][0])


@pytest.mark.parametrize("version", [0, 2, True, "1", None])
def test_new_track_refuses_invalid_or_future_clip_placement_marker(version):
    row = {"id": "future", "kind": "visual",
           "app": {"owner": "keep", CLIP_PLACEMENT_VERSION: version}}
    before = copy.deepcopy(row)

    with pytest.raises(ValueError, match=r"track 'future' property app\.clipPlacementVersion"):
        capture_track_create(initial_bundle(), "parent", row)

    assert row == before


def test_new_track_refuses_invalid_existing_scale_marker():
    row = {"id": "bad-scale", "kind": "visual",
           "app": {SCALE_APPLIES_TO_POSITIONED_CLIPS: "yes"}}
    with pytest.raises(ValueError, match=r"app\.scaleAppliesToPositionedClips must be a boolean"):
        stamp_new_track_appearance(row)


def test_valid_marker_is_idempotent_and_scale_marker_false_is_stamped_current():
    row = {"id": "already", "kind": "visual",
           "app": {"owner": "keep", CLIP_PLACEMENT_VERSION: 1,
                   SCALE_APPLIES_TO_POSITIONED_CLIPS: False}}
    assert stamp_new_track_appearance(row) is row
    _assert_stamped(row)
    assert row["app"]["owner"] == "keep"
