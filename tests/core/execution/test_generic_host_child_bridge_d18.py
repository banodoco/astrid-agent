"""D18 offline proof: local sockets, disposable Runtime realm, fake CAS bytes.

Set ASTRID_D18_RUNTIME_SOURCE to the audited Runtime worktree. Its canonical
client is loaded under a private module name; no vendored source is replaced.
ASTRID_D18_USE_VENDORED_CLIENT=1 retains the normal host-created client class
and substitutes only its offline transport after the B01 vendor sync.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from astrid.core.execution._child_bridge import ChildBridgeError, HostChildBridge, _child_wire_id, _snapshot, resource
from astrid.core.execution.generic_host import GenericPackHost, HostError, RuntimeProtocolClient, _DiscoveryCollection, _prepare_review_resume_input
from astrid.sdk._child_bridge import ChildBridge, _BridgeRejected
from astrid.sdk.execution_request import normalize_execution_request
from banodoco_workspace_client import ApiError, WorkspaceClient

PARENT = "bridge.parent"
CHILD = "bridge.child"
SECRET = "worker-secret-must-not-cross-bridge"


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def discovery_collection_fixture():
    """Wire-body fixture independent of a Runtime process, CAS or network."""
    grant = {"project_id": "P", "run_id": "historical-run", "task_id": "historical-task",
             "attempt_id": "historical-attempt", "capability_id": PARENT,
             "capability_digest": digest(b"parent"), "limits": {
                 "max_discovery_rows": 750, "max_discovery_metadata_bytes": 67108864,
                 "max_selected_output_objects": 2, "max_selected_output_bytes": 16777216,
                 "max_child_media_bindings": 1, "max_child_media_bytes": 16777216}}
    binding = {"status": "claimed", "task_id": "parent-task", "attempt_id": "parent-attempt",
               "lease_id": "parent-lease", "fence": 1, "runtime_epoch": 1}
    parent = {"task_id": "parent-task", "run_id": "parent-run", "project_id": "P",
              "capability_id": PARENT, "capability_digest": grant["capability_digest"],
              "state": "running", "attempt_id": "parent-attempt", "lease_fence": 1,
              "runtime_epoch": 1, "lease_expires_at": "2099-01-01T00:00:00+00:00",
              "execution_binding": binding, "spec": {"capability_digest": grant["capability_digest"],
                  "child_delegation": {"capabilities": [{"capability_id": CHILD,
                      "capability_digest": digest(b"child")}], "targets": [{"kind": "default"}],
                      "input_object_ids": [], "discovery_grant": grant}}}
    row = {"association_id": "association", "project_id": "P", "run_id": grant["run_id"],
           "task_id": grant["task_id"], "attempt_id": grant["attempt_id"],
           "object_id": digest(b"historical-bytes"), "digest": digest(b"historical-bytes"),
           "size": len(b"historical-bytes")}
    responses = {
        ("get_task", "parent-task"): parent,
        ("get_project", "P"): {"project_id": "P"},
        ("get_run", grant["run_id"]): {"run_id": grant["run_id"], "project_id": "P",
                                       "task_ids": [grant["task_id"]]},
        ("get_task", grant["task_id"]): {"task_id": grant["task_id"], "run_id": grant["run_id"],
            "project_id": "P", "attempt_id": "newer-current-attempt"},
        ("list_managed_outputs", grant["task_id"]): {"items": [row, copy.deepcopy(row),
            {**row, "association_id": "newer-association", "attempt_id": "newer-current-attempt"}],
            "next_cursor": None},
        ("get_managed_output", "association"): copy.deepcopy(row)}
    calls = []

    class Generated(WorkspaceClient):
        def __init__(self):
            def transport(method, key, headers, body):
                calls.append(key)
                value = responses[key]
                if isinstance(value, Exception):
                    raise value
                body = value if type(value) is bytes else json.dumps(value, indent=2).encode()
                return 200, {}, body
            super().__init__("http://offline", "shared-auth", transport=transport)

        def typed(self, operation, identifier):
            # Model generated projection losing unknown fields/coercing values.
            raw = json.loads(self._request("GET", (operation, identifier))[2])
            return {key: value for key, value in raw.items() if key in {"task_id", "project_id"}}

        def get_task(self, identifier):
            return self.typed("get_task", identifier)

        def get_project(self, identifier):
            return self.typed("get_project", identifier)

        def get_run(self, identifier):
            return self.typed("get_run", identifier)

        def list_managed_outputs(self, identifier):
            return self.typed("list_managed_outputs", identifier)

        def get_managed_output(self, identifier):
            return self.typed("get_managed_output", identifier)

    client = SimpleNamespace(generated=Generated())
    cancelled = [False]

    def collection():
        return _DiscoveryCollection(client, parent, attempt_id="parent-attempt", lease_id="parent-lease",
                                    fence=1, runtime_epoch=1, cancelled=lambda: cancelled[0])

    return SimpleNamespace(client=client, parent=parent, grant=grant, row=row, responses=responses,
                           calls=calls, cancelled=cancelled, collection=collection)


def test_discovery_collection_exact_historical_identity_and_private_meter():
    f = discovery_collection_fixture()
    shared_request = f.client.generated._request
    shared_attributes = dict(vars(f.client.generated))
    reader = f.collection()
    result = reader.collect_metadata()
    assert result["task"]["attempt_id"] == "newer-current-attempt"
    assert result["outputs"] == [f.row]
    assert reader.examined_rows == 9  # Includes duplicate/discarded associations.
    assert reader.raw_metadata_bytes > reader.canonical_metadata_bytes > 0
    assert reader.read_attempts == len(f.calls) == 7
    assert reader._reader is not f.client.generated
    assert f.client.generated._request == shared_request
    assert vars(f.client.generated) == shared_attributes
    assert all(operation in {"get_task", "get_project", "get_run", "list_managed_outputs",
                             "get_managed_output"} for operation, _ in f.calls)


class _MetadataTrackingStream(io.BytesIO):
    def __init__(self, body, chunk, headers):
        super().__init__(body)
        self.status = 200
        self.chunk = chunk
        self.headers = headers
        self.read_sizes = []
        self.acquired = 0

    def read(self, size=-1):
        self.read_sizes.append(size)
        data = super().read(min(size, self.chunk) if size >= 0 else size)
        self.acquired += len(data)
        return data


@pytest.mark.parametrize("http_error", [False, True])
@pytest.mark.parametrize("head", [{}, {"Content-Length": "0"}, {"Content-Length": "999999999"}])
@pytest.mark.parametrize("chunk", [1, 7, 65536])
@pytest.mark.parametrize("slack", [-1, 0, 5])
def test_discovery_acquisition_production_cap_and_error_identity(monkeypatch, http_error, head, chunk, slack):
    from banodoco_workspace_client import generated
    body = json.dumps({"code": "conflict", "message": "é🙂", "details": {"field": "x"},
                       "request_id": "wire-request"}, ensure_ascii=False).encode()
    cap = len(body) + slack
    f = discovery_collection_fixture()
    f.grant["limits"]["max_discovery_metadata_bytes"] = cap
    f.client.generated = WorkspaceClient("http://runtime", "shared-auth")
    shared = dict(vars(f.client.generated))
    reader = f.collection()
    stream = _MetadataTrackingStream(body, chunk, head)
    def open_response(request, timeout):
        assert request.get_header("Authorization") == "Bearer shared-auth"
        if http_error:
            raise urllib.error.HTTPError(request.full_url, 409, "Conflict", head, stream)
        return stream
    monkeypatch.setattr(urllib.request, "urlopen", open_response)
    decoded = []
    decode_error = generated._decode_error
    monkeypatch.setattr(generated, "_decode_error", lambda *args, **kwargs: decoded.append(True) or decode_error(*args, **kwargs))
    observed = []
    observe = reader._observe_metadata
    reader._observe_metadata = lambda data: observed.append(True) or observe(data)
    if slack < 0:
        with pytest.raises(HostError, match="raw metadata budget"):
            reader._reader._request("GET", "/probe")
        assert stream.acquired == cap + 1
        assert reader.raw_metadata_bytes == 0 and reader._raw is None
        assert reader.rejected_read == {"reason": "discovery raw metadata budget exceeded",
            "attempted_call_index": 1, "accepted_bytes_before_rejection": 0,
            "raw_metadata_bytes_lower_bound": cap + 1}
        assert not decoded and not observed
    elif http_error:
        with pytest.raises(ApiError) as caught:
            reader._reader._request("GET", "/probe")
        error = caught.value
        assert (error.status, error.code, error.details, error.request_id) == (409, "conflict", {"field": "x"}, "wire-request")
        assert reader.raw_metadata_bytes == len(body) and decoded and observed
    else:
        assert reader._reader._request("GET", "/probe")[2] == body
        assert reader.raw_metadata_bytes == len(body) and observed and not decoded
    assert stream.closed and stream.acquired <= cap + 1
    assert all(0 < size <= cap + 1 for size in stream.read_sizes)
    assert vars(f.client.generated) == shared
    if slack < 0 or http_error:
        reads = list(stream.read_sizes)
        with pytest.raises(HostError, match="already ended"):
            reader._reader._request("GET", "/follow-on")
        assert stream.read_sizes == reads


@pytest.mark.parametrize("http_error", [False, True])
@pytest.mark.parametrize("body", [b'{"code":"private-prefix"', b'{"message":"\xc3', b'[]'])
def test_discovery_acquisition_truncated_json_is_terminal_before_error_decode(monkeypatch, http_error, body):
    from banodoco_workspace_client import generated
    f = discovery_collection_fixture()
    f.client.generated = WorkspaceClient("http://runtime")
    reader = f.collection()
    stream = _MetadataTrackingStream(body, 3, {})
    def open_response(request, timeout):
        if http_error:
            raise urllib.error.HTTPError(request.full_url, 400, "Bad", {}, stream)
        return stream
    monkeypatch.setattr(urllib.request, "urlopen", open_response)
    monkeypatch.setattr(generated, "_decode_error", lambda *args, **kwargs: pytest.fail("truncated error decoded"))
    with pytest.raises(HostError) as caught:
        reader._reader._request("GET", "/probe")
    assert reader.failed and reader._raw is None and stream.closed
    assert caught.value.__context__ is None or isinstance(caught.value.__context__, urllib.error.HTTPError)
    assert "private-prefix" not in json.dumps(reader.rejected_read)
    assert reader.raw_metadata_bytes == len(body)
    assert reader.rejected_read["raw_metadata_bytes_lower_bound"] == len(body)
    reads = list(stream.read_sizes)
    with pytest.raises(HostError, match="already ended"):
        reader._reader._request("GET", "/follow-on")
    assert stream.read_sizes == reads


@pytest.mark.parametrize("cap", [0, -1, True, 1.5, None, "8", "exhausted"])
def test_discovery_acquisition_invalid_or_exhausted_cap_precedes_transport(cap):
    f = discovery_collection_fixture()
    reader = f.collection()
    if cap == "exhausted":
        reader.raw_metadata_bytes = reader.grant["limits"]["max_discovery_metadata_bytes"]
    else:
        reader.grant["limits"]["max_discovery_metadata_bytes"] = cap
    with pytest.raises(HostError, match="invalid or exhausted"):
        reader._reader._request("GET", ("get_project", "P"))
    assert reader.failed and not f.calls


def test_discovery_acquisition_remaining_budget_error_shared_client_and_binary_isolation(monkeypatch):
    f = discovery_collection_fixture()
    cap = 100
    f.grant["limits"]["max_discovery_metadata_bytes"] = cap
    f.client.generated = WorkspaceClient("http://runtime", "shared-auth")
    shared = dict(vars(f.client.generated))
    reader = f.collection()
    streams = []
    error = [False]
    def open_response(request, timeout):
        body = b'{"project_id":"P"}' if not error[0] else b'{"code":"bad"}'
        stream = _MetadataTrackingStream(body, 65536, {})
        streams.append(stream)
        if error[0]:
            raise urllib.error.HTTPError(request.full_url, 409, "Conflict", {}, stream)
        return stream
    monkeypatch.setattr(urllib.request, "urlopen", open_response)
    reader._reader._request("GET", "/first")
    accepted = streams[0].acquired
    assert streams[0].read_sizes[0] == cap + 1
    reader._object_read = True
    reader._reader._request("GET", "/binary")
    reader._object_read = False
    assert streams[1].read_sizes == [-1] and reader.raw_metadata_bytes == accepted
    assert reader.acquired_object_bytes == accepted
    error[0] = True
    with pytest.raises(ApiError):
        reader._reader._request("GET", "/error")
    assert streams[2].read_sizes[0] == cap - accepted + 1
    assert reader.raw_metadata_bytes == accepted + streams[2].acquired
    assert reader.failed
    assert vars(f.client.generated) == shared
    error[0] = False
    f.client.generated._request("GET", "/heartbeat")
    assert streams[3].read_sizes == [-1]
    assert all(stream.closed for stream in streams)


def test_discovery_collection_unknown_raw_fields_count_before_typed_projection():
    f = discovery_collection_fixture()
    f.grant["limits"]["max_discovery_metadata_bytes"] = 10000
    f.responses[("get_project", "P")]["unknown_payload"] = "secret" * 4000
    reader = f.collection()
    with pytest.raises(HostError, match="raw metadata budget"):
        reader.collect_metadata()
    rejected = reader.rejected_read
    assert rejected["attempted_call_index"] == 2
    assert rejected["accepted_bytes_before_rejection"] == reader.raw_metadata_bytes > 0
    assert rejected["raw_metadata_bytes_lower_bound"] > 10000
    assert "secret" not in json.dumps(rejected)
    before = list(f.calls)
    with pytest.raises(HostError, match="already ended"):
        reader.collect_metadata()
    assert f.calls == before


@pytest.mark.parametrize("cursor", ["next", "", 42, False])
def test_discovery_collection_rejects_every_managed_output_continuation(cursor):
    f = discovery_collection_fixture()
    f.responses[("list_managed_outputs", f.grant["task_id"])]["next_cursor"] = cursor
    reader = f.collection()
    with pytest.raises(HostError):
        reader.collect_metadata()
    assert reader.failed
    assert not any(operation == "get_managed_output" for operation, _ in f.calls)


@pytest.mark.parametrize("field", ["project_id", "run_id", "task_id"])
def test_discovery_collection_rejects_foreign_association(field):
    f = discovery_collection_fixture()
    f.row[field] = "foreign"
    reader = f.collection()
    with pytest.raises(HostError, match="foreign"):
        reader.collect_metadata()
    assert reader.failed


@pytest.mark.parametrize("field", ["project_id", "capability_id", "capability_digest"])
def test_discovery_collection_rejects_wrong_root_grant_identity(field):
    f = discovery_collection_fixture()
    f.grant[field] = digest(b"wrong") if field == "capability_digest" else "wrong"
    with pytest.raises(HostError):
        f.collection()
    assert not f.calls


@pytest.mark.parametrize("mutation", ["fence", "epoch", "spec", "binding", "cancelled"])
def test_discovery_collection_latches_parent_authority_loss(mutation):
    f = discovery_collection_fixture()
    reader = f.collection()
    if mutation == "fence":
        f.parent["lease_fence"] = True  # Do not allow bool-to-int projection.
    elif mutation == "epoch":
        f.parent["runtime_epoch"] = 2
    elif mutation == "spec":
        f.parent["spec"]["unrelated"] = "changed"
    elif mutation == "binding":
        f.parent["execution_binding"]["lease_id"] = "replacement"
    else:
        f.cancelled[0] = True
    with pytest.raises(HostError):
        reader.collect_metadata()
    assert reader.failed
    assert not any(operation == "get_project" for operation, _ in f.calls)


def test_discovery_collection_allows_only_runtime_owned_registry_mutation():
    f = discovery_collection_fixture()
    reader = f.collection()
    f.parent["spec"]["derived_input_registry"] = {"association": {"object_id": digest(b"derived")}}
    assert reader.collect_metadata()["outputs"] == [f.row]


def test_discovery_collection_rejects_row_budget_and_changed_readback():
    f = discovery_collection_fixture()
    f.grant["limits"]["max_discovery_rows"] = 4
    reader = f.collection()
    with pytest.raises(HostError, match="row budget"):
        reader.collect_metadata()
    assert reader.examined_rows == 7
    f = discovery_collection_fixture()
    f.responses[("get_managed_output", "association")]["size"] += 1
    reader = f.collection()
    with pytest.raises(HostError, match="readback changed"):
        reader.collect_metadata()


@pytest.mark.parametrize("mutation", ["missing_limit", "boolean_limit", "over_limit", "unknown", "wildcard"])
def test_discovery_collection_requires_strict_persisted_grant(mutation):
    f = discovery_collection_fixture()
    if mutation == "missing_limit":
        f.grant["limits"].pop("max_child_media_bytes")
    elif mutation == "boolean_limit":
        f.grant["limits"]["max_selected_output_objects"] = True
    elif mutation == "over_limit":
        f.grant["limits"]["max_discovery_rows"] = 751
    elif mutation == "unknown":
        f.grant["execution_target"] = {"kind": "default"}
    else:
        f.grant["task_id"] = "*"
    with pytest.raises(HostError, match="persisted discovery policy"):
        f.collection()
    assert not f.calls


@pytest.mark.parametrize("body", [b'{"project_id":"P","project_id":"other"}',
                                  b'{"project_id":"P","unknown":NaN}', b'[]'])
def test_discovery_collection_latches_invalid_raw_json(body):
    f = discovery_collection_fixture()
    f.responses[("get_project", "P")] = body
    reader = f.collection()
    with pytest.raises((HostError, ValueError)):
        reader.collect_metadata()
    assert reader.failed
    calls = list(f.calls)
    with pytest.raises(HostError, match="already ended"):
        reader.collect_metadata()
    assert f.calls == calls


@pytest.fixture
def discovery_objects(tmp_path):
    """Real confinement routines with deterministic generated-client bytes."""
    f = discovery_collection_fixture()
    data = b"historical-bytes"
    f.row.update(media_type="video/mp4", filename="../../caller-path.mp4", role="output",
                 durability="durable", state="available", lifecycle={"state": "available"})
    second = {**copy.deepcopy(f.row), "association_id": "second-association"}
    f.responses[("list_managed_outputs", f.grant["task_id"])]["items"] = [f.row, copy.deepcopy(f.row), second]
    for row in (f.row, second):
        f.responses[("get_managed_output", row["association_id"])] = copy.deepcopy(row)
    f.responses[("get_object", f.row["object_id"])] = data
    f.after_object = None

    def get_object(client, object_id):
        status, headers, body = client._request("GET", ("get_object", object_id))
        if f.after_object:
            f.after_object()
        return SimpleNamespace(data=body, status=status, headers=headers)

    type(f.client.generated).get_object = get_object
    b = HostChildBridge.__new__(HostChildBridge)
    b.context = {"project_id": "P", "run_id": "parent-run", "task_id": "parent-task",
                 "attempt_id": "parent-attempt", "lease_id": "parent-lease", "fence": 1, "runtime_epoch": 1}
    b.output_root = tmp_path / "current-parent-attempt" / "outputs"
    b.output_root.mkdir(parents=True)
    b._attempt_fd = os.open(b.output_root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    b._attempt_identity = b._directory_identity(os.fstat(b._attempt_fd))
    b._output_identity = b._directory_identity(os.stat(b.output_root, follow_symlinks=False))
    b._fd_lock = threading.Lock()
    b._materialization_lock = threading.Lock()
    b._materialization_blocked = False
    b._discovery_preparations = {}
    b._revoked = threading.Event()
    b.cancelled = lambda: f.cancelled[0]
    f.bridge = b
    f.second = second
    try:
        yield f
    finally:
        os.close(b._attempt_fd)


def test_discovery_object_preparation_deduplicates_cas_preserving_associations(discovery_objects):
    f = discovery_objects
    reader = f.collection()
    caller_map = reader.collect_metadata()
    caller_map["outputs"][0]["object_id"] = digest(b"caller-forged")
    prepared = reader.prepare_objects(f.bridge)
    assert len(prepared) == 1
    descriptor = prepared[0]
    assert descriptor["source_association_ids"] == ("association", "second-association")
    assert descriptor["parent_task_id"] == "parent-task"
    assert descriptor["parent_attempt_id"] == "parent-attempt"
    assert descriptor["filename"] == "discovery-objects/" + f.row["object_id"][7:] + "/object"
    path = f.bridge.output_root / descriptor["filename"]
    assert path.read_bytes() == b"historical-bytes"
    assert reader.acquired_object_bytes == len(b"historical-bytes")
    assert sum(operation == "get_object" for operation, _ in f.calls) == 1
    assert reader.prepare_objects(f.bridge) == prepared
    assert sum(operation == "get_object" for operation, _ in f.calls) == 1
    assert not any("authority" in operation or "admit" in operation for operation, _ in f.calls)


@pytest.mark.parametrize("mutation", ["digest", "size", "boolean_size", "temporary", "deleted", "role"])
def test_discovery_object_preparation_rejects_invalid_descriptors_before_acquisition(discovery_objects, mutation):
    f = discovery_objects
    for row in f.responses[("list_managed_outputs", f.grant["task_id"])]["items"]:
        if mutation == "digest": row["digest"] = digest(b"different")
        elif mutation == "size": row["size"] = -1
        elif mutation == "boolean_size": row["size"] = True
        elif mutation == "temporary": row["durability"] = "temporary"
        elif mutation == "deleted": row.update(state="deleted", lifecycle={"state": "deleted"})
        else: row["role"] = "derived_input"
        f.responses[("get_managed_output", row["association_id"])] = copy.deepcopy(row)
    reader = f.collection()
    reader.collect_metadata()
    with pytest.raises(HostError, match="descriptor or lifecycle"):
        reader.prepare_objects(f.bridge)
    assert not any(operation == "get_object" for operation, _ in f.calls)
    assert not (f.bridge.output_root / "discovery-objects").exists()


@pytest.mark.parametrize("budget", ["count", "bytes"])
def test_discovery_object_preparation_checks_selected_caps_before_acquisition(discovery_objects, budget):
    f = discovery_objects
    if budget == "count":
        f.grant["limits"]["max_selected_output_objects"] = 1
        f.second.update(object_id=digest(b"other"), digest=digest(b"other"), size=5)
        f.responses[("get_managed_output", f.second["association_id"])] = copy.deepcopy(f.second)
    else:
        f.grant["limits"]["max_selected_output_bytes"] = len(b"historical-bytes") - 1
    reader = f.collection()
    reader.collect_metadata()
    with pytest.raises(HostError, match="selected object budget"):
        reader.prepare_objects(f.bridge)
    assert not any(operation == "get_object" for operation, _ in f.calls)


@pytest.mark.parametrize("body", [b"short", b"X" * len(b"historical-bytes")])
def test_discovery_object_preparation_verifies_cas_size_and_digest(discovery_objects, body):
    f = discovery_objects
    f.responses[("get_object", f.row["object_id"])] = body
    reader = f.collection()
    reader.collect_metadata()
    with pytest.raises(HostError, match="bytes failed verification"):
        reader.prepare_objects(f.bridge)
    assert reader.failed
    assert not (f.bridge.output_root / "discovery-objects").exists()


@pytest.mark.parametrize("loss", ["association", "cancelled", "lease", "binding"])
def test_discovery_object_preparation_rechecks_after_acquisition(discovery_objects, loss):
    f = discovery_objects
    def change():
        if loss == "association":
            f.responses[("get_managed_output", "association")]["state"] = "deleted"
        elif loss == "cancelled": f.cancelled[0] = True
        elif loss == "lease": f.parent["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
        else: f.parent["execution_binding"]["lease_id"] = "replacement"
    f.after_object = change
    reader = f.collection()
    reader.collect_metadata()
    with pytest.raises(HostError):
        reader.prepare_objects(f.bridge)
    assert reader.failed
    assert not (f.bridge.output_root / "discovery-objects").exists()


@pytest.mark.parametrize("attack", ["namespace_symlink", "digest_symlink", "output_replaced", "collision"])
def test_discovery_object_staging_rejects_confinement_attacks(discovery_objects, tmp_path, attack):
    f = discovery_objects
    namespace = f.bridge.output_root / "discovery-objects"
    outside = tmp_path / "outside"; outside.mkdir()
    if attack == "namespace_symlink": namespace.symlink_to(outside, target_is_directory=True)
    elif attack == "output_replaced":
        f.bridge.output_root.rename(tmp_path / "old-outputs")
        f.bridge.output_root.mkdir()
    elif attack == "digest_symlink":
        namespace.mkdir()
        (namespace / f.row["object_id"][7:]).symlink_to(outside, target_is_directory=True)
    else:
        target = namespace / f.row["object_id"][7:] / "object"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"collision")
    reader = f.collection()
    reader.collect_metadata()
    with pytest.raises(Exception): reader.prepare_objects(f.bridge)
    assert list(outside.iterdir()) == []
    if attack == "collision": assert target.read_bytes() == b"collision"


@pytest.mark.parametrize("attack", ["replace", "symlink", "hardlink", "corrupt"])
def test_discovery_object_staging_replay_revalidates_file_identity(discovery_objects, tmp_path, attack):
    f = discovery_objects
    reader = f.collection()
    reader.collect_metadata()
    descriptor = reader.prepare_objects(f.bridge)[0]
    path = f.bridge.output_root / descriptor["filename"]
    replacement = tmp_path / "replacement"; replacement.write_bytes(b"historical-bytes")
    if attack == "corrupt": path.write_bytes(b"corrupt")
    else:
        path.unlink()
        if attack == "replace": path.write_bytes(b"historical-bytes")
        elif attack == "symlink": path.symlink_to(replacement)
        else: os.link(replacement, path)
    with pytest.raises(Exception): reader.prepare_objects(f.bridge)
    assert reader.failed


def test_discovery_object_staging_requires_current_attempt_and_collection(discovery_objects):
    f = discovery_objects
    reader = f.collection()
    with pytest.raises(HostError, match="completed collection"):
        reader.prepare_objects(f.bridge)
    reader = f.collection()
    reader.collect_metadata()
    f.bridge.context["attempt_id"] = "historical-attempt"
    with pytest.raises(HostError, match="different parent attempt"):
        reader.prepare_objects(f.bridge)
    assert not any(operation == "get_object" for operation, _ in f.calls)


@pytest.mark.parametrize("loss", ["cancelled", "lease", "bridge_revoked"])
def test_discovery_object_staging_authority_loss_cleans_unpublished_file(discovery_objects, monkeypatch, loss):
    f = discovery_objects
    reader = f.collection()
    reader.collect_metadata()
    original = f.bridge._write_materialized_file
    def lose_authority(*args, **kwargs):
        if loss == "cancelled": f.cancelled[0] = True
        elif loss == "lease": f.parent["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
        else: f.bridge._revoked.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(f.bridge, "_write_materialized_file", lose_authority)
    with pytest.raises(HostError): reader.prepare_objects(f.bridge)
    assert reader.failed
    assert not any(path.is_file() for path in f.bridge.output_root.rglob("*"))
    assert f.bridge._discovery_preparations == {}


@pytest.fixture
def prepared_submission(discovery_objects, monkeypatch):
    """Exercise existing _submit and Protocol receipt handling with fake transport."""
    from astrid.core.execution import _child_bridge as module
    f = discovery_objects
    ports = tuple(SimpleNamespace(name=name, type="file", required=name == "media", default=None)
                  for name in ("media", "second_media", "document"))
    definition = SimpleNamespace(id=CHILD, inputs=ports, metadata={"action_invocation": True})
    record = SimpleNamespace(id=CHILD, capability_digest=digest(b"child"), definition=definition)
    host = SimpleNamespace(capabilities={CHILD: record}, executor_id="worker",
                           admit=lambda *args: (record, SimpleNamespace()))
    monkeypatch.setattr(module, "_verify_admitted_source", lambda admission: None)
    policy = f.parent["spec"]["child_delegation"]
    policy["limits"] = {"max_children": 4, "max_active_children": 2, "max_derived_objects": 4,
                        "max_derived_bytes": 1024, "max_child_inputs": 4, "max_child_bytes": 1024,
                        "max_recoverable_snapshots": 1, "max_recoverable_bytes": 1024, "max_snapshot_bytes": 1024}
    b = f.bridge
    b.host, b.policy = host, policy
    b._children, b._admitted_children = {}, {}
    b._admission_lock = threading.Lock()
    b._channel_closed = False
    protocol = RuntimeProtocolClient.__new__(RuntimeProtocolClient)
    protocol.generated = f.client.generated
    protocol.executor_id = "worker"
    protocol._attempt_runtime_epochs = {"parent-attempt": 1}
    protocol._heartbeat_lock = threading.RLock()
    protocol.health = lambda: {"runtime_epoch": 1}
    protocol.task = lambda task_id: copy.deepcopy(f.parent)
    protocol.heartbeat = lambda *args, **kwargs: None
    b.client = protocol
    f.mutations = []
    f.after_upload = None
    f.receipt_mutation = None

    def ingest(data, **kwargs):
        f.mutations.append(("upload", data, copy.deepcopy(kwargs)))
        if f.after_upload: f.after_upload()
        return {}

    def issue(attempt_id, **kwargs):
        f.mutations.append(("issue", attempt_id, copy.deepcopy(kwargs)))
        refs = [{**row, "parent_attempt_id": attempt_id, "association_id": "derived-" + row["name"]}
                for row in kwargs["derived_inputs"]]
        receipt = {"parent_task_id": "parent-task", "parent_attempt_id": attempt_id,
                   "child": kwargs["child"], "derived_inputs": refs, "authority": "fixture-private-authority"}
        if f.receipt_mutation == "order": receipt["derived_inputs"] = list(reversed(refs))
        elif f.receipt_mutation == "digest": refs[0]["object_id"] = digest(b"wrong")
        elif f.receipt_mutation == "parent": receipt["parent_attempt_id"] = "foreign"
        return receipt

    def admit(**kwargs):
        f.mutations.append(("admit", copy.deepcopy(kwargs)))
        task = kwargs["task"]
        lineage = {"parent_task_id": "parent-task", "parent_attempt_id": "parent-attempt",
                   "parent_lease_id": "parent-lease", "parent_fence": 1, "runtime_epoch": 1,
                   "project_id": "P", "executor_id": "worker"}
        if f.receipt_mutation == "lineage": lineage["parent_attempt_id"] = "foreign"
        return {"task_id": "child-task", "run_id": "child-run", "state": "queued",
                "capability_id": task["capability_id"], "capability_digest": task["capability_digest"],
                "idempotency_key": kwargs["idempotency_key"],
                "spec": {**task["spec"], "delegated_parent": lineage}}

    protocol.generated.ingest_object = ingest
    protocol.generated.issue_child_authority = issue
    protocol.generated.admit_delegated_task = admit
    f.request = {"v": 1, "request_id": 1, "op": "submit", "child": {
        "capability_id": CHILD, "capability_digest": record.capability_digest}, "inputs": {},
        "input_descriptors": [], "child_key": "prepared-child", "wait": False,
        "timeout_seconds": 1, "poll_seconds": 0.01}
    f.reader = f.collection()
    f.reader.collect_metadata()
    f.prepared = f.reader.prepare_objects(b)
    return f


def test_prepared_submission_uses_current_attempt_receipts_and_private_provenance(prepared_submission):
    f = prepared_submission
    result = f.reader.submit_prepared_child(f.bridge, f.request, media_bindings={"media": "association"})
    assert result["task_id"] == "child-task"
    assert [row[0] for row in f.mutations] == ["upload", "issue", "admit"]
    upload = f.mutations[0]
    assert upload[1] == b"historical-bytes"
    assert upload[2]["upload_binding"]["attempt_id"] == "parent-attempt"
    assert upload[2]["upload_binding"]["task_id"] == "parent-task"
    assert f.mutations[1][1] == "parent-attempt"
    task = f.mutations[2][1]["task"]
    assert task["execution_request"]["inputs"][0]["required"] is True
    assert task["execution_request"]["inputs"][0]["object_id"] == f.row["object_id"]
    snapshot = f.bridge.admitted_children_snapshot().children[0]
    provenance = json.loads(snapshot.discovery_provenance_json)["media"]
    assert provenance["source_association_ids"] == ["association", "second-association"]
    assert provenance["selected_association_id"] == "association"
    assert provenance["parent_attempt_id"] == "parent-attempt"
    assert "authority" not in json.dumps(result)
    calls = len(f.mutations)
    assert f.reader.submit_prepared_child(f.bridge, f.request, media_bindings={"media": "association"}) == result
    assert len(f.mutations) == calls


@pytest.mark.parametrize("attack", ["replacement", "between_snapshot_and_upload", "forged_selection", "forged_input"])
def test_prepared_submission_rejects_replacements_and_forged_selection(prepared_submission, monkeypatch, attack):
    f = prepared_submission
    path = f.bridge.output_root / f.prepared[0]["filename"]
    bindings = {"media": "association"}
    if attack == "replacement":
        path.unlink(); path.write_bytes(b"historical-bytes")
    elif attack == "between_snapshot_and_upload":
        original = f.bridge._verify_prepared_snapshot
        calls = [0]
        def replace(*args):
            calls[0] += 1
            if calls[0] == 2:
                path.unlink(); path.write_bytes(b"historical-bytes")
            return original(*args)
        monkeypatch.setattr(f.bridge, "_verify_prepared_snapshot", replace)
    elif attack == "forged_selection": bindings["media"] = "caller-association"
    else: f.request["inputs"]["media"] = {"filename": "caller", "output_port": "media", "media_type": "video/mp4"}
    with pytest.raises(HostError):
        f.reader.submit_prepared_child(f.bridge, f.request, media_bindings=bindings)
    assert not f.mutations


@pytest.mark.parametrize("budget", ["media_count", "media_bytes", "child_inputs", "child_bytes", "derived_objects", "derived_bytes"])
def test_prepared_submission_caps_precede_any_mutation(prepared_submission, budget):
    f = prepared_submission
    bindings = {"media": "association"}
    if budget == "media_count": bindings["second_media"] = "second-association"
    elif budget == "media_bytes":
        f.reader.grant["limits"]["max_child_media_bytes"] = 1
        f.bridge.policy["discovery_grant"]["limits"]["max_child_media_bytes"] = 1
        f.reader.parent["spec"]["child_delegation"]["discovery_grant"]["limits"]["max_child_media_bytes"] = 1
    else:
        if budget in {"child_inputs", "derived_objects"}:
            (f.bridge.output_root / "document.json").write_bytes(b"{}")
            value = {"filename": "document.json", "output_port": "document", "media_type": "application/json"}
            f.request["inputs"]["document"] = value
            f.request["input_descriptors"] = [{"name": "document", "kind": "producer_file", **value}]
        key = {"child_inputs": "max_child_inputs", "child_bytes": "max_child_bytes",
               "derived_objects": "max_derived_objects", "derived_bytes": "max_derived_bytes"}[budget]
        f.bridge.policy["limits"][key] = 1
        f.reader.parent["spec"]["child_delegation"]["limits"][key] = 1
    with pytest.raises(HostError):
        f.reader.submit_prepared_child(f.bridge, f.request, media_bindings=bindings)
    assert not f.mutations


@pytest.mark.parametrize("loss", ["cancelled", "lease", "source"])
def test_prepared_submission_rechecks_between_upload_and_admission(prepared_submission, loss):
    f = prepared_submission
    def change():
        if loss == "cancelled": f.cancelled[0] = True
        elif loss == "lease": f.parent["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
        else: f.responses[("get_managed_output", "association")]["state"] = "deleted"
    f.after_upload = change
    with pytest.raises(HostError):
        f.reader.submit_prepared_child(f.bridge, f.request, media_bindings={"media": "association"})
    assert [row[0] for row in f.mutations] == ["upload"]


@pytest.mark.parametrize("mutation", ["order", "digest", "parent", "lineage"])
def test_prepared_submission_preserves_receipt_order_and_lineage_validation(prepared_submission, mutation):
    f = prepared_submission
    (f.bridge.output_root / "document.json").write_bytes(b"{}")
    value = {"filename": "document.json", "output_port": "document", "media_type": "application/json"}
    f.request["inputs"]["document"] = value
    f.request["input_descriptors"] = [{"name": "document", "kind": "producer_file", **value}]
    f.receipt_mutation = mutation
    with pytest.raises(HostError):
        f.reader.submit_prepared_child(f.bridge, f.request, media_bindings={"media": "association"})
    assert not f.bridge.admitted_children_snapshot().children
    if mutation != "lineage": assert not any(row[0] == "admit" for row in f.mutations)


@pytest.fixture
def iteration_video_wiring(prepared_submission):
    """Already-admitted frozen CAS, real staging/submission, deterministic reads."""
    from astrid.core.execution.generic_host import _iteration_video_discovery
    f = prepared_submission
    root_id, render_id = "video_editing.iteration_video", "rendering.render"
    f.parent["capability_id"] = f.grant["capability_id"] = root_id
    f.parent["spec"]["child_delegation"]["capabilities"][0]["capability_id"] = render_id
    ports = tuple(SimpleNamespace(name=name, type="file", required=name == "media_dependency", default=None)
                  for name in ("timeline", "assets_registry", "media_dependency", "theme"))
    child = SimpleNamespace(id=render_id, capability_digest=digest(b"child"),
        definition=SimpleNamespace(id=render_id, inputs=ports, metadata={"action_invocation": True}))
    f.bridge.host.capabilities = {render_id: child}
    f.bridge.host.admit = lambda *args: (child, SimpleNamespace())
    f.bridge._sequence = 0
    f.bridge._iteration_video_submit = None
    artifact = {"kind": "video", "object_id": f.row["object_id"], "sha256": f.row["digest"][7:],
                "size": f.row["size"], "media_type": f.row["media_type"],
                "task_id": f.grant["task_id"], "source_association_id": "association"}
    authority = {"kind": "runtime", "project": "P"}
    f.frozen = {"schema_version": 1, "project": "P", "target_run_id": f.grant["run_id"],
        "manifest": {"target_run_id": f.grant["run_id"], "authority": authority,
                     "runs": [{"run_id": f.grant["run_id"], "output_artifacts": [artifact]}]},
        "quality": {"target_run_id": f.grant["run_id"], "authority": authority},
        "media_bindings": [{"name": "video_0", "object_id": f.row["object_id"],
            "sha256": f.row["digest"][7:], "size": f.row["size"], "media_type": f.row["media_type"],
            "filename": "admitted.mp4", "associations": [{"project": "P", "run_id": f.grant["run_id"],
                "artifact_index": 0, "task_id": f.grant["task_id"], "source_association_id": "association"}]}]}
    f.frozen_path = f.bridge.output_root.parent / "inputs" / "frozen.json"
    f.frozen_path.parent.mkdir()
    f.inputs = {"project_id": "P", "target_run_id": f.grant["run_id"], "frozen_inputs": str(f.frozen_path)}
    f.root_record = SimpleNamespace(id=root_id)
    def admit_document():
        body = json.dumps(f.frozen).encode()
        f.frozen_path.write_bytes(body)
        descriptor = {"object_id": digest(body), "digest": digest(body), "size": len(body), "filename": "frozen.json"}
        f.parent["input_object_ids"] = [digest(body)]
        f.parent["spec"]["inputs"] = {"project_id": "P", "target_run_id": f.grant["run_id"], "frozen_inputs": descriptor}
    f.admit_document = admit_document
    admit_document()
    f.request["child"]["capability_id"] = render_id
    f.request["inputs"] = {}
    f.request["input_descriptors"] = []
    for name, filename, data, media in (
            ("timeline", "render-inputs/timeline.json", b'{"tracks":[]}', "application/json"),
            ("assets_registry", "render-inputs/assets.json", b'{"assets":{}}', "application/json"),
            ("media_dependency", "render-inputs/media/admitted.mp4", b"historical-bytes", "video/mp4")):
        path = f.bridge.output_root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        value = {"filename": filename, "output_port": name, "media_type": media}
        f.request["inputs"][name] = value
        f.request["input_descriptors"].append({"name": name, "kind": "producer_file", **value})
    f.install = lambda: _iteration_video_discovery(SimpleNamespace(client=f.client), f.parent,
        f.root_record, f.bridge, f.inputs, attempt_id="parent-attempt", lease_id="parent-lease",
        fence=1, runtime_epoch=1, cancelled=lambda: f.cancelled[0])
    f.calls.clear()
    return f


def test_iteration_video_host_wiring_retains_exact_media_and_normal_submission(iteration_video_wiring):
    f = iteration_video_wiring
    reader = f.install()
    assert Path(f.inputs["media_dependency"]).read_bytes() == b"historical-bytes"
    assert Path(f.inputs["media_dependency"]).is_relative_to(f.bridge.output_root)
    assert json.loads(reader._prepared_objects[0])["source_association_ids"] == ["association"]
    result = f.bridge.dispatch(f.request)
    assert result["task_id"] == "child-task"
    uploads = [row for row in f.mutations if row[0] == "upload"]
    assert [row[1] for row in uploads] == [b'{"tracks":[]}', b'{"assets":{}}', b"historical-bytes"]
    assert all(row[2]["upload_binding"]["attempt_id"] == "parent-attempt" for row in uploads)
    snapshot = f.bridge.admitted_children_snapshot().children[0]
    descriptors = json.loads(snapshot.descriptors_json)
    assert [row["name"] for row in descriptors] == ["timeline", "assets_registry", "media_dependency"]
    assert json.loads(snapshot.inputs_json)["media_dependency"]["filename"].startswith("discovery-objects/")
    provenance = json.loads(snapshot.discovery_provenance_json)["media_dependency"]
    assert provenance["selected_association_id"] == "association"
    assert provenance["source_association_ids"] == ["association"]
    assert f.request["input_descriptors"][2]["filename"] == "render-inputs/media/admitted.mp4"


def test_iteration_video_logical_key_preserved_at_wire_boundary(iteration_video_wiring):
    f = iteration_video_wiring
    f.request["child_key"] = "iteration-render"
    f.install()
    f.bridge.dispatch(f.request)
    wire_id = _child_wire_id(project_id="P", parent_task_id="parent-task",
                             parent_attempt_id="parent-attempt", logical_child_key="iteration-render")
    issue = next(row[2] for row in f.mutations if row[0] == "issue")
    admission = next(row[1] for row in f.mutations if row[0] == "admit")
    snapshot = f.bridge.admitted_children_snapshot().children[0]
    assert issue["child"]["child_id"] == admission["idempotency_key"] == wire_id
    assert json.loads(snapshot.task_json)["idempotency_key"] == wire_id
    assert f.request["child_key"] == snapshot.child_key == "iteration-render"
    assert set(f.bridge._children) == {"iteration-render"}


@pytest.mark.parametrize("attack", ["missing-cas", "unauthorized-cas", "cas-alias", "changed-document", "foreign-project", "foreign-target", "foreign-task", "foreign-association", "wrong-artifact", "binding-size", "two-bindings"])
def test_iteration_video_host_rejects_unadmitted_or_foreign_frozen_identity(iteration_video_wiring, attack):
    f = iteration_video_wiring
    binding = f.frozen["media_bindings"][0]
    if attack == "foreign-project": f.frozen["project"] = "foreign"
    elif attack == "foreign-target": f.frozen["target_run_id"] = "foreign"
    elif attack == "foreign-task": binding["associations"][0]["task_id"] = "foreign"
    elif attack == "foreign-association": binding["associations"][0]["source_association_id"] = "foreign"
    elif attack == "wrong-artifact": binding["associations"][0]["artifact_index"] = 1
    elif attack == "binding-size": binding["size"] += 1
    elif attack == "two-bindings": f.frozen["media_bindings"].append(copy.deepcopy(binding))
    f.admit_document()
    if attack == "missing-cas": f.parent["spec"]["inputs"].pop("frozen_inputs")
    elif attack == "unauthorized-cas": f.parent["input_object_ids"] = []
    elif attack == "cas-alias": f.parent["spec"]["inputs"]["frozen_inputs"]["digest"] = digest(b"other")
    elif attack == "changed-document": f.frozen_path.write_bytes(b"{}")
    with pytest.raises(HostError):
        f.install()
    assert not f.mutations
    assert f.bridge._iteration_video_submit is None
    assert not any(operation == "get_object" for operation, _ in f.calls)


@pytest.mark.parametrize("attack", ["tampered-media", "wrong-mime", "object-binding", "missing-media", "changed-frozen", "source-changed", "cancelled", "lease-lost"])
def test_iteration_video_render_intercept_cannot_bypass_retained_custody(iteration_video_wiring, attack):
    f = iteration_video_wiring
    reader = f.install()
    if attack == "tampered-media": (f.bridge.output_root / "render-inputs/media/admitted.mp4").write_bytes(b"forged")
    elif attack == "wrong-mime": f.request["input_descriptors"][2]["media_type"] = "image/png"
    elif attack == "object-binding": f.request["input_descriptors"][2] = {"name": "media_dependency", "kind": "object", "object_id": f.row["object_id"]}
    elif attack == "missing-media":
        f.request["inputs"].pop("media_dependency")
        f.request["input_descriptors"].pop()
    elif attack == "changed-frozen": f.frozen_path.write_bytes(b"{}")
    elif attack == "source-changed": f.responses[("get_managed_output", "association")]["state"] = "deleted"
    elif attack == "cancelled": f.cancelled[0] = True
    else: f.parent["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
    with pytest.raises(HostError):
        f.bridge._iteration_video_submit(f.request)
    assert not f.mutations
    assert reader.failed


def test_iteration_video_dispatch_intercepts_only_rendering_submit(iteration_video_wiring, monkeypatch):
    f = iteration_video_wiring
    f.install()
    calls = []
    monkeypatch.setattr(f.bridge, "_submit", lambda request: calls.append(request) or {"ordinary": True})
    request = copy.deepcopy(f.request)
    request["child"]["capability_id"] = "other.action"
    assert f.bridge.dispatch(request) == {"ordinary": True}
    assert calls == [request]


@pytest.mark.parametrize("selection", [["foreign"], [], ["association", "association"], "association"])
def test_discovery_selected_association_filter_rejects_unretained_identity(discovery_objects, selection):
    f = discovery_objects
    reader = f.collection()
    reader.collect_metadata()
    with pytest.raises(HostError, match="not retained"):
        reader.prepare_objects(f.bridge, selected_association_ids=selection)
    assert not any(operation == "get_object" for operation, _ in f.calls)


def test_discovery_selected_association_filter_acquires_only_selected_object(discovery_objects):
    f = discovery_objects
    f.second.update(object_id=digest(b"unselected"), digest=digest(b"unselected"), size=len(b"unselected"))
    f.responses[("get_managed_output", "second-association")] = copy.deepcopy(f.second)
    reader = f.collection()
    reader.collect_metadata()
    prepared = reader.prepare_objects(f.bridge, selected_association_ids=["association"])
    assert len(prepared) == 1 and prepared[0]["object_id"] == f.row["object_id"]
    assert prepared[0]["source_association_ids"] == ("association",)
    assert [(operation, identifier) for operation, identifier in f.calls if operation == "get_object"] == [
        ("get_object", f.row["object_id"])]


@pytest.fixture
def world(tmp_path, monkeypatch):
    source = os.environ.get("ASTRID_D18_RUNTIME_SOURCE")
    if not source:
        pytest.skip("requires explicitly selected audited D18 Runtime source")
    source = Path(source)
    monkeypatch.syspath_prepend(str(source))
    monkeypatch.setenv("ASTRID_SOURCE_STATE", str(tmp_path / "absent.json"))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore

    spec = importlib.util.spec_from_file_location("f05_d18_generated_client", source / "packages/python/banodoco_workspace_client/generated.py")
    generated = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = generated
    spec.loader.exec_module(generated)
    root = tmp_path / "realm"
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    identity = {"actor": "worker", "scopes": ["worker:execute"], "execution_binding": {
        "actual": {"kind": "machine", "id": "fixture-machine"}, "executor_incarnation": "worker/fixture",
        "verification": {"method": "credential_claim", "verified": True, "evidence_digest": digest(b"placement")}}}
    lock = threading.RLock()
    calls = []

    def transport(method, path, headers, body):
        with lock:
            service = state.service
            assert headers.get("Authorization") == "Bearer " + SECRET
            payload = json.loads(body) if body and headers.get("Content-Type") == "application/json" else None
            calls.append((method, path, copy.deepcopy(payload)))
            key = headers.get("Idempotency-Key")
            parts = path.split("/")
            mutation = False
            if path == "/v1/health":
                data = service.health()
                data["runtime_instance_id"] = "offline-fixture"
            elif path == "/v1/objects" and method == "POST":
                data = service.ingest_object(body, media_type=headers["Content-Type"], original_name=headers.get("X-Filename"),
                                             idempotency_key=key, identity=identity, upload_binding=json.loads(headers["X-Output-Binding"]))
                mutation = True
            elif parts[2] == "objects":
                return 200, {}, service.cas.path_for(parts[3].removeprefix("sha256:")).read_bytes()
            elif path == "/v1/tasks" and method == "POST":
                data = {"task": service._task_resource(service.create_task(
                    {**payload, "idempotency_key": key}, enforce_readiness=True))}
                mutation = True
            elif parts[2] == "tasks":
                data = service.managed_output_page(parts[3]) if len(parts) == 5 else service._task_resource(service.task(parts[3]))
            elif parts[2] == "managed-outputs":
                data = service.managed_output(parts[3])
            elif path == "/v1/delegated-tasks":
                value = service.admit_delegated_child(payload, idempotency_key=key, identity=identity)
                data = {"task": service._task_resource(value)}
                mutation = True
            elif parts[2] == "attempts":
                action = parts[4]
                if action == "child-authority":
                    data = service.issue_child_authority(parts[3], payload, identity=identity)
                elif action == "heartbeat":
                    data = service.heartbeat_attempt(parts[3], payload, idempotency_key=key, identity=identity)
                    mutation = True
                elif action == "settle":
                    value = service.settle_attempt(parts[3], payload, idempotency_key=key, identity=identity)
                    data = value
                    mutation = True
                elif action == "fail":
                    data = service.fail_attempt(parts[3], payload, idempotency_key=key, identity=identity)
                    mutation = True
                elif action == "recoverable-snapshots":
                    data = service.publish_recoverable_snapshot(parts[3], payload, idempotency_key=key, identity=identity)
                    mutation = True
                else:
                    raise AssertionError(path)
            else:
                raise AssertionError(path)
            if mutation and set(data) != {"data", "receipt"}:
                data = {"data": data, "receipt": {"fixture": "committed"}}
            return 200, {}, json.dumps(data).encode()

    client = RuntimeProtocolClient("http://127.0.0.1:1", SECRET)
    if os.environ.get("ASTRID_D18_USE_VENDORED_CLIENT") == "1":
        from banodoco_workspace_client import WorkspaceClient
        import inspect
        assert type(client.generated) is WorkspaceClient
        assert Path(inspect.getfile(WorkspaceClient)).resolve() == Path(__file__).resolve().parents[3] / "banodoco_workspace_client/generated.py"
        client.generated._transport = transport
        client.generated.timeout = 1
    else:
        client.generated = generated.WorkspaceClient("http://127.0.0.1:1", SECRET, transport=transport, timeout=1)
    client.executor_id = "worker"
    project = service.create_project({"slug": "d18-host", "name": "D18 host fixture"})["id"]
    pack = tmp_path / "bridge"
    pack.mkdir()
    state = SimpleNamespace(service=service, client=client, identity=identity, calls=calls, root=pack,
                            project=project, lock=lock, tmp=tmp_path, bridges=[], transport=transport, realm=root)
    try:
        yield state
    finally:
        for bridge in state.bridges:
            bridge.revoke()
        state.service.close()
        sys.modules.pop(spec.name, None)


def setup(world, *, form="python", derived=False, code=None, flags=(), targeted=False, input_ports=None, argv_override=None, delegating=True, root_object_ids=(), child_file_output=False, network_policy=None, prelude="", module_prelude="", parent_outputs=None, parent_inputs=None, parent_command_fields=None, parent_input_values=None, child_limits=None, child_code_override=None, parent_execution_inputs=None):
    root = world.root
    (root / "actions").mkdir(exist_ok=True)
    inputs = input_ports if input_ports is not None else [{"name": "value", "type": "file" if derived else "string", "required": True}]
    invoke = ("from astrid.core.execution.executor import registry as er\n"
              "from astrid.core.execution.orchestrator import registry as ore\n"
              "from astrid.core.pack import discovery as pd\n"
              "for module in (er, ore, pd): module.discover_packs = lambda *a, **kw: ()\n"
              "from astrid import invoke\n"
              f"r = invoke('{CHILD}', kind='action', extra_pack_roots=({str(root)!r},), inputs={{'value': "
              + ("{'filename': 'generated.txt', 'output_port': 'source', 'media_type': 'text/plain'}" if derived else "'scalar'")
              + "}, child_key='logical-child', wait=True, timeout_seconds=2, poll_seconds=0.01)\n"
                "assert r.ok, r.error\n"
                "assert r.kernel_task_id\n"
                "assert not any(k in __import__('os').environ for k in ('BANODOCO_RUNTIME_CREDENTIAL', 'BANODOCO_RUNTIME_ENDPOINT', 'CHILD_AUTHORITY', 'PARENT_LEASE_ID'))\n")
    prefix = "from pathlib import Path\nPath('outputs').mkdir(exist_ok=True)\n"
    if derived:
        prefix += "Path('outputs/generated.txt').write_text('parent-bytes')\n"
    if child_file_output:
        invoke += "assert r.outputs['managed_outputs'] and r.outputs['artifacts']\n"
    body = prefix + prelude + invoke if code is None else code
    if form == "python":
        (root / "actions/parent.py").write_text(module_prelude + "def run():\n" + "\n".join("    " + line for line in body.splitlines()) + "\n    return {'child': r.kernel_task_id}\n")
        parent_invocation = {"kind": "python", "path": "actions/parent.py", "function": "run"}
    else:
        if form == "-c":
            args = ["-c", "exec(__import__('base64').b64decode(" + repr(base64.b64encode(body.encode()).decode()) + "))"]
        else:
            (root / "command.py").write_text(body)
            args = ["-m", "command"] if form == "-m" else [str(root / "command.py")]
        parent_invocation = {"kind": "command", "command": {"argv": argv_override or ["{python_exec}", *flags, *args]}}
        parent_invocation["command"].update(parent_command_fields or {})
    child_code = "def run(value):\n    import os\n    assert 'BANODOCO_RUNTIME_CREDENTIAL' not in os.environ\n"
    if child_file_output:
        child_code += "    from pathlib import Path\n    Path('outputs').mkdir(exist_ok=True)\n    Path('outputs/answer.txt').write_text('managed-child-bytes')\n"
    child_code += "    return {'answer': " + ("__import__('pathlib').Path(value).read_text()" if derived else "value") + "}\n"
    (root / "actions/child.py").write_text(child_code_override if child_code_override is not None else child_code)
    (root / "pack.yaml").write_text(yaml.safe_dump({"schema_version": 3, "id": "bridge", "name": "Bridge", "version": "1.0.0", "actions": {
        "parent": {"description": "Parent", "invocation": parent_invocation, "inputs": parent_inputs or [], "outputs": parent_outputs if parent_outputs is not None else {},
                   **({"isolation": {"mode": "subprocess", "network": True},
                       "metadata": {"network_policy": network_policy}} if network_policy else {})},
        "child": {"description": "Child", "invocation": {"kind": "python", "path": "actions/child.py", "function": "run"}, "inputs": inputs, "outputs": [{"name": "answer", "type": "file", "mode": "create", "path_template": "{out}/answer.txt", "artifact_type": "text/plain"}] if child_file_output else {}}}}))
    host = GenericPackHost(pack_roots=[root], client=world.client, executor_id="worker", max_concurrency=2)
    host.discover()
    for record in host.capabilities.values():
        world.service.register_capability({"capability_id": record.id, "definition_digest": record.capability_digest})
    world.service.register_executor({"executor_id": "worker", "capabilities": [PARENT, CHILD], "max_concurrency": 2}, idempotency_key="fixture-register")
    policy = {"capabilities": [{"capability_id": CHILD, "capability_digest": host.capabilities[CHILD].capability_digest}],
              "targets": [{"kind": "default"}], "input_object_ids": list(root_object_ids), "limits": {"max_children": 4, **(child_limits or {})}}
    body = {"project": world.project, "capability_id": PARENT, "capability_digest": host.capabilities[PARENT].capability_digest,
                               "input_object_ids": list(root_object_ids), "spec": {"inputs": parent_input_values or {}}, "child_delegation": policy, "idempotency_key": "parent"}
    if not delegating:
        body.pop("child_delegation")
    claim_body = {"executor_id": "worker", "capability_ids": [PARENT], "runtime_epoch": 1}
    if targeted:
        target = world.identity["execution_binding"]["actual"]
        body["execution_request"] = normalize_execution_request({"schema_version": 1, "target": target,
                                                                 "inputs": list(parent_execution_inputs or ())})
        claim_body["target"] = target
    world.service.create_task(body, enforce_readiness=True)
    claim = world.service.claim_next(claim_body, idempotency_key="claim-parent", identity=world.identity)
    world.client._attempt_runtime_epochs[claim["attempt_id"]] = claim["runtime_epoch"]
    task = world.service.task(claim["task_id"])["task"]
    task.update(runtime_epoch=claim["runtime_epoch"], fence=claim["fence"], project_id=world.project,
                spec=claim["spec"], input_object_ids=claim["input_object_ids"], execution_binding=claim.get("execution_binding"),
                lease_id=claim["lease_id"], executor_id="worker")
    world.host, world.task, world.claim = host, task, claim
    return host


def bridge(world):
    output = world.tmp / "attempt" / "outputs"
    output.mkdir(parents=True, exist_ok=True)
    c = world.claim
    result = HostChildBridge(world.host, world.task, attempt_id=c["attempt_id"], lease_id=c["lease_id"], fence=c["fence"],
                             runtime_epoch=c["runtime_epoch"], output_root=output, cancelled=lambda: False)
    result._thread.start()  # direct wire fixture, no process launch
    world.bridges.append(result)
    return result


def request(world, *, key="child", value="scalar", descriptors=None, request_id=1):
    return {"v": 1, "request_id": request_id, "op": "submit", "child": {"capability_id": CHILD,
            "capability_digest": world.host.capabilities[CHILD].capability_digest}, "inputs": {"value": value},
            "input_descriptors": descriptors or [], "child_key": key, "wait": True, "timeout_seconds": 0.5, "poll_seconds": 0.01}


def serve_one(world):
    """Independent host instance serves the same target/registered executor."""
    host = GenericPackHost(pack_roots=[world.root], client=world.client, executor_id="worker", max_concurrency=2)
    host.discover()
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        with world.lock:
            body = {"executor_id": "worker", "capability_ids": [CHILD], "runtime_epoch": 1}
            row = world.service.store.conn.execute("SELECT id FROM tasks WHERE capability=? AND status='queued' LIMIT 1", (CHILD,)).fetchone()
            if row is not None:
                target = world.service.store.effective_execution_target(row["id"])
                if target is not None:
                    body["target"] = target
            c = world.service.claim_next(body, idempotency_key="claim-child-" + str(time.monotonic_ns()), identity=world.identity)
        if c:
            world.client._attempt_runtime_epochs[c["attempt_id"]] = c["runtime_epoch"]
            task = world.service.task(c["task_id"])["task"]
            task.update(runtime_epoch=c["runtime_epoch"], fence=c["fence"], project_id=world.project,
                        spec=c["spec"], input_object_ids=c["input_object_ids"], execution_binding=c.get("execution_binding"),
                        lease_id=c["lease_id"], executor_id="worker")
            return host.run_task({"task": task}, lease_token=c["lease_id"], attempt_id=c["attempt_id"], fence=c["fence"])
        time.sleep(0.01)
    raise AssertionError("child was not admitted")


@pytest.mark.parametrize("form", ["python", "-m", "script", "-c"])
@pytest.mark.parametrize("derived", [False, True])
def test_real_sdk_parent_routes_and_independent_child_progress(world, form, derived):
    host = setup(world, form=form, derived=derived, targeted=True)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(serve_one, world)
        c = world.claim
        try:
            result = host.run_task({"task": world.task}, lease_token=c["lease_id"], attempt_id=c["attempt_id"], fence=c["fence"])
        except Exception:
            future.result(timeout=5)
            raise
        assert future.result(timeout=5)
    assert world.service.task(c["task_id"])["task"]["status"] == "completed"
    delegated = [call[2] for call in world.calls if call[1] == "/v1/delegated-tasks"]
    assert len(delegated) == 1
    authority = [call[2] for call in world.calls if call[1].endswith("child-authority")][0]
    assert authority["child"]["child_id"] == _child_wire_id(project_id=world.project,
        parent_task_id=c["task_id"], parent_attempt_id=c["attempt_id"], logical_child_key="logical-child")
    assert {k: authority[k] for k in ("lease_id", "fence", "runtime_epoch")} == {k: c[k] for k in ("lease_id", "fence", "runtime_epoch")}
    assert authority["derived_inputs"] == ([] if not derived else [{"name": "value", "filename": "generated.txt", "output_port": "source",
            "media_type": "text/plain", "object_id": digest(b"parent-bytes"), "size": len(b"parent-bytes")}])
    assert SECRET not in json.dumps(result)
    assert delegated[0]["task"].get("child_delegation") is None
    assert not any(call[1] == "/v1/tasks" and call[0] == "POST" for call in world.calls)


def test_exact_replay_and_changed_input_rejection(world):
    setup(world)
    b = bridge(world)
    sdk = ChildBridge(b.child_channel)
    first = sdk._exchange("submit", {k: v for k, v in request(world).items() if k not in {"v", "request_id", "op"}})
    second = sdk._exchange("submit", {k: v for k, v in request(world).items() if k not in {"v", "request_id", "op"}})
    assert first == second
    changed = request(world, value="different")
    with pytest.raises(_BridgeRejected):
        sdk._exchange("submit", {k: v for k, v in changed.items() if k not in {"v", "request_id", "op"}})
    assert len(world.service.store.delegated_children(world.task["id"], world.claim["attempt_id"])) == 1
    with pytest.raises(_BridgeRejected):
        sdk.status("child", "foreign-task")
    sdk.close()


@pytest.mark.parametrize("tamper", ["digest", "policy", "parent", "token", "wait", "descriptor", "key"])
def test_malformed_or_widening_frames_never_admit(world, tamper):
    setup(world)
    b = bridge(world)
    r = request(world)
    if tamper == "digest": r["child"]["capability_digest"] = digest(b"foreign")
    elif tamper == "policy": r["child_delegation"] = {"limits": {"max_children": 999}}
    elif tamper == "parent": r["parent_attempt_id"] = "foreign"
    elif tamper == "token": r["authority"] = "forged"
    elif tamper == "wait": r["timeout_seconds"] = float("nan")
    elif tamper == "descriptor": r["input_descriptors"] = [{"name": "value", "kind": "object", "object_id": digest(b"foreign")}]
    else: r["child_key"] = "../foreign"
    with pytest.raises(Exception): b.dispatch(r)
    assert not world.service.store.delegated_children(world.task["id"], world.claim["attempt_id"])


@pytest.mark.parametrize("filename", ["../foreign", "/tmp/foreign", "link", "sub/link", "hard", "fifo"])
def test_attempt_file_confinement(world, filename):
    setup(world, derived=True)
    b = bridge(world)
    outside = world.tmp / "outside"
    outside.write_bytes(b"outside")
    (b.output_root / "link").symlink_to(outside)
    (b.output_root / "sub").symlink_to(world.tmp, target_is_directory=True)
    os.link(outside, b.output_root / "hard")
    os.mkfifo(b.output_root / "fifo")
    binding = {"name": "value", "kind": "producer_file", "filename": filename, "output_port": "source", "media_type": "text/plain"}
    with pytest.raises(Exception): b.dispatch(request(world, value={k: binding[k] for k in ("filename", "output_port", "media_type")}, descriptors=[binding]))
    assert not any(call[1] == "/v1/objects" for call in world.calls)


def test_changed_producer_bytes_on_key_replay_are_rejected(world):
    setup(world, derived=True)
    b = bridge(world)
    (b.output_root / "generated.txt").write_bytes(b"before")
    binding = {"name": "value", "kind": "producer_file", "filename": "generated.txt", "output_port": "source", "media_type": "text/plain"}
    r = request(world, value={k: binding[k] for k in ("filename", "output_port", "media_type")}, descriptors=[binding])
    first = b.dispatch(r)
    (b.output_root / "generated.txt").write_bytes(b"after")
    r["request_id"] = 2
    with pytest.raises(ChildBridgeError, match="different inputs or bytes"): b.dispatch(r)
    assert first["task_id"]


@pytest.mark.parametrize("end", ["cancel", "epoch", "attempt"])
def test_parent_authority_loss_revokes_channel(world, end):
    setup(world)
    b = bridge(world)
    child = b.dispatch(request(world))
    if end == "cancel":
        world.service.cancel_task_canonical(world.task["id"], {}, idempotency_key="cancel")
    elif end == "epoch":
        world.service.store.begin_runtime_session("new-runtime")
    else:
        world.service.store.conn.execute("UPDATE tasks SET attempt_id='foreign' WHERE id=?", (world.task["id"],))
    with pytest.raises(Exception): b.dispatch({"v": 1, "request_id": 2, "op": "status", "child_key": "child", "task_id": child["task_id"]})
    assert b._revoked.is_set()


def test_parent_success_waits_and_no_capacity_is_finite(world):
    setup(world)
    b = bridge(world)
    b.dispatch(request(world))
    start = time.monotonic()
    with pytest.raises(ChildBridgeError, match="bounded wait"): b.finish()
    assert time.monotonic() - start < 1
    assert world.service.task(world.task["id"])["task"]["status"] == "running"


@pytest.mark.parametrize("corruption", ["bytes", "lifecycle", "association", "settlement"])
def test_child_outputs_must_verify_before_parent_success(world, corruption):
    setup(world)
    b = bridge(world)
    child = b.dispatch(request(world))
    c = world.service.claim_next({"executor_id": "worker", "capability_ids": [CHILD], "runtime_epoch": 1}, idempotency_key="claim-output", identity=world.identity)
    data = b"verified-output"
    world.service.settle_attempt(c["attempt_id"], {**{k: c[k] for k in ("lease_id", "fence", "runtime_epoch")},
            "outputs": [{"name": "answer", "digest": digest(data), "data_base64": base64.b64encode(data).decode()}]}, idempotency_key="settle-output", identity=world.identity)
    output = world.service.managed_outputs(child["task_id"])[0]
    if corruption == "bytes": world.service.cas.path_for(digest(data)[7:]).write_bytes(b"corrupt")
    elif corruption == "lifecycle": world.service.store.conn.execute("UPDATE managed_output_lifecycle SET state='reclaimed' WHERE association_id=?", (output["association_id"],))
    elif corruption == "association": world.service.store.conn.execute("UPDATE managed_output_associations SET task_id=? WHERE association_id=?", (world.task["id"], output["association_id"]))
    else:
        settled = world.service.task(child["task_id"])["task"]["result"]
        settled["outputs"][0]["digest"] = digest(b"different-settlement")
        world.service.store.conn.execute("UPDATE tasks SET result_json=? WHERE id=?", (json.dumps(settled), child["task_id"]))
    with pytest.raises(Exception): b.finish()
    assert world.service.task(world.task["id"])["task"]["status"] == "running"


@pytest.mark.parametrize("flags", [("-u",), ("-B",), ("-u", "-B")])
def test_retained_interpreter_flags(world, flags):
    code = "import sys\n"
    if "-B" in flags:
        (world.root / "flag_probe.py").write_text("value = 42\n")
        code += "assert sys.dont_write_bytecode\nimport flag_probe\nassert flag_probe.value == 42\n"
        code += f"assert not __import__('pathlib').Path({str(world.root / '__pycache__')!r}).exists()\n"
    if "-u" in flags: code += "assert sys.stdout.write_through\n"
    code += "import __main__\nmain_value = 42\nassert __main__.main_value == 42\n"
    setup(world, form="-c", code=code + "pass\n", flags=flags)
    c = world.claim
    assert world.host.run_task({"task": world.task}, lease_token=c["lease_id"], attempt_id=c["attempt_id"], fence=c["fence"])


@pytest.mark.parametrize("flag", ["-I", "-S", "--unknown"])
def test_unsupported_flags_fail_before_command_work(world, flag):
    marker = world.tmp / "work-ran"
    setup(world, form="-c", code=f"__import__('pathlib').Path({str(marker)!r}).touch()", flags=(flag,))
    c = world.claim
    with pytest.raises(HostError, match="unsupported delegating Python command flags"):
        world.host.run_task({"task": world.task}, lease_token=c["lease_id"], attempt_id=c["attempt_id"], fence=c["fence"])
    assert not marker.exists()


def test_successful_managed_outputs_have_verified_public_projection(world):
    setup(world)
    b = bridge(world)
    child = b.dispatch(request(world))
    c = world.service.claim_next({"executor_id": "worker", "capability_ids": [CHILD], "runtime_epoch": 1}, idempotency_key="claim-output", identity=world.identity)
    data = b"verified-output"
    world.service.settle_attempt(c["attempt_id"], {**{k: c[k] for k in ("lease_id", "fence", "runtime_epoch")},
            "outputs": [{"name": "answer", "digest": digest(data), "data_base64": base64.b64encode(data).decode()}]}, idempotency_key="settle-output", identity=world.identity)
    rows = b.dispatch({"v": 1, "request_id": 2, "op": "outputs", "child_key": "child", "task_id": child["task_id"]})
    assert rows[0]["object_id"] == digest(data)
    assert not {"lease_id", "provenance", "producer", "authority", "runtime_epoch"} & rows[0].keys()
    b.finish()


def test_wait_poll_does_not_block_second_admission_or_heartbeat(world):
    setup(world)
    b = bridge(world)
    sdk = ChildBridge(b.child_channel)
    body = {k: v for k, v in request(world).items() if k not in {"v", "request_id", "op"}}
    first = sdk._exchange("submit", body)
    stopped = threading.Event()
    errors = []
    def poll():
        try:
            while not stopped.is_set():
                sdk.status("child", first["task_id"])
                time.sleep(0.01)
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=poll)
    worker.start()
    try:
        start = time.monotonic()
        second = sdk._exchange("submit", {**body, "child_key": "second"})
        c = world.claim
        world.client.heartbeat(world.task["id"], c["lease_id"], attempt_id=c["attempt_id"], fence=c["fence"])
        assert time.monotonic() - start < 0.5
        assert second["task_id"] != first["task_id"]
    finally:
        stopped.set()
        worker.join(timeout=1)
        sdk.close()
    assert not errors and not worker.is_alive()


def test_heartbeat_cannot_change_deadline_between_authority_and_admission(world, monkeypatch):
    setup(world)
    b = bridge(world)
    issued = threading.Event()
    continue_admission = threading.Event()
    original = world.client.generated.issue_child_authority
    def issue(*args, **kwargs):
        receipt = original(*args, **kwargs)
        issued.set()
        assert continue_admission.wait(2)
        return receipt
    monkeypatch.setattr(world.client.generated, "issue_child_authority", issue)
    from concurrent.futures import ThreadPoolExecutor
    c = world.claim
    with ThreadPoolExecutor(max_workers=2) as pool:
        admission = pool.submit(b.dispatch, request(world))
        assert issued.wait(2)
        heartbeat = pool.submit(world.client.heartbeat, world.task["id"], c["lease_id"], attempt_id=c["attempt_id"], fence=c["fence"])
        assert not heartbeat.done()
        continue_admission.set()
        assert admission.result(timeout=2)["task_id"]
        heartbeat.result(timeout=2)
    # A later child obtains a fresh receipt after the deadline changes.
    assert b.dispatch(request(world, key="fresh", request_id=2))["task_id"]


@pytest.mark.parametrize("field", ["lease_id", "fence", "runtime_epoch"])
def test_stale_host_claim_identity_cannot_issue_authority(world, field):
    setup(world)
    b = bridge(world)
    b.context[field] = "foreign" if field == "lease_id" else b.context[field] + 1
    with pytest.raises(Exception): b.dispatch(request(world))
    assert not world.service.store.delegated_children(world.task["id"], world.claim["attempt_id"])


@pytest.mark.parametrize("frame", [b'{}\n', b'{"v":1,"v":1}\n', b'[]\n', b'{"x":NaN}\n', b'x' * 1048577])
def test_malformed_channel_is_revoked_without_fallback(world, frame):
    setup(world)
    b = bridge(world)
    local = b.child_channel
    local.settimeout(1)
    try:
        local.sendall(frame)
        reply = local.recv(4096)
        if reply:
            assert json.loads(reply)["ok"] is False
    except (OSError, socket.timeout):
        pass
    assert not world.service.store.delegated_children(world.task["id"], world.claim["attempt_id"])


def test_runtime_error_text_and_authority_do_not_cross_channel(world, monkeypatch):
    setup(world)
    b = bridge(world)
    monkeypatch.setattr(world.client, "admit_child", lambda **kwargs: (_ for _ in ()).throw(RuntimeError(SECRET + " signed-authority-token")))
    sdk = ChildBridge(b.child_channel)
    with pytest.raises(_BridgeRejected) as error:
        sdk._exchange("submit", {k: v for k, v in request(world).items() if k not in {"v", "request_id", "op"}})
    assert SECRET not in str(error.value.error) and "signed-authority-token" not in str(error.value.error)
    sdk.close()


def test_default_vendor_gap_is_explicit(world):
    # This proof deliberately documents the prerequisite, without replacing
    # default import resolution or claiming the injected fixture is production.
    import inspect
    from banodoco_workspace_client import WorkspaceClient
    if "derived_inputs" not in inspect.signature(WorkspaceClient.issue_child_authority).parameters:
        pytest.xfail("B01 vendor sync prerequisite remains open")
    assert "derived_inputs" in inspect.signature(type(world.client.generated).issue_child_authority).parameters


def test_changed_bytes_during_snapshot_fail(tmp_path, monkeypatch):
    root = tmp_path / "outputs"
    root.mkdir()
    source = root / "input.txt"
    source.write_bytes(b"before")
    original = os.read
    changed = False
    def read(fd, count):
        nonlocal changed
        data = original(fd, count)
        if data and not changed:
            changed = True
            source.write_bytes(b"after!")
        return data
    monkeypatch.setattr(os, "read", read)
    with pytest.raises(ChildBridgeError, match="changed during snapshot"):
        _snapshot(root, "input.txt", 1024)


def test_runtime_capacity_exhaustion_has_explicit_finite_result(world):
    setup(world)
    world.service.register_executor({"executor_id": "worker", "capabilities": [PARENT, CHILD], "max_concurrency": 1, "runtime_epoch": 1}, idempotency_key="one-slot")
    b = bridge(world)
    child = b.dispatch(request(world))
    claim = world.service.claim_next({"executor_id": "worker", "capability_ids": [CHILD], "runtime_epoch": 1}, idempotency_key="no-capacity", identity=world.identity)
    assert claim["waiting_reason"] == "waiting_for_worker" and "attempt_id" not in claim
    status = b.dispatch({"v": 1, "request_id": 2, "op": "status", "child_key": "child", "task_id": child["task_id"]})
    assert status["state"] == "queued"
    start = time.monotonic()
    with pytest.raises(ChildBridgeError, match="no serving capacity observed"):
        b.finish()
    assert time.monotonic() - start < 1


def test_real_sdk_timeout_fails_parent_and_runtime_contains_child(world):
    setup(world, form="python")
    start = time.monotonic()
    c = world.claim
    with pytest.raises(HostError, match="task_wait_timeout"):
        world.host.run_task({"task": world.task}, lease_token=c["lease_id"], attempt_id=c["attempt_id"], fence=c["fence"])
    assert time.monotonic() - start < 5
    children = world.service.store.delegated_children(world.task["id"], c["attempt_id"])
    assert len(children) == 1
    assert world.service.task(children[0]["id"])["task"]["status"] == "cancelled"
    assert world.service.task(world.task["id"])["task"]["status"] == "failed"


def test_two_derived_inputs_keep_declared_order_and_runtime_receipts(world):
    setup(world, input_ports=[{"name": name, "type": "file", "required": True} for name in ("zeta", "alpha")])
    b = bridge(world)
    bindings = []
    values = {}
    for name in ("zeta", "alpha"):
        (b.output_root / (name + ".txt")).write_bytes(name.encode())
        binding = {"name": name, "kind": "producer_file", "filename": name + ".txt", "output_port": name, "media_type": "text/plain"}
        bindings.append(binding)
        values[name] = {k: binding[k] for k in ("filename", "output_port", "media_type")}
    r = request(world, descriptors=bindings)
    r["inputs"] = {name: values[name] for name in ("alpha", "zeta")}
    child = b.dispatch(r)
    admission = [call[2] for call in world.calls if call[1] == "/v1/delegated-tasks"][0]
    descriptors = admission["task"]["execution_request"]["inputs"]
    assert [ref["name"] for ref in descriptors] == ["zeta", "alpha"]
    assert [ref["object_id"] for ref in descriptors] == [digest(b"zeta"), digest(b"alpha")]
    refs = world.service.task(child["task_id"])["task"]["spec"]["delegated_inputs"]
    assert [ref["name"] for ref in refs] == ["zeta", "alpha"]
    assert all(ref["association_id"] and ref["parent_attempt_id"] == world.claim["attempt_id"] for ref in refs)


def test_actual_runtime_idempotent_replay_uses_attempt_scoped_child_wire_id(world):
    setup(world)
    b = bridge(world)
    first = b.dispatch(request(world))
    wire_id = _child_wire_id(project_id=world.project, parent_task_id=world.claim["task_id"],
                             parent_attempt_id=world.claim["attempt_id"], logical_child_key="child")
    args = {"child": {"child_id": wire_id, "capability_id": CHILD, "capability_digest": world.host.capabilities[CHILD].capability_digest},
            "inputs": {"value": "scalar"}, "ordered_inputs": [], "derived_inputs": [], **b.context}
    replay = world.client.admit_child(**args)
    assert replay["task"]["task_id"] == first["task_id"]
    assert replay["task"]["idempotency_key"] == wire_id
    assert len(world.service.store.delegated_children(world.task["id"], world.claim["attempt_id"])) == 1


def test_child_wire_idempotency_canonical_version_and_scope():
    values = {"project_id": "project", "parent_task_id": "parent", "parent_attempt_id": "attempt",
              "logical_child_key": "iteration-render"}
    canonical = json.dumps({"version": 1, **values}, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False).encode("utf-8")
    expected = "child-v1-" + hashlib.sha256(canonical).hexdigest()
    assert _child_wire_id(**values) == expected
    assert _child_wire_id(**dict(reversed(list(values.items())))) == expected
    assert len(expected) == 73
    for field in values:
        assert _child_wire_id(**{**values, field: values[field] + "-other"}) != expected
    assert _child_wire_id(**{**values, "project_id": None}) != expected


def test_child_wire_idempotency_same_attempt_replay_and_changed_inputs(world, monkeypatch):
    setup(world)
    b = bridge(world)
    issued, admitted = [], []
    original_issue = world.client.generated.issue_child_authority
    original_admit = world.client.generated.admit_delegated_task

    def issue(*args, **kwargs):
        issued.append(kwargs["child"]["child_id"])
        return original_issue(*args, **kwargs)

    def admit(**kwargs):
        admitted.append(kwargs["idempotency_key"])
        return original_admit(**kwargs)

    monkeypatch.setattr(world.client.generated, "issue_child_authority", issue)
    monkeypatch.setattr(world.client.generated, "admit_delegated_task", admit)
    first = b.dispatch(request(world, key="iteration-render"))
    assert b.dispatch(request(world, key="iteration-render", request_id=2)) == first
    reopened = bridge(world)
    assert reopened.dispatch(request(world, key="iteration-render")) == first
    wire_id = _child_wire_id(project_id=world.project, parent_task_id=world.claim["task_id"],
                             parent_attempt_id=world.claim["attempt_id"], logical_child_key="iteration-render")
    assert issued == admitted == [wire_id, wire_id]
    for current in (b, reopened):
        snapshot = current.admitted_children_snapshot().children[0]
        assert snapshot.child_key == "iteration-render"
        assert json.loads(snapshot.task_json)["idempotency_key"] == wire_id
        assert set(current._children) == {"iteration-render"}
    with pytest.raises(ChildBridgeError, match="different inputs or bytes"):
        b.dispatch(request(world, key="iteration-render", value="changed", request_id=3))
    assert issued == admitted == [wire_id, wire_id]
    # A fresh bridge has no local replay cache: Runtime must still reject a
    # changed input under the same signed attempt/key identity.
    with pytest.raises(Exception, match="idempotency key was already used with different input"):
        bridge(world).dispatch(request(world, key="iteration-render", value="changed"))


@pytest.mark.parametrize("parent_mode", ["fresh-parent", "same-parent-retry"])
def test_child_wire_idempotency_distinct_parent_attempts_reuse_logical_key(world, parent_mode):
    from astrid.core.execution._child_bridge import task_resource
    setup(world)
    first_bridge = bridge(world)
    first = first_bridge.dispatch(request(world, key="iteration-render"))
    previous = dict(world.claim)
    if parent_mode == "same-parent-retry":
        first_bridge.revoke()
        world.service.cancel_task_canonical(previous["task_id"], {}, idempotency_key="wire-cancel-parent")
        world.service.retry_task(previous["task_id"], {}, idempotency_key="wire-retry-parent")
    else:
        world.service.create_task({"project": world.project, "capability_id": PARENT,
            "capability_digest": world.host.capabilities[PARENT].capability_digest,
            "input_object_ids": [], "spec": {"inputs": {}},
            "child_delegation": world.task["spec"]["child_delegation"],
            "idempotency_key": "wire-fresh-parent"}, enforce_readiness=True)
    claim = world.service.claim_next({"executor_id": "worker", "capability_ids": [PARENT],
        "runtime_epoch": 1}, idempotency_key="wire-next-parent", identity=world.identity)
    assert claim["attempt_id"] != previous["attempt_id"]
    assert (claim["task_id"] == previous["task_id"]) == (parent_mode == "same-parent-retry")
    world.client._attempt_runtime_epochs[claim["attempt_id"]] = claim["runtime_epoch"]
    world.claim, world.task = claim, task_resource(world.client.task(claim["task_id"]))
    second_bridge = bridge(world)
    second = second_bridge.dispatch(request(world, key="iteration-render", value="second-attempt"))
    assert second["task_id"] != first["task_id"]
    wire_ids = [json.loads(b.admitted_children_snapshot().children[0].task_json)["idempotency_key"]
                for b in (first_bridge, second_bridge)]
    assert wire_ids[0] != wire_ids[1]
    assert wire_ids == [_child_wire_id(project_id=world.project, parent_task_id=c["task_id"],
        parent_attempt_id=c["attempt_id"], logical_child_key="iteration-render") for c in (previous, claim)]
    assert [call[2]["child"]["child_id"] for call in world.calls
            if call[1].endswith("child-authority")] == wire_ids
    assert all(b.admitted_children_snapshot().children[0].child_key == "iteration-render"
               for b in (first_bridge, second_bridge))


@pytest.mark.parametrize("returned_key", ["iteration-render", "child-v1-" + "0" * 64])
def test_child_wire_idempotency_rejects_foreign_returned_task(world, monkeypatch, returned_key):
    setup(world)
    b = bridge(world)
    original = world.client.admit_child

    def admit(**kwargs):
        result = original(**kwargs)
        result["task"]["idempotency_key"] = returned_key
        return result

    monkeypatch.setattr(world.client, "admit_child", admit)
    with pytest.raises(ChildBridgeError, match="identity disagrees"):
        b.dispatch(request(world, key="iteration-render"))
    assert not b.admitted_children_snapshot().children
    assert b._children["iteration-render"]["task"] is None


def test_foreign_runtime_receipt_is_rejected_before_admission(world, monkeypatch):
    setup(world, derived=True)
    b = bridge(world)
    (b.output_root / "source.txt").write_bytes(b"source")
    binding = {"name": "value", "kind": "producer_file", "filename": "source.txt", "output_port": "source", "media_type": "text/plain"}
    original = world.client.generated.issue_child_authority
    def issue(*args, **kwargs):
        receipt = original(*args, **kwargs)
        receipt["derived_inputs"][0]["object_id"] = digest(b"foreign")
        return receipt
    monkeypatch.setattr(world.client.generated, "issue_child_authority", issue)
    with pytest.raises(ChildBridgeError, match="receipt identity or order"):
        b.dispatch(request(world, value={k: binding[k] for k in ("filename", "output_port", "media_type")}, descriptors=[binding]))
    assert not any(call[1] == "/v1/delegated-tasks" for call in world.calls)


def test_lease_loss_revokes_channel_and_runtime_contains_descendants(world):
    setup(world)
    b = bridge(world)
    child = b.dispatch(request(world))
    from datetime import datetime, timedelta, timezone
    expiry = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    c = world.claim
    world.service.store.conn.execute("UPDATE tasks SET lease_expires_at=? WHERE id=?", (expiry, world.task["id"]))
    world.service.store.conn.execute("UPDATE attempts SET lease_expires_at=? WHERE id=?", (expiry, c["attempt_id"]))
    with world.service.store._transaction():
        world.service.store._reap_expired_leases()
    with pytest.raises(ChildBridgeError):
        b.dispatch({"v": 1, "request_id": 2, "op": "status", "child_key": "child", "task_id": child["task_id"]})
    assert b._revoked.is_set()
    assert world.service.task(child["task_id"])["task"]["status"] == "cancelled"


def test_source_change_rejected_before_child_admission(world):
    setup(world)
    b = bridge(world)
    (world.root / "actions/child.py").write_text("def run(value):\n    return 'changed'\n")
    with pytest.raises(HostError, match="source digest changed"):
        b.dispatch(request(world))
    assert not world.service.store.delegated_children(world.task["id"], world.claim["attempt_id"])


def test_non_python_delegating_command_rejected_before_work(world):
    marker = world.tmp / "command-ran"
    setup(world, form="-c", argv_override=["/bin/sh", "-c", f"printf done > {str(marker)!r}"])
    c = world.claim
    with pytest.raises(HostError, match="require the admitted Python interpreter"):
        world.host.run_task({"task": world.task}, lease_token=c["lease_id"], attempt_id=c["attempt_id"], fence=c["fence"])
    assert not marker.exists()
    assert not world.service.store.delegated_children(world.task["id"], c["attempt_id"])


def test_non_python_ordinary_command_preserves_existing_route(world):
    marker = world.tmp / "command-ran"
    setup(world, form="-c", argv_override=["/bin/sh", "-c", f"printf done > {str(marker)!r}"], delegating=False)
    c = world.claim
    world.host.run_task({"task": world.task}, lease_token=c["lease_id"], attempt_id=c["attempt_id"], fence=c["fence"])
    assert marker.read_text() == "done"
    assert world.service.task(world.task["id"])["task"]["status"] == "completed"


@pytest.mark.parametrize("admitted", [True, False])
def test_existing_object_requires_admitted_parent_custody(world, admitted):
    data = b"existing-parent-object"
    oid = digest(data)
    if admitted:
        world.service.ingest(world.project, data, media_type="text/plain", original_name="existing.txt", idempotency_key="root-object")
    else:
        world.service.ingest_object(data, media_type="text/plain", original_name="existing.txt", idempotency_key="root-object")
    setup(world, derived=True, root_object_ids=[oid] if admitted else [])
    b = bridge(world)
    r = request(world, value={"object_id": oid, "digest": oid, "filename": "existing.txt"}, descriptors=[{"name": "value", "kind": "object", "object_id": oid}])
    if admitted:
        child = b.dispatch(r)
        assert child["task_id"]
        task = world.service.task(child["task_id"])["task"]
        assert task["spec"]["input_object_ids"] == [oid]
        assert not any(call[0] == "POST" and call[1] == "/v1/objects" for call in world.calls)
    else:
        with pytest.raises(ChildBridgeError, match="outside admitted parent custody"):
            b.dispatch(r)


@pytest.mark.parametrize("form", ["python", "-m"])
def test_real_sdk_verified_artifacts_and_managed_outputs(world, form):
    setup(world, form=form, child_file_output=True, targeted=True)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(serve_one, world)
        c = world.claim
        try:
            world.host.run_task({"task": world.task}, lease_token=c["lease_id"], attempt_id=c["attempt_id"], fence=c["fence"])
        except Exception:
            future.result(timeout=5)
            raise
        future.result(timeout=5)
    assert world.service.task(world.task["id"])["task"]["status"] == "completed"


def test_existing_network_sandbox_preserves_inherited_local_channel(tmp_path):
    """No TCP connection: only the already-connected private socket pair."""
    import subprocess
    from astrid.core.execution.generic_host import _network_sandbox_argv
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists():
        pytest.skip("existing host OS network sandbox requires macOS sandbox-exec")
    host, child = socket.socketpair()
    host.settimeout(5)
    try:
        code = "import socket,sys; s=socket.socket(fileno=int(sys.argv[1])); s.sendall(b'local'); assert s.recv(5)==b'reply'"
        process = subprocess.Popen(_network_sandbox_argv([sys.executable, "-c", code, str(child.fileno())], tmp_path, "http://127.0.0.1:1"),
                                   cwd=tmp_path, pass_fds=(child.fileno(),), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        child.close()
        try:
            assert host.recv(5) == b"local"
            host.sendall(b"reply")
            _, stderr = process.communicate(timeout=5)
            assert process.returncode == 0, stderr.decode()
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    finally:
        host.close()
        child.close()


def _context_receipt(name, value):
    """Optional bounded coordinator artifacts; never write under the worktree."""
    destination = os.environ.get("ASTRID_D18_BROKER_CONTEXT_EVIDENCE")
    if destination:
        root = Path(destination)
        assert root.is_absolute()
        assert not root.is_relative_to(Path(__file__).resolve().parents[3])
        (root / (name + ".json")).write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


@pytest.mark.parametrize("form,flags", [("python", ()), ("-m", ("-u",)), ("script", ("-B",)), ("-c", ("-u", "-B"))])
def test_d18_bounded_network_context_and_contained_child(world, monkeypatch, form, flags):
    """Real action, live loopback handshake/route, independent Runtime child."""
    import hmac
    from astrid.core.execution.network_broker import ObservableNetworkBroker
    from concurrent.futures import ThreadPoolExecutor

    attempt = world.tmp / "broker-attempt"
    upstream = "http://127.0.0.1:9/declared"
    prelude = f'''import os, json, socket, time
os.environ["ASTRID_SOURCE_STATE"] = {str(world.tmp / "absent.json")!r}
from urllib.parse import urlsplit
from astrid.core.execution import network_policy as np
assert 'ASTRID_NETWORK_ADMISSION' not in os.environ
assert 'ASTRID_NETWORK_BROKER_EVIDENCE' not in os.environ
assert 'BANODOCO_RUNTIME_ENDPOINT' not in os.environ
assert 'BANODOCO_RUNTIME_CREDENTIAL' not in os.environ
assert len(os.environ['ASTRID_NETWORK_ADMISSION_DIGEST']) == 64
assert os.environ['ASTRID_NETWORK_NONCE'] and os.environ['ASTRID_NETWORK_BROKER_TOKEN']
assert 'evidence_path' not in json.loads(os.environ['ASTRID_NETWORK_POLICY'])['broker']
spool = Path({str(attempt)!r})
assert not (spool / 'broker-evidence.json').exists()
proxy = urlsplit(os.environ['ASTRID_BROKER_PROXY'])
def route(url):
    with socket.create_connection((proxy.hostname, proxy.port), timeout=2) as conn:
        conn.sendall(('GET ' + url + ' HTTP/1.1\\r\\nHost: fixture\\r\\n\\r\\n').encode())
        return conn.recv(4096)
assert b'200 OK' in route({upstream!r})
assert b'403 Forbidden' in route('http://127.0.0.1:10/undeclared')
np._write_evidence()
diag = json.loads((spool / 'network-evidence.json').read_text())
assert set(diag['admission']) == {{'admission_digest', 'network_nonce'}}
assert 'broker_evidence' not in diag
assert not (spool / 'broker-evidence.json').exists()
readable_json = {{}}
for path in spool.rglob('*.json'):
    if path.is_file():
        content = json.loads(path.read_text())
        if isinstance(content, dict):
            candidate = content.get('admission', {{}})
            assert not {{'task_id', 'attempt_id', 'fence'}}.intersection(candidate)
            assert 'broker_evidence' not in content
        readable_json[str(path.relative_to(spool))] = content
observations = {{'readable_json': readable_json, 'environment': {{k: v for k, v in os.environ.items() if k.startswith('ASTRID_NETWORK_') and k != 'ASTRID_NETWORK_BROKER_TOKEN'}}, 'diagnostic': diag, 'readable_paths': [str(p.relative_to(spool)) for p in spool.rglob('*') if p.is_file()]}}
Path('outputs/action-context.json').write_text(json.dumps(observations))
pid = os.fork()
if pid == 0:
    os.close(1)
    os.close(2)
    Path('outputs/descendant-ready').write_text(str(os.getpid()))
    while True:
        if (spool / 'broker-evidence.json').exists():
            Path('outputs/descendant-saw-full-evidence').touch()
        time.sleep(0.005)
while not Path('outputs/descendant-ready').exists():
    time.sleep(0.005)
'''
    if "-u" in flags:
        prelude += "assert __import__('sys').stdout.write_through\n"
    if "-B" in flags:
        prelude += "assert __import__('sys').dont_write_bytecode\n"
    host = setup(world, form=form, flags=flags, targeted=True, prelude=prelude,
                 network_policy={"allowed_protocols": ["dns", "tcp"], "allowed_routes": [upstream],
                                 "broker": {"host_managed": True}})
    host.attempt_root = attempt
    grant = host.request_provider_route_grant({"task": world.task})
    attempt.mkdir()
    # A reused caller-owned spool must not reveal a prior signed admission.
    (attempt / "broker-evidence.json").write_text('{"admission":{"task_id":"old-attempt"}}')
    (attempt / "network-evidence.json").write_text('{"admission":{"task_id":"old-attempt"}}')
    original_start = host._start_network_broker
    contexts = []
    def start(*args, **kwargs):
        context = original_start(*args, **kwargs)
        context.broker.response_body = b"loopback-fixture"
        assert context.broker.defer_evidence
        contexts.append(context)
        return context
    monkeypatch.setattr(host, "_start_network_broker", start)
    captures = []
    original_invoke = host.invoke_capability
    def invoke(**kwargs):
        captures.append({"worker_admission": copy.deepcopy(kwargs["admission"]),
                         "worker_request": copy.deepcopy(kwargs["request"]),
                         "worker_environment": {k: v for k, v in kwargs["child_env"].items()
                                                if k.startswith("ASTRID_NETWORK_") and k != "ASTRID_NETWORK_BROKER_TOKEN"}})
        assert not {"task_id", "attempt_id", "fence", "allowed_routes", "network_nonce"} & kwargs["admission"].keys()
        return original_invoke(**kwargs)
    monkeypatch.setattr(host, "invoke_capability", invoke)
    finalizations = []
    original_finalize = ObservableNetworkBroker.finalize_evidence
    def finalize(broker):
        if broker in [c.broker for c in contexts]:
            assert not host._active_processes
            assert not (attempt / "broker-evidence.json").exists()
            assert not (attempt / "outputs/descendant-saw-full-evidence").exists()
            descendant = int((attempt / "outputs/descendant-ready").read_text())
            # Process group cleanup may leave a zombie pending OS reaping.
            import subprocess
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(descendant)], capture_output=True, text=True).stdout.strip()
            assert not state or state.startswith("Z"), state
            diagnostic = json.loads((attempt / "network-evidence.json").read_text())
            assert set(diagnostic["admission"]) == {"admission_digest", "network_nonce"}
            finalizations.append({"descendant_state": state or "absent", "diagnostic": diagnostic,
                                  "broker_artifact_absent_before_finalization": True})
        original_finalize(broker)
    monkeypatch.setattr(ObservableNetworkBroker, "finalize_evidence", finalize)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(serve_one, world)
        c = world.claim
        result = host.run_task({"task": world.task}, lease_token=c["lease_id"], attempt_id=c["attempt_id"], fence=c["fence"],
                               provider_route_grant=grant)
        assert future.result(timeout=5)
    signed = json.loads((attempt / "broker-evidence.json").read_text())
    expected = contexts[0].broker._admission
    assert signed["admission"] == expected
    assert {k: expected[k] for k in ("task_id", "attempt_id", "fence")} == {
        "task_id": world.task["id"], "attempt_id": c["attempt_id"], "fence": c["fence"]}
    assert expected["allowed_routes"] == [upstream]
    unsigned = {k: v for k, v in signed.items() if k not in {"signature", "signature_algorithm"}}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert hmac.compare_digest(signed["signature"], hmac.new(contexts[0].evidence_key.encode(), canonical, hashlib.sha256).hexdigest())
    assert finalizations and (bool(captures) == (form == "python"))
    observations = json.loads((attempt / "outputs/action-context.json").read_text())
    assert not any("broker-evidence.json" in name for name in observations["readable_paths"])
    assert not (attempt / "outputs/descendant-saw-full-evidence").exists()
    assert world.service.task(c["task_id"])["task"]["status"] == "completed"
    _context_receipt("bounded-parent-" + form.removeprefix("-"), {
        "form": form, "action": observations, "python_worker": captures, "finalization": finalizations,
        "signed_broker_evidence": signed, "host_admission_exact": True, "hmac_verified": True,
        "runtime_child_count": len(world.service.store.delegated_children(world.task["id"], c["attempt_id"]))})


@pytest.mark.parametrize("field", ["digest", "nonce", "token"])
@pytest.mark.parametrize("change", ["missing", "wrong", "malformed"])
def test_d18_broker_rejects_invalid_bounded_handshake(tmp_path, field, change):
    from astrid.core.execution.network_broker import ObservableNetworkBroker
    from urllib.parse import urlsplit
    broker = ObservableNetworkBroker(defer_evidence=True).register_admission(
        {"task_id": "task", "attempt_id": "attempt", "fence": 1, "network_nonce": "nonce"},
        allowed_routes=["http://127.0.0.1:9"], evidence_path=tmp_path / "broker-evidence.json",
        evidence_key="host-key", auth_token="token").start()
    values = {"digest": broker.expected_admission_digest, "nonce": "nonce", "token": "token"}
    values[field] = {"missing": "", "wrong": "0" * 64 if field == "digest" else "wrong", "malformed": "bad value"}[change]
    try:
        endpoint = urlsplit(broker.endpoint)
        with socket.create_connection((endpoint.hostname, endpoint.port), timeout=2) as connection:
            connection.sendall(f"ASTRID-BROKER/1 HELLO {values['digest']} {values['nonce']} {values['token']}\n".encode())
            assert connection.recv(64) == b"ASTRID-BROKER/1 REJECT\n"
        assert not broker.evidence_path.exists()
        assert broker.evidence()[-1]["detail"].endswith("allowed=false")
    finally:
        broker.stop()


def test_d18_broker_cross_attempt_replay_rejected(tmp_path):
    from astrid.core.execution.network_broker import ObservableNetworkBroker
    from urllib.parse import urlsplit
    brokers = []
    try:
        for number in (1, 2):
            brokers.append(ObservableNetworkBroker(defer_evidence=True).register_admission(
                {"task_id": "same-task", "attempt_id": f"attempt-{number}", "fence": number, "network_nonce": f"nonce-{number}"},
                evidence_path=tmp_path / f"broker-{number}.json", evidence_key="host-key", auth_token=f"token-{number}").start())
        first, second = brokers
        endpoint = urlsplit(second.endpoint)
        with socket.create_connection((endpoint.hostname, endpoint.port), timeout=2) as connection:
            connection.sendall(f"ASTRID-BROKER/1 HELLO {first.expected_admission_digest} {first.expected_nonce} {first.auth_token}\n".encode())
            assert connection.recv(64) == b"ASTRID-BROKER/1 REJECT\n"
        assert not any(b.evidence_path.exists() for b in brokers)
    finally:
        for broker in brokers:
            broker.stop()


@pytest.mark.parametrize("field", ["admission_digest", "network_nonce", "token"])
@pytest.mark.parametrize("value", ["", "bad value", "☃", "x" * 257])
def test_d18_network_hook_rejects_malformed_material(monkeypatch, field, value):
    from astrid.core.execution import network_policy as policy
    monkeypatch.setattr(policy, "_POLICY", {"proxy": "http://127.0.0.1:9"})
    admission = {"admission_digest": "0" * 64, "network_nonce": "valid-nonce"}
    monkeypatch.setenv("ASTRID_NETWORK_BROKER_TOKEN", "valid-token")
    if field == "token":
        monkeypatch.setenv("ASTRID_NETWORK_BROKER_TOKEN", value)
    else:
        admission[field] = value
    monkeypatch.setattr(policy, "_ADMISSION", admission)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: pytest.fail("malformed material reached socket"))
    with pytest.raises(policy.NetworkPolicyError, match="malformed"):
        policy._broker_handshake()


@pytest.mark.parametrize("corruption", ["signature", "admission", "route"])
def test_d18_host_evidence_verification_rejects_tampering(tmp_path, monkeypatch, corruption):
    import hmac
    from astrid.core.execution.network_broker import ObservableNetworkBroker
    admission = {"task_id": "task", "attempt_id": "attempt", "fence": 3, "network_nonce": "nonce",
                 "allowed_routes": ["http://127.0.0.1:9"]}
    broker = ObservableNetworkBroker(defer_evidence=True).register_admission(
        admission, allowed_routes=admission["allowed_routes"], evidence_path=tmp_path / "broker-evidence.json",
        evidence_key="host-secret", auth_token="action-token")
    broker._record("handshake", "digest:nonce")
    broker._record("route", "http://127.0.0.1:9/allowed")
    assert not broker.evidence_path.exists()
    broker.finalize_evidence()
    payload = json.loads(broker.evidence_path.read_text())
    if corruption == "signature":
        payload["signature"] = "0" * 64
    else:
        if corruption == "admission":
            payload["admission"]["attempt_id"] = "foreign-attempt"
        else:
            payload["events"][-1]["detail"] = "http://127.0.0.1:10/foreign|allowed=true"
        unsigned = {k: v for k, v in payload.items() if k not in {"signature", "signature_algorithm"}}
        canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        payload["signature"] = hmac.new(b"host-secret", canonical, hashlib.sha256).hexdigest()
    broker.evidence_path.write_text(json.dumps(payload))
    monkeypatch.setattr(broker, "finalize_evidence", lambda: None)
    context = SimpleNamespace(broker=broker, evidence_key="host-secret")
    message = {"signature": "signature is invalid", "admission": "admission binding is invalid", "route": "unregistered route"}[corruption]
    with pytest.raises(HostError, match=message):
        GenericPackHost._network_evidence(tmp_path, admission=admission, broker_required=True, broker_context=context)


@pytest.mark.parametrize("form", ["python", "-c"])
@pytest.mark.parametrize("corruption", ["missing_digest", "wrong_digest", "malformed_digest", "missing_nonce", "wrong_nonce", "malformed_nonce", "missing_token", "wrong_token", "malformed_token", "cross_attempt"])
def test_d18_invalid_network_context_cannot_start_action(world, monkeypatch, form, corruption):
    attempt = world.tmp / "invalid-broker-attempt"
    marker = attempt / "pack-work-started"
    host = setup(world, form=form, code=f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
                 module_prelude=f"from pathlib import Path\nPath({str(attempt / 'pack-imported')!r}).touch()\n",
                 network_policy={"allowed_protocols": ["dns", "tcp"], "allowed_routes": [], "broker": {"host_managed": True}})
    host.attempt_root = attempt
    grant = host.request_provider_route_grant({"task": world.task})
    original = host._child_environment
    captured = []
    def environment(*args, **kwargs):
        env, secret_map = original(*args, **kwargs)
        if corruption == "cross_attempt":
            from astrid.core.execution.network_broker import ObservableNetworkBroker
            foreign = ObservableNetworkBroker().register_admission(
                {"task_id": world.task["id"], "attempt_id": "prior-attempt", "fence": 0, "network_nonce": "prior-nonce"},
                auth_token="prior-token")
            env.update(ASTRID_NETWORK_ADMISSION_DIGEST=foreign.expected_admission_digest,
                       ASTRID_NETWORK_NONCE=foreign.expected_nonce, ASTRID_NETWORK_BROKER_TOKEN=foreign.auth_token)
        else:
            kind, field = corruption.split("_", 1)
            key = {"digest": "ASTRID_NETWORK_ADMISSION_DIGEST", "nonce": "ASTRID_NETWORK_NONCE", "token": "ASTRID_NETWORK_BROKER_TOKEN"}[field]
            if kind == "missing":
                env.pop(key)
            else:
                env[key] = "bad value" if kind == "malformed" else ("0" * 64 if field == "digest" else "wrong")
        return env, secret_map
    original_start = host._start_network_broker
    def start(*args, **kwargs):
        result = original_start(*args, **kwargs)
        captured.append(result)
        return result
    monkeypatch.setattr(host, "_child_environment", environment)
    monkeypatch.setattr(host, "_start_network_broker", start)
    c = world.claim
    with pytest.raises(HostError) as error:
        host.run_task({"task": world.task}, lease_token=c["lease_id"], attempt_id=c["attempt_id"], fence=c["fence"], provider_route_grant=grant)
    assert "astrid network startup failed" in str(error.value)
    assert not (attempt / "pack-imported").exists()
    assert not marker.exists(), "broker handshake failure allowed pack code to run"
    assert world.service.task(c["task_id"])["task"]["status"] == "failed"
    assert not world.service.store.delegated_children(world.task["id"], c["attempt_id"])
    assert not any(event["detail"].endswith("|allowed=true") for context in captured for event in context.broker.evidence())

    _context_receipt("startup-" + form.removeprefix("-") + "-" + corruption, {"error": str(error.value), "action_started": marker.exists(), "broker_events": [context.broker.evidence() for context in captured]})


@pytest.mark.parametrize("form", ["python", "-c"])
@pytest.mark.parametrize("corruption", ["missing_policy", "malformed_policy", "nonmapping_policy", "missing_evidence", "missing_proxy", "missing_hook", "changed_hook", "shadow_hook", "import_failure", "install_failure", "noninstalling_hook", "broker_reject", "broker_disconnect", "broker_timeout", "broker_malformed_reply", "broker_connect_failure"])
def test_d18_network_startup_faults_are_fatal_before_pack_import(world, monkeypatch, form, corruption):
    import astrid.core.execution.generic_host as host_module
    import astrid.core.execution.network_broker as broker_module
    attempt = world.tmp / "startup-fault-attempt"
    sentinels = [attempt / name for name in ("pack-imported", "action-started", "child-started", "provider-started")]
    body = "from pathlib import Path\n" + "\n".join(f"Path({str(path)!r}).touch()" for path in sentinels) + "\n"
    host = setup(world, form=form, code=body,
                 module_prelude=f"from pathlib import Path\nPath({str(sentinels[0])!r}).touch()\n",
                 network_policy={"allowed_protocols": ["dns", "tcp"], "allowed_routes": [], "broker": {"host_managed": True}})
    host.attempt_root = attempt
    grant = host.request_provider_route_grant({"task": world.task})
    original_environment = host._child_environment
    contexts = []
    def environment(*args, **kwargs):
        env, secret_map = original_environment(*args, **kwargs)
        if corruption == "missing_policy": env.pop("ASTRID_NETWORK_POLICY")
        elif corruption == "malformed_policy": env["ASTRID_NETWORK_POLICY"] = "{"
        elif corruption == "nonmapping_policy": env["ASTRID_NETWORK_POLICY"] = "[]"
        elif corruption == "missing_evidence": env.pop("ASTRID_NETWORK_EVIDENCE")
        elif corruption == "missing_proxy":
            policy = json.loads(env["ASTRID_NETWORK_POLICY"])
            policy.pop("proxy")
            env["ASTRID_NETWORK_POLICY"] = json.dumps(policy)
        hook = attempt / ".astrid-network-hook/sitecustomize.py"
        shadow_code = f"from pathlib import Path\nPath({str(sentinels[0])!r}).touch()\n"
        if corruption == "missing_hook": hook.unlink()
        elif corruption == "changed_hook": hook.write_text(shadow_code)
        elif corruption == "shadow_hook": (attempt / "sitecustomize.py").write_text(shadow_code)
        return env, secret_map
    monkeypatch.setattr(host, "_child_environment", environment)
    original_start = host._start_network_broker
    def start(*args, **kwargs):
        context = original_start(*args, **kwargs)
        contexts.append(context)
        if corruption == "broker_connect_failure": context.broker.stop()
        return context
    monkeypatch.setattr(host, "_start_network_broker", start)
    if corruption in {"broker_reject", "broker_disconnect", "broker_timeout", "broker_malformed_reply"}:
        def handle(handler):
            handler.rfile.readline(8192)
            handler.server.broker._record("handshake", "startup-fixture-reject", allowed=False)
            if corruption == "broker_timeout":
                time.sleep(3.3)
            elif corruption == "broker_reject":
                handler.wfile.write(b"ASTRID-BROKER/1 REJECT\n")
                handler.wfile.flush()
            elif corruption == "broker_malformed_reply":
                handler.wfile.write(b"ASTRID-BROKER/1 unexpected\n")
                handler.wfile.flush()
        monkeypatch.setattr(broker_module._BrokerHandler, "handle", handle)
    if corruption in {"import_failure", "install_failure", "noninstalling_hook"}:
        source = attempt / "failing-network-policy.py"
        original_argv = host_module._network_startup_argv
        def argv(*args, **kwargs):
            command = original_argv(*args, **kwargs)
            bodies = {"import_failure": "raise ImportError('fixture')\n",
                      "install_failure": "def install_from_environment(**kwargs):\n    raise RuntimeError('fixture')\n",
                      "noninstalling_hook": "def install_from_environment(**kwargs):\n    return False\n"}
            source.write_text(bodies[corruption])
            location = command.index(host_module._STRICT_NETWORK_STARTUP) + 1
            config = json.loads(command[location])
            config.update(policy=str(source), policy_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
            command[location] = json.dumps(config)
            return command
        monkeypatch.setattr(host_module, "_network_startup_argv", argv)
    c = world.claim
    start_time = time.monotonic()
    with pytest.raises(HostError, match="astrid network startup failed") as error:
        host.run_task({"task": world.task}, lease_token=c["lease_id"], attempt_id=c["attempt_id"], fence=c["fence"], provider_route_grant=grant)
    seconds = time.monotonic() - start_time
    assert seconds < 8
    assert not any(path.exists() for path in sentinels)
    assert not host._active_processes
    assert world.service.task(c["task_id"])["task"]["status"] == "failed"
    assert not world.service.store.delegated_children(world.task["id"], c["attempt_id"])
    _context_receipt("fatal-startup-" + form.removeprefix("-") + "-" + corruption,
                     {"error": str(error.value), "seconds": seconds, "sentinels_absent": True,
                      "active_processes": False, "runtime_parent_status": "failed", "runtime_children": 0,
                      "broker_events": [context.broker.evidence() for context in contexts]})


def test_d18_same_admission_repeated_handshake_preserves_contract(tmp_path):
    from astrid.core.execution.network_broker import ObservableNetworkBroker
    from urllib.parse import urlsplit
    broker = ObservableNetworkBroker(defer_evidence=True).register_admission(
        {"task_id": "task", "attempt_id": "attempt", "fence": 1, "network_nonce": "nonce"},
        evidence_path=tmp_path / "broker-evidence.json", evidence_key="host-key", auth_token="token").start()
    try:
        endpoint = urlsplit(broker.endpoint)
        for _ in range(2):
            with socket.create_connection((endpoint.hostname, endpoint.port), timeout=2) as connection:
                connection.sendall(f"ASTRID-BROKER/1 HELLO {broker.expected_admission_digest} nonce token\n".encode())
                assert connection.recv(64) == b"ASTRID-BROKER/1 OK\n"
        assert len(broker.events) == 2
        assert not broker.evidence_path.exists()
    finally:
        broker.stop()


def test_d18_context_uses_registered_dynamic_admission_digest(world):
    setup(world, network_policy={"allowed_protocols": ["dns", "tcp"], "allowed_routes": ["http://127.0.0.1:9"],
                                 "dynamic_url_inputs": ["url"], "broker": {"host_managed": True}})
    attempt = world.tmp / "dynamic-broker-attempt"
    attempt.mkdir()
    admission = {"task_id": "parent", "attempt_id": "current", "fence": 2, "network_nonce": "current-nonce", "allowed_routes": []}
    prior = hashlib.sha256(json.dumps(admission, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    record = world.host.capabilities[PARENT]
    context = world.host._start_network_broker(record, attempt, admission, {"url": "http://127.0.0.1:10/request"}, private_runtime_context=True)
    try:
        env, secrets = world.host._child_environment(record, attempt, admission={"network_nonce": "untrusted-action-view"},
                                                    network_broker=context, allow_runtime_connection=False)
        assert admission["allowed_routes"] == ["http://127.0.0.1:9", "http://127.0.0.1:10"]
        assert context.broker._admission == admission
        assert env["ASTRID_NETWORK_ADMISSION_DIGEST"] == context.broker.expected_admission_digest != prior
        assert env["ASTRID_NETWORK_NONCE"] == "current-nonce"
        assert "ASTRID_NETWORK_ADMISSION" not in env
        assert "ASTRID_NETWORK_BROKER_EVIDENCE" not in env
        assert not context.broker.evidence_path.exists()
        env.clear()
        secrets.clear()
    finally:
        context.stop()

# F05 host acceptance fixture. This pins the declared Human Review ID/port and
# ordinary state file ABI, but intentionally does not claim M04 receiver/schema
# or browser save/reload proof.
REVIEW = "editorial.human_review"


def _snapshot_fixture(world, *, form="python", limits=None):
    setup(world)
    world.service.cancel_task_canonical(world.claim["task_id"], {}, idempotency_key="discard-initial")
    review = world.tmp / "editorial"
    (review / "actions").mkdir(parents=True)
    (review / "actions/review.py").write_text(
        "def run(state=None):\n"
        "    import json, os\n"
        "    from pathlib import Path\n"
        "    from astrid.sdk import _child_bridge as bridge_sdk\n"
        "    assert not any(k in os.environ for k in ('BANODOCO_RUNTIME_CREDENTIAL', 'BANODOCO_RUNTIME_ENDPOINT', 'CHILD_AUTHORITY', 'PARENT_LEASE_ID'))\n"
        "    data = Path(state).read_bytes() if state else b'{\"draft\":1}'\n"
        "    Path('outputs').mkdir(exist_ok=True)\n"
        "    Path('outputs/draft.json').write_bytes(data)\n"
        "    receipt = bridge_sdk._bridge.publish_snapshot(filename='draft.json', output_port='state_result', revision=1)\n"
        "    assert set(receipt) == {'association_id', 'receipt_id', 'revision', 'output_port', 'digest', 'size', 'durability'}\n"
        "    Path('outputs/answer.txt').write_bytes(b'final:' + data)\n"
        "    return {'state': json.loads(data), 'snapshot': receipt}\n")
    (review / "pack.yaml").write_text(yaml.safe_dump({"schema_version": 3, "id": "editorial", "name": "Host review fixture", "version": "1.0.0", "actions": {
        "human_review": {"description": "Bounded host fixture", "invocation": {"kind": "python", "path": "actions/review.py", "function": "run"},
            "inputs": [{"name": "state", "type": "file", "required": False}],
            "outputs": [{"name": "answer", "type": "file", "mode": "create", "path_template": "{out}/answer.txt", "artifact_type": "text/plain"}]}}}))
    body = ("from pathlib import Path\nimport hashlib, os\n"
        "from astrid.core.execution.executor import registry as er\n"
        "from astrid.core.execution.orchestrator import registry as ore\n"
        "from astrid.core.pack import discovery as pd\n"
        "for module in (er, ore, pd): module.discover_packs = lambda *a, **kw: ()\n"
        "from astrid import invoke\n"
        "assert isinstance(resume_state, str) and Path(resume_state).is_file()\n"
        "oid = 'sha256:' + hashlib.sha256(Path(resume_state).read_bytes()).hexdigest()\n"
        "descriptor = {'object_id': oid, 'digest': oid, 'filename': 'review-state.json'}\n"
        f"r = invoke({REVIEW!r}, kind='action', extra_pack_roots=({str(review)!r},), inputs={{'state': descriptor}}, child_key='resumed-review', wait=True, timeout_seconds=3, poll_seconds=0.01)\n"
        "assert r.ok, r.error\n"
        "assert len(r.outputs['managed_outputs']) == 1\n"
        "assert r.outputs['managed_outputs'][0]['output_port'] == 'answer'\n"
        "assert not any(k in os.environ for k in ('BANODOCO_RUNTIME_CREDENTIAL', 'BANODOCO_RUNTIME_ENDPOINT', 'CHILD_AUTHORITY', 'PARENT_LEASE_ID'))\n")
    if form == "python":
        (world.root / "actions/parent.py").write_text("def run(resume_state=None):\n" + "\n".join("    " + line for line in body.splitlines()) + "\n    return {'child': r.kernel_task_id}\n")
        invocation = {"kind": "python", "path": "actions/parent.py", "function": "run"}
    else:
        (world.root / "resume.py").write_text("import sys\nresume_state = sys.argv[1]\n" + body)
        invocation = {"kind": "command", "command": {"argv": ["{python_exec}", str(world.root / "resume.py"), "{resume_state}"]}}
    manifest = yaml.safe_load((world.root / "pack.yaml").read_text())
    manifest["actions"]["parent"]["invocation"] = invocation
    manifest["actions"]["parent"]["inputs"] = [{"name": "resume_state", "type": "file", "required": False}]
    (world.root / "pack.yaml").write_text(yaml.safe_dump(manifest))
    host = GenericPackHost(pack_roots=[world.root, review], client=world.client, executor_id="worker", max_concurrency=4)
    host.discover()
    for record in host.capabilities.values():
        world.service.register_capability({"capability_id": record.id, "definition_digest": record.capability_digest})
    world.service.register_executor({"executor_id": "worker", "capabilities": [PARENT, CHILD, REVIEW], "max_concurrency": 4, "runtime_epoch": world.service.health()["runtime_epoch"]}, idempotency_key="snapshot-register")
    cap = {"capability_id": REVIEW, "capability_digest": host.capabilities[REVIEW].capability_digest}
    policy = {"capabilities": [cap], "targets": [{"kind": "default"}], "input_object_ids": [],
              "recoverable_outputs": [{**cap, "output_ports": ["state_result"]}], "limits": {"max_children": 4, **(limits or {})}}
    world.client.generated.admit_task(capability_id=PARENT, capability_digest=host.capabilities[PARENT].capability_digest,
        input_object_ids=[], project_id=world.project, spec={"inputs": {}}, child_delegation=policy, idempotency_key="snapshot-parent")
    world.host = host
    world.policy = policy
    world.review_root = review
    world.claim = _claim_snapshot_task(world, PARENT, key="claim-snapshot-parent")
    world.task = _snapshot_task(world, world.claim)
    parent_bridge = bridge(world)
    frame = {"v": 1, "request_id": 1, "op": "submit", "child_key": "source-review", "child": cap,
             "inputs": {}, "input_descriptors": [], "wait": True, "timeout_seconds": 2, "poll_seconds": 0.01}
    child = parent_bridge.dispatch(frame)
    claim = _claim_snapshot_task(world, REVIEW, key="claim-source-review")
    assert claim["task_id"] == child["task_id"]
    task = _snapshot_task(world, claim)
    output = world.tmp / "source-review-attempt/outputs"
    output.mkdir(parents=True)
    source = HostChildBridge(host, task, **{k: claim[k] for k in ("attempt_id", "lease_id", "fence", "runtime_epoch")}, output_root=output, cancelled=lambda: False)
    source._thread.start()  # direct wire fixture, no process launch
    world.bridges.append(source)
    world.source_bridge, world.source_claim, world.source_task, world.parent_bridge = source, claim, task, parent_bridge
    return source


def _claim_snapshot_task(world, capability_id, *, key):
    epoch = world.service.health()["runtime_epoch"]
    body = {"executor_id": "worker", "capability_ids": [capability_id], "runtime_epoch": epoch}
    row = world.service.store.conn.execute("SELECT id FROM tasks WHERE capability=? AND status='queued' LIMIT 1", (capability_id,)).fetchone()
    if row is not None:
        target = world.service.store.effective_execution_target(row["id"])
        if target is not None:
            body["target"] = target
    claim = world.service.claim_next(body, idempotency_key=key, identity=world.identity)
    assert claim
    world.client._attempt_runtime_epochs[claim["attempt_id"]] = claim["runtime_epoch"]
    return claim


def _snapshot_task(world, claim):
    task = world.service.task(claim["task_id"])["task"]
    task.update(runtime_epoch=claim["runtime_epoch"], fence=claim["fence"], project_id=world.project,
        spec=claim["spec"], input_object_ids=claim["input_object_ids"], lease_id=claim["lease_id"], executor_id="worker")
    return task


def _save_snapshot(world, *, revision=1, data=b'{"last_acknowledged":true}', sdk=None):
    source = world.source_bridge
    (source.output_root / "draft.json").write_bytes(data)
    if sdk is not None:
        return sdk.publish_snapshot(filename="draft.json", output_port="state_result", revision=revision)
    return source.dispatch({"v": 1, "request_id": source._sequence + 1, "op": "publish_snapshot",
        "filename": "draft.json", "output_port": "state_result", "revision": revision})


def _fresh_snapshot_reader(world, *, authorized=True):
    from runtime_protocol.auth import CredentialStore
    from runtime_protocol.server import RuntimeHandler
    credentials = CredentialStore(world.tmp / ("fresh-reader-" + str(time.monotonic_ns())))
    token, _ = credentials.provision("fresh-reader", ["tasks:read", "objects:read"] if authorized else [])
    client = RuntimeProtocolClient("http://127.0.0.1:1", token)
    from banodoco_workspace_client import WorkspaceClient
    import inspect
    assert type(client.generated) is WorkspaceClient
    assert Path(inspect.getfile(WorkspaceClient)).resolve() == Path(__file__).resolve().parents[3] / "banodoco_workspace_client/generated.py"

    def transport(method, path, headers, body):
        with world.lock:
            handler = object.__new__(RuntimeHandler)
            handler.server = SimpleNamespace(runtime=world.service, credentials=credentials)
            handler.command, handler.path, handler.headers = method, path, headers
            handler._send = lambda status, value=None, *, headers=None, body=None: (
                status, headers or {}, body if body is not None else json.dumps(value).encode())
            assert method == "GET" and body is None
            return handler._route()
    client.generated._transport = transport
    assert client._attempt_runtime_epochs == {}
    return client


def _resume_selector(world, receipt):
    return dict(source_task_id=world.source_claim["task_id"], source_attempt_id=world.source_claim["attempt_id"],
        association_id=receipt["association_id"], revision=receipt["revision"], expected_project_id=world.project,
        expected_capability_digest=world.host.capabilities[REVIEW].capability_digest)


def test_snapshot_exact_public_receipt_and_snapshot_only_submit_denied(world):
    source = _snapshot_fixture(world)
    sdk = ChildBridge(source.child_channel)
    receipt = _save_snapshot(world, sdk=sdk)
    assert receipt == _save_snapshot(world, sdk=sdk)
    assert set(receipt) == {"association_id", "receipt_id", "revision", "output_port", "digest", "size", "durability"}
    assert SECRET not in json.dumps(receipt)
    with pytest.raises(_BridgeRejected):
        sdk.submit({k: v for k, v in request(world).items() if k not in {"v", "request_id", "op"}})
    assert len(world.service.store.recoverable_snapshots(attempt_id=world.source_claim["attempt_id"])) == 1


@pytest.mark.parametrize("fault", ["lost", "receipt", "digest", "revision", "attempt"])
def test_snapshot_uncertain_response_not_acknowledged_and_exact_replay(world, monkeypatch, fault):
    _snapshot_fixture(world)
    original = world.client.generated.publish_recoverable_snapshot
    def uncertain(*args, **kwargs):
        result = original(*args, **kwargs)
        if fault == "lost":
            raise TimeoutError("response lost after commit")
        if fault == "receipt":
            result["receipt"] = None
        elif fault == "digest":
            result["digest"] = digest(b"other")
        elif fault == "revision":
            result["provenance"]["revision"] += 1
        elif fault == "attempt":
            result["attempt_id"] = "foreign"
        return result
    monkeypatch.setattr(world.client.generated, "publish_recoverable_snapshot", uncertain)
    with pytest.raises((TimeoutError, ChildBridgeError)):
        _save_snapshot(world)
    rows = world.service.store.recoverable_snapshots(attempt_id=world.source_claim["attempt_id"])
    assert len(rows) == 1
    monkeypatch.setattr(world.client.generated, "publish_recoverable_snapshot", original)
    receipt = _save_snapshot(world)
    assert receipt["association_id"] == rows[0]["association_id"]
    assert _save_snapshot(world) == receipt
    with pytest.raises(ChildBridgeError, match="different bytes"):
        _save_snapshot(world, data=b'{"different":true}')


@pytest.mark.parametrize("filename", ["../escape.json", "/tmp/escape.json", "link.json", "folder/draft.json", "fifo", "hard.json"])
def test_snapshot_publish_confines_attempt_file(world, filename):
    source = _snapshot_fixture(world)
    outside = world.tmp / "outside.json"
    outside.write_bytes(b"outside")
    (source.output_root / "link.json").symlink_to(outside)
    (source.output_root / "folder").symlink_to(world.tmp, target_is_directory=True)
    os.mkfifo(source.output_root / "fifo")
    os.link(outside, source.output_root / "hard.json")
    with pytest.raises((ChildBridgeError, OSError)):
        source.dispatch({"v": 1, "request_id": 1, "op": "publish_snapshot", "filename": filename, "output_port": "state_result", "revision": 1})
    assert not any(call[1].endswith("recoverable-snapshots") for call in world.calls)


@pytest.mark.parametrize("bad", [{"revision": 0}, {"revision": True}, {"output_port": "other"}, {"task_id": "foreign"}, {"digest": digest(b"claim")}, {"size": 1}])
def test_snapshot_publish_rejects_invalid_or_authority_bearing_frame(world, bad):
    source = _snapshot_fixture(world)
    (source.output_root / "draft.json").write_bytes(b"{}")
    with pytest.raises(ChildBridgeError):
        source.dispatch({"v": 1, "request_id": 1, "op": "publish_snapshot", "filename": "draft.json", "output_port": "state_result", "revision": 1, **bad})
    assert not any(call[1].endswith("recoverable-snapshots") for call in world.calls)


@pytest.mark.parametrize("bound", ["object", "count", "total"])
def test_snapshot_publication_enforces_frozen_bounds(world, bound):
    limits = {"max_snapshot_bytes": 10, "max_recoverable_snapshots": 1 if bound == "count" else 4,
              "max_recoverable_bytes": 5 if bound == "total" else 100}
    _snapshot_fixture(world, limits=limits)
    if bound == "object":
        with pytest.raises(ChildBridgeError):
            _save_snapshot(world, data=b"x" * 11)
    else:
        _save_snapshot(world, data=b"123")
        with pytest.raises(ChildBridgeError):
            _save_snapshot(world, revision=2, data=b"456")


@pytest.mark.parametrize("field,value", [
    ("task_id", "foreign"), ("attempt_id", "foreign"), ("project_id", "foreign"), ("output_port", "other"),
    ("role", "output"), ("durability", "temporary"), ("state", "expired"), ("digest", digest(b"wrong")),
    ("object_id", digest(b"wrong")), ("size", 1), ("size", 64 * 1024 * 1024 + 1),
    ("provenance", {"revision": 2}), ("lifecycle", {"state": "expired"}), ("variant_key", "2")])
def test_snapshot_resume_rejects_invalid_custody(world, monkeypatch, field, value):
    _snapshot_fixture(world)
    receipt = _save_snapshot(world)
    reader = _fresh_snapshot_reader(world)
    original_list = reader.generated.list_managed_outputs
    original_get = reader.generated.get_managed_output
    def corrupt(row):
        row = resource(row)
        row[field] = value
        return row
    monkeypatch.setattr(reader.generated, "list_managed_outputs", lambda tid: ([corrupt(r) for r in original_list(tid)[0]], None))
    monkeypatch.setattr(reader.generated, "get_managed_output", lambda aid: corrupt(original_get(aid)))
    with pytest.raises(ChildBridgeError):
        _prepare_review_resume_input(reader, **_resume_selector(world, receipt))


@pytest.mark.parametrize("fault", ["list_get", "reread", "bytes", "page", "missing", "duplicate", "capability", "limit", "selector"])
def test_snapshot_resume_rejects_changed_or_uncertain_selection(world, monkeypatch, fault):
    _snapshot_fixture(world)
    receipt = _save_snapshot(world)
    reader = _fresh_snapshot_reader(world)
    selector = _resume_selector(world, receipt)
    if fault in {"list_get", "reread"}:
        original, count = reader.generated.get_managed_output, [0]
        def get(aid):
            row = resource(original(aid)); count[0] += 1
            if count[0] >= (2 if fault == "reread" else 1):
                row["filename"] = "changed.json"
            return row
        monkeypatch.setattr(reader.generated, "get_managed_output", get)
    elif fault == "bytes":
        monkeypatch.setattr(reader.generated, "get_object", lambda oid: SimpleNamespace(data=b"corrupt"))
    elif fault in {"page", "missing", "duplicate"}:
        rows, _ = reader.generated.list_managed_outputs(selector["source_task_id"])
        monkeypatch.setattr(reader.generated, "list_managed_outputs", lambda tid: (rows * 2 if fault == "duplicate" else [] if fault == "missing" else rows, "next" if fault == "page" else None))
    elif fault in {"capability", "limit"}:
        task = resource(reader.generated.get_task(selector["source_task_id"]))
        if fault == "capability":
            task["capability_digest"] = digest(b"changed-source")
        else:
            task["spec"]["delegated_recoverable_outputs"]["limits"]["max_snapshot_bytes"] = 1
        monkeypatch.setattr(reader.generated, "get_task", lambda tid: task)
    else:
        selector["revision"] = 2
    with pytest.raises(ChildBridgeError):
        _prepare_review_resume_input(reader, **selector)


@pytest.mark.parametrize("corruption", [None, "attempt_id", "output_port", "revision", "role", "lifecycle", "digest", "size", "list_get"])
def test_snapshot_excluded_from_exact_final_output_set(world, monkeypatch, corruption):
    _snapshot_fixture(world)
    receipt = _save_snapshot(world)
    claim = world.source_claim
    final = b"normal-final"
    world.service.settle_attempt(claim["attempt_id"], {**{k: claim[k] for k in ("lease_id", "fence", "runtime_epoch")},
        "outputs": [{"name": "answer", "output_port": "answer", "digest": digest(final), "data_base64": base64.b64encode(final).decode()}]}, idempotency_key="final-review", identity=world.identity)
    if corruption:
        original_list, original_get = world.client.child_outputs, world.client.child_output
        def corrupt(value):
            row = resource(value)
            if row["association_id"] == receipt["association_id"]:
                if corruption == "revision": row["provenance"]["revision"] = 0
                elif corruption == "lifecycle": row["lifecycle"] = {"state": "expired"}
                elif corruption == "size": row["size"] = 0
                elif corruption == "digest": row["digest"] = digest(b"wrong")
                elif corruption == "role": row["role"] = "output"
                else: row[corruption if corruption != "list_get" else "filename"] = "foreign"
            return row
        monkeypatch.setattr(world.client, "child_outputs", lambda tid: [corrupt(row) for row in original_list(tid)])
        if corruption != "list_get": monkeypatch.setattr(world.client, "child_output", lambda aid: corrupt(original_get(aid)))
    frame = {"v": 1, "request_id": 2, "op": "outputs", "child_key": "source-review", "task_id": claim["task_id"]}
    if corruption:
        with pytest.raises(ChildBridgeError): world.parent_bridge.dispatch(frame)
    else:
        rows = world.parent_bridge.dispatch(frame)
        assert len(rows) == 1 and rows[0]["output_port"] == "answer" and rows[0]["digest"] == digest(final)
        world.parent_bridge.finish()


@pytest.mark.parametrize("end", ["cancel", "expiry", "restart"])
@pytest.mark.parametrize("form", ["python", "command"])
def test_snapshot_fresh_authorized_resume_through_ordinary_managed_child(world, end, form):
    source = _snapshot_fixture(world, form=form)
    acknowledged = _save_snapshot(world)
    # A later committed response was not acknowledged by the browser/controller.
    _save_snapshot(world, revision=2, data=b'{"unacknowledged":true}')
    selector = _resume_selector(world, acknowledged)
    if end == "cancel":
        world.service.cancel_task_canonical(world.source_claim["task_id"], {}, idempotency_key="cancel-source")
    elif end == "expiry":
        from datetime import datetime, timedelta, timezone
        expiry = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with world.service.store._transaction():
            world.service.store.conn.execute("UPDATE tasks SET lease_expires_at=? WHERE id=?", (expiry, selector["source_task_id"]))
            world.service.store.conn.execute("UPDATE attempts SET lease_expires_at=? WHERE id=?", (expiry, selector["source_attempt_id"]))
            world.service.store._reap_expired_leases()
    else:
        from runtime_protocol.service import RuntimeService
        world.service.close()
        world.service = RuntimeService(world.realm)
    with pytest.raises(ChildBridgeError):
        source.dispatch({"v": 1, "request_id": source._sequence + 1, "op": "publish_snapshot", "filename": "draft.json", "output_port": "state_result", "revision": 3})
    assert source._revoked.is_set()
    reader = _fresh_snapshot_reader(world)
    from runtime_protocol.errors import AuthorizationError
    with pytest.raises(AuthorizationError):
        _prepare_review_resume_input(_fresh_snapshot_reader(world, authorized=False), **selector)
    descriptor = _prepare_review_resume_input(reader, **selector)
    assert descriptor == {"object_id": digest(b'{"last_acknowledged":true}'), "digest": digest(b'{"last_acknowledged":true}'), "filename": "review-state.json"}
    assert world.service.task(selector["source_task_id"])["task"]["status"] != "completed"
    # Expiry/restart can requeue the historical task. Finish containment of
    # the interrupted lineage after verified readback, before new admission.
    world.service.cancel_task_canonical(world.claim["task_id"], {}, idempotency_key="contain-old-parent")
    fresh = RuntimeProtocolClient("http://127.0.0.1:1", SECRET)
    from banodoco_workspace_client import WorkspaceClient
    assert type(fresh.generated) is WorkspaceClient
    fresh.generated._transport = world.transport
    fresh.executor_id = "worker"
    world.client = fresh
    host = GenericPackHost(pack_roots=[world.root, world.review_root], client=fresh, executor_id="worker", max_concurrency=4)
    host.discover()
    policy = copy.deepcopy(world.policy)
    policy["input_object_ids"] = [descriptor["digest"]]
    fresh.generated.admit_task(capability_id=PARENT, capability_digest=host.capabilities[PARENT].capability_digest,
        input_object_ids=[descriptor["digest"]], project_id=world.project, spec={"inputs": {"resume_state": descriptor}},
        execution_request={"schema_version": 1, "target": {"kind": "default"}, "inputs": [{"name": "resume_state", **descriptor}]},
        child_delegation=policy, idempotency_key="fresh-resume-parent")
    claim = _claim_snapshot_task(world, PARENT, key="claim-resume-parent")
    task = _snapshot_task(world, claim)
    assert task["input_object_ids"] == task["spec"]["child_delegation"]["input_object_ids"] == [descriptor["digest"]]
    def serve_review():
        child_host = GenericPackHost(pack_roots=[world.root, world.review_root], client=fresh, executor_id="worker", max_concurrency=4)
        child_host.discover()
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            with world.lock:
                body = {"executor_id": "worker", "capability_ids": [REVIEW], "runtime_epoch": world.service.health()["runtime_epoch"]}
                row = world.service.store.conn.execute("SELECT id FROM tasks WHERE capability=? AND status='queued' LIMIT 1", (REVIEW,)).fetchone()
                if row is not None:
                    target = world.service.store.effective_execution_target(row["id"])
                    if target is not None:
                        body["target"] = target
                child_claim = world.service.claim_next(body, idempotency_key="claim-resumed-" + str(time.monotonic_ns()), identity=world.identity)
            if child_claim:
                fresh._attempt_runtime_epochs[child_claim["attempt_id"]] = child_claim["runtime_epoch"]
                child_task = _snapshot_task(world, child_claim)
                return child_host.run_task({"task": child_task}, lease_token=child_claim["lease_id"], attempt_id=child_claim["attempt_id"], fence=child_claim["fence"])
            time.sleep(0.01)
        raise AssertionError("resumed child not admitted")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        child_future = pool.submit(serve_review)
        try:
            result = host.run_task({"task": task}, lease_token=claim["lease_id"], attempt_id=claim["attempt_id"], fence=claim["fence"])
        except Exception:
            child_future.result(timeout=7)
            raise
        child_result = child_future.result(timeout=7)
    assert result and child_result
    resumed_task = world.service.task(child_result["task_id"])["task"]
    assert resumed_task["status"] == "completed"
    assert resumed_task["result"]["action_result"]["state"] == {"last_acknowledged": True}
    assert world.service.task(claim["task_id"])["task"]["status"] == "completed"
    delegated = [call[2]["task"] for call in world.calls if call[1] == "/v1/delegated-tasks"][-1]
    assert delegated["spec"]["inputs"] == {"state": descriptor}
    assert delegated["input_object_ids"] == [descriptor["digest"]]
    assert not any(k in delegated["spec"]["inputs"] for k in selector)
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("corruption", [None, "provenance_task", "provenance_attempt", "provenance_capability", "group", "final_attempt"])
def test_snapshot_from_prior_cancelled_attempt_excluded_after_same_task_retry(world, monkeypatch, corruption):
    source = _snapshot_fixture(world)
    saved = _save_snapshot(world)
    attempt_a = world.source_claim
    world.service.cancel_task_canonical(attempt_a["task_id"], {}, idempotency_key="cancel-attempt-a")
    assert world.service.task(attempt_a["task_id"])["task"]["status"] == "cancelled"
    assert world.service.task(world.claim["task_id"])["task"]["status"] == "running"
    with pytest.raises(ChildBridgeError):
        source._live()
    world.service.retry_task(attempt_a["task_id"], {}, idempotency_key="retry-same-review-task")
    attempt_b = _claim_snapshot_task(world, REVIEW, key="claim-attempt-b")
    assert attempt_b["task_id"] == attempt_a["task_id"]
    assert attempt_b["attempt_id"] != attempt_a["attempt_id"]
    final = b"final-from-attempt-b"
    world.service.settle_attempt(attempt_b["attempt_id"], {**{k: attempt_b[k] for k in ("lease_id", "fence", "runtime_epoch")},
        "outputs": [{"name": "answer", "output_port": "answer", "digest": digest(final), "data_base64": base64.b64encode(final).decode()}]}, idempotency_key="settle-attempt-b", identity=world.identity)
    if corruption:
        original_list, original_get = world.client.child_outputs, world.client.child_output
        def corrupt(value):
            row = resource(value)
            if row["association_id"] == saved["association_id"]:
                if corruption == "group":
                    row["group_key"] = "recoverable-foreign"
                elif corruption.startswith("provenance_"):
                    field = {"provenance_task": "task_id", "provenance_attempt": "attempt_id", "provenance_capability": "capability_id"}[corruption]
                    row["provenance"][field] = "foreign"
            elif corruption == "final_attempt":
                row["attempt_id"] = attempt_a["attempt_id"]
            return row
        monkeypatch.setattr(world.client, "child_outputs", lambda tid: [corrupt(row) for row in original_list(tid)])
        monkeypatch.setattr(world.client, "child_output", lambda aid: corrupt(original_get(aid)))
    frame = {"v": 1, "request_id": 2, "op": "outputs", "child_key": "source-review", "task_id": attempt_a["task_id"]}
    if corruption:
        with pytest.raises(ChildBridgeError):
            world.parent_bridge.dispatch(frame)
    else:
        rows = world.parent_bridge.dispatch(frame)
        assert len(rows) == 1 and rows[0]["digest"] == digest(final)
        assert rows[0]["attempt_id"] == attempt_b["attempt_id"]
        assert rows[0]["association_id"] != saved["association_id"]
        world.parent_bridge.finish()


def _command_output_record(outputs, *, inputs=()):
    from astrid.core.execution.executor.schema import validate_executor_definition

    definition = validate_executor_definition({"id": "fixture.command", "name": "Command output bindings",
        "kind": "external", "version": "1.0", "inputs": list(inputs), "outputs": outputs,
        "command": {"argv": ["{python_exec}", "-c", "pass"]}})
    return SimpleNamespace(id=definition.id, definition=definition)


def test_declared_command_output_names_aliases_templates_and_caller_precedence(tmp_path):
    from astrid.core.execution.generic_host import _bind_host_owned_command_outputs

    root = tmp_path / "outputs"
    root.mkdir()
    record = _command_output_record([
        {"name": "report", "type": "file", "placeholder": "report_path", "path_template": "{out}/{stem}.json"},
        {"name": "sibling", "type": "file", "path_template": "{report_path}.copy"},
        {"name": "default", "type": "file"},
        {"name": "relative", "type": "file", "path_template": "nested/relative.txt"},
        {"name": "video", "type": "file", "path_template": "{out}/{output_name}"},
    ], inputs=[{"name": "report", "type": "string"}, {"name": "stem", "type": "string"}, {"name": "output_name", "type": "string"}])
    values = {"out": str(root), "run_root": str(tmp_path), "python_exec": sys.executable,
        "stem": "accepted", "output_name": "hype.mp4", "report": "/caller/outside.json", "report_path": "/caller/alias.json"}
    command_values, bound = _bind_host_owned_command_outputs(record, values, output_root=root)
    assert command_values["report"] == values["report"]
    assert command_values["report_path"] == str(root / "accepted.json")
    assert bound["report"] == bound["report_path"] == str(root / "accepted.json")
    assert bound["sibling"] == str(root / "accepted.json.copy")
    assert bound["default"] == str(root / "default")
    assert bound["relative"] == str(root / "nested/relative.txt")
    assert bound["video"] == str(root / "hype.mp4")
    assert {name: bound[name] for name in ("out", "run_root", "python_exec", "stem", "output_name")} == {
        name: values[name] for name in ("out", "run_root", "python_exec", "stem", "output_name")}
    assert values["report"] == "/caller/outside.json", "binding mutated the caller mapping"


@pytest.mark.parametrize("escape", ["relative", "absolute", "symlink", "input"])
def test_declared_command_output_rejects_path_escape(tmp_path, escape):
    from astrid.core.execution.generic_host import _bind_host_owned_command_outputs

    root = tmp_path / "outputs"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    template = {"relative": "../escape.json", "absolute": str(outside / "escape.json"),
        "symlink": "{out}/linked/escape.json", "input": "{out}/{filename}"}[escape]
    record = _command_output_record([{"name": "report", "type": "file", "path_template": template}],
        inputs=[{"name": "filename", "type": "string"}])
    with pytest.raises(HostError, match="escapes output root"):
        _bind_host_owned_command_outputs(record, {"out": str(root), "filename": "../escape.json"}, output_root=root)
    assert list(outside.iterdir()) == []


def test_declared_command_output_rejects_host_placeholder_override(tmp_path):
    from astrid.core.execution.generic_host import _bind_host_owned_command_outputs

    root = tmp_path / "outputs"
    root.mkdir()
    record = _command_output_record([{"name": "report", "type": "file",
        "placeholder": "out", "path_template": "{out}/report.json"}])
    with pytest.raises(HostError, match="overlaps"):
        _bind_host_owned_command_outputs(record, {"out": str(root), "source": "/caller/outside"}, output_root=root)


def test_declared_command_output_input_aliases_defaults_and_template_seed(tmp_path):
    from astrid.core.execution.generic_host import _bind_host_owned_command_outputs
    from astrid.core.contracts.binding import expand_command

    root = tmp_path / "outputs"
    root.mkdir()
    record = _command_output_record([
        {"name": "report", "type": "file", "placeholder": "destination", "path_template": "{out}/{stem}.json"},
        {"name": "stem", "type": "file", "path_template": "{report}.copy"},
        {"name": "video", "type": "file", "path_template": "{out}/{output_name}"},
    ], inputs=[{"name": "source", "type": "string", "placeholder": "destination", "required": False, "default": "original-input"},
        {"name": "stem", "type": "string", "required": False, "default": "default-stem"},
        {"name": "output_name", "type": "string", "required": False}])
    values = {"out": str(root), "python_exec": sys.executable, "report": "/caller/output", "destination": "/caller/alias"}
    command_values, harvest_values = _bind_host_owned_command_outputs(record, values, output_root=root)
    assert command_values["source"] == "original-input"
    assert command_values["destination"] == harvest_values["destination"]
    assert command_values["stem"] == "default-stem"
    assert command_values["report"] == harvest_values["report"] == str(root / "default-stem.json")
    assert harvest_values["destination"] == harvest_values["report"]
    assert harvest_values["stem"] == str(root / "default-stem.json.copy")
    assert command_values["video"] == harvest_values["video"] == str(root / "hype.mp4")
    binding = expand_command({"argv": ["{python_exec}", "{source}", "{destination}", "{stem}", "{report}", "{output_name}"]},
        record.definition.inputs, command_values, {})
    assert binding.argv == (sys.executable, "original-input", "original-input", "default-stem", str(root / "default-stem.json"), "hype.mp4")
    assert values == {"out": str(root), "python_exec": sys.executable, "report": "/caller/output", "destination": "/caller/alias"}


def test_declared_command_output_input_alias_does_not_replace_another_canonical_input(tmp_path):
    from astrid.core.execution.generic_host import _bind_host_owned_command_outputs
    from astrid.core.contracts.binding import expand_command

    root = tmp_path / "outputs"
    root.mkdir()
    record = _command_output_record([
        {"name": "target", "type": "file", "path_template": "{out}/{target}.json"},
    ], inputs=[{"name": "target", "type": "string", "required": False, "default": "B"},
        {"name": "source", "type": "string", "placeholder": "target", "required": False, "default": "A"}])
    command_values, harvest_values = _bind_host_owned_command_outputs(record, {"out": str(root)}, output_root=root)
    assert command_values["target"] == "B" and command_values["source"] == "A"
    assert harvest_values["target"] == str(root / "B.json")
    binding = expand_command({"argv": ["program", "{target}"],
        "input_args": [{"input": "target", "flag": "--target"}, {"input": "source", "flag": "--source"}]},
        record.definition.inputs, command_values, {})
    # Normal port-order alias expansion is preserved, while mapped arguments
    # still consume their own canonical values.
    assert binding.argv == ("program", "A", "--target", "B", "--source", "A")


def test_declared_command_outputs_expand_and_settle_through_normal_host(world):
    code = ("import sys\nfrom pathlib import Path\n"
        "assert sys.argv[1] == sys.argv[2]\n"
        "assert Path(sys.argv[1]).parent == Path(sys.argv[3])\n"
        "assert Path(sys.argv[1]).name == sys.argv[4] + '.txt'\n"
        "Path(sys.argv[1]).write_bytes(b'host-declared-output')\n")
    host = setup(world, form="script", code=code, delegating=False,
        parent_inputs=[{"name": "stem", "type": "string", "required": False, "default": "named"}],
        parent_outputs=[{"name": "report", "type": "file", "placeholder": "report_path", "mode": "create",
            "path_template": "{out}/{stem}.txt", "artifact_type": "text/plain"}],
        argv_override=["{python_exec}", str(world.root / "command.py"), "{report}", "{report_path}", "{out}", "{stem}"])
    claim = world.claim
    result = host.run_task({"task": world.task}, lease_token=claim["lease_id"], attempt_id=claim["attempt_id"], fence=claim["fence"])
    task = world.service.task(claim["task_id"])["task"]
    assert task["status"] == "completed" and result
    rows, cursor = world.client.generated.list_managed_outputs(claim["task_id"])
    assert cursor is None and len(rows) == 1
    row = resource(rows[0])
    assert row["output_port"] == "report" and row["digest"] == digest(b"host-declared-output")
    assert world.client.get_object(row["digest"][7:]) == b"host-declared-output"
    assert [(item.get("output_port", item["name"]), item["digest"], item["size"]) for item in task["result"]["outputs"]] == [
        (row["output_port"], row["digest"], row["size"])]


@pytest.mark.parametrize("transport", ["placeholder", "mapped", "auto"])
def test_declared_command_output_collision_keeps_materialized_input_and_independent_harvest(world, transport):
    original = b"original-admitted-input"
    world.service.ingest(world.project, original, media_type="text/plain", original_name="source.txt", idempotency_key="collision-source")
    descriptor = {"object_id": digest(original), "digest": digest(original), "filename": "source.txt"}
    code = ("import sys\nfrom pathlib import Path\n"
        "destination = Path(sys.argv[1])\n"
        "assert destination.name == 'rewritten.txt'\n"
        "assert sys.argv[2] in ('--original', '--source')\n"
        "source = Path(sys.argv[3])\n"
        "assert source.name == 'source.txt' and source != destination\n"
        "assert source.read_bytes() == b'original-admitted-input'\n"
        "assert len(sys.argv) == 4 or sys.argv[4] == str(source)\n"
        "destination.write_bytes(b'independent-host-output')\n")
    argv = ["{python_exec}", str(world.root / "command.py"), "{out}/rewritten.txt"]
    fields = {}
    if transport == "placeholder":
        argv += ["--original", "{source}", "{incoming}"]
    elif transport == "mapped":
        fields = {"input_args": [{"input": "source", "flag": "--original"}]}
    host = setup(world, form="script", code=code, delegating=False,
        root_object_ids=[descriptor["digest"]], parent_input_values={"source": descriptor},
        parent_inputs=[{"name": "source", "type": "file", "placeholder": "incoming", "required": True}],
        parent_outputs=[{"name": "source", "type": "file", "placeholder": "incoming", "mode": "create",
            "path_template": "{out}/rewritten.txt", "artifact_type": "text/plain"}],
        argv_override=argv, parent_command_fields=fields)
    claim = world.claim
    result = host.run_task({"task": world.task}, lease_token=claim["lease_id"], attempt_id=claim["attempt_id"], fence=claim["fence"])
    task = world.service.task(claim["task_id"])["task"]
    assert result and task["status"] == "completed"
    rows, cursor = world.client.generated.list_managed_outputs(claim["task_id"])
    assert cursor is None and len(rows) == 1
    row = resource(rows[0])
    assert row["output_port"] == "source" and row["digest"] == digest(b"independent-host-output")
    assert row["digest"] != descriptor["digest"]
    assert world.client.get_object(descriptor["digest"][7:]) == original
    assert world.client.get_object(row["digest"][7:]) == b"independent-host-output"
    assert [(item.get("output_port", item["name"]), item["digest"], item["size"]) for item in task["result"]["outputs"]] == [
        (row["output_port"], row["digest"], row["size"])]


def _real_review_fixture(world):
    """Only the parent is synthetic; Human Review and M17 schemas are source-owned."""
    import io
    import zipfile

    from astrid.packs.editorial.actions.human_review import run
    from astrid.core.foundation.hash import executor_definition_digest
    from astrid.core.pack.loader import load_pack_manifest
    from astrid.sdk.actions import action_executor_definition

    setup(world)
    world.service.cancel_task_canonical(world.claim["task_id"], {}, idempotency_key="discard-initial")
    candidate = Path(__file__).resolve().parents[3]
    review = candidate / "astrid/packs/editorial"
    pack = load_pack_manifest(review / "pack.yaml")
    definition = action_executor_definition(pack, "human_review", pack.actions["human_review"])
    declared_digest = "sha256:" + executor_definition_digest(definition)
    schemas = candidate / "astrid/packs/training/actions/dataset_build/schemas"
    schema_bytes = io.BytesIO()
    schema_hashes = {}
    with zipfile.ZipFile(schema_bytes, "w") as archive:
        for name in sorted(run._STATE_SCHEMA_FILES):
            data = (schemas / name).read_bytes()
            archive.writestr(name, data)
            schema_hashes[name] = digest(data)
    assets = io.BytesIO()
    with zipfile.ZipFile(assets, "w") as archive:
        archive.writestr("human-review-assets.json", json.dumps({"html_root": "html", "mounts": {"/clips": "clips"}}))
        archive.writestr("html/index.html", "<html>actual Human Review</html>")
        archive.writestr("clips/sample.txt", "mounted-review-media")
    initial = run._encode_state(run.make_initial_state(
        run_id="real-receiver", writer_id="human", status="reviewing", now="2026-10-03T00:00:00Z"))
    payloads = {
        "assets_bundle": ("assets.zip", assets.getvalue()),
        "state_schema_bundle": ("schemas.zip", schema_bytes.getvalue()),
        "data": ("data.json", json.dumps({"items": [{"item_id": "one"}, {"item_id": "two"}]}).encode()),
        "state": ("review-state.json", initial),
        "response_schema": ("response.schema.json", json.dumps({"type": "object", "required": ["decisions"],
            "properties": {"decisions": {"type": "object"}}, "additionalProperties": False}).encode()),
    }
    inputs = {"no_open": True, "timeout": 30}
    for name, (filename, data) in payloads.items():
        world.service.ingest(world.project, data, media_type="application/octet-stream",
            original_name=filename, idempotency_key="real-input-" + name)
        inputs[name] = {"object_id": digest(data), "digest": digest(data), "filename": filename}
    stable_inputs = {name: value for name, value in inputs.items() if name != "state"}
    # Exercise the existing managed SDK path with the real static action
    # projection. Whole-registry public discovery is a separate owner seam.
    body = ("from pathlib import Path\nimport hashlib, os\n"
        "from astrid.core.foundation.hash import executor_definition_digest\n"
        "from astrid.core.pack.loader import load_pack_manifest\n"
        "from astrid.core.execution.executor.registry import ExecutorRegistry\n"
        "from astrid.sdk.actions import action_executor_definition, validate_action_inputs_definition\n"
        "from astrid.sdk.discovery import _capability_from_executor\n"
        "from astrid.sdk.invocation import _invoke_bridge_child\n"
        "from astrid.sdk import _child_bridge\n"
        f"pack = load_pack_manifest(Path({str(review)!r}) / 'pack.yaml')\n"
        "definition = action_executor_definition(pack, 'human_review', pack.actions['human_review'])\n"
        f"assert 'sha256:' + executor_definition_digest(definition) == {declared_digest!r}\n"
        "registry = ExecutorRegistry([definition])\n"
        "capability = _capability_from_executor(definition, registry)\n"
        "assert _child_bridge._bridge is not None\n"
        "assert isinstance(resume_state, str) and Path(resume_state).is_file()\n"
        "oid = 'sha256:' + hashlib.sha256(Path(resume_state).read_bytes()).hexdigest()\n"
        "descriptor = {'object_id': oid, 'digest': oid, 'filename': 'review-state.json'}\n"
        f"inputs = {stable_inputs!r}\ninputs['state'] = descriptor\n"
        "assert set(inputs) <= {port.name for port in definition.inputs}\n"
        "validate_action_inputs_definition(definition, inputs)\n"
        "r = _invoke_bridge_child(capability, registries=(registry,), inputs=inputs, bridge=_child_bridge._bridge, child_key='resumed-review', wait=True, timeout_seconds=20, poll_seconds=0.01)\n"
        "assert r.ok, r.error\n"
        "assert len(r.outputs['managed_outputs']) == 2\n"
        "assert {row['output_port'] for row in r.outputs['managed_outputs']} == {'decisions', 'state_result'}\n"
        "assert not any(k in os.environ for k in ('BANODOCO_RUNTIME_CREDENTIAL', 'BANODOCO_RUNTIME_ENDPOINT', 'CHILD_AUTHORITY', 'PARENT_LEASE_ID'))\n")
    (world.root / "actions/parent.py").write_text("def run(resume_state=None):\n"
        + "\n".join("    " + line for line in body.splitlines()) + "\n    return {'child': r.kernel_task_id}\n")
    manifest = yaml.safe_load((world.root / "pack.yaml").read_text())
    manifest["actions"]["parent"]["inputs"] = [{"name": "resume_state", "type": "file", "required": False}]
    (world.root / "pack.yaml").write_text(yaml.safe_dump(manifest))
    host = GenericPackHost(pack_roots=[world.root, review], client=world.client, executor_id="worker", max_concurrency=4)
    host.discover()
    record = host.capabilities[REVIEW]
    assert record.capability_digest == declared_digest
    declaration = yaml.safe_load((review / "pack.yaml").read_text())["actions"]["human_review"]
    assert record.source_root.resolve() == review.resolve()
    assert record.definition.metadata["action_invocation"] == declaration["invocation"]
    assert declaration["invocation"]["command"]["argv"][2] == "astrid.packs.editorial.actions.human_review.run"
    assert record.definition.metadata["runtime_file"] == "actions/human_review/run.py"
    for capability_id in (PARENT, REVIEW):
        record = host.capabilities[capability_id]
        world.service.register_capability({"capability_id": record.id, "definition_digest": record.capability_digest})
    world.service.register_executor({"executor_id": "worker", "capabilities": [PARENT, CHILD, REVIEW],
        "max_concurrency": 4, "runtime_epoch": world.service.health()["runtime_epoch"]}, idempotency_key="real-register")
    cap = {"capability_id": REVIEW, "capability_digest": host.capabilities[REVIEW].capability_digest}
    object_ids = sorted(value["digest"] for value in inputs.values() if isinstance(value, dict))
    policy = {"capabilities": [cap], "targets": [{"kind": "default"}], "input_object_ids": object_ids,
        "recoverable_outputs": [{**cap, "output_ports": ["state_result"]}], "limits": {"max_children": 4}}
    world.client.generated.admit_task(capability_id=PARENT, capability_digest=host.capabilities[PARENT].capability_digest,
        input_object_ids=object_ids, project_id=world.project, spec={"inputs": {}}, child_delegation=policy,
        idempotency_key="real-source-parent")
    world.host, world.policy, world.review_root = host, policy, review
    world.claim = _claim_snapshot_task(world, PARENT, key="claim-real-source-parent")
    world.task = _snapshot_task(world, world.claim)
    world.parent_bridge = bridge(world)
    child = world.parent_bridge.dispatch({"v": 1, "request_id": 1, "op": "submit", "child_key": "source-review",
        "child": cap, "inputs": inputs,
        "input_descriptors": [{"name": port.name, "kind": "object", "object_id": inputs[port.name]["digest"]}
            for port in host.capabilities[REVIEW].definition.inputs if port.type == "file" and port.name in inputs],
        "wait": True, "timeout_seconds": 2, "poll_seconds": 0.01})
    world.source_claim = _claim_snapshot_task(world, REVIEW, key="claim-real-source-review")
    assert world.source_claim["task_id"] == child["task_id"]
    world.source_task = _snapshot_task(world, world.source_claim)
    world.real_inputs, world.schema_hashes, world.initial_state = stable_inputs, schema_hashes, initial


def _real_receiver_host(world, monkeypatch, name):
    host = GenericPackHost(pack_roots=[world.root, world.review_root], client=world.client,
        executor_id="worker", max_concurrency=4, attempt_root=world.tmp / name)
    host.discover()
    launched = threading.Event()
    processes = []
    original = host._track_process
    def track(process):
        original(process)
        processes.append(process)
        launched.set()
    monkeypatch.setattr(host, "_track_process", track)
    original_environment = host._child_environment
    def environment(*args, **kwargs):
        env, secrets = original_environment(*args, **kwargs)
        assert not any(key in env for key in (
            "BANODOCO_RUNTIME_CREDENTIAL", "BANODOCO_RUNTIME_ENDPOINT", "CHILD_AUTHORITY", "PARENT_LEASE_ID"))
        assert SECRET not in json.dumps(env)
        return env, secrets
    monkeypatch.setattr(host, "_child_environment", environment)
    return host, launched, processes


def _real_receiver_url(launched, processes, future):
    import select

    assert launched.wait(8), future.result(timeout=1)
    process = processes[0]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if select.select([process.stdout], [], [], 0.05)[0]:
            line = process.stdout.readline().strip()
            if line.startswith("human_review: serving at "):
                return line.split("serving at ", 1)[1]
        if future.done():
            pytest.fail(f"receiver exited before startup: {future.result()}")
    pytest.fail("actual receiver did not announce its local HTTP URL")


def _real_review_http(url, route, body=None):
    from urllib.error import HTTPError
    from urllib.parse import parse_qs, urlsplit
    from urllib.request import Request, urlopen

    parsed = urlsplit(url)
    request = Request(f"http://{parsed.netloc}{route}", data=None if body is None else json.dumps(body).encode(),
        headers={"X-Session-Token": parse_qs(parsed.query)["token"][0], "Content-Type": "application/json"})
    try:
        response = urlopen(request, timeout=8)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.read()


def _acknowledge_real_snapshot(world, monkeypatch, url, route, body, revision):
    """Observe Runtime custody while the actual browser response is still gated."""
    from concurrent.futures import ThreadPoolExecutor

    original = world.client.generated.publish_recoverable_snapshot
    committed, release = threading.Event(), threading.Event()
    observed = {}
    def publish(*args, **kwargs):
        result = original(*args, **kwargs)
        row = resource(result)
        with world.lock:
            assert kwargs["revision"] == row["provenance"]["revision"] == revision
            assert row["role"] == "recoverable_snapshot" and row["durability"] == "durable"
            assert row["output_port"] == "state_result"
            assert row["attempt_id"] == world.source_claim["attempt_id"]
            stored = world.service.managed_output(row["association_id"])
            assert resource(stored)["digest"] == row["digest"]
            data = world.service.cas.path_for(row["digest"][7:]).read_bytes()
            assert digest(data) == row["digest"] and len(data) == row["size"]
            assert json.loads(data)["state_version"] == revision
            observed.update(row=row, data=data)
        committed.set()
        assert release.wait(8), "HTTP publication gate was not released"
        return result
    monkeypatch.setattr(world.client.generated, "publish_recoverable_snapshot", publish)
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(_real_review_http, url, route, body)
        try:
            assert committed.wait(7), response.result(timeout=1)
            assert not response.done(), "HTTP success preceded durable publication receipt"
        finally:
            release.set()
        status, raw = response.result(timeout=8)
    monkeypatch.setattr(world.client.generated, "publish_recoverable_snapshot", original)
    assert status == 200 and json.loads(raw)["state_version"] == revision
    assert _real_review_http(url, "/state.json") == (200, observed["data"])
    return {"association_id": observed["row"]["association_id"], "revision": revision}, observed["data"]


def _real_unacknowledged_batch(world, monkeypatch, url, state_bytes):
    original = world.client.generated.publish_recoverable_snapshot
    committed = []
    def lose_response(*args, **kwargs):
        committed.append(resource(original(*args, **kwargs)))
        raise TimeoutError("actual receiver publication response lost after Runtime commit")
    monkeypatch.setattr(world.client.generated, "publish_recoverable_snapshot", lose_response)
    try:
        status, raw = _real_review_http(url, "/submit-batch", {
            "base_state_version": 2, "item_ids": ["one"], "decision": "reject"})
    finally:
        monkeypatch.setattr(world.client.generated, "publish_recoverable_snapshot", original)
    assert status == 503 and json.loads(raw)["error"] == "batch_unpublished"
    assert len(committed) == 1 and committed[0]["provenance"]["revision"] == 3
    assert _real_review_http(url, "/state.json") == (200, state_bytes)
    assert world.service.cas.path_for(committed[0]["digest"][7:]).read_bytes() != state_bytes
    return committed[0]


@pytest.mark.parametrize("end", ["cancel", "expiry", "restart"])
def test_actual_human_review_acknowledged_batch_recovers_and_settles(world, monkeypatch, end):
    from concurrent.futures import ThreadPoolExecutor

    _real_review_fixture(world)
    source_host, launched, processes = _real_receiver_host(world, monkeypatch, "actual-source")
    with ThreadPoolExecutor(max_workers=2) as pool:
        claim = world.source_claim
        source_future = pool.submit(source_host.run_task, {"task": world.source_task},
            lease_token=claim["lease_id"], attempt_id=claim["attempt_id"], fence=claim["fence"])
        try:
            url = _real_receiver_url(launched, processes, source_future)
            assert _real_review_http(url, "/state.json") == (200, world.initial_state)
            assert _real_review_http(url, "/clips/sample.txt") == (200, b"mounted-review-media")
            _, saved = _acknowledge_real_snapshot(world, monkeypatch, url, "/save", {
                "base_state_version": 0, "revisions": {"one": {"decision": "reject"}}}, 1)
            acknowledged, batch_bytes = _acknowledge_real_snapshot(world, monkeypatch, url, "/submit-batch", {
                "base_state_version": 1, "item_ids": ["one", "two"], "decision": "accept", "reviewer_id": "batch-human"}, 2)
            batch = json.loads(batch_bytes)
            assert saved != batch_bytes and batch["state_version"] == 2
            assert set(batch["review_decisions"]) == {"one", "two"}
            assert all(row["decision"] == "accept" and row["state_version"] == 2
                and row["reviewer_id"] == "batch-human" for row in batch["review_decisions"].values())
            status, raw = _real_review_http(url, "/submit-batch", {
                "base_state_version": 1, "item_ids": ["one"], "decision": "reject"})
            assert status == 409 and json.loads(raw)["error"] == "stale_state"
            assert len(world.service.store.recoverable_snapshots(attempt_id=claim["attempt_id"])) == 2
            newer = _real_unacknowledged_batch(world, monkeypatch, url, batch_bytes)
            assert newer["association_id"] != acknowledged["association_id"]
            selector = _resume_selector(world, acknowledged)
            with source_host._process_lock:
                old_bridge, = source_host._child_bridges
            if end == "cancel":
                with world.lock:
                    world.service.cancel_task_canonical(claim["task_id"], {}, idempotency_key="cancel-real-source")
            elif end == "expiry":
                from datetime import datetime, timedelta, timezone
                expiry = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
                with world.lock, world.service.store._transaction():
                    world.service.store.conn.execute("UPDATE tasks SET lease_expires_at=? WHERE id=?", (expiry, claim["task_id"]))
                    world.service.store.conn.execute("UPDATE attempts SET lease_expires_at=? WHERE id=?", (expiry, claim["attempt_id"]))
                    world.service.store._reap_expired_leases()
            else:
                from runtime_protocol.service import RuntimeService
                with world.lock:
                    world.service.close()
                    world.service = RuntimeService(world.realm)
            # A verified execution binding can retain a running task while
            # Runtime contains uncertain placement. The normal lease pump
            # observes the expired/stale attempt and stops this owned process.
            assert source_future.result(timeout=8)["status"] == "cancelled"
            with pytest.raises(ChildBridgeError):
                old_bridge._live()
            assert old_bridge._revoked.is_set()
            reader = _fresh_snapshot_reader(world)
            from runtime_protocol.errors import AuthorizationError
            with pytest.raises(AuthorizationError):
                _prepare_review_resume_input(_fresh_snapshot_reader(world, authorized=False), **selector)
            descriptor = _prepare_review_resume_input(reader, **selector)
            assert descriptor == {"object_id": digest(batch_bytes), "digest": digest(batch_bytes), "filename": "review-state.json"}
            assert reader.get_object(descriptor["digest"][7:]) == batch_bytes
            assert world.service.task(claim["task_id"])["task"]["status"] != "completed"
            with world.lock:
                world.service.cancel_task_canonical(world.claim["task_id"], {}, idempotency_key="contain-real-parent")
        finally:
            if not source_future.done():
                with world.lock:
                    world.service.cancel_task_canonical(world.claim["task_id"], {}, idempotency_key="cleanup-real-source-parent")
                source_future.result(timeout=8)
            source_host.shutdown()
        assert source_future.result(timeout=8)["status"] == "cancelled"

        fresh = RuntimeProtocolClient("http://127.0.0.1:1", SECRET)
        from banodoco_workspace_client import WorkspaceClient
        import inspect
        assert type(fresh.generated) is WorkspaceClient
        assert Path(inspect.getfile(WorkspaceClient)).resolve() == Path(__file__).resolve().parents[3] / "banodoco_workspace_client/generated.py"
        fresh.generated._transport = world.transport
        fresh.executor_id = "worker"
        world.client = fresh
        host = GenericPackHost(pack_roots=[world.root, world.review_root], client=fresh,
            executor_id="worker", max_concurrency=4, attempt_root=world.tmp / "actual-parent")
        host.discover()
        policy = copy.deepcopy(world.policy)
        policy["input_object_ids"] = sorted([descriptor["digest"], *[value["digest"]
            for value in world.real_inputs.values() if isinstance(value, dict)]])
        fresh.generated.admit_task(capability_id=PARENT, capability_digest=host.capabilities[PARENT].capability_digest,
            input_object_ids=policy["input_object_ids"], project_id=world.project, spec={"inputs": {"resume_state": descriptor}},
            child_delegation=policy, idempotency_key="actual-fresh-parent")
        parent_claim = _claim_snapshot_task(world, PARENT, key="claim-actual-fresh-parent")
        parent_task = _snapshot_task(world, parent_claim)
        assert parent_task["input_object_ids"] == parent_task["spec"]["child_delegation"]["input_object_ids"] == policy["input_object_ids"]
        assert descriptor["digest"] in parent_task["input_object_ids"]
        parent_future = pool.submit(host.run_task, {"task": parent_task}, lease_token=parent_claim["lease_id"],
            attempt_id=parent_claim["attempt_id"], fence=parent_claim["fence"])
        resumed_host, launched, processes = _real_receiver_host(world, monkeypatch, "actual-resumed")
        def serve_resumed():
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if parent_future.done():
                    pytest.fail(f"ordinary parent ended before child admission: {parent_future.result()}")
                with world.lock:
                    queued = world.service.store.conn.execute("SELECT id FROM tasks WHERE capability=? AND status='queued' LIMIT 1", (REVIEW,)).fetchone()
                    if queued:
                        child_claim = _claim_snapshot_task(world, REVIEW, key="claim-actual-resumed")
                        world.resumed_claim = child_claim
                        child_task = _snapshot_task(world, child_claim)
                        break
                time.sleep(0.01)
            else:
                pytest.fail("ordinary parent did not admit actual resumed Human Review")
            return resumed_host.run_task({"task": child_task}, lease_token=child_claim["lease_id"],
                attempt_id=child_claim["attempt_id"], fence=child_claim["fence"])
        resumed_future = pool.submit(serve_resumed)
        try:
            resumed_url = _real_receiver_url(launched, processes, resumed_future)
            assert _real_review_http(resumed_url, "/state.json") == (200, batch_bytes)
            assert (resumed_host.attempt_root / "inputs/review-state.json").read_bytes() == batch_bytes
            resumed_snapshot = _real_unacknowledged_batch(world, monkeypatch, resumed_url, batch_bytes)
            final_body = {"decisions": batch["review_decisions"]}
            assert _real_review_http(resumed_url, "/submit", final_body) == (204, b"")
            child_result = resumed_future.result(timeout=10)
            parent_result = parent_future.result(timeout=10)
        finally:
            if not resumed_future.done() or not parent_future.done():
                with world.lock:
                    world.service.cancel_task_canonical(parent_claim["task_id"], {}, idempotency_key="cleanup-real-resumed-parent")
                resumed_future.result(timeout=8)
                parent_future.result(timeout=8)
            resumed_host.shutdown()
            host.shutdown()
        assert child_result and parent_result
        child_task = world.service.task(world.resumed_claim["task_id"])["task"]
        assert child_task["status"] == "completed"
        assert world.service.task(parent_claim["task_id"])["task"]["status"] == "completed"
        rows, cursor = fresh.generated.list_managed_outputs(child_task["id"])
        rows = [resource(row) for row in rows]
        assert cursor is None and len(rows) == 3
        snapshots = [row for row in rows if row["role"] == "recoverable_snapshot"]
        assert len(snapshots) == 1 and snapshots[0]["association_id"] == resumed_snapshot["association_id"]
        ordinary = [row for row in rows if row["role"] != "recoverable_snapshot"]
        assert {row["output_port"] for row in ordinary} == {"decisions", "state_result"}
        assert {(row["output_port"], row["digest"], row["size"]) for row in ordinary} == {
            (row.get("output_port", row["name"]), row["digest"], row["size"]) for row in child_task["result"]["outputs"]}
        for row in ordinary:
            data = fresh.get_object(row["digest"][7:])
            assert data == batch_bytes if row["output_port"] == "state_result" else json.loads(data) == final_body
        delegated = [call[2]["task"] for call in world.calls if call[1] == "/v1/delegated-tasks"][-1]
        assert delegated["spec"]["inputs"] == {**world.real_inputs, "state": descriptor}
        assert delegated["input_object_ids"] == [delegated["spec"]["inputs"][port.name]["digest"]
            for port in host.capabilities[REVIEW].definition.inputs
            if port.type == "file" and port.name in delegated["spec"]["inputs"]]
        assert set(delegated["input_object_ids"]) == set(policy["input_object_ids"])
        assert not any(key in delegated["spec"]["inputs"] for key in selector)
        assert SECRET not in json.dumps(parent_result)
        evidence = os.environ.get("ASTRID_D18_REAL_RECEIVER_EVIDENCE")
        if evidence:
            Path(evidence, f"actual-receiver-{end}.json").write_text(json.dumps({
                "lifecycle": end, "source_selector": selector, "acknowledged_digest": digest(batch_bytes),
                "acknowledged_state": batch, "unacknowledged_newer_association": newer["association_id"],
                "recovered_descriptor": descriptor, "old_bridge_revoked": True, "normal_vendor_identity": True,
                "publication_before_http_success": [1, 2], "schema_hashes": world.schema_hashes,
                "resumed_task_id": child_task["id"], "ordinary_output_ports": [row["output_port"] for row in ordinary],
                "excluded_snapshot_association": resumed_snapshot["association_id"],
            }, indent=2) + "\n")


# D18 host materialization: retain the real generated-client/offline Runtime
# route; corrupt only the boundary under examination.
def _materialization_fixture(world, data=(b"child-bytes",), *, expose=True, filenames=None):
    setup(world, derived=True, child_limits={"max_children": 8})
    b = bridge(world)
    (b.output_root / "source.txt").write_bytes(b"initial-parent-input")
    producer = {"filename": "source.txt", "output_port": "source", "media_type": "text/plain"}
    rows = []
    first = None
    # Runtime forbids duplicate digests in one settlement. Distinct admitted
    # child settlements retain distinct association authority for equal bytes.
    for i, value in enumerate(data):
        key = "child" if i == 0 else "child-" + str(i)
        child = b.dispatch(request(world, key=key, value=producer,
                                   descriptors=[{"name": "value", "kind": "producer_file", **producer}],
                                   request_id=b._sequence + 1))
        first = first or child
        c = _claim_snapshot_task(world, CHILD, key="claim-materialization-" + str(i))
        output = {"name": "answer" + str(i), "output_port": "answer" + str(i),
                  "filename": filenames[i] if filenames is not None else "answer" + str(i) + ".txt", "media_type": "text/plain",
                  "digest": digest(value), "data_base64": base64.b64encode(value).decode()}
        world.service.settle_attempt(c["attempt_id"], {**{k: c[k] for k in ("lease_id", "fence", "runtime_epoch")},
                                     "outputs": [output]}, idempotency_key="settle-materialization-" + str(i), identity=world.identity)
        page = (b.dispatch({"v": 1, "request_id": b._sequence + 1, "op": "outputs", "child_key": key, "task_id": child["task_id"]})
                if expose else [resource(row) for row in world.client.child_outputs(child["task_id"])])
        rows.extend(page)
    return b, first, rows


def _materialize(b, child, row, **changes):
    task_id = row["task_id"]
    key = next(key for key, bound in b._children.items()
               if bound["task"] is not None and bound["task"].get("task_id", bound["task"].get("id")) == task_id)
    return b.dispatch({"v": 1, "request_id": b._sequence + 1, "op": "materialize_output",
                       "child_key": key, "task_id": task_id, "association_id": row["association_id"], **changes})


def _retained_usage(b):
    return len(b._materializations), sum(entry["output"]["size"] for entry in b._materializations.values())


def _expected_registration_filename(source):
    # Independently spell out the ruled projection, rather than calling the host helper.
    return (source if "/" not in source else
            "producer-" + hashlib.sha256(source.encode("utf-8")).hexdigest()
            + "".join(Path(source).suffixes))


def _capture_registration(world, monkeypatch):
    captured = SimpleNamespace(uploads=[], admissions=[], receipts=[])
    upload = world.client.upload_child_input
    admit = world.client.admit_child
    issue = world.client.generated.issue_child_authority

    def uploading(data, **kwargs):
        captured.uploads.append((bytes(data), copy.deepcopy(kwargs["descriptor"])))
        return upload(data, **kwargs)

    def admitting(**kwargs):
        captured.admissions.append(copy.deepcopy(kwargs))
        return admit(**kwargs)

    def issuing(*args, **kwargs):
        receipt = issue(*args, **kwargs)
        captured.receipts.append(resource(receipt))
        return receipt

    monkeypatch.setattr(world.client, "upload_child_input", uploading)
    monkeypatch.setattr(world.client, "admit_child", admitting)
    monkeypatch.setattr(world.client.generated, "issue_child_authority", issuing)
    return captured


@pytest.mark.parametrize("source", ["flat.txt", "nested/answer.txt", "nested/deeper/answer.tar.gz", "nested/extensionless", "nésted/answer.PNG"])
def test_materialization_producer_registration_names_and_exact_repeat(world, monkeypatch, source):
    setup(world, derived=True)
    b = bridge(world)
    path = b.output_root / source
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"registration-bytes")
    producer = {"filename": source, "output_port": "source", "media_type": "text/plain"}
    captured = _capture_registration(world, monkeypatch)

    def submit(value=producer):
        return b.dispatch(request(world, key="registration", value=value,
                          descriptors=[{"name": "value", "kind": "producer_file", **value}],
                          request_id=b._sequence + 1))

    first = submit()
    alias = _expected_registration_filename(source)
    assert Path(alias).name == alias
    assert "".join(Path(alias).suffixes) == "".join(Path(source).suffixes)
    ref = captured.uploads[0][1]
    assert ref == {"name": "value", "filename": alias, "output_port": "source", "media_type": "text/plain",
                   "object_id": digest(b"registration-bytes"), "size": len(b"registration-bytes")}
    assert captured.uploads[0][0] == path.read_bytes() == b"registration-bytes"
    assert producer["filename"] == source
    assert captured.admissions[0]["inputs"] == {"value": producer}
    assert captured.admissions[0]["ordered_inputs"] == [{**ref, "required": True}]
    assert captured.admissions[0]["derived_inputs"] == [ref]
    authority_ref = captured.receipts[0]["derived_inputs"][0]
    assert all(authority_ref[k] == v for k, v in ref.items())
    registry = world.service.task(world.task["id"])["task"]["spec"]["derived_input_registry"]
    assert registry[ref["object_id"]]["filename"] == alias
    delegated = [call[2]["task"] for call in world.calls if call[1] == "/v1/delegated-tasks"][0]
    expected = {"object_id": ref["object_id"], "digest": ref["object_id"], "filename": alias}
    assert delegated["spec"]["inputs"]["value"] == expected
    assert delegated["execution_request"]["inputs"] == [{"name": "value", **expected, "required": True}]
    assert submit() == first
    assert len(captured.uploads) == len(captured.admissions) == len(captured.receipts) == 1
    changed = {**producer, "output_port": "changed"}
    with pytest.raises(ChildBridgeError, match="different inputs or bytes"):
        submit(changed)
    path.write_bytes(b"changed-registration-bytes")
    with pytest.raises(ChildBridgeError, match="different inputs or bytes"):
        submit()
    assert len(captured.uploads) == len(captured.admissions) == len(captured.receipts) == 1
    assert world.service.task(world.task["id"])["task"]["spec"]["derived_input_registry"] == registry


@pytest.mark.parametrize("collision", ["same_source", "flat_alias", "object_alias"])
def test_materialization_duplicate_final_registration_names_rejected_before_mutation(world, monkeypatch, collision):
    oid = digest(b"owned-object")
    if collision == "object_alias":
        world.service.ingest(world.project, b"owned-object", media_type="text/plain", original_name="owned.txt", idempotency_key="collision-owned")
    setup(world, derived=True, root_object_ids=[oid] if collision == "object_alias" else [],
          input_ports=[{"name": name, "type": "file", "required": True} for name in ("value", "other")])
    b = bridge(world)
    source = "nested/same.txt"
    alias = _expected_registration_filename(source)
    path = b.output_root / source
    path.parent.mkdir()
    path.write_bytes(b"first-bytes")
    first = {"filename": source, "output_port": "source", "media_type": "text/plain"}
    if collision == "object_alias":
        second = {"object_id": oid, "digest": oid, "filename": alias}
        second_binding = {"name": "other", "kind": "object", "object_id": oid}
    else:
        second = {**first, "filename": source if collision == "same_source" else alias}
        if collision == "flat_alias":
            (b.output_root / alias).write_bytes(b"second-bytes")
        second_binding = {"name": "other", "kind": "producer_file", **second}
    captured = _capture_registration(world, monkeypatch)
    body = request(world, value=first, descriptors=[{"name": "value", "kind": "producer_file", **first}, second_binding])
    body["inputs"]["other"] = second
    with pytest.raises(ChildBridgeError, match="registration filenames collide"):
        b.dispatch(body)
    assert not captured.uploads and not captured.admissions and not captured.receipts
    assert not b._children
    assert not world.service.store.delegated_children(world.task["id"], world.claim["attempt_id"])
    assert "derived_input_registry" not in world.service.task(world.task["id"])["task"]["spec"]
    assert path.read_bytes() == b"first-bytes"


@pytest.mark.parametrize("filenames", [None, ("answer.tar.gz", "answer.tar.gz")])
def test_materialization_exact_wire_and_sdk_producer_registration(world, monkeypatch, filenames):
    from astrid.sdk.results import MaterializedChildOutput
    b, child, rows = _materialization_fixture(world, (b"chain-bytes", b"chain-bytes"), filenames=filenames)
    sdk = ChildBridge(b.child_channel)
    sdk._sequence = b._sequence
    # SDK sends only child key, task and association; no bytes/path/credential.
    first = sdk.materialize_output(child_key="child", task_id=child["task_id"], output=rows[0])
    assert isinstance(first, MaterializedChildOutput)
    assert dict(first.output) == rows[0]
    assert first.filename == "child-outputs/" + rows[0]["association_id"] + "/" + rows[0]["filename"]
    assert (b.output_root / first.filename).read_bytes() == b"chain-bytes"
    assert set(first.to_dict()) == {"output", "filename"}
    assert SECRET not in json.dumps(first.to_dict())
    assert first.producer_file() == {"filename": first.filename, "media_type": "text/plain", "output_port": "answer0"}
    before = _retained_usage(b)
    repeated = sdk.materialize_output(child_key="child", task_id=child["task_id"], output=rows[0])
    assert repeated == first and _retained_usage(b) == before
    def submit(key, producer):
        body = request(world, key=key, value=producer, descriptors=[{"name": "value", "kind": "producer_file", **producer}])
        return sdk._exchange("submit", {k: v for k, v in body.items() if k not in {"v", "request_id", "op"}})
    consumed = submit("consume-1", first.producer_file())
    registry = world.service.task(world.task["id"])["task"]["spec"]["derived_input_registry"]
    assert len(registry) == 2  # Initial producer plus fresh downstream snapshot.
    assert registry[digest(b"chain-bytes")]["filename"] == _expected_registration_filename(first.filename)
    uploads = len([call for call in world.calls if call[0] == "POST" and call[1] == "/v1/objects"])
    assert submit("consume-1", repeated.producer_file()) == consumed
    assert len([call for call in world.calls if call[0] == "POST" and call[1] == "/v1/objects"]) == uploads
    submit("consume-2", first.producer_file())
    assert len([call for call in world.calls if call[0] == "POST" and call[1] == "/v1/objects"]) == uploads + 1
    assert world.service.task(world.task["id"])["task"]["spec"]["derived_input_registry"] == registry
    assert _retained_usage(b) == before
    second = sdk.materialize_output(child_key="child-1", task_id=rows[1]["task_id"], output=rows[1])
    assert _retained_usage(b) == (2, 2 * len(b"chain-bytes"))
    with pytest.raises(_BridgeRejected):
        submit("consume-conflict", second.producer_file())
    assert world.service.task(world.task["id"])["task"]["spec"]["derived_input_registry"] == registry
    assert _retained_usage(b) == (2, 2 * len(b"chain-bytes"))
    (b.output_root / second.filename).unlink()
    assert _retained_usage(b) == (2, 2 * len(b"chain-bytes"))
    sdk.close()


def test_materialization_public_sdk_same_leaf_outputs_consumed_together_without_hc04(world, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    code = (
        "from pathlib import Path\n"
        "import hashlib, json\n"
        "from astrid.core.execution.executor import registry as er\n"
        "from astrid.core.execution.orchestrator import registry as ore\n"
        "from astrid.core.pack import discovery as pd\n"
        "for module in (er, ore, pd): module.discover_packs = lambda *a, **kw: ()\n"
        "from astrid import invoke\n"
        "Path('outputs').mkdir(exist_ok=True)\n"
        "sources = []\n"
        "for index, text in enumerate(('left-bytes', 'right-bytes')):\n"
        "    filename = 'initial-' + str(index) + '.txt'\n"
        "    Path('outputs', filename).write_text('seed-' + text)\n"
        "    producer = {'filename': filename, 'output_port': 'source', 'media_type': 'text/plain'}\n"
        f"    child = invoke({CHILD!r}, kind='action', extra_pack_roots=({str(world.root)!r},), inputs={{'value': producer}}, child_key='produce-' + str(index), wait=True, timeout_seconds=10, poll_seconds=0.01)\n"
        "    assert child.ok, child.error\n"
        "    row = child.outputs['managed_outputs'][0]\n"
        "    local = child.materialize_output(row['association_id'])\n"
        "    assert local.output['filename'] == 'answer.txt'\n"
        "    assert local.filename == 'child-outputs/' + row['association_id'] + '/answer.txt'\n"
        "    assert Path('outputs', local.filename).read_text() == text\n"
        "    assert child.materialize_output(row['association_id']) == local\n"
        "    assert local.producer_file()['filename'] == local.filename\n"
        "    sources.append(local)\n"
        "assert sources[0].filename != sources[1].filename\n"
        f"r = invoke({CHILD!r}, kind='action', extra_pack_roots=({str(world.root)!r},), inputs={{'value': sources[0].producer_file(), 'other': sources[1].producer_file()}}, child_key='consume-together', wait=True, timeout_seconds=10, poll_seconds=0.01)\n"
        "assert r.ok, r.error\n"
        "result = r.materialize_output(r.outputs['managed_outputs'][0]['association_id'])\n"
        "observed = json.loads(Path('outputs', result.filename).read_text())\n"
        "assert observed['left'] == 'left-bytes' and observed['right'] == 'right-bytes'\n"
        "expected = ['producer-' + hashlib.sha256(item.filename.encode('utf-8')).hexdigest() + '.txt' for item in sources]\n"
        "assert [observed['left_filename'], observed['right_filename']] == expected\n"
        "assert all(Path('outputs', item.filename).read_text() == text for item, text in zip(sources, ('left-bytes', 'right-bytes')))\n"
    )
    child_code = (
        "def run(value, other=None):\n"
        "    from pathlib import Path\n"
        "    import json\n"
        "    left = Path(value)\n"
        "    text = left.read_text()\n"
        "    if other is None: text = text.removeprefix('seed-')\n"
        "    if other is not None:\n"
        "        right = Path(other)\n"
        "        assert left != right\n"
        "        text = json.dumps({'left': text, 'right': right.read_text(), 'left_filename': left.name, 'right_filename': right.name}, sort_keys=True)\n"
        "    Path('outputs').mkdir(exist_ok=True)\n"
        "    Path('outputs/answer.txt').write_text(text)\n"
        "    return {'answer': text}\n"
    )
    setup(world, derived=True, child_file_output=True, code=code, child_code_override=child_code,
          input_ports=[{"name": "value", "type": "file", "required": True}, {"name": "other", "type": "file", "required": False}],
          child_limits={"max_children": 3})
    assert "hc04_param_ports" not in world.host.capabilities[CHILD].definition.metadata
    captured = _capture_registration(world, monkeypatch)

    def serve_three():
        for _ in range(3):
            serve_one(world)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(serve_three)
        c = world.claim
        try:
            world.host.run_task({"task": world.task}, lease_token=c["lease_id"], attempt_id=c["attempt_id"], fence=c["fence"])
        except Exception:
            try:
                future.result(timeout=15)
            except Exception:
                pass
            raise
        future.result(timeout=15)
    assert world.service.task(c["task_id"])["task"]["status"] == "completed"
    admission = captured.admissions[-1]
    assert admission["child"]["child_id"] == _child_wire_id(project_id=world.project,
        parent_task_id=c["task_id"], parent_attempt_id=c["attempt_id"], logical_child_key="consume-together")
    refs = admission["derived_inputs"]
    assert admission["ordered_inputs"] == [
        {**ref, "required": ref["name"] == "value"} for ref in refs
    ]
    assert set(admission["inputs"]) == {"value", "other"}
    assert [ref["name"] for ref in refs] == ["value", "other"]
    assert len({ref["filename"] for ref in refs}) == 2
    registry = world.service.task(c["task_id"])["task"]["spec"]["derived_input_registry"]
    receipt_refs = captured.receipts[-1]["derived_inputs"]
    delegated = [call[2]["task"] for call in world.calls if call[1] == "/v1/delegated-tasks"][-1]
    for ref, receipt_ref, (data, upload) in zip(refs, receipt_refs, captured.uploads[-2:]):
        local = admission["inputs"][ref["name"]]["filename"]
        assert local.startswith("child-outputs/") and local.endswith("/answer.txt")
        assert ref["filename"] == _expected_registration_filename(local)
        assert upload == ref and digest(data) == ref["object_id"]
        assert all(receipt_ref[k] == v for k, v in ref.items())
        assert registry[ref["object_id"]]["filename"] == ref["filename"]
        assert delegated["spec"]["inputs"][ref["name"]]["filename"] == ref["filename"]
    assert [ref["filename"] for ref in delegated["execution_request"]["inputs"]] == [ref["filename"] for ref in refs]


@pytest.mark.parametrize("tamper", ["key", "task", "association", "unexposed", "failed", "unsettled", "child_attempt",
                                    "child_run", "lineage", "descriptor", "missing", "bytes", "size", "association_race", "revoked"])
def test_materialization_authority_and_exact_readback_fail_closed(world, monkeypatch, tamper):
    b, child, rows = _materialization_fixture(world, expose=tamper != "unexposed")
    changes = {}
    if tamper == "key": changes["child_key"] = "foreign"
    elif tamper == "task": changes["task_id"] = world.task["id"]
    elif tamper == "association": changes["association_id"] = "foreign"
    elif tamper == "revoked": b.revoke()
    elif tamper in {"failed", "unsettled", "child_attempt", "child_run", "lineage"}:
        original = world.client.task
        def task(tid):
            result = copy.deepcopy(original(tid))
            if tid == child["task_id"]:
                result = resource(result)
                value = result.get("task", result)
                if tamper in {"failed", "unsettled"}: value["state"] = value["status"] = "failed" if tamper == "failed" else "running"
                elif tamper == "child_attempt": value["attempt_id"] = "foreign"
                elif tamper == "child_run": value["run_id"] = "foreign"
                else: value["spec"]["delegated_parent"]["parent_attempt_id"] = "foreign"
            return result
        monkeypatch.setattr(world.client, "task", task)
    elif tamper == "bytes": monkeypatch.setattr(world.client, "get_object", lambda _: b"bad-bytes")
    elif tamper == "missing": monkeypatch.setattr(world.client, "child_outputs", lambda _: [])
    elif tamper == "association_race":
        original = world.client.child_output
        count = [0]
        def changed(aid):
            row = resource(original(aid)); count[0] += 1
            if count[0] > 1: row["filename"] = "changed.txt"
            return row
        monkeypatch.setattr(world.client, "child_output", changed)
    elif tamper in {"descriptor", "size"}:
        original_list, original_get = world.client.child_outputs, world.client.child_output
        def corrupt(raw):
            row = resource(raw)
            row["ordinal" if tamper == "descriptor" else "size"] += 1
            return row
        monkeypatch.setattr(world.client, "child_outputs", lambda tid: [corrupt(row) for row in original_list(tid)])
        monkeypatch.setattr(world.client, "child_output", lambda aid: corrupt(original_get(aid)))
    with pytest.raises(Exception): _materialize(b, child, rows[0], **changes)
    assert not (b.output_root / "child-outputs").exists()
    assert _retained_usage(b) == (0, 0)


@pytest.mark.parametrize("extra", ["filename", "descriptor", "context", "credential", "data_base64"])
def test_materialization_rejects_caller_selected_authority_or_destination(world, extra):
    b, child, rows = _materialization_fixture(world)
    with pytest.raises(ChildBridgeError): _materialize(b, child, rows[0], **{extra: "caller"})
    assert b._revoked.is_set()
    assert not (b.output_root / "child-outputs").exists()


@pytest.mark.parametrize("data,counts,bytes_limit", [((b"abc", b"d"), 2, 3), ((b"abc", b"abc"), 1, 6),
                                                   ((b"", b""), 1, 1), ((b"abc", b"abc", b"d"), 3, 6)])
def test_materialization_exact_aggregate_and_count_limits(world, data, counts, bytes_limit, monkeypatch):
    b, child, rows = _materialization_fixture(world, data)
    b.policy["limits"].update(max_derived_objects=counts, max_derived_bytes=bytes_limit)
    accepted = rows[:-1]
    for row in accepted: _materialize(b, child, row)
    assert _retained_usage(b) == (len(accepted), sum(row["size"] for row in accepted))
    last = rows[-1]
    with pytest.raises(ChildBridgeError, match="retention"): _materialize(b, child, last)
    assert not (b.output_root / "child-outputs" / last["association_id"]).exists()
    original_write = b._write_materialized_file
    monkeypatch.setattr(b, "_write_materialized_file", lambda *args: pytest.fail("repeat must not copy"))
    for row in accepted:
        result = _materialize(b, child, row)
        assert result["output"] == row
    assert _retained_usage(b) == (len(accepted), sum(row["size"] for row in accepted))
    monkeypatch.setattr(b, "_write_materialized_file", original_write)


@pytest.mark.parametrize("mutation", ["replace", "corrupt", "symlink", "hardlink", "delete"])
def test_materialization_repeat_reauthenticates_and_rejects_replaced_files(world, mutation):
    b, child, rows = _materialization_fixture(world)
    result = _materialize(b, child, rows[0])
    path = b.output_root / result["filename"]
    if mutation == "corrupt": path.write_bytes(b"corrupt")
    else:
        path.unlink()
        outside = world.tmp / "replacement.txt"; outside.write_bytes(b"child-bytes")
        if mutation == "replace": path.write_bytes(b"child-bytes")
        elif mutation == "symlink": path.symlink_to(outside)
        elif mutation == "hardlink": os.link(outside, path)
    with pytest.raises(Exception): _materialize(b, child, rows[0])
    assert _retained_usage(b) == (1, len(b"child-bytes"))


def test_materialization_repeat_authenticates_source_even_at_capacity(world, monkeypatch):
    b, child, rows = _materialization_fixture(world)
    b.policy["limits"].update(max_derived_objects=1, max_derived_bytes=rows[0]["size"])
    first = _materialize(b, child, rows[0])
    calls = len(world.calls)
    assert _materialize(b, child, rows[0]) == first
    assert any(path.startswith("/v1/managed-outputs/") for _, path, _ in world.calls[calls:])
    monkeypatch.setattr(world.client, "get_object", lambda _: b"corrupt")
    with pytest.raises(ChildBridgeError): _materialize(b, child, rows[0])
    assert _retained_usage(b) == (1, rows[0]["size"])


@pytest.mark.parametrize("uncertain", [False, True])
def test_materialization_failed_write_cleanup_controls_reservation_release(world, monkeypatch, uncertain):
    b, child, rows = _materialization_fixture(world, (b"one", b"two"))
    from astrid.core.execution import _child_bridge as module
    original_unlink, original_write = module.os.unlink, module.os.write
    monkeypatch.setattr(module.os, "write", lambda *args: (_ for _ in ()).throw(OSError("injected write failure")))
    if uncertain:
        monkeypatch.setattr(module.os, "unlink", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected cleanup failure")))
    with pytest.raises(OSError): _materialize(b, child, rows[0])
    monkeypatch.setattr(module.os, "write", original_write)
    monkeypatch.setattr(module.os, "unlink", original_unlink)
    files = [path for path in (b.output_root / "child-outputs").rglob("*") if path.is_file()]
    if uncertain:
        assert len(files) == 1 and files[0].name.startswith(".materialize-")
        assert _retained_usage(b) == (1, 3) and b._materialization_blocked
        with pytest.raises(ChildBridgeError, match="cleanup"): _materialize(b, child, rows[1])
    else:
        assert files == [] and _retained_usage(b) == (0, 0)
        assert _materialize(b, child, rows[0])["output"] == rows[0]


def test_materialization_pending_writes_consume_capacity(world, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    b, child, rows = _materialization_fixture(world, (b"one", b"two"))
    b.policy["limits"].update(max_derived_objects=1, max_derived_bytes=3)
    original = b._write_materialized_file
    ready, release = threading.Event(), threading.Event()
    def blocked(*args):
        ready.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(b, "_write_materialized_file", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_materialize, b, child, rows[0])
        try:
            assert ready.wait(5)
            assert _retained_usage(b) == (1, 3)
            with pytest.raises(ChildBridgeError, match="retention"): _materialize(b, child, rows[1])
            with pytest.raises(ChildBridgeError, match="pending"): _materialize(b, child, rows[0])
            assert not (b.output_root / "child-outputs" / rows[1]["association_id"]).exists()
        finally:
            release.set()
        assert future.result(timeout=5)["output"] == rows[0]


@pytest.mark.parametrize("component", ["attempt", "output", "namespace", "association", "source"])
def test_materialization_directory_replacement_and_symlinks_fail_closed(world, component):
    b, child, rows = _materialization_fixture(world)
    outside = world.tmp / "outside"; outside.mkdir()
    if component == "attempt":
        b.output_root.parent.rename(world.tmp / "old-attempt")
        b.output_root.parent.mkdir(); b.output_root.mkdir()
    elif component == "output":
        b.output_root.rename(b.output_root.parent / "old-output"); b.output_root.mkdir()
    else:
        target = b.output_root / "child-outputs"
        if component != "namespace":
            target.mkdir(); target = target / rows[0]["association_id"]
        if component == "source":
            target.mkdir(); target = target / rows[0]["filename"]
        target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(Exception): _materialize(b, child, rows[0])
    assert list(outside.iterdir()) == []
    assert _retained_usage(b) == (0, 0)


def _materialization_readback_override(world, monkeypatch, child, row, **changes):
    """Keep settlement/association consistent to reach exact byte/path checks."""
    original_task = world.client.task
    replacement = {**row, **changes}
    def task(tid):
        result = resource(original_task(tid))
        if tid == child["task_id"]:
            result = copy.deepcopy(result)
            value = result.get("task", result)
            expected = value["result"]["outputs"][0]
            for key in ("digest", "size", "filename", "ordinal", "media_type", "output_port"):
                if key in changes: expected[key] = changes[key]
        return result
    monkeypatch.setattr(world.client, "task", task)
    monkeypatch.setattr(world.client, "child_outputs", lambda tid: [dict(replacement)])
    monkeypatch.setattr(world.client, "child_output", lambda aid: dict(replacement))
    return replacement


@pytest.mark.parametrize("size", [64 * 1024 * 1024, 64 * 1024 * 1024 + 1])
def test_materialization_exact_per_object_boundary(world, monkeypatch, size):
    b, child, rows = _materialization_fixture(world, expose=False)
    # Actual bounded CAS readback through the unchanged host route. The
    # 64-MiB-plus-one descriptor must reject before requesting CAS bytes.
    payload = b"x" * min(size, 64 * 1024 * 1024)
    oid = digest(payload)
    _materialization_readback_override(world, monkeypatch, child, rows[0], size=size, digest=oid, object_id=oid)
    reads = []
    monkeypatch.setattr(world.client, "get_object", lambda oid: reads.append(oid) or payload)
    b.policy["limits"].update(max_derived_bytes=64 * 1024 * 1024)
    frame = {"v": 1, "request_id": b._sequence + 1, "op": "outputs", "child_key": "child", "task_id": child["task_id"]}
    if size > 64 * 1024 * 1024:
        with pytest.raises(ChildBridgeError, match="byte identity"): b.dispatch(frame)
        assert reads == [] and _retained_usage(b) == (0, 0)
        assert not (b.output_root / "child-outputs").exists()
    else:
        public = b.dispatch(frame)[0]
        result = _materialize(b, child, public)
        assert (b.output_root / result["filename"]).stat().st_size == size
        assert _retained_usage(b) == (1, size)
        assert _materialize(b, child, public) == result


@pytest.mark.parametrize("tamper", ["actual_size", "stable_descriptor"])
def test_materialization_consistent_settlement_still_requires_original_descriptor_and_size(world, monkeypatch, tamper):
    b, child, rows = _materialization_fixture(world)
    full_row = resource(world.client.child_output(rows[0]["association_id"]))
    changes = {"size": full_row["size"] + 1} if tamper == "actual_size" else {"ordinal": full_row["ordinal"] + 1}
    _materialization_readback_override(world, monkeypatch, child, full_row, **changes)
    with pytest.raises(ChildBridgeError, match="bytes failed|descriptor changed"):
        _materialize(b, child, rows[0])
    assert _retained_usage(b) == (0, 0)
    assert not (b.output_root / "child-outputs").exists()


@pytest.mark.parametrize("filename", ["../escape.txt", "/absolute.txt", "nested/../../escape.txt", "back\\slash", "bad\nname", "bad\x7fname", "."])
def test_materialization_rejects_unconfined_original_descriptor_filename(world, monkeypatch, filename):
    b, child, rows = _materialization_fixture(world, expose=False)
    _materialization_readback_override(world, monkeypatch, child, rows[0], filename=filename)
    with pytest.raises(ValueError):
        b.dispatch({"v": 1, "request_id": b._sequence + 1, "op": "outputs", "child_key": "child", "task_id": child["task_id"]})
    assert _retained_usage(b) == (0, 0)
    assert not (b.output_root / "child-outputs").exists()


@pytest.mark.parametrize("phase", ["post_link", "directory_race", "parent_revoked"])
def test_materialization_publication_failure_cleans_before_release(world, monkeypatch, phase):
    from astrid.core.execution import _child_bridge as module
    b, child, rows = _materialization_fixture(world)
    original_link = module.os.link
    def linked(*args, **kwargs):
        result = original_link(*args, **kwargs)
        if phase == "directory_race":
            namespace = b.output_root / "child-outputs"
            namespace.rename(b.output_root / "replaced-namespace")
            namespace.mkdir()
        elif phase == "parent_revoked": b.revoke()
        return result
    monkeypatch.setattr(module.os, "link", linked)
    if phase == "post_link":
        original_fsync = module.os.fsync
        def fsync(fd):
            import stat
            if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError("post-publication fsync failed")
            return original_fsync(fd)
        monkeypatch.setattr(module.os, "fsync", fsync)
    with pytest.raises(Exception): _materialize(b, child, rows[0])
    assert _retained_usage(b) == (0, 0)
    assert not [path for path in b.output_root.rglob("*") if path.is_file() and path.name != "source.txt"]


def _f05_runtime_review_pair(world, *, timeout=0, runtime_limit=None, before_child=None, with_source=False, training=False, media_payloads=None):
    """Admit exact review identities through the ordinary offline Runtime ABI."""
    from astrid.core.execution._child_bridge import task_resource
    parent_pack, parent_action = ("training", "dataset_build") if training else ("iteration", "experiment_review_session")
    parent_id, child_id = parent_pack + "." + parent_action, "editorial.human_review"
    for pack_id, action, inputs in (
        (parent_pack, parent_action, ([{"name": "config", "type": "path", "required": True},
            {"name": "out", "type": "path", "required": True}] if training else [{"name": "runs_dir", "type": "directory", "required": True},
            {"name": "timeout", "type": "integer", "required": False, "default": 0},
            {"name": "skip_server", "type": "boolean", "required": False, "default": False}])),
        ("editorial", "human_review", [{"name": "timeout", "type": "integer", "required": False, "default": 0},
            *([{"name": "source", "type": "file", "required": False}] if with_source else []),
            *([{"name": name, "type": "file", "required": name == "data"}
               for name in ("data", "state", "assets_bundle", "state_schema_bundle")] if media_payloads is not None else [])]),
    ):
        root = world.root / pack_id
        root.mkdir()
        (root / "pack.yaml").write_text(yaml.safe_dump({"schema_version": 3, "id": pack_id, "name": pack_id,
            "version": "1.0.0", "actions": {action: {"description": "F05 lifetime fixture", "inputs": inputs,
                "outputs": [], "invocation": {"kind": "command", "command": {"argv": ["{python_exec}", "-c", "pass"]}}}}}))
    host = GenericPackHost(pack_roots=[world.root / parent_pack, world.root / "editorial"],
                           client=world.client, executor_id="worker", max_concurrency=2)
    host.discover()
    for record in host.capabilities.values():
        world.service.register_capability({"capability_id": record.id, "definition_digest": record.capability_digest})
    for executor in ("worker", "child-worker"):
        world.service.register_executor({"executor_id": executor, "capabilities": [parent_id, child_id], "max_concurrency": 2},
                                        idempotency_key="f05-register-" + executor)
    child_digest = host.capabilities[child_id].capability_digest
    policy = {"capabilities": [{"capability_id": child_id, "capability_digest": child_digest}],
              "targets": [{"kind": "default"}], "input_object_ids": [],
              "recoverable_outputs": [{"capability_id": child_id, "capability_digest": child_digest,
                                       "output_ports": ["state_result"]}], "limits": {"max_children": 2}}
    target = world.identity["execution_binding"]["actual"]
    runs = world.tmp / "runs"; runs.mkdir()
    world.service.create_task({"project": world.project, "capability_id": parent_id,
        "capability_digest": host.capabilities[parent_id].capability_digest, "input_object_ids": [],
        "spec": {"inputs": {"config": str(runs / "config.json"), "out": str(runs)} if training else {"runs_dir": str(runs)}}, "child_delegation": policy,
        "execution_request": {"schema_version": 1, "target": target}, "idempotency_key": "f05-parent"}, enforce_readiness=True)
    parent_claim = world.service.claim_next({"executor_id": "worker", "capability_ids": [parent_id], "runtime_epoch": 1,
                                           "target": target}, idempotency_key="f05-claim-parent", identity=world.identity)
    world.client._attempt_runtime_epochs[parent_claim["attempt_id"]] = 1
    parent = task_resource(world.client.task(parent_claim["task_id"]))
    review_wire_id = _child_wire_id(project_id=world.project, parent_task_id=parent_claim["task_id"],
                                   parent_attempt_id=parent_claim["attempt_id"], logical_child_key="f05-review")
    if before_child is not None:
        before_child(host, parent, parent_claim)
    if media_payloads is not None:
        output = world.tmp / "review-attempt" / "outputs"
        output.mkdir(parents=True)
        b = HostChildBridge(host, parent, attempt_id=parent_claim["attempt_id"], lease_id=parent_claim["lease_id"],
            fence=parent_claim["fence"], runtime_epoch=1, output_root=output, cancelled=lambda: False)
        world.bridges.append(b)
        world.media_bridge = b
        review_inputs, bindings = {"timeout": timeout}, []
        for name in ("data", "state", "assets_bundle", "state_schema_bundle"):
            payload = media_payloads[name]
            media_type = "application/zip" if name.endswith("bundle") else "application/json"
            filename = "review-inputs/" + name + (".zip" if name.endswith("bundle") else ".json")
            path = output / filename
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(payload)
            review_inputs[name] = {"filename": filename, "output_port": name, "media_type": media_type}
            bindings.append({"name": name, "kind": "producer_file", **review_inputs[name]})
        admitted = b.dispatch({"v": 1, "request_id": 1, "op": "submit", "child_key": "f05-review",
            "child": {"capability_id": child_id, "capability_digest": child_digest},
            "inputs": review_inputs, "input_descriptors": bindings, "wait": True,
            "timeout_seconds": 0.5, "poll_seconds": 0.01})
    elif runtime_limit is None:
        admitted = resource(world.client.admit_child(child={"child_id": review_wire_id, "capability_id": child_id,
            "capability_digest": child_digest}, inputs={"timeout": timeout}, ordered_inputs=[], derived_inputs=[],
            task_id=parent_claim["task_id"], attempt_id=parent_claim["attempt_id"], lease_id=parent_claim["lease_id"],
            fence=parent_claim["fence"], runtime_epoch=1, run_id=parent_claim["run_id"], project_id=world.project))
    else:
        # Admission fixture only: carry an explicit child absolute cap through
        # the existing public authority/admission operations.
        receipt = world.client.generated.issue_child_authority(parent_claim["attempt_id"],
            lease_id=parent_claim["lease_id"], fence=parent_claim["fence"], runtime_epoch=1,
            child={"child_id": review_wire_id, "capability_id": child_id, "capability_digest": child_digest}, derived_inputs=[])
        admitted = resource(world.client.generated.admit_delegated_task(authority=receipt["authority"],
            task={"capability_id": child_id, "capability_digest": child_digest, "input_object_ids": [],
                  "spec": {"capability_id": child_id, "kind": "action", "inputs": {"timeout": timeout}},
                  "execution_request": {"schema_version": 1, "target": {"kind": "default"},
                                        "limits": {"max_runtime_seconds": runtime_limit}}}, idempotency_key=review_wire_id))
    # The child has its own worker actor on the same verified machine.
    world.identity["actor"] = "child-worker"
    child_claim = world.service.claim_next({"executor_id": "child-worker", "capability_ids": [child_id], "runtime_epoch": 1,
                                          "target": target}, idempotency_key="f05-claim-child", identity=world.identity)
    assert child_claim["task_id"] == admitted.get("task_id", admitted.get("task", {}).get("task_id"))
    world.client._attempt_runtime_epochs[child_claim["attempt_id"]] = 1
    world.client.executor_id = "child-worker"
    child = task_resource(world.client.task(child_claim["task_id"]))
    return host, parent, parent_claim, child, child_claim


def _f05_authority(host, task, claim):
    from astrid.core.execution.generic_host import _interactive_review_authority
    return _interactive_review_authority(host, task, host.capabilities[task["capability_id"]],
        attempt_id=claim["attempt_id"], lease_id=claim["lease_id"], fence=claim["fence"], runtime_epoch=claim["runtime_epoch"])


def test_f05_bridge_snapshot_publishes_only_verified_immutable_admissions(world, monkeypatch):
    from dataclasses import FrozenInstanceError
    setup(world)
    b = bridge(world)
    assert b.admitted_children_snapshot().children == ()
    original_admit = world.client.admit_child
    def admit(**kwargs):
        assert b._admission_lock.acquire(blocking=False)
        b._admission_lock.release()
        assert b.admitted_children_snapshot().children == ()
        return original_admit(**kwargs)
    monkeypatch.setattr(world.client, "admit_child", admit)
    child = b.dispatch(request(world))
    snapshot = b.admitted_children_snapshot()
    assert len(snapshot.children) == 1
    admitted = snapshot.children[0]
    assert admitted.child_key == "child"
    assert json.loads(admitted.task_json)["task_id"] == child["task_id"]
    assert json.loads(admitted.inputs_json) == {"value": "scalar"}
    assert json.loads(admitted.descriptors_json) == []
    assert json.loads(admitted.parent_context_json) == b.context
    with pytest.raises(FrozenInstanceError): admitted.child_key = "tampered"
    with pytest.raises(FrozenInstanceError): snapshot.closed = True
    b.close_channel()
    assert not snapshot.closed
    assert b.admitted_children_snapshot().closed
    b.revoke()
    assert b.admitted_children_snapshot().revoked


def test_f05_bridge_rejected_lineage_never_enters_snapshot(world, monkeypatch):
    setup(world)
    b = bridge(world)
    def reject(task): raise ChildBridgeError("foreign child")
    monkeypatch.setattr(b, "_assert_lineage", reject)
    with pytest.raises(ChildBridgeError): b.dispatch(request(world))
    assert b.admitted_children_snapshot().children == ()


def _f05_training_accounting_fixture(world, monkeypatch, *, media_verified):
    from astrid.core.execution import generic_host
    from astrid.core.execution._child_bridge import AdmittedChildSnapshot, AdmittedChildrenSnapshot
    host, parent, pc, child, cc = _f05_runtime_review_pair(world, training=True)
    context = {"task_id": parent["task_id"], "run_id": parent["run_id"], "project_id": parent["project_id"],
               "attempt_id": pc["attempt_id"], "lease_id": pc["lease_id"], "fence": pc["fence"], "runtime_epoch": 1}
    admission = AdmittedChildSnapshot("f05-review", "fixed-admission", json.dumps(child),
                                      json.dumps({"timeout": 0}), "[]", json.dumps(context))
    snapshot = [AdmittedChildrenSnapshot((admission,), False, False)]
    bridge_view = SimpleNamespace(admitted_children_snapshot=lambda: snapshot[0])
    # Isolate the authority/accounting mechanism from the intentionally missing
    # production M17 media evidence. Never upgrade these test tokens in production.
    if media_verified:
        monkeypatch.setattr(generic_host, "_training_review_media_scope", lambda *args: ("verified-test-custody",))
    accounting = generic_host._training_review_authority(host, parent, host.capabilities["training.dataset_build"],
        bridge_view, attempt_id=pc["attempt_id"], lease_id=pc["lease_id"], fence=pc["fence"], runtime_epoch=1)
    assert accounting is not None
    values = {parent["task_id"]: copy.deepcopy(parent), child["task_id"]: copy.deepcopy(child)}
    monkeypatch.setattr(host.client, "task", lambda tid: copy.deepcopy(values[tid]))
    return host, parent, child, accounting, values, snapshot


def test_f05_training_current_admission_without_media_custody_gets_no_credit(world, monkeypatch):
    _, _, _, accounting, _, _ = _f05_training_accounting_fixture(world, monkeypatch, media_verified=False)
    assert accounting.verify() == frozenset()
    assert accounting.credited_seconds == 0


@pytest.mark.parametrize("tamper", ["logical-runtime-key", "foreign-runtime-key", "snapshot-wire-key", "snapshot-logical-key"])
def test_f05_training_child_wire_idempotency_rejects_changed_admission(world, monkeypatch, tamper):
    from dataclasses import replace
    _, parent, child, accounting, values, snapshots = _f05_training_accounting_fixture(
        world, monkeypatch, media_verified=True)
    admission = snapshots[0].children[0]
    wire_id = _child_wire_id(project_id=parent["project_id"], parent_task_id=parent["task_id"],
                             parent_attempt_id=values[parent["task_id"]]["attempt_id"],
                             logical_child_key=admission.child_key)
    assert child["idempotency_key"] == wire_id
    assert accounting.verify()
    if tamper in {"logical-runtime-key", "foreign-runtime-key"}:
        values[child["task_id"]]["idempotency_key"] = admission.child_key if tamper == "logical-runtime-key" else "foreign"
    elif tamper == "snapshot-wire-key":
        original = json.loads(admission.task_json)
        original["idempotency_key"] = admission.child_key
        admission = replace(admission, task_json=json.dumps(original))
    else:
        admission = replace(admission, child_key="foreign-logical-key")
    snapshots[0] = replace(snapshots[0], children=(admission,))
    with pytest.raises(HostError, match="Training child admission identity changed"):
        accounting.verify()


@pytest.mark.parametrize("timeout", [False, 0.0, "0", 12, None])
def test_f05_training_timeout_must_be_exact_integer_zero(world, monkeypatch, timeout):
    _, _, child, accounting, values, _ = _f05_training_accounting_fixture(world, monkeypatch, media_verified=True)
    inputs = values[child["task_id"]]["spec"]["spec"]["inputs"]
    if timeout is None: inputs.pop("timeout")
    else: inputs["timeout"] = timeout
    assert accounting.verify() == frozenset()


@pytest.mark.parametrize("loss", ["fence", "epoch", "expiry", "placement", "delegation", "bridge", "readback"])
def test_f05_training_qualified_claim_authority_loss_cancels(world, monkeypatch, loss):
    host, parent, child, accounting, values, snapshot = _f05_training_accounting_fixture(world, monkeypatch, media_verified=True)
    assert accounting.verify()
    value = values[child["task_id"]]
    if loss == "fence": value["lease_fence"] += 1
    elif loss == "epoch": value["runtime_epoch"] += 1
    elif loss == "expiry": value["lease_expires_at"] = "2000-01-01T00:00:00Z"
    elif loss == "placement": value["execution_binding"]["actual_target"]["id"] = "foreign"
    elif loss == "delegation": values[parent["task_id"]]["spec"]["delegation_closed_attempt_id"] = parent["attempt_id"]
    elif loss == "bridge":
        from dataclasses import replace
        snapshot[0] = replace(snapshot[0], revoked=True)
    else:
        monkeypatch.setattr(host.client, "task", lambda tid: (_ for _ in ()).throw(OSError("readback failed")))
    with pytest.raises(Exception): accounting.verify()


def test_f05_training_completion_charges_tail_and_retains_original_allowance(world, monkeypatch):
    from astrid.core.execution import generic_host
    _, _, child, accounting, values, _ = _f05_training_accounting_fixture(world, monkeypatch, media_verified=True)
    now = [0.0]
    monkeypatch.setattr(generic_host.time, "monotonic", lambda: now[0])
    accounting.sample(executing=True)
    now[0] = 4000
    accounting.sample(executing=True)
    assert accounting.credited_seconds == 4000
    values[child["task_id"]]["state"] = "succeeded"
    now[0] = 4100
    accounting.sample(executing=True)
    assert accounting.credited_seconds == 4000
    assert accounting.previous_claims == frozenset()


def test_f05_exact_runtime_pair_survives_hour_with_different_executors(world, monkeypatch):
    from astrid.core.execution import generic_host
    from astrid.core.execution.guards import ExecutionGuardPolicy, ExecutionDeadlineError
    host, parent, pc, child, cc = _f05_runtime_review_pair(world)
    assert parent["execution_binding"]["executor_id"] != child["execution_binding"]["executor_id"]
    authorities = [_f05_authority(host, parent, pc), _f05_authority(host, child, cc)]
    assert all(callable(check) for check in authorities)
    before_calls = len(world.calls)
    now = [100.0]
    monkeypatch.setattr(generic_host.time, "monotonic", lambda: now[0])
    lifetimes = [generic_host._AttemptLifetime(ExecutionGuardPolicy(), now[0], None, check) for check in authorities]
    for lifetime in lifetimes: lifetime.begin_execution()
    for elapsed in (3601, 7201, 10801):
        now[0] = 100 + elapsed
        for lifetime in lifetimes:
            lifetime.assert_deadline()
            assert not lifetime.expired()
    # Repeated authority validation uses only public task/health GETs.
    assert all(method == "GET" for method, _, _ in world.calls[before_calls:])
    capped = generic_host._AttemptLifetime(ExecutionGuardPolicy(), 100, 7200, authorities[1], phase="execution")
    assert capped.expired()
    with pytest.raises(ExecutionDeadlineError): capped.assert_deadline()


@pytest.mark.parametrize("tamper", [
    "parent_capability", "parent_digest", "parent_timeout", "bool_timeout", "skip_server", "runs_dir",
    "policy_missing", "policy_limit", "policy_missing_limit", "policy_capability_digest", "policy_grant",
    "child_timeout", "child_capability", "child_digest", "grant_missing", "grant_limits", "grant_port",
    "lineage_missing", "lineage_shape", "lineage_attempt", "lineage_lease", "lineage_fence", "lineage_epoch",
    "lineage_policy", "lineage_machine", "lineage_target", "lineage_placement_version", "lineage_executor",
    "parent_expiry_missing", "parent_expiry_bad", "parent_expiry_stale", "parent_cancelled", "parent_epoch",
    "parent_fence", "parent_binding_missing", "parent_verification", "child_machine", "default_target", "readback",
])
def test_f05_incomplete_interactive_conjunction_stays_bounded(world, monkeypatch, tamper):
    from astrid.core.execution._child_bridge import task_resource
    host, parent, pc, child, cc = _f05_runtime_review_pair(world)
    parent, child = copy.deepcopy(parent), copy.deepcopy(child)
    pinputs = parent["spec"]["spec"]["inputs"]
    cinputs = child["spec"]["spec"]["inputs"]
    policy = parent["spec"]["child_delegation"]
    lineage = child["spec"]["delegated_parent"]
    grant = child["spec"]["delegated_recoverable_outputs"]
    if tamper == "parent_capability": parent["capability_id"] = "unrelated.parent"
    elif tamper == "parent_digest": parent["capability_digest"] = digest(b"foreign")
    elif tamper == "parent_timeout": pinputs["timeout"] = 12
    elif tamper == "bool_timeout": pinputs["timeout"] = False
    elif tamper == "skip_server": pinputs["skip_server"] = True
    elif tamper == "runs_dir": pinputs.pop("runs_dir")
    elif tamper == "policy_missing": parent["spec"].pop("child_delegation")
    elif tamper == "policy_limit": policy["limits"]["max_children"] = True
    elif tamper == "policy_missing_limit": policy["limits"].pop("max_snapshot_bytes")
    elif tamper == "policy_capability_digest": policy["capabilities"][0]["capability_digest"] = digest(b"foreign")
    elif tamper == "policy_grant": policy["recoverable_outputs"][0]["output_ports"] = ["other"]
    elif tamper == "child_timeout": cinputs["timeout"] = 5
    elif tamper == "child_capability": child["capability_id"] = "unrelated.child"
    elif tamper == "child_digest": child["capability_digest"] = digest(b"foreign")
    elif tamper == "grant_missing": child["spec"].pop("delegated_recoverable_outputs")
    elif tamper == "grant_limits": grant["limits"]["max_snapshot_bytes"] += 1
    elif tamper == "grant_port": grant["output_ports"] = ["other"]
    elif tamper == "lineage_missing": child["spec"].pop("delegated_parent")
    elif tamper == "lineage_shape": lineage["unrecognized"] = 1
    elif tamper.startswith("lineage_"):
        key, value = {
            "lineage_attempt": ("parent_attempt_id", "foreign"), "lineage_lease": ("parent_lease_id", "foreign"),
            "lineage_fence": ("parent_fence", 999), "lineage_epoch": ("runtime_epoch", 999),
            "lineage_policy": ("policy_digest", "foreign"), "lineage_machine": ("parent_placement", {}),
            "lineage_target": ("parent_effective_target", {"kind": "default"}),
            "lineage_placement_version": ("parent_placement_version", 999), "lineage_executor": ("executor_id", "foreign"),
        }[tamper]
        lineage[key] = value
    elif tamper == "parent_expiry_missing": parent.pop("lease_expires_at")
    elif tamper == "parent_expiry_bad": parent["lease_expires_at"] = "bad"
    elif tamper == "parent_expiry_stale": parent["lease_expires_at"] = "2000-01-01T00:00:00Z"
    elif tamper == "parent_cancelled": parent["state"] = "cancelled"
    elif tamper == "parent_epoch": parent["runtime_epoch"] = 999
    elif tamper == "parent_fence": parent["lease_fence"] += 1
    elif tamper == "parent_binding_missing": parent.pop("execution_binding")
    elif tamper == "parent_verification": parent["execution_binding"]["verification"]["verified"] = False
    elif tamper == "child_machine": child["execution_binding"]["actual_target"]["id"] = "foreign"
    elif tamper == "default_target": parent["execution_request"]["target"] = {"kind": "default"}
    originals = {parent["task_id"]: parent, child["task_id"]: child}
    def read(tid):
        if tamper == "readback": raise OSError("authority unavailable")
        return copy.deepcopy(originals[tid])
    monkeypatch.setattr(host.client, "task", read)
    # Keep the locally admitted child digest as the selected record even when
    # testing a foreign capability in the Runtime readback.
    from astrid.core.execution.generic_host import _interactive_review_authority
    check = _interactive_review_authority(host, child, host.capabilities["editorial.human_review"],
        attempt_id=cc["attempt_id"], lease_id=cc["lease_id"], fence=cc["fence"], runtime_epoch=1)
    assert check is None


@pytest.mark.parametrize("loss", ["cancel", "expiry", "fence", "epoch", "binding", "readback"])
def test_f05_qualified_child_loses_authority_immediately(world, monkeypatch, loss):
    from astrid.core.execution._child_bridge import task_resource
    host, parent, pc, child, cc = _f05_runtime_review_pair(world)
    check = _f05_authority(host, child, cc)
    assert callable(check)
    check()
    if loss == "cancel":
        world.service.cancel_task_canonical(pc["task_id"], {}, idempotency_key="f05-cancel-parent")
    elif loss == "expiry":
        world.service.store.conn.execute("UPDATE tasks SET lease_expires_at=? WHERE id=?", ("2000-01-01T00:00:00Z", pc["task_id"]))
    elif loss == "fence":
        world.service.store.conn.execute("UPDATE tasks SET lease_fence=lease_fence+1 WHERE id=?", (pc["task_id"],))
    elif loss == "epoch":
        monkeypatch.setattr(host.client, "health", lambda: {"runtime_epoch": 999})
    elif loss == "binding":
        original = host.client.task
        def read(tid):
            value = task_resource(original(tid))
            if tid == pc["task_id"]: value["execution_binding"]["status"] = "stale"
            return value
        monkeypatch.setattr(host.client, "task", read)
    else:
        monkeypatch.setattr(host.client, "task", lambda tid: (_ for _ in ()).throw(OSError("readback lost")))
    with pytest.raises(Exception): check()


@pytest.mark.parametrize("mode", ["interactive", "absolute_cap", "finite_timeout", "cancel", "evidence", "heartbeat"])
def test_f05_host_execution_uses_one_lifetime_through_collection(world, monkeypatch, mode):
    from astrid.core.execution import generic_host
    from astrid.core.execution.guards import ExecutionGuardPolicy
    host, parent, pc, child, cc = _f05_runtime_review_pair(world,
        timeout=12 if mode == "finite_timeout" else 0, runtime_limit=7200 if mode == "absolute_cap" else None)
    now = [100.0]
    monkeypatch.setattr(generic_host.time, "monotonic", lambda: now[0])
    if mode == "evidence": host.execution_policy = ExecutionGuardPolicy(evidence_cap_bytes=65536)
    checks = []
    def execute(record, inputs, output_root, attempt, *, cancelled, **kwargs):
        assert inputs == {"timeout": 12 if mode == "finite_timeout" else 0}
        for elapsed in (3601, 4001, 7201):
            now[0] = 100 + elapsed
            if mode == "cancel" and elapsed == 3601:
                world.service.cancel_task_canonical(pc["task_id"], {}, idempotency_key="f05-live-cancel")
            if mode == "evidence" and elapsed == 3601:
                (output_root / "too-large.bin").write_bytes(b"x" * 65537)
            checks.append(cancelled())
            if checks[-1]: break
        if mode == "heartbeat":
            (attempt / ".astrid-progress.json").write_text('{"phase":"done"}')
            def heartbeat(*args, **kwargs): raise OSError("lease heartbeat lost")
            monkeypatch.setattr(host.client, "heartbeat", heartbeat)
        return SimpleNamespace(payload={}, process_id=12345, returncode=0)
    monkeypatch.setattr(host, "_run_command_definition", execute)
    task = {**child, "id": child["task_id"], "capability": child["capability_id"], "fence": cc["fence"],
            "lease_id": cc["lease_id"], "executor_id": "child-worker"}
    if mode == "evidence":
        with pytest.raises(HostError, match="evidence cap"):
            host.run_task({"task": task}, lease_token=cc["lease_id"], attempt_id=cc["attempt_id"], fence=cc["fence"])
    else:
        result = host.run_task({"task": task}, lease_token=cc["lease_id"], attempt_id=cc["attempt_id"], fence=cc["fence"])
        if mode == "interactive":
            assert checks == [False, False, False]
            assert world.service.task(child["task_id"])["task"]["status"] == "completed"
            payload = world.service.task(child["task_id"])["task"]["result"]
            guards = payload.get("payload", payload)["execution_guards"]
            assert guards["lifetime"] == "lease_bound_interactive"
            assert guards["deadline_seconds"] is None
            assert guards["collection_seconds"] == 3600
        elif mode in {"absolute_cap", "finite_timeout"}:
            assert result["status"] == "failed" and result["deadline_exceeded"] is True
            assert checks == ([False, False, True] if mode == "absolute_cap" else [True])
        else:
            assert result["status"] == "cancelled"
    heartbeats = [body for _, path, body in world.calls if path.endswith("/heartbeat")]
    assert heartbeats
    assert all(body["lease_id"] in {pc["lease_id"], cc["lease_id"]} for body in heartbeats)


def test_f05_standalone_human_review_and_bool_readback_do_not_gain_authority(world, monkeypatch):
    host, parent, pc, child, cc = _f05_runtime_review_pair(world)
    from astrid.core.execution._child_bridge import task_resource
    original = host.client.task
    qualified = _f05_authority(host, child, cc)
    assert callable(qualified)
    def bool_read(tid):
        value = task_resource(original(tid))
        if tid == pc["task_id"]: value["spec"]["spec"]["inputs"]["timeout"] = False
        return value
    monkeypatch.setattr(host.client, "task", bool_read)
    with pytest.raises(HostError): qualified()
    monkeypatch.setattr(host.client, "task", original)
    standalone = copy.deepcopy(child)
    standalone["spec"].pop("delegated_parent")
    standalone["spec"].pop("delegated_recoverable_outputs")
    monkeypatch.setattr(host.client, "task", lambda tid: standalone if tid == cc["task_id"] else original(tid))
    assert _f05_authority(host, standalone, cc) is None


@pytest.mark.parametrize("mutation", ["policy", "inputs", "nested_registry", "closed"])
def test_f05_parent_pin_survives_normal_runtime_registry_updates(world, monkeypatch, mutation):
    from astrid.core.execution._child_bridge import task_resource
    established = []
    def before_child(host, parent, claim):
        assert "derived_input_registry" not in parent["spec"]
        check = _f05_authority(host, parent, claim)
        assert callable(check)
        check()
        established.append(check)
    host, parent, pc, child, cc = _f05_runtime_review_pair(world, before_child=before_child, with_source=True)
    parent_check = established[0]
    assert task_resource(world.client.task(pc["task_id"]))["spec"]["derived_input_registry"] == {}
    parent_check()
    # The original claim predates even the empty registration update.
    assert callable(_f05_authority(host, parent, pc))
    child_check = _f05_authority(host, child, cc)
    assert callable(child_check)
    data = b"normal immutable producer file"
    descriptor = {"name": "source", "output_port": "source", "filename": "source.txt",
                  "object_id": digest(data), "size": len(data), "media_type": "text/plain"}
    world.identity["actor"] = "worker"
    world.client.executor_id = "worker"
    context = {"task_id": pc["task_id"], "attempt_id": pc["attempt_id"], "lease_id": pc["lease_id"],
               "fence": pc["fence"], "runtime_epoch": 1, "run_id": pc["run_id"], "project_id": world.project}
    world.client.upload_child_input(data, descriptor=descriptor, **context)
    world.client.admit_child(child={"child_id": "f05-with-producer", "capability_id": "editorial.human_review",
        "capability_digest": host.capabilities["editorial.human_review"].capability_digest},
        inputs={"timeout": 0}, ordered_inputs=[{**descriptor, "required": False}],
        derived_inputs=[descriptor], **context)
    registry = task_resource(world.client.task(pc["task_id"]))["spec"]["derived_input_registry"]
    assert registry[descriptor["object_id"]]["parent_attempt_id"] == pc["attempt_id"]
    parent_check()
    child_check()
    assert callable(_f05_authority(host, parent, pc))
    # Every real admission field remains pinned; the live closure fence is
    # checked before the projected spec comparison as well.
    original_read = world.client.task
    def read(tid):
        value = task_resource(original_read(tid))
        if tid == pc["task_id"]:
            if mutation == "policy": value["spec"]["child_delegation"]["limits"]["max_children"] += 1
            elif mutation == "inputs": value["spec"]["spec"]["inputs"]["timeout"] = 1
            elif mutation == "nested_registry": value["spec"]["spec"]["derived_input_registry"] = {}
            else: value["spec"]["delegation_closed_attempt_id"] = pc["attempt_id"]
        return value
    monkeypatch.setattr(world.client, "task", read)
    with pytest.raises(HostError): parent_check()
    with pytest.raises(HostError): child_check()


@pytest.mark.parametrize("required", [True, False])
@pytest.mark.parametrize("source_kind", ["object", "producer_file"])
def test_f05_child_request_requiredness_canonical_matrix_executes(world, monkeypatch, required, source_kind):
    from astrid.core.execution.generic_host import _execution_contract
    from astrid.core.execution._child_bridge import task_resource
    from astrid.sdk.execution_request import normalize_execution_request

    data = b"canonical-requiredness"
    oid = digest(data)
    if source_kind == "object":
        world.service.ingest(world.project, data, media_type="text/plain", original_name="source.txt",
                             idempotency_key="requiredness-object")
    child_code = (
        "def run(value=None):\n"
        "    from pathlib import Path\n"
        "    data = Path(value).read_bytes() if value is not None else b'omitted'\n"
        "    Path('outputs').mkdir(exist_ok=True)\n"
        "    Path('outputs/answer.txt').write_bytes(data)\n"
        "    return {'answer': data.decode()}\n"
    )
    parent_value = {"object_id": oid, "digest": oid, "filename": "source.txt"}
    parent_inputs = ({
        "parent_inputs": [{"name": "source", "type": "file", "required": True}],
        "parent_input_values": {"source": parent_value},
        "parent_execution_inputs": [{"name": "source", **parent_value, "required": True}],
    } if source_kind == "object" else {})
    setup(world, derived=True, targeted=True, child_file_output=True, child_code_override=child_code,
          input_ports=[{"name": "value", "type": "file", "required": required}],
          root_object_ids=[oid] if source_kind == "object" else (), **parent_inputs)
    b = bridge(world)
    if source_kind == "producer_file":
        (b.output_root / "source.txt").write_bytes(data)
        value = {"filename": "source.txt", "output_port": "source", "media_type": "text/plain"}
        descriptor = {"name": "value", "kind": source_kind, **value}
    else:
        value = {"object_id": oid, "digest": oid, "filename": "source.txt"}
        descriptor = {"name": "value", "kind": source_kind, "object_id": oid}
    original_wire = copy.deepcopy(descriptor)
    captured = _capture_registration(world, monkeypatch)
    frame = request(world, value=value, descriptors=[descriptor], request_id=b._sequence + 1)
    child = b.dispatch(frame)
    ordered = captured.admissions[0]["ordered_inputs"]
    assert ordered[0]["required"] is required
    assert descriptor == original_wire and "required" not in descriptor
    assert captured.admissions[0]["inputs"] == {"value": value}
    assert all("required" not in ref for _, ref in captured.uploads)
    assert all("required" not in ref for ref in captured.admissions[0]["derived_inputs"])
    assert all("required" not in ref for ref in captured.receipts[0]["derived_inputs"])
    assert len(captured.uploads) == (1 if source_kind == "producer_file" else 0)
    if source_kind == "producer_file":
        ref = captured.receipts[0]["derived_inputs"][0]
        assert "association_id" in ref and "association_id" not in ordered[0]
        assert ref["object_id"] == oid and ref["filename"] == "source.txt"
    outgoing = next(body["task"] for _, path, body in world.calls if path == "/v1/delegated-tasks")
    assert outgoing["execution_request"] == normalize_execution_request(outgoing["execution_request"])
    assert outgoing["execution_request"]["inputs"] == [{
        "name": "value", "object_id": oid, "digest": oid, "filename": "source.txt", "required": required}]
    assert outgoing["spec"]["inputs"] == {"value": {
        "object_id": oid, "digest": oid, "filename": "source.txt"}}
    stored = task_resource(world.client.task(child["task_id"]))
    stored_request = stored["execution_request"]
    assert stored_request == normalize_execution_request(stored_request)
    assert stored_request["inputs"] == outgoing["execution_request"]["inputs"]
    noncanonical = copy.deepcopy(stored)
    noncanonical["execution_request"]["inputs"][0].pop("required")
    with pytest.raises(HostError, match="not normalized"):
        _execution_contract(noncanonical)
    frame["request_id"] = b._sequence + 1
    assert b.dispatch(frame) == child
    assert len(captured.admissions) == len(captured.receipts) == 1
    assert len(world.service.store.delegated_children(world.task["id"], world.claim["attempt_id"])) == 1
    if not required and source_kind == "producer_file":
        replay = world.client.admit_child(**copy.deepcopy(captured.admissions[0]))
        assert replay["task"]["task_id"] == child["task_id"]
        replay_request = [body["task"]["execution_request"] for _, path, body in world.calls
                          if path == "/v1/delegated-tasks"][-1]
        assert replay_request == outgoing["execution_request"]
        assert replay_request["inputs"][0]["required"] is False
        assert len(world.service.store.delegated_children(world.task["id"], world.claim["attempt_id"])) == 1
    claim = _claim_snapshot_task(world, CHILD, key="claim-requiredness-child")
    assert claim["execution_request"] == stored_request
    task = _snapshot_task(world, claim)
    task["execution_binding"] = claim["execution_binding"]
    assert _execution_contract(task) == claim["execution_request"]
    host = GenericPackHost(pack_roots=[world.root], client=world.client, executor_id="worker",
                           max_concurrency=2, attempt_root=world.tmp / "requiredness-child")
    host.discover()
    try:
        assert host.run_task({"task": task}, lease_token=claim["lease_id"],
                             attempt_id=claim["attempt_id"], fence=claim["fence"])
    finally:
        host.shutdown()
    assert world.service.task(child["task_id"])["task"]["status"] == "completed"
    rows = [resource(row) for row in world.client.child_outputs(child["task_id"])]
    assert len(rows) == 1 and rows[0]["output_port"] == "answer"
    assert rows[0]["digest"] == oid and world.client.get_object(oid[7:]) == data


@pytest.mark.parametrize("required", [True, False])
def test_f05_child_request_requiredness_omission_before_custody(world, monkeypatch, required):
    import astrid.core.execution._child_bridge as child_bridge_module

    child_code = (
        "def run(value=None):\n"
        "    from pathlib import Path\n"
        "    assert value is None\n"
        "    Path('outputs').mkdir(exist_ok=True)\n"
        "    Path('outputs/answer.txt').write_text('omitted')\n"
        "    return {'answer': 'omitted'}\n"
    )
    setup(world, derived=True, child_file_output=True, child_code_override=child_code,
          input_ports=[{"name": "value", "type": "file", "required": required}])
    b = bridge(world)
    captured = _capture_registration(world, monkeypatch)
    frame = request(world, request_id=b._sequence + 1)
    frame["inputs"] = {}
    if required:
        def unexpected_custody(*args, **kwargs):
            pytest.fail("required-input rejection must precede file custody")

        monkeypatch.setattr(child_bridge_module, "_snapshot", unexpected_custody)
        monkeypatch.setattr(world.client, "get_object", unexpected_custody)
        for inputs in ({}, {"value": None}, {"value": ""}):
            frame["inputs"] = inputs
            frame["request_id"] = b._sequence + 1
            with pytest.raises(ChildBridgeError, match=r"missing required input\(s\): value"):
                b.dispatch(frame)
            assert captured.admissions == captured.receipts == captured.uploads == []
            assert not world.service.store.delegated_children(world.task["id"], world.claim["attempt_id"])
    else:
        child = b.dispatch(frame)
        assert child["task_id"]
        assert captured.admissions[0]["ordered_inputs"] == captured.admissions[0]["derived_inputs"] == []
        outgoing = next(body["task"] for _, path, body in world.calls if path == "/v1/delegated-tasks")
        assert outgoing["input_object_ids"] == [] and outgoing["spec"]["inputs"] == {}
        assert "execution_request" not in outgoing
        assert captured.uploads == []
        assert serve_one(world)
        assert world.service.task(child["task_id"])["task"]["status"] == "completed"
        rows = [resource(row) for row in world.client.child_outputs(child["task_id"])]
        assert len(rows) == 1 and rows[0]["digest"] == digest(b"omitted")


def test_f05_child_request_requiredness_defaults_and_falsy_scalars(world, monkeypatch):
    child_code = (
        "def run(defaulted='fallback', flag=True, count=1):\n"
        "    assert defaulted == 'fallback' and flag is False and count == 0\n"
        "    return {'answer': defaulted}\n"
    )
    setup(world, child_code_override=child_code, input_ports=[
        {"name": "defaulted", "type": "string", "required": False, "default": "fallback"},
        {"name": "flag", "type": "boolean", "required": True},
        {"name": "count", "type": "integer", "required": True},
    ])
    b = bridge(world)
    captured = _capture_registration(world, monkeypatch)
    frame = request(world, request_id=b._sequence + 1)
    frame["inputs"] = {"flag": False, "count": 0}
    child = b.dispatch(frame)
    assert child["task_id"]
    assert captured.admissions[0]["inputs"] == {"flag": False, "count": 0}
    assert captured.admissions[0]["ordered_inputs"] == captured.admissions[0]["derived_inputs"] == []
    assert captured.uploads == []
    outgoing = next(body["task"] for _, path, body in world.calls if path == "/v1/delegated-tasks")
    assert outgoing["spec"]["inputs"] == {"flag": False, "count": 0}
    assert outgoing["input_object_ids"] == [] and "execution_request" not in outgoing
    assert len(world.service.store.delegated_children(world.task["id"], world.claim["attempt_id"])) == 1


@pytest.mark.parametrize("source_kind", ["object", "producer_file"])
def test_f05_child_request_requiredness_cannot_be_supplied_by_wire(world, source_kind):
    data = b"wire-requiredness"
    oid = digest(data)
    if source_kind == "object":
        world.service.ingest(world.project, data, media_type="text/plain", original_name="source.txt",
                             idempotency_key="wire-requiredness-object")
    setup(world, derived=True, root_object_ids=[oid] if source_kind == "object" else ())
    b = bridge(world)
    if source_kind == "producer_file":
        (b.output_root / "source.txt").write_bytes(data)
        value = {"filename": "source.txt", "output_port": "source", "media_type": "text/plain"}
        binding = {"name": "value", "kind": source_kind, **value, "required": False}
    else:
        value = {"object_id": oid, "filename": "source.txt"}
        binding = {"name": "value", "kind": source_kind, "object_id": oid, "required": False}
    with pytest.raises(ChildBridgeError, match="invalid .* binding"):
        b.dispatch(request(world, value=value, descriptors=[binding]))
    assert not world.service.store.delegated_children(world.task["id"], world.claim["attempt_id"])


def test_f05_child_request_requiredness_keeps_mixed_port_order_and_custody(world, monkeypatch):
    data = b"existing optional object"
    oid = digest(data)
    world.service.ingest(world.project, data, media_type="text/plain", original_name="alpha.txt",
                         idempotency_key="mixed-requiredness-object")
    setup(world, root_object_ids=[oid], input_ports=[
        {"name": "zeta", "type": "file", "required": True},
        {"name": "alpha", "type": "file", "required": False}])
    b = bridge(world)
    (b.output_root / "zeta.txt").write_bytes(b"required producer")
    producer = {"filename": "zeta.txt", "output_port": "source", "media_type": "text/plain"}
    bindings = [{"name": "zeta", "kind": "producer_file", **producer},
                {"name": "alpha", "kind": "object", "object_id": oid}]
    frame = request(world, descriptors=list(reversed(bindings)))
    frame["inputs"] = {"alpha": {"object_id": oid, "filename": "alpha.txt"}, "zeta": producer}
    captured = _capture_registration(world, monkeypatch)
    with pytest.raises(ChildBridgeError, match="declared port order"):
        b.dispatch(frame)
    assert captured.uploads == captured.receipts == captured.admissions == []
    frame["input_descriptors"] = bindings
    frame["request_id"] = b._sequence + 1
    child = b.dispatch(frame)
    private = captured.admissions[0]["ordered_inputs"]
    assert [(ref["name"], ref["required"]) for ref in private] == [("zeta", True), ("alpha", False)]
    outgoing = next(body["task"] for _, path, body in world.calls if path == "/v1/delegated-tasks")
    assert [(ref["name"], ref["required"]) for ref in outgoing["execution_request"]["inputs"]] == [
        ("zeta", True), ("alpha", False)]
    assert outgoing["input_object_ids"] == [digest(b"required producer"), oid]
    assert captured.admissions[0]["derived_inputs"] == [captured.uploads[0][1]]
    assert "required" not in captured.receipts[0]["derived_inputs"][0]
    assert child["task_id"]


# F05 current-attempt custody: synthetic immutable bytes only; production media
# fit and failed-child acknowledged-state restoration remain separate work.
def _f05_media_payloads(mutate=None):
    import io
    import stat
    import zipfile
    def encoded(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    media = b"captured synthetic clip"
    source = {"source_type": "fixture", "source_id": "source-1", "source_url": "https://fixture.invalid/1",
              "acquired_at": "2026-01-01", "content_hash": "a" * 64, "media_type": "video/mp4",
              "source_metadata": {"preserved": True}, "scene_index": 2}
    data = {"items": [{"item_id": "item-1", **source, "media_path": "/media/clip%20one.mp4", "accepted": False}]}
    mapping = {"schema": "training-review-media-map/v1",
        "data": {}, "canonical_data": {"digest": digest(b"descriptive canonical bytes"), "size": 27},
        "items": [{"item_id": "item-1", "source": copy.deepcopy(source), "source_media_path": "clips/clip one.mp4",
                   "media_path": "/media/clip%20one.mp4", "archive_member": "media/clip one.mp4",
                   "digest": digest(media), "size": len(media)}]}
    members = {"ui/": b"", "media/": b"", "ui/index.html": b"<html>review</html>",
               "ui/app.js": b"app", "ui/styles.css": b"style", "media/clip one.mp4": media,
               "human-review-assets.json": encoded({"html_root": "ui", "mounts": {"/media": "media"}})}
    options = {"duplicate": None, "special": None}
    if mutate:
        mutate(data, mapping, members, options)
    data_bytes = encoded(data)
    mapping["data"] = {"digest": digest(data_bytes), "size": len(data_bytes)}
    if options.get("data_identity"):
        mapping["data"].update(options["data_identity"])
    members["review-media-map.json"] = encoded(mapping)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = ((stat.S_IFDIR if name.endswith("/") else stat.S_IFREG) | 0o644) << 16
            if name == options["special"]:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, payload)
        if options["duplicate"]:
            archive.writestr(options["duplicate"], b"duplicate")
    return {"data": data_bytes, "assets_bundle": stream.getvalue(), "state": b"{}", "state_schema_bundle": b"schema fixture"}


def _f05_media_fixture(world, monkeypatch, mutate=None):
    from astrid.core.execution import generic_host
    payloads = _f05_media_payloads(mutate)
    host, parent, pc, child, cc = _f05_runtime_review_pair(world, training=True, media_payloads=payloads)
    admission = world.media_bridge.admitted_children_snapshot().children[0]
    objects = {digest(payload)[7:]: payload for payload in payloads.values()}
    reads = []
    monkeypatch.setattr(host.client, "get_object", lambda oid: reads.append(oid) or objects[oid])
    ports = [port for port in host.capabilities["editorial.human_review"].definition.inputs if port.type == "file"]
    def scope(selected=admission, current=child):
        return generic_host._training_review_media_scope(parent, current, selected, host.client, ports)
    def accounting():
        return generic_host._training_review_authority(host, parent, host.capabilities["training.dataset_build"],
            world.media_bridge, attempt_id=pc["attempt_id"], lease_id=pc["lease_id"], fence=pc["fence"], runtime_epoch=1)
    return SimpleNamespace(host=host, parent=parent, child=child, admission=admission, objects=objects,
                           payloads=payloads, reads=reads, scope=scope, accounting=accounting)


def test_f05_media_immutable_closure_and_live_authority_cache(world, monkeypatch):
    from astrid.core.execution import generic_host
    f = _f05_media_fixture(world, monkeypatch)
    original = json.loads(f.admission.task_json)
    assert original.get("attempt_id") != f.child["attempt_id"]
    scope = f.scope()
    assert scope and len(f.reads) == 2
    descriptors = json.loads(f.admission.descriptors_json)
    assert [row["name"] for row in descriptors] == ["data", "state", "assets_bundle", "state_schema_bundle"]
    assert all(row["filename"].startswith("producer-") and "digest" not in row for row in descriptors)
    review = f.accounting()
    f.reads.clear()
    assert review is not None and review.verify()
    calls = len(world.calls)
    assert review.verify() and len(f.reads) == 2
    assert len(world.calls) > calls  # Task/health authority never uses the media cache.
    # Cache retains only verified scope; local producer paths are never reopened.
    (world.media_bridge.output_root / "review-inputs/assets_bundle.zip").write_bytes(b"replaced locally")
    assert review.verify() and len(f.reads) == 2
    original_task = f.host.client.task
    def stale(tid):
        from astrid.core.execution._child_bridge import task_resource
        value = task_resource(original_task(tid))
        if tid == f.child["task_id"]:
            value["lease_fence"] += 1
        return value
    monkeypatch.setattr(f.host.client, "task", stale)
    with pytest.raises(HostError):
        review.verify()


@pytest.mark.parametrize("fault", ["object_id", "size", "bytes", "read", "filename", "extra", "duplicate", "order", "required", "producer_shape", "task_inputs", "missing"])
def test_f05_media_descriptor_custody_rejects(world, monkeypatch, fault):
    from dataclasses import replace
    f = _f05_media_fixture(world, monkeypatch)
    descriptors = json.loads(f.admission.descriptors_json)
    inputs = json.loads(f.admission.inputs_json)
    child = copy.deepcopy(f.child)
    if fault == "object_id": descriptors[0]["object_id"] = digest(b"foreign")
    elif fault == "size": descriptors[0]["size"] += 1
    elif fault == "bytes": f.objects[descriptors[0]["object_id"][7:]] = b"x" * descriptors[0]["size"]
    elif fault == "read": monkeypatch.setattr(f.host.client, "get_object", lambda _: (_ for _ in ()).throw(OSError("missing object")))
    elif fault == "filename": descriptors[0]["filename"] = inputs["data"]["filename"]
    elif fault in {"extra", "duplicate"}: descriptors.append(copy.deepcopy(descriptors[0]))
    elif fault == "order": descriptors.reverse()
    elif fault == "required": descriptors[0]["required"] = not descriptors[0]["required"]
    elif fault == "producer_shape": inputs["data"]["digest"] = descriptors[0]["object_id"]
    elif fault == "task_inputs": child["spec"]["spec"]["inputs"]["data"]["filename"] = "foreign.json"
    elif fault == "missing": inputs.pop("state")
    selected = replace(f.admission, inputs_json=json.dumps(inputs), descriptors_json=json.dumps(descriptors))
    assert f.scope(selected, child) is None


@pytest.mark.parametrize("fault", ["map_schema", "data_digest", "data_size", "duplicate_rows", "duplicate_map", "provenance", "optional_provenance", "url", "member_url", "digest", "size", "missing_media", "extra_media", "collision", "missing_ui", "mount", "unsafe", "absolute", "link", "duplicate_member", "unexpected_root"])
def test_f05_media_bundle_and_projected_rows_reject(world, monkeypatch, fault):
    def mutate(data, mapping, members, options):
        item = mapping["items"][0]
        if fault == "map_schema": mapping["schema"] = "foreign"
        elif fault == "data_digest": options["data_identity"] = {"digest": digest(b"foreign")}
        elif fault == "data_size": options["data_identity"] = {"size": 0}
        elif fault == "duplicate_rows": data["items"].append(copy.deepcopy(data["items"][0]))
        elif fault == "duplicate_map": mapping["items"].append(copy.deepcopy(item))
        elif fault == "provenance": item["source"]["content_hash"] = "b" * 64
        elif fault == "optional_provenance": item["source"].pop("scene_index")
        elif fault == "url": data["items"][0]["media_path"] = "/media/clip one.mp4"
        elif fault == "member_url": item["media_path"] = "/media/foreign.mp4"
        elif fault == "digest": item["digest"] = digest(b"foreign")
        elif fault == "size": item["size"] = True
        elif fault == "missing_media": members.pop("media/clip one.mp4")
        elif fault == "extra_media": members["media/extra.mp4"] = b"extra"
        elif fault == "collision":
            data["items"].append({**data["items"][0], "item_id": "item-2"})
            mapping["items"].append({**item, "item_id": "item-2"})
        elif fault == "missing_ui": members.pop("ui/app.js")
        elif fault == "mount": members["human-review-assets.json"] = b'{"html_root":"ui","mounts":{"/media":"foreign"}}'
        elif fault == "unsafe": members["media/../escape"] = b"escape"
        elif fault == "absolute": members["/media/escape"] = b"escape"
        elif fault == "link": options["special"] = "media/clip one.mp4"
        elif fault == "duplicate_member": options["duplicate"] = "media/clip one.mp4"
        elif fault == "unexpected_root": members["elsewhere/file"] = b"extra"
    f = _f05_media_fixture(world, monkeypatch, mutate)
    assert f.scope() is None
    review = f.accounting()
    assert review is not None and review.verify() == frozenset()
    assert review.credited_seconds == 0


@pytest.mark.parametrize("limit", ["object", "compressed", "entries", "aggregate"])
def test_f05_media_limits_are_independent(world, monkeypatch, limit):
    from astrid.core.execution import generic_host, _child_bridge
    f = _f05_media_fixture(world, monkeypatch)
    if limit == "object": monkeypatch.setattr(_child_bridge, "_MAX_OBJECT", len(f.payloads["data"]) - 1)
    elif limit == "compressed": monkeypatch.setattr(generic_host, "_TRAINING_REVIEW_ARCHIVE_BYTES", len(f.payloads["assets_bundle"]) - 1)
    elif limit == "entries": monkeypatch.setattr(generic_host, "_TRAINING_REVIEW_ARCHIVE_ENTRIES", 7)
    else: f.parent["spec"]["child_delegation"]["limits"]["max_child_bytes"] = sum(map(len, f.payloads.values())) - 1
    assert f.scope() is None


def test_f05_media_compressible_members_use_extracted_not_aggregate_budget(world, monkeypatch):
    from astrid.core.execution import generic_host
    def large(data, mapping, members, options): members["ui/app.js"] = b"x" * 8192
    f = _f05_media_fixture(world, monkeypatch, large)
    total = sum(map(len, f.payloads.values()))
    f.parent["spec"]["child_delegation"]["limits"]["max_child_bytes"] = total
    assert total < 8192 and f.scope()
    monkeypatch.setattr(generic_host, "_TRAINING_REVIEW_ARCHIVE_BYTES", 4096)
    assert len(f.payloads["assets_bundle"]) < 4096 and f.scope() is None


@pytest.mark.parametrize("port,value,accepted", [
    ("html", "__none__", True), ("serve", "__none__", True), ("serve", "", True),
    ("html", "direct.html", False), ("serve", "/media=/local/clips", False),
    ("timeout", False, False), ("timeout", 0.0, False), ("timeout", "0", False), ("timeout", None, False),
])
def test_f05_media_absence_sentinels_and_actual_overrides(world, monkeypatch, port, value, accepted):
    from dataclasses import replace
    f = _f05_media_fixture(world, monkeypatch)
    inputs = json.loads(f.admission.inputs_json)
    original = json.loads(f.admission.task_json)
    child = copy.deepcopy(f.child)
    inputs[port] = value
    original["spec"]["spec"]["inputs"][port] = value
    child["spec"]["spec"]["inputs"][port] = value
    selected = replace(f.admission, inputs_json=json.dumps(inputs), task_json=json.dumps(original))
    assert bool(f.scope(selected, child)) is accepted


def test_f05_media_corrupt_zip_readback_and_archive_metadata_fail_closed(world, monkeypatch):
    from dataclasses import replace
    f = _f05_media_fixture(world, monkeypatch)
    corrupt = b"not a zip archive"
    descriptors = json.loads(f.admission.descriptors_json)
    row = next(row for row in descriptors if row["name"] == "assets_bundle")
    previous = row["object_id"]
    row.update(object_id=digest(corrupt), size=len(corrupt))
    f.objects[digest(corrupt)[7:]] = corrupt
    original = json.loads(f.admission.task_json)
    child = copy.deepcopy(f.child)
    for task in (original, child):
        task["input_object_ids"] = [digest(corrupt) if oid == previous else oid for oid in task["input_object_ids"]]
        task["spec"]["spec"]["inputs"]["assets_bundle"].update(object_id=digest(corrupt), digest=digest(corrupt))
    selected = replace(f.admission, descriptors_json=json.dumps(descriptors), task_json=json.dumps(original))
    assert f.scope(selected, child) is None


def test_f05_media_cache_identity_change_and_closed_bridge_revoke(world, monkeypatch):
    from dataclasses import replace
    from astrid.core.execution import generic_host
    f = _f05_media_fixture(world, monkeypatch)
    review = f.accounting()
    assert review.verify() and len(f.reads) == 2
    snapshot = world.media_bridge.admitted_children_snapshot()
    altered = replace(f.admission, admission_identity="changed-admission")
    monkeypatch.setattr(world.media_bridge, "admitted_children_snapshot", lambda: replace(snapshot, children=(altered,)))
    with pytest.raises(HostError): review.verify()
    assert len(f.reads) == 4  # Admission change cannot reuse the prior verified scope.
    monkeypatch.setattr(world.media_bridge, "admitted_children_snapshot", lambda: replace(snapshot, closed=True))
    with pytest.raises(HostError): review.verify()



@pytest.fixture
def private_nonblocking_diagnostic_sink():
    """Install the owned PIPE during the call, after pytest capture setup."""
    from contextlib import contextmanager
    import fcntl
    import os

    @contextmanager
    def scope():
        reader, writer = os.pipe()
        saved = os.dup(2)
        os.dup2(writer, 2)
        # Ordinary warm-up write sets Darwin's kernel FWASWRITTEN bit.
        # It is not a caller-settable flag and F_SETFL cannot clear it.
        os.write(writer, b"x")
        assert os.read(reader, 1) == b"x"
        flags = fcntl.fcntl(2, fcntl.F_GETFL)
        fcntl.fcntl(reader, fcntl.F_SETFL, fcntl.fcntl(reader, fcntl.F_GETFL) | os.O_NONBLOCK)

        def captured():
            assert fcntl.fcntl(2, fcntl.F_GETFL) == flags
            chunks = []
            while True:
                try:
                    chunk = os.read(reader, 65536)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks).decode("ascii")

        try:
            yield captured
            assert fcntl.fcntl(2, fcntl.F_GETFL) == flags
        finally:
            os.dup2(saved, 2)
            os.close(saved)
            os.close(writer)
            os.close(reader)

    return scope


@pytest.mark.parametrize("sink_failed", [False, True])
def test_host_bridge_dispatch_diagnostic_preserves_redacted_reply(
        world, monkeypatch, capsys, sink_failed, private_nonblocking_diagnostic_sink):
    with private_nonblocking_diagnostic_sink() as private_nonblocking_diagnostic_sink:
        from astrid.sdk import _child_bridge as sdk_bridge

        class PrivateTokenDispatchError(OSError):
            pass

        original_write = os.write if "os" in globals() else __import__("os").write

        def broken_sink(fd, frame):
            if fd == 2:
                raise OSError("secret-sink-error")
            return original_write(fd, frame)

        monkeypatch.delenv("ASTRID_DEBUG_CHILD_BRIDGE_ERRORS", raising=False)
        setup(world)
        b = bridge(world)
        sdk = ChildBridge(b.child_channel)
        monkeypatch.setattr(world.client, "admit_child", lambda **kwargs: (_ for _ in ()).throw(
            PrivateTokenDispatchError(5, SECRET + " /private/path signed-token")))
        try:
            with monkeypatch.context() as patch:
                if sink_failed:
                    patch.setattr(sdk_bridge.os, "write", broken_sink)
                with pytest.raises(_BridgeRejected) as caught:
                    sdk._exchange("submit", {k: v for k, v in request(world).items()
                                             if k not in {"v", "request_id", "op"}})
            assert caught.value.error == {"code": "authorization_error",
                                          "message": "child bridge rejected request", "details": {}}
            assert not b._revoked.is_set() and not b._channel_closed and not sdk._closed
            assert not world.service.store.delegated_children(world.task["id"], world.claim["attempt_id"])
            # Deferred host diagnostics cannot precede mandatory channel cleanup.
            assert private_nonblocking_diagnostic_sink() == ""
            with monkeypatch.context() as patch:
                if sink_failed:
                    patch.setattr(sdk_bridge.os, "write", broken_sink)
                sdk.close()
                b._thread.join(timeout=1)
            assert not b._thread.is_alive() and b._channel_closed
            diagnostic = private_nonblocking_diagnostic_sink()
            assert SECRET not in diagnostic and "/private/path" not in diagnostic
            assert "PrivateTokenDispatchError" not in diagnostic and "signed-token" not in diagnostic
            if sink_failed:
                assert diagnostic == ""
            else:
                row, = [json.loads(line) for line in diagnostic.splitlines()]
                assert (row["endpoint"], row["phase"], row["operation"], row["exception_class"], row["errno"]) == (
                    "host", "dispatch", "submit", "OSError", 5)
                assert row["request_sequence"] == 1 and row["dispatch_elapsed_seconds"] >= 0
                assert row["elapsed_seconds"] >= row["dispatch_elapsed_seconds"]
        finally:
            sdk.close()
            b.revoke()


@pytest.mark.parametrize("sink_failed", [False, True])
def test_host_bridge_outer_send_diagnostic_preserves_revoke_close(
        world, monkeypatch, capsys, sink_failed, private_nonblocking_diagnostic_sink):
    with private_nonblocking_diagnostic_sink() as private_nonblocking_diagnostic_sink:
        from astrid.sdk import _child_bridge as sdk_bridge
        from astrid.sdk.exceptions import CapabilityInvocationError

        class PrivateTokenSendError(OSError):
            pass

        class SendFailureChannel:
            def __init__(self, channel):
                self.channel = channel
            def recv(self, count):
                return self.channel.recv(count)
            def settimeout(self, value):
                return self.channel.settimeout(value)
            def sendall(self, value):
                raise PrivateTokenSendError(32, SECRET + " /private/path payload")
            def shutdown(self, how):
                return self.channel.shutdown(how)
            def close(self):
                return self.channel.close()

        original_write = os.write if "os" in globals() else __import__("os").write

        def broken_sink(fd, frame):
            if fd == 2:
                raise OSError("secret-sink-error")
            return original_write(fd, frame)

        monkeypatch.delenv("ASTRID_DEBUG_CHILD_BRIDGE_ERRORS", raising=False)
        setup(world)
        b = bridge(world)
        b.channel = SendFailureChannel(b.channel)
        sdk = ChildBridge(b.child_channel)
        # The real dispatch rejects this bounded-wait preference before Runtime
        # admission, while retaining the channel for its redacted reply. A bad
        # protocol version instead revokes immediately and cannot reach send.
        body = {k: v for k, v in request(world).items() if k not in {"v", "request_id", "op"}}
        body["timeout_seconds"] = 0
        try:
            with monkeypatch.context() as patch:
                if sink_failed:
                    patch.setattr(sdk_bridge.os, "write", broken_sink)
                with pytest.raises(CapabilityInvocationError, match="unavailable"):
                    sdk._exchange("submit", body)
                b._thread.join(timeout=1)
            assert not b._thread.is_alive() and b._revoked.is_set() and b._channel_closed
            assert sdk._closed and not world.service.store.delegated_children(world.task["id"], world.claim["attempt_id"])
            diagnostic = private_nonblocking_diagnostic_sink()
            assert SECRET not in diagnostic and "/private/path" not in diagnostic
            assert "PrivateTokenSendError" not in diagnostic and "payload" not in diagnostic
            if sink_failed:
                assert diagnostic == ""
            else:
                rows = [json.loads(line) for line in diagnostic.splitlines()]
                host_rows = [row for row in rows if row["endpoint"] == "host"]
                assert any(row["phase"] == "send" for row in host_rows), [(row["endpoint"], row["phase"], row["exception_class"], row["errno"]) for row in rows]
                row, = [row for row in host_rows if row["phase"] == "send"]
                assert row["exception_class"] == "OSError" and row["errno"] == 32
                assert row["operation"] == "submit" and row["request_sequence"] == 1
                assert row["budget_seconds"] == 1.0 and row["dispatch_elapsed_seconds"] >= 0
        finally:
            sdk.close()
            b.revoke()
