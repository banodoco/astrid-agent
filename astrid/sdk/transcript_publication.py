"""Frozen transcript publication over the Host's caller-bound restricted ports."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from astrid.sdk.managed_transcript import (
    TRANSCRIPT_DOCUMENT_KIND,
    canonical_transcript_json,
    transcript_document_id,
    validate_transcript_record,
)
from astrid.sdk.timeline.transcripts import digest_bytes
from astrid.sdk.transcript_publication_types import (
    PreparedTranscriptPublication,
    ResolvedTranscriptPublication,
    TranscriptParentSelection,
    _capture_resolved_transcript_publication,
    _revision_id,
    prepare_transcript_publication,
)

if TYPE_CHECKING:
    from astrid.core.execution.document_invocation import BoundDocumentInvocation

ACTION_ID = "media.transcript_publish"


def _key(prepared, operation):
    from astrid.sdk.contracts import derive_stable_id
    return derive_stable_id(command_kind=ACTION_ID + "." + operation,
        scope=json.dumps([prepared.project_id, prepared.document_id], separators=(",", ":"), ensure_ascii=False),
        idempotency_key=prepared.request_id)


def _record(prepared):
    if type(prepared) is not PreparedTranscriptPublication:
        raise TypeError("a frozen PreparedTranscriptPublication is required")
    raw = prepared.canonical_record_json.encode("utf-8")
    record = validate_transcript_record(json.loads(prepared.canonical_record_json))
    if (canonical_transcript_json(record) != prepared.canonical_record_json
            or digest_bytes(raw) != prepared.content_digest
            or prepared.snapshot_object_id != prepared.content_digest
            or prepared.admitted_action_id != ACTION_ID
            or record["source"]["producer_digest"] != prepared.producer_digest
            or transcript_document_id(prepared.project_id, record["subject"], record["source"]) != prepared.document_id
            or record["source"]["revision_id"] != _revision_id(project_id=prepared.project_id,
                document_id=prepared.document_id, admitted_action_id=ACTION_ID, request_id=prepared.request_id)):
        raise ValueError("prepared transcript bytes/identity/source differ from frozen request")
    if prepared.expected_document_version is not None and prepared.expected_document_version < 1:
        raise ValueError("an update requires a positive exact parent version")
    if (prepared.expected_document_version is None) != (prepared.parent_selection is None):
        raise ValueError("updates require an exact frozen parent selection; creates have no parent")
    return record, raw


def _bound(invocation, prepared, record):
    from astrid.core.execution.document_invocation import BoundDocumentInvocation
    from astrid.core.pack.document_access import MEDIA_TRANSCRIPT_SCHEMA_ID
    if type(invocation) is not BoundDocumentInvocation:
        raise TypeError("only a Host-minted BoundDocumentInvocation may publish")
    if (invocation.project_id, invocation.request_id, invocation.action_id,
            invocation.capability_id, invocation.source_digest, invocation.producer_id,
            invocation.document_kind, invocation.schema_id, invocation.schema_version) != (
            prepared.project_id, prepared.request_id, ACTION_ID, ACTION_ID,
            prepared.producer_digest, record["source"]["producer_id"],
            TRANSCRIPT_DOCUMENT_KIND, MEDIA_TRANSCRIPT_SCHEMA_ID, 1):
        raise ValueError("publisher request differs from authenticated source/caller binding")
    if invocation.document_id != prepared.document_id or "write" not in invocation.access:
        raise ValueError("publisher lacks the exact selected document writer grant")
    if not all((invocation.actor_id, invocation.realm_id, invocation.session_id, invocation.resource_closure)):
        raise ValueError("publisher requires an active authenticated source/resource closure")


def _mutation(invocation, prepared, record, *, replay_only):
    key = _key(prepared, "document")
    operation = "createDocument" if prepared.expected_document_version is None else "updateDocument"
    if replay_only and invocation.supports_operation_version(operation, 2) is not True:
        raise ValueError("transcript publication requires exact advertised document operation version 2")
    if prepared.expected_document_version is None:
        return invocation.documents.create_document(prepared.document_id, TRANSCRIPT_DOCUMENT_KIND,
            record, idempotency_key=key, replay_only=replay_only)
    return invocation.documents.update_document(prepared.document_id,
        expected_version=prepared.expected_document_version, idempotency_key=key,
        content=record, kind=TRANSCRIPT_DOCUMENT_KIND, replay_only=replay_only)


def _miss(exc):
    # Only C's explicit typed ledger miss admits the mutation/preflight path.
    # Authorization, transport, conflict and unknown failures always propagate.
    return getattr(exc, "code", None) == "no_committed_request"


def _verify_mutation(prepared, mutation):
    if not isinstance(mutation, dict) or set(mutation) != {"data", "receipt"} or not isinstance(mutation["data"], dict):
        raise TypeError("publisher requires the exact bound WorkspaceClient mutation envelope")
    receipt = mutation["receipt"]
    operation = "document.create" if prepared.expected_document_version is None else "document.update"
    if (not isinstance(receipt, dict) or receipt.get("project_id") != prepared.project_id
            or receipt.get("idempotency_key") != _key(prepared, "document")
            or receipt.get("command_kind") != operation
            or not all(receipt.get(k) for k in ("receipt_id", "request_hash", "created_at"))
            or type(receipt.get("project_seq")) is not int or receipt["project_seq"] < 1
            or not isinstance(receipt.get("event_ids"), list)):
        raise ValueError("document mutation receipt differs from frozen operation identity")
    data = mutation["data"]
    expected_version = 1 if prepared.expected_document_version is None else prepared.expected_document_version + 1
    if (data.get("project_id"), data.get("document_id"), data.get("kind"), data.get("version")) != (
            prepared.project_id, prepared.document_id, TRANSCRIPT_DOCUMENT_KIND, expected_version):
        raise ValueError("document mutation returned wrong scope/version/kind")
    if canonical_transcript_json(data.get("content")) != prepared.canonical_record_json:
        raise ValueError("document mutation returned different transcript bytes")
    from astrid.sdk.timeline.commit import _digest
    if prepared.expected_document_version is None:
        body = dict(project_id=prepared.project_id, document_id=prepared.document_id,
                    kind=TRANSCRIPT_DOCUMENT_KIND, content=json.loads(prepared.canonical_record_json))
    else:
        body = dict(project_id=prepared.project_id, document_id=prepared.document_id,
                    body=dict(expected_version=prepared.expected_document_version,
                              content=json.loads(prepared.canonical_record_json), kind=TRANSCRIPT_DOCUMENT_KIND))
    if receipt["request_hash"].removeprefix("sha256:") != _digest(body).removeprefix("sha256:"):
        raise ValueError("receipt hash differs from the exact frozen mutation body")
    # Result summaries remain Runtime-owned. Validate known returned identity
    # fields without inventing a historical-version response shape.
    result = receipt.get("result")
    if not isinstance(result, dict) or any(k in result and result[k] != data[k] for k in ("document_id", "version")):
        raise ValueError("receipt result differs from original document identity")



def _snapshot(invocation, prepared, raw, *, replay):
    if not replay:
        result = invocation.objects.ingest_project_object(raw, media_type="application/json",
            idempotency_key=_key(prepared, "snapshot"), filename="transcript.json")
        if not isinstance(result, dict) or not isinstance(result.get("data"), dict) or result["data"].get("object_id") != prepared.snapshot_object_id:
            raise ValueError("transcript object ingest returned wrong immutable identity")
    try:
        response = invocation.objects.get_object(prepared.snapshot_object_id)
    except Exception as exc:
        if not replay or getattr(exc, "code", None) != "not_found":
            raise
        # A lost object-ingest response can be recovered with the SAME key and
        # exact frozen bytes; never a new logical source/document mutation.
        result = invocation.objects.ingest_project_object(raw, media_type="application/json",
            idempotency_key=_key(prepared, "snapshot"), filename="transcript.json")
        if not isinstance(result, dict) or not isinstance(result.get("data"), dict) or result["data"].get("object_id") != prepared.snapshot_object_id:
            raise ValueError("transcript object replay returned wrong immutable identity")
        response = invocation.objects.get_object(prepared.snapshot_object_id)
    if not isinstance(response, dict) or response.get("status") != 200 or response.get("data") != raw:
        raise ValueError("authenticated transcript snapshot bytes differ from frozen request")
    return response


def _parent(invocation, prepared, record):
    parent = prepared.parent_selection
    if parent is None:
        if "parent_revision_id" in record["source"]:
            raise ValueError("new transcript stream cannot claim correction ancestry")
        # Verify the declared immutable media bytes through the same restricted
        # authenticated CAS port; no filename/discovery/provider authority.
        media = invocation.objects.get_object(record["subject"]["media_object_id"])
        if (not isinstance(media, dict) or media.get("status") != 200 or
                digest_bytes(media["data"]).removeprefix("sha256:") != record["subject"]["media_sha256"].removeprefix("sha256:")):
            raise ValueError("transcript subject media bytes differ from immutable declaration")
        return
    row = invocation.documents.get_document(prepared.document_id)
    if not isinstance(row, dict) or (row.get("project_id"), row.get("document_id"), row.get("kind"), row.get("version")) != (
            prepared.project_id, prepared.document_id, TRANSCRIPT_DOCUMENT_KIND, parent.expected_document_version):
        raise ValueError("prepared transcript parent is stale or outside scope")
    old = validate_transcript_record(row["content"])
    raw = canonical_transcript_json(old).encode("utf-8")
    if (digest_bytes(raw) != "sha256:" + parent.content_digest.removeprefix("sha256:")
            or old["source"]["lineage_id"] != parent.lineage_id
            or old["source"]["revision_id"] != parent.revision_id
            or record["source"]["revision_id"] == parent.revision_id
            or record["source"].get("parent_revision_id") != parent.revision_id
            or record["source"]["lineage_id"] != parent.lineage_id
            or old["subject"] != record["subject"]
            or any(old["source"][k] != record["source"][k] for k in ("producer_id", "source_id"))):
        raise ValueError("transcript correction differs from exact prepared parent lineage/media/source")
    snapshot = invocation.objects.get_object(parent.snapshot_object_id)
    if not isinstance(snapshot, dict) or snapshot.get("status") != 200 or snapshot.get("data") != raw:
        raise ValueError("prepared parent snapshot differs from current authenticated parent")
    # Opaque IDs may survive a correction; no text/time/ordinal matching and no
    # implicit mapping to another take is inferred here.


def _capture_authenticated(invocation, prepared, mutation, snapshot, record, raw):
    # Only this trusted entry has both an actual J invocation and the bound
    # WorkspaceClient response. The DTO public constructor remains closed.
    _bound(invocation, prepared, record)
    _verify_mutation(prepared, mutation)
    if snapshot.get("status") != 200 or snapshot.get("data") != raw:
        raise ValueError("cannot capture an unverified transcript snapshot")
    return _capture_resolved_transcript_publication(prepared, mutation,
        snapshot_object_id=prepared.snapshot_object_id, snapshot_response=snapshot)


def _publish_frozen(invocation, prepared, record, raw):
    """Sequence restricted port operations after the trusted entry binds them."""
    try:
        mutation = _mutation(invocation, prepared, record, replay_only=True)
    except Exception as exc:
        if not _miss(exc):
            raise
    else:
        _verify_mutation(prepared, mutation)
        return mutation, _snapshot(invocation, prepared, raw, replay=True)
    # J's restricted ports recheck active caller/source before every actual I/O.
    _parent(invocation, prepared, record)
    snapshot = _snapshot(invocation, prepared, raw, replay=False)
    mutation = _mutation(invocation, prepared, record, replay_only=False)
    _verify_mutation(prepared, mutation)
    return mutation, snapshot


def publish_transcript_source(invocation: BoundDocumentInvocation,
                              prepared: PreparedTranscriptPublication) -> ResolvedTranscriptPublication:
    record, raw = _record(prepared)
    _bound(invocation, prepared, record)
    mutation, snapshot = _publish_frozen(invocation, prepared, record, raw)
    return _capture_authenticated(invocation, prepared, mutation, snapshot, record, raw)


def publication_matches_selection(publication, request, selection):
    if type(publication) is not ResolvedTranscriptPublication:
        return False
    d = selection.descriptor()
    if (publication.prepared.project_id != request["project_id"]
            or publication.prepared.request_id != request["request_id"]
            or publication.prepared.document_id != d["document"]["id"]
            or publication.data["version"] != d["document"]["version"]
            or publication.prepared.content_digest != d["document"]["content_digest"]
            or publication.snapshot_object_id != d["snapshot"]["object_id"]):
        return False
    from astrid.sdk.timeline.transcripts import verify_snapshot
    verify_snapshot(selection, publication.snapshot_bytes)
    return True


class Transcripts:
    """Authenticated SDK front door for the built-in caller-bound Host writer.

    Construct this from an already-open :class:`AstridClient`. The factory
    retains that client's selected WorkspaceClient and the existing
    GenericPackHost invocation boundary; it does not accept a client, document
    writer, or callback supplied independently by the caller.
    """

    def __init__(self, client, *, project_id):
        from astrid.core.execution.generic_host import GenericPackHost
        from astrid.core.pack.canonical import validate_canonical_pack
        from astrid.core.pack.document_access import (
            builtin_media_root,
            document_source_digest,
            select_transcript_writer,
        )
        from astrid.sdk.client import AstridClient
        from astrid.sdk.remote import RemoteAstridClient
        from astrid.sdk.workspace_client import WorkspaceClient

        if type(client) is not AstridClient:
            raise TypeError("Transcripts requires the selected authenticated AstridClient")
        remote = client._remote
        if type(remote) is not RemoteAstridClient or type(remote._transport) is not WorkspaceClient:
            raise TypeError("AstridClient does not expose its selected WorkspaceClient route")
        connection = remote._transport
        if type(project_id) is not str or not project_id or project_id != project_id.strip():
            raise ValueError("an exact selected project id is required")
        project = connection.get_project(project_id)
        if not isinstance(project, dict) or project.get("project_id") != project_id:
            raise ValueError("caller project differs from canonical selection")

        root = builtin_media_root()
        entry = validate_canonical_pack(root)
        producer_digest = document_source_digest(entry)
        # This check refuses a broken/missing declaration or incomplete hashed
        # closure before the SDK offers a publishable facade.
        select_transcript_writer(entry, producer_digest)
        self._project_id = project_id
        self._producer_digest = producer_digest
        self._caller_connection = connection
        self._host = GenericPackHost(pack_roots=[root])

    @classmethod
    def from_client(cls, client, *, project_id):
        """Bind the transcript front door to an open, authenticated client."""
        return cls(client, project_id=project_id)

    def prepare(self, record, *, expected_document_version, request_id,
                project_id=None, expected_source_digest=None) -> PreparedTranscriptPublication:
        project_id = self._project_id if project_id is None else project_id
        expected_source_digest = (self._producer_digest if expected_source_digest is None
                                  else expected_source_digest)
        if project_id != self._project_id or expected_source_digest != self._producer_digest:
            raise ValueError("transcript preparation differs from selected project/source")
        validated = validate_transcript_record(record)
        parent = None
        if expected_document_version is not None:
            document_id = transcript_document_id(project_id, validated["subject"], validated["source"])
            row = self._caller_connection.get_document(project_id, document_id)
            if not isinstance(row, dict) or (row.get("project_id"), row.get("document_id"), row.get("kind"), row.get("version")) != (
                    project_id, document_id, TRANSCRIPT_DOCUMENT_KIND, expected_document_version):
                raise ValueError("transcript prepare requires the exact current parent version")
            old = validate_transcript_record(row["content"])
            raw = canonical_transcript_json(old).encode("utf-8")
            parent = TranscriptParentSelection(row["version"], old["source"]["lineage_id"],
                old["source"]["revision_id"], digest_bytes(raw), digest_bytes(raw))
        return prepare_transcript_publication(validated, project_id=project_id,
            expected_document_version=expected_document_version, request_id=request_id,
            admitted_action_id=ACTION_ID, producer_digest=expected_source_digest, parent_selection=parent)

    def publish(self, prepared, *, request_id) -> ResolvedTranscriptPublication:
        if type(prepared) is not PreparedTranscriptPublication or (
                request_id, prepared.project_id, prepared.producer_digest) != (
                prepared.request_id, self._project_id, self._producer_digest):
            raise ValueError("publish must resend the exact frozen request identity")
        result = self._host.invoke_document_action(ACTION_ID, prepared,
            caller_connection=self._caller_connection, project_id=prepared.project_id,
            request_id=request_id, expected_source_digest=prepared.producer_digest)
        if type(result) is not ResolvedTranscriptPublication or result.prepared != prepared:
            raise ValueError("Host did not return the original authenticated publication capture")
        return result


def publication_output(publication: ResolvedTranscriptPublication) -> dict:
    """Host output admission projection; the original private capture is retained."""
    from collections.abc import Mapping
    if type(publication) is not ResolvedTranscriptPublication:
        raise TypeError("only an internal transcript publication has a public output")
    def thaw(value):
        if isinstance(value, Mapping):
            return {key: thaw(child) for key, child in value.items()}
        if isinstance(value, tuple):
            return [thaw(child) for child in value]
        return value
    return dict(data=thaw(publication.data), receipt=thaw(publication.receipt),
        snapshot=dict(object_id=publication.snapshot_object_id,
                      sha256=publication.snapshot_object_id.removeprefix("sha256:")))
