"""Current bundle checkout through the workspace client's verified-head wire."""

from __future__ import annotations

import copy
import json
from functools import partial

import pytest

from astrid.sdk import AstridClient, workspace_client
from astrid.sdk.timeline import store
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from astrid_runtime_client.contract_metadata import PROTOCOL, SCHEMA_DIGEST


PROJECT = "project-1"
TIMELINE = "timeline-1"
REVISION = "revision-1"
PUBLICATION = "publication-2"
HISTORICAL_PUBLICATION = "publication-1"
SCOPE = f"/v1/projects/{PROJECT}/timelines/{TIMELINE}"
VERIFIED_HEAD = f"{SCOPE}/verified-head"
HISTORY = f"{SCOPE}/commits"


@pytest.fixture
def wire(monkeypatch):
    payload = {"config": {}, "occurrences": []}
    document = {
        "schema_version": 1,
        "project_id": PROJECT,
        "timeline_id": TIMELINE,
        "parent_revision": {
            "revision_id": REVISION, "content_digest": _digest(payload), "payload": payload,
        },
        "shot_revisions": [],
        "internal_timeline_revisions": [],
        "dependency_manifest": {"shots": [], "internal_timelines": [], "media": [], "documents": []},
    }
    head = {"revision_id": REVISION, "publication_id": PUBLICATION}
    scope = {"project_id": PROJECT, "timeline_id": TIMELINE}
    verified = {**scope, "head": head, "canonical_document": document}
    calls = []

    def connect(*, advertised=True, response=None):
        def request(method, path, headers, body):
            assert headers["Authorization"] == "Bearer local-test"
            calls.append((method, path))
            if path == "/v1/health":
                value = {
                    "status": "ok", "protocol": PROTOCOL, "schema_digest": SCHEMA_DIGEST,
                    "runtime_epoch": 1, "runtime_instance_id": "instance", "runtime_session_id": "session",
                    "identity": None,
                }
            elif path == "/v1/handshake":
                capabilities = ["execution_binding.targeted.v1"]
                if advertised:
                    capabilities.append("timeline.verified_head.v1")
                value = {
                    "protocol": PROTOCOL, "schema_digest": SCHEMA_DIGEST,
                    "session_id": "session", "actor_id": "actor", "realm_id": "realm",
                    "scopes": json.loads(body)["requested_scopes"], "capabilities": capabilities,
                }
            elif path in (f"/v1/projects/{PROJECT}/timelines?limit=50", "/v1/projects/project-slug/timelines?limit=50"):
                value = {"items": [{**scope, "slug": "film", "head_revision_id": REVISION}], "next_cursor": None}
            elif path == VERIFIED_HEAD:
                assert advertised
                if isinstance(response, Exception):
                    raise response
                if response is not None:
                    status, answer = response
                    return status, {}, json.dumps(answer).encode()
                value = verified
            elif path in (f"{SCOPE}/inspect", f"/v1/projects/project-slug/timelines/{TIMELINE}/inspect"):
                value = {**scope, "revision_id": REVISION, "head_revision_id": REVISION}
            elif path == f"{SCOPE}/composition-revisions/{REVISION}":
                value = {**scope, **document["parent_revision"], "dependency_manifest": document["dependency_manifest"]}
            elif path in (f"{HISTORY}?limit=1", f"{HISTORY}?limit=100"):
                entries = [
                    {"revision_id": REVISION, "publication_id": publication, "receipt": {
                        "receipt_id": publication, "result": {"canonical_document": document},
                    }}
                    for publication in (PUBLICATION, HISTORICAL_PUBLICATION)
                ]
                value = {"head": head, "items": entries[:1] if path.endswith("=1") else entries, "next_cursor": None}
            else:
                pytest.fail(f"unexpected workspace request {method} {path}")
            return 200, {}, json.dumps(value).encode()

        generated = workspace_client.GeneratedWorkspaceClient
        monkeypatch.setattr(workspace_client, "GeneratedWorkspaceClient", partial(generated, transport=request))
        return AstridClient.open(
            endpoint="http://127.0.0.1:12345", credential="local-test", realm_id="realm", actor_id="actor",
            client_name="checkout-reader", client_version="1", protocol_version=PROTOCOL,
        )

    return connect, verified, calls


