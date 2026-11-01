from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_add_media
from tests.editing_conformance.test_recipe_m1_commands import inverse
from tests.editing_conformance.test_recipe_occurrence_media import inputs


def _audio_case():
    document, _old, media, verified = inputs("audio")
    verified = [dict(row, duration_seconds=3.0) for row in verified]
    bundle = bundle_from_document(document)
    lane = {"id": "voice", "kind": "audio", "role": "output"}
    track_command = capture_track_create(bundle, "occurrence:occ-a", lane)
    with_track = execute(bundle, [track_command]).bundle
    row = {
        "id": "audio-child",
        "clipType": "audio",
        "asset": media["asset"],
        "track": "voice",
        "at": 0.125,
        "hold": 1.25,
        "from": 0.5,
        "to": 1.75,
        "speed": 1,
    }
    add_command = capture_occurrence_add_media(
        with_track, "occurrence:occ-a", row, media, verified_media=verified
    )
    return document, bundle, track_command, add_command, verified, media


def test_occurrence_audio_uses_one_active_audio_lane_and_undo_restores_document_order():
    document, bundle, track_command, add_command, verified, media = _audio_case()
    before = copy.deepcopy(bundle)

    committed = execute(bundle, [track_command, add_command], verified_media=verified)
    state = State(committed.bundle)
    tracks = state.collection("occurrence:occ-a", "track")
    audio_tracks = [row for row in tracks if row.get("kind") == "audio"]
    clip = state.entity("occurrence:occ-a/clip:audio-child")

    assert [row["id"] for row in audio_tracks] == ["voice"]
    assert clip["track"] == "voice"
    assert (clip["from"], clip["to"], clip["hold"]) == (0.5, 1.75, 1.25)
    assert state.registry("occurrence:occ-a")["assets"][media["asset"]]["type"] == "audio"

    undone = inverse(committed.bundle, committed.records)
    assert undone.bundle == before
    assert document_from_bundle(undone.bundle) == document


def test_occurrence_audio_refuses_visual_lane():
    _document, bundle, track_command, add_command, verified, _media = _audio_case()
    with_track = execute(bundle, [track_command]).bundle
    State(with_track).entity("occurrence:occ-a/track:voice")["kind"] = "visual"
    before = copy.deepcopy(with_track)
    with pytest.raises(CommandError) as refused:
        capture_occurrence_add_media(
            with_track, "occurrence:occ-a", add_command["args"]["row"],
            add_command["args"]["media"], verified_media=verified
        )
    assert refused.value.kind == "unsupported_media"
    assert with_track == before


def test_occurrence_audio_refuses_source_window_beyond_verified_duration():
    _document, bundle, track_command, add_command, verified, _media = _audio_case()
    with_track = execute(bundle, [track_command]).bundle
    row = copy.deepcopy(add_command["args"]["row"])
    # Keep source-window length equal to hold; only the verified media bound fails.
    row.update({"from": 2.25, "to": 3.5, "hold": 1.25})
    before = copy.deepcopy(with_track)

    with pytest.raises(CommandError, match="source window") as refused:
        capture_occurrence_add_media(
            with_track, "occurrence:occ-a", row,
            add_command["args"]["media"], verified_media=verified
        )

    assert refused.value.kind == "unsupported_timing"
    assert with_track == before


def test_occurrence_audio_accepts_source_window_at_verified_duration_boundary():
    _document, bundle, track_command, add_command, verified, _media = _audio_case()
    with_track = execute(bundle, [track_command]).bundle
    row = copy.deepcopy(add_command["args"]["row"])
    row.update({"from": 1.75, "to": 3.0, "hold": 1.25})

    command = capture_occurrence_add_media(
        with_track, "occurrence:occ-a", row,
        add_command["args"]["media"], verified_media=verified
    )
    committed = execute(bundle, [track_command, command], verified_media=verified)

    clip = State(committed.bundle).entity("occurrence:occ-a/clip:audio-child")
    assert (clip["from"], clip["to"], clip["hold"]) == (1.75, 3.0, 1.25)
