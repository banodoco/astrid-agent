"""Chapters as labels: one authoring shot, music joined, a layer carried across a chapter wall."""
from __future__ import annotations

import pytest
import copy

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline_structure import carry_across, join_music, label_chapters, one_shot

from tests.sdk.test_timeline_checkout import bundle


@pytest.fixture(autouse=True)
def _authoring_films_are_not_flattened(monkeypatch):
    """one_shot is the authoring-time flatten; these films are checked out as placed (not as a stored chapter film)."""
    monkeypatch.setattr("astrid.sdk.timeline_restructure.is_split", lambda bundle: False)


def two_chapters():
    data = bundle()
    # Complete detached test-authoring envelope; no real Runtime identity.
    data["source_mapping"] = {"shots": {}, "placements": {}}
    a, b = data["shots"]["A"]["internal_timeline"]["clips"], data["shots"]["B"]["internal_timeline"]["clips"]
    data["shots"]["A"]["payload"]["name"] = "01 ONE"
    data["shots"]["B"]["payload"]["name"] = "02 TWO"
    a[0]["app"] = {"cut": "c1"}
    b[0]["app"] = {"cut": "c2"}
    a.append({"id": "a-tower", "clipType": "am-sprite", "track": "sprite", "asset": "R", "at": 3.0, "hold": 1.0,
              "params": {"x": 5, "enter": "stamp"}, "app": {"cut": "c1"}})
    b.append({"id": "b-tower", "clipType": "am-sprite", "track": "sprite", "asset": "R", "at": 0.0, "hold": 2.0,
              "params": {"x": 5, "enter": "stamp"}, "app": {"cut": "c2", "for": 2.0}})
    b.append({"id": "b-music", "clipType": "media", "track": "music", "asset": "Q", "at": 0.0, "from": 4.0, "to": 8.0})
    return data


def test_one_shot_keeps_every_time_and_labels_the_chapters():
    tl = Checkout(two_chapters())
    before = {c.id: (c.start, c.end) for c in tl.clips()}
    walls = one_shot(tl, shot_id="film", name="FILM")
    assert tl._shot_ids() == ["film"] and walls == [{"name": "01 ONE", "at": 0.0}, {"name": "02 TWO", "at": 4.0}]
    assert {c.id: (c.start, c.end) for c in tl.clips()} == {k: (pytest.approx(s), pytest.approx(e)) for k, (s, e) in before.items()}
    assert tl.clip("b-type").data["at"] == pytest.approx(6.0)  # at is now timeline seconds
    assert join_music(tl) == ["b-music"]
    music = [c for c in tl.clips() if c.track == "music"]
    assert len(music) == 1 and music[0].end == pytest.approx(8.0)
    assert label_chapters(tl, walls) == [{"name": "01 ONE", "from": "c1"}, {"name": "02 TWO", "from": "c2"}]
    assert intent.chapters(tl.bundle)[1]["from"] == "c2"


def test_a_layer_duplicated_at_a_wall_becomes_one_clip_that_carries_across():
    tl = Checkout(two_chapters())
    walls = one_shot(tl, shot_id="film", name="FILM")
    merged = carry_across(tl, [w["at"] for w in walls[1:]])
    assert merged and "a-tower carries across 4.00 s" in merged[0]
    tower = [c for c in tl.clips() if c.id.endswith("-tower")]
    assert [(c.id, c.start, c.end) for c in tower] == [("a-tower", pytest.approx(3.0), pytest.approx(6.0))]
    assert intent.for_s(tower[0].data) == pytest.approx(3.0)  # it kept b-tower's literal end
    assert {c.id for c in tl.clips() if c.track == "plate"} == {"a-plate", "b-plate"}  # pictures are never merged


def test_one_shot_rejects_incomplete_authoring_mapping_without_mutation():
    from astrid.sdk.timeline._common import TimelineEditError
    tl = Checkout(bundle())
    before = copy.deepcopy(tl.bundle)
    with pytest.raises(TimelineEditError, match="not an authoring bundle"):
        one_shot(tl, shot_id="film", name="FILM")
    assert tl.bundle == before


def test_carried_layer_removes_old_end_anchor_and_survives_resolve():
    tl = Checkout(two_chapters())
    one_shot(tl, shot_id="film")
    intent.set_until(tl.clip("a-tower").data, "end of c1")
    assert carry_across(tl, [4.0])
    assert intent.until(tl.clip("a-tower").data) is None
    tl.resolve()
    assert tl.clip("a-tower").end == pytest.approx(6.0)
    assert intent.for_s(tl.clip("a-tower").data) == pytest.approx(3.0)


def test_joined_music_updates_source_and_literal_duration_for_publication():
    from astrid.core.timeline.authoring_bundle import publish_authoring_candidate
    from tests.timeline.test_authoring_bundle import _Writer
    data = two_chapters()
    for shot in data["shots"].values():
        for index, asset in enumerate(shot["internal_timeline"]["registry"]["assets"].values(), 1):
            asset["media_id"] = "sha256:" + str(index) * 64
    data.update(schema_version=1, base_placements={}, base_parent_payload=copy.deepcopy(data["parent"]))
    data["base_parent"]["content_digest"] = "sha256:" + "0" * 64
    tl = Checkout(data)
    one_shot(tl, shot_id="film")
    intent.set_for(tl.clip("a-music").data, 4.0)
    intent.set_until(tl.clip("a-music").data, "end of c1")
    assert join_music(tl) == ["b-music"]
    tl.resolve()
    music = tl.clip("a-music")
    assert music.end == pytest.approx(8.0)
    assert music.data["to"] == pytest.approx(8.0)
    assert intent.for_s(music.data) == pytest.approx(8.0)
    assert intent.until(music.data) is None
    writer = _Writer()  # service-free transport; the real compiler/publication code runs.
    result = publish_authoring_candidate(tl.document(), writer, idempotency_key="joined-music")
    assert len(writer.calls) == 1
    publication = writer.calls[0][2]
    assert publication["expected_head"] == "rev-0"
    stored = publication["internal_timeline_revisions"][0]["payload"]["clips"]
    published_music = next(c for c in stored if c["id"] == "a-music")
    assert published_music["to"] == pytest.approx(8.0)
    assert intent.for_s(published_music) == pytest.approx(8.0)
    assert result["publication"]["data"]["new_head"] == publication["parent_revision_id"]


@pytest.mark.parametrize("location,key,value", [
    ("row", "gain", 0.5), ("row", "muted", True),
    ("row", "speed", {"numerator": 2, "denominator": 1}),
    ("row", "source_offset", {"start": 100, "end": 4100}),
    ("row", "transform", {"opacity": 0.5}),
    ("row", "provenance", {"keep": "opaque"}),
    ("internal", "effects", [{"type": "fade"}]),
    ("internal", "audio", [{"track_id": "music", "gain": 0.5}]),
    ("internal", "layout", {"width": 640}),
    ("internal", "opaque_timeline", {"keep": True}),
    ("payload", "text_bindings", [{"text": "keep"}]),
])
def test_one_shot_rejects_unflattenable_envelope_before_any_mutation(location, key, value):
    from astrid.sdk.timeline._common import TimelineEditError
    data = two_chapters()
    target = data["placements"][1] if location == "row" else data["shots"]["B"]["internal_timeline" if location == "internal" else "payload"]
    target[key] = value
    tl = Checkout(data)
    before = copy.deepcopy(tl.bundle)
    with pytest.raises(TimelineEditError, match="one_shot cannot flatten"):
        one_shot(tl, shot_id="film")
    assert tl.bundle == before
