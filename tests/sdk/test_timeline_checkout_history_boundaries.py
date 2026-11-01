"""Accepted boundaries and failed local restores preserve unsent intent safely."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline import publish, store
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline_sheet import find_version, sheet_version
from tests.sdk.test_timeline_transaction import draft  # noqa: F401


def edit(draft, value):
    draft.clip("c1.rocket").set(x=value)
    draft.save(quiet=True)


def test_new_unsent_steps_stop_before_the_accepted_boundary(draft, monkeypatch):
    edit(draft, 40)
    accepted = copy.deepcopy(draft.bundle)
    accepted["base_parent"]["revision_id"] = "accepted-B"
    monkeypatch.setattr(store, "fetch_bundle", lambda *a, **kw: copy.deepcopy(accepted))
    draft._reset_base(client=object())
    base = draft.document()
    edit(draft, 99)
    edited = draft.document()
    assert len(draft.undo(100)) == 1
    assert draft.document() == base
    with pytest.raises(TimelineEditError, match="no unsent draft edit"):
        draft.undo()
    draft.redo()
    assert draft.document() == edited


@pytest.mark.parametrize("redo", [False, True])
def test_return_to_retained_revision_does_not_reopen_old_local_history(draft, monkeypatch, redo):
    original = copy.deepcopy(draft.bundle)
    edit(draft, 40)
    edit(draft, 99)
    version = sheet_version(draft)
    draft._exported = {version: copy.deepcopy(draft.bundle)}
    if redo:
        draft.undo()
    accepted = copy.deepcopy(draft.bundle)
    accepted["base_parent"]["revision_id"] = "accepted-B"
    monkeypatch.setattr(store, "fetch_bundle", lambda *a, **kw: copy.deepcopy(accepted))
    draft._reset_base(client=object())
    # Runtime restore reuses the retained source revision; the new publication
    # identity is absent from Checkout's base_parent (revision+digest only).
    monkeypatch.setattr(store, "fetch_bundle", lambda *a, **kw: copy.deepcopy(original))
    draft._reset_base(client=object())
    for checkout in (draft, Checkout.load(draft.path)):
        with pytest.raises(TimelineEditError, match="no unsent draft edit"):
            (checkout.redo if redo else checkout.undo)()
        assert find_version(checkout, version) is None


@pytest.mark.parametrize("failure", ["read", "write"])
def test_successful_publish_with_failed_reset_does_not_reopen_old_steps(draft, monkeypatch, failure):
    edit(draft, 40)
    edit(draft, 99)
    accepted = []

    def commit_document(candidate, message, *, idempotency_key=None, client=None):
        accepted.append(copy.deepcopy(candidate))
        return {"new_head": "accepted-B"}

    def unavailable(*args, **kwargs):
        raise OSError("injected reset failure")

    def head(*args, **kwargs):
        bundle = copy.deepcopy(accepted[0])
        bundle["base_parent"]["revision_id"] = "accepted-B"
        return bundle

    monkeypatch.setattr(draft, "_commit_document", commit_document)
    monkeypatch.setattr(store, "fetch_bundle", unavailable if failure == "read" else head)
    if failure == "write":
        monkeypatch.setattr(draft, "_save_locked", unavailable)
    monkeypatch.setattr(draft, "_check_core", lambda: SimpleNamespace(valid=True))
    monkeypatch.setattr(draft, "_publish_narration", lambda *a, **kw: {})
    monkeypatch.setattr(publish._transcripts, "plan_narration_transcripts", lambda *a: [])
    assert draft.publish(client=object())["new_head"] == "accepted-B"
    assert len(accepted) == 1
    warning = "could not re-read" if failure == "read" else "could not be reset"
    assert any(warning in note for note in draft.notes)
    if failure == "write":
        monkeypatch.delattr(draft, "_save_locked")
    for checkout in (draft, Checkout.load(draft.path)):
        with pytest.raises(TimelineEditError, match="no unsent draft edit"):
            checkout.undo()
    checkout = Checkout.load(draft.path)
    stale = checkout.document()
    edit(checkout, 120)
    checkout.undo()
    assert checkout.document() == stale
    checkout.redo()
    assert checkout.clip("c1.rocket").get("x") == 120


@pytest.mark.parametrize("operation", ["undo", "redo"])
@pytest.mark.parametrize("failure", ["write", "replace"])
def test_failed_draft_write_preserves_restore_sources(draft, monkeypatch, operation, failure):
    edit(draft, 40)
    edit(draft, 99)
    if operation == "redo":
        draft.undo(2)
    folder = store._redo_dir if operation == "redo" else store._history_dir
    history = {p: p.read_bytes() for p in folder(draft.path).glob("*.json")}
    original = draft.path.read_bytes()
    write = Path.write_text
    replace = os.replace

    def fail_write(path, *args, **kwargs):
        if path.parent == draft.path.parent and path.name.endswith(".tmp"):
            write(path, "partial", encoding="utf-8")
            raise OSError("injected draft write failure")
        return write(path, *args, **kwargs)

    def fail_target(source, target):
        if target == draft.path:
            raise OSError("injected draft write failure")
        return replace(source, target)
    with monkeypatch.context() as fault:
        if failure == "write":
            fault.setattr(Path, "write_text", fail_write)
        else:
            fault.setattr(os, "replace", fail_target)
        with pytest.raises(OSError, match="injected"):
            getattr(draft, operation)(2)
    assert draft.path.read_bytes() == original
    assert {p: p.read_bytes() for p in folder(draft.path).glob("*.json")} == history
    reverse = store._history_dir if operation == "redo" else store._redo_dir
    assert list(reverse(draft.path).glob("*")) == []
    assert not list(draft.path.parent.glob(".*.tmp"))
    reloaded = Checkout.load(draft.path)
    assert len(getattr(reloaded, operation)(2)) == 2
    inverse = "undo" if operation == "redo" else "redo"
    assert len(getattr(reloaded, inverse)(2)) == 2
    assert reloaded.path.read_bytes() == original
