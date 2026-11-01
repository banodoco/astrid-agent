"""Public portable reads feed the existing canonical reader and commit provider."""
from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace

import pytest

from astrid.core.receipts.canonical import canonical_json
from astrid.core.receipts.contract import CommandReceipt
from astrid.sdk import timeline
from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.remote import RemoteTimelines
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.commands import CommandError
from astrid.sdk.timeline.commit import create_runtime_commit_service
from tests.sdk.test_timeline_commit_provider import (  # noqa: F401
    CommitRejected,
    DetachedPort,
    DetachedRealm,
    runtime_abi,
)
from tests.sdk.test_timeline_commit_provider_document import document_fixture


@pytest.fixture
def public_session(tmp_path, runtime_abi):  # noqa: F811
    realm = DetachedRealm(document_fixture(shared=True))
    sent = []

    class Reader(RemoteTimelines):
        def _resolve_timeline(self, *_args):
            return DomainResult.success({"project_id": "project-1", "timeline_id": "film"})

        def open_composition(self, *_args, revision_id=None, **_kwargs):
            return DomainResult.success({
                "native_inspection": {"project_id": "project-1", "timeline_id": "film"},
                "summary": {"head_revision_id": realm.head["revision_id"],
                            "revision_id": revision_id or realm.head["revision_id"]},
            })

        def _typed(self, operation, project, *args, **kwargs):
            if operation == "list_timeline_commits":
                return DomainResult.success({"head": copy.deepcopy(realm.head),
                                             "items": copy.deepcopy(realm.entries), "next_cursor": None})
            if operation == "commit_timeline":
                body = copy.deepcopy(args[1])
                sent.append(body)
                invocation = {"operation": "commitTimeline",
                              "path": "/v1/projects/project-1/timelines/film/commit",
                              "publisher": "actor-1", "request": body}
                context = {"request": body, "realm_id": "realm-1", "publisher": "actor-1",
                           "idempotency_key": kwargs["idempotency_key"],
                           "request_hash": hashlib.sha256(canonical_json(invocation).encode()).hexdigest()}
                # The existing detached port expects normalized optional annotations.
                context["request"].setdefault("rationale", "")
                try:
                    result = create_runtime_commit_service().commit(context, DetachedPort(realm, context))
                except CommitRejected as exc:
                    failure = exc.failure
                    return DomainResult.failure(ErrorObject(failure["code"], failure["message"], failure["details"]))
                if result["receipt"]:
                    # DetachedPort uses a scalar sequence range; the public DTO uses both endpoints.
                    sequence = result["receipt"]["project_seq"][0]
                    result["receipt"]["project_seq"] = [sequence, sequence]
                    realm.entries[-1]["receipt"] = copy.deepcopy(result["receipt"])
                receipt = CommandReceipt.from_dict(result["receipt"]) if result["receipt"] else None
                return DomainResult.success(result["data"], receipt=receipt)
            document = next(document for document in realm.documents.values()
                            if any(row["revision_id"] == args[-1] for row in [
                                document["parent_revision"], *document["shot_revisions"],
                                *document["internal_timeline_revisions"]]))
            rows = {"get_project_parent_composition_revision": [document["parent_revision"]],
                    "get_project_shot_revision": document["shot_revisions"],
                    "get_project_timeline_revision": document["internal_timeline_revisions"]}
            image = next(row for row in rows[operation] if row["revision_id"] == args[-1])
            return DomainResult.success({**copy.deepcopy(image), "project_id": project,
                **({"timeline_id": "film", "dependency_manifest": document["dependency_manifest"]}
                   if operation == "get_project_parent_composition_revision" else {})})

    reader = Reader(object())
    client = SimpleNamespace(timelines=reader, _remote=SimpleNamespace(_transport=SimpleNamespace(
        _last_handshake={"realm_id": "realm-1", "actor_id": "actor-1"})))
    path = tmp_path / "outbox.json"
    opened = timeline.open("project-1", "film", client=client, outbox_path=path)
    assert "_runtime_document" not in opened.bundle["source_mapping"]
    return opened, realm, sent, client, path


def rename(ref="parent", before="Renamed Film", value="Public edit"):
    return {"schema_version": 1, "op": "doc.patch", "version": "1", "select": None,
            "args": {"changes": [{"op": "set", "ref": ref, "path": ["name"],
                                  "before": {"state": "present", "value": before}, "value": value}]}}


