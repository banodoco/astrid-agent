"""CE-2 retry journeys through the generated client's HTTP transport edge."""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
from urllib.parse import urlsplit

import pytest

from astrid.core.receipts.canonical import canonical_json
from astrid.core.timeline.authoring_bundle import compile_authoring_candidate
from astrid.sdk.client import AstridClient
from astrid.sdk.remote import RemoteAstridClient
from astrid.sdk.timeline import CommitRefused, Timeline
from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.rebase import replay
from astrid.sdk.workspace_client import WorkspaceClient
from astrid_runtime_client import WorkspaceClient as GeneratedWorkspaceClient
from tests.sdk.test_timeline_sync import initial_bundle


class _TimelineHttp:
    """The retry tests' HTTP fake, with the command engine owning edit meaning."""

    path = "/v1/projects/p/timelines/t/commit"

    def __init__(self, outbox):
        self.outbox = outbox
        self.head = {"revision_id": "r0", "publication_id": "pub0"}
        self.documents = {"r0": self._document(initial_bundle(), "r0")}
        self.bundles = {"r0": bundle_from_document(self.documents["r0"])}
        self.accepted, self.posts = {}, []
        self.editing = False
        self.refusals_before_release = None
        self.refusals = 0
        self.entries = [{**self.head, "previous": {"revision_id": "root", "publication_id": "root"},
                         "publisher": "bootstrap", "receipt": {
                             "receipt_id": "pub0", "result": {"canonical_document": self.documents["r0"]}}}]

    @staticmethod
    def _document(bundle, revision):
        publication = compile_authoring_candidate(bundle).publication
        return {"schema_version": 1, "project_id": "p", "timeline_id": "t",
                "parent_revision": {"revision_id": revision,
                                    "content_digest": publication["content_digest"],
                                    "payload": publication["parent_composition"]},
                "shot_revisions": publication["shot_revisions"],
                "internal_timeline_revisions": publication["internal_timeline_revisions"],
                "dependency_manifest": {**publication["dependency_manifest"], "documents": []}}

    @staticmethod
    def _response(value, status=200):
        return status, {"Content-Type": "application/json"}, json.dumps(value).encode()

    def __call__(self, method, request_path, headers, raw):
        path = urlsplit(request_path).path
        body = json.loads(raw) if raw else {}
        if method == "GET" and path == "/v1/projects/p/timelines":
            return self._response({"items": [{"project_id": "p", "timeline_id": "t", "slug": "t",
                                               "head_revision_id": self.head["revision_id"]}],
                                   "next_cursor": None})
        if method == "POST" and path == "/v1/projects/p/timelines/t/inspect":
            revision = body.get("revision_id", self.head["revision_id"])
            return self._response({"project_id": "p", "timeline_id": "t", "revision_id": revision,
                                   "head_revision_id": self.head["revision_id"],
                                   "is_current_head": revision == self.head["revision_id"], "selected": []})
        if method == "GET" and path.startswith("/v1/projects/p/timelines/t/composition-revisions/"):
            document = self.documents[path.rsplit("/", 1)[-1]]
            return self._response({**document["parent_revision"], "project_id": "p", "timeline_id": "t",
                                   "dependency_manifest": document["dependency_manifest"]})
        if method == "GET" and path == "/v1/projects/p/timelines/t/commits":
            return self._response({"head": self.head, "items": self.entries, "next_cursor": None})
        if path == "/v1/projects/p/timelines/t/editing" and method in {"PUT", "DELETE"}:
            self.editing = method == "PUT"
            return self._response({"data": {"active": self.editing}})
        assert method == "POST" and path == self.path, (method, request_path)
        key = headers["Idempotency-Key"]
        if not body.get("dry_run") and body.get("source") == {"via": "python"}:
            flight = json.loads(self.outbox.read_bytes())["flight"]
            assert flight["body"].encode() == raw and flight["key"] == key
            assert flight["path"] == path
            self.posts.append((bytes(raw), key, body["base_revision_id"].encode(), flight["fence"].encode()))
        if key in self.accepted:
            original, response = self.accepted[key]
            assert raw == original, "an idempotency key must retain its exact wire bytes"
            return self._response(response)
        if self.editing and body.get("source", {}).get("via") != "app":
            self.refusals += 1
            if self.refusals_before_release is None or self.refusals <= self.refusals_before_release:
                return self._response({"code": "refused", "message": "The user is still editing",
                                       "details": {"status": "refused", "reason": "user_editing"}}, 409)
            self.editing = False
        try:
            plan = execute(self.bundles[body["base_revision_id"]], body["commands"])
            if self.head["revision_id"] != body["base_revision_id"]:
                plan = replay(plan, self.bundles[self.head["revision_id"]])
        except CommandError as error:
            return self._response({"code": "conflict", "message": str(error), "details": {
                "status": "conflict", "reason": "guard_changed", "current": self.head,
                "conflicts": [error.to_json()]}}, 409)
        ordinal = len(self.accepted) + 1
        revision, publication = f"r{ordinal}", f"pub{ordinal}"
        document = self._document(plan.bundle, revision)
        merged = body["base_revision_id"] != self.head["revision_id"]
        base = {name: body[name] for name in ("base_revision_id", "base_publication_id")}
        candidate = {"canonical_document": document, "document_digest": "sha256:" + hashlib.sha256(
            canonical_json(document).encode()).hexdigest(), "commands": plan.commands,
            "id_map": {}, "provider_version": "astrid-v1", "compiler_version": "m1"}
        if body.get("dry_run"):
            return self._response({"data": {"status": "preview", "would_status": "merged" if merged else "accepted",
                                            "evaluated_head": self.head, **base, **candidate}, "receipt": None})
        data = {"status": "merged" if merged else "accepted", **base, **candidate,
                "accepted_revision_id": revision, "previous_revision_id": self.head["revision_id"],
                "previous_publication_id": self.head["publication_id"], "publisher": "actor",
                "rebased_onto": copy.deepcopy(self.head) if merged else None,
                **{name: body[name] for name in ("source", "label", "group_id") if name in body}}
        receipt = {"receipt_id": publication, "command_kind": "timeline.commit", "idempotency_key": key,
                   "request_hash": hashlib.sha256(canonical_json({"operation": "commitTimeline", "path": path,
                       "publisher": "actor", "request": body}).encode()).hexdigest(),
                   "project_id": "p", "project_seq": [ordinal, ordinal], "event_ids": [f"e{ordinal}"],
                   "result": copy.deepcopy(data), "created_at": "2026-11-04T00:00:00Z"}
        response = {"data": data, "receipt": receipt}
        self.entries.append({"publication_id": publication, "revision_id": revision,
                             "previous": copy.deepcopy(self.head), "publisher": "actor",
                             "intent": "recorded", "records": {"schema_version": 1, "commands": plan.records},
                             "receipt": receipt})
        self.documents[revision] = document
        self.bundles[revision] = bundle_from_document(document)
        self.head = {"revision_id": revision, "publication_id": publication}
        self.accepted[key] = (bytes(raw), response)
        return self._response(response)


