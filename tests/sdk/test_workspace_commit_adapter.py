"""Focused facade/generated-transport fixtures, not CLI or installed Runtime proof.

These fixtures supply an already prepared request. They do not stand in for the
missing saved-draft command producer or authenticated session lifecycle.
"""
from __future__ import annotations

import copy
import json

import pytest

from banodoco_workspace_client import WorkspaceClient as GeneratedWorkspaceClient
from banodoco_workspace_client.generated import CommitRequest

from astrid.sdk.workspace_client import WorkspaceClient, WorkspaceClientError


def _request() -> CommitRequest:
    return dict(
        api_version=1, project_id="fixture-project", timeline_id="fixture-timeline",
        request_id="explicit-producer-key", session_id="fixture-session", session_sequence=7,
        base_revision_id="fixture-base", base_publication_id="fixture-publication",
        scope=dict(target="parent", complete=[], projection_version=1),
        change=dict(commands=[dict(op="clip.set", select="c1.rocket", args={"scale": 1.2})]),
        source=dict(via="cli", detail="adapter fixture"),
        asserted_actor=dict(kind="agent", id="fixture-agent"), label="échelle", reason="fixture",
        dry_run=False,
    )


def _result(body, status="accepted", error=None):
    return dict(
        status=status, request_id=body["request_id"], request_hash="sha256:fixture-only",
        base_revision_id=body["base_revision_id"], accepted_revision_id="fixture-accepted",
        accepted_publication_id="fixture-accepted-publication", previous_revision_id=body["base_revision_id"],
        rebased_onto=None, canonical_document=None, projection_version=1, document_digest=None,
        id_map={}, change=dict(changes=[], commands=[], direct_writes=[], consequences=[], affected=[]),
        conflicts=[], choices=[], provenance=None, evidence=None, schema_report=None,
        runtime_receipt=dict(publisher="fixture-principal", accepted_at="2026-10-15T00:00:00Z",
                             evidence_digest="sha256:fixture-only", narration_state=None), error=error,
    )


def _client(transport):
    client = WorkspaceClient("http://127.0.0.1:1", "fixture-token")
    # Use the real generated encoder/auth adapter and its supported test transport.
    client._generated = GeneratedWorkspaceClient(client.endpoint, client.token, transport=transport)
    return client


def test_commit_facade_preserves_complete_producer_body_and_generated_route():
    body = _request()
    original = copy.deepcopy(body)
    expected = _result(body)
    calls = []

    def transport(method, path, headers, payload):
        calls.append((method, path, headers, payload))
        return 200, {}, json.dumps(expected).encode()

    result = _client(transport).commit_timeline(body)

    assert result == expected
    assert body == original
    assert len(calls) == 1
    method, path, headers, payload = calls[0]
    assert (method, path) == (
        "POST", "/astrid/v1/projects/fixture-project/timelines/fixture-timeline/commit",
    )
    assert payload == json.dumps(original, ensure_ascii=False).encode("utf-8")
    assert headers["Idempotency-Key"] == original["request_id"]
    assert headers["X-Astrid-Session"] == original["session_id"]
    assert headers["Authorization"] == "Bearer fixture-token"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Request-ID"]


@pytest.mark.parametrize("http_status,code", [(400, "bad_request"), (503, "runtime_unavailable")])
def test_commit_facade_preserves_generated_typed_error_result(http_status, code):
    body = _request()
    expected = _result(body, "error", dict(code=code, message="fixture", hint="fixture", next_action="fixture"))
    expected.update(accepted_revision_id=None, accepted_publication_id=None, runtime_receipt=None)

    def transport(*_args):
        return http_status, {}, json.dumps(expected).encode()

    assert _client(transport).commit_timeline(body) == expected


def test_commit_facade_lost_response_retries_exact_prepared_body(tmp_path):
    """Synthetic receipt replay only: no real CAS, durability, draft adapter or CLI."""
    body = _request()
    original = copy.deepcopy(body)
    path = tmp_path / "prepared-request.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    calls = []
    receipts = {}
    publications = []

    def transport(method, route, headers, payload):
        calls.append((method, route, headers, payload))
        request = json.loads(payload)
        key = headers["Idempotency-Key"]
        if key not in receipts:
            publications.append("fixture-publication")
            receipts[key] = (payload, _result(request))
            raise TimeoutError("fixture accepted; response lost")
        saved_payload, result = receipts[key]
        assert payload == saved_payload
        return 200, {}, json.dumps(result).encode()

    with pytest.raises(WorkspaceClientError) as caught:
        _client(transport).commit_timeline(body)
    assert caught.value.code == "transport_timeout"
    assert caught.value.request_id == calls[0][2]["X-Request-ID"]
    assert len(calls) == 1  # no automatic retry/rebase in the facade
    assert body == original

    # A fresh facade and reloaded prepared envelope preserve the producer identity.
    result = _client(transport).commit_timeline(json.loads(path.read_text(encoding="utf-8")))
    assert result == receipts[body["request_id"]][1]
    assert len(publications) == len(receipts) == 1
    assert len(calls) == 2
    assert calls[0][:2] == calls[1][:2] == (
        "POST", "/astrid/v1/projects/fixture-project/timelines/fixture-timeline/commit",
    )
    assert calls[0][3] == calls[1][3] == json.dumps(original, ensure_ascii=False).encode("utf-8")
    for _, _, headers, _ in calls:
        assert headers["Idempotency-Key"] == body["request_id"]
        assert headers["X-Astrid-Session"] == body["session_id"]
        assert headers["Authorization"] == "Bearer fixture-token"
