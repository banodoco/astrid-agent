"""Re-importing a Whisper transcript compares its content; differing content needs explicit replacement."""
from types import SimpleNamespace

import pytest

from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.transcript_run import transcription_run_id
from astrid.sdk.whisper_transcript import import_whisper_transcript, whisper_source_id

MEDIA = "sha256:" + "b" * 64


class Documents:
    def __init__(self):
        self.row = None
        self.calls = []

    def show(self, document_id, *, project):
        self.calls.append(("show", document_id))
        if self.row is None:
            return DomainResult.failure(ErrorObject("not_found", "missing", {}))
        return DomainResult.success(self.row)

    def create(self, *, kind, content, project, document_id):
        self.calls.append(("create", document_id))
        self.row = {"document_id": document_id, "kind": kind, "content": content, "version": 1}
        return DomainResult.success(self.row)

    def update(self, document_id, *, content, expected_version, project):
        self.calls.append(("update", expected_version))
        self.row = {**self.row, "content": content, "version": expected_version + 1}
        return DomainResult.success(self.row)


def _client(documents):
    projects = SimpleNamespace(show=lambda project: DomainResult.success({"id": "project-id"}))
    return SimpleNamespace(projects=projects, documents=documents)


SOURCE = whisper_source_id("test-model")
FIRST = ("hello", [{"value": "hello", "start": 0, "end": 1}], [{"value": "hello", "start": 0.1, "end": 0.9}])
SECOND = ("bye", [{"value": "bye", "start": 0, "end": 1}], [{"value": "bye", "start": 0.1, "end": 0.9}])


def test_identical_reimport_is_unchanged_and_writes_nothing():
    docs = Documents()
    target = _client(docs)
    assert import_whisper_transcript(target, "slug", MEDIA, SOURCE, FIRST, media_duration=2) == "created"
    docs.calls.clear()

    assert import_whisper_transcript(target, "slug", MEDIA, SOURCE, FIRST, media_duration=2) == "unchanged"
    assert docs.calls == [("show", docs.calls[0][1])]


def test_differing_reimport_is_refused_and_names_the_replacement():
    docs = Documents()
    target = _client(docs)
    import_whisper_transcript(target, "slug", MEDIA, SOURCE, FIRST, media_duration=2)

    with pytest.raises(ValueError, match=r"differs.*--replace --expected-version 1"):
        import_whisper_transcript(target, "slug", MEDIA, SOURCE, SECOND, media_duration=2)
    assert not any(call[0] == "update" for call in docs.calls)


def test_differing_reimport_replaces_only_with_the_expected_version():
    docs = Documents()
    target = _client(docs)
    import_whisper_transcript(target, "slug", MEDIA, SOURCE, FIRST, media_duration=2)

    assert import_whisper_transcript(target, "slug", MEDIA, SOURCE, SECOND, media_duration=2,
                                     replace=True, expected_version=1) == "updated"
    assert docs.row["version"] == 2


def test_transcription_run_id_reads_the_run_or_the_task_and_refuses_neither():
    client = SimpleNamespace(tasks=SimpleNamespace(
        show=lambda task_id: DomainResult.success({"run_id": f"run-of-{task_id}"})))

    assert transcription_run_id(client, {"kernel_run_id": "run-1"}) == "run-1"
    assert transcription_run_id(client, {"task_id": "t2"}) == "run-of-t2"
    with pytest.raises(ValueError, match="no run id"):
        transcription_run_id(client, {})
