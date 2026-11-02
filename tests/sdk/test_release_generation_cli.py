"""CLI path: `astrid generate release --engine comfyui --target managed --json` into AstridClient."""

from __future__ import annotations

import json

from astrid.core.generation import cli
from astrid.sdk.client import AstridClient
from tests.sdk.test_release_generation_client import _FakeComfy, _registered

ARGV = ["release", "--engine", "comfyui", "--target", "managed", "--json"]


def test_cli_release_succeeds_and_prints_only_proven_claims(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_open_client", lambda: AstridClient(remote=object()))
    with _FakeComfy() as fake:
        _registered(monkeypatch, fake.url)
        code = cli.main(ARGV)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["released"] is True and payload["models_unloaded"] is True
    assert payload["process_exited"] is False


def test_cli_release_failure_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_open_client", lambda: AstridClient(remote=object()))
    with _FakeComfy(free_fails=True) as fake:
        _registered(monkeypatch, fake.url)
        code = cli.main(ARGV)
    out = capsys.readouterr().out
    assert code != 0
    assert '"released": true' not in out


def test_cli_attached_target_is_refused(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_open_client", lambda: AstridClient(remote=object()))
    code = cli.main(["release", "--engine", "comfyui", "--target", "http://127.0.0.1:8188", "--json"])
    assert code != 0


def test_cli_no_managed_engine_is_missing_dependency(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_open_client", lambda: AstridClient(remote=object()))
    _registered(monkeypatch, None)
    code = cli.main(ARGV)
    assert code != 0


def test_cli_release_not_proven_exits_nonzero(monkeypatch, capsys):
    from astrid.core.generation import engine_custody
    monkeypatch.setattr(cli, "_open_client", lambda: AstridClient(remote=object()))
    with _FakeComfy() as fake:
        _registered(monkeypatch, fake.url)
        monkeypatch.setattr(engine_custody.LinuxEngineServiceClient, "_request",
                            lambda *args, **kwargs: None)
        code = cli.main(ARGV)
        assert fake.posts == []
    assert code != 0
    assert "release_not_proven" in capsys.readouterr().out
