"""Release gates for Astrid's vendored workspace client.

The Runtime repository owns generation. Astrid does not invoke that repository's generator at test time: the
checked-in client is a copy, and ``runtime.lock.json`` (written by
``scripts/vendor_lock.py update``) records the digest of every file and the producer version it came from. This
gate runs the lock check, so a one-byte local edit fails here, and the CI ``boundary`` job checks the lock
against the pinned Runtime commit. No hash or schema digest is copied by hand into this file (testing plan C1, R-3).
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid_runtime_client import ManagedOutput, ObjectLocation, WorkspaceClient, generated
from astrid_runtime_client.contract_metadata import (
    COMPONENT_MANIFEST_SHA256,
    OPERATIONS,
    PROTOCOL,
    SCHEMA_DIGEST,
)
from astrid.sdk.workspace_client import WorkspaceClient as AstridWorkspaceClient

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "runtime.lock.json"
_spec = importlib.util.spec_from_file_location("vendor_lock", ROOT / "scripts" / "vendor_lock.py")
vendor_lock = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vendor_lock)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def _camel_to_snake(value: str) -> str:
    value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).lower()


def test_vendored_client_is_the_locked_runtime_artifact() -> None:
    assert vendor_lock.check(LOCK) == []
    assert {name for name in json.loads(LOCK.read_text())["files"] if name.startswith("astrid_runtime_client/")} == {
        f"astrid_runtime_client/{name}" for name in ("__init__.py", "generated.py", "contract_metadata.py")
    }


def test_vendored_client_contract_exports_agree() -> None:
    assert PROTOCOL == "workspace.v1" == generated.PROTOCOL
    assert SCHEMA_DIGEST == generated.SCHEMA_DIGEST
    assert DIGEST.fullmatch(SCHEMA_DIGEST) and DIGEST.fullmatch(COMPONENT_MANIFEST_SHA256)


def test_vendored_client_operation_catalog_matches_typed_methods() -> None:
    assert tuple(OPERATIONS) == tuple(generated.OPERATIONS)
    methods = {
        name
        for name, value in inspect.getmembers(WorkspaceClient, inspect.isfunction)
        if not name.startswith("_")
    }
    operation_methods = {_camel_to_snake(operation) for operation in OPERATIONS}
    # This is a generated typed composition, not an independent OpenAPI
    # operation ID: it composes updateDocument while retaining a convenient
    # resource-scoped method for product adapters.
    composed_helpers = {"update_timeline_document"}
    assert methods == operation_methods | composed_helpers


def test_astrid_event_wrapper_passes_latest_to_generated_client() -> None:
    calls = []
    client = object.__new__(AstridWorkspaceClient)
    client._generated = SimpleNamespace(list_events=lambda **kwargs: (calls.append(kwargs), ([], "cursor"))[1])
    assert client.list_events(from_="latest") == [[], "cursor"]
    assert calls == [{"cursor": None, "limit": 50, "aggregate_id": None, "from_": "latest"}]


@pytest.mark.parametrize("identity", [
    None,
    {"release": 1, "managed_by": None},
    {"release": 1, "managed_by": "pinokio"},
    {
        "release": 0, "managed_by": "source", "runtime_head": "a" * 40,
        "agent_head": "b" * 64, "dirty": True, "dirty_hash": "c" * 64,
    },
])
def test_vendored_health_preserves_runtime_identity(identity: object) -> None:
    health = generated.Health.from_json({
        "status": "ok",
        "protocol": "workspace.v1",
        "schema_digest": SCHEMA_DIGEST,
        "runtime_epoch": 1,
        "runtime_session_id": "runtime-session-1",
        "runtime_instance_id": "runtime-instance-1",
        "identity": identity,
    })
    assert health.runtime_session_id == "runtime-session-1"
    assert health.runtime_instance_id == "runtime-instance-1"
    assert health.identity == identity
    client = object.__new__(AstridWorkspaceClient)
    client._generated = SimpleNamespace(health=lambda: health)
    adapted = client.health()
    assert adapted["runtime_session_id"] == "runtime-session-1"
    assert adapted["runtime_instance_id"] == "runtime-instance-1"
    assert adapted["identity"] == identity


def test_frozen_mutation_signatures_require_idempotency_keys() -> None:
    for name in (
        "update_timeline_document",
        "create_generation",
        "create_variant",
        "export_managed_output",
        "adopt_managed_output",
        "update_managed_output_lifecycle",
        "promote_project_shot_candidate",
    ):
        parameter = inspect.signature(getattr(WorkspaceClient, name)).parameters["idempotency_key"]
        assert parameter.default is inspect.Parameter.empty


def test_object_location_preserves_typed_wire_contract(monkeypatch) -> None:
    payload = {
        "object_id": "object", "digest": "sha256:abc", "size": 12,
        "media_type": "video/mp4", "local_path": "/runtime/object.mp4",
        "storage": "local", "verified": True, "filename": "object.mp4",
    }
    calls = []

    def request(self, method, path):
        calls.append((method, path))
        return 200, {}, json.dumps(payload).encode()

    monkeypatch.setattr(WorkspaceClient, "_request", request)
    client = object.__new__(WorkspaceClient)
    result = client.get_project_object_location("project/one", "object/two")
    assert isinstance(result, ObjectLocation)
    assert result == ObjectLocation(**payload)
    assert calls == [("GET", "/v1/projects/project%2Fone/objects/object%2Ftwo/location")]


def test_both_api_families_are_exported() -> None:
    assert ObjectLocation is generated.ObjectLocation
    assert ManagedOutput is generated.ManagedOutput
    assert "getProjectObjectLocation" in OPERATIONS
    for name in (
        "list_managed_outputs", "get_managed_output", "export_managed_output",
        "adopt_managed_output", "update_managed_output_lifecycle",
    ):
        assert callable(getattr(WorkspaceClient, name))


def test_preferences_and_documents_operations_are_in_the_runtime_contract() -> None:
    assert "getPreferences" in OPERATIONS
    assert "updatePreferences" in OPERATIONS
    assert "listDocuments" in OPERATIONS
    assert "createDocument" in OPERATIONS
    assert "getDocument" in OPERATIONS
    assert "updateDocument" in OPERATIONS
    for name in (
        "get_preferences", "update_preferences", "create_document",
        "list_documents", "get_document", "update_document",
    ):
        assert callable(getattr(WorkspaceClient, name))


def test_obsolete_generic_client_artifact_is_absent() -> None:
    assert not (ROOT / "generated" / "runtime_client.py").exists()
    assert not (ROOT / "generated" / "runtime_client_metadata.py").exists()
    assert not (ROOT / "scripts" / "generate_runtime_client.py").exists()


def test_generation_preserves_source_task_identity():
    row = generated.Generation.from_json({"generation_id": "g", "project_id": "p", "source_task_id": "task-1", "type": "image", "status": "completed", "version": 1, "created_at": "now", "updated_at": "now"})
    assert row.source_task_id == "task-1"
