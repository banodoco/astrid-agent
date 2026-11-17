"""Producers write the one store: a replaced or inserted take keeps its words as its media's transcript,
and converting a timeline moves clip words into media transcripts safely."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk import timeline_transcripts as transcripts
from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.media_transcript import IMPORT_SOURCE, NARRATION_SOURCE, transcript_document_id
from astrid.sdk.timeline_checkout import Checkout

FPS = 30
PROJECT = "p"
VO = "sha256:" + "1" * 64
VO2 = "sha256:" + "5" * 64
TAKE = "sha256:" + "2" * 64
PIC = "sha256:" + "3" * 64


def _bundle():
    clips_a = [
        {"id": "a-plate", "clipType": "am-snap-plate", "track": "plate", "asset": "P", "at": 0.0, "hold": 4.0, "params": {}},
        {"id": "a-vo", "clipType": "media", "track": "vo", "asset": "VO", "at": 0.5, "from": 0.0, "to": 2.0,
         "app": {"segment": "s1", "text": "it went viral",
                 "words": [[0.1, 0.3, "it"], [0.4, 0.6, "went"], [0.8, 1.2, "viral"]]}},
    ]
    clips_b = [
        {"id": "b-plate", "clipType": "am-snap-plate", "track": "plate", "asset": "P", "at": 0.0, "hold": 4.0, "params": {}},
        {"id": "b-vo", "clipType": "media", "track": "vo", "asset": "VO2", "at": 1.9, "from": 0.0, "to": 1.0,
         "app": {"segment": "s2", "words": [[0.1, 0.4, "Live"], [0.5, 0.8, "now"]]}},
    ]
    tracks = [{"id": "plate", "kind": "visual"}, {"id": "vo", "kind": "audio"}]
    registry = {"assets": {"P": {"media_id": PIC}, "VO": {"media_id": VO}, "VO2": {"media_id": VO2}}}

    def shot(clips):
        return {"payload": {"name": "SHOT"},
                "internal_timeline": {"tracks": tracks, "clips": clips, "registry": copy.deepcopy(registry)}}

    return {
        "project_id": PROJECT, "timeline_id": "t", "base_parent": {"revision_id": "rev-0"},
        "parent": {"config": {"theme_overrides": {"visual": {"canvas": {"fps": FPS, "width": 1920, "height": 1080}}}},
                   "clips": []},
        "placements": [{"shot_id": "A", "occurrence_id": "occ-A", "placement": {"start_ms": 0}, "duration_ms": 4000},
                       {"shot_id": "B", "occurrence_id": "occ-B", "placement": {"start_ms": 4000}, "duration_ms": 4000}],
        "shots": {"A": shot(clips_a), "B": shot(clips_b)},
    }


TAKE_ENTRY = {"key": "TAKE", "media_id": TAKE, "content_sha256": TAKE, "type": "audio"}
NEW_WORDS = [[0.1, 0.5, "it"], [0.6, 1.0, "went"], [1.4, 2.4, "viral"]]


def test_replace_stores_the_take_words_as_its_media_transcript_not_on_the_clip():
    tl = Checkout(_bundle())
    tl.voice("s1").replace(TAKE_ENTRY, words=NEW_WORDS)

    clip = tl.clip("a-vo")
    assert intent.words(clip.data) == []                     # no timings on the clip
    assert intent.line(clip.data) == "s1"                    # line id, script text and media window stay
    assert intent.line_text(clip.data) == "it went viral"
    assert (clip.data["asset"], clip.data["from"]) == ("TAKE", 0.0)
    staged = tl.bundle["pending_transcripts"][TAKE][IMPORT_SOURCE]
    assert staged["source_id"] == IMPORT_SOURCE and staged["media_id"] == TAKE
    assert [w["text"] for w in staged["segments"][0]["words"]] == ["it", "went", "viral"]
    # readers still see the words, through the one projection, at the clip's place
    assert tl.word("viral").start == pytest.approx(0.5 + 1.4)
    assert tl.word("viral").source == IMPORT_SOURCE


def test_insert_line_stores_the_new_lines_words_as_its_media_transcript():
    tl = Checkout(_bundle())
    tl.insert_line("s1b", TAKE_ENTRY, words=[[0.0, 0.4, "and"], [0.5, 1.0, "then"]], after="s1", gap_after=0.3,
                   text="and then")

    clip = tl.voice("s1b").clips[0]
    assert intent.words(clip.data) == [] and intent.line_text(clip.data) == "and then"
    assert tl.bundle["pending_transcripts"][TAKE][IMPORT_SOURCE]["segments"][0]["id"] == "s1b"
    assert [w.text for w in tl.voice("s1b").words] == ["and", "then"]


def test_media_without_a_sha256_identity_keeps_words_on_the_clip():
    tl = Checkout(_bundle())
    tl.voice("s1").replace({"key": "ODD", "media_id": "m-odd"}, words=NEW_WORDS)
    assert "pending_transcripts" not in tl.bundle
    assert len(intent.words(tl.clip("a-vo").data)) == 3


class _Docs:
    def __init__(self):
        self.rows = {}
        self.writes = []

    def show(self, document_id, project=None):
        if document_id in self.rows:
            return DomainResult.success(self.rows[document_id])
        return DomainResult.failure(ErrorObject("not_found", "missing", {}))

    def create(self, *, kind, content, project, document_id):
        self.writes.append(("create", content["media_id"], content["source_id"]))
        self.rows[document_id] = {"document_id": document_id, "kind": kind, "content": content, "version": 1}
        return DomainResult.success(self.rows[document_id])

    def update(self, document_id, *, content, expected_version, project):
        self.writes.append(("update", content["media_id"], content["source_id"]))
        self.rows[document_id] = {**self.rows[document_id], "content": content, "version": expected_version + 1}
        return DomainResult.success(self.rows[document_id])


def _client():
    return SimpleNamespace(projects=SimpleNamespace(show=lambda project: DomainResult.success({"project_id": PROJECT})),
                           documents=_Docs())


def test_publish_writes_staged_transcripts_before_the_commit_and_a_failure_stops_it():
    tl = Checkout(_bundle())
    tl.voice("s1").replace(TAKE_ENTRY, words=NEW_WORDS)
    client = _client()
    written = tl._flush_pending_transcripts(client)
    assert written == [{"label": TAKE, "status": "created", "source": IMPORT_SOURCE}]
    assert client.documents.rows[transcript_document_id(PROJECT, TAKE, IMPORT_SOURCE)]["content"]["segments"][0]["id"] == "s1"
    assert tl._flush_pending_transcripts(client)[0]["status"] == "unchanged"

    broken = _client()
    broken.documents.create = lambda **_kw: DomainResult.failure(ErrorObject("boom", "no", {}))
    with pytest.raises(Exception, match="not publishing: could not store the transcript"):
        tl._flush_pending_transcripts(broken)


def test_staged_transcript_for_media_no_clip_plays_any_more_is_not_written():
    tl = Checkout(_bundle())
    tl.voice("s1").replace(TAKE_ENTRY, words=NEW_WORDS)
    tl.voice("s1").replace({"key": "T2", "media_id": "sha256:" + "4" * 64}, words=NEW_WORDS)
    client = _client()
    media = {row["label"] for row in tl._flush_pending_transcripts(client)}
    assert media == {"sha256:" + "4" * 64}


def test_staged_words_survive_a_saved_working_copy(tmp_path):
    tl = Checkout(_bundle())
    tl.voice("s1").replace(TAKE_ENTRY, words=NEW_WORDS)
    tl.save(tmp_path / "w.json", quiet=True)
    again = Checkout.load(tmp_path / "w.json")
    assert [w.text for w in again.voice("s1").words] == ["it", "went", "viral"]
    assert intent.words(again.clip("a-vo").data) == []


# ---- conversion -------------------------------------------------------------

class _Store:
    """The two functions conversion uses to talk to the store, kept in memory."""

    def __init__(self, monkeypatch):
        self.docs = {}
        monkeypatch.setattr(transcripts, "_read_transcript_result",
                            lambda client, project, media, source: (self.docs.get((media, source)), None))

        def write(client, project, media, source, content):
            self.docs[(media, source)] = {"content": content, "version": 1}
            return "created", None

        monkeypatch.setattr(transcripts, "_write_transcript_result", write)


def test_plan_conversion_keeps_projected_words_identical():
    tl = Checkout(_bundle())
    plan = transcripts.plan_conversion(tl)
    assert sorted((p["status"], p["media_id"]) for p in plan) == [("planned", VO), ("planned", VO2)]


def test_one_media_split_across_several_lines_is_left_on_its_clips(monkeypatch):
    store = _Store(monkeypatch)
    data = _bundle()
    data["shots"]["B"]["internal_timeline"]["clips"][1]["asset"] = "VO"  # the same file plays line s1 and line s2
    tl = Checkout(data)
    report = transcripts.convert_narration_transcripts(tl, object(), PROJECT)
    assert [r["status"] for r in report if r.get("media_id") != VO2 and r["label"] == VO] == ["skipped"]
    assert (VO, NARRATION_SOURCE) not in store.docs
    assert len(intent.words(tl.clip("a-vo").data)) == 3
    assert not transcripts.is_converted(tl.bundle)


def test_convert_writes_transcripts_then_removes_clip_words_and_marks_the_timeline(monkeypatch):
    store = _Store(monkeypatch)
    tl = Checkout(_bundle())
    before = [(w.id, w.text, w.start, w.end) for w in tl.words()]

    report = transcripts.convert_narration_transcripts(tl, object(), PROJECT)

    assert [r["status"] for r in report] == ["converted", "converted"]
    assert (VO, NARRATION_SOURCE) in store.docs and (VO2, NARRATION_SOURCE) in store.docs
    for clip_id in ("a-vo", "b-vo"):
        data = tl.clip(clip_id).data
        assert intent.words(data) == []
    assert intent.line(tl.clip("a-vo").data) == "s1" and intent.line_text(tl.clip("a-vo").data) == "it went viral"
    assert transcripts.is_converted(tl.bundle)
    # a reader now gets the same words, from the stored transcript
    tl._stored_transcripts = lambda: {media: {NARRATION_SOURCE: {"document_id": None, "version": 1,
                                                                 "content": store.docs[(media, NARRATION_SOURCE)]["content"]}}
                                      for media in (VO, VO2)}
    after = [(w.id, w.text, w.start, w.end) for w in tl.words()]
    assert after == before


def test_convert_dry_run_writes_and_changes_nothing(monkeypatch):
    store = _Store(monkeypatch)
    tl = Checkout(_bundle())
    snapshot = copy.deepcopy(tl.bundle)
    report = transcripts.convert_narration_transcripts(tl, object(), PROJECT, dry_run=True)
    assert all(r["status"].startswith("would convert") for r in report)
    assert store.docs == {} and tl.bundle == snapshot


def test_convert_leaves_a_clip_alone_when_its_transcript_cannot_be_stored(monkeypatch):
    _Store(monkeypatch)

    def boom(*_a):
        raise RuntimeError("store down")

    monkeypatch.setattr(transcripts, "_write_transcript_result", boom)
    tl = Checkout(_bundle())
    report = transcripts.convert_narration_transcripts(tl, object(), PROJECT)
    assert all(r["status"] == "skipped" and "store down" in r["reason"] for r in report)
    assert len(intent.words(tl.clip("a-vo").data)) == 3
    assert not transcripts.is_converted(tl.bundle)


def test_convert_skips_a_media_whose_stored_words_would_differ(monkeypatch):
    _Store(monkeypatch)
    data = _bundle()
    # a clip word past the end of its media window is shown from the clip today but not from the media transcript
    data["shots"]["A"]["internal_timeline"]["clips"][1]["app"]["words"].append([2.5, 2.9, "late"])
    tl = Checkout(data)
    report = transcripts.convert_narration_transcripts(tl, object(), PROJECT)
    assert sorted(r["status"] for r in report) == ["converted", "skipped"]
    assert len(intent.words(tl.clip("a-vo").data)) == 4
    assert not transcripts.is_converted(tl.bundle)


def test_publish_narration_sync_is_off_only_for_a_converted_timeline():
    assert not transcripts.is_converted(_bundle())
    converted = _bundle()
    converted["parent"]["config"]["bundle"] = {transcripts.CONVERTED_KEY: {"source": NARRATION_SOURCE}}
    assert transcripts.is_converted(converted)


def _publishable(monkeypatch, tl, order):
    monkeypatch.setattr(Checkout, "_check_core", lambda self: SimpleNamespace(valid=True, problems=[]))
    monkeypatch.setattr(Checkout, "narration_changes", lambda self: [])
    monkeypatch.setattr(Checkout, "_sync_film_length", lambda self: [])

    def commit(self, stored, message, *, idempotency_key, client):
        order.append(("commit", sorted(stored)))
        return {"new_head": "rev-1", "idempotency_key": "k"}

    monkeypatch.setattr(Checkout, "_commit_document", commit)
    monkeypatch.setattr(Checkout, "stored_document", lambda self, restructure=False: {**self.document(), "project_id": PROJECT})


def test_publish_stores_the_take_transcript_first_and_keeps_it_out_of_the_committed_document(monkeypatch):
    tl = Checkout(_bundle())
    tl.voice("s1").replace(TAKE_ENTRY, words=NEW_WORDS)
    order = []
    _publishable(monkeypatch, tl, order)
    client = _client()
    original = client.documents.create
    client.documents.create = lambda **kw: (order.append(("transcript", kw["content"]["source_id"])), original(**kw))[1]

    receipt = tl.publish("take", client=client)

    assert order[0] == ("transcript", IMPORT_SOURCE) and order[1][0] == "commit"
    assert "pending_transcripts" not in order[1][1]
    assert {"label": TAKE, "status": "created", "source": IMPORT_SOURCE} in receipt["transcripts"]


def test_a_failed_transcript_write_commits_nothing(monkeypatch):
    tl = Checkout(_bundle())
    tl.voice("s1").replace(TAKE_ENTRY, words=NEW_WORDS)
    order = []
    _publishable(monkeypatch, tl, order)
    client = _client()
    client.documents.create = lambda **kw: DomainResult.failure(ErrorObject("boom", "no", {}))
    with pytest.raises(Exception, match="not publishing"):
        tl.publish("take", client=client)
    assert order == []
