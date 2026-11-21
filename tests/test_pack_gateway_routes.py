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
    assert parser.parse_args(["--json", "item", "--kind", "resource", "--id", "42"]).json is True
    assert parser.parse_args(["item", "--kind", "resource", "--id", "42", "--json"]).json is True


@pytest.mark.parametrize("flag", ["--id", "--revision-id"])
@pytest.mark.parametrize("value", ["", "１２", "1x", "-1"])
def test_hivemind_item_rejects_non_ascii_decimal_ids_by_flag(flag, value, capsys) -> None:
    from astrid.core.gateway.hivemind import _parser

    argv = ["item", "--kind", "resource", "--id", "42"]
    if flag == "--id":
        argv[argv.index("42")] = value
    else:
        argv.extend([flag, value])
    with pytest.raises(SystemExit):
        _parser().parse_args(argv)
    assert flag in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--id", "--revision-id"])
@pytest.mark.parametrize("value", ["", "１２", "1x", "-1"])
def test_get_item_executor_rejects_non_ascii_decimal_ids_by_flag(flag, value, capsys) -> None:
    from astrid.core.hivemind.executors.get_item.run import build_parser

    argv = ["--kind", "resource", "--id", "42"]
    if flag == "--id":
        argv[argv.index("42")] = value
    else:
        argv.extend([flag, value])
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)
    assert flag in capsys.readouterr().err


def test_hivemind_item_invokes_get_item_and_renders_artifact_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from types import SimpleNamespace

    from astrid.core.gateway.hivemind import dispatch
    from astrid.sdk.client import AstridClient

    item = {"item": {"item_id": "42", "title": "Example"}, "cited_by": []}
    calls: list[dict[str, object]] = []

    class FakeClient:
        media = SimpleNamespace(read_bytes=lambda digest: b'{"item":{"item_id":"42","title":"Example"},"cited_by":[]}')

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def invoke_result(self, capability_id, **kwargs):
            calls.append({"capability_id": capability_id, **kwargs})
            return SimpleNamespace(to_dict=lambda: {
                "ok": True,
                "run_id": "run-1",
                "capability_id": capability_id,
                "outputs": {"artifacts": [{"name": "item", "digest": "abc"}]},
                "raw_result": {},
            })

    monkeypatch.setattr(AstridClient, "open_from_launcher", lambda: FakeClient())
    assert dispatch("hivemind", [
        "item", "--kind", "resource", "--id", "42", "--revision-id", "7", "--json",
    ]) == 0

    import json

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["data"]["item"] == item
    assert calls == [{
        "capability_id": "hivemind.get_item",
        "kind": "executor",
        "inputs": {"kind": "resource", "id": "42", "revision_id": "7"},
        "wait": True,
        "timeout_seconds": 120.0,
    }]


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
    # Plan 59 adds transcription and wraps the help line (c76fea09b).
    assert "--mode {music,tts,sfx}" in " ".join(help_output.split())
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
