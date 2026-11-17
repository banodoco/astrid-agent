"""Producers of the one transcript store besides Whisper: speech words, `generate --transcribe`."""
from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli, delivery
from astrid.sdk import transcript_producers as producers
from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.media_transcript import transcript_document_id

PROJECT = "project-1"


class _Docs:
    def __init__(self):
        self.rows = {}

    def show(self, document_id, project=None):
        if document_id in self.rows:
            return DomainResult.success(self.rows[document_id])
        return DomainResult.failure(ErrorObject("not_found", "missing", {}))

    def create(self, *, kind, content, project, document_id):
        self.rows[document_id] = {"document_id": document_id, "kind": kind, "content": content, "version": 1}
        return DomainResult.success(self.rows[document_id])


class _Client:
    def __init__(self, objects):
        self.objects = {"sha256:" + hashlib.sha256(raw).hexdigest(): raw for raw in objects}
        self.projects = SimpleNamespace(show=lambda project: DomainResult.success({"project_id": PROJECT}))
        self.documents = _Docs()
        self.media = SimpleNamespace(read_bytes=lambda digest: self.objects[digest])


def _speech(audio=b"RIFFaudio", words=None, audio_sha=None):
    audio_digest = hashlib.sha256(audio).hexdigest()
    document = {"schema_version": 1, "source": "edge-tts", "boundary": "WordBoundary",
                "audio_sha256": audio_sha or audio_digest,
                "words": words or [{"word": "hello", "start_s": 0.1, "end_s": 0.4},
                                   {"word": "world", "start_s": 0.5, "end_s": 0.9}]}
    raw = json.dumps(document).encode()
    client = _Client([audio, raw])
    payload = {"manifest": {"text": "Hello world", "outputs": [
        {"path": "audio/speech.wav", "role": "result", "media_type": "audio/wav", "content_hash": "sha256:" + audio_digest},
        {"path": "speech-words.json", "name": "speech_words", "role": "auxiliary", "media_type": "application/json",
         "content_hash": "sha256:" + hashlib.sha256(raw).hexdigest()}]},
               "image_paths": ["/tmp/speech.wav"]}
    return client, payload, "sha256:" + audio_digest


def test_speech_words_become_the_takes_astrid_tts_transcript():
    client, payload, audio = _speech()

    stored = producers.import_tts_words(client, "slug", payload)

    assert stored == {"media_id": audio, "status": "created"}
    content = client.documents.rows[transcript_document_id(PROJECT, audio, producers.TTS_SOURCE)]["content"]
    assert content["source_id"] == "astrid.tts" and content["media_id"] == audio
    assert [(w["start"], w["end"], w["text"]) for w in content["segments"][0]["words"]] == [(0.1, 0.4, "hello"), (0.5, 0.9, "world")]
    assert content["segments"][0]["text"] == "Hello world"
    assert producers.import_tts_words(client, "slug", payload)["status"] == "unchanged"


def test_speech_words_measured_on_other_audio_are_refused():
    client, payload, _audio = _speech(audio_sha="f" * 64)
    with pytest.raises(ValueError, match="different audio"):
        producers.import_tts_words(client, "slug", payload)


def test_a_generation_without_speech_words_stores_nothing():
    client, payload, _ = _speech()
    payload["manifest"]["outputs"].pop()
    assert producers.import_tts_words(client, "slug", payload) is None
    assert producers.import_tts_words(client, "slug", {"manifest": {"outputs": []}}) is None


def test_transcribe_generated_runs_the_transcripts_run_path_per_audio_output(monkeypatch):
    client, payload, audio = _speech()
    calls = []

    def run(client_, project, media, **options):
        calls.append((project, media, options))
        return "created", media, "run-1"

    monkeypatch.setattr(producers, "run_transcription", run)
    rows = producers.transcribe_generated(client, PROJECT, payload, placement="runpod", consent=())

    assert rows == [{"media_id": audio, "status": "created", "run_id": "run-1"}]
    assert calls == [(PROJECT, audio, {"placement": "runpod", "consent": ()})]


