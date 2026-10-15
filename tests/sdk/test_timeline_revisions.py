"""Offline checks for composition revision refs: a fake store and a fake client, never the live Runtime."""

from __future__ import annotations

import pytest

from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.remote import RemoteTimelines
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
C = "reigh.editor.timeline.cccc3333"
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
            summary["previous_revision_id"] = self.revisions[rid]
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


def test_runtime_parent_history_refs_and_restore_use_generated_contract():
    initial = "authoring-parent-revision-initial"
    first_publish = "authoring-parent-revision-publish1"
    second_publish = "authoring-parent-revision-publish2"

    class GeneratedFixture:
        def __init__(self):
            self.calls = []
            self.restore_results = {}
            self.head = initial
            self.rows = [{"revision_id": initial, "created_at": "2026-10-01T00:00:00Z"}]

        def get_project_parent_composition_revision(self, project_id, timeline_id, revision):
            self.calls.append(("get_revision", project_id, timeline_id, revision))
            assert (project_id, timeline_id) == ("project-id", "timeline-id")
            return {
                "revision_id": revision,
                "payload": {"config": {"app": {"astrid": {"v": 0}}}},
            }

        def list_timelines(self, project_id, *, cursor=None, limit=50):
            return ([{"project_id": "project-id", "timeline_id": "timeline-id", "slug": "main"}], None)

        def get_project(self, project):
            assert project == "project-id"
            return {"id": "project-id", "project_id": "project-id"}

        def publish_parent_composition(self, project_id, timeline_id, publication, *, idempotency_key):
            self.calls.append(("publish", project_id, timeline_id, publication["expected_head"], idempotency_key))
            assert (project_id, timeline_id) == ("project-id", "timeline-id")
            assert publication["expected_head"] == self.head
            revision_id = first_publish if len(self.rows) == 1 else second_publish
            self.rows.append({"revision_id": revision_id, "created_at": f"2026-10-0{len(self.rows) + 2}T00:00:00Z"})
            self.head = revision_id
            return {"new_head": revision_id}

        def list_project_parent_composition_revisions(self, project_id, timeline_id, *, cursor=None, limit=50):
            self.calls.append(("list", project_id, timeline_id, cursor, limit))
            assert (project_id, timeline_id) == ("project-id", "timeline-id")
            rows = [
                {**row, "is_current_head": row["revision_id"] == self.head}
                for row in reversed(self.rows)
            ]
            return rows, None

        def restore_project_parent_composition_revision(
            self, project_id, timeline_id, revision, *, expected_head, idempotency_key
        ):
            self.calls.append(("restore", project_id, timeline_id, revision, expected_head, idempotency_key))
            assert (project_id, timeline_id) == ("project-id", "timeline-id")
            if idempotency_key in self.restore_results:
                return self.restore_results[idempotency_key]
            assert expected_head == self.head == second_publish
            result = {"new_head": f"restore-{revision}"}
            self.restore_results[idempotency_key] = result
            self.head = result["new_head"]
            return result

    from astrid.sdk.workspace_client import WorkspaceClient

    generated = GeneratedFixture()
    workspace = object.__new__(WorkspaceClient)
    workspace._generated = generated
    timelines = RemoteTimelines(workspace)
    store = RuntimeRevisions(timelines)

    first = workspace.publish_parent_composition(
        "project-id", "timeline-id", {"expected_head": initial}, idempotency_key="publish-one"
    )
    second = workspace.publish_parent_composition(
        "project-id", "timeline-id", {"expected_head": first["new_head"]}, idempotency_key="publish-two"
    )
    assert first["new_head"] == first_publish
    assert second["new_head"] == second_publish

    assert resolve_revision("project-id", "main", "prev", store=store) == first_publish
    assert resolve_revision("project-id", "main", "-2", store=store) == initial

    restored = store.restore(
        "project-id",
        "main",
        initial,
        expected_head=second_publish,
        idempotency_key="restore-key",
    )
    replayed = store.restore(
        "project-id",
        "main",
        initial,
        expected_head=second_publish,
        idempotency_key="restore-key",
    )
    assert restored.ok and restored.data == {"new_head": f"restore-{initial}"}
    assert replayed.ok and replayed.data == restored.data
    assert [call[0] for call in generated.calls].count("restore") == 2
    assert all(call[-2:] == (second_publish, "restore-key") for call in generated.calls if call[0] == "restore")


def test_runtime_walks_explicit_publication_ancestry_without_collapsing_repeats():
    class FakeRuntime:
        def head(self, project, timeline):
            return {'revision_id': A, 'publication_id': 'pub-3', 'updated_at': 'now'}

        def list_parent_composition_revisions(self, project, timeline, **kwargs):
            # Creation order is deliberately misleading; publication ancestry wins.
            return {'items': [
                {'revision_id': A, 'publication_id': 'pub-3', 'previous_revision_id': A,
                 'previous_publication_id': 'pub-2', 'is_current_head': True,
                 'parent_revision_id': 'not-ancestry', 'provenance': {'source': {'via': 'script'}}},
                {'revision_id': B, 'publication_id': 'pub-1'},
                {'revision_id': A, 'publication_id': 'pub-2', 'previous_revision_id': B,
                 'previous_publication_id': 'pub-1', 'restored_from_revision_id': C},
            ], 'next_cursor': None}

        def open_composition(self, *args, **kwargs):
            pytest.fail('compact head and list must not inspect')

    store = RuntimeRevisions(FakeRuntime())
    assert store.head('P', 'TL') == A
    walked = store.list('P', 'TL')
    assert [r.publication_id for r in walked] == ['pub-3', 'pub-2', 'pub-1']
    assert [r.id for r in walked] == [A, A, B]
    assert walked[0].parent == A
    assert walked[1].restored_from_revision_id == C
    assert walked[0].provenance == {'source': {'via': 'script'}}
    assert resolve_revision('P', 'TL', 'prev', store=store) == A
    assert resolve_revision('P', 'TL', '-2', store=store) == B
    assert resolve_revision('P', 'TL', 'aaaa', store=store) == A


def test_runtime_never_walks_publication_parent_revision_id_as_ancestry():
    class OldRuntime(FakeTimelines):
        def open_composition(self, *args, **kwargs):
            result = super().open_composition(*args, **kwargs)
            result.data['summary']['parent_revision_id'] = B
            return result
    assert RuntimeRevisions(OldRuntime({A: None})).list('P', 'TL') is None



def test_positional_ref_does_not_invent_unknown_migrated_publication_links():
    store = FakeStore([
        Revision(A, 'astrid', None, None, None, publication_id='pub-2'),
        Revision(B, 'astrid', None, None, None, publication_id='pub-1'),
    ])
    with pytest.raises(TimelineEditError, match='publication ancestry is unavailable'):
        resolve_revision('P', 'TL', 'prev', store=store)
