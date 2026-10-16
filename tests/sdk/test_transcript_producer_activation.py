from __future__ import annotations

import hashlib
import inspect
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.pack import document_access
from astrid.core.pack.canonical import validate_canonical_pack
from astrid.sdk.client import _REQUIRED_SCOPES, AstridClient
from astrid.sdk.managed_transcript import transcript_document_id
from astrid.sdk.remote import RemoteAstridClient
from astrid.sdk.timeline.commit import _digest
from astrid.sdk.transcript_publication import ACTION_ID, Transcripts, publication_output
from astrid.sdk.workspace_client import WorkspaceClient, WorkspaceClientError
from banodoco_workspace_client.contract_metadata import PROTOCOL, SCHEMA_DIGEST
from banodoco_workspace_client.generated import (
    ByteResponse,
    MutationResult,
    ObjectLocation,
    ProjectDocument,
)


def _builtin_root() -> Path:
    return document_access.builtin_media_root()


def _entry_and_digest():
    root = _builtin_root()
    entry = validate_canonical_pack(root)
    return entry, document_access.document_source_digest(entry)


def test_exact_builtin_transcript_declaration_and_resource_closure_are_admitted():
    entry, digest = _entry_and_digest()
    action = entry.definition.actions["transcript_publish"]
    action, kind = document_access.select_transcript_writer(entry, digest)
    handles = {handle.path: handle for handle in entry.resources}

    assert document_access.transcript_writer_declaration(entry.root, entry.definition)
    assert f"{entry.id}.transcript_publish" == ACTION_ID
    assert action["inputs"] == "schemas/transcript-publication-request-v1.json"
    assert action["invocation"] == {
        "kind": "python",
        "path": "actions/transcript_publish/run.py",
        "function": "run",
    }
    assert kind.kind == "media.transcript/v1"
    assert set(kind.access) == {"read", "list", "write"}
    assert {
        "actions/transcript_publish/run.py",
        "schemas/transcript-publication-request-v1.json",
        "schemas/transcript-publication-output-v1.json",
    } <= set(handles)
    assert all(handle.file_kind == "file" for handle in handles.values())


@pytest.mark.parametrize(
    "removed_path",
    [
        "actions/transcript_publish/run.py",
        "schemas/transcript-publication-request-v1.json",
    ],
)
def test_missing_action_module_or_request_schema_closure_fails_closed(removed_path):
    entry, _digest = _entry_and_digest()
    incomplete = replace(entry, resources=tuple(
        handle for handle in entry.resources if handle.path != removed_path
    ))

    with pytest.raises(ValueError, match="resource is not admitted"):
        document_access.select_transcript_writer(
            incomplete, document_access.document_source_digest(incomplete),
        )


def test_mismatched_module_or_request_schema_declaration_fails_closed():
    entry, _digest = _entry_and_digest()
    actions = {key: dict(value) for key, value in entry.definition.actions.items()}

    wrong_module_actions = {key: dict(value) for key, value in actions.items()}
    wrong_module_actions["transcript_publish"] = dict(actions["transcript_publish"])
    wrong_module_actions["transcript_publish"]["invocation"] = {
        **actions["transcript_publish"]["invocation"],
        "path": "actions/transcript_publish/unadmitted.py",
    }
    wrong_module = replace(entry, definition=replace(
        entry.definition, actions=wrong_module_actions,
    ))
    with pytest.raises(ValueError, match="resource is not admitted"):
        document_access.select_transcript_writer(
            wrong_module, document_access.document_source_digest(wrong_module),
        )

    wrong_schema_actions = {key: dict(value) for key, value in actions.items()}
    wrong_schema_actions["transcript_publish"] = dict(actions["transcript_publish"])
    wrong_schema_actions["transcript_publish"]["inputs"] = "schemas/transcript-v1.json"
    wrong_schema = replace(entry, definition=replace(
        entry.definition, actions=wrong_schema_actions,
    ))
    with pytest.raises(ValueError, match="writer/source"):
        document_access.select_transcript_writer(
            wrong_schema, document_access.document_source_digest(wrong_schema),
        )


