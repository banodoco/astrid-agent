"""Every dry run that precedes a paid submission runs the invocation preflight.

Also: a remote request is refused locally before any claimed machine is required,
and that refusal says its scope instead of reading as an unexplained bug.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import wave
from contextlib import redirect_stderr
from pathlib import Path

import pytest

from astrid.core.contracts.errors import render_astrid_error
from astrid.core.generation.preflight import GenerationReadinessError
from astrid.packs.vibecomfy import invocation_preflight as ip
from astrid.sdk import generation as sdk_generation
from astrid.sdk.generation import GenerationInputError

_NO_MACHINE = sdk_generation._NO_MACHINE


def _wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0\0" * 16000)
    return path


def _refuse_prompt(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise ip.InvocationPreflightError(
            "VIBECOMFY_PROMPT_EMPTY: every reachable prompt input is empty"
        )

    monkeypatch.setattr(ip, "preflight_invocation", refuse)


def test_transcription_dry_run_runs_the_invocation_preflight(monkeypatch, tmp_path):
    from astrid.sdk.generation import generate

    calls = []
    original = ip.preflight_invocation

    def record(*args, **kwargs):
        calls.append(kwargs.get("phase"))
        return original(*args, **kwargs)

    monkeypatch.setattr(ip, "preflight_invocation", record)

    result = generate.audio(entry="transcription", mode="transcribe",
                            audio_ref=str(_wav(tmp_path / "input.wav")),
                            placement="runpod", dry_run=True)

    assert calls == ["dry-run"]
    assert result is not None


def test_dry_run_refuses_what_the_submission_preflight_would_refuse(monkeypatch, tmp_path):
    from astrid.sdk.generation import generate

    _refuse_prompt(monkeypatch)

    with pytest.raises(GenerationReadinessError) as caught:
        generate.audio(entry="transcription", mode="transcribe",
                       audio_ref=str(_wav(tmp_path / "input.wav")),
                       placement="runpod", dry_run=True)

    message = caught.value.readiness["issues"][0]["message"]
    assert "VIBECOMFY_PROMPT_EMPTY" in message
    assert "refused locally before submission" in message
    assert "nothing was sent to a machine" in message
    assert "any claimed machine is unaffected" in message


def test_scoped_refusal_is_structured_and_names_its_scope(monkeypatch, tmp_path, capsys):
    from astrid.sdk.generation import generate

    _refuse_prompt(monkeypatch)
    with pytest.raises(GenerationReadinessError) as caught:
        generate.audio(entry="transcription", mode="transcribe",
                       audio_ref=str(_wav(tmp_path / "input.wav")),
                       placement="runpod", dry_run=True)
    error = caught.value

    assert not error.degraded
    with redirect_stderr(io.StringIO()) as stderr:
        render_astrid_error(error)
    rendered = stderr.getvalue()
    assert "unstructured" not in rendered
    assert "report this bug" not in rendered
    assert "nothing was sent to a machine" in rendered


def test_remote_request_is_refused_locally_before_the_machine_requirement(monkeypatch, tmp_path):
    from astrid.sdk.generation import generate

    _refuse_prompt(monkeypatch)

    def no_machine(*_args, **_kwargs):
        raise GenerationInputError(_NO_MACHINE)

    monkeypatch.setattr(sdk_generation, "_require_claimed_machine", no_machine)

    with pytest.raises(GenerationReadinessError, match="VIBECOMFY_PROMPT_EMPTY"):
        generate.audio(entry="transcription", mode="transcribe",
                       audio_ref=str(_wav(tmp_path / "input.wav")), placement="runpod")


def test_valid_remote_request_still_requires_a_claimed_machine(monkeypatch, tmp_path):
    from astrid.sdk.generation import generate

    def no_machine(*_args, **_kwargs):
        raise GenerationInputError(_NO_MACHINE)

    monkeypatch.setattr(sdk_generation, "_require_claimed_machine", no_machine)

    with pytest.raises(GenerationInputError, match="no machine"):
        generate.audio(entry="transcription", mode="transcribe",
                       audio_ref=str(_wav(tmp_path / "input.wav")), placement="runpod")


def _transcripts_namespace(client, consent=()):
    return argparse.Namespace(
        media_id="sha256:" + "0" * 64, project="minkhole", client=client,
        placement="runpod", engine=None, execution=None, align=None, recovery_path=None,
        consent=list(consent), replace=False, expected_version=None, dry_run=True,
    )


def _fake_client(raw: bytes):
    class Media:
        def read_bytes(self, _digest):
            return raw

    class Client:
        media = Media()

    return Client()


def test_timelines_transcripts_run_dry_run_stops_before_admission(monkeypatch, tmp_path, capsys):
    from astrid.packs.timeline import cli_working
    import astrid.sdk.media_handles as media_handles
    import astrid.sdk.transcript_run as transcript_run
    from types import SimpleNamespace

    from astrid.sdk import generation as generation_module

    raw = b"not really audio"
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    parsed = _transcripts_namespace(_fake_client(raw))
    parsed.media_id = digest
    monkeypatch.setattr(media_handles, "resolve_media_handle",
                        lambda _client, _project, _media: ({"digest": digest, "filename": "clip.wav"}, {}))
    monkeypatch.setattr(cli_working, "need_project", lambda _parsed: "minkhole", raising=False)
    calls = []

    def fake_audio(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "dry_run": True, "entry": "transcription"}

    def admitted(*_args, **_kwargs):
        raise AssertionError("a dry run must not import or admit a transcript")

    monkeypatch.setattr(generation_module, "generate", SimpleNamespace(audio=fake_audio))
    monkeypatch.setattr(transcript_run, "import_transcription_run", admitted)

    assert cli_working._cmd_transcripts_run(parsed) == 0

    assert calls and calls[0]["dry_run"] is True
    assert '"dry_run": true' in capsys.readouterr().out


def test_dry_run_flag_is_declared_on_timelines_transcripts_run():
    from astrid.core.cli import domain_timelines

    parser = argparse.ArgumentParser()
    domain_timelines._working_configure_transcripts_run(parser)

    assert parser.parse_args(["sha256:" + "0" * 64, "--project", "p", "--dry-run"]).dry_run is True
    assert parser.parse_args(["sha256:" + "0" * 64, "--project", "p"]).dry_run is False


def test_generate_cli_refuses_a_bad_request_before_the_machine_requirement(monkeypatch, capsys):
    from astrid.core.generation import cli as generation_cli

    _refuse_prompt(monkeypatch)

    def no_machine(*_args, **_kwargs):
        raise GenerationInputError(_NO_MACHINE)

    monkeypatch.setattr(sdk_generation, "_require_claimed_machine", no_machine)

    with pytest.raises(GenerationReadinessError) as caught:
        generation_cli.main(["image", "--entry", "image/z_image", "--prompt", "a red square",
                             "--placement", "runpod"])

    message = caught.value.readiness["issues"][0]["message"]
    assert "VIBECOMFY_PROMPT_EMPTY" in message
    assert "nothing was sent to a machine" in message
    assert "no machine" not in message
    assert not caught.value.degraded


def test_generate_cli_still_requires_a_claimed_machine_for_a_valid_request(monkeypatch, capsys):
    from astrid.core.generation import cli as generation_cli

    def no_machine(*_args, **_kwargs):
        raise GenerationInputError(_NO_MACHINE)

    monkeypatch.setattr(sdk_generation, "_require_claimed_machine", no_machine)

    code = generation_cli.main(["image", "--entry", "image/z_image", "--prompt", "a red square",
                                "--placement", "runpod"])

    output = capsys.readouterr()
    assert code == 2
    assert "no machine" in output.out + output.err
