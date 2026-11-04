"""Chapters as labels: one authoring shot, music joined, a layer carried across a chapter wall."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline_structure import carry_across, join_music, label_chapters, one_shot
from tests.sdk.test_timeline_checkout import bundle


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return 'sha256:' + hashlib.sha256(encoded.encode()).hexdigest()


@pytest.fixture(autouse=True)
def _authoring_films_are_not_flattened(monkeypatch):
    """one_shot is the authoring-time flatten; these films are checked out as placed (not as a stored chapter film)."""
    monkeypatch.setattr("astrid.sdk.timeline_restructure.is_split", lambda bundle: False)


def two_chapters():
    data = bundle()
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
    for shot in data["shots"].values():
        for index, asset in enumerate(shot["internal_timeline"]["registry"]["assets"].values(), 1):
            asset["media_id"] = "sha256:" + str(index) * 64
    parent_payload = copy.deepcopy(data["parent"])
    source_shots, source_internals, occurrences = [], [], []
    for row in data["placements"]:
        shot_id = row["shot_id"]
        shot = data["shots"][shot_id]
        revision_id, internal_revision_id = f"{shot_id}-revision", f"{shot_id}-internal"
        shot_payload = copy.deepcopy(shot["payload"])
        internal_payload = copy.deepcopy(shot["internal_timeline"])
        source_shots.append({
            "revision_id": revision_id, "project_id": data["project_id"], "shot_id": shot_id,
            "internal_timeline_revision_id": internal_revision_id,
            "content_digest": _digest(shot_payload), "payload": shot_payload,
        })
        source_internals.append({
            "revision_id": internal_revision_id, "project_id": data["project_id"],
            "timeline_id": data["timeline_id"], "content_digest": _digest(internal_payload),
            "payload": internal_payload,
        })
        occurrences.append({**copy.deepcopy(row), "shot_revision_id": revision_id})
    parent_payload["occurrences"] = occurrences
    parent_revision = {
        "revision_id": "parent-1", "project_id": data["project_id"], "timeline_id": data["timeline_id"],
        "content_digest": _digest(parent_payload), "payload": parent_payload,
    }
    from astrid.core.timeline.authoring_bundle import open_authoring_bundle

    candidate = open_authoring_bundle(
        parent_revision, shot_revisions=source_shots, internal_timeline_revisions=source_internals,
    )
    from tests.timeline.test_authoring_bundle import _runtime_document

    candidate["source_mapping"]["_runtime_document"] = _runtime_document(
        parent_revision, source_shots, source_internals,
    )
    return candidate


class _PublicationWriter:
    """Keep the authenticated base read separate from the accepted publication."""

    def __init__(self, base_document, published_document):
        from tests.timeline.test_authoring_bundle import _Writer

        self._writer = _Writer(base_document)
        self.published_document = published_document
        self.calls = self._writer.calls

    def __getattr__(self, name):
        return getattr(self._writer, name)

    def commit_timeline(self, project_id, timeline_id, body, *, idempotency_key):
        self._writer.document = self.published_document
        return self._writer.commit_timeline(project_id, timeline_id, body, idempotency_key=idempotency_key)


def _publication_documents(bundle):
    """Build the complete provider-shaped base and accepted publication images."""
    from astrid.core.timeline.authoring_bundle import compile_authoring_candidate

    publication = compile_authoring_candidate(bundle).publication
    manifest = copy.deepcopy(publication['dependency_manifest'])
    manifest.setdefault('documents', [])
    accepted = {
        'schema_version': 1,
        'project_id': publication['project_id'],
        'timeline_id': publication['timeline_id'],
        'parent_revision': {
            'revision_id': 'parent-1',
            'content_digest': publication['content_digest'],
            'payload': copy.deepcopy(publication['parent_composition']),
        },
        'shot_revisions': copy.deepcopy(publication['shot_revisions']),
        'internal_timeline_revisions': copy.deepcopy(publication['internal_timeline_revisions']),
        'dependency_manifest': manifest,
    }
    return copy.deepcopy(bundle['source_mapping']['_runtime_document']), accepted


def _pin_flattened_source(checkout):
    """Use the current flattened authoring image as the accepted provider basis."""
    from astrid.core.timeline.authoring_bundle import compile_authoring_candidate

    publication = compile_authoring_candidate(checkout.bundle).publication
    source = {
        'schema_version': 1, 'project_id': publication['project_id'], 'timeline_id': publication['timeline_id'],
        'parent_revision': {
            'revision_id': 'parent-1', 'content_digest': publication['content_digest'],
            'payload': copy.deepcopy(publication['parent_composition']),
        },
        'shot_revisions': copy.deepcopy(publication['shot_revisions']),
        'internal_timeline_revisions': copy.deepcopy(publication['internal_timeline_revisions']),
        'dependency_manifest': {**copy.deepcopy(publication['dependency_manifest']), 'documents': []},
    }
    checkout.bundle['base_parent'].update(
        revision_id='parent-1', content_digest=publication['content_digest'],
    )
    checkout.bundle['base_parent_payload'] = copy.deepcopy(checkout.bundle['parent'])
    checkout.bundle['base_placements'] = copy.deepcopy({
        row['occurrence_id']: row for row in checkout.bundle['placements']
    })
    for shot_id, shot in checkout.bundle['shots'].items():
        shot['base_payload'] = copy.deepcopy(shot['payload'])
        shot['base_internal_timeline'] = copy.deepcopy(shot['internal_timeline'])
    source_by_shot = {row['shot_id']: row for row in source['shot_revisions']}
    for shot_id, row in source_by_shot.items():
        checkout.bundle['shots'][shot_id]['payload'] = copy.deepcopy(row['payload'])
        checkout.bundle['shots'][shot_id]['internal_timeline'] = copy.deepcopy(next(
            item['payload'] for item in source['internal_timeline_revisions']
            if item['revision_id'] == row['internal_timeline_revision_id']
        ))
        checkout.bundle['shots'][shot_id]['base_payload'] = copy.deepcopy(row['payload'])
        checkout.bundle['shots'][shot_id]['base_internal_timeline'] = copy.deepcopy(next(
            item['payload'] for item in source['internal_timeline_revisions']
            if item['revision_id'] == row['internal_timeline_revision_id']
        ))
        checkout.bundle['source_mapping']['shots'][shot_id] = {
            'shot_id': shot_id, 'revision_id': row['revision_id'],
            'content_digest': row['content_digest'],
            'internal_timeline_id': next(
                item['timeline_id'] for item in source['internal_timeline_revisions']
                if item['revision_id'] == row['internal_timeline_revision_id']
            ),
            'internal_timeline_revision_id': row['internal_timeline_revision_id'],
            'internal_timeline_content_digest': next(
                item['content_digest'] for item in source['internal_timeline_revisions']
                if item['revision_id'] == row['internal_timeline_revision_id']
            ),
            'item_id_map': {},
        }
    checkout.bundle['source_mapping']['shots'] = {
        key: checkout.bundle['source_mapping']['shots'][key] for key in source_by_shot
    }
    checkout.bundle['source_mapping']['placements'] = {
        row['occurrence_id']: copy.deepcopy(row) for row in checkout.bundle['placements']
    }
    checkout.bundle['source_mapping']['_runtime_document'] = source


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
    data = two_chapters()
    for shot in data["shots"].values():
        for index, asset in enumerate(shot["internal_timeline"]["registry"]["assets"].values(), 1):
            asset["media_id"] = "sha256:" + str(index) * 64
    data.update(schema_version=1, base_placements={}, base_parent_payload=copy.deepcopy(data["parent"]))
    data["base_parent"].update(
        revision_id="parent-1",
        content_digest=data["source_mapping"]["_runtime_document"]["parent_revision"]["content_digest"],
    )
    tl = Checkout(data)
    one_shot(tl, shot_id="film")
    _pin_flattened_source(tl)
    intent.set_for(tl.clip("a-music").data, 4.0)
    intent.set_until(tl.clip("a-music").data, "end of c1")
    assert join_music(tl) == ["b-music"]
    tl.resolve()
    music = tl.clip("a-music")
    assert music.end == pytest.approx(8.0)
    assert music.data["to"] == pytest.approx(8.0)
    assert intent.for_s(music.data) == pytest.approx(8.0)
    assert intent.until(music.data) is None
    writer = _PublicationWriter(*_publication_documents(tl.bundle))  # service-free transport; the real compiler/publication code runs.
    result = publish_authoring_candidate(tl.document(), writer, idempotency_key="joined-music")
    assert len(writer.calls) == 1
    publication = writer.calls[0][2]
    assert publication["base_revision_id"] == "parent-1"
    assert publication["base_publication_id"] == "publication-1"
    assert publication["api_version"] == 1
    stored = result["publication"]["data"]["canonical_document"]["internal_timeline_revisions"][0]["payload"]["clips"]
    published_music = next(c for c in stored if c["id"] == "a-music")
    assert published_music["to"] == pytest.approx(8.0)
    assert intent.for_s(published_music) == pytest.approx(8.0)
    assert result["publication"]["data"]["new_head"] == result["publication"]["data"]["accepted_revision_id"]


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