def test_changed_admitted_source_digest_refuses(tmp_path, monkeypatch):
    copied_root = tmp_path / "media"
    shutil.copytree(_builtin_root(), copied_root)
    monkeypatch.setattr(document_access, "builtin_media_root", lambda: copied_root)
    entry = validate_canonical_pack(copied_root)
    original_digest = document_access.document_source_digest(entry)
    module_path = copied_root / "actions/transcript_publish/run.py"
    module_path.write_bytes(module_path.read_bytes() + b"\n# changed after admission\n")

    with pytest.raises(ValueError, match="source changed"):
        document_access.select_transcript_writer(entry, original_digest)

    changed_entry = validate_canonical_pack(copied_root)
    assert document_access.document_source_digest(changed_entry) != original_digest


class _SyntheticPersistedRuntime:
    """In-memory Runtime-shaped fixture; no response is real Runtime evidence."""

    def __init__(self, project_id: str, media_object_id: str, media_bytes: bytes):
        self.project_id = project_id
        self.objects: dict[str, bytes] = {media_object_id: media_bytes}
        self.project_objects: set[tuple[str, str]] = {(project_id, media_object_id)}
        self.documents: dict[tuple[str, str], dict] = {}
        self.ledger: dict[str, MutationResult] = {}
        self.calls: list[tuple[str, tuple, dict]] = []

    def get_project(self, project_id: str) -> dict:
        self.calls.append(("get_project", (project_id,), {}))
        if project_id != self.project_id:
            raise WorkspaceClientError(404, "not_found", "synthetic project missing")
        return {"project_id": project_id}

    def get_document(self, project_id: str, document_id: str) -> ProjectDocument:
        self.calls.append(("get_document", (project_id, document_id), {}))
        row = self.documents.get((project_id, document_id))
        if row is None:
            raise WorkspaceClientError(404, "not_found", "synthetic document missing")
        return ProjectDocument(**row)

    def get_project_object_location(self, project_id: str, object_id: str) -> ObjectLocation:
        self.calls.append(("get_project_object_location", (project_id, object_id), {}))
        if (project_id, object_id) not in self.project_objects or object_id not in self.objects:
            raise WorkspaceClientError(404, "not_found", "synthetic object missing")
        data = self.objects[object_id]
        return ObjectLocation(
            object_id=object_id,
            digest="sha256:" + hashlib.sha256(data).hexdigest(),
            size=len(data),
            media_type="application/octet-stream",
            local_path=f"/synthetic/{object_id}",
            storage="memory",
            verified=True,
            filename=None,
        )

    def get_object(self, object_id: str) -> ByteResponse:
        self.calls.append(("get_object", (object_id,), {}))
        if object_id not in self.objects:
            raise WorkspaceClientError(404, "not_found", "synthetic object missing")
        return ByteResponse(self.objects[object_id], 200, {"content-type": "application/json"})

    def ingest_project_object(
        self, project_id: str, data: bytes, *, media_type: str,
        idempotency_key: str, filename: str | None = None,
    ) -> MutationResult:
        self.calls.append(("ingest_project_object", (project_id, bytes(data)), {
            "media_type": media_type, "idempotency_key": idempotency_key, "filename": filename,
        }))
        if project_id != self.project_id:
            raise WorkspaceClientError(404, "not_found", "synthetic project missing")
        object_id = "sha256:" + hashlib.sha256(data).hexdigest()
        self.objects[object_id] = bytes(data)
        self.project_objects.add((project_id, object_id))
        return MutationResult(
            {"object_id": object_id},
            {"receipt_id": "synthetic-object-receipt", "project_id": project_id},
        )

    def _document_mutation(
        self, *, project_id: str, document_id: str, kind: str, content: dict,
        idempotency_key: str, command_kind: str, body: dict, version: int,
        replay_only: bool,
    ) -> MutationResult:
        self.calls.append((command_kind, (project_id, document_id), {
            "idempotency_key": idempotency_key, "replay_only": replay_only,
            "content": content,
        }))
        if replay_only:
            committed = self.ledger.get(idempotency_key)
            if committed is None:
                raise WorkspaceClientError(409, "no_committed_request", "synthetic ledger miss")
            return committed
        data = {
            "project_id": project_id,
            "document_id": document_id,
            "kind": kind,
            "content": content,
            "version": version,
            "created_at": "synthetic-created",
            "updated_at": "synthetic-updated",
        }
        receipt = {
            "receipt_id": "synthetic-receipt-1",
            "command_kind": "document.create" if command_kind == "create_document" else "document.update",
            "idempotency_key": idempotency_key,
            "request_hash": _digest(body),
            "project_id": project_id,
            "project_seq": 1,
            "event_ids": ["synthetic-event-1"],
            "result": {"document_id": document_id, "version": version},
            "created_at": "synthetic-created",
        }
        result = MutationResult(data, receipt)
        self.documents[(project_id, document_id)] = data
        self.ledger[idempotency_key] = result
        return result

    def create_document(
        self, project_id: str, document_id: str, kind: str, content: dict, *,
        idempotency_key: str, replay_only: bool = False,
    ) -> MutationResult:
        body = {"project_id": project_id, "document_id": document_id,
                "kind": kind, "content": content}
        return self._document_mutation(
            project_id=project_id, document_id=document_id, kind=kind,
            content=content, idempotency_key=idempotency_key,
            command_kind="create_document", body=body, version=1,
            replay_only=replay_only,
        )

    def update_document(
        self, project_id: str, document_id: str, *, expected_version: int,
        idempotency_key: str, content: dict, kind: str | None = None,
        replay_only: bool = False,
    ) -> MutationResult:
        selected_kind = kind or "media.transcript/v1"
        body = {"project_id": project_id, "document_id": document_id,
                "body": {"expected_version": expected_version,
                         "content": content, "kind": selected_kind}}
        return self._document_mutation(
            project_id=project_id, document_id=document_id, kind=selected_kind,
            content=content, idempotency_key=idempotency_key,
            command_kind="update_document", body=body,
            version=expected_version + 1, replay_only=replay_only,
        )


