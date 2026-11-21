"""The release omits transcription generation while retaining timeline transcript reads."""
from __future__ import annotations

from inspect import signature

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


def test_audio_sdk_forwards_only_the_requested_generation_arguments(monkeypatch):
    expected_names = set(signature(GenerationFacade.audio).parameters) - {"self", "inputs"} | {"prompt"}
    result = object()

    def dispatch(self, modality, **kwargs):
        assert modality == "audio"
        assert set(kwargs) == expected_names
        assert kwargs["mode"] == "sfx" and kwargs["prompt"] == "waves"
        return result

    monkeypatch.setattr(GenerationFacade, "_media_request", dispatch)
    assert GenerationFacade().audio(mode="sfx", prompt="waves") is result


@pytest.mark.parametrize("entry_id", ["transcription", "audio/transcribe"])
def test_generic_generation_refuses_transcription_before_discovery(monkeypatch, entry_id):
    from astrid.sdk import generation

    def unexpected(**_kwargs):
        pytest.fail("the holdout must refuse before discovering models or dispatching generation")

    monkeypatch.setattr(generation, "_load_model_registry", unexpected)
    with pytest.raises(GenerationInputError, match="audio transcription is unavailable"):
        GenerationFacade().generate(entry_id)


def test_saved_and_contributed_entries_cannot_reintroduce_the_held_ids(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from vibecomfy import entries as vibe_entries
    from astrid.core.extensions import scopes

    monkeypatch.setattr(vibe_entries, "discover_entries",
                        lambda: [{"id": "transcription"}, {"id": "saved-audio"}])
    pack = tmp_path / "custom-audio"
    extensions = pack / "extensions" / "vibecomfy.workflows"
    extensions.mkdir(parents=True)
    (extensions / "custom-audio.yaml").write_text(
        "ready_root: ../../workflows\n"
        "entries:\n"
        "  - {id: audio/transcribe, bundle: retired}\n"
        "  - {id: custom-audio, bundle: custom}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(scopes, "contribution_roots", lambda: [SimpleNamespace(path=tmp_path, scope="user")])

    assert [row["id"] for row in entries.user_entries()] == ["saved-audio", "custom-audio"]
    assert entries.find_entry("transcription") is None
    assert entries.find_entry("audio/transcribe") is None