def _open(outbox, *, timeout=15.0):
    http = _TimelineHttp(outbox)
    generated = GeneratedWorkspaceClient("http://ce2.invalid", transport=http)
    transport = WorkspaceClient.__new__(WorkspaceClient)
    transport._generated = generated
    transport._last_handshake = {"realm_id": "realm", "actor_id": "actor"}
    client = AstridClient(remote=RemoteAstridClient(transport))
    timeline = Timeline.open("p", "t", client=client, outbox_path=outbox,
                             user_editing_timeout=timeout, user_editing_interval=0.002)
    return http, generated, client, timeline


@contextmanager
def _known_refusal_clock(monkeypatch, timeline):
    """Let real HTTP refusals arrive; virtualize only the retry backoff."""
    import time as real_time
    from types import SimpleNamespace

    import astrid.sdk.timeline.sync as sync_module

    virtual_offset = [0.0]
    deadline_clock = SimpleNamespace(
        monotonic=lambda: real_time.monotonic() + virtual_offset[0],
        sleep=lambda delay: virtual_offset.__setitem__(0, virtual_offset[0] + delay),
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(timeline.session, "user_editing_timeout", 15.0)
        scoped.setattr(timeline.session, "user_editing_interval", 10.0)
        scoped.setattr(sync_module, "time", deadline_clock)
        yield


def _lease(generated, *, release=False):
    _, _, payload = generated._request(
        "DELETE" if release else "PUT", "/v1/projects/p/timelines/t/editing",
        body=json.dumps({"tab_session_id": "ce2-tab", "sequence": 2 if release else 1}).encode(),
        headers={"Content-Type": "application/json"})
    assert generated._json(payload)["data"]["active"] is not release


def test_ce2_pending_user_editing_refusal_is_retryable_via_tl_retry(tmp_path, monkeypatch):
    for contradict in (False, True):
        path = tmp_path / f"pending-{contradict}.json"
        http, generated, client, timeline = _open(path)
        commands = [capture_move(timeline.bundle, "parent/clip:a", 5)]
        _lease(generated)
        with _known_refusal_clock(monkeypatch, timeline):
            with pytest.raises(CommitRefused) as caught:
                timeline.commit(commands, label="CE-2 captured move", key="ce2-pending", reads=[])
        pending_id = caught.value.pending_id
        assert pending_id
        assert caught.value.detail == (
            f"The user is still editing; your change is saved as {pending_id}. Retry after they finish.")
        assert caught.value.fix == f'tl.retry("{pending_id}")'
        saved = timeline.pending_show(pending_id)
        assert saved["state"] == "pending" and saved["origin"] == "list"
        assert saved["parts"][0]["commands"] == json.loads(http.posts[0][0])["commands"]
        assert saved["flight"]["body"].encode() == http.posts[0][0]
        assert saved["flight"]["key"] == "ce2-pending"
        assert saved["failure"]["details"] == {"status": "refused", "reason": "user_editing"}
        assert len(http.posts) >= 2
        assert json.loads(path.read_bytes())["flight"] is None
        if contradict:
            user = json.loads(http.posts[0][0])
            user.update(commands=[capture_move(timeline.bundle, "parent/clip:a", 9)],
                        source={"via": "app"}, label="User chose start time")
            result = client.timelines.commit("p", "t", user, idempotency_key="ce2-user-save")
            assert result.ok, result.as_dict()
        _lease(generated, release=True)
        reopened = Timeline.open("p", "t", client=client, outbox_path=path)
        before = len(http.posts)
        review = reopened.retry(pending_id)
        assert len(http.posts) == before, "retry must review before writing"
        if contradict:
            assert review["status"] == "re_derive"
            conflict = review["data"]["conflicts"][0]
            assert conflict["ref"] == "parent/clip:a" and conflict["path"] == ["at"]
            assert conflict["theirs"] == {"state": "present", "value": 9}
            assert "start time" in review["summary"]
            assert "the user (editor)" in review["summary"]
            assert "user value is 9" in review["summary"]
            assert "preserve it" in review["summary"]
            assert reopened.pending_show(pending_id)["state"] == "pending"
            assert http.bundles[http.head["revision_id"]]["parent"]["clips"][0]["at"] == 9
        else:
            assert review["status"] == "preview" and review["pending_id"] == pending_id
            accepted = reopened.commit(review)
            assert accepted["status"] == "accepted"
            assert accepted["receipt"]["idempotency_key"] == "ce2-pending"
            assert reopened.pending_show(pending_id)["state"] == "landed"
            assert accepted["data"]["canonical_document"]["parent_revision"]["payload"]["clips"][0]["at"] == 5


def test_ce2_resend_preserves_body_key_base_flight(tmp_path):
    path = tmp_path / "same-flight.json"
    http, generated, _, timeline = _open(path, timeout=1)
    http.refusals_before_release = 2
    _lease(generated)
    command = capture_move(timeline.bundle, "parent/clip:a", 5)
    accepted = timeline.commit([command], label="CE-2 café bounded resend", key="ce2-same-flight", reads=[])
    assert accepted["status"] == "accepted"
    assert http.refusals == 3 and len(http.posts) == 3
    assert http.posts[0] == http.posts[1] == http.posts[2]
    body, key, base, flight = http.posts[0]
    assert isinstance(body, bytes) and b"caf\\u00e9" in body
    assert key == "ce2-same-flight" and base == b"r0" and flight
    assert json.loads(body)["base_publication_id"] == "pub0"
    assert len(http.accepted) == 1
    assert json.loads(path.read_bytes())["flight"] is None
