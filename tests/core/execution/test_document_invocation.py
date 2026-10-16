"""Synthetic Host boundary evidence; no Runtime publication or producer join."""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import pickle
import sys
from types import ModuleType, SimpleNamespace

import pytest
import yaml

from astrid.core.execution.document_invocation import BoundDocumentInvocation
from astrid.core.execution.executor.actions import action_executor_definition
from astrid.core.execution.executor.runner import ExecutorRunnerError, _run_python_action
from astrid.core.execution.generic_host import GenericPackHost, HostError
from astrid.core.pack import document_access
from astrid.core.pack.canonical import validate_canonical_pack
from astrid.sdk.client import _REQUIRED_SCOPES, AstridClient, _validate_handshake, _validate_health
from astrid.sdk.managed_transcript import (
    TRANSCRIPT_RECORD_SCHEMA,
    canonical_transcript_json,
    transcript_document_id,
)
from astrid.sdk.workspace_client import WorkspaceClient, WorkspaceClientError
from banodoco_workspace_client.contract_metadata import PROTOCOL, SCHEMA_DIGEST
from banodoco_workspace_client.generated import ByteResponse, MutationResult, ProjectDocument


@dataclasses.dataclass(frozen=True)
class Prepared:
    project_id: str
    document_id: str
    expected_document_version: int
    request_id: str
    admitted_action_id: str
    producer_digest: str
    canonical_record_json: str
    content_digest: str
    snapshot_object_id: str
    parent_selection: dict | None


@dataclasses.dataclass(frozen=True)
class Parent:
    expected_document_version: int
    lineage_id: str
    revision_id: str
    content_digest: str
    snapshot_object_id: str


def _manifest():
    # Uses existing V3 fields only. D's joined action/schema/publisher are absent
    # from this receiver; this fixture exercises the contract seam explicitly.
    return {
        "schema_version": 3, "id": "media", "name": "Media", "version": "1.0.0",
        "documents": {"transcript": {
            "format_version": 1, "schema": "schemas/transcript-v1.json",
            "runtime": {"kind": "media.transcript/v1", "schema_id": "media.transcript",
                        "schema_version": 1, "identity": "media-transcript-v1",
                        "access": ["read", "list", "write"]},
        }},
        "actions": {"transcript_publish": {
            "name": "Publish", "version": "1.0.0", "description": "Publish transcript",
            "invocation": {"kind": "python", "path": "actions/publish.py", "function": "publish"},
            "inputs": "schemas/transcript-publication-request-v1.json",
            "outputs": {"type": "object", "properties": {"ok": {"const": True}},
                        "required": ["ok"], "additionalProperties": False},
        }},
    }


def _write(root, payload=None):
    root.mkdir(parents=True)
    (root / "schemas").mkdir()
    (root / "actions").mkdir()
    (root / "actions/publish.py").write_text("raise AssertionError('ordinary action executed')\n")
    (root / "schemas/transcript-v1.json").write_text(json.dumps(TRANSCRIPT_RECORD_SCHEMA))
    properties = {f.name: {} for f in dataclasses.fields(Prepared)}
    schema = {"$id": "media.transcript-publication-request/v1", "type": "object",
              "properties": properties, "required": list(properties), "additionalProperties": False}
    (root / "schemas/transcript-publication-request-v1.json").write_text(json.dumps(schema))
    (root / "pack.yaml").write_text(yaml.safe_dump(payload or _manifest()))


