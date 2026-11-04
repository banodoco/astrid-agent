from __future__ import annotations

import contextlib

from pathlib import Path

from astrid.packs.timeline import cli as timeline_cli
from astrid.packs.timeline import cli_sweep


def test_assets_verb_lists_keys_with_their_clips(monkeypatch, capsys) -> None:
    class FakeCheckout:
        def assets(self, key=None):
            return [{"key": "R", "media": {"content_sha256": "abc", "type": "image/png", "size_bytes": 12,
                                           "source": "run:old/sprites#0"}, "clips": ["a-rocket", "b-tool"]}]

    monkeypatch.setattr("astrid.sdk.timeline_checkout.find_draft", lambda *a, **k: "draft.json")
    monkeypatch.setattr("astrid.sdk.timeline_checkout.Checkout.load", classmethod(lambda cls, path: FakeCheckout()))
    parsed = timeline_cli.build_parser(object()).parse_args(["assets", "--project", "p1", "main"])
    assert parsed.handler(parsed) == 0
    out = capsys.readouterr().out
    assert "R  abc  image/png  12 bytes  run:old/sprites#0" in out
    assert "clips: a-rocket, b-tool" in out


def test_edit_replace_asset_and_swap_asset_forward_to_the_sdk(monkeypatch, capsys) -> None:
    calls = []

    class FakeClip:
        data: dict = {}

        def swap_asset(self, asset):
            calls.append(("swap", asset))

    class FakeCheckout:
        music, path = "keep_end", Path("draft.json")
        version = 0

        def document(self):
            return {"version": self.version}

        def replace_asset(self, key, handle):
            calls.append(("replace", key, handle))
            self.version += 1
            return ["c19.broom", "c26.broom"]

        def clip(self, address, **_kw):
            self.version += 1
            return FakeClip()

        def save(self, **_kwargs):
            return Path("draft.json")

        merged: list = []
        report: list = []

        @contextlib.contextmanager
        def step(self, _label):
            yield self

        def check(self):
            return "valid"

        _check_core = check

    checkout = FakeCheckout()
    monkeypatch.setattr(timeline_cli, "_edit_checkout", lambda _parsed: checkout)
    monkeypatch.setattr(timeline_cli, "_describe_edit", lambda *_args: [])
    monkeypatch.setattr(timeline_cli, "_edit_cuts", lambda *_args: [])
    monkeypatch.setattr(cli_sweep, "touched_cuts", lambda *_args: set())
    monkeypatch.setattr(cli_sweep, "auto_sweep_after_edit", lambda *_args, **_kwargs: 0)
    parser = timeline_cli.build_parser(object())
    parsed = parser.parse_args(["edit", "--project", "p1", "main", "--replace-asset", "BROOM=run:r/sprites#0"])
    assert parsed.handler(parsed) == 0
    assert "replaced BROOM in 2 clip(s): c19.broom, c26.broom" in capsys.readouterr().out
    parsed = parser.parse_args(["edit", "--project", "p1", "main", "--clip", "c19.broom", "--swap-asset", "BROOM=run:r/sprites#0"])
    assert parsed.handler(parsed) == 0
    assert calls == [("replace", "BROOM", "run:r/sprites#0"), ("swap", "BROOM=run:r/sprites#0")]
