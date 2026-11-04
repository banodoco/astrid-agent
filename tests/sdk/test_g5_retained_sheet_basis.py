"""Retained sheets keep publication identity through the public revision reader."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from astrid.sdk.contracts import DomainResult
from astrid.sdk.remote import RemoteTimelines
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline_sheet import find_version, sheet_version
from tests.sdk.test_timeline_commit_provider import DetachedRealm
from tests.sdk.test_timeline_commit_provider_document import document_fixture


class RetainedTimelines(RemoteTimelines):
    """Fake immutable transport reads; keep the public closure reader real."""

    def __init__(self, realm):
        super().__init__(object())
        self.realm = realm
        self.bundle_reads = []

    def _resolve_timeline(self, project, ref):
        assert (project, ref) == ("project-1", "film")
        return DomainResult.success({"project_id": project, "timeline_id": ref})

    def open_composition(self, project, ref, *, revision_id=None, **kwargs):
        return DomainResult.success({
            "native_inspection": {"project_id": project, "timeline_id": ref},
            "summary": {"revision_id": revision_id or self.realm.head["revision_id"],
                        "head_revision_id": self.realm.head["revision_id"]},
        })

    def open_bundle(self, project, ref, *, revision_id=None, publication_id=None):
        self.bundle_reads.append((revision_id, publication_id))
        return super().open_bundle(project, ref, revision_id=revision_id,
                                   publication_id=publication_id)

    def _typed(self, operation, *args, **kwargs):
        if operation == "list_timeline_commits":
            offset = int(kwargs.get("cursor") or 0)
            end = min(len(self.realm.entries), offset + kwargs["limit"], offset + 1)
            return DomainResult.success(copy.deepcopy({
                "head": self.realm.head, "items": self.realm.entries[offset:end],
                "next_cursor": str(end) if end < len(self.realm.entries) else None,
            }))
        project, owner, revision = args
        if operation == "get_project_parent_composition_revision":
            document = self.realm.documents[revision]
            return DomainResult.success(copy.deepcopy({
                **document["parent_revision"], "project_id": project, "timeline_id": owner,
                "dependency_manifest": document["dependency_manifest"],
            }))
        field, owner_key = {
            "get_project_shot_revision": ("shot_revisions", "shot_id"),
            "get_project_timeline_revision": ("internal_timeline_revisions", "timeline_id"),
        }[operation]
        row = next(row for document in self.realm.documents.values() for row in document[field]
                   if (row[owner_key], row["revision_id"]) == (owner, revision))
        return DomainResult.success(copy.deepcopy({**row, "project_id": project}))


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("accepted_basis", ["bootstrap", "accepted", "repeated"])
def test_find_version_preserves_exact_retained_publication(shared, accepted_basis):
    realm = DetachedRealm(document_fixture(shared=shared))
    timelines = RetainedTimelines(realm)
    client = SimpleNamespace(timelines=timelines)
    if accepted_basis != "bootstrap":
        realm.call([realm.move(on=2)])
    if accepted_basis == "repeated":
        previous = copy.deepcopy(realm.head)
        realm.call([realm.move(on=2)])
        assert realm.head["revision_id"] == previous["revision_id"]
        assert realm.head["publication_id"] != previous["publication_id"]
    original = Checkout.open("project-1", "film", client=client)
    pair = copy.deepcopy(realm.head)
    version = sheet_version(original)
    realm.call([realm.move(on=3)])
    realm.call([realm.move(ref="parent/clip:clip-b", on=7)])
    reopened = Checkout.open("project-1", "film", client=client)
    before = copy.deepcopy(realm.documents), copy.deepcopy(realm.entries)
    timelines.bundle_reads.clear()

    restored = find_version(reopened, version, client=client)

    assert restored is not None
    assert restored.document() == original.document()
    assert restored.bundle["source_mapping"] == original.bundle["source_mapping"]
    assert restored.bundle["base_parent"]["publication_id"] == pair["publication_id"]
    assert (pair["revision_id"], pair["publication_id"]) in timelines.bundle_reads
    assert before == (realm.documents, realm.entries)
