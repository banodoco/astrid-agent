"""The Agent audio CLI refuses transcription requests for this release."""
from __future__ import annotations

import json

import pytest

from astrid.core.generation import cli, defaults


def _assert_refusal(capsys, request):
    assert cli.main(["audio", *request, "--json"]) == 2
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "blocked"
    assert envelope["task_id"] is None
    assert envelope["issues"][0]["code"] == "input_rejected"
    assert "audio transcription is unavailable in this release" in envelope["issues"][0]["message"]


@pytest.fixture(autouse=True)
def _forbid_dispatch(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("transcription refusal must precede generation and Runtime access")

    monkeypatch.setattr(cli, "_generate_facade", unexpected)
    monkeypatch.setattr(cli, "_open_client", unexpected)


def test_transcribe_mode_and_audio_ref_are_refused_without_an_entry(capsys, tmp_path):
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"RIFF")
    _assert_refusal(capsys, ["--mode", "transcribe", "--audio-ref", str(clip), "--dry-run"])


def test_audio_help_lists_only_sound_generation_modes(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["audio", "--help"])

    assert caught.value.code == 0
    help_text = capsys.readouterr().out
    assert "--mode {music,tts,sfx}" in help_text
    assert "transcribe" not in help_text
    assert "--audio-ref" not in help_text


def test_transcription_entry_is_refused_before_admission(capsys):
    _assert_refusal(capsys, ["--mode", "tts", "--entry", "transcription"])


def test_transcription_template_is_refused_as_a_legacy_model(capsys):
    _assert_refusal(capsys, ["--mode", "tts", "--model", "audio/transcribe"])


def test_audio_transcribe_default_is_absent():
    resolved = defaults.resolve("audio", "transcribe", pins={}, candidates=defaults.pack_candidates())

    assert resolved.entry is None
    assert resolved.source == "none"


def test_transcribe_audio_ref_refuses_without_reading_project_media(capsys):
    _assert_refusal(capsys, ["--mode", "transcribe", "--audio-ref", "sha256:" + "c" * 64,
                            "--project", "minkhole"])


def test_transcribe_audio_ref_without_project_reports_the_holdout(capsys):
    _assert_refusal(capsys, ["--mode", "transcribe", "--audio-ref", "sha256:" + "c" * 64])
