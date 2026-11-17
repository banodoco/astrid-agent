"""The shared word projection: stored media transcripts mapped through clip occurrences."""
from __future__ import annotations

import pytest

from astrid.sdk.media_transcript import IMPORT_SOURCE, NARRATION_SOURCE, transcript_document_id
from astrid.sdk.timeline.words import (
    as_words,
    choose_document,
    default_source_order,
    project_words,
)

MEDIA = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
PROJECT = "project-1"


def _doc(media, words, *, source=IMPORT_SOURCE, version=3, segment="seg"):
    content = {"segments": [{"id": segment, "words": [
        {"id": f"word:{i}", "start": s, "end": e, "text": t} for i, (s, e, t) in enumerate(words)]}]}
    return {"document_id": transcript_document_id(PROJECT, media, source), "version": version,
            "content": content}


def _shot_clip(clip_id, *, track="vo", asset="a1", at=0.0, src_from=0.0, to=None, speed=1.0, duration=None, **extra):
    row = {"id": clip_id, "track": track, "asset": asset, "at": at, "from": src_from, "speed": speed}
    if to is not None:
        row["to"] = to
    if duration is not None:
        row["duration"] = duration
    row.update(extra)
    return row


def _bundle(*, placements, shots, parent_clips=()):
    shot_rows = {}
    for sid, (media, clips) in shots.items():
        internal = {"clips": clips, "tracks": [{"id": "vo", "kind": "vo"}],
                    "registry": {"assets": {"a1": {"media_id": media}}}}
        shot_rows[sid] = {"internal_timeline": internal}
    return {
        "project_id": PROJECT,
        "placements": [{"shot_id": sid, "occurrence_id": f"occ-{i}", "at_ms": at_ms, "duration_ms": duration_ms}
                       for i, (sid, at_ms, duration_ms) in enumerate(placements)],
        "parent": {"clips": list(parent_clips), "tracks": [{"id": "vo", "kind": "vo"}],
                   "registry": {"assets": {"a1": {"media_id": MEDIA}}}},
        "shots": shot_rows,
    }


def _texts(rows):
    return [row["text"] for row in rows]


WORDS = [(0.0, 0.5, "one"), (1.0, 1.5, "two"), (2.0, 2.5, "three"), (3.0, 3.5, "four")]


def test_default_source_order_puts_whisper_first_and_narration_last():
    order = default_source_order()
    assert order[0].startswith("astrid.transcription/")
    assert order[-1] == NARRATION_SOURCE
    assert IMPORT_SOURCE in order


def test_child_shot_scope_keeps_words_inside_the_clip_window_at_shot_time():
    clip = _shot_clip("c1", at=0.5, src_from=1.0, to=3.0)
    bundle = _bundle(placements=[("s1", 0, 4000)], shots={"s1": (MEDIA, [clip])})
    transcripts = {MEDIA: {IMPORT_SOURCE: _doc(MEDIA, WORDS)}}

    rows = project_words(bundle, transcripts, "shot:s1")

    # from <= t < to keeps "two" (1.0) and "three" (2.0); "four" starts at 3.0 (== to) and is dropped.
    assert _texts(rows) == ["two", "three"]
    assert rows[0]["timeline_start"] == pytest.approx(0.5)
    assert rows[1]["timeline_start"] == pytest.approx(1.5)
    assert rows[0]["word_id"] == "word:1"
    assert rows[0]["media_id"] == MEDIA
    assert rows[0]["source"] == IMPORT_SOURCE
    assert rows[0]["version"] == 3
    assert rows[0]["clip_id"] == "c1"
    assert rows[0]["shot_id"] == "s1"
    assert rows[0]["source_start"] == pytest.approx(1.0)


def test_repeated_placement_repeats_words_at_each_placement_clipped_to_its_length():
    clip = _shot_clip("c1", src_from=0.0, to=4.0)
    bundle = _bundle(placements=[("s1", 0, 2500), ("s1", 10000, 4000)],
                     shots={"s1": (MEDIA, [clip])})
    transcripts = {MEDIA: {IMPORT_SOURCE: _doc(MEDIA, WORDS)}}

    rows = project_words(bundle, transcripts, "parent")

    # First placement is 2.5 s long: "four" (3.0) falls outside it. Second placement starts at 10 s.
    first = [r for r in rows if r["occurrence"] == 0]
    second = [r for r in rows if r["occurrence"] == 1]
    assert _texts(first) == ["one", "two", "three"]
    assert _texts(second) == ["one", "two", "three", "four"]
    assert second[0]["timeline_start"] == pytest.approx(10.0)
    assert second[3]["timeline_start"] == pytest.approx(13.0)
    # The shot scope shows the shot's own view once, at owner start 0.
    own = project_words(bundle, transcripts, "shot:s1")
    assert [r["timeline_start"] for r in own][0] == pytest.approx(0.0)
    assert len(own) == 3


@pytest.mark.parametrize("speed, expected", [(0.5, [0.0, 2.0, 4.0, 6.0]), (2.0, [0.0, 0.5, 1.0, 1.5])])
def test_speed_scales_timeline_position(speed, expected):
    clip = _shot_clip("c1", src_from=0.0, to=4.0, speed=speed)
    bundle = _bundle(placements=[("s1", 0, 20000)], shots={"s1": (MEDIA, [clip])})
    transcripts = {MEDIA: {IMPORT_SOURCE: _doc(MEDIA, WORDS)}}

    rows = project_words(bundle, transcripts, "shot:s1")

    assert [round(r["timeline_start"], 6) for r in rows] == pytest.approx(expected)