def _authenticated_workspace(project_id: str, *, media_object_id: str, media_bytes: bytes):
    workspace = WorkspaceClient("http://127.0.0.1:1", "synthetic-token")
    workspace.actor_id = "actor-synthetic"
    workspace._last_handshake = {
        "protocol": PROTOCOL,
        "schema_digest": SCHEMA_DIGEST,
        "session_id": "session-synthetic",
        "actor_id": "actor-synthetic",
        "realm_id": "realm-synthetic",
        "scopes": sorted(_REQUIRED_SCOPES),
        "capabilities": ["execution_binding.targeted.v1"],
        "operation_versions": {"createDocument": [1, 2], "updateDocument": [1, 2]},
    }
    workspace._generated = _SyntheticPersistedRuntime(
        project_id, media_object_id, media_bytes,
    )
    return workspace


def _synthetic_record(producer_digest: str, media_bytes: bytes) -> dict:
    media_digest = "sha256:" + hashlib.sha256(media_bytes).hexdigest()
    return {
        "schema_version": 1,
        "subject": {"media_object_id": media_digest, "media_sha256": media_digest},
        "source": {
            "producer_id": ACTION_ID,
            "source_id": "synthetic-source",
            "lineage_id": "synthetic-lineage",
            "revision_id": "caller-value-replaced-during-prepare",
            "producer_digest": producer_digest,
        },
        "clock": {"kind": "media", "unit": "seconds", "origin_seconds": 0},
        "words": [{"id": "word-1", "text": "Synthetic", "start_s": 0.0, "end_s": 0.5}],
        "segments": [{"id": "segment-1", "word_ids": ["word-1"]}],
    }