def test_public_command_stays_private_and_commits_through_history(public_session):
    opened, realm, sent, client, path = public_session
    original = copy.deepcopy(realm.documents[realm.head["revision_id"]])
    durable = path.read_bytes()
    opened.command(rename(), label="Rename film", reads=[])
    opened.command(rename(before="Public edit", value="Second edit"), label="Rename again", reads=[])
    assert sent == [] and len(realm.entries) == 1
    assert path.read_bytes() == durable
    assert "_runtime_document" not in opened.bundle["source_mapping"]
    result = opened.commit("Two names", reads=[])
    assert result["data"]["status"] == "accepted"
    assert sent[0]["base_publication_id"] == "bootstrap"
    assert [step["command_ordinals"] for step in sent[0]["steps"]] == [[0], [1]]
    assert sent[0]["steps"][0]["reads"] == []
    assert result["data"]["canonical_document"]["shot_revisions"] == original["shot_revisions"]
    assert result["data"]["canonical_document"]["internal_timeline_revisions"] == original["internal_timeline_revisions"]
    assert realm.entries[-1]["records"]["label"] == "Two names"
    assert len(realm.entries[-1]["records"]["steps"]) == 2
    reopened = timeline.open("project-1", "film", client=client, outbox_path=path)
    assert reopened.bundle["parent"]["name"] == "Second edit"
    assert "_runtime_document" not in reopened.bundle["source_mapping"]
    reopened.command(rename(before="Second edit", value="After restart"))
    assert len(sent) == 1


def test_public_child_free_edit_hydrates_both_emitter_inputs(public_session):
    opened, realm, sent, _, _ = public_session
    for shot in opened.bundle["shots"].values():
        shot["payload"]["name"] = "Updated shared chapter"
    result = opened.commit("Rename shared chapter")
    assert result["data"]["status"] == "accepted"
    assert sent[0]["merge"] == "atomic" and "reads" not in sent[0]
    assert len(sent[0]["commands"][0]["args"]["changes"]) == 1
    assert len(realm.entries) == 2
    assert "_runtime_document" not in opened.bundle["source_mapping"]


def test_public_free_edit_cannot_broadcast_one_shared_alias(public_session):
    opened, realm, sent, _, _ = public_session
    next(iter(opened.bundle["shots"].values()))["payload"]["name"] = "Only this occurrence"
    with pytest.raises(TimelineEditError, match="contradictory authoring aliases"):
        opened.commit("One occurrence")
    assert sent == [] and len(realm.entries) == 1


def test_public_function_rehydrates_returned_conflict_pair(public_session):
    opened, realm, sent, _, _ = public_session
    evaluations = []
    realm.before_accept = lambda: realm.call([rename(value="Peer name")])

    def suffix(working):
        name = working.bundle["parent"]["name"]
        evaluations.append((working.head, name))
        working.command(rename(before=name, value=name + " reviewed"))

    result = opened.commit(suffix)
    assert result["data"]["status"] == "accepted"
    assert [name for _, name in evaluations] == ["Renamed Film", "Peer name"]
    assert evaluations[0][0]["publication_id"] == "bootstrap"
    assert evaluations[1][0] == {
        "revision_id": realm.entries[1]["revision_id"], "publication_id": realm.entries[1]["publication_id"]}
    assert [body["base_publication_id"] for body in sent] == ["bootstrap", realm.entries[1]["publication_id"]]
    assert opened.bundle["parent"]["name"] == "Peer name reviewed"
    assert "_runtime_document" not in opened.bundle["source_mapping"]


def test_public_patch_preview_keeps_original_pair_and_reports_would_status(public_session):
    opened, realm, sent, _, _ = public_session
    base = opened.head
    opened.command(rename(), reads=[])
    preview = opened.commit("Preview name", reads=[], dry_run=True)
    assert preview["data"]["status"] == "preview" and preview["receipt"] is None
    assert preview["data"]["would_status"] == "accepted"
    assert preview["data"]["steps"][0]["patch"] == sent[0]["steps"][0]["patch"]
    assert opened.head == base and len(realm.entries) == 1
    assert opened.commit("Accept name", reads=[])["data"]["status"] == "accepted"


@pytest.mark.parametrize("failure", ["pair", "foreign", "digest", "outside", "previous"])
def test_public_command_keeps_source_and_patch_guards(public_session, failure):
    opened, realm, sent, client, path = public_session
    command = rename()
    if failure == "pair":
        realm.entries[0]["revision_id"] = "another-revision"
    elif failure == "foreign":
        realm.documents[realm.head["revision_id"]]["project_id"] = "foreign"
        realm.entries[0]["receipt"]["result"]["canonical_document"] = realm.documents[realm.head["revision_id"]]
    elif failure == "digest":
        realm.documents[realm.head["revision_id"]]["parent_revision"]["content_digest"] = "sha256:" + "0" * 64
    elif failure == "outside":
        command = rename("shot-revision:outside", "Chapter One")
    else:
        command = rename(before="wrong previous value")
    with pytest.raises((TimelineEditError, CommandError)) as error:
        if failure in {"pair", "foreign", "digest"}:
            fresh_session = timeline.open("project-1", "film", client=client, outbox_path=path)
            fresh_session.command(command)
        else:
            opened.command(command)
    if failure in {"outside", "previous"}:
        assert error.value.kind == {"outside": "outside_closure", "previous": "previous_value_mismatch"}[failure]
    assert sent == [] and len(realm.entries) == 1 and opened._commands == []
