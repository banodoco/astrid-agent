"""`astrid generate audio --mode transcribe`: the audio input, the default entry and the run it reports."""
from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli, defaults


def _facade(calls, payload):
    def audio(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=True, to_dict=lambda: dict(payload))
    return SimpleNamespace(audio=audio)


def test_transcribe_mode_and_audio_ref_are_accepted_without_an_entry(monkeypatch, capsys, tmp_path):
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"RIFF")
    calls = []
    monkeypatch.setattr(cli, "_generate_facade", lambda: _facade(calls, {"status": "preview"}))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("dry run connected to Runtime"))

    assert cli.main(["audio", "--mode", "transcribe", "--audio-ref", str(clip), "--dry-run", "--json"]) == 0

    assert calls == [{"model": None, "mode": "transcribe", "placement": "auto", "dry_run": True,
                      "audio_ref": str(clip)}]
    assert json.loads(capsys.readouterr().out) == {"status": "preview"}


def test_transcribe_is_listed_and_audio_ref_is_audio_only():
    parser = cli._parser()
    audio = parser._subparsers._group_actions[0].choices["audio"]
    image = parser._subparsers._group_actions[0].choices["image"]
    mode = next(action for action in audio._actions if action.dest == "mode")
    assert "transcribe" in mode.choices and "music" in mode.choices
    assert any(action.dest == "audio_ref" for action in audio._actions)
    assert not any(action.dest == "audio_ref" for action in image._actions)


def test_transcribe_run_id_is_reported_for_import(monkeypatch, capsys, tmp_path):
    from astrid.core.generation import delivery

    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"RIFF")

    calls = []
    monkeypatch.setattr(cli, "_generate_facade", lambda: _facade(calls, {"kernel_run_id": "run-7"}))
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext("runtime-client"))
    monkeypatch.setattr(delivery, "deliver_generation", lambda result, **kwargs: result)

    assert cli.main(["audio", "--mode", "transcribe", "--audio-ref", str(clip), "--json"]) == 0

    assert calls[0]["mode"] == "transcribe" and calls[0]["audio_ref"] == str(clip)
    assert json.loads(capsys.readouterr().out)["run_id"] == "run-7"


def test_transcribe_run_id_is_found_through_the_task_like_transcripts_run(monkeypatch, capsys, tmp_path):
    from astrid.core.generation import delivery
    from astrid.sdk.contracts import DomainResult

    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"RIFF")

    client = SimpleNamespace(tasks=SimpleNamespace(
        show=lambda task_id: DomainResult.success({"run_id": "run-from-task"})))
    monkeypatch.setattr(cli, "_generate_facade", lambda: _facade([], {"kernel_task_id": "task-1"}))
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext(client))
    monkeypatch.setattr(delivery, "deliver_generation", lambda result, **kwargs: result)

    assert cli.main(["audio", "--mode", "transcribe", "--audio-ref", str(clip), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "run-from-task"


def test_audio_transcribe_default_resolves_to_the_transcription_entry():
    resolved = defaults.resolve("audio", "transcribe", pins={}, candidates=defaults.pack_candidates())

    assert resolved.entry == "transcription"
    assert resolved.source == "pack default"


def test_transcribe_audio_ref_accepts_a_project_media_id(monkeypatch, capsys):
    import hashlib
    from pathlib import Path

    from astrid.core.generation import delivery
    from astrid.sdk import media_handles

    raw = b"RIFF-media"
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    seen = {}

    def invoke(**kwargs):
        seen.update(kwargs)
        assert Path(kwargs["audio_ref"]).read_bytes() == raw  # materialized for the whole generation
        return SimpleNamespace(ok=True, to_dict=lambda: {"kernel_run_id": "run-m"})

    client = SimpleNamespace(media=SimpleNamespace(read_bytes=lambda value: raw))
    monkeypatch.setattr(media_handles, "resolve_media_handle",
                        lambda client, project, ref: ({"digest": digest, "filename": "clip.wav"}, {}))
    monkeypatch.setattr(cli, "_generate_facade", lambda: SimpleNamespace(audio=invoke))
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext(client))
    monkeypatch.setattr(delivery, "deliver_generation", lambda result, **kwargs: result)

    assert cli.main(["audio", "--mode", "transcribe", "--audio-ref", digest, "--project", "minkhole", "--json"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["media_id"] == digest and out["run_id"] == "run-m"
    assert not Path(seen["audio_ref"]).exists()  # the temporary copy is removed afterwards


def test_transcribe_audio_ref_media_id_needs_a_project(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("no project, so no Runtime connection"))
    assert cli.main(["audio", "--mode", "transcribe", "--audio-ref", "sha256:" + "c" * 64, "--json"]) != 0
    assert "needs --project" in capsys.readouterr().out
