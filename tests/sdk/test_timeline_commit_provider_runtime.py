"""Real Runtime custody of provider reservations; run in the integration lane."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("selected", [False, True])
def test_public_undo_redo_persists_v2_effect_bindings(tmp_path, selected):
    use_runtime()
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider

    from astrid.sdk import timeline
    from astrid.sdk.contracts import DomainResult
    from astrid.sdk.remote import RemoteTimelines
    from astrid.sdk.timeline.capture import capture_track_create
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.workspace_client import WorkspaceClient

    root = tmp_path / "public-undo-realm"
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    service.register_timeline_commit_provider(load_timeline_commit_provider("astrid"))
    project = service.create_project({"slug": "public-undo", "name": "Public undo"})["id"]
    service.create_timeline(project, "film", idempotency_key="birth")
    generated = SimpleNamespace()
    transport = WorkspaceClient.__new__(WorkspaceClient)
    transport._generated = generated
    transport._last_handshake = {"realm_id": service.realm["id"], "actor_id": "editor"}
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=transport), timelines=RemoteTimelines(transport))
    client.timelines._resolve_timeline = lambda p, t: DomainResult.success({"project_id": p, "timeline_id": t})
    client.timelines.open_composition = lambda p, t: DomainResult.success({
        "native_inspection": {"project_id": p, "timeline_id": t},
        "summary": {"head_revision_id": service.get_project_timeline(p, t)["head_revision_id"]}})
    client.timelines.open_bundle = lambda p, t, revision_id=None, **kw: DomainResult.success({
        "bundle": bundle_from_document(service._timeline_commits()[0].revision_closure(p, t, revision_id))})
    generated.list_timeline_commits = lambda p, t, cursor=None, limit=200: service.list_timeline_commits(
        p, t, cursor=cursor, limit=limit, authorize=lambda: None)
    wires = []

    def send(p, t, body, *, idempotency_key):
        result = service.commit_timeline(p, t, body, idempotency_key=idempotency_key, publisher="editor",
            invocation_path=f"/v1/projects/{p}/timelines/{t}/commit", authorize=lambda: None)
        wires.append({"body": copy.deepcopy(body), "key": idempotency_key, "response": copy.deepcopy(result)})
        return result

    generated.commit_timeline = send
    outbox = tmp_path / "public-outbox.json"

    def opened():
        return timeline.open(project, "film", client=client, outbox_path=outbox)

    try:
        tl = opened()
        for track in ("one", "two"):
            tl.command(capture_track_create(tl.bundle, "parent", {"id": track, "kind": "visual"}),
                       label=f"Create {track}")
        born = tl.commit("Create tracks")
        tl = opened()
        for track in ("one", "two"):
            tl.command({"schema_version": 1, "op": "track.set", "version": "1",
                        "select": f"parent/track:{track}",
                        "args": {"values": {"label": track.title()}, "clear": []}}, label=f"Label {track}")
        forward = tl.commit("Label tracks")
        publication = forward["receipt"]["receipt_id"]
        selector = {publication: ["step-1"]} if selected else None
        tl = opened()
        preview = tl.undo(publications=[publication], steps=selector, dry_run=True, idempotency_key="preview")
        assert preview["data"]["would_status"] == "accepted" and preview["receipt"] is None
        assert service.get_project_timeline(project, "film")["head_publication_id"] == publication
        undone = tl.undo(publications=[publication], steps=selector, idempotency_key="undo")
        tracks = undone["data"]["canonical_document"]["parent_revision"]["payload"]["config"]["tracks"]
        assert "label" not in tracks[0]
        if selected:
            assert tracks[1]["label"] == "Two"
        else:
            assert undone["data"]["canonical_document"] == born["data"]["canonical_document"]
        original_wire = copy.deepcopy(wires[-1])
        assert generated.commit_timeline(project, "film", original_wire["body"], idempotency_key="undo") == original_wire["response"]
        redone = opened().undo(publications=[undone["receipt"]["receipt_id"]], idempotency_key="redo")
        assert redone["data"]["canonical_document"] == forward["data"]["canonical_document"]
        history = service.list_timeline_commits(project, "film", authorize=lambda: None)["items"]
        assert len(history) == 5  # Public birth, tracks, labels, undo, redo; preview/retry do not append.
        inverse_entry = history[-2]
        if selected:
            assert inverse_entry["records"]["undo_steps"] == selector
        source_records = history[-3]["records"]["commands"][:1 if selected else 2]
        source_reads = {(read["ref"], tuple(read["path"])) for record in source_records for read in record["reads"]}
        inverse_reads = {(read["ref"], tuple(read["path"])) for read in inverse_entry["records"]["commands"][0]["reads"]}
        assert source_reads and source_reads <= inverse_reads
        for entry in history[-2:]:
            records = entry["records"]
            step = records["steps"][0]
            assert step["id"] == "inverse" and step["command_ordinals"] == [0]
            assert step["patch"] and step["dependencies"]
            command = records["commands"][0]
            assert command["reads"]
            bindings = [binding for binding in command["bindings"] if binding["key"] == "commit.effect"]
            assert {(binding["effect_ordinal"], binding["boundary"]["side"]) for binding in bindings} == {
                (ordinal, side) for ordinal in range(len(step["patch"])) for side in ("before", "after")}
            assert all(binding["step_id"] == "inverse" for binding in bindings)
        for wire in wires[2:]:
            body = wire["body"]
            assert body["label"] == "Undo timeline edit" and body["merge"] == "fields"
            assert body["steps"][0]["id"] == "inverse" and body["steps"][0]["patch"] == []
            assert "reads" not in body and "reads" not in body["steps"][0]
            assert body["source"] == {"via": "python"}
            assert "rationale" not in body and "group_id" not in body
            response = wire["response"]
            assert response["data"]["status"] in {"accepted", "preview"}
            if response["receipt"] is not None:
                assert response["receipt"]["result"] == response["data"]
    finally:
        service.close()


def test_structural_reservations_and_receipt_retry_survive_runtime_restart(tmp_path, monkeypatch):
    use_runtime()
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_port import route_path

    from astrid.sdk.timeline import commands, commit

    root = tmp_path / "realm"
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    service.register_timeline_commit_provider(commit.create_runtime_commit_service())
    project = service.create_project({"slug": "reservations", "name": "Reservations"})["id"]
    service.create_timeline(project, "film", idempotency_key="create-film")

    def head():
        resource = service.get_project_timeline(project, "film")
        return {"revision_id": resource["head_revision_id"],
                "publication_id": resource["head_publication_id"]}

    def request(calls, *, dry_run=False):
        current = head()
        return {"api_version": 1, "project_id": project, "timeline_id": "film",
                "base_revision_id": current["revision_id"],
                "base_publication_id": current["publication_id"], "commands": calls,
                "dry_run": dry_run}

    def submit(body, key):
        return service.commit_timeline(
            project, "film", body, idempotency_key=key, publisher="author",
            invocation_path=route_path(project, "film"), authorize=lambda: None,
        )

    def inverse(publication):
        return {"schema_version": 1, "op": "timeline.inverse", "version": "1",
                "select": None, "args": {"publications": [publication]}}

    create = {"schema_version": 1, "op": "track.create", "version": "1", "select": "parent",
              "args": {"row": {"id": "stable-track", "kind": "visual", "opaque": [None, False]}}}
    try:
        initial = head()
        preview = submit(request([create], dry_run=True), "preview")
        assert preview["receipt"] is None and head() == initial
        original_request = request([create])
        created = submit(original_request, "original-create")
        occupied = head()
        with pytest.raises(RuntimeErrorBase) as error:
            submit(request([create]), "occupied-create")
        assert error.value.code == "conflict" and error.value.status == 409
        assert error.value.details["reason"] == "identity_collision" and head() == occupied
        deleted = submit(request([inverse(created["receipt"]["receipt_id"])]), "undo-create")
        assert deleted["data"]["canonical_document"]["parent_revision"]["payload"][
            "config"]["tracks"] == []
        retired = head()
        service.close()
        service = RuntimeService(root)
        service.register_timeline_commit_provider(commit.create_runtime_commit_service())
        assert head() == retired

        def forbidden(*args, **kwargs):
            raise AssertionError("receipt retry executed provider semantics or reservation checks")

        with monkeypatch.context() as retry:
            retry.setattr(commands, "execute", forbidden)
            retry.setattr(commit, "_check_reservations", forbidden)
            assert submit(original_request, "original-create") == created
        assert head() == retired
        with pytest.raises(RuntimeErrorBase) as error:
            submit(request([copy.deepcopy(create)]), "new-create-after-restart")
        assert error.value.code == "conflict" and error.value.status == 409
        assert error.value.details["reason"] == "identity_collision" and head() == retired
        restored = submit(request([inverse(deleted["receipt"]["receipt_id"])]), "redo-create")
        assert restored["data"]["canonical_document"] == created["data"]["canonical_document"]
        history = service.list_timeline_commits(project, "film", authorize=lambda: None)
        assert len(history["items"]) == 4  # Bootstrap, create, inverse-delete, inverse-restore.
    finally:
        service.close()


def test_restore_inverse_redo_and_exact_retry_survive_runtime_restart(tmp_path, monkeypatch):
    use_runtime()
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_port import route_path

    from astrid.sdk.timeline import commands, commit

    root = tmp_path / "restore-realm"
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    service.register_timeline_commit_provider(commit.create_runtime_commit_service())
    project = service.create_project({"slug": "restore", "name": "Restore"})["id"]
    service.create_timeline(project, "film", idempotency_key="create-film")

    def head():
        resource = service.get_project_timeline(project, "film")
        return {"revision_id": resource["head_revision_id"],
                "publication_id": resource["head_publication_id"]}

    def request(calls, *, dry_run=False):
        current = head()
        return {"api_version": 1, "project_id": project, "timeline_id": "film",
                "base_revision_id": current["revision_id"],
                "base_publication_id": current["publication_id"], "commands": calls,
                "dry_run": dry_run}

    def submit(body, key):
        return service.commit_timeline(
            project, "film", body, idempotency_key=key, publisher="author",
            invocation_path=route_path(project, "film"), authorize=lambda: None,
        )

    def inverse(publication):
        return {"schema_version": 1, "op": "timeline.inverse", "version": "1",
                "select": None, "args": {"publications": [publication]}}

    try:
        initial = head()
        create = {"schema_version": 1, "op": "track.create", "version": "1", "select": "parent",
                  "args": {"row": {"id": "historical-track", "kind": "visual",
                                    "unknown": {"literal": "historical-track", "value": None}}}}
        created = submit(request([create]), "create-track")
        historical = created["data"]["canonical_document"]
        populated = head()
        original_body = {"expected_head": populated["revision_id"],
                         "expected_publication_id": populated["publication_id"]}
        path = route_path(project, "film", "composition-revisions/" + initial["revision_id"] + "/restore")
        restored = service.restore_project_parent_composition_revision(
            project, "film", initial["revision_id"], original_body,
            idempotency_key="restore-original", publisher="author", invocation_path=path,
            authorize=lambda: None,
        )
        empty = restored["data"]["canonical_document"]
        assert restored["data"]["accepted_revision_id"] == initial["revision_id"]
        assert restored["data"]["status"] == "accepted" and restored["data"]["rebased_onto"] is None
        assert empty["parent_revision"]["payload"]["config"]["tracks"] == []
        history = service.list_timeline_commits(project, "film", authorize=lambda: None)
        record = history["items"][-1]["records"]
        assert record["restored_from"] == {key: empty["parent_revision"][key]
                                            for key in ("revision_id", "content_digest")}
        assert record["commands"][0]["writes"][0]["ref"] == "timeline"
        assert record["commands"][0]["created"] == []
        assert restored["receipt"]["command_kind"] == "timeline.commit"
        assert restored["receipt"]["result"] == restored["data"]
        service.close()
        service = RuntimeService(root)
        service.register_timeline_commit_provider(commit.create_runtime_commit_service())
        retired = head()

        def forbidden(*args, **kwargs):
            raise AssertionError("exact restore retry executed provider semantics")

        with monkeypatch.context() as retry:
            retry.setattr(commands, "execute", forbidden)
            assert service.restore_project_parent_composition_revision(
                project, "film", initial["revision_id"], original_body,
                idempotency_key="restore-original", publisher="author", invocation_path=path,
                authorize=lambda: None,
            ) == restored
        assert head() == retired
        with pytest.raises(RuntimeErrorBase) as error:
            submit(request([create]), "retired-id")
        assert error.value.details["reason"] == "identity_collision" and head() == retired
        undone = submit(request([inverse(restored["receipt"]["receipt_id"])]), "undo-restore")
        assert undone["data"]["canonical_document"] == historical
        redone = submit(request([inverse(undone["receipt"]["receipt_id"])]), "redo-restore")
        assert redone["data"]["canonical_document"] == empty
        stale_body = {"expected_head": initial["revision_id"],
                      "expected_publication_id": initial["publication_id"]}
        with pytest.raises(RuntimeErrorBase) as error:
            service.restore_project_parent_composition_revision(
                project, "film", initial["revision_id"], stale_body,
                idempotency_key="stale-publication", publisher="author", invocation_path=path,
                authorize=lambda: None,
            )
        assert error.value.details["reason"] == "stale_head"
        current = head()
        call = {"schema_version": 1, "op": "timeline.restore", "version": "1", "select": None,
                "args": {key: historical["parent_revision"][key]
                         for key in ("revision_id", "content_digest")}}
        preview = submit(request([call], dry_run=True), "restore-preview")
        assert preview["receipt"] is None and head() == current
        assert preview["data"]["canonical_document"] == historical
        reused = submit(request([call]), "restore-historical-ids")
        assert reused["data"]["canonical_document"] == historical
    finally:
        service.close()
