"""The release omits transcription generation while retaining timeline transcript reads."""
from __future__ import annotations

import pytest

from astrid.core.cli.domain_timelines import build_parser
from astrid.core.generation import cli, defaults, entries
from astrid.sdk.generation import GenerationFacade, GenerationInputError


@pytest.mark.parametrize(("options", "diagnostic"), [
    (["--mode", "transcribe"], "audio transcription is unavailable"),
    (["--mode", "tts", "--entry", "transcription"], "audio transcription is unavailable"),
    (["--mode", "tts", "--model", "audio/transcribe"], "audio transcription is unavailable"),
])
def test_audio_cli_refuses_transcription_before_dispatch(monkeypatch, capsys, options, diagnostic):
    def unexpected(*args, **kwargs):
        pytest.fail("refusal must occur before getting a Runtime client or dispatching generation")

    monkeypatch.setattr(cli, "_open_client", unexpected)
    monkeypatch.setattr(cli, "_generate_facade", unexpected)

    with pytest.raises(SystemExit) as caught:
        cli.main(["audio", *options])

    assert caught.value.code == 2
    assert diagnostic in capsys.readouterr().err


@pytest.mark.parametrize("selectors", [
    {"mode": "transcribe"},
    {"entry": "transcription"},
    {"model": "audio/transcribe"},
])
def test_audio_sdk_refuses_transcription_before_dispatch(monkeypatch, selectors):
    facade = GenerationFacade()

    def unexpected(*args, **kwargs):
        pytest.fail("refusal must occur before dispatching generation")

    monkeypatch.setattr(GenerationFacade, "_media_request", unexpected)

    with pytest.raises(GenerationInputError, match="audio transcription is unavailable"):
        facade.audio(placement="runpod", **selectors)


def test_transcription_entries_and_default_are_absent():
    assert entries.find_entry("transcription") is None
    assert entries.find_entry("audio/transcribe") is None
    resolved = defaults.resolve("audio", "transcribe", pins={}, candidates=defaults.pack_candidates())
    assert resolved.entry is None and resolved.source == "none"


@pytest.mark.parametrize(("args", "diagnostic"), [
    (["run", "media-id", "--project", "p"], "audio transcription is unavailable"),
    (["refresh", "timeline", "--project", "p", "--run-missing", "--placement", "runpod"],
     "audio transcription is unavailable"),
])
def test_timeline_parser_refuses_retired_launches(capsys, args, diagnostic):
    with pytest.raises(SystemExit) as caught:
        build_parser(None).parse_args(["transcripts", *args])

    assert caught.value.code == 2
    assert diagnostic in capsys.readouterr().err
