"""Flat parent ripple helpers publish as one guarded edit and undo exactly."""
from __future__ import annotations

import copy

import pytest

from astrid.core.timeline.duration import clip_timeline_duration
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.commit import _digest, _manifest
from tests.sdk import test_timeline_arrange_commit as arrange_tests

arranged_session = arrange_tests.arranged_session


def _flat_audio_picture_fixture(realm, *, short_gap=False):
    """Give the parent composition flat picture/audio rows and a real embedded transcript."""
    document = realm.documents[realm.head["revision_id"]]
    parent = document["parent_revision"]["payload"]
    clips = [
        {"id": "before", "clipType": "media", "track": "picture", "asset": "picture",
         "at": 0.0, "from": 0.0, "to": 0.5},
        {"id": "inside", "clipType": "media", "track": "picture2", "asset": "picture",
         "at": 4.5, "from": 10.0, "to": 10.5},
        {"id": "across", "clipType": "media", "track": "picture", "asset": "picture",
         "at": 3.0, "from": 20.0, "to": 24.0},
        {"id": "after", "clipType": "media", "track": "picture", "asset": "long",
         "at": 8.0, "from": 30.0, "to": 31.0},
        {"id": "voice", "clipType": "media", "track": "audio", "asset": "voice",
         "at": 0.0, "from": 0.0, "to": 10.0,
         "app": {"astrid": {"segment": "voice", "text": "before after",
                               "words": [[0.1, 1.2, "before"],
                                         [4.1 if short_gap else 6.1,
                                          4.4 if short_gap else 6.4, "after"]]}}},
        {"id": "sfx-before", "clipType": "media", "track": "sfx", "asset": "voice",
         "at": 0.5, "from": 11.0, "to": 11.5},
        {"id": "sfx-inside", "clipType": "media", "track": "sfx", "asset": "voice",
         "at": 4.5, "from": 12.0, "to": 12.5},
        {"id": "sfx-edge-start", "clipType": "media", "track": "sfx", "asset": "voice",
         "at": 4.0, "from": 13.0, "to": 13.5},
        {"id": "sfx-edge-end", "clipType": "media", "track": "sfx", "asset": "voice",
         "at": 6.0, "from": 14.0, "to": 14.5},
        {"id": "sfx-after", "clipType": "media", "track": "sfx", "asset": "voice",
         "at": 8.0, "from": 15.0, "to": 15.5},
    ]
    parent["clips"] = copy.deepcopy(clips)
    parent["config"]["clips"] = copy.deepcopy(clips)
    parent["config"]["tracks"] = [
        {"id": "picture", "kind": "visual"}, {"id": "picture2", "kind": "visual"},
        {"id": "audio", "kind": "audio"},
        {"id": "sfx", "kind": "audio"},
    ]
    parent["config"]["output"]["fps"] = {"numerator": 30, "denominator": 1}
    parent["occurrences"] = []
    parent["registry"]["assets"]["picture"].update(type="video", duration=100.0)
    parent["registry"]["assets"]["long"] = copy.deepcopy(parent["registry"]["assets"]["picture"])
    parent["registry"]["assets"]["voice"] = {
        "media_id": parent["registry"]["assets"]["picture"]["media_id"],
        "type": "audio", "duration": 20.0,
    }
    parent["config"]["registry"] = copy.deepcopy(parent["registry"])
    document["parent_revision"]["content_digest"] = _digest(parent)
    document["shot_revisions"] = []
    document["internal_timeline_revisions"] = []
    document["dependency_manifest"] = _manifest(
        [], [], document["dependency_manifest"]["media"], [],
    )
    return copy.deepcopy(document)


