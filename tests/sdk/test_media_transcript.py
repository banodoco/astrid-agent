"""Shared transcript identity, CAS, and clip source clocks (fixture only)."""
from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.media_transcript import (
    NARRATION_SOURCE,
    build_transcript,
    read_transcript,
    transcript_document_id,
    write_transcript,
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


def test_import_words_uses_source_seconds_and_refuses_unpinned_replacement():
    from astrid.sdk.media_transcript import import_transcript_words

    client = fake_client()
    media = "sha256:" + "5" * 64
    words = [[0.1, 0.3, "hello"], {"start_s": 0.4, "end_s": 0.6, "word": "there"}]
    assert import_transcript_words(client, "slug", media, words, segment_id="line") == "created"
    stored = client.documents.row["content"]
    assert [(word["start"], word["end"]) for word in stored["segments"][0]["words"]] == [
        (0.1, 0.3), (0.4, 0.6)]
    assert stored["segments"][0]["start"] == 0.1
    assert stored["source_id"] == "astrid.import"

    with pytest.raises(ValueError, match="whole-document replacement"):
        import_transcript_words(client, "slug", media, [[0.0, 0.2, "replacement"]],
                                segment_id="line")
    assert client.documents.row["content"] == stored

    assert import_transcript_words(client, "slug", media, words, segment_id="line") == "unchanged"
    with pytest.raises(ValueError, match="expected version"):
        import_transcript_words(client, "slug", media, [[0.0, 0.2, "replacement"]],
                                segment_id="line", replace=True)
    assert import_transcript_words(client, "slug", media, [[0.0, 0.2, "replacement"]],
                                   segment_id="line", replace=True,
                                   expected_version=1) == "updated"
    assert client.documents.row["content"]["segments"][0]["words"][0]["start"] == 0.0


def test_import_replacement_uses_the_version_it_checked_when_a_race_occurs():
    from astrid.sdk.media_transcript import import_transcript_words

    class RacingDocuments(FakeDocuments):
        def update(self, document_id, *, content, expected_version, project):
            self.calls.append(("update", expected_version, project))
            self.row = {**self.row, "content": {"concurrent": True}, "version": expected_version + 1}
            return DomainResult.failure(ErrorObject("stale_version", "stale", {}))

    client = fake_client()
    client.documents = RacingDocuments()
    media = "sha256:" + "c" * 64
    original = build_transcript(media, "line", "old", [[0.0, 0.2, "old"]])
    write_transcript(client, "slug", media, NARRATION_SOURCE, original)
    with pytest.raises(RuntimeError, match="cannot import transcript"):
        import_transcript_words(client, "slug", media, [[0.0, 0.2, "new"]], segment_id="line",
                                replace=True, expected_version=1)
    assert ("update", 1, "canonical-project") in client.documents.calls
    assert client.documents.row["content"] == {"concurrent": True}


@pytest.mark.parametrize("words", [
    [[float("nan"), 0.2, "bad"]], [[0.4, 0.2, "bad"]], [[0, 0.1, "  "]], [[-0.1, 0.1, "bad"]],
])
def test_import_rejects_invalid_word_rows(words):
    from astrid.sdk.media_transcript import import_transcript_words

    with pytest.raises(ValueError, match="word"):
        import_transcript_words(fake_client(), "slug", "sha256:" + "d" * 64, words,
                                segment_id="line")


@pytest.mark.parametrize("words", [
    [[0.0, 0.1, None]], [[0.0, 0.1, 3]], [[0.0, 0.1, ""]], [[0.0, 0.1, "  "]],
    [[True, 0.1, "word"]], [[0.0, False, "word"]],
])
def test_import_rejects_missing_or_invalid_word_text_and_boolean_times(words):
    from astrid.sdk.media_transcript import import_transcript_words

    with pytest.raises(ValueError, match="word"):
        import_transcript_words(fake_client(), "slug", "sha256:" + "e" * 64, words,
                                segment_id="line")


def test_transcripts_import_parser_requires_media_identity_and_file():
    from astrid.core.cli.domain_timelines import build_parser

    media = "sha256:" + "a" * 64
    args = build_parser(None).parse_args([
        "transcripts", "import", media, "words.json", "--project", "demo",
        "--replace", "--expected-version", "3",
    ])
    assert (args.media_id, args.file, args.project,
            args.replace, args.expected_version) == (media, Path("words.json"), "demo", True, 3)
    assert args.source == "astrid.import"


def test_transcripts_show_parser_accepts_source_range_and_clip_projection():
    from astrid.core.cli.domain_timelines import build_parser

    media = "sha256:" + "b" * 64
    args = build_parser(None).parse_args([
        "transcripts", "show", media, "--project", "demo", "--range", "10..12",
        "--timeline", "rough-cut", "--clip", "clip-a", "--json",
    ])
    assert (args.media_id, args.source, args.source_range, args.timeline, args.clip, args.json) == (
        media, None, "10..12", "rough-cut", "clip-a", True)
    clip_args = build_parser(None).parse_args([
        "transcripts", "show", "--project", "demo", "--timeline", "rough-cut", "--clip", "clip-a",
    ])
    assert clip_args.media_id is None
    assert clip_args.clip == "clip-a"


def test_transcript_word_mapping_keeps_source_clock_and_maps_each_overlapping_occurrence():
    from astrid.sdk.timeline_transcript_show import map_transcript_words

    transcript = {"segments": [{"words": [
        {"id": "w1", "start": 9.5, "end": 10.5, "text": "hello"},
        {"id": "w2", "start": 10.5, "end": 11.0, "text": "again"},
        {"id": "w3", "start": 12.0, "end": 12.5, "text": "outside"},
    ]}]}
    clips = [
        {"clip_id": "fast", "source_start": 10.0, "source_end": 11.0,
         "timeline_start": 100.0, "speed": 2.0},
        {"clip_id": "repeat", "source_start": 9.0, "source_end": 11.0,
         "timeline_start": 200.0, "speed": 0.5},
    ]
    rows = map_transcript_words(transcript, source_range=(9.0, 11.0), clips=clips)
    assert [(row["word_id"], row["clip_id"]) for row in rows] == [
        ("w1", "fast"), ("w1", "repeat"), ("w2", "fast"), ("w2", "repeat")]
    assert rows[0] == {"word_id": "w1", "text": "hello", "source_start": 9.5,
                       "source_end": 10.5, "clip_id": "fast",
                       "timeline_start": 99.75, "timeline_end": 100.25}
    assert rows[1]["timeline_start"] == 201.0
    assert rows[1]["timeline_end"] == 203.0


def test_transcript_word_mapping_without_clip_selection_preserves_source_clock_only():
    from astrid.sdk.timeline_transcript_show import map_transcript_words

    transcript = {"segments": [{"words": [
        {"id": "w1", "start": 1.25, "end": 1.75, "text": "source"},
    ]}]}
    assert map_transcript_words(transcript) == [{
        "word_id": "w1", "text": "source", "source_start": 1.25, "source_end": 1.75,
    }]


def test_transcripts_show_handler_reads_media_and_filters_source_range(monkeypatch, capsys):
    from types import SimpleNamespace
    from astrid.packs.timeline import cli_working
    from astrid.sdk import media_transcript

    media = "sha256:" + "c" * 64
    content = build_transcript(media, "line", "one two", [[1.0, 1.3, "one"], [2.0, 2.4, "two"]])
    seen = []
    monkeypatch.setattr(media_transcript, "read_transcript",
                        lambda _client, project, media_id, source: (seen.append((project, media_id, source)) or
                                                                    {"content": content}))
    args = SimpleNamespace(client=object(), project="demo", media_id=media, source=None,
                           source_range="1.5..2.5", clip=None, timeline=None, json=False)
    assert cli_working._cmd_transcripts_show(args) == 0
    output = capsys.readouterr().out
    assert "source 2.000–2.400s  two" in output
    assert "one" not in output
    assert seen == [("demo", media, "astrid.transcription/006a709848259ed3d69e2f9fcfd7a75f6c860ddd/large-v3-turbo.pt")]


def test_transcripts_show_handler_infers_clip_media_and_maps_repeated_checkout_occurrences(monkeypatch, capsys):
    from types import SimpleNamespace
    from astrid.packs.timeline import cli_working
    from astrid.sdk import media_transcript
    from astrid.sdk.timeline_checkout import Checkout
    from astrid.sdk.whisper_transcript import whisper_source_id

    media = "sha256:" + "d" * 64
    content = build_transcript(media, "line", "spoken", [[10.0, 10.5, "spoken"]],
                               source_id=whisper_source_id("large-v3-turbo.pt"))
    rows = []
    monkeypatch.setattr(media_transcript, "read_transcript",
                        lambda _client, project, media_id, source: (rows.append((project, media_id, source)) or
                                                                    {"content": content}))
    def clip(clip_id, at, source_in, source_out, speed):
        return SimpleNamespace(id=clip_id, shot_id="shot-1", asset="asset-1", start=at, duration=(source_out-source_in)/speed,
                               data={"from": source_in, "to": source_out, "speed": speed})
    timeline = SimpleNamespace(
        bundle={"shots": {"shot-1": {"internal_timeline": {"registry": {"assets": {
            "asset-1": {"media_id": media, "type": "audio/wav"},
        }}}}}},
        clips=lambda: [clip("fast", 100.0, 10.0, 11.0, 2.0), clip("repeat", 200.0, 9.0, 11.0, 0.5)],
    )
    monkeypatch.setattr(Checkout, "open", classmethod(lambda cls, project, ref, client: timeline))
    args = SimpleNamespace(client=object(), project="demo", media_id=None, source=None,
                           source_range=None, clip=None, timeline="rough-cut", json=False)
    assert cli_working._cmd_transcripts_show(args) == 1
    assert "provide MEDIA_ID or select a timeline --clip" in capsys.readouterr().err
    args.media_id = media
    assert cli_working._cmd_transcripts_show(args) == 0
    output = capsys.readouterr().out
    assert "source 10.000–10.500s  spoken  timeline 100.000–100.250s [fast]" in output
    assert "source 10.000–10.500s  spoken  timeline 202.000–203.000s [repeat]" in output
    assert rows == [("demo", media, whisper_source_id("large-v3-turbo.pt"))]

    args.media_id = None
    args.clip = "fast"
    assert cli_working._cmd_transcripts_show(args) == 0
    selected = capsys.readouterr().out
    assert "[fast]" in selected
    assert "[repeat]" not in selected


def test_sync_preserves_the_exact_server_pin_from_create_result_without_rereading():
    from astrid.sdk import media_transcript

    media = "sha256:" + "7" * 64
    content = build_transcript(media, "line", "Hello", [[0.1, 0.3, "Hello"]])
    mutation = {
        "document_id": transcript_document_id("canonical-project", media, NARRATION_SOURCE),
        "kind": "media.transcript",
        "content": content,
        "version": 7,
        "content_digest": "sha256:" + "7" * 64,
    }

    class Documents(FakeDocuments):
        def create(self, *, kind, content, project, document_id):
            self.calls.append(("create", document_id, project))
            # A later GET is deliberately a different version. The mutation
            # result is the only accepted source for this dormant metadata.
            self.row = {**mutation, "content": content, "version": 8,
                        "content_digest": "sha256:" + "8" * 64}
            return DomainResult.success(mutation)

    client = SimpleNamespace(
        projects=SimpleNamespace(show=lambda project: DomainResult.success({"id": "canonical-project"})),
        documents=Documents(),
    )
    tl = FakeTimeline([{
        "id": "clip", "asset": "take", "from": 0, "speed": 1,
        "app": {"astrid": {"segment": "line", "text": "Hello", "words": [[0.1, 0.3, "Hello"]]}},
    }], {"take": {"media_id": media}})

    report = sync_narration_transcripts(tl, client, "slug")
    assert report[0]["status"] == "created"
    assert report[0]["pin"] == {
        "document_id": mutation["document_id"],
        "version": 7,
        "content_digest": mutation["content_digest"],
    }
    assert [call[0] for call in client.documents.calls] == ["show", "create"]
    assert media_transcript.write_transcript(client, "slug", media, NARRATION_SOURCE, content) == "unchanged"
    dry_run = sync_narration_transcripts(tl, client, "slug", dry_run=True)
    assert dry_run[0]["pin"] == {
        "document_id": mutation["document_id"],
        "version": 8,
        "content_digest": "sha256:" + "8" * 64,
    }


def test_sync_keeps_legacy_success_when_server_pin_is_missing_or_malformed():
    media = "sha256:" + "8" * 64
    content = build_transcript(media, "line", "Hello", [[0.1, 0.3, "Hello"]])
    tl = FakeTimeline([{
        "id": "clip", "asset": "take", "from": 0, "speed": 1,
        "app": {"astrid": {"segment": "line", "text": "Hello", "words": [[0.1, 0.3, "Hello"]]}},
    }], {"take": {"media_id": media}})

    for digest in (None, "not-a-digest", "sha256:" + "A" * 64):
        documents = FakeDocuments()
        documents.row = {
            "document_id": transcript_document_id("canonical-project", media, NARRATION_SOURCE),
            "kind": "media.transcript", "content": content, "version": 3,
            **({} if digest is None else {"content_digest": digest}),
        }
        client = SimpleNamespace(
            projects=SimpleNamespace(show=lambda project: DomainResult.success({"id": "canonical-project"})),
            documents=documents,
        )
        report = sync_narration_transcripts(tl, client, "slug", dry_run=True)
        assert report[0]["status"] == "unchanged"
        assert "pin" not in report[0]


def test_sync_preserves_the_exact_server_pin_from_update_result():
    media = "sha256:" + "9" * 64
    content = build_transcript(media, "line", "Hello", [[0.1, 0.3, "Hello"]])
    current = {"document_id": transcript_document_id("canonical-project", media, NARRATION_SOURCE),
               "kind": "media.transcript", "content": {"old": True}, "version": 3}
    mutation = {**current, "content": content, "version": 4, "content_digest": "sha256:" + "4" * 64}

    class Documents(FakeDocuments):
        def update(self, document_id, *, content, expected_version, project):
            self.calls.append(("update", expected_version, project))
            self.row = {**mutation, "version": 5, "content_digest": "sha256:" + "5" * 64}
            return DomainResult.success(mutation)

    client = SimpleNamespace(
        projects=SimpleNamespace(show=lambda project: DomainResult.success({"id": "canonical-project"})),
        documents=Documents(),
    )
    client.documents.row = current
    tl = FakeTimeline([{
        "id": "clip", "asset": "take", "from": 0, "speed": 1,
        "app": {"astrid": {"segment": "line", "text": "Hello", "words": [[0.1, 0.3, "Hello"]]}},
    }], {"take": {"media_id": media}})

    report = sync_narration_transcripts(tl, client, "slug")
    assert report[0]["status"] == "updated"
    assert report[0]["pin"] == {
        "document_id": mutation["document_id"],
        "version": 4,
        "content_digest": "sha256:" + "4" * 64,
    }
    assert [call[0] for call in client.documents.calls] == ["show", "update"]


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
    from astrid.sdk import timeline_transcripts
    from astrid.sdk.timeline.checkout import Checkout

    bundle = {"project_id": "p", "timeline_id": "t", "placements": [], "shots": {}, "parent": {"clips": []}}
    tl = Checkout(bundle)
    monkeypatch.setattr(tl, "_sync_film_length", lambda: [])
    monkeypatch.setattr(tl, "_check_core", lambda: SimpleNamespace(valid=True))
    monkeypatch.setattr(tl, "edits", lambda: {"changes": ["take"]})
    monkeypatch.setattr(tl, "narration_changes", lambda: {})
    monkeypatch.setattr(tl, "stored_document", lambda **_kwargs: bundle)
    sequence = []
    monkeypatch.setattr(tl, "_publish_narration", lambda *a, **kw: {})
    monkeypatch.setattr(tl, "_commit_document", lambda *_args, **_kwargs: (
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


def test_transcript_show_handles_parent_and_repeated_shot_occurrences(monkeypatch, capsys):
    """Use real checkout clip handles, including an assetless effect and a repeated shot."""
    from astrid.packs.timeline import cli_working
    from astrid.sdk import media_transcript
    from astrid.sdk.timeline_checkout import Checkout

    media = 'sha256:' + '9' * 64
    content = build_transcript(media, 'line', 'hello', [[1.0, 1.5, 'hello']])
    monkeypatch.setattr(media_transcript, 'read_transcript',
                        lambda client, project, media_id, source: {'content': content})
    bundle = {
        'project_id': 'p', 'timeline_id': 't',
        'parent': {'config': {'registry': {'assets': {'audio': {'media_id': media}}}},
                   'clips': [{'id': 'parent-audio', 'clipType': 'media', 'asset': 'audio',
                              'track': 'music', 'at': 30, 'from': 0, 'to': 2},
                             {'id': 'text', 'clipType': 'am-type', 'track': 'visual',
                              'at': 0, 'hold': 2}]},
        'placements': [{'shot_id': 'shot', 'occurrence_id': 'one', 'placement': {'start_ms': 0}, 'duration_ms': 4000},
                       {'shot_id': 'shot', 'occurrence_id': 'two', 'placement': {'start_ms': 10000}, 'duration_ms': 4000}],
        'shots': {'shot': {'internal_timeline': {
            'tracks': [], 'registry': {'assets': {'audio': {'media_id': media}}},
            'clips': [{'id': 'shot-audio', 'clipType': 'media', 'track': 'audio', 'asset': 'audio',
                       'at': 0.5, 'from': 0, 'to': 2, 'speed': 2},
                      {'id': 'effect', 'clipType': 'am-type', 'track': 'visual', 'at': 0, 'hold': 1}]}}},
    }
    timeline = Checkout(bundle)
    monkeypatch.setattr(Checkout, 'open', classmethod(lambda cls, project, ref, client: timeline))
    args = SimpleNamespace(client=object(), project='p', media_id=media, source=None,
                           source_range=None, clip=None, timeline='cut', json=False)
    assert cli_working._cmd_transcripts_show(args) == 0
    output = capsys.readouterr().out
    assert 'timeline 1.000–1.250s [shot-audio]' in output
    assert 'timeline 11.000–11.250s [shot-audio]' in output
    assert 'timeline 31.000–31.500s [parent-audio]' in output
    assert output.count('[shot-audio]') == 2
    args.media_id, args.clip = None, 'parent-audio'
    assert cli_working._cmd_transcripts_show(args) == 0
    assert 'timeline 31.000–31.500s [parent-audio]' in capsys.readouterr().out


def test_transcript_show_rejects_ambiguous_clip_media_and_respects_shot_edge(monkeypatch, capsys):
    from astrid.packs.timeline import cli_working
    from astrid.sdk import media_transcript
    from astrid.sdk.timeline_checkout import Checkout

    first, second = 'sha256:' + '1' * 64, 'sha256:' + '2' * 64
    content = build_transcript(first, 'line', 'in out', [[1, 1.5, 'in'], [3, 3.5, 'out']])
    monkeypatch.setattr(media_transcript, 'read_transcript',
                        lambda client, project, media_id, source: {'content': content})
    def shot(media):
        return {'internal_timeline': {'tracks': [], 'registry': {'assets': {'audio': {'media_id': media}}},
            'clips': [{'id': 'shared', 'clipType': 'media', 'track': 'audio', 'asset': 'audio',
                       'at': 0, 'from': 0, 'to': 4}]}}
    timeline = Checkout({'project_id': 'p', 'timeline_id': 't', 'parent': {'config': {}, 'clips': []},
        'placements': [{'shot_id': 'a', 'occurrence_id': 'a1', 'placement': {'start_ms': 0}, 'duration_ms': 2000},
                       {'shot_id': 'b', 'occurrence_id': 'b1', 'placement': {'start_ms': 10000}, 'duration_ms': 4000}],
        'shots': {'a': shot(first), 'b': shot(second)}})
    monkeypatch.setattr(Checkout, 'open', classmethod(lambda cls, project, ref, client: timeline))
    args = SimpleNamespace(client=object(), project='p', media_id=None, source=None,
                           source_range=None, clip='shared', timeline='cut', json=False)
    assert cli_working._cmd_transcripts_show(args) != 0
    assert 'different media' in capsys.readouterr().err
    args.media_id = first
    assert cli_working._cmd_transcripts_show(args) == 0
    output = capsys.readouterr().out
    assert 'source 1.000–1.500s  in  timeline 1.000–1.500s' in output
    assert 'out' not in output


def test_transcript_show_respects_parent_clip_hold(monkeypatch, capsys):
    from astrid.packs.timeline import cli_working
    from astrid.sdk import media_transcript
    from astrid.sdk.timeline_checkout import Checkout
    media = 'sha256:' + '3' * 64
    content = build_transcript(media, 'line', 'in out', [[1, 1.5, 'in'], [3, 3.5, 'out']])
    monkeypatch.setattr(media_transcript, 'read_transcript', lambda *args: {'content': content})
    timeline = Checkout({'project_id': 'p', 'timeline_id': 't',
        'parent': {'registry': {'assets': {'audio': {'media_id': media}}}, 'config': {},
                   'clips': [{'id': 'audio', 'clipType': 'media', 'track': 'music', 'asset': 'audio',
                              'at': 10, 'from': 0, 'to': 4, 'hold': 2}]},
        'placements': [], 'shots': {}})
    monkeypatch.setattr(Checkout, 'open', classmethod(lambda cls, project, ref, client: timeline))
    args = SimpleNamespace(client=object(), project='p', media_id=None, source=None,
                           source_range=None, clip='audio', timeline='cut', json=False)
    assert cli_working._cmd_transcripts_show(args) == 0
    output = capsys.readouterr().out
    assert 'timeline 11.000–11.500s' in output
    assert 'out' not in output