class Generated:
    def __init__(self):
        self.calls = []
        self.results = {}
        self.denied = False

    def __getattr__(self, operation):
        def call(*args, **kwargs):
            self.calls.append((operation, args, kwargs))
            if self.denied:
                raise WorkspaceClientError(403, "forbidden", "revoked")
            if operation == "get_project":
                return {"project_id": "project-a"}
            if operation == "get_project_object_location":
                override = self.results.get("location_overrides", {}).get(args[1])
                if override is not None:
                    return override
                value = self.results["get_object"][args[1]]
                return {"object_id": args[1],
                        "digest": "sha256:" + hashlib.sha256(value.data).hexdigest(),
                        "size": len(value.data), "verified": True}
            if operation == "get_object":
                return self.results[operation][args[0]]
            return self.results[operation]
        return call


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    root = tmp_path / "media"
    _write(root)
    # Only this test's temporary root stands in for installed built-in source.
    monkeypatch.setattr(document_access, "builtin_media_root", lambda: root)
    entry = validate_canonical_pack(root)
    digest = document_access.document_source_digest(entry)
    record = {
        "schema_version": 1,
        "subject": {"media_object_id": "media-object-01",
                    "media_sha256": hashlib.sha256(b"source media").hexdigest()},
        "source": {"producer_id": "media.transcript_publish", "source_id": "main",
                   "lineage_id": "lineage-a", "revision_id": "revision-a", "producer_digest": digest},
        "clock": {"kind": "media", "unit": "seconds", "origin_seconds": 0},
        "words": [{"id": "word-a", "text": "Hello", "start_s": 0.1, "end_s": 0.4}],
        "segments": [{"id": "segment-a", "word_ids": ["word-a"]}],
    }
    text = canonical_transcript_json(record)
    content_digest = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    media_bytes = b"source media"
    prepared = Prepared("project-a", transcript_document_id("project-a", record["subject"], record["source"]),
                        1, "request-a", "media.transcript_publish", digest, text, content_digest,
                        content_digest, None)
    from astrid.sdk.contracts import derive_stable_id
    scope = json.dumps(["project-a", prepared.document_id], ensure_ascii=False,
                       separators=(",", ":"), allow_nan=False)
    document_key = derive_stable_id(command_kind="media.transcript_publish.document",
                                    scope=scope, idempotency_key=prepared.request_id)
    snapshot_key = derive_stable_id(command_kind="media.transcript_publish.snapshot",
                                    scope=scope, idempotency_key=prepared.request_id)
    client = WorkspaceClient("http://127.0.0.1:1", "synthetic-credential-never-exposed")
    client.actor_id = "actor-a"
    client._last_handshake = {
        "protocol": PROTOCOL, "schema_digest": SCHEMA_DIGEST, "actor_id": "actor-a",
        "session_id": "session-a", "realm_id": "realm-a", "scopes": sorted(_REQUIRED_SCOPES),
        "capabilities": ["execution_binding.targeted.v1"],
        "operation_versions": {"createDocument": [1, 2], "updateDocument": [1, 2]},
    }
    generated = Generated()
    client._generated = generated
    row = ProjectDocument(prepared.document_id, "project-a", "media.transcript/v1", record, 1, "now", "now")
    mutation = MutationResult({"document_id": prepared.document_id}, {"receipt_id": "receipt-a"})
    byte_response = ByteResponse(text.encode(), 200, {"content-type": "application/json"})
    media_response = ByteResponse(media_bytes, 200, {"content-type": "application/octet-stream"})
    generated.results.update(get_document=row, create_document=mutation, update_document=mutation,
                             ingest_project_object=mutation,
                             get_object={prepared.snapshot_object_id: byte_response,
                                         "media-object-01": media_response})
    workspace_values = {
        "get_document": client.get_document("project-a", prepared.document_id),
        "create_document": client.create_document(
            "project-a", prepared.document_id, "media.transcript/v1", record,
            idempotency_key=document_key, replay_only=True,
        ),
        "update_document": client.update_document(
            "project-a", prepared.document_id, expected_version=1, idempotency_key=document_key,
            content=record, kind="media.transcript/v1", replay_only=True,
        ),
        "ingest_project_object": client.ingest_project_object(
            "project-a", text.encode(), media_type="application/json", idempotency_key=snapshot_key,
        ),
        "get_object": client.get_object(prepared.snapshot_object_id),
        "media_object": client.get_object("media-object-01"),
    }
    generated.calls.clear()
    workspace_call_results = {}
    for method_name in ("get_document", "create_document", "update_document",
                        "ingest_project_object", "get_object"):
        original = getattr(client, method_name)
        def capture(*args, _name=method_name, _original=original, **kwargs):
            result = _original(*args, **kwargs)
            workspace_call_results.setdefault(_name, []).append(result)
            return result
        setattr(client, method_name, capture)
    module = ModuleType("astrid.sdk.transcript_publication")
    module.PreparedTranscriptPublication = Prepared
    calls = []
    projection_calls = []
    def publish(invocation, prepared):
        calls.append((invocation, prepared))
        return {"ok": True}
    def publication_output(result):
        projection_calls.append(result)
        return result
    module.publish_transcript_source = publish
    module.publication_output = publication_output
    monkeypatch.setitem(sys.modules, module.__name__, module)
    host = GenericPackHost(pack_roots=[root], client=object())
    def invoke(params=prepared, **kw):
        values = dict(caller_connection=client, project_id="project-a", request_id="request-a",
                      expected_source_digest=digest)
        values.update(kw)
        return host.invoke_document_action("media.transcript_publish", params, **values)
    return SimpleNamespace(**locals())


