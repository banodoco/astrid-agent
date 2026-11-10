"""Regression coverage for installed-pack gateway routing."""

from __future__ import annotations

import pytest


def test_pack_routes_never_shadow_core_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    from astrid.core.gateway import dispatch

    local_handlers = dict(dispatch._TOP_LEVEL_HANDLERS)
    monkeypatch.setattr(dispatch, "_TOP_LEVEL_HANDLERS", local_handlers)
    original_media = dispatch._TOP_LEVEL_HANDLERS["media"]
    monkeypatch.setattr(
        dispatch,
        "_installed_pack_ids",
        lambda: frozenset({"media", "vibecomfy"}),
    )
    dispatch._register_installed_pack_routes()

    assert dispatch._TOP_LEVEL_HANDLERS["media"] is original_media
    assert "vibecomfy" in dispatch._TOP_LEVEL_HANDLERS


def test_installed_pack_without_cli_mapping_is_honest(capsys: pytest.CaptureFixture[str]) -> None:
    from astrid.core.gateway.hivemind import dispatch

    assert dispatch("vibecomfy", []) == 0
    output = capsys.readouterr().out
    assert "no declared CLI commands" in output
    assert "PackCommandSpec" not in output


def test_hivemind_parser_preserves_json_before_or_after_subcommand() -> None:
    from astrid.core.gateway.hivemind import _parser

    parser = _parser()
    assert parser.parse_args(["--json", "search", "query"]).json is True
    assert parser.parse_args(["search", "query", "--json"]).json is True


def test_undeclared_pack_operation_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    from astrid.core.gateway.hivemind import dispatch

    assert dispatch("vibecomfy", ["search", "query"]) == 2
    assert "no declared CLI commands" in capsys.readouterr().err


def test_public_generate_audio_uses_audio_parser_and_admits_runpod_sfx(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    from astrid.core import gateway
    from astrid.core.generation import cli

    assert gateway.main(["generate", "audio", "--help"]) == 0
    help_output = capsys.readouterr().out
    assert "--mode {music,tts,sfx}" in help_output
    assert "generation history" not in help_output

    calls = []
    monkeypatch.setattr(cli, "_generate_facade", lambda: SimpleNamespace(
        audio=lambda **kwargs: calls.append(kwargs) or SimpleNamespace(ok=True, kernel_task_id=None,
            to_dict=lambda: {"status": "preview"})))
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext("runtime-client"))
    assert gateway.main([
        "generate", "audio", "waves", "--mode", "sfx", "--placement", "runpod", "--dry-run", "--json",
    ]) == 0
    assert calls and calls[0]["mode"] == "sfx" and calls[0]["placement"] == "runpod"
    capsys.readouterr()
