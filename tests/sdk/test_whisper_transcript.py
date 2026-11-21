from types import SimpleNamespace

import pytest

from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.whisper_transcript import (
    build_whisper_transcript,
    import_whisper_transcript,
    whisper_source_id,
)

MEDIA = "sha256:" + "a" * 64


def test_serializes_exact_words_and_overlapping_times_with_runtime_shape():
    source = whisper_source_id("test-model")
    # Synthetic values in the tuple shape returned by the pinned apply_whisper.py;
    # this is a serializer fixture, not an inference result.
    result = (
        "hello there again",
        [{"value": "hello there", "start": 0, "end": 2},
         {"value": "again", "start": 2, "end": 3}],
        [{"value": "hello", "start": 0.1, "end": 1.1},
         {"value": "there", "start": 0.9, "end": 1.5},
         {"value": "again", "start": 2.2, "end": 2.8}],
    )
    actual = build_whisper_transcript(MEDIA, source, result, media_duration=3)
    assert set(actual) == {"schema", "media_id", "source_id", "segments"}
    assert actual["media_id"] == MEDIA
    assert actual["source_id"] == source
    assert len(actual["segments"]) == 2
    words = [word for segment in actual["segments"] for word in segment["words"]]
    assert [word["text"] for word in words] == ["hello", "there", "again"]
    assert words[0]["end"] > words[1]["start"]
    assert len({word["id"] for word in words}) == 3


def test_empty_result_serializes_to_empty_transcript():
    assert build_whisper_transcript(MEDIA, "source", ("", [], []), media_duration=0) == {
        "schema": "media.transcript/v1", "media_id": MEDIA, "source_id": "source", "segments": []
    }


def test_rejects_ambiguous_membership_and_words_beyond_media_duration():
    ambiguous = ("x", [{"value": "a", "start": 0, "end": 2},
                       {"value": "b", "start": 0.5, "end": 2.5}],
                 [{"value": "x", "start": 1, "end": 1.5}])
    with pytest.raises(ValueError, match="ambiguous or missing"):
        build_whisper_transcript(MEDIA, "source", ambiguous, media_duration=3)
    beyond = ("x", [{"value": "x", "start": 0, "end": 2}],
              [{"value": "x", "start": 1.9, "end": 2.1}])
    with pytest.raises(ValueError, match="beyond media duration"):
        build_whisper_transcript(MEDIA, "source", beyond, media_duration=2)


def test_rejects_word_outside_its_segment_even_inside_media():
    result = ("x", [{"value": "x", "start": 0, "end": 1}],
              [{"value": "x", "start": 0.9, "end": 1.1}])
    with pytest.raises(ValueError, match="ambiguous or missing"):
        build_whisper_transcript(MEDIA, "source", result, media_duration=2)


class Documents:
    def __init__(self, row=None):
        self.row = row
        self.calls = []

    def show(self, document_id, *, project):
        self.calls.append(("show", document_id, project))
        return (DomainResult.success(self.row) if self.row else
                DomainResult.failure(ErrorObject("not_found", "missing", {})))

    def create(self, *, kind, content, project, document_id):
        self.calls.append(("create", kind, project, document_id))
        self.row = {"document_id": document_id, "kind": kind, "content": content, "version": 1}
        return DomainResult.success(self.row)

    def update(self, document_id, *, content, expected_version, project):
        self.calls.append(("update", expected_version, project))
        self.row = {**self.row, "content": content, "version": expected_version + 1}
        return DomainResult.success(self.row)


def client(documents):
    projects = SimpleNamespace(show=lambda project: DomainResult.success({"id": "project-id"}))
    return SimpleNamespace(projects=projects, documents=documents)


def test_persistence_is_idempotent_and_replacement_uses_expected_version():
    docs = Documents()
    target = client(docs)
    source = whisper_source_id("test-model")
    original = ("a", [{"value": "a", "start": 0, "end": 1}],
                [{"value": "a", "start": 0.1, "end": 0.9}])
    changed = ("b", [{"value": "b", "start": 0, "end": 1}],
               [{"value": "b", "start": 0.1, "end": 0.9}])
    assert import_whisper_transcript(target, "slug", MEDIA, source, original, media_duration=2) == "created"
    with pytest.raises(ValueError, match="differs"):
        import_whisper_transcript(target, "slug", MEDIA, source, changed, media_duration=2)
    assert not any(call[0] == "update" for call in docs.calls)
    with pytest.raises(ValueError, match="expected version"):
        import_whisper_transcript(target, "slug", MEDIA, source, changed, media_duration=2,
                                  replace=True, expected_version=2)
    assert import_whisper_transcript(target, "slug", MEDIA, source, changed, media_duration=2,
                                     replace=True, expected_version=1) == "updated"
    assert ("update", 1, "project-id") in docs.calls


def test_rejects_non_digest_media_identity():
    with pytest.raises(ValueError, match="sha256 digest"):
        build_whisper_transcript("video-1", "source", ("", [], []), media_duration=0)