def test_dedicated_host_only_dispatches_trusted_publisher(boundary):
    b = boundary
    result = b.invoke()
    assert result == {"ok": True}
    assert b.projection_calls == [result]
    invocation, prepared = b.calls[0]
    assert prepared is b.prepared
    assert invocation.project_id == "project-a"
    assert (invocation.actor_id, invocation.session_id, invocation.realm_id) == ("actor-a", "session-a", "realm-a")
    assert invocation.action_id == invocation.capability_id == invocation.producer_id == "media.transcript_publish"
    assert invocation.source_digest == b.digest
    assert invocation.resource_closure == tuple(sorted((h.path, h.sha256) for h in (b.entry.manifest, *b.entry.resources)))
    assert invocation.document_kind == "media.transcript/v1"
    assert invocation.access == ("list", "read", "write")
    assert not hasattr(invocation, "client") and not hasattr(invocation.documents, "client")
    assert not hasattr(invocation, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        invocation.project_id = "other"
    for value in (invocation, invocation.documents, invocation.objects):
        with pytest.raises(TypeError):
            pickle.dumps(value)
        with pytest.raises(TypeError):
            copy.deepcopy(value)
        with pytest.raises(TypeError):
            json.dumps(value)
    with pytest.raises(ValueError, match="expired"):
        invocation.documents.get_document(b.prepared.document_id)


@pytest.mark.parametrize("caller", [None, {}, "serialized", object(), SimpleNamespace(actor_id="actor-a")])
def test_malformed_connection_refuses_without_publisher(boundary, caller):
    with pytest.raises(ValueError):
        boundary.invoke(caller_connection=caller)
    assert not boundary.calls


def test_handshake_operation_versions_are_optional_strict_metadata():
    handshake = {
        "protocol": PROTOCOL, "schema_digest": SCHEMA_DIGEST,
        "actor_id": "actor-a", "session_id": "session-a", "realm_id": "realm-a",
        "scopes": sorted(_REQUIRED_SCOPES), "capabilities": ["execution_binding.targeted.v1"],
        "operation_versions": {"createDocument": [1, 2], "updateDocument": [1]},
        "capability_catalog_versions": [1, 2],
    }
    assert _validate_handshake(
        handshake, expected_protocol=PROTOCOL, expected_digest=SCHEMA_DIGEST,
        expected_actor="actor-a", expected_realm="realm-a",
    ) == {"createDocument": [1, 2], "updateDocument": [1]}
    malformed = {**handshake, "operation_versions": {"createDocument": [True]}}
    assert _validate_handshake(
        malformed, expected_protocol=PROTOCOL, expected_digest=SCHEMA_DIGEST,
        expected_actor="actor-a", expected_realm="realm-a",
    ) is None
    malformed_empty = {**handshake, "operation_versions": {"createDocument": []}}
    assert _validate_handshake(
        malformed_empty, expected_protocol=PROTOCOL, expected_digest=SCHEMA_DIGEST,
        expected_actor="actor-a", expected_realm="realm-a",
    ) is None
    assert _validate_handshake(
        {**handshake, "capability_catalog_versions": [1, True]},
        expected_protocol=PROTOCOL, expected_digest=SCHEMA_DIGEST,
        expected_actor="actor-a", expected_realm="realm-a",
    ) == {"createDocument": [1, 2], "updateDocument": [1]}
    with pytest.raises(WorkspaceClientError, match="unexpected schema"):
        _validate_handshake(
            {**handshake, "unexpected_authority": True},
            expected_protocol=PROTOCOL, expected_digest=SCHEMA_DIGEST,
            expected_actor="actor-a", expected_realm="realm-a",
        )


def test_health_accepts_only_known_optional_runtime_profiles():
    value = {
        "status": "ok", "protocol": PROTOCOL, "schema_digest": SCHEMA_DIGEST,
        "runtime_epoch": 1, "runtime_instance_id": "instance-a",
        "runtime_session_id": "runtime-session-a",
        "operation_versions": {"createDocument": [1, 2]},
        "capability_catalog_versions": [1, 2],
    }
    _validate_health(value, expected_protocol=PROTOCOL, expected_digest=SCHEMA_DIGEST)
    with pytest.raises(WorkspaceClientError, match="unexpected schema"):
        _validate_health(
            {**value, "unrelated": True},
            expected_protocol=PROTOCOL, expected_digest=SCHEMA_DIGEST,
        )


def test_open_binds_validated_operation_versions_to_authenticated_workspace(monkeypatch):
    handshake = {
        "protocol": PROTOCOL, "schema_digest": SCHEMA_DIGEST,
        "session_id": "session-a", "actor_id": "actor-a", "realm_id": "realm-a",
        "scopes": sorted(_REQUIRED_SCOPES),
        "capabilities": ["execution_binding.targeted.v1"],
        "operation_versions": {"createDocument": [1, 2], "updateDocument": [1, 2]},
        "capability_catalog_versions": [1, 2],
    }
    health = {
        "status": "ok", "protocol": PROTOCOL, "schema_digest": SCHEMA_DIGEST,
        "runtime_epoch": 1, "runtime_instance_id": "instance-a",
        "runtime_session_id": "runtime-session-a",
        "operation_versions": {"createDocument": [1, 2], "updateDocument": [1, 2]},
        "capability_catalog_versions": [1, 2],
    }
    instances = []

    class Workspace:
        def __init__(self, *_args):
            self._last_handshake = None
            instances.append(self)

        def health(self):
            return health

        def handshake(self, *_args):
            self._last_handshake = dict(handshake)
            return self._last_handshake

    monkeypatch.setattr("astrid.sdk.workspace_client.WorkspaceClient", Workspace)
    AstridClient.open(
        endpoint="http://127.0.0.1:1", credential="token", realm_id="realm-a",
        actor_id="actor-a", client_name="test", client_version="1",
        protocol_version=PROTOCOL,
    )
    assert instances[0]._last_handshake["operation_versions"] == {
        "createDocument": [1, 2], "updateDocument": [1, 2],
    }
    assert instances[0]._last_handshake["capability_catalog_versions"] == [1, 2]


def test_workspace_client_normalization_preserves_generated_operation_versions():
    @dataclasses.dataclass(frozen=True)
    class GeneratedHandshake:
        protocol: str
        schema_digest: str
        session_id: str
        actor_id: str
        realm_id: str
        scopes: tuple[str, ...]
        capabilities: tuple[str, ...]
        operation_versions: dict[str, tuple[int, ...]]

    class GeneratedClient:
        def handshake(self, *_args):
            return GeneratedHandshake(
                PROTOCOL, SCHEMA_DIGEST, "session-a", "actor-a", "realm-a",
                tuple(sorted(_REQUIRED_SCOPES)), ("execution_binding.targeted.v1",),
                {"createDocument": (1, 2), "updateDocument": (1, 2)},
            )

    client = WorkspaceClient("http://127.0.0.1:1", "synthetic-credential-never-exposed")
    client._generated = GeneratedClient()
    result = client.handshake("test", "1", sorted(_REQUIRED_SCOPES))
    assert result["operation_versions"] == {"createDocument": [1, 2], "updateDocument": [1, 2]}
    assert client._last_handshake is result


@pytest.mark.parametrize("field", ["actor_id", "session_id", "realm_id"])
@pytest.mark.parametrize("value", [None, "", " padded "])
def test_missing_authentication_refuses(boundary, field, value):
    boundary.client._last_handshake[field] = value
    with pytest.raises(ValueError):
        boundary.invoke()
    assert not boundary.calls


@pytest.mark.parametrize("override", [
    {"project_id": "project-b"}, {"project_id": " project-a"}, {"request_id": "other"},
    {"expected_source_digest": "sha256:" + "f" * 64},
])
def test_wrong_selection_refuses(boundary, override):
    with pytest.raises(ValueError):
        boundary.invoke(**override)
    assert not boundary.calls


@pytest.mark.parametrize("field,value", [
    ("project_id", "project-b"), ("document_id", "other-document"),
    ("admitted_action_id", "evil.publish"), ("producer_digest", "sha256:" + "e" * 64),
    ("content_digest", "e" * 64), ("snapshot_object_id", "sha256:" + "e" * 64),
])
def test_forged_prepared_selection_refuses(boundary, field, value):
    with pytest.raises(ValueError):
        boundary.invoke(dataclasses.replace(boundary.prepared, **{field: value}))
    assert not boundary.calls


def test_serialized_and_private_user_authority_refused(boundary):
    b = boundary
    for value in (dataclasses.asdict(b.prepared), json.dumps(dataclasses.asdict(b.prepared)),
                  {"invocation": {"actor_id": "owner"}, "_client": b.client}):
        with pytest.raises(TypeError):
            b.invoke(value)
    with pytest.raises(TypeError):
        BoundDocumentInvocation(project_id="evil", documents=b.client)
    with pytest.raises(TypeError):
        BoundDocumentInvocation()
    assert not b.calls


@pytest.mark.parametrize("file", ["actions/publish.py", "schemas/transcript-publication-request-v1.json",
                                  "schemas/transcript-v1.json", "pack.yaml"])
def test_changed_resource_or_manifest_refuses(boundary, file):
    with (boundary.root / file).open("a") as stream:
        stream.write("\n ")
    with pytest.raises(ValueError):
        boundary.invoke()
    assert not boundary.calls


@pytest.mark.parametrize("change", ["readonly", "schema", "kind", "action", "input-schema"])
def test_wrong_declaration_refuses_before_publisher(boundary, change):
    b = boundary
    data = _manifest()
    if change == "readonly":
        data["documents"]["transcript"]["runtime"]["access"] = ["read", "list"]
    elif change == "schema":
        data["documents"]["transcript"]["runtime"]["schema_id"] = "other.schema"
    elif change == "kind":
        data["documents"]["transcript"]["runtime"]["kind"] = "other.kind"
    elif change == "action":
        data["actions"]["evil"] = data["actions"].pop("transcript_publish")
    else:
        schema_path = b.root / "schemas/transcript-publication-request-v1.json"
        schema = json.loads(schema_path.read_text())
        schema["$id"] = "other.schema/v1"
        schema_path.write_text(json.dumps(schema))
    (b.root / "pack.yaml").write_text(yaml.safe_dump(data))
    try:
        digest = document_access.document_source_digest(validate_canonical_pack(b.root))
    except ValueError:
        digest = b.digest
    with pytest.raises(ValueError):
        b.invoke(expected_source_digest=digest)
    assert not b.calls


def test_contribution_root_cannot_claim_writer(boundary, tmp_path):
    lookalike = tmp_path / "contribution" / "media"
    _write(lookalike)
    with pytest.raises(ValueError):
        validate_canonical_pack(lookalike)
    host = GenericPackHost(pack_roots=[lookalike], client=boundary.client)
    with pytest.raises(HostError):
        host.invoke_document_action("media.transcript_publish", boundary.prepared,
                                    caller_connection=boundary.client, project_id="project-a",
                                    request_id="request-a", expected_source_digest=boundary.digest)
    assert not boundary.calls


def test_wrong_host_action_refused(boundary):
    with pytest.raises(HostError):
        boundary.host.invoke_document_action("other.publish", {}, caller_connection=boundary.client,
                                            project_id="project-a", request_id="r", expected_source_digest=boundary.digest)
    assert not boundary.calls


def test_ordinary_runner_rejects_before_import_even_with_task_admission(boundary):
    b = boundary
    definition = action_executor_definition(SimpleNamespace(id="media", root=b.root, version="1.0.0"),
                                            "transcript_publish", b.entry.definition.actions["transcript_publish"])
    assert definition.metadata["requires_caller_document_adapter"] is True
    with pytest.raises(ExecutorRunnerError, match="caller-document"):
        _run_python_action(definition, SimpleNamespace(dry_run=False), {"invocation": {}}, _admission={"worker": "owner"})


def test_all_ports_keep_caller_project_workspace_values_and_replay_mode(boundary):
    b = boundary
    def publisher(invocation, prepared):
        docs, objects = invocation.documents, invocation.objects
        assert docs.get_document(prepared.document_id) is b.workspace_call_results["get_document"][0]
        assert docs.update_document(prepared.document_id, expected_version=1, content=b.record,
                                    idempotency_key=b.document_key, kind="media.transcript/v1", replay_only=True) is b.workspace_call_results["update_document"][0]
        assert objects.ingest_project_object(b.text.encode(), media_type="application/json",
                                             idempotency_key=b.snapshot_key, filename="transcript.json") is b.workspace_call_results["ingest_project_object"][0]
        assert objects.get_object(prepared.snapshot_object_id) is b.workspace_call_results["get_object"][0]
        assert objects.get_object("media-object-01") is b.workspace_call_results["get_object"][1]
        assert b.workspace_values["create_document"] == {
            "data": {"document_id": prepared.document_id}, "receipt": {"receipt_id": "receipt-a"}
        }
        assert b.workspace_values["get_object"] == {
            "data": b.text.encode(), "status": 200, "headers": {"content-type": "application/json"}
        }
        assert invocation.supports_operation_version("createDocument", 2)
        assert invocation.supports_operation_version("updateDocument", 2)
        return {"ok": True}
    b.module.publish_transcript_source = publisher
    b.invoke()
    assert [c[0] for c in b.generated.calls] == ["get_project", "get_document", "update_document",
                                               "ingest_project_object", "get_project_object_location",
                                               "get_object", "get_project_object_location", "get_object"]
    for op, args, kw in b.generated.calls:
        if op == "get_object":
            assert args[0] in {b.prepared.snapshot_object_id, "media-object-01"}
        else:
            assert args[0] == "project-a"
        if op in {"create_document", "update_document"}:
            assert kw["replay_only"] is True


def test_create_port_preserves_normalized_workspace_mutation(boundary):
    b = boundary
    create_request = dataclasses.replace(b.prepared, expected_document_version=None, parent_selection=None)

    def publisher(invocation, prepared):
        result = invocation.documents.create_document(
            prepared.document_id, "media.transcript/v1", b.record,
            idempotency_key=b.document_key, replay_only=True,
        )
        assert result is b.workspace_call_results["create_document"][0]
        assert result == {"data": {"document_id": prepared.document_id},
                          "receipt": {"receipt_id": "receipt-a"}}
        return {"ok": True}

    b.module.publish_transcript_source = publisher
    assert b.invoke(params=create_request) == {"ok": True}
    assert [call[0] for call in b.generated.calls] == ["get_project", "create_document"]


def test_object_port_admits_exact_parent_and_source_media_identities(boundary):
    b = boundary
    parent_bytes = b"previous transcript snapshot"
    parent_id = "sha256:" + hashlib.sha256(parent_bytes).hexdigest()
    b.generated.results["get_object"][parent_id] = ByteResponse(
        parent_bytes, 200, {"content-type": "application/json"}
    )
    parent = Parent(1, "lineage-a", "revision-parent", parent_id, parent_id)
    prepared = dataclasses.replace(b.prepared, parent_selection=parent)

    def publisher(invocation, _prepared):
        media = invocation.objects.get_object("media-object-01")
        previous = invocation.objects.get_object(parent_id)
        assert media["data"] == b.media_bytes
        assert previous["data"] == parent_bytes
        with pytest.raises(ValueError, match="outside the selected snapshot"):
            invocation.objects.get_object("unrelated-object")
        return {"ok": True}

    b.module.publish_transcript_source = publisher
    assert b.invoke(params=prepared) == {"ok": True}
    assert [call[0] for call in b.generated.calls] == [
        "get_project", "get_project_object_location", "get_object",
        "get_project_object_location", "get_object",
    ]


def test_object_port_refuses_mismatched_project_membership_digest(boundary):
    b = boundary
    b.generated.results["location_overrides"] = {
        "media-object-01": {
            "object_id": "media-object-01", "digest": "sha256:" + "0" * 64,
            "size": len(b.media_bytes), "verified": True,
        }
    }

    def publisher(invocation, _prepared):
        with pytest.raises(ValueError, match="membership identity"):
            invocation.objects.get_object("media-object-01")
        return {"ok": True}

    b.module.publish_transcript_source = publisher
    assert b.invoke() == {"ok": True}
    assert [call[0] for call in b.generated.calls] == [
        "get_project", "get_project_object_location",
    ]


@pytest.mark.parametrize("operation_versions", [
    None,
    {},
    {"createDocument": [1]},
    {"createDocument": [True]},
    {"createDocument": "2"},
])
def test_replay_only_requires_exact_advertised_create_version(boundary, operation_versions):
    b = boundary
    b.client._last_handshake["operation_versions"] = operation_versions
    create_request = dataclasses.replace(b.prepared, expected_document_version=None, parent_selection=None)
    create_key = b.document_key

    def publisher(invocation, prepared):
        assert not invocation.supports_operation_version("createDocument", 2)
        with pytest.raises(ValueError, match="createDocument replay-only version 2"):
            invocation.documents.create_document(
                prepared.document_id, "media.transcript/v1", b.record,
                idempotency_key=create_key, replay_only=True,
            )
        return {"ok": True}

    b.module.publish_transcript_source = publisher
    assert b.invoke(params=create_request) == {"ok": True}
    assert not any(operation == "create_document" for operation, _, _ in b.generated.calls)


def test_ports_refuse_wider_document_kind_content_object_or_project(boundary):
    b = boundary
    def publisher(invocation, prepared):
        docs, objects = invocation.documents, invocation.objects
        for call in (
            lambda: docs.get_document("foreign-document"),
            lambda: docs.get_document("project-b", prepared.document_id),
            lambda: docs.create_document(prepared.document_id, "timeline", b.record, idempotency_key="x"),
            lambda: docs.update_document(prepared.document_id, expected_version=1, idempotency_key="x", content={}, kind=None),
            lambda: docs.create_document("foreign-document", "media.transcript/v1", b.record, idempotency_key="x"),
            lambda: objects.get_object("foreign-object"),
            lambda: objects.ingest_project_object(b"foreign", media_type="application/json", idempotency_key="x"),
            lambda: objects.ingest_project_object(b.text.encode(), project_id="project-b", media_type="application/json", idempotency_key="x"),
        ):
            with pytest.raises((ValueError, TypeError)):
                call()
        return {"ok": True}
    b.module.publish_transcript_source = publisher
    b.invoke()
    assert [c[0] for c in b.generated.calls] == ["get_project"]


def test_revoked_caller_is_never_replaced_with_host_client(boundary):
    b = boundary
    b.host.client = b.client
    b.generated.denied = True
    with pytest.raises(WorkspaceClientError):
        b.invoke()
    assert not b.calls


def test_public_workspace_mode_retains_existing_normalization(boundary):
    result = boundary.client.create_document("project-a", "document-a", "demo.record", {}, idempotency_key="x")
    assert type(result) is dict
    assert result["data"] == {"document_id": boundary.prepared.document_id}
    assert result["receipt"] == boundary.mutation.receipt


@pytest.mark.parametrize("fault", ["actor", "worker-scopes", "session-changed", "source-changed"])
def test_authentication_and_source_are_rechecked_at_effect_boundary(boundary, fault):
    b = boundary
    if fault == "actor":
        b.client.actor_id = "worker"
        with pytest.raises(ValueError):
            b.invoke()
        assert not b.calls
        return
    if fault == "worker-scopes":
        b.client._last_handshake["scopes"] = ["worker:execute"]
        with pytest.raises(Exception, match="scopes"):
            b.invoke()
        assert not b.calls
        return
    def publisher(invocation, prepared):
        if fault == "session-changed":
            b.client._last_handshake["session_id"] = "another-session"
        else:
            with (b.root / "actions/publish.py").open("a") as stream:
                stream.write("# changed after selection\n")
        with pytest.raises(ValueError):
            invocation.documents.create_document(prepared.document_id, "media.transcript/v1", b.record,
                                                  idempotency_key="x")
        return {"ok": True}
    b.module.publish_transcript_source = publisher
    b.invoke()
    assert [c[0] for c in b.generated.calls] == ["get_project"]


@pytest.mark.parametrize("fault", ["project", "document", "kind", "source"])
def test_foreign_document_response_refused(boundary, fault):
    b = boundary
    if fault == "source":
        content = copy.deepcopy(b.record)
        content["source"]["source_id"] = "foreign"
        row = dataclasses.replace(b.row, content=content)
    else:
        field = {"project": "project_id", "document": "document_id", "kind": "kind"}[fault]
        row = dataclasses.replace(b.row, **{field: "foreign"})
    b.generated.results["get_document"] = row
    def publisher(invocation, prepared):
        with pytest.raises(ValueError):
            invocation.documents.get_document(prepared.document_id)
        return {"ok": True}
    b.module.publish_transcript_source = publisher
    b.invoke()


def test_no_caller_fallback_even_when_host_has_authenticated_connection(boundary):
    boundary.host.client = boundary.client
    with pytest.raises(ValueError):
        boundary.invoke(caller_connection=None)
    assert not boundary.calls
    assert not boundary.generated.calls


def test_source_id_and_producer_cannot_be_widened(boundary):
    b = boundary
    record = copy.deepcopy(b.record)
    record["source"]["producer_id"] = "forged-producer"
    with pytest.raises(ValueError, match="producer"):
        b.invoke(dataclasses.replace(b.prepared, canonical_record_json=canonical_transcript_json(record)))
    assert not b.calls


def test_output_contract_is_enforced_and_ports_expire_on_failure(boundary):
    b = boundary
    retained = []
    def publisher(invocation, prepared):
        retained.append(invocation)
        return {"ok": False}
    b.module.publish_transcript_source = publisher
    with pytest.raises(Exception, match="True"):
        b.invoke()
    with pytest.raises(ValueError, match="expired"):
        retained[0].objects.get_object(b.prepared.snapshot_object_id)


def test_unjoined_replay_extension_fails_without_mutation(boundary):
    b = boundary
    effects = []
    # This pin has no generated replay_only parameter. Never catch TypeError
    # and retry with an ordinary potentially mutating call.
    def old_create(project, document, kind, content, *, idempotency_key):
        effects.append(document)
    b.generated.create_document = old_create
    create_request = dataclasses.replace(b.prepared, expected_document_version=None, parent_selection=None)
    def publisher(invocation, prepared):
        with pytest.raises(WorkspaceClientError):
            invocation.documents.create_document(prepared.document_id, "media.transcript/v1", b.record,
                                                  idempotency_key=b.document_key, replay_only=True)
        return {"ok": True}
    b.module.publish_transcript_source = publisher
    b.invoke(params=create_request)
    assert not effects


def test_actual_receiver_admits_builtin_writer_declaration():
    root = document_access.builtin_media_root()
    entry = validate_canonical_pack(root)
    action, kind = document_access.select_transcript_writer(
        entry, document_access.document_source_digest(entry),
    )
    assert action["invocation"]["path"] == "actions/transcript_publish/run.py"
    assert action["inputs"] == "schemas/transcript-publication-request-v1.json"
    assert kind.kind == "media.transcript/v1"
    assert set(kind.access) == {"read", "list", "write"}


@pytest.mark.parametrize("reference", ["unadmitted.json", "https://invalid.example/schema", "../../foreign.json"])
def test_schema_reference_must_belong_to_admitted_resource_closure(boundary, reference):
    b = boundary
    path = b.root / "schemas/transcript-publication-request-v1.json"
    schema = json.loads(path.read_text())
    schema["properties"]["parent_selection"] = {"$ref": reference}
    path.write_text(json.dumps(schema))
    (b.root / "schemas/unadmitted.json").write_text('{"type": "null"}')
    digest = document_access.document_source_digest(validate_canonical_pack(b.root))
    with pytest.raises(ValueError, match="schema reference"):
        b.invoke(expected_source_digest=digest)
    assert not b.calls
