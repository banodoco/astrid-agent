from __future__ import annotations

import contextlib
from pathlib import Path

from astrid.packs.timeline import cli as timeline_cli
from astrid.packs.timeline import cli_sweep


def _fake_checkout(calls):
    class FakeVoice:
        def __init__(self, segment):
            self.segment = segment

        def replace(self, media, **kwargs):
            calls.append(("replace", self.segment, media, kwargs))
            checkout.version += 1

        def set_gap_after(self, gap, reflow=True):
            calls.append(("gap", self.segment, gap))
            checkout.version += 1

    class FakeCheckout:
        music, path = "keep_end", Path("draft.json")
        version = 0

        def document(self):
            return {"version": self.version}

        def voice(self, segment):
            return FakeVoice(segment)

        def insert_line(self, segment, media, **kwargs):
            calls.append(("insert", segment, media, kwargs))
            self.version += 1

        def apply_script(self, script, **kwargs):
            calls.append(("script", script, kwargs))
            self.version += 1

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

        def reflow(self):
            return []

    checkout = FakeCheckout()
    return checkout


def _bind_fake_edit(monkeypatch, checkout):
    monkeypatch.setattr(timeline_cli, "_edit_checkout", lambda _parsed: checkout)
    monkeypatch.setattr(timeline_cli, "_describe_edit", lambda *_args: [])
    monkeypatch.setattr(timeline_cli, "_edit_cuts", lambda *_args: [])
    monkeypatch.setattr(cli_sweep, "touched_cuts", lambda *_args: set())
    monkeypatch.setattr(cli_sweep, "auto_sweep_after_edit", lambda *_args, **_kwargs: 0)


def test_replace_insert_and_from_script_forward_existing_sdk_arguments(monkeypatch) -> None:
    parser = timeline_cli.build_parser(object())
    calls = []
    checkout = _fake_checkout(calls)
    _bind_fake_edit(monkeypatch, checkout)

    replace = parser.parse_args([
        "edit", "main", "--project", "p1", "--replace-line", "vo-1",
        "--take", "take.wav", "--words", "take.words.json", "--text", "New words",
    ])
    assert replace.handler(replace) == 0
    assert calls == [(
        "replace", "vo-1", "take.wav",
        {"words": "take.words.json", "text": "New words"},
    )]

    calls.clear()
    checkout.version = 0
    insert = parser.parse_args([
        "edit", "main", "--project", "p1", "--insert-line", "vo-2", "--after", "vo-1",
        "--take", "take-2.wav", "--words", "take-2.words.json", "--insert-gap-after", "0.25",
    ])
    assert insert.handler(insert) == 0
    assert calls == [(
        "insert", "vo-2", "take-2.wav",
        {"words": "take-2.words.json", "after": "vo-1", "gap_after": 0.25, "text": None},
    )]

    calls.clear()
    checkout.version = 0
    scripted = parser.parse_args([
        "edit", "main", "--project", "p1", "--from-script", "script.json", "--takes", "takes", "--gaps",
    ])
    assert scripted.handler(scripted) == 0
    assert calls == [("script", "script.json", {"takes": "takes", "gaps": True})]


def test_repeated_gap_flags_and_script_gap_override_preserve_sdk_rules(monkeypatch) -> None:
    parser = timeline_cli.build_parser(object())
    calls = []
    checkout = _fake_checkout(calls)
    _bind_fake_edit(monkeypatch, checkout)
    parsed = parser.parse_args([
        "edit", "main", "--project", "p1", "--from-script", "script.json", "--takes", "takes", "--gaps",
        "--gap-after", "vo-1=0.2", "--gap-after", "vo-2=0.4",
    ])

    assert parsed.handler(parsed) == 0
    assert calls == [
        ("script", "script.json", {"takes": "takes", "gaps": True}),
        ("gap", "vo-1", 0.2),
        ("gap", "vo-2", 0.4),
    ]
