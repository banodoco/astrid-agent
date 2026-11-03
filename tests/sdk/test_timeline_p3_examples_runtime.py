"""Execute every Python example in the three Timeline extra skills on Runtime."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.sdk.test_timeline_editing_acceptance_runtime import (
    _sequential_births,
    _snapshot,
    _voice_file,
)

pytest_plugins = ("tests.sdk.test_timeline_editing_acceptance_runtime",)

PACK = Path(__file__).parents[2] / "astrid" / "packs" / "timeline"


@pytest.fixture
def p3_editing_case(tmp_path, monkeypatch, real_runtime):
    from tests.helpers.runtime import runtime_checkout, use_runtime

    runtime = runtime_checkout()
    monkeypatch.setenv("ASTRID_RUNTIME_CHECKOUT", str(runtime))
    use_runtime()

    daemon = real_runtime(authoring_profile="astrid")
    project = daemon.service.create_project({"slug": "p3-examples", "name": "P3 examples"})["id"]
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    client = open_client(daemon)
    created = client.timelines.create_empty(
        project=project,
        timeline_id="film",
        name="P3 film",
        idempotency_key="p3-examples-create-film",
    )
    assert created.ok, created.as_dict()
    from astrid.sdk.timeline import Timeline

    timeline = Timeline.open(project, "film", client=client, outbox_path=tmp_path / "p3-outbox.json")
    return daemon, client, project, timeline


def _python_examples(path: Path) -> list[str]:
    return re.findall(r"```python\s*\n(.*?)```", path.read_text(encoding="utf-8"), re.S)


def test_every_extra_python_example_runs_and_lands_on_scratch_runtime(
    p3_editing_case, tmp_path, monkeypatch,
):
    _daemon, client, project, timeline = p3_editing_case
    voice_file = _voice_file(tmp_path / "narration.wav", seconds=4)

    def seed(t):
        t.track("titles", kind="visual")
        t.track("Shots", kind="visual")
        t.add_text("Intro", at=0, hold=1, track="titles", id="intro")
        t.add_text("Badge", at=0, hold=0.2, track="titles", id="badge")
        t.add_text("Hit", at=0, hold=0.2, track="titles", id="hit")
        t.add_text("Source pin", at=0, hold=0.1, track="titles", id="source-pin")

    timeline.commit(seed, label="Seed P3 skill examples", key="p3-example-seed")

    from astrid.sdk.timeline.commit import bundle_from_document
    bundle = bundle_from_document(_snapshot(timeline))
    occurrences = _sequential_births(bundle, [
        dict(occurrence_id="occ-later", shot_id="shot-later", name="Later",
             track="Shots", on=4, duration=4),
        dict(occurrence_id="occ-earlier", shot_id="shot-earlier", name="Earlier",
             track="Shots", on=1, duration=2),
    ])
    timeline.commit(occurrences, label="Seed P3 shot examples", key="p3-example-shots")

    from astrid.sdk.timeline.capture import capture_track_create

    bundle = bundle_from_document(_snapshot(timeline))
    voice_track = capture_track_create(
        bundle, "occurrence:occ-later", {"id": "voice", "kind": "audio", "role": "output"}
    )
    timeline.commit([voice_track], label="Add narration track", key="p3-example-voice-track")

    from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_add_media

    later_placement = next(row for row in _snapshot(timeline)["parent_revision"]["payload"]["occurrences"]
                           if row["occurrence_id"] == "occ-later")
    later_shot_id = later_placement["shot_id"]
    transport = client._remote._transport
    imported = transport.import_project_media(
        project, voice_file.read_bytes(), media_type="audio/wav", filename="narration.wav",
        duration_seconds=4, idempotency_key="p3-example-audio-import",
    )
    imported = imported["data"]
    media = transport.get_project_media_selection(project, {
        "asset": imported["variant_id"], "generation_id": imported["generation_id"],
        "variant_id": imported["variant_id"],
    })
    verified = transport.get_project_verified_media(project, media)
    bundle = bundle_from_document(_snapshot(timeline))
    add_voice = capture_occurrence_add_media(
        bundle, "occurrence:occ-later", {
            "id": "narration", "clipType": "audio", "track": "voice", "asset": media["asset"],
            "at": 0, "from": 0, "to": 4, "hold": 4, "speed": 1,
        }, media, verified_media=[verified],
    )
    timeline.commit([add_voice], label="Add narration media", key="p3-example-narration-media")
    def annotate_narration(t):
        t.clip("narration").data["app"] = {"astrid": {
            "segment": "narration",
            "text": "Astrid speaks",
            "words": [[0.5, 0.8, "Astrid"], [1.0, 1.3, "speaks"]],
            "beats": {"beats": [0.5, 1.0, 1.5, 2.0, 2.5, 3.0], "time": "cue_seconds"},
        }}

    timeline.commit(annotate_narration, label="Annotate narration", key="p3-example-narration-words")

    source_bundle = bundle_from_document(_snapshot(timeline))
    source_clip = next(
        clip for clip in source_bundle["shots"][later_shot_id]["internal_timeline"]["clips"]
        if clip["id"] == "narration"
    )
    media_id = source_bundle["shots"][later_shot_id]["internal_timeline"]["registry"]["assets"][source_clip["asset"]]["media_id"]
    from astrid.sdk.media_transcript import build_transcript, transcript_document_id

    transcript = build_transcript(media_id, "narration", "Astrid speaks",
                                  [[0.5, 0.8, "Astrid"], [1.0, 1.3, "speaks"]])
    document_id = transcript_document_id(project, media_id, transcript["source_id"])
    created = client.documents.create(document_id=document_id, kind="media.transcript",
                                      content=transcript, project=project)
    assert created.ok, created.as_dict()
    def link_transcript(t):
        bundle_config = t.bundle["parent"]["config"].setdefault("bundle", {})
        bundle_config["transcriptReferences"] = {
            "schema": "media.transcript.references/v1",
            "associations": [{"assetId": media["asset"], "documentId": document_id}],
        }

    timeline.commit(link_transcript, label="Link narration transcript", key="p3-example-transcript-link")

    from runtime_protocol.timeline_commit_inputs import content_digest
    from astrid.sdk.timeline.api import Timeline
    from astrid.sdk.timeline.native_word_anchor import capture_anchor

    transcript_document = _daemon.service.get_document(project, document_id)
    transcript_pin = {"document_id": document_id, "version": transcript_document["version"],
                      "content_digest": content_digest(transcript_document["content"])}
    source_bundle = bundle_from_document(_snapshot(timeline))
    source_bundle["source_mapping"]["pinned_documents"] = [
        {"pin": transcript_pin, "document": transcript_document}
    ]
    seed_pin = capture_anchor(
        source_bundle, "parent/clip:source-pin", "on", '"Astrid"', {
            "source": "occurrence:occ-later/clip:narration", "document": transcript_pin,
            "word_ids": [transcript["segments"][0]["words"][0]["id"]],
            "edge": "start", "offset_s": 0, "offset_frames": 0,
        },
    )
    timeline.commit([seed_pin], label="Pin narrated source", key="p3-example-pin-transcript")

    from astrid.sdk.timeline import store

    original_fetch_bundle = store.fetch_bundle
    fresh_bundle = original_fetch_bundle(project, "film", client=client)
    saved_anchor = next(
        clip["app"]["astrid"]["word_anchors"]["on"]
        for clip in fresh_bundle["parent"]["clips"] if clip["id"] == "source-pin"
    )
    assert saved_anchor["document"] == transcript_pin

    def fetch_fixture_inputs(*args, **kwargs):
        bundle = original_fetch_bundle(*args, **kwargs)
        from astrid.sdk.timeline.native_source_inputs import read_verified_transcript

        captures = bundle.setdefault("source_mapping", {}).setdefault("pinned_documents", [])
        pins = [
            choice["document"]
            for clip in bundle["parent"]["clips"]
            for choice in (clip.get("app", {}).get("astrid", {}).get("word_anchors", {}) or {}).values()
        ]
        for pin in pins:
            capture = read_verified_transcript(client._remote._transport, project, pin)
            if not any(row.get("pin") == pin for row in captures):
                captures.append(capture)
        return bundle

    monkeypatch.setattr(store, "fetch_bundle", fetch_fixture_inputs)

    original_open = Timeline.open.__func__

    def open_on_case_runtime(cls, *args, **kwargs):
        kwargs.setdefault("client", client)
        return original_open(cls, *args, **kwargs)

    monkeypatch.setattr(Timeline, "open", classmethod(open_on_case_runtime))

    skill_paths = [
        PACK / "docs" / "series" / "SKILL.md",
        PACK / "docs" / "narrated" / "SKILL.md",
        PACK / "docs" / "review" / "SKILL.md",
    ]
    examples = [example for path in skill_paths for example in _python_examples(path)]
    assert len(examples) == 4

    namespace = {"project": project, "timeline": "film", "episodes": ["film"]}
    for example in examples:
        exec(compile(example, "timeline extra skill example", "exec"), namespace)

    landed = Timeline.open(project, "film", client=client)
    clips = {row["id"]: row for row in landed.bundle["parent"]["clips"]}
    words = list(landed.words())
    astrid_word = next(word for word in words if word.text == "Astrid")
    assert clips["badge"]["at"] == pytest.approx(astrid_word.end)
    assert clips["hit"]["at"] == pytest.approx(3.7)
    assert clips["outro"]["hold"] == pytest.approx(2.0)
    assert clips["intro"]["at"] == pytest.approx(1.0)

    placements = {row["occurrence_id"]: row for row in landed.bundle["placements"]}
    assert placements["occ-earlier"]["placement"]["start_ms"] == 0
    assert placements["occ-later"]["placement"]["start_ms"] == 2200


def test_shots_example_authors_cuts_restructures_and_reuses_stored_shot(
    p3_editing_case, tmp_path, monkeypatch,
):
    _daemon, client, project, timeline = p3_editing_case
    created = client.timelines.create_empty(
        project=project,
        timeline_id="shots",
        name="Shots example",
        idempotency_key="p3-shots-create-film",
    )
    assert created.ok, created.as_dict()

    from astrid.sdk.timeline import Checkout, Timeline

    timeline = Timeline.open(project, "shots", client=client, outbox_path=tmp_path / "p3-shots-outbox.json")

    original_open = Timeline.open.__func__

    def open_on_case_runtime(cls, *args, **kwargs):
        kwargs.setdefault("client", client)
        kwargs.setdefault("outbox_path", tmp_path / "p3-shots-outbox.json")
        return original_open(cls, *args, **kwargs)

    monkeypatch.setattr(Timeline, "open", classmethod(open_on_case_runtime))
    original_checkout_open = Checkout.open.__func__

    def checkout_on_case_runtime(cls, *args, **kwargs):
        kwargs.setdefault("client", client)
        return original_checkout_open(cls, *args, **kwargs)

    monkeypatch.setattr(Checkout, "open", classmethod(checkout_on_case_runtime))
    from tests.sdk.test_timeline_editing_acceptance_runtime import _media_file

    picture = _media_file(tmp_path / "shots.png")
    from astrid.sdk.timeline.capture import capture_track_create
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
    from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_add_media
    from astrid.sdk.timeline.commit import bundle_from_document

    bundle = bundle_from_document(_snapshot(timeline))
    parent_track = capture_track_create(bundle, "parent", {"id": "picture", "kind": "visual"})
    timeline.commit([parent_track], label="Prepare picture track", key="p3-shots-picture-track")
    timeline = Timeline.open(project, "shots", client=client, outbox_path=tmp_path / "p3-shots-outbox.json")
    bundle = bundle_from_document(_snapshot(timeline))
    birth = capture_occurrence_create(
        bundle, "opening-source", "opening-source-shot", name="Opening source",
        track="picture", on=0, duration=4,
    )
    timeline.commit([birth], label="Create source shot", key="p3-shots-source-shot")
    timeline = Timeline.open(project, "shots", client=client, outbox_path=tmp_path / "p3-shots-outbox.json")
    bundle = bundle_from_document(_snapshot(timeline))
    track = capture_track_create(
        bundle, "occurrence:opening-source", {"id": "plate", "kind": "visual"}
    )
    timeline.commit([track], label="Create shot picture track", key="p3-shots-shot-picture-track")
    timeline = Timeline.open(project, "shots", client=client, outbox_path=tmp_path / "p3-shots-outbox.json")
    transport = client._remote._transport
    imported = transport.import_project_media(
        project, picture.read_bytes(), media_type="image/png", filename=picture.name,
        idempotency_key="p3-shots-picture-import",
    )["data"]
    media = transport.get_project_media_selection(project, {
        "asset": imported["variant_id"], "generation_id": imported["generation_id"],
        "variant_id": imported["variant_id"],
    })
    verified = transport.get_project_verified_media(project, media)
    bundle = bundle_from_document(_snapshot(timeline))
    seed = capture_occurrence_add_media(bundle, "occurrence:opening-source", {
        "id": "opening-seed", "clipType": "image", "track": "plate",
        "asset": media["asset"], "at": 0, "hold": 4,
    }, media, verified_media=[verified])
    timeline.commit([seed], label="Add source picture", key="p3-shots-source-picture")
    checkout = Checkout.open(project, "shots", client=client)
    checkout.clip("opening-seed").remove()
    picture_clip = checkout.add(
        "media", at=0, hold=4, asset=media["asset"], track="plate", cut="c1", layer="picture",
    )
    second_cut = checkout.add_cut(2, after="c1", id="c2")
    picture_clip.hold_for(2)
    second_cut.picture.on(None)
    checkout.publish("Author picture cuts", idempotency_key="p3-shots-author-picture-cuts", client=client)

    example = _python_examples(PACK / "docs" / "shots" / "SKILL.md")[0]
    namespace = {"project": project, "timeline": "shots", "client": client}
    exec(compile(example, "timeline shots skill example", "exec"), namespace)

    reopened = Timeline.open(project, "shots", client=client, outbox_path=tmp_path / "p3-shots-verify.json")
    rows = sorted(reopened.bundle["placements"], key=lambda row: row["placement"]["start_ms"])
    assert len(rows) == 3
    assert [(row["occurrence_id"], row["placement"]["start_ms"]) for row in rows] == [
        (namespace["source"]["occurrence_id"], 0),
        ("opening-repeat", 2500),
        (namespace["other"]["occurrence_id"], 5000),
    ]
    assert all(row["shot_id"] for row in rows)
    canonical = {
        row["occurrence_id"]: row
        for row in reopened._runtime_document["parent_revision"]["payload"]["occurrences"]
    }
    original = canonical[namespace["source"]["occurrence_id"]]
    repeat = canonical["opening-repeat"]
    assert original["shot_id"] == repeat["shot_id"]
    assert original["shot_revision_id"] == repeat["shot_revision_id"]
    assert canonical[rows[0]["occurrence_id"]]["shot_revision_id"] != canonical[rows[2]["occurrence_id"]]["shot_revision_id"]
