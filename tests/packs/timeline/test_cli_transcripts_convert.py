"""timelines transcripts refresh --convert: one guarded commit of the published head."""
from __future__ import annotations

from argparse import Namespace

import pytest

from astrid.packs.timeline import cli_working
from astrid.sdk import timeline_transcripts as transcripts
from astrid.sdk.timeline_checkout import Checkout
from tests.sdk.test_timeline_take_transcripts import PROJECT, _bundle, _Store


class _Published(Checkout):
    published = None

    def publish(self, message, *, client=None, **kw):
        _Published.published = message
        return {"new_head": "rev-9"}


def _parsed(**kw):
    base = dict(ref="t", project=PROJECT, client=object(), convert=True, dry_run=False, run_missing=False,
                use_source=None, use_document=None, shot=None, placement=None, draft=None)
    return Namespace(**{**base, **kw})


@pytest.fixture
def opened(monkeypatch):
    store = _Store(monkeypatch)
    tl = _Published(_bundle())
    monkeypatch.setattr(Checkout, "open", classmethod(lambda cls, *a, **k: tl))
    monkeypatch.setattr(cli_working, "working_copy", lambda parsed, create: (None, False))
    _Published.published = None
    return tl, store


def test_dry_run_prints_the_plan_and_commits_nothing(opened, capsys):
    tl, store = opened
    assert cli_working._convert_clip_words(_parsed(dry_run=True), PROJECT) == 0
    out = capsys.readouterr().out
    assert "would convert" in out and "nothing written, nothing published" in out
    assert store.docs == {} and _Published.published is None and not transcripts.is_converted(tl.bundle)


def test_convert_commits_once_and_marks_the_timeline(opened, capsys):
    tl, store = opened
    assert cli_working._convert_clip_words(_parsed(), PROJECT) == 0
    assert _Published.published == "Convert clip words to narration transcripts"
    assert len(store.docs) == 2 and transcripts.is_converted(tl.bundle)
    assert "narration sync is off" in capsys.readouterr().out
    # a second run says so and commits nothing more
    _Published.published = None
    assert cli_working._convert_clip_words(_parsed(), PROJECT) == 0
    assert _Published.published is None and "already converted" in capsys.readouterr().out


def test_convert_refuses_a_working_copy_with_unpublished_edits(monkeypatch):
    draft = type("D", (), {"edits": lambda self: {"changes": ["a-vo"]}})()
    monkeypatch.setattr(cli_working, "working_copy", lambda parsed, create: (draft, True))
    with pytest.raises(cli_working.VerbError, match="working copy with unpublished edits"):
        cli_working._convert_clip_words(_parsed(), PROJECT)


def test_the_flags_are_parsed():
    from astrid.core.cli.domain_timelines import build_parser

    parser = build_parser(None)
    refresh = parser.parse_args(["transcripts", "refresh", "t", "--project", "p", "--convert", "--dry-run"])
    assert refresh.convert is True and refresh.dry_run is True
    run = parser.parse_args(["transcripts", "run", "sha256:" + "a" * 64, "--project", "p", "--language", "auto", "--prompt", "Astrid"])
    assert (run.language, run.prompt) == ("auto", "Astrid")
