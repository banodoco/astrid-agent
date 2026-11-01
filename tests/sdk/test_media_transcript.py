"""Shared transcript identity, CAS, and clip source clocks (fixture only)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.media_transcript import (
    NARRATION_SOURCE, build_transcript, read_transcript, transcript_document_id, write_transcript,
)
from astrid.sdk.timeline_transcripts import plan_narration_transcripts, sync_narration_transcripts


@pytest.mark.parametrize("project,media,source,expected", [
    ("demo", "sha256:" + "0" * 64, "astrid.narration", "transcript:b205b9e3d9acde28c68f00cdb391788edc0c4f821f11fcad8442faa11a7b1954"),
    ("proj-é", "sha256:" + "a" * 64, "whisper", "transcript:73aace11630d7517aa65704006ff9a03cf355d3aab9adaca8748c27bfcd79504"),
    ("映画", "sha256:" + "f" * 64, "źródło/🎤", "transcript:77902f848c82400e658d332289439e0995e5fb52ce410f1a3f425670274cf45e"),
])
def test_id_matches_runtime_function(project, media, source, expected):
    # Expected values computed with RuntimeService._transcript_document_id.
    assert transcript_document_id(project, media, source) == expected


class FakeDocuments:
    def __init__(self):
        self.row = None
        self.calls = []

    def show(self, document_id, *, project):
        self.calls.append(("show", document_id, project))
        return DomainResult.success(self.row) if self.row else DomainResult.failure(ErrorObject("not_found", "missing", {}))

    def create(self, *, kind, content, project, document_id):
        self.calls.append(("create", document_id, project))
        self.row = {"document_id": document_id, "kind": kind, "content": content, "version": 1}
        return DomainResult.success(self.row)

    def update(self, document_id, *, content, expected_version, project):
        self.calls.append(("update", expected_version, project))
        assert expected_version == self.row["version"]
        self.row = {**self.row, "content": content, "version": expected_version + 1}
        return DomainResult.success(self.row)


def fake_client():
    return SimpleNamespace(projects=SimpleNamespace(show=lambda project: DomainResult.success({"id": "canonical-project"})),
                           documents=FakeDocuments())


def test_write_create_unchanged_update_with_expected_version():
    client = fake_client()
    media = "sha256:" + "1" * 64
    content = build_transcript(media, "line-1", "Hello", [[0.1, 0.3, "Hello"]])
    assert write_transcript(client, "slug", media, NARRATION_SOURCE, content) == "created"
    assert read_transcript(client, "slug", media, NARRATION_SOURCE)["content"] == content
    assert write_transcript(client, "slug", media, NARRATION_SOURCE, content) == "unchanged"
    revised = build_transcript(media, "line-1", "Hello!", [[0.1, 0.3, "Hello!"]])
    assert write_transcript(client, "slug", media, NARRATION_SOURCE, revised) == "updated"
    assert ("update", 1, "canonical-project") in client.documents.calls
    assert sum(call[0] == "create" for call in client.documents.calls) == 1


def test_word_ids_survive_a_front_trim_of_the_same_recording():
    media = "sha256:" + "6" * 64
    complete = build_transcript(media, "line", "one two", [[0.1, 0.3, "one"], [0.5, 0.8, "two"]])
    trimmed = build_transcript(media, "line", "one two", [[0.5, 0.8, "two"]])
    assert complete["segments"][0]["words"][1]["id"] == trimmed["segments"][0]["words"][0]["id"]


class FakeTimeline:
    def __init__(self, clips, assets):
        self._clips = [SimpleNamespace(id=clip["id"], track=clip.get("track", "vo"), data=clip,
                                       shot_id="shot", asset=clip.get("asset")) for clip in clips]
        self.assets = assets

    def clips(self):
        return self._clips

    def _registry(self, shot_id):
        return self.assets


def test_backfill_converts_trimmed_local_words_to_media_seconds_and_deduplicates():
    media = "sha256:" + "2" * 64
    tl = FakeTimeline([
        {"id": "a", "asset": "take", "from": 1.0, "speed": 1.5,
         "app": {"astrid": {"segment": "line", "text": "one two", "words": [[0.1, 0.3, "one"]]} }},
        {"id": "b", "asset": "take", "from": 1.45, "speed": 1.5,
         "app": {"astrid": {"segment": "line", "text": "one two", "words": [[0.1, 0.3, "two"]]} }},
        {"id": "a-copy", "asset": "take", "from": 1.0, "speed": 1.5,
         "app": {"astrid": {"segment": "line", "text": "one two", "words": [[0.1, 0.3, "one"]]} }},
    ], {"take": {"media_id": media}})
    plan = plan_narration_transcripts(tl)
    assert len(plan) == 1 and plan[0]["status"] == "planned"
    assert [(w["start"], w["end"], w["text"]) for w in plan[0]["content"]["segments"][0]["words"]] == [
        (1.15, 1.45, "one"), (1.6, 1.9, "two")]
    client = fake_client()
    assert sync_narration_transcripts(tl, client, "slug", dry_run=True)[0]["status"] == "would create"
    assert client.documents.row is None
    assert sync_narration_transcripts(tl, client, "slug")[0]["status"] == "created"
    assert sync_narration_transcripts(tl, client, "slug", dry_run=True)[0]["status"] == "unchanged"


def test_backfill_reports_missing_sha_words_and_conflicting_media():
    media = "sha256:" + "3" * 64
    tl = FakeTimeline([
        {"id": "missing", "asset": "bad", "app": {"astrid": {"segment": "x", "words": [[0, 1, "x"]]}}},
        {"id": "empty", "asset": "take", "app": {"astrid": {"segment": "x", "words": []}}},
        {"id": "one", "asset": "take", "app": {"astrid": {"segment": "x", "words": [[0, 1, "x"]]}}},
        {"id": "other", "asset": "take", "app": {"astrid": {"segment": "y", "words": [[0, 1, "y"]]}}},
    ], {"bad": {}, "take": {"media_id": media}})
    reasons = [row["reason"] for row in plan_narration_transcripts(tl)]
    assert any("missing sha256" in reason for reason in reasons)
    assert "no words" in reasons
    assert any("conflicting duplicates" in reason for reason in reasons)


def test_documents_create_derives_transcript_id_by_default(monkeypatch):
    from astrid.sdk.documents import Documents

    documents = object.__new__(Documents)
    calls = []
    monkeypatch.setattr(Documents, "_project", lambda self, project: DomainResult.success("canonical-project"))
    monkeypatch.setattr(Documents, "_typed", lambda self, operation, *args, **kwargs: (
        calls.append((operation, args, kwargs)) or DomainResult.success({"document_id": args[1]})))
    media = "sha256:" + "4" * 64
    content = build_transcript(media, "line", "Hi", [[0, 0.2, "Hi"]])
    result = documents.create(kind="media.transcript", content=content, project="slug")
    assert result.ok
    assert calls[0][1][1] == transcript_document_id("canonical-project", media, NARRATION_SOURCE)


def test_timelines_transcripts_sync_parser():
    from astrid.packs.timeline.cli import build_parser

    args = build_parser(None).parse_args(["transcripts", "sync", "tl", "--project", "demo", "--dry-run"])
    assert (args.ref, args.project, args.dry_run) == ("tl", "demo", True)


def test_publish_registers_after_timeline_commit_and_survives_document_failure(monkeypatch, capsys):
    from astrid.sdk.timeline.checkout import Checkout
    from astrid.sdk.timeline import store
    from astrid.sdk import timeline_transcripts

    bundle = {"project_id": "p", "timeline_id": "t", "placements": [], "shots": {}, "parent": {"clips": []}}
    tl = Checkout(bundle)
    monkeypatch.setattr(tl, "_sync_film_length", lambda: [])
    monkeypatch.setattr(tl, "check", lambda: SimpleNamespace(valid=True))
    monkeypatch.setattr(tl, "edits", lambda: {"changes": ["take"]})
    monkeypatch.setattr(tl, "narration_changes", lambda: {})
    monkeypatch.setattr(tl, "stored_document", lambda **_kwargs: bundle)
    sequence = []
    monkeypatch.setattr(store, "publish_bundle", lambda *_args, **_kwargs: (
        sequence.append("publish") or {"new_head": "rev-1"}))
    monkeypatch.setattr(timeline_transcripts, "plan_narration_transcripts", lambda _tl: [
        {"status": "planned"}])

    def fail_sync(*_args, **_kwargs):
        sequence.append("transcript")
        raise RuntimeError("document unavailable")

    monkeypatch.setattr(timeline_transcripts, "sync_narration_transcripts", fail_sync)
    assert tl.publish(client=object())["new_head"] == "rev-1"
    assert sequence == ["publish", "transcript"]
    assert "document unavailable" in capsys.readouterr().err
