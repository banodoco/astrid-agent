"""Transactions: `with tl.edit(label) as tx` (API.md §5, §5.1) and publish resets the base (AUDIT bug 6)."""
from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path

import pytest

from astrid.sdk import timeline_checkout as tc
from astrid.sdk.timeline import store as _store
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.check import CheckReport
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.transaction import Plan

# tiny_publishable: schema 1, a music bed under both shots, and a pinned base (so changes() can see an edit)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing" / "tiny_publishable.json"


def tiny() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def draft(tmp_path, monkeypatch):
    """A working copy of the tiny fixture under a temp data root. The seams are fakes; nothing real is touched."""
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(tc, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    monkeypatch.setattr(tc, "fetch_bundle", lambda project, timeline, revision_id=None, client=None: copy.deepcopy(tiny()))
    monkeypatch.setattr(_store, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    monkeypatch.setattr(_store, "fetch_bundle", lambda project, timeline, revision_id=None, client=None: copy.deepcopy(tiny()))
    return Checkout.draft("P", "t")


def _path(tl: Checkout) -> Path:
    return Path(tl.path)


def _history(tl: Checkout) -> list[str]:
    """The undo steps' labels, oldest first (read without taking any back)."""
    return [_store._read_step(p)[0] for p in sorted(_store._history_dir(_path(tl)).glob("*.json"))]


def _x(tl: Checkout) -> object:
    return tl.clip("c1.rocket").get("x")


def test_two_edits_in_one_transaction_are_one_undo_step_that_restores_both(draft):
    base = draft.document()
    with draft.edit("two sets") as tx:
        draft.clip("c1.rocket").set(x=90)
        draft.clip("c1.rocket").set(y=44)
    assert tx.changed is True
    assert _history(draft) == ["two sets"]
    undone = draft.undo(1)
    assert undone == ["two sets"]
    assert draft.document() == base


def test_an_exception_restores_the_document_exactly_and_saves_nothing(draft):
    base, text = draft.document(), _path(draft).read_text(encoding="utf-8")
    with pytest.raises(RuntimeError, match="boom"):
        with draft.edit("doomed"):
            draft.clip("c1.rocket").set(x=90)
            raise RuntimeError("boom")
    assert draft.document() == base
    assert _path(draft).read_text(encoding="utf-8") == text
    assert _history(draft) == []
    assert draft._journal == []


def test_a_transaction_with_no_change_makes_no_step_and_no_save(draft, capsys):
    text = _path(draft).read_text(encoding="utf-8")
    with draft.edit("nothing") as tx:
        pass
    assert tx.changed is False
    assert tx.changes == []
    assert tx.report is None
    assert _history(draft) == []
    assert _path(draft).read_text(encoding="utf-8") == text
    assert "no change: nothing" in capsys.readouterr().out


def test_no_change_still_fills_a_plan_with_equal_digests(draft):
    with draft.edit("nothing") as tx:
        pass
    assert tx.plan is not None and tx.plan.changed is False
    assert tx.plan.digests["before"] == tx.plan.digests["after"]


def test_a_dry_run_saves_nothing_leaves_the_document_unchanged_and_fills_the_plan(draft, capsys):
    base, text = draft.document(), _path(draft).read_text(encoding="utf-8")
    with draft.edit("preview", dry_run=True) as tx:
        draft.clip("c1.rocket").set(x=90)
    assert tx.dry_run is True
    assert tx.changed is True
    assert tx.saved is None
    assert tx.plan.changes and list(tx.plan.changes) == tx.changes
    assert draft.document() == base
    assert _path(draft).read_text(encoding="utf-8") == text
    assert _history(draft) == []
    out = capsys.readouterr().out
    assert "dry run (nothing saved):" in out


def test_nested_edits_join_the_outer_transaction(draft):
    base = draft.document()
    with draft.edit("outer") as outer:
        with draft.edit("inner") as inner:
            assert inner is outer
            draft.clip("c1.rocket").set(x=90)
        draft.clip("c1.rocket").set(y=44)
    assert _history(draft) == ["outer"]
    assert draft.undo(1) == ["outer"]
    assert draft.document() == base


def test_an_exception_in_a_nested_edit_rolls_back_the_outer_one_too(draft):
    base = draft.document()
    with pytest.raises(RuntimeError):
        with draft.edit("outer"):
            draft.clip("c1.rocket").set(x=90)
            with draft.edit("inner"):
                draft.clip("c1.rocket").set(y=44)
                raise RuntimeError("inner failed")
    assert draft.document() == base
    assert _history(draft) == []


def test_plan_digests_differ_when_the_document_changed(draft):
    with draft.edit("move") as tx:
        draft.clip("c1.rocket").set(x=90)
    assert tx.plan.changed is True
    assert tx.plan.digests["before"].startswith("sha256:")
    assert tx.plan.digests["after"].startswith("sha256:")
    assert tx.plan.digests["before"] != tx.plan.digests["after"]


def test_plan_names_the_cuts_and_clips_it_touched(draft):
    with draft.edit("move", dry_run=True) as tx:
        draft.clip("c1.rocket").set(x=90)
    assert "c1" in tx.plan.cuts
    assert tx.plan.clips  # the clip ids the diff touched
    assert tx.plan.sweep == tx.plan.cuts


def test_plan_is_frozen_and_serializes_to_json(draft):
    with draft.edit("move", dry_run=True) as tx:
        draft.clip("c1.rocket").set(x=90)
    assert isinstance(tx.plan, Plan)
    with pytest.raises(dataclasses.FrozenInstanceError):
        tx.plan.label = "other"
    data = json.loads(json.dumps(tx.plan.to_json()))
    assert data["label"] == "move" and data["changed"] is True and data["digests"] == tx.plan.digests


def test_check_fills_the_report_when_asked_and_not_when_not(draft):
    with draft.edit("checked") as tx:
        draft.clip("c1.rocket").set(x=90)
    assert isinstance(tx.report, CheckReport)
    with draft.edit("unchecked", check=False) as tx2:
        draft.clip("c1.rocket").set(x=95)
    assert tx2.report is None


def test_a_saved_transaction_reports_the_path_it_saved(draft):
    with draft.edit("saved") as tx:
        draft.clip("c1.rocket").set(x=90)
    assert tx.saved == _path(draft)


def test_an_in_memory_transaction_is_not_saved_and_says_so(tmp_path, monkeypatch, capsys):
    tl = Checkout(tiny())
    with tl.edit("memory") as tx:
        tl.clip("c1.rocket").set(x=90)
    assert tx.changed is True and tx.saved is None
    assert "in memory" in capsys.readouterr().out


def test_two_api_calls_then_one_save_are_one_undo_step(draft):
    """AUDIT bug 10: a Python save with two edits used to be two undo steps."""
    base = draft.document()
    draft.clip("c1.rocket").set(x=90)
    draft.clip("c1.rocket").set(x=120)
    draft.save()
    assert len(_history(draft)) == 1
    assert _x(draft) == 120
    assert draft.undo(1)
    assert draft.document() == base
    with pytest.raises(TimelineEditError, match="nothing to undo"):
        draft.undo(1)


def test_two_saves_are_two_undo_steps(draft):
    draft.clip("c1.rocket").set(x=90)
    draft.save()
    draft.clip("c1.rocket").set(x=120)
    draft.save()
    assert len(_history(draft)) == 2
    draft.undo(1)
    assert _x(draft) == 90


def test_the_undo_label_joins_the_labels_of_the_edits_in_the_save(draft):
    draft.clip("c1.rocket").set(x=90)
    draft.clip("c1.rocket").set(y=44)
    draft.save()
    (label,) = _history(draft)
    assert "set" in label and "; " in label and len(label) <= 160


def test_publish_resets_the_base_so_nothing_is_left_to_publish(draft, monkeypatch):
    """AUDIT bug 6: after publish the working copy is the new head, with no changes and the new base."""
    with draft.edit("move"):
        draft.clip("c1.rocket").set(x=90)
    assert draft.changes() != []
    published: dict = {}

    def fake_publish(document, key, client=None, force=False):
        published["doc"] = copy.deepcopy(document)
        return {"old_head": "rev-0", "new_head": "rev-1", "structure": "changed"}

    def fake_fetch(project, timeline, revision_id=None, client=None):
        head = copy.deepcopy(published["doc"])  # the head is what was published: its base is its own content
        for key in ("source_mapping", "base_placements", "base_parent_payload"):
            head.pop(key, None)
        for shot in head["shots"].values():
            shot["base_internal_timeline"] = copy.deepcopy(shot.get("internal_timeline"))
            shot["base_payload"] = copy.deepcopy(shot.get("payload"))
        head["base_parent"] = {"revision_id": "rev-1"}
        return head

    monkeypatch.setattr(_store, "publish_bundle", fake_publish)
    monkeypatch.setattr(_store, "fetch_bundle", fake_fetch)
    monkeypatch.setattr(draft, "_publish_narration", lambda key, client=None: {})
    receipt = draft.publish("move the rocket")
    assert receipt["new_head"] == "rev-1"
    assert draft.changes() == []
    assert draft.base_revision == "rev-1"
    assert draft._journal == []
    assert _x(draft) == 90


def test_a_failed_head_reread_after_publish_keeps_the_old_base_and_says_so(draft, monkeypatch):
    with draft.edit("move"):
        draft.clip("c1.rocket").set(x=90)
    before_doc = draft.document()

    def fake_publish(document, key, client=None, force=False):
        return {"old_head": "rev-0", "new_head": "rev-1", "structure": "changed"}

    def broken_fetch(project, timeline, revision_id=None, client=None):
        raise RuntimeError("runtime unreachable")

    monkeypatch.setattr(_store, "publish_bundle", fake_publish)
    monkeypatch.setattr(_store, "fetch_bundle", broken_fetch)
    monkeypatch.setattr(draft, "_publish_narration", lambda key, client=None: {})
    receipt = draft.publish("move the rocket")  # the publish itself does not fail
    assert receipt["new_head"] == "rev-1"
    assert draft.document() == before_doc
    assert any("could not re-read the new head" in note for note in draft.notes)
