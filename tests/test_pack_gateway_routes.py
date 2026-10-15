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


def test_pixel_pack_lists_actions_and_sdk_invocation(capsys: pytest.CaptureFixture[str]) -> None:
    from astrid.core.gateway.hivemind import dispatch

    assert dispatch("pixel", []) == 0
    output = capsys.readouterr().out
    assert "pixel.cutout, pixel.snap, pixel.sprites, pixel.strip" in output
    assert 'sdk.invoke("pixel.cutout", kind="action"' in output


def test_hivemind_parser_preserves_json_before_or_after_subcommand() -> None:
    from astrid.core.gateway.hivemind import _parser

    parser = _parser()
    assert parser.parse_args(["--json", "search", "query"]).json is True
    assert parser.parse_args(["search", "query", "--json"]).json is True


def test_undeclared_pack_operation_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    from astrid.core.gateway.hivemind import dispatch

    assert dispatch("vibecomfy", ["search", "query"]) == 2
    assert "no declared CLI commands" in capsys.readouterr().err
