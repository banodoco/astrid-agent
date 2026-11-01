from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_add_media
from astrid.sdk.timeline.native_parent_media_commands import capture_clip_add_media
from tests.editing_conformance.test_recipe_m1_commands import inverse
from tests.editing_conformance.test_recipe_occurrence_media import inputs


def _case(owner):
    document, _old, media, verified = inputs("audio")
    verified = [dict(row, duration_seconds=3.0) for row in verified]
    bundle = bundle_from_document(document)
    if owner == "parent":
        lane_owner, track = "parent", "voice"
    else:
        lane_owner, track = "occurrence:occ-a", "voice"
    track_command = capture_track_create(bundle, lane_owner, {"id": track, "kind": "audio"})
    with_track = execute(bundle, [track_command]).bundle
    if owner == "parent":
        row = {"id": "audio-child", "clipType": "audio", "asset": media["asset"],
               "track": track, "at": 2.5, "hold": 1.25, "from": 0.5, "to": 1.75,
               "speed": 1}
        media_command = capture_clip_add_media(with_track, row, media, verified_media=verified)
    else:
        row = {"id": "audio-child", "clipType": "audio", "asset": media["asset"],
               "track": track, "at": 0.125, "hold": 1.25, "from": 0.5, "to": 1.75,
               "speed": 1}
        media_command = capture_occurrence_add_media(
            with_track, "occurrence:occ-a", row, media, verified_media=verified)
    return bundle, track_command, media_command, media, verified, row


@pytest.mark.parametrize("owner", ["parent", "occurrence"])
def test_native_audio_birth_captures_verified_window_and_undoes_exactly(owner):
    bundle, track_command, media_command, media, verified, row = _case(owner)
    before = copy.deepcopy(bundle)
    plan = execute(bundle, [track_command, media_command], verified_media=verified)

    state = State(plan.bundle)
    if owner == "parent":
        clip = state.entity("parent/clip:audio-child")
        registry = state.registry("parent")["assets"][media["asset"]]
    else:
        clip = state.entity("occurrence:occ-a/clip:audio-child")
        registry = state.registry("occurrence:occ-a")["assets"][media["asset"]]
    assert clip["track"] == "voice"
    assert (clip["from"], clip["to"], clip["hold"]) == (0.5, 1.75, 1.25)
    assert registry["type"] == "audio"
    assert registry["duration"] == 3.0
    assert inverse(plan.bundle, plan.records).bundle == before
    assert bundle == before


@pytest.mark.parametrize("owner", ["parent", "occurrence"])
@pytest.mark.parametrize("failure", ["visual-lane", "bad-duration", "outside-window"])
def test_native_audio_birth_refuses_invalid_audio_inputs_without_mutation(owner, failure):
    bundle, track_command, media_command, _media, verified, row = _case(owner)
    if failure in ("visual-lane", "outside-window"):
        target = execute(bundle, [track_command]).bundle
        before = copy.deepcopy(target)
        bad_row = dict(row)
        if failure == "visual-lane":
            bad_row["track"] = "picture"
        else:
            bad_row["to"] = 3.5
        with pytest.raises(CommandError):
            if owner == "parent":
                capture_clip_add_media(target, bad_row, media_command["args"]["media"],
                                       verified_media=verified)
            else:
                capture_occurrence_add_media(target, "occurrence:occ-a", bad_row,
                                             media_command["args"]["media"],
                                             verified_media=verified)
        assert target == before
    else:
        before = copy.deepcopy(bundle)
        bad_verified = [dict(item, duration_seconds=0) for item in verified]
        with pytest.raises(CommandError):
            execute(bundle, [track_command, media_command], verified_media=bad_verified)
        assert bundle == before