def test_one_media_split_across_four_shots_maps_each_slice_once():
    # blue.wav style: one media, four clips, each covering one slice of the file.
    slices = [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 4.0)]
    shots, placements = {}, []
    for index, (lo, hi) in enumerate(slices):
        sid = f"s{index}"
        shots[sid] = (MEDIA, [_shot_clip(f"c{index}", src_from=lo, to=hi)])
        placements.append((sid, index * 1000, 1000))
    bundle = _bundle(placements=placements, shots=shots)
    transcripts = {MEDIA: {IMPORT_SOURCE: _doc(MEDIA, WORDS)}}

    rows = project_words(bundle, transcripts, "parent")

    assert _texts(rows) == ["one", "two", "three", "four"]
    assert [r["shot_id"] for r in rows] == ["s0", "s1", "s2", "s3"]
    assert [round(r["timeline_start"], 6) for r in rows] == pytest.approx([0.0, 1.0, 2.0, 3.0])


def test_pinned_reference_wins_over_source_order():
    whisper = default_source_order()[0]
    media_docs = {whisper: _doc(MEDIA, WORDS, source=whisper, version=1),
                  IMPORT_SOURCE: _doc(MEDIA, WORDS, source=IMPORT_SOURCE, version=2)}
    pinned = media_docs[IMPORT_SOURCE]["document_id"]

    source, doc = choose_document(media_docs, {pinned}, default_source_order())
    assert source == IMPORT_SOURCE and doc["version"] == 2

    source, _doc_row = choose_document(media_docs, set(), default_source_order())
    assert source == whisper


def test_clip_without_stored_transcript_falls_back_to_clip_words():
    clip = _shot_clip("c1", at=1.0, src_from=0.0, to=3.0,
                      app={"astrid": {"words": [[0.0, 0.5, "hello"], [1.0, 1.4, "there"]]}})
    bundle = _bundle(placements=[("s1", 2000, 4000)], shots={"s1": (MEDIA, [clip])})

    rows = project_words(bundle, {}, "parent")

    assert _texts(rows) == ["hello", "there"]
    assert rows[0]["source"] == "clip"
    assert rows[0]["word_id"] is None
    assert rows[0]["timeline_start"] == pytest.approx(3.0)
    assert rows[0]["address"] == "c1:0"


def test_line_segment_gives_line_addresses_and_stored_ids_are_kept():
    clip = _shot_clip("c1", src_from=0.0, to=4.0, app={"astrid": {"segment": "line-7"}})
    bundle = _bundle(placements=[("s1", 0, 4000)], shots={"s1": (MEDIA, [clip])})
    transcripts = {MEDIA: {IMPORT_SOURCE: _doc(MEDIA, WORDS)}}

    rows = project_words(bundle, transcripts, "shot:s1")
    words = as_words(rows)

    assert [w.id for w in words] == ["line-7:0", "line-7:1", "line-7:2", "line-7:3"]
    assert words[0].word_id == "word:0"
    assert words[0].segment == "line-7"
    assert words[0].index == 0


def test_parent_level_audio_clip_is_projected_at_parent_time():
    parent_clip = _shot_clip("pc", src_from=0.0, to=4.0, at=5.0)
    bundle = _bundle(placements=[], shots={}, parent_clips=[parent_clip])
    transcripts = {MEDIA: {IMPORT_SOURCE: _doc(MEDIA, WORDS)}}

    rows = project_words(bundle, transcripts, "parent")

    assert rows[0]["timeline_start"] == pytest.approx(5.0)
    assert rows[0]["shot_id"] == ""


def test_unknown_scope_is_refused():
    with pytest.raises(ValueError):
        project_words(_bundle(placements=[], shots={}), {}, "everything")


def test_other_media_words_do_not_leak_into_a_clip():
    clip = _shot_clip("c1", src_from=0.0, to=4.0)
    bundle = _bundle(placements=[("s1", 0, 4000)], shots={"s1": (OTHER, [clip])})
    transcripts = {MEDIA: {IMPORT_SOURCE: _doc(MEDIA, WORDS)}}

    assert project_words(bundle, transcripts, "parent") == []


class _Result:
    def __init__(self, data=None, error=None):
        self.ok = error is None
        self.data = data
        self.error = error


class _Documents:
    def __init__(self, docs):
        self.docs = docs

    def show(self, document_id, project=None):
        if document_id in self.docs:
            return _Result(self.docs[document_id])
        return _Result(error=type("E", (), {"code": "not_found"})())


class _Client:
    def __init__(self, docs):
        self.projects = type("P", (), {"show": lambda _self, name: _Result({"project_id": PROJECT})})()
        self.documents = _Documents(docs)


def test_checkout_word_list_uses_stored_transcript_and_keeps_clip_words_without_one():
    from astrid.sdk.timeline_checkout import Checkout

    clip = _shot_clip("c1", src_from=0.0, to=4.0)
    bundle = _bundle(placements=[("s1", 0, 4000)], shots={"s1": (MEDIA, [clip])})
    docs = {transcript_document_id(PROJECT, MEDIA, IMPORT_SOURCE): _doc(MEDIA, WORDS)}
    checkout = Checkout(bundle)
    checkout._scene_client = _Client(docs)
    checkout._scene_project = "minkhole"

    words = checkout._word_list()

    assert [w.text for w in words] == ["one", "two", "three", "four"]
    assert words[1].word_id == "word:1"
    assert words[1].version == 3
    assert words[1].source == IMPORT_SOURCE

    # A detached checkout (no client) still shows clip-local words for the same timeline.
    detached = Checkout(_bundle(placements=[("s1", 0, 4000)], shots={"s1": (MEDIA, [
        _shot_clip("c1", src_from=0.0, to=4.0, app={"astrid": {"words": [[0.0, 0.5, "solo"]]}})])}))
    assert [w.text for w in detached._word_list()] == ["solo"]