def test_a_failed_transcription_is_reported_not_raised(monkeypatch):
    client, payload, audio = _speech()
    monkeypatch.setattr(producers, "run_transcription", lambda *a, **k: (_ for _ in ()).throw(ValueError("no GPU")))
    assert producers.transcribe_generated(client, PROJECT, payload) == [
        {"media_id": audio, "status": "failed", "error": "no GPU"}]


def test_video_output_counts_only_with_an_audio_stream(monkeypatch):
    from astrid.core import media

    digest = "sha256:" + "9" * 64
    payload = {"manifest": {"outputs": [{"role": "result", "media_type": "video/mp4", "content_hash": digest}]},
               "image_paths": ["/tmp/clip.mp4"]}
    monkeypatch.setattr(media, "ffprobe_metadata", lambda path: SimpleNamespace(has_audio_stream=False))
    assert producers.audio_outputs(None, payload) == []
    monkeypatch.setattr(media, "ffprobe_metadata", lambda path: SimpleNamespace(has_audio_stream=True))
    assert producers.audio_outputs(None, payload) == [{"media_id": digest, "path": "/tmp/clip.mp4"}]


# ---- the generate CLI -------------------------------------------------------

def _facade(payload):
    return SimpleNamespace(audio=lambda **kw: SimpleNamespace(ok=True, to_dict=lambda: dict(payload)),
                           video=lambda **kw: SimpleNamespace(ok=True, to_dict=lambda: dict(payload)))


def test_transcribe_flag_is_for_audio_and_video_only():
    parser = cli._parser()
    choices = parser._subparsers._group_actions[0].choices
    assert any(a.dest == "transcribe" for a in choices["audio"]._actions)
    assert any(a.dest == "transcribe" for a in choices["video"]._actions)
    assert not any(a.dest == "transcribe" for a in choices["image"]._actions)


@pytest.mark.parametrize("extra", [["--dry-run"], ["--no-wait"]])
def test_transcribe_needs_a_completed_run(monkeypatch, extra):
    monkeypatch.setattr(cli, "_generate_facade", lambda: _facade({}))
    with pytest.raises(SystemExit):
        cli.main(["audio", "--mode", "tts", "hello", "--transcribe", *extra])


def test_speech_words_and_transcription_are_stored_after_a_completed_run(monkeypatch, capsys):
    client, payload, audio = _speech()
    monkeypatch.setattr(cli, "_generate_facade", lambda: _facade(payload))
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext(client))
    monkeypatch.setattr(delivery, "deliver_generation", lambda result, **kwargs: result)
    runs = []
    monkeypatch.setattr(producers, "run_transcription", lambda c, p, m, **o: runs.append((p, m, o)) or ("created", m, "run-9"))

    assert cli.main(["audio", "--mode", "tts", "hello world", "--project", PROJECT, "--transcribe",
                     "--placement", "managed", "--json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert [(row["source"], row["status"]) for row in out["transcripts"]] == [("astrid.tts", "created"), ("whisper", "created")]
    assert runs[0][:2] == (PROJECT, audio) and runs[0][2]["placement"] == "managed"
    assert transcript_document_id(PROJECT, audio, "astrid.tts") in client.documents.rows


def test_speech_without_the_flag_stores_words_but_runs_no_transcription(monkeypatch, capsys):
    client, payload, audio = _speech()
    monkeypatch.setattr(cli, "_generate_facade", lambda: _facade(payload))
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext(client))
    monkeypatch.setattr(delivery, "deliver_generation", lambda result, **kwargs: result)
    monkeypatch.setattr(producers, "run_transcription", lambda *a, **k: pytest.fail("ran a GPU transcription unasked"))

    assert cli.main(["audio", "--mode", "tts", "hello world", "--project", PROJECT, "--json"]) == 0

    assert [row["source"] for row in json.loads(capsys.readouterr().out)["transcripts"]] == ["astrid.tts"]
