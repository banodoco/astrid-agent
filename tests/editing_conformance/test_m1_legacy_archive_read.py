"""Read both the held M1 source and the later authenticated word-bound head."""
from __future__ import annotations

import pytest

from astrid.sdk.timeline import store

# Reuse the genuine archive, backup verification, Runtime registration, and
# transport through pytest's fixture dependency.  Do not call the fixture
# wrapper directly: the archive is the sole source for both explicit reads.
pytest_plugins = ("tests.editing_conformance.test_current_pair_reader",)
pytestmark = pytest.mark.integration

PROJECT = "7b6f2bae8ffe42bf925216c38249ed79"
BASELINE_REVISION = "film-parent-v1"
BASELINE_PUBLICATION = "txn-55c0651b2cd64255ae06e73f9708220a"
BASELINE_PARENT_DIGEST = "sha256:3df0dbb371b6add88b147b444f6ee01006b12655d9bde5f67f184f05be38f750"
ADVANCED_REVISION = "a3e671256f1c42b8837d3e118c97b21b"
ADVANCED_PUBLICATION = "txn-11c90075be6c4c8bb9e2627366f0fe4f"
TRANSCRIPT = "transcript:c49d7d673dc883c363f95ea19d01d75b66c00ba849b09cb833966e39d392133d"
BINDING = "fa141364-9d4d-57ca-b7dc-9a23d259312e"
TRANSCRIPT_MEDIA = "sha256:fef50eab9e9b23b75133cf7553faee605c3ff14d993599c8d2c551030f0bcb3d"
MEDIA = (
    "sha256:beec97efbe7637ec4c5468a08f39fd3263dd3762483d66318d13093c22645beb",
    "sha256:bfa95bbc854e387c08f73177c2a48719d9a928c3f2c94eca0e693f3f970014e5",
    "sha256:e29d6131553bfda749080174ccc5fb5f1be8eb27f2775323cb0293fcb6933a65",
    "sha256:f39a4fd033486fb1e77eed03a6787fa83a5860d280e4b5ee47f7f8372eed95a2",
    TRANSCRIPT_MEDIA,
)


def test_explicit_held_baseline_then_current_word_bound_read_is_closure_exact(reader):
    service, client = reader

    # This is an explicit historical read of the held source/publication pair;
    # the current head must not substitute for the original baseline.
    baseline = store.fetch_bundle(
        PROJECT,
        "film",
        revision_id=BASELINE_REVISION,
        publication_id=BASELINE_PUBLICATION,
        client=client,
    )
    baseline_document = baseline["source_mapping"]["_runtime_document"]
    assert baseline["project_id"] == PROJECT
    assert baseline["timeline_id"] == "film"
    assert baseline["base_parent"]["revision_id"] == BASELINE_REVISION
    assert baseline["base_parent"]["publication_id"] == BASELINE_PUBLICATION
    assert baseline_document == service._timeline_commits()[0].revision_closure(
        PROJECT, "film", BASELINE_REVISION)
    assert baseline_document["parent_revision"]["content_digest"] == BASELINE_PARENT_DIGEST

    parent = baseline_document["parent_revision"]["payload"]
    assert parent["clips"] == [{
        "asset": "music-bed",
        "at": 0,
        "clipType": "audio",
        "effects": [{
            "id": "music-fade",
            "params": {"direction": "in", "duration": 0.25},
            "type": "fade",
        }],
        "hold": 5.5,
        "id": "music-bed",
        "track": "music",
        "volume": 0.65,
    }]
    assert parent["config"] == {
        "output": {"fps": 24},
        "playback": {"fade": {"in": 0.25, "out": 0.4}, "music_bed": "music-bed"},
        "tracks": [
            {"id": "picture", "kind": "visual", "role": "output"},
            {"id": "music", "kind": "audio"},
        ],
    }
    assert [
        (row["occurrence_id"], row["shot_id"], row["shot_revision_id"],
         row["placement"], row["duration_ms"])
        for row in parent["occurrences"]
    ] == [
        ("occurrence-1", "shot-1", "film-shot-revision-1",
         {"start_ms": 0, "track": "picture"}, 1800),
        ("occurrence-2", "shot-2", "film-shot-revision-2",
         {"start_ms": 1650, "track": "picture"}, 1800),
        ("occurrence-3", "shot-3", "film-shot-revision-3",
         {"start_ms": 3300, "track": "picture"}, 1800),
    ]
    assert parent["registry"]["assets"]["music-bed"]["object_id"] == MEDIA[2]
    assert [
        (row["media_id"], row["content_digest"])
        for row in baseline_document["dependency_manifest"]["media"]
    ] == [(media_id, media_id) for media_id in MEDIA]
    assert baseline_document["dependency_manifest"]["documents"] == []

    shot = next(row for row in baseline_document["shot_revisions"] if row["shot_id"] == "shot-1")
    assert shot["payload"]["metadata"]["transcript_anchor"] == {
        "document_id": TRANSCRIPT,
        "segment_id": "segment-1",
        "word_id": "word-1",
    }
    assert shot["payload"]["text_bindings"] == [{
        "binding_id": BINDING,
        "byte_size": 17,
        "content_hash": TRANSCRIPT_MEDIA,
        "event_stream_id": BINDING + ":shot.text_binding",
        "head": 1,
        "kind": "transcript",
        "media_id": TRANSCRIPT_MEDIA,
        "mime_type": "text/plain",
        "project_id": PROJECT,
        "shot_id": "shot-1",
        "slot": None,
        "text": "Open on the river",
    }]

    # Read the later accepted word-bound head separately through its exact
    # authenticated pair, then compare the complete returned closure.
    advanced = store.fetch_bundle(
        PROJECT,
        "film",
        revision_id=ADVANCED_REVISION,
        publication_id=ADVANCED_PUBLICATION,
        client=client,
    )
    advanced_document = advanced["source_mapping"]["_runtime_document"]
    assert advanced["base_parent"]["revision_id"] == ADVANCED_REVISION
    assert advanced["base_parent"]["publication_id"] == ADVANCED_PUBLICATION
    assert advanced_document == service._timeline_commits()[0].revision_closure(
        PROJECT, "film", ADVANCED_REVISION)
    advanced_clips = advanced_document["parent_revision"]["payload"]["clips"]
    word_bound = next(clip for clip in advanced_clips if clip["id"] == "music-bed")
    assert word_bound["at"] == 0 and word_bound["hold"] == 5.5
    assert word_bound["on"] == "word:word-1"
    assert word_bound["until"] is None and word_bound["until_film_end"] is True
    assert word_bound["app"]["astrid"] == {
        "fixed_start": False,
        "for": 5.5,
        "on": "word:word-1",
        "opaque": [None, {"keep": True}],
        "until": None,
        "until_film_end": True,
    }
    assert next(clip for clip in advanced_clips if clip["id"] == "music-other") == {
        "asset": "music-bed",
        "at": 0,
        "clipType": "audio",
        "extension": {"opaque": [None, {"keep": True}]},
        "hold": 5.5,
        "id": "music-other",
        "track": "music-other",
    }