@pytest.mark.parametrize("operation", ["insert", "delete", "close-gap"])
def test_flat_ripple_function_commit_and_immediate_undo_are_exact(arranged_session, operation):
    tl, realm, calls = arranged_session
    original = _flat_audio_picture_fixture(realm, short_gap=operation == "close-gap")

    def edit(t):
        if operation == "insert":
            t.insert_time(4, 2)
        elif operation == "delete":
            t.ripple_delete(4, 6)
        else:
            # Resolve the id from the checkout passed into the function, as an agent would.
            observed = next(word for word in t.words() if word.text == "after")
            t.close_gap(before=observed.id)

    result = tl.commit(edit)
    assert len(calls) == len(realm.accepted) == 1
    assert calls[0]["merge"] == "atomic"
    payload = result["data"]["canonical_document"]["parent_revision"]["payload"]
    rows = {row["id"]: row for row in payload["clips"]}
    if operation == "insert":
        assert rows["before"]["at"] == 0
        assert rows["inside"]["at"] == pytest.approx(6.5)
        assert rows["after"]["at"] == pytest.approx(10)
        assert rows["across"]["to"] == pytest.approx(26)
    elif operation == "delete":
        assert "inside" not in rows
        assert rows["before"]["at"] == 0
        assert rows["across"]["to"] == pytest.approx(22)
        assert rows["after"]["at"] == pytest.approx(6)
    else:
        assert rows["before"]["at"] == 0
        assert rows["inside"]["at"] == pytest.approx(1.6)
        assert rows["after"]["at"] == pytest.approx(5.1)
        assert (rows["across"]["at"], rows["across"]["from"], rows["across"]["to"]) == pytest.approx(
            (1.2, 21.1, 24.0))

    # The spanning audio clip follows picture time while retaining the right source windows.
    voice_rows = [row for row in payload["clips"] if row["track"] == "audio"]
    assert len(voice_rows) == 2
    assert all(row.get("from") is not None and row.get("to") is not None for row in voice_rows)
    if operation == "delete":
        assert rows["across"]["to"] == pytest.approx(22)
        assert [(row["from"], row["to"]) for row in voice_rows] == [(0.0, 4.0), (6.0, 10.0)]
        assert voice_rows[1]["at"] == pytest.approx(4.0)
    elif operation == "close-gap":
        assert [(row["from"], row["to"]) for row in voice_rows] == [(0.0, 1.2), (4.1, 10.0)]
        assert voice_rows[1]["at"] == pytest.approx(1.2)
    else:
        assert [(row["from"], row["to"]) for row in voice_rows] == [(0.0, 4.0), (4.0, 10.0)]
        assert voice_rows[1]["at"] == pytest.approx(6.0)

    sfx = {row["id"]: row for row in payload["clips"] if row["track"] == "sfx"}
    assert sfx["sfx-before"]["at"] == pytest.approx(0.5)
    if operation == "insert":
        assert {key: sfx[key]["at"] for key in sfx if key != "sfx-before"} == {
            "sfx-inside": 6.5, "sfx-edge-start": 6.0, "sfx-edge-end": 8.0, "sfx-after": 10.0}
    elif operation == "delete":
        assert "sfx-inside" not in sfx and "sfx-edge-start" not in sfx
        assert sfx["sfx-edge-end"]["at"] == pytest.approx(4)
        assert sfx["sfx-after"]["at"] == pytest.approx(6)
    else:
        assert sfx["sfx-inside"]["at"] == pytest.approx(1.6)
        assert (sfx["sfx-edge-end"]["at"], sfx["sfx-edge-end"]["from"], sfx["sfx-edge-end"]["to"]) == pytest.approx(
            (3.1, 14.0, 14.5))
        assert (sfx["sfx-edge-start"]["at"], sfx["sfx-edge-start"]["from"]) == pytest.approx((1.2, 13.1))
        assert sfx["sfx-after"]["at"] == pytest.approx(5.1)

    undone = tl.undo(publications=[result["receipt"]["receipt_id"]])
    assert len(calls) == len(realm.accepted) == 2
    assert undone["data"]["canonical_document"] == original


@pytest.mark.parametrize("operation", ["insert", "delete", "close-gap"])
def test_flat_ripple_composes_with_free_edits_under_one_capture_guard(arranged_session, operation):
    tl, realm, calls = arranged_session
    original = _flat_audio_picture_fixture(realm, short_gap=operation == "close-gap")

    def edit(t):
        t.bundle["parent"]["name"] = "Ripple with surrounding edits"
        t.bundle["parent"]["custom"] = {"before": True}
        if operation == "insert":
            t.insert_time(4, 2)
        elif operation == "delete":
            t.ripple_delete(4, 6)
        else:
            observed = next(word for word in t.words() if word.text == "after")
            t.close_gap(before=observed.id)
        t.bundle["parent"]["custom"]["after"] = True
        # Helpers edit the evaluation checkout; only commit() may publish it.
        assert calls == [] and realm.accepted == []

    result = tl.commit(edit)
    assert len(calls) == len(realm.accepted) == 1
    assert calls[0]["merge"] == "atomic" and "reads" not in calls[0]
    assert calls[0]["commands"] and all(command["op"] == "doc.patch" for command in calls[0]["commands"])
    assert calls[0]["steps"]
    assert all({"kind": "scope", "ref": "timeline", "path": []} in step["reads"]
               for step in calls[0]["steps"])

    landed = result["data"]["canonical_document"]
    payload = landed["parent_revision"]["payload"]
    assert payload["name"] == "Ripple with surrounding edits"
    assert payload["custom"] == {"before": True, "after": True}
    after = next(row for row in payload["clips"] if row["id"] == "after")
    assert after["at"] == pytest.approx({"insert": 10, "delete": 6, "close-gap": 5.1}[operation])

    undone = tl.undo(publications=[result["receipt"]["receipt_id"]])
    assert len(calls) == len(realm.accepted) == 2
    assert undone["data"]["canonical_document"] == original