def test_authenticated_sdk_activation_calls_real_host_with_selected_connection_and_frozen_request(
    monkeypatch,
):
    entry, producer_digest = _entry_and_digest()
    media_bytes = b"synthetic transcript source media"
    media_object_id = "sha256:" + hashlib.sha256(media_bytes).hexdigest()
    workspace = _authenticated_workspace(
        "project-synthetic", media_object_id=media_object_id, media_bytes=media_bytes,
    )
    client = AstridClient(remote=RemoteAstridClient(workspace))
    observed = []
    original = GenericPackHost.invoke_document_action

    def observe(host, action_id, params, **kwargs):
        observed.append((host, action_id, params, kwargs.copy()))
        return original(host, action_id, params, **kwargs)

    monkeypatch.setattr(GenericPackHost, "invoke_document_action", observe)
    facade = Transcripts.from_client(client, project_id="project-synthetic")
    record = _synthetic_record(producer_digest, media_bytes)
    prepared = facade.prepare(record, expected_document_version=None, request_id="request-synthetic")
    publication = facade.publish(prepared, request_id=prepared.request_id)

    assert len(observed) == 1
    host, action_id, sent_request, kwargs = observed[0]
    assert action_id == ACTION_ID
    assert host.pack_roots[0] == entry.root
    assert sent_request is prepared
    assert kwargs["caller_connection"] is workspace
    assert kwargs["project_id"] == prepared.project_id == "project-synthetic"
    assert kwargs["request_id"] == prepared.request_id == "request-synthetic"
    assert kwargs["expected_source_digest"] == prepared.producer_digest == producer_digest
    assert publication.prepared is prepared
    output = publication_output(publication)
    assert set(output) == {"data", "receipt", "snapshot"}
    assert output["data"]["document_id"] == prepared.document_id
    assert output["data"]["content"]["source"]["revision_id"] == json.loads(
        prepared.canonical_record_json
    )["source"]["revision_id"]
    assert output["receipt"]["command_kind"] == "document.create"
    assert output["snapshot"] == {
        "object_id": prepared.snapshot_object_id,
        "sha256": prepared.snapshot_object_id.removeprefix("sha256:"),
    }

    generated = workspace._generated
    assert generated.documents[("project-synthetic", prepared.document_id)]["content"] == json.loads(
        prepared.canonical_record_json
    )
    assert [call[2]["replay_only"] for call in generated.calls if call[0] == "create_document"] == [True, False]
    assert sum(call[0] == "create_document" for call in generated.calls) == 2
    assert sum(call[0] == "update_document" for call in generated.calls) == 0
    assert not hasattr(facade, "_invoke_document_action")
    assert "invoke_document_action" not in inspect.signature(Transcripts).parameters


def test_sdk_activation_rejects_unbound_callback_or_client_surface():
    with pytest.raises(TypeError, match="selected authenticated AstridClient"):
        Transcripts.from_client(object(), project_id="project-synthetic")


def test_prepare_reads_only_the_selected_project_and_document():
    _entry, producer_digest = _entry_and_digest()
    media_bytes = b"synthetic transcript parent media"
    media_object_id = "sha256:" + hashlib.sha256(media_bytes).hexdigest()
    workspace = _authenticated_workspace(
        "project-synthetic", media_object_id=media_object_id, media_bytes=media_bytes,
    )
    facade = Transcripts.from_client(
        AstridClient(remote=RemoteAstridClient(workspace)),
        project_id="project-synthetic",
    )
    runtime = workspace._generated
    parent = _synthetic_record(producer_digest, media_bytes)
    parent["source"]["revision_id"] = "revision-parent"
    document_id = transcript_document_id(
        "project-synthetic", parent["subject"], parent["source"],
    )
    runtime.documents[("project-synthetic", document_id)] = {
        "project_id": "project-synthetic",
        "document_id": document_id,
        "kind": "media.transcript/v1",
        "content": parent,
        "version": 3,
        "created_at": "synthetic-created",
        "updated_at": "synthetic-updated",
    }
    correction = _synthetic_record(producer_digest, media_bytes)
    correction["source"]["lineage_id"] = "synthetic-lineage"
    correction["source"]["parent_revision_id"] = "revision-parent"
    prepared = facade.prepare(
        correction, expected_document_version=3, request_id="request-correction",
    )

    document_reads = [call for call in runtime.calls if call[0] == "get_document"]
    assert document_reads == [
        ("get_document", ("project-synthetic", document_id), {}),
    ]
    assert prepared.project_id == "project-synthetic"
    assert prepared.document_id == document_id
    assert prepared.parent_selection.expected_document_version == 3
    with pytest.raises(ValueError, match="selected project/source"):
        facade.prepare(
            correction, project_id="project-other", expected_document_version=3,
            request_id="request-wrong-project",
        )
    assert [call for call in runtime.calls if call[0] == "get_document"] == document_reads


def test_transcripts_has_one_host_publication_route_without_workspace_mutation_bypass():
    publish_source = inspect.getsource(Transcripts.publish)
    prepare_source = inspect.getsource(Transcripts.prepare)
    assert "self._host.invoke_document_action" in publish_source
    assert "self._invoke_document_action" not in publish_source
    assert not any(
        operation in publish_source or operation in prepare_source
        for operation in (".create_document(", ".update_document(", ".ingest_project_object(")
    )
