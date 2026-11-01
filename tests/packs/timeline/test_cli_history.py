"""Offline checks: timelines history and the TL@ref forms of show and diff, with a fake client injected.

Nothing here touches the Runtime or the workspace.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from astrid.packs.timeline import cli, cli_history
from astrid.sdk.contracts import DomainResult, ErrorObject

A = "authoring-parent-revision-aaaa1111"
B = "authoring-parent-revision-bbbb2222"
C = "astrid.editor.timeline.cccc3333"


class FakeTimelines:
    """Head A, which names B as parent; B names C; C names nothing. ``named=False``: no parent is named."""

    def __init__(self, *, named=True):
        self.parents = {A: B, B: C, C: None} if named else {A: None}
        self.opened = []

    def open_composition(self, project, ref, *, revision_id=None, limit=50, **_):
        self.opened.append(revision_id)
        rid = revision_id or A
        if rid not in self.parents:
            return DomainResult.failure(ErrorObject("not_found", "revision not found", {}))
        summary = {"head_revision_id": A, "revision_id": rid, "created_at": "2026-10-0%dT09:00:00Z" % (len(rid) % 9)}
        if self.parents[rid]:
            summary["parent_revision_id"] = self.parents[rid]
        return DomainResult.success({"summary": summary, "native_inspection": {}})


def _parsed(timelines, **extra):
    base = dict(client=SimpleNamespace(timelines=timelines), project="P", ref="TL", limit=50, json=False,
                published=False, from_revision=None, to_revision=None, from_version=None, to_version=None,
                revision_id=None, ref_revision=None)
    base.update(extra)
    return SimpleNamespace(**base)


def test_history_lists_newest_first_with_source_time_and_message(capsys):
    code = cli_history.run_history(_parsed(FakeTimelines()))
    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out[0].startswith("Revisions of TL")
    assert out[1].split()[0] == "aaaa1111"
    assert out[1].split()[1] == "astrid"
    assert out[2].split()[0] == "bbbb2222"
    assert out[3].split()[0] == "cccc3333"
    assert "app" in out[3]


def test_history_json_envelope(capsys):
    code = cli_history.run_history(_parsed(FakeTimelines(), json=True))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True
    assert payload["data"]["complete"] is True
    assert [r["id"] for r in payload["data"]["revisions"]] == [A, B, C]


def test_history_prints_the_head_and_says_earlier_revisions_cannot_be_listed(capsys):
    code = cli_history.run_history(_parsed(FakeTimelines(named=False)))
    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert out[1].split()[0] == "aaaa1111"  # the head row
    assert len(out) == 3
    assert out[2].startswith("earlier revisions: this Runtime cannot list them")
    assert "parent_revision_id" in out[2]


def test_history_json_when_the_runtime_cannot_list(capsys):
    code = cli_history.run_history(_parsed(FakeTimelines(named=False), json=True))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert [r["id"] for r in payload["data"]["revisions"]] == [A]
    assert payload["data"]["complete"] is False
    assert payload["data"]["reason"].startswith("earlier revisions: this Runtime cannot list them")


def test_history_json_complete_when_the_runtime_can_list(capsys):
    cli_history.run_history(_parsed(FakeTimelines(), json=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["complete"] is True
    assert payload["data"]["reason"] is None


def test_history_routes_through_the_cli_handler(capsys):
    code = cli._cmd_history(_parsed(FakeTimelines(), limit=2))
    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert len(lines) == 3  # header plus the two newest


def test_show_ref_resolves_prev_into_revision_id():
    timelines = FakeTimelines()
    parsed = _parsed(timelines, ref="TL@prev")
    assert cli_history.show_ref(parsed) is None
    assert parsed.ref == "TL"
    assert parsed.revision_id == B


def test_show_ref_head_keeps_the_working_head():
    parsed = _parsed(FakeTimelines(), ref="TL@head")
    assert cli_history.show_ref(parsed) is None
    assert parsed.ref == "TL" and parsed.revision_id is None


def test_show_ref_refuses_prev_when_no_parent_is_named(capsys):
    parsed = _parsed(FakeTimelines(named=False), ref="TL@prev")
    assert cli_history.show_ref(parsed) == 2
    assert "cannot list revisions" in capsys.readouterr().out


def test_diff_ref_sets_base_for_the_working_copy_and_from_for_published():
    working = _parsed(FakeTimelines(), ref="TL@-2")
    assert cli_history.diff_ref(working) is None
    assert working.ref == "TL" and working.ref_revision == C
    assert working.from_revision is None  # the working copy is the other side

    published = _parsed(FakeTimelines(), ref="TL@prev", published=True)
    assert cli_history.diff_ref(published) is None
    assert published.from_revision == B


def test_diff_ref_refuses_ref_plus_from(capsys):
    parsed = _parsed(FakeTimelines(), ref="TL@prev", published=True, from_revision=A)
    assert cli_history.diff_ref(parsed) == 2
    assert "not both" in capsys.readouterr().out