@pytest.mark.parametrize("damage", ["split-id-collision", "source-overrun"])
def test_flat_ripple_refusal_is_atomic(arranged_session, damage):
    tl, realm, calls = arranged_session
    _flat_audio_picture_fixture(realm)
    document = realm.documents[realm.head["revision_id"]]
    parent = document["parent_revision"]["payload"]
    if damage == "split-id-collision":
        collision = {"id": "voice~4000", "clipType": "media", "track": "picture", "asset": "picture",
                     "at": 12.0, "from": 40.0, "to": 41.0}
        parent["clips"].append(collision)
        parent["config"]["clips"].append(copy.deepcopy(collision))
    else:
        parent["registry"]["assets"]["picture"]["duration"] = 25.0
        parent["config"]["registry"] = copy.deepcopy(parent["registry"])
    document["parent_revision"]["content_digest"] = _digest(parent)
    before = copy.deepcopy(realm.documents[realm.head["revision_id"]])
    outbox_before = tl.session.outbox.path.read_bytes()

    expected = "colliding clip id" if damage == "split-id-collision" else "only 25"
    with pytest.raises(TimelineEditError, match=expected):
        tl.commit(lambda t: t.insert_time(4, 2))

    assert realm.documents[realm.head["revision_id"]] == before
    assert realm.accepted == [] and calls == []
    assert tl.session.outbox.path.read_bytes() == outbox_before


def test_flat_close_gap_refuses_inside_content_atomically(arranged_session):
    tl, realm, calls = arranged_session
    _flat_audio_picture_fixture(realm)
    before = copy.deepcopy(realm.documents[realm.head["revision_id"]])
    outbox_before = tl.session.outbox.path.read_bytes()

    def close_observed_gap(t):
        observed = next(word for word in t.words() if word.text == "after")
        t.close_gap(before=observed.id)

    with pytest.raises(TimelineEditError, match="inside a closed gap"):
        tl.commit(close_observed_gap)

    assert realm.documents[realm.head["revision_id"]] == before
    assert realm.accepted == [] and calls == []
    assert tl.session.outbox.path.read_bytes() == outbox_before


@pytest.mark.parametrize("operation", ["insert", "delete"])
@pytest.mark.parametrize("policy", ["keep_end", "plays_on"])
@pytest.mark.parametrize("held", [False, True])
def test_flat_music_ripple_commits_once_and_undo_restores_source_and_order(arranged_session, operation, policy, held):
    tl, realm, calls = arranged_session
    _flat_audio_picture_fixture(realm)
    document = realm.documents[realm.head["revision_id"]]
    parent = document["parent_revision"]["payload"]
    parent["config"]["tracks"].append({"id": "music", "kind": "audio"})
    music = {"id": "bed", "clipType": "media", "track": "music", "asset": "voice",
             "at": 0, "from": 0, "to": 10}
    if held:
        music["hold"] = 10
    parent["clips"].insert(2, music)
    parent["config"]["clips"] = copy.deepcopy(parent["clips"])
    document["parent_revision"]["content_digest"] = _digest(parent)
    original = copy.deepcopy(document)

    def edit(t):
        if operation == "insert":
            t.insert_time(4, 2, music=policy)
        else:
            t.ripple_delete(4, 6, music=policy)

    result = tl.commit(edit)
    assert len(calls) == len(realm.accepted) == 1
    after = result["data"]["canonical_document"]["parent_revision"]["payload"]
    beds = [row for row in after["clips"] if row["track"] == "music"]
    assert len(beds) == (2 if policy == "keep_end" else 1)
    expected_end = (12 if operation == "insert" else 8) if policy == "keep_end" else 10
    assert max(row["at"] + clip_timeline_duration(row) for row in beds) == pytest.approx(expected_end)
    undone = tl.undo(publications=[result["receipt"]["receipt_id"]])
    assert len(calls) == len(realm.accepted) == 2
    assert undone["data"]["canonical_document"] == original


@pytest.mark.parametrize(
    ("operation", "match"),
    [
        (lambda t: t.insert_time(4, -0.1), "positive"),
        (lambda t: t.insert_time(4, True), "positive"),
        (lambda t: t.insert_time(4, 0.001), "frame rounding"),
        (lambda t: t.insert_time(float("nan"), 1), "finite"),
        (lambda t: t.insert_time(-1, 1), "nonnegative"),
        (lambda t: t.ripple_delete(-1, 2), "start < end"),
        (lambda t: t.ripple_delete(1, float("inf")), "start < end"),
        (lambda t: t.ripple_delete(0.001, 0.002), "frame rounding"),
        (lambda t: t.close_gap(before="voice:1", keep=-1), "nonnegative"),
        (lambda t: t.close_gap(before="voice:1", keep=True), "nonnegative"),
        (lambda t: t.close_gap(before="voice:1", keep=float("inf")), "nonnegative"),
    ],
)
def test_flat_ripple_bad_timing_is_rejected_before_capture(arranged_session, operation, match):
    tl, realm, calls = arranged_session
    _flat_audio_picture_fixture(realm)
    before = copy.deepcopy(realm.documents[realm.head["revision_id"]])
    outbox_before = tl.session.outbox.path.read_bytes()

    with pytest.raises(TimelineEditError, match=match):
        tl.commit(operation)

    assert realm.documents[realm.head["revision_id"]] == before
    assert realm.accepted == [] and calls == []
    assert tl.session.outbox.path.read_bytes() == outbox_before