def test_current_fetch_uses_one_verified_head_and_no_history_or_revision_reads(wire):
    connect, verified, calls = wire
    with connect() as client:
        bundle = store.fetch_bundle("project-slug", "film", client=client)

    assert calls[2:] == [
        ("GET", "/v1/projects/project-slug/timelines?limit=50"),
        ("GET", VERIFIED_HEAD),
    ]
    document = verified["canonical_document"]
    expected = bundle_from_document(document)
    expected["base_parent"]["publication_id"] = PUBLICATION
    assert bundle == expected
    assert bundle["source_mapping"]["_runtime_document"] == document


@pytest.mark.parametrize("status,code", [
    (403, "forbidden"), (404, "not_found"), (409, "stale_head"), (500, "integrity_error"),
])
def test_capable_runtime_failures_surface_without_fallback(wire, status, code):
    connect, _, calls = wire
    with connect(response=(status, {"code": code, "message": "head read failed"})) as client:
        with pytest.raises(TimelineEditError, match=code):
            store.fetch_bundle(PROJECT, TIMELINE, client=client)
    assert calls[-1] == ("GET", VERIFIED_HEAD)


def test_capable_runtime_timeout_surfaces_without_fallback(wire):
    connect, _, calls = wire
    with connect(response=TimeoutError("test timeout")) as client:
        with pytest.raises(TimelineEditError, match="transport_timeout"):
            store.fetch_bundle(PROJECT, TIMELINE, client=client)
    assert calls[-1] == ("GET", VERIFIED_HEAD)


@pytest.mark.parametrize("damage", [
    "project", "timeline", "head", "publication", "document-project", "document-timeline", "revision", "digest",
])
def test_capable_runtime_requires_exact_scope_pin_and_closure(wire, damage):
    connect, verified, calls = wire
    response = copy.deepcopy(verified)
    document = response["canonical_document"]
    if damage == "project":
        response["project_id"] = "foreign"
    elif damage == "timeline":
        response["timeline_id"] = "foreign"
    elif damage == "head":
        response["head"] = None
    elif damage == "publication":
        response["head"]["publication_id"] = " "
    elif damage == "document-project":
        document["project_id"] = "foreign"
    elif damage == "document-timeline":
        document["timeline_id"] = "foreign"
    elif damage == "revision":
        document["parent_revision"]["revision_id"] = "different"
    elif damage == "digest":
        document["parent_revision"]["content_digest"] = "invalid"
    with connect(response=(200, response)) as client:
        with pytest.raises(TimelineEditError):
            store.fetch_bundle(PROJECT, TIMELINE, client=client)
    assert calls[-1] == ("GET", VERIFIED_HEAD)


def test_legacy_runtime_keeps_exact_current_publication_and_history_reads(wire):
    connect, verified, calls = wire
    with connect(advertised=False) as client:
        bundle = store.fetch_bundle(PROJECT, TIMELINE, client=client)

    assert ("GET", VERIFIED_HEAD) not in calls
    assert [path for _, path in calls if path.startswith(HISTORY)] == [f"{HISTORY}?limit=1", f"{HISTORY}?limit=100"]
    expected = bundle_from_document(verified["canonical_document"])
    expected["base_parent"]["publication_id"] = PUBLICATION
    assert bundle == expected


def test_explicit_historical_publication_uses_original_path_on_capable_runtime(wire):
    connect, verified, calls = wire
    with connect() as client:
        bundle = store.fetch_bundle(PROJECT, TIMELINE, revision_id=REVISION,
                                    publication_id=HISTORICAL_PUBLICATION, client=client)

    assert ("GET", VERIFIED_HEAD) not in calls
    assert [path for _, path in calls if path.startswith(HISTORY)] == [f"{HISTORY}?limit=100"]
    expected = bundle_from_document(verified["canonical_document"])
    expected["base_parent"]["publication_id"] = HISTORICAL_PUBLICATION
    assert bundle == expected
