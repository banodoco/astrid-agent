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


def _history_item(publication_id, revision_id, previous=None, *, label=None, source=None):
    item = {
        "publication_id": publication_id,
        "revision_id": revision_id,
        "previous": previous,
        "receipt": {"created_at": f"{publication_id}-time"},
        "intent": "recorded",
        "records": {},
        "provider_version": "astrid-v1",
        "compiler_version": "compiler-v1",
        "publisher": "actor-1",
    }
    if label is not None:
        item["label"] = label
    if source is not None:
        item["source"] = {"via": source}
    return item


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
    with pytest.raises(TimelineEditError, match="cyclic"):
        store.list("P", "TL", limit=50)


def test_runtime_get_and_head_and_missing_revision():
    timelines = FakeTimelines({A: None})
    store = RuntimeRevisions(timelines)
    assert store.head("P", "TL") == A
    got = store.get("P", "TL", A)
    assert (got.id, got.source, got.parent) == (A, "astrid", None)
    with pytest.raises(TimelineEditError, match="no revision"):
        store.get("P", "TL", "authoring-parent-revision-missing")


@pytest.mark.parametrize("returned_id", [None, B])
def test_runtime_get_requires_exact_loaded_revision_identity(returned_id):
    class Timelines:
        def open_composition(self, project, timeline, *, revision_id=None, **_):
            summary = {"head_revision_id": A}
            if returned_id is not None:
                summary["revision_id"] = returned_id
            return DomainResult.success({"summary": summary})

    with pytest.raises(TimelineEditError, match="did not prove requested revision"):
        RuntimeRevisions(Timelines()).get("P", "TL", A)


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


def test_runtime_commit_history_keeps_same_content_publications_and_walks_edges():
    same = "authoring-parent-revision-same"
    later = "authoring-parent-revision-later"

    class CommitHistory:
        def __init__(self):
            self.calls = []

        def list_timeline_commits(self, project, timeline, *, cursor=None, limit=50):
            self.calls.append((project, timeline, cursor, limit))
            return DomainResult.success({
                # The frozen Runtime route returns acceptance order. The
                # adapter must expose newest-first refs without deduplicating
                # the two accepted writes of identical content.
                "items": [
                    _history_item("pub-0", same),
                    _history_item("pub-1", same, {"publication_id": "pub-0", "revision_id": same},
                                  label="same value"),
                    _history_item("pub-2", later, {"publication_id": "pub-1", "revision_id": same}, source="app"),
                ],
                "next_cursor": None,
                "head": {"revision_id": later, "publication_id": "pub-2"},
            })

        def open_composition(self, *args, **kwargs):
            pytest.fail("commit history must not reload a moving composition head")

    runtime = CommitHistory()
    store = RuntimeRevisions(runtime)
    revisions = store.list("P", "TL")

    assert [(revision.publication_id, revision.id) for revision in revisions] == [
        ("pub-2", later), ("pub-1", same), ("pub-0", same)
    ]
    assert revisions[1].message == "same value"
    assert revisions[0].source == "app"
    assert resolve_revision("P", "TL", "prev", store=store) == same
    assert resolve_revision("P", "TL", "-2", store=store) == same
    assert resolve_revision("P", "TL", "same", store=store) == same
    assert runtime.calls


def test_runtime_commit_history_does_not_bypass_public_client_for_private_generated_method():
    class Generated:
        def list_timeline_commits(self, project, timeline, *, cursor=None, limit=50):
            pytest.fail("private generated client must not bypass the public adapter")

    class Wrapped:
        _client = type("Workspace", (), {"_generated": Generated()})()

        def open_composition(self, project, timeline, *, revision_id=None, **_):
            assert (project, timeline) == ("project", "main")
            rid = revision_id or A
            parent = B if rid == A else None
            summary = {"revision_id": rid, "head_revision_id": A}
            if parent is not None:
                summary["parent_revision_id"] = parent
            return DomainResult.success({"summary": summary})

    store = RuntimeRevisions(Wrapped())
    assert store.head("project", "main") == A
    assert [revision.id for revision in store.list("project", "main")] == [A, B]


def test_runtime_commit_history_error_does_not_fall_back_to_parent_links():
    class Client:
        def history(self, project, timeline, *, cursor=None, limit=50):
            return DomainResult.failure(ErrorObject("unavailable", "immutable history is unavailable", {}))

        def open_composition(self, *args, **kwargs):
            pytest.fail("a present immutable history route must not fall back")

    with pytest.raises(TimelineEditError, match="could not list revisions.*unavailable"):
        RuntimeRevisions(Client()).list("P", "TL")


def test_frozen_history_rejects_duplicate_publication_ids():
    class Client:
        def list_timeline_commits(self, *args, **kwargs):
            return {
                "items": [_history_item("pub", A), _history_item("pub", B)],
                "next_cursor": None,
                "head": {"revision_id": B, "publication_id": "pub"},
            }

    with pytest.raises(TimelineEditError, match="repeated publication"):
        RuntimeRevisions(Client()).list("P", "TL")


def test_frozen_history_rejects_publication_cycle():
    class Client:
        def list_timeline_commits(self, *args, **kwargs):
            return {
                "items": [
                    _history_item("pub-a", A, {"publication_id": "pub-b", "revision_id": B}),
                    _history_item("pub-b", B, {"publication_id": "pub-a", "revision_id": A}),
                ],
                "next_cursor": None,
                "head": {"revision_id": B, "publication_id": "pub-b"},
            }

    with pytest.raises(TimelineEditError, match="cycle"):
        RuntimeRevisions(Client()).list("P", "TL")


def test_frozen_history_rejects_changing_head_and_cursor_loop():
    class Client:
        def __init__(self, pages):
            self.pages = pages

        def list_timeline_commits(self, project, timeline, *, cursor=None, limit=50):
            return self.pages[cursor]

    first = {
        "items": [_history_item("pub-a", A)],
        "next_cursor": "next",
        "head": {"revision_id": A, "publication_id": "pub-a"},
    }
    second = {
        "items": [_history_item("pub-b", B, {"publication_id": "pub-a", "revision_id": A})],
        "next_cursor": None,
        "head": {"revision_id": B, "publication_id": "pub-b"},
    }
    with pytest.raises(TimelineEditError, match="changed its head"):
        RuntimeRevisions(Client({None: first, "next": second})).list("P", "TL")

    loop = dict(first, head={"revision_id": A, "publication_id": "pub-a"}, next_cursor="next")
    with pytest.raises(TimelineEditError, match="cursor loop"):
        RuntimeRevisions(Client({None: loop, "next": loop})).list("P", "TL")


def test_frozen_history_rejects_missing_ancestor_instead_of_guessing_order():
    class Client:
        def list_timeline_commits(self, *args, **kwargs):
            return {
                "items": [_history_item("pub-b", B, {"publication_id": "missing", "revision_id": A})],
                "next_cursor": None,
                "head": {"revision_id": B, "publication_id": "pub-b"},
            }

    with pytest.raises(TimelineEditError, match="missing or conflicting ancestor"):
        RuntimeRevisions(Client()).list("P", "TL")
