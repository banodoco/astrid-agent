"""Offline checks for composition revision refs: a fake store and a fake client, never the live Runtime."""

from __future__ import annotations

import pytest

from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.revisions import (
    Revision,
    RuntimeRevisions,
    resolve_revision,
    revision_source,
    split_ref,
)

A = "authoring-parent-revision-aaaa1111"
B = "authoring-parent-revision-bbbb2222"
C = "astrid.editor.timeline.cccc3333"
D = "restore-dddd4444"


def _rev(rid, parent=None):
    return Revision(id=rid, source=revision_source(rid), created_at="2026-10-01T00:00:00Z", parent=parent, message=None)


class FakeStore:
    """A chain newest first: chain[0] is the head. ``listable=False`` models a Runtime with no list route."""

    def __init__(self, chain, *, listable=True):
        self.chain = list(chain)
        self.listable = listable

    def head(self, project, timeline):
        return self.chain[0].id

    def get(self, project, timeline, revision_id):
        for rev in self.chain:
            if rev.id == revision_id:
                return rev
        raise TimelineEditError(f"no revision {revision_id!r}")

    def list(self, project, timeline, *, limit=50):
        if not self.listable:
            return None
        return self.chain[:limit]


def _chain():
    return [_rev(A), _rev(B), _rev(C), _rev(D)]


def test_source_by_prefix():
    assert revision_source(A) == "astrid"
    assert revision_source(C) == "app"
    assert revision_source(D) == "restore"
    assert revision_source("something-else") == "other"


def test_split_ref_forms():
    assert split_ref("TL@prev") == ("TL", "prev")
    assert split_ref("TL") == ("TL", None)
    assert split_ref("TL@-2") == ("TL", "-2")
    assert split_ref("a@b@head") == ("a@b", "head")
    assert split_ref("TL@") == ("TL@", None)
    assert split_ref("@prev") == ("@prev", None)


def test_head_and_none_resolve_to_no_explicit_id():
    store = FakeStore(_chain())
    assert resolve_revision("P", "TL", None, store=store) is None
    assert resolve_revision("P", "TL", "head", store=store) is None


def test_prev_is_the_head_parent_and_minus_n_walks_back():
    store = FakeStore(_chain())
    assert resolve_revision("P", "TL", "prev", store=store) == B
    assert resolve_revision("P", "TL", "-2", store=store) == C
    assert resolve_revision("P", "TL", "-3", store=store) == D


def test_minus_n_past_the_start_names_what_is_available():
    store = FakeStore(_chain()[:2])
    with pytest.raises(TimelineEditError, match="only 2 revision"):
        resolve_revision("P", "TL", "-5", store=store)


def test_prefix_matches_full_id_and_id_without_its_prefix():
    store = FakeStore(_chain())
    assert resolve_revision("P", "TL", "authoring-parent-revision-bb", store=store) == B
    assert resolve_revision("P", "TL", "cccc", store=store) == C
    assert resolve_revision("P", "TL", "restore-dd", store=store) == D


def test_ambiguous_prefix_is_refused():
    store = FakeStore([_rev(A), _rev("authoring-parent-revision-aaaa9999")])
    with pytest.raises(TimelineEditError, match="ambiguous"):
        resolve_revision("P", "TL", "authoring-parent-revision-aaaa", store=store)


def test_ref_that_does_not_resolve_names_the_available_revisions():
    store = FakeStore(_chain())
    with pytest.raises(TimelineEditError, match="no revision matches") as info:
        resolve_revision("P", "TL", "zzzz", store=store)
    assert A in str(info.value)


def test_bad_minus_ref_is_refused():
    store = FakeStore(_chain())
    with pytest.raises(TimelineEditError, match="not a revision ref"):
        resolve_revision("P", "TL", "-0", store=store)


def test_prev_on_a_store_that_cannot_list_says_so():
    store = FakeStore(_chain(), listable=False)
    with pytest.raises(TimelineEditError, match="cannot list revisions") as info:
        resolve_revision("P", "TL", "prev", store=store)
    assert "needs a revision that names its parent" in str(info.value)


def test_full_id_still_resolves_on_a_store_that_cannot_list():
    store = FakeStore(_chain(), listable=False)
    assert resolve_revision("P", "TL", B, store=store) == B
    with pytest.raises(TimelineEditError, match="only a full revision id resolves"):
        resolve_revision("P", "TL", "bbbb", store=store)


class FakeTimelines:
    """Stands in for the timelines client: open_composition returns a projection with a summary."""

    def __init__(self, revisions):
        self.revisions = dict(revisions)  # id -> parent id or None
        self.head_id = next(iter(self.revisions))
        self.calls = []

    def open_composition(self, project, ref, *, revision_id=None, limit=50, **_):
        self.calls.append(revision_id)
        rid = revision_id or self.head_id
        if rid not in self.revisions:
            return DomainResult.failure(ErrorObject("not_found", "revision not found", {}))
        summary = {"head_revision_id": self.head_id, "revision_id": rid, "message": "cut two"}
        if self.revisions[rid] is not None:
            summary["parent_revision_id"] = self.revisions[rid]
        return DomainResult.success({"summary": summary, "native_inspection": {}})


def test_runtime_list_walks_parent_links_from_head_and_stops_at_limit():
    timelines = FakeTimelines({A: B, B: C, C: None})
    store = RuntimeRevisions(timelines)
    walked = store.list("P", "TL", limit=10)
    assert [r.id for r in walked] == [A, B, C]
    assert walked[0].message == "cut two"
    assert [r.id for r in store.list("P", "TL", limit=2)] == [A, B]


def test_runtime_list_is_none_when_no_revision_names_its_parent():
    store = RuntimeRevisions(FakeTimelines({A: None}))
    assert store.list("P", "TL") is None


def test_runtime_list_stops_on_a_cycle():
    store = RuntimeRevisions(FakeTimelines({A: B, B: A}))
    assert [r.id for r in store.list("P", "TL", limit=50)] == [A, B]


def test_runtime_get_and_head_and_missing_revision():
    timelines = FakeTimelines({A: None})
    store = RuntimeRevisions(timelines)
    assert store.head("P", "TL") == A
    got = store.get("P", "TL", A)
    assert (got.id, got.source, got.parent) == (A, "astrid", None)
    with pytest.raises(TimelineEditError, match="no revision"):
        store.get("P", "TL", "authoring-parent-revision-missing")


def test_prev_through_runtime_adapter_uses_the_parent_walk():
    store = RuntimeRevisions(FakeTimelines({A: B, B: None}))
    assert resolve_revision("P", "TL", "prev", store=store) == B
    assert resolve_revision("P", "TL", "head", store=store) is None
    with pytest.raises(TimelineEditError, match="only 2 revision"):
        resolve_revision("P", "TL", "-2", store=store)


def test_runtime_prev_refuses_when_no_parent_is_named():
    store = RuntimeRevisions(FakeTimelines({A: None}))
    with pytest.raises(TimelineEditError, match="cannot list revisions"):
        resolve_revision("P", "TL", "prev", store=store)
