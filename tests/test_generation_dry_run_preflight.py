"""Every dry run that precedes a paid submission runs the invocation preflight.

Also: a remote request is refused locally before any claimed machine is required,
and that refusal says its scope instead of reading as an unexplained bug.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr

import pytest

from astrid.core.contracts.errors import render_astrid_error
from astrid.core.generation.preflight import GenerationReadinessError
from astrid.packs.vibecomfy import invocation_preflight as ip
from astrid.sdk import generation as sdk_generation
from astrid.sdk.generation import GenerationInputError

_NO_MACHINE = sdk_generation._NO_MACHINE


def _refuse_prompt(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise ip.InvocationPreflightError(
            "VIBECOMFY_PROMPT_EMPTY: every reachable prompt input is empty"
        )

    monkeypatch.setattr(ip, "preflight_invocation", refuse)


def test_transcription_dry_run_runs_the_invocation_preflight(monkeypatch):
    from astrid.sdk.generation import generate

    calls = []
    original = ip.preflight_invocation

    def record(*args, **kwargs):
        calls.append(kwargs.get("phase"))
        return original(*args, **kwargs)

    monkeypatch.setattr(ip, "preflight_invocation", record)

    result = generate.image(entry="image/z_image", prompt="a red square",
                            placement="runpod", dry_run=True)

    assert calls == ["dry-run"]
    assert result is not None


def test_dry_run_refuses_what_the_submission_preflight_would_refuse(monkeypatch):
    from astrid.sdk.generation import generate

    _refuse_prompt(monkeypatch)

    with pytest.raises(GenerationReadinessError) as caught:
        generate.image(entry="image/z_image", prompt="a red square",
                       placement="runpod", dry_run=True)

    message = caught.value.readiness["issues"][0]["message"]
    assert "VIBECOMFY_PROMPT_EMPTY" in message
    assert "refused locally before submission" in message
    assert "nothing was sent to a machine" in message
    assert "any claimed machine is unaffected" in message


def test_scoped_refusal_is_structured_and_names_its_scope(monkeypatch, capsys):
    from astrid.sdk.generation import generate

    _refuse_prompt(monkeypatch)
    with pytest.raises(GenerationReadinessError) as caught:
        generate.image(entry="image/z_image", prompt="a red square",
                       placement="runpod", dry_run=True)
    error = caught.value

    assert not error.degraded
    with redirect_stderr(io.StringIO()) as stderr:
        render_astrid_error(error)
    rendered = stderr.getvalue()
    assert "unstructured" not in rendered
    assert "report this bug" not in rendered
    assert "nothing was sent to a machine" in rendered


def test_remote_request_is_refused_locally_before_the_machine_requirement(monkeypatch):
    from astrid.sdk.generation import generate

    _refuse_prompt(monkeypatch)

    def no_machine(*_args, **_kwargs):
        raise GenerationInputError(_NO_MACHINE)

    monkeypatch.setattr(sdk_generation, "_require_claimed_machine", no_machine)

    with pytest.raises(GenerationReadinessError, match="VIBECOMFY_PROMPT_EMPTY"):
        generate.image(entry="image/z_image", prompt="a red square", placement="runpod")


def test_valid_remote_request_still_requires_a_claimed_machine(monkeypatch):
    from astrid.sdk.generation import generate

    def no_machine(*_args, **_kwargs):
        raise GenerationInputError(_NO_MACHINE)

    monkeypatch.setattr(sdk_generation, "_require_claimed_machine", no_machine)

    with pytest.raises(GenerationInputError, match="no machine"):
        generate.image(entry="image/z_image", prompt="a red square", placement="runpod")


@pytest.mark.parametrize("launch_flags", [[], ["--dry-run"]])
def test_timelines_transcripts_run_refuses_before_admission(capsys, launch_flags):
    from astrid.core.cli.domain_timelines import build_parser

    with pytest.raises(SystemExit) as caught:
        build_parser(None).parse_args(["transcripts", "run", "sha256:" + "0" * 64,
                                       "--project", "p", *launch_flags])

    assert caught.value.code == 2
    assert "audio transcription is unavailable in this release" in capsys.readouterr().err


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
