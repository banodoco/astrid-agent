"""Frontend durability with generated transport fakes and the real command engine."""
from __future__ import annotations

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from runtime_protocol import platform

from astrid.core.receipts.canonical import canonical_json
from astrid.core.timeline.authoring_bundle import compile_authoring_candidate, open_authoring_bundle
from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.remote import RemoteTimelines
from astrid.sdk.timeline import store
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.outbox import Outbox, request_bytes
from astrid.sdk.timeline.rebase import replay
from astrid.sdk.timeline.sync import CommitConflict, CommitRefused, TimelineSession
from astrid.sdk.workspace_client import WorkspaceClient, WorkspaceClientError
from astrid_runtime_client import ApiError


def initial_bundle():
    payload = {"config": {"output": {"fps": 30}, "tracks": [{"id": "picture", "kind": "visual"}]},
               "clips": [{"id": name, "track": "picture", "clipType": "am-type", "at": 0,
                          "hold": 1, "params": {"text": "café"}} for name in ("a", "b")],
               "occurrences": []}
    digest = "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return open_authoring_bundle({"project_id": "p", "timeline_id": "t", "revision_id": "r0",
                                  "content_digest": digest, "payload": payload},
                                 shot_revisions=[], internal_timeline_revisions=[])


class Generated:
    """Only the generated method boundary is fake; command meaning stays core-owned."""
    def __init__(self, path):
        self.path = path
        self.bundles = {"r0": initial_bundle()}
        self.head = {"revision_id": "r0", "publication_id": "pub0"}
        self.calls, self.accepted, self.records = [], {}, {}
        self.entries, self.boundaries = [], {}
        self.publications = {"r0": "pub0"}
        self.lose_ack = False
        self.refusal = None

    def list_timeline_commits(self, project, timeline, *, cursor=None, limit):
        assert (project, timeline, limit) == ("p", "t", 1)
        return {"head": copy.deepcopy(self.head), "items": [], "next_cursor": None}

    def commit_timeline(self, project, timeline, body, *, idempotency_key):
        raw = request_bytes(body)
        durable = json.loads(self.path.read_bytes())
        flight = durable["flight"] or next(
            (saved for saved in durable.get('acknowledged', {}).values()
             if saved['key'] == idempotency_key), None)
        assert flight["body"].encode() == raw
        assert flight["key"] == idempotency_key
        assert flight["path"] == "/v1/projects/p/timelines/t/commit"
        self.calls.append((raw, idempotency_key))
        if self.refusal:
            raise self.refusal
        if idempotency_key in self.accepted:
            original, response = self.accepted[idempotency_key]
            if original != raw:
                raise ApiError(409, "conflict", "key payload changed", details={"status": "refused"})
            return copy.deepcopy(response)
        inverse_records = []
        if body["commands"][0]["op"] == "timeline.inverse":
            publications = body["commands"][0]["args"]["publications"]
            entries = [entry for entry in self.entries if entry["publication_id"] in publications]
            for publication in publications:
                inverse_records.extend(self.records[publication])
            if any("physical_ref" in binding for record in inverse_records
                   for binding in record.get("bindings", [])):
                from astrid.sdk.timeline._collections import AcceptedRecords
                from astrid.sdk.timeline._commit_patch import inverse_address_resolver
                from astrid.sdk.timeline._step_undo import select_inverse_records

                def load_boundary(entry):
                    return self.boundaries[entry["publication_id"]]

                accepted = AcceptedRecords(entries, inverse_records, load_boundary)
                resolver = inverse_address_resolver(entries, self.bundles[self.head["revision_id"]],
                                                   load_boundary=load_boundary)
                inverse_records = select_inverse_records(
                    accepted, body["commands"][0]["args"].get("steps"), address_resolver=resolver)
        try:
            if body["commands"][0]["op"] == "timeline.restore":
                target = body["commands"][0]["args"]["revision_id"]
                plan = SimpleNamespace(
                    bundle=copy.deepcopy(self.bundles[target]),
                    commands=copy.deepcopy(body["commands"]),
                    records=[],
                )
            else:
                plan = execute(self.bundles[body["base_revision_id"]], body["commands"], inverse_records=inverse_records)
                if self.head["revision_id"] != body["base_revision_id"]:
                    plan = replay(plan, self.bundles[self.head["revision_id"]])
        except TimelineEditError as exc:
            raise ApiError(409, "conflict", str(exc), details={"status": "conflict", "reason": "guard_changed"}) from exc
        ordinal = len(self.accepted) + 1
        revision, publication = f"r{ordinal}", f"pub{ordinal}"
        compiled = compile_authoring_candidate(plan.bundle).publication
        document = {"schema_version": 1, "project_id": project, "timeline_id": timeline,
                    "parent_revision": {"revision_id": revision, "content_digest": compiled["content_digest"],
                                        "payload": compiled["parent_composition"]},
                    "shot_revisions": compiled["shot_revisions"],
                    "internal_timeline_revisions": compiled["internal_timeline_revisions"],
                    "dependency_manifest": {**compiled["dependency_manifest"], "documents": []}}
        candidate = {"canonical_document": document, "document_digest": "sha256:" + hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
            "commands": plan.commands, "id_map": {}, "provider_version": "astrid-v1", "compiler_version": "m1"}
        base = {key: body[key] for key in ("base_revision_id", "base_publication_id")}
        if body.get("dry_run", False):
            return {"data": {"status": "preview", **base, "evaluated_head": copy.deepcopy(self.head), **candidate}, "receipt": None}
        rebased = self.head != {"revision_id": body["base_revision_id"], "publication_id": body["base_publication_id"]}
        data = {"status": "rebased" if rebased else "accepted", **base,
                "previous_revision_id": self.head["revision_id"], "previous_publication_id": self.head["publication_id"],
                "accepted_revision_id": revision, "rebased_onto": copy.deepcopy(self.head) if rebased else None,
                **candidate, "publisher": "actor"}
        for key in ("source", "label", "rationale", "group_id"):
            if key in body:
                data[key] = copy.deepcopy(body[key])
        receipt = {"receipt_id": publication, "command_kind": "timeline.commit", "idempotency_key": idempotency_key,
                   "request_hash": hashlib.sha256(canonical_json({"operation": "commitTimeline", "path": flight["path"],
                       "publisher": "actor", "request": body}).encode()).hexdigest(), "project_id": project, "project_seq": [ordinal, ordinal],
                   "event_ids": [f"e{ordinal}"], "result": copy.deepcopy(data), "created_at": "2026-10-19T00:00:00Z"}
        response = {"data": data, "receipt": receipt}
        self.bundles[revision] = plan.bundle
        self.records[publication] = plan.records
        self.publications[revision] = publication
        entry = {"publication_id": publication, "revision_id": revision,
                 "previous": copy.deepcopy(self.head),
                 "records": {"commands": plan.records, "steps": copy.deepcopy(body.get("steps"))}}
        self.entries.append(entry)
        self.boundaries[publication] = (copy.deepcopy(self.bundles[body["base_revision_id"]]),
                                        copy.deepcopy(plan.bundle))
        self.head = {"revision_id": revision, "publication_id": publication}
        self.accepted[idempotency_key] = (raw, copy.deepcopy(response))
        if self.lose_ack:
            self.lose_ack = False
            raise ConnectionError("response lost after acceptance")
        return response


class Client:
    def __init__(self, generated):
        transport = WorkspaceClient.__new__(WorkspaceClient)
        transport._generated = generated
        transport._last_handshake = {"realm_id": "realm", "actor_id": "actor"}
        self._remote = SimpleNamespace(_transport=transport)
        self.timelines = RemoteTimelines(transport)
        self.timelines.open_composition = self.open_composition
        self.timelines.open_bundle = self.open_bundle
        self.timelines._resolve_timeline = lambda *a, **kw: DomainResult.success({"project_id": "p", "timeline_id": "t"})
        self.projects = SimpleNamespace(current=lambda: DomainResult.success({"project": {"id": "p"}}))
        self.generated = generated
        self.reads = []
        self.bundle_reads = []

    def open_composition(self, project, timeline):
        return DomainResult.success({"native_inspection": {"project_id": "p", "timeline_id": "t"},
                                     "summary": {"head_revision_id": self.generated.head["revision_id"]}})

    def open_bundle(self, project, timeline, *, revision_id, publication_id=None):
        self.reads.append(revision_id)
        self.bundle_reads.append((revision_id, publication_id))
        if publication_id is not None and self.generated.publications.get(revision_id) != publication_id:
            return DomainResult.failure(ErrorObject(
                "conflict", "revision/publication pair is not available", {
                    "revision_id": revision_id, "publication_id": publication_id,
                },
            ))
        return DomainResult.success({"bundle": copy.deepcopy(self.generated.bundles[revision_id])})


@pytest.fixture
def connected(tmp_path, monkeypatch):
    path = tmp_path / "outbox.json"
    generated = Generated(path)
    client = Client(generated)
    for module, name in ((store, "publish_bundle"),):
        monkeypatch.setattr(module, name, lambda *a, **kw: pytest.fail("legacy writer called"), raising=False)
    return client, generated, path


def open_session(connected):
    client, _, path = connected
    return TimelineSession.open("project-slug", "timeline-slug", client=client, outbox_path=path)


def test_acknowledged_function_key_replays_original_request_read_only(connected):
    session = open_session(connected)
    client, generated, path = connected
    command = capture_move(initial_bundle(), "parent/clip:a", 3)
    state = session._state()
    disclosure = {"object_id": "media-object", "generation_id": "generation",
                  "variant_id": "original", "catalog_write": True, "retained_on_undo": True}
    result = session.submit(
        [command], label="Move clip", steps=[{"id": "step-1", "label": "Move clip",
        "intent": "", "tags": [], "command_ordinals": [0], "patch": []}],
        idempotency_key="logical-function-key", expected_state=state,
        evaluation={"kind": "function", "count": 1, "media_cache": {
            "sha256:media": {"pin": {"object_id": "media-object"}, "imports": [disclosure]}}},
    )
    head_after_commit = copy.deepcopy(session._state()["base"])
    original_request, original_wire_key = generated.calls[-1]

    replayed = session.resolve_acknowledged("logical-function-key")

    assert generated.calls[-1] == (original_request, original_wire_key)
    assert replayed["receipt"] == result["receipt"]
    assert replayed["imports"] == [disclosure]
    assert session._state()["base"] == head_after_commit
    assert json.loads(path.read_bytes())["acknowledged"]["logical-function-key"]["evaluation"]["media_cache"]["sha256:media"]["imports"] == [disclosure]
    assert client is session.client


def test_function_key_replays_original_receipt_after_undo_without_evaluation(connected):
    from astrid.sdk import timeline

    client, generated, path = connected
    editor = timeline.open("project-slug", "timeline-slug", client=client, outbox_path=path)
    evaluations = []

    def edit(working):
        evaluations.append(True)
        working.move("parent/clip:a", 3)

    accepted = editor.commit(edit, idempotency_key="edit-key")
    original_request = generated.calls[-1]
    editor.undo(publications=[accepted["receipt"]["receipt_id"]])
    head_after_undo = copy.deepcopy(editor.session._state()["base"])

    replayed = editor.commit(
        lambda working: pytest.fail("acknowledged function must replay before evaluation"),
        idempotency_key="edit-key",
    )

    assert evaluations == [True]
    assert replayed["receipt"] == accepted["receipt"]
    assert generated.calls[-1] == original_request
    assert editor.session._state()["base"] == head_after_undo
    assert json.loads(path.read_bytes())["base"] == head_after_undo


def test_function_replay_refuses_key_acknowledged_for_literal_edit(connected):
    session = open_session(connected)
    command = capture_move(initial_bundle(), "parent/clip:a", 3)
    session.commit(command, idempotency_key="literal-key")

    with pytest.raises(TimelineEditError, match="belongs to another edit"):
        session.resolve_acknowledged("literal-key")


def test_conflict_reevaluation_keeps_logical_key_and_uses_fresh_wire_key(connected):
    session = open_session(connected)
    _, generated, path = connected
    prior = capture_move(initial_bundle(), "parent/clip:a", 2)
    next_command = capture_move(initial_bundle(), "parent/clip:a", 4)
    with session.outbox.locked():
        state = session._state()
        state["pending"] = [prior]
        state["flight"] = {"path": session.path, "body": "{}", "key": "logical-key",
                            "logical_key": "logical-key", "count": 1, "fence": "old-fence",
                            "explicit_key": True}
        state["conflict"] = {"details": {"status": "conflict"}}
        session.outbox.write(state)
    state = session._state()

    result = session.submit(
        [next_command], label="Move clip", steps=[{"id": "step-1", "label": "Move clip",
        "intent": "", "tags": [], "command_ordinals": [0], "patch": []}],
        idempotency_key="logical-key", expected_state=state,
        evaluation={"kind": "function", "count": 2, "media_cache": {}},
    )

    state = json.loads(path.read_bytes())
    final_flight = state["acknowledged"]["logical-key"]
    assert final_flight["key"] != "logical-key"
    assert set(state["acknowledged"]) == {"logical-key"}
    assert generated.calls[-1][1] == final_flight["key"]
    assert result["receipt"]["idempotency_key"] == final_flight["key"]
    assert session.resolve_acknowledged("logical-key")["receipt"] == result["receipt"]


def test_fresh_timeline_open_after_clash_observes_runtime_head(connected, tmp_path):
    from astrid.sdk import timeline

    client, generated, path = connected
    stale = timeline.open("project-slug", "timeline-slug", client=client, outbox_path=path)
    peer_path = tmp_path / "peer-outbox.json"
    peer = timeline.open("project-slug", "timeline-slug", client=client,
                         outbox_path=peer_path)
    generated.path = peer_path
    peer.session.move("parent/clip:a", 2)
    generated.path = path

    with pytest.raises(CommitConflict):
        stale.session.move("parent/clip:a", 4)

    assert generated.head == {"revision_id": "r1", "publication_id": "pub1"}
    reopened = timeline.open("project-slug", "timeline-slug", client=client, outbox_path=path)

    assert reopened.session._state()["base"] == generated.head
    assert reopened._basis == generated.head
    assert client.bundle_reads[-1] == ("r1", "pub1")


def test_conflict_attempt_unknown_outcome_replays_exact_wire_request_after_reopen(connected):
    session = open_session(connected)
    _, generated, path = connected
    prior = capture_move(initial_bundle(), "parent/clip:a", 2)
    next_command = capture_move(initial_bundle(), "parent/clip:a", 4)
    with session.outbox.locked():
        state = session._state()
        state["pending"] = [prior]
        state["flight"] = {"path": session.path, "body": "{}", "key": "logical-key",
                            "logical_key": "logical-key", "count": 1, "fence": "old-fence",
                            "explicit_key": True}
        state["conflict"] = {"details": {"status": "conflict"}}
        session.outbox.write(state)
    state = session._state()
    generated.lose_ack = True

    with pytest.raises(WorkspaceClientError) as unknown:
        session.submit(
            [next_command], label="Move clip", steps=[{"id": "step-1", "label": "Move clip",
            "intent": "", "tags": [], "command_ordinals": [0], "patch": []}],
            idempotency_key="logical-key", expected_state=state,
            evaluation={"kind": "function", "count": 2, "media_cache": {}},
        )
    assert unknown.value.outcome == "unknown"
    assert unknown.value.cause["message"] == "response lost after acceptance"

    flight = json.loads(path.read_bytes())["flight"]
    expected = (flight["body"].encode(), flight["key"])
    reloaded = TimelineSession.open("project-slug", "timeline-slug", client=session.client,
                                    outbox_path=path)
    result = reloaded.flush(idempotency_key="logical-key")

    assert generated.calls[-2:] == [expected, expected]
    assert result["receipt"]["idempotency_key"] == flight["key"]
    assert json.loads(path.read_bytes())["acknowledged"]["logical-key"]["body"] == flight["body"]


def test_clean_bundle_hydration_authenticates_exact_pair_without_pending_commands(connected):
    client, _, _ = connected
    session = open_session(connected)

    assert session._state()["pending"] == []
    assert session._bundle(session._state()) == initial_bundle()
    assert client.bundle_reads == [("r0", "pub0")]


def test_clean_bundle_hydration_refuses_wrong_publication_for_revision(connected):
    client, generated, path = connected
    session = open_session(connected)
    with session.outbox.locked():
        state = session._state()
        state["base"]["publication_id"] = "wrong-publication"
        session.outbox.write(state)

    with pytest.raises(TimelineEditError, match="could not open"):
        session._bundle(session._state())
    assert client.bundle_reads == [("r0", "wrong-publication")]
    assert generated.calls == [] and json.loads(path.read_bytes())["pending"] == []


def test_outbox_unlocks_when_locked_body_raises(tmp_path, monkeypatch):
    released = []
    unlock = platform.unlock_file

    def observe(fd):
        released.append(fd)
        unlock(fd)

    monkeypatch.setattr(platform, "unlock_file", observe)
    with pytest.raises(RuntimeError, match="body failed"):
        with Outbox(tmp_path / "outbox.json").locked():
            raise RuntimeError("body failed")
    assert len(released) == 1


def test_reciprocal_retry_outbox_locks_use_canonical_order_and_dedupe_aliases(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from astrid.sdk.timeline.sync import _retry_outbox_locks

    left = Outbox(tmp_path / "left" / "outbox.json")
    right = Outbox(tmp_path / "right" / "outbox.json")
    acquired = []

    @contextmanager
    def observe_lock(outbox):
        acquired.append(outbox.path.resolve())
        yield

    monkeypatch.setattr(Outbox, "locked", observe_lock)

    with _retry_outbox_locks(left, right.path):
        pass
    with _retry_outbox_locks(right, left.path):
        pass

    ordered = sorted((left.path.resolve(), right.path.resolve()))
    assert acquired == ordered + ordered

    acquired.clear()
    left_alias = left.path.parent / ".." / "left" / "outbox.json"
    with _retry_outbox_locks(left, left_alias):
        pass
    assert acquired == [left.path.resolve()]


@pytest.mark.parametrize('guard', ['sheet', 'producer'])
def test_stage_guards_precede_even_matching_pending_retry(connected, guard):
    session = open_session(connected)
    _, generated, path = connected
    command = capture_move(initial_bundle(), 'parent/clip:a', 3)
    if guard == 'producer':
        session.queue_proposal({'schema_version': 1, 'producer': {
            'task_id': 'task', 'attempt_id': 'attempt', 'executor_id': 'worker',
            'fence': 1, 'runtime_epoch': 2}, 'admitted_target': {
                'project_id': 'p', 'timeline_id': 't',
                'base_revision_id': 'r0', 'base_publication_id': 'pub0'},
            'proposal_digest': 'sha256:' + 'a' * 64, 'commands': [command],
            'rationale': 'Keep the producer batch intact'})
        options, message = {}, 'producer proposal'
    else:
        session.queue(command)
        options = {'base': {'revision_id': 'different', 'publication_id': 'different-pub'}}
        message = 'original sheet basis'
    before = path.read_bytes()
    with pytest.raises(TimelineEditError, match=message):
        session._stage(lambda state: pytest.fail('guard allowed recapture'), lambda pending: True, **options)
    assert path.read_bytes() == before and generated.calls == []


def test_capture_and_same_value_write_are_sent_even_without_a_diff(connected):
    session = open_session(connected)
    client, generated, path = connected
    command = session.queue_move("parent/clip:a", 0)
    assert command == capture_move(initial_bundle(), "parent/clip:a", 0)
    result = session.flush()
    assert generated.bundles["r1"]["parent"]["clips"][0]["at"] == 0
    assert any(write["path"] == ["at"] and write["before"] == write["after"]
               for write in generated.records["pub1"][0]["writes"])
    assert result["data"]["accepted_publication_id"] == "pub1"
    assert result["data"]["show_command"] == "astrid timelines show t --project p --published --revision-id r1"
    assert result["receipt"] == next(iter(generated.accepted.values()))[1]["receipt"]
    assert "show_command" not in result["receipt"]["result"]
    assert json.loads(path.read_bytes())["pending"] == []
    assert client.reads == ["r0"]


def test_restore_sends_exact_revision_and_digest_through_native_commit(connected):
    session = open_session(connected)
    _, generated, path = connected
    digest = initial_bundle()["base_parent"]["content_digest"]
    result = session.restore("r0", digest, idempotency_key="restore-r0")
    body = json.loads(generated.calls[0][0])
    assert body["commands"] == [{
        "schema_version": 1,
        "op": "timeline.restore",
        "version": "1",
        "select": None,
        "args": {"revision_id": "r0", "content_digest": digest},
    }]
    assert result["data"]["status"] == "accepted"
    assert json.loads(path.read_bytes())["pending"] == []
    assert generated.head["revision_id"] == "r1"


def test_restore_lost_ack_retries_exact_request_after_reload(connected):
    session = open_session(connected)
    _, generated, path = connected
    digest = initial_bundle()["base_parent"]["content_digest"]
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        session.restore("r0", digest, idempotency_key="restore-r0")
    flight = json.loads(path.read_bytes())["flight"]
    open_session(connected).flush()
    assert generated.calls[0] == generated.calls[1]
    assert generated.calls[1] == (flight["body"].encode(), "restore-r0")
    assert json.loads(path.read_bytes())["pending"] == []


@pytest.mark.parametrize("target", ["parent/clip:a", "parent/clip:b"])
def test_lost_ack_reload_retries_exact_request_before_sending_suffix(connected, target):
    session = open_session(connected)
    client, generated, path = connected
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        session.move("parent/clip:a", 5, idempotency_key="original")
    before = json.loads(path.read_bytes())["flight"]
    session = open_session(connected)
    suffix = session.queue_move(target, 7)
    # An independent peer changes the head after the ambiguous request.
    generated.bundles["peer"] = copy.deepcopy(generated.bundles["r1"])
    generated.head = {"revision_id": "peer", "publication_id": "peer-publication"}
    generated.publications["peer"] = "peer-publication"
    session.flush()
    after = json.loads(path.read_bytes())
    assert generated.calls[0] == generated.calls[1]
    assert generated.calls[1] == (before["body"].encode(), "original")
    assert after["base"] == {"revision_id": "r1", "publication_id": "pub1"}
    assert after["pending"] == [suffix]
    assert after["flight"] is None
    # This sends B's ORIGINAL capture, only binding it to A's accepted base.
    session = open_session(connected)
    session.flush()
    body = json.loads(generated.calls[-1][0])
    assert body["commands"] == [suffix]
    assert body["base_revision_id"] == "r1" and body["base_publication_id"] == "pub1"
    assert generated.calls[-1][1] != "original"
    assert len(generated.accepted) == 2
    assert client.reads == ["r0", "r0"]


def test_failed_ack_write_recovers_the_original_flight(connected, monkeypatch):
    session = open_session(connected)
    _, generated, path = connected
    write = session.outbox.write

    def fail_ack(state):
        if state["base"]["revision_id"] == "r1":
            raise OSError("disk unavailable while acknowledging")
        write(state)

    monkeypatch.setattr(session.outbox, "write", fail_ack)
    with pytest.raises(OSError):
        session.move("parent/clip:a", 5)
    assert json.loads(path.read_bytes())["flight"]
    open_session(connected).flush()
    assert len(generated.accepted) == 1
    assert generated.calls[0] == generated.calls[1]
    assert json.loads(path.read_bytes())["pending"] == []


def test_failed_fsync_prevents_send_and_atomic_replace_preserves_unsent_intent(connected, monkeypatch):
    from astrid.sdk.timeline import outbox

    session = open_session(connected)
    _, generated, path = connected
    session.queue_move("parent/clip:a", 5)
    unsent = path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(outbox.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync failed")))
        with pytest.raises(OSError):
            session.flush()
    assert generated.calls == []
    assert path.read_bytes() == unsent
    assert list(path.parent.glob(".outbox-*")) == []
    session.flush()
    assert generated.accepted


def test_changed_intent_key_and_dry_run_cannot_replace_an_ambiguous_request(connected):
    session = open_session(connected)
    _, generated, path = connected
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        session.move("parent/clip:a", 5, idempotency_key="original")
    persisted = path.read_bytes()
    for attempt in (lambda: session.move("parent/clip:a", 6),
                    lambda: session.flush(idempotency_key="different"),
                    lambda: session.flush(dry_run=True)):
        with pytest.raises(TimelineEditError):
            attempt()
        assert path.read_bytes() == persisted
    assert len(generated.calls) == 1


def test_semantic_conflict_is_retained_but_transport_failure_stays_ambiguous(connected):
    session = open_session(connected)
    _, generated, path = connected
    session.queue_move("parent/clip:a", 5)
    peer = execute(initial_bundle(), [capture_move(initial_bundle(), "parent/clip:a", 3)])
    generated.bundles["peer"] = peer.bundle
    generated.head = {"revision_id": "peer", "publication_id": "peer-publication"}
    generated.publications["peer"] = "peer-publication"
    with pytest.raises(CommitConflict):
        session.flush()
    state = json.loads(path.read_bytes())
    assert state["flight"] is None and state["pending"] == []
    assert len(state["pending_changes"]) == 1
    record = next(iter(state["pending_changes"].values()))
    assert record["state"] == "pending"
    assert record["failure"]["details"]["status"] == "conflict"
    assert record["parts"][0]["commands"]
    assert isinstance(record["diff"], list) and record["diff"]
    assert not generated.accepted
    session.queue_move("parent/clip:b", 6)
    generated.refusal = ConnectionError("connection unavailable")
    with pytest.raises(WorkspaceClientError) as unknown:
        open_session(connected).flush()
    assert unknown.value.outcome == "unknown"
    saved = json.loads(path.read_bytes())
    assert saved["flight"] is not None and saved["pending"]


def test_record_saved_before_enrichment_failure(connected, monkeypatch):
    from astrid.sdk.timeline import sync

    session = open_session(connected)
    _, generated, path = connected
    session.queue_move("parent/clip:a", 5)
    generated.refusal = ApiError(409, "conflict", "guards changed", details={
        "status": "conflict", "reason": "guard_changed", "conflicts": [],
    })

    def fail_enrichment(saved_session, record):
        persisted = json.loads(path.read_bytes())
        assert record["id"] in persisted["pending_changes"]
        raise RuntimeError("diff enrichment failed")

    monkeypatch.setattr(sync, "_pending_diff", fail_enrichment)
    with pytest.raises(CommitConflict) as error:
        session.flush()

    state = json.loads(path.read_bytes())
    record = state["pending_changes"][error.value.pending_id]
    assert record["state"] == "pending"
    assert record["diff"] is None
    assert record["attempts"] == 1
    assert record["closed_by"] is None
    assert not {"choices", "candidate", "flight_fence"} & record.keys()


def test_conflict_packet_enrichment_runs_after_pending_record_is_saved(connected, monkeypatch):
    from astrid.sdk.timeline import clash

    session = open_session(connected)
    _, generated, path = connected
    generated.refusal = ApiError(409, "conflict", "concurrent edit", details={
        "status": "conflict", "current": generated.head, "conflicts": [],
    })
    packet = {"schema": "astrid.clash/2", "summary": "saved", "receipts": []}

    def enrich(saved_session, record):
        durable = json.loads(path.read_bytes())
        assert durable["pending_changes"][record["id"]]["state"] == "pending"
        assert record["parts"][0]["commands"]
        return packet

    monkeypatch.setattr(clash, "explain", enrich)
    command = capture_move(initial_bundle(), "parent/clip:a", 5)
    with pytest.raises(CommitConflict) as conflict:
        session.submit([command], label="Move clip", steps=[], expected_state=session._state())

    saved = json.loads(path.read_bytes())["pending_changes"][conflict.value.pending_id]
    assert conflict.value.clash == packet
    assert saved["clash"] == packet


def test_pending_show_retries_missing_packet_enrichment(connected, monkeypatch):
    from astrid.sdk.timeline import commit_session
    from astrid.sdk.timeline.commit_session import Timeline

    session = open_session(connected)
    record = {"id": "pc_saved", "state": "pending", "rev": 2,
              "failure": {"details": {"status": "conflict"}}}
    with session.outbox.locked():
        state = session._state()
        state["pending_changes"] = {record["id"]: copy.deepcopy(record)}
        session.outbox.write(state)
    timeline = Timeline.__new__(Timeline)
    timeline.session = session
    calls = []

    def enrich(saved_session, saved):
        calls.append(saved["id"])
        packet = {"schema": "astrid.clash/2", "receipts": []}
        saved["clash"] = packet
        return saved

    monkeypatch.setattr(commit_session, "_pending_clash", enrich)

    assert timeline.pending_show(record["id"])["clash"]["receipts"] == []
    assert calls == [record["id"]]


@pytest.mark.parametrize(("status", "reason"), [("refused", "policy"), ("invalid", "bad_command")])
def test_final_refusal_releases_flight_and_saves_pending_edit(connected, status, reason):
    session = open_session(connected)
    _, generated, path = connected
    rejected = session.queue_move("parent/clip:a", 5)
    generated.refusal = ApiError(409, status, "edit is not allowed",
                                 details={"status": status, "reason": reason})

    with pytest.raises(CommitRefused) as error:
        session.flush(idempotency_key="rejected-edit")

    state = json.loads(path.read_bytes())
    assert state["flight"] is None
    assert state["pending"] == []
    assert not state.get("rejected")
    assert len(state["pending_changes"]) == 1
    record = next(iter(state["pending_changes"].values()))
    assert record["state"] == "pending"
    assert record["parts"][0]["commands"] == [rejected]
    assert record["flight"]["key"] == "rejected-edit"
    assert json.loads(record["flight"]["body"])["commands"] == [rejected]
    assert record["failure"] == {
        "code": status, "message": "edit is not allowed",
        "details": {"status": status, "reason": reason},
    }
    assert error.value.failure == record["failure"]
    assert error.value.fix == f'Inspect the saved edit with tl.pending_show("{record["id"]}"), fix the edit, then retry or drop it.'
    assert record["clash"]["runtime"]["reason"] == reason
    assert record["clash"]["next"] == f'tl.pending_show("{record["id"]}")'

    # A refusal no longer wedges the session; later intent gets its own request.
    generated.refusal = None
    later = open_session(connected).queue_move("parent/clip:b", 7)
    open_session(connected).flush(idempotency_key="later-edit")
    assert json.loads(generated.calls[-1][0])["commands"] == [later]
    assert generated.calls[-1][1] == "later-edit"
    assert json.loads(path.read_bytes())["pending_changes"][record["id"]] == record


@pytest.mark.parametrize("status", ["refused", "invalid"])
def test_final_refusal_saves_and_releases_dependent_queued_suffix(connected, monkeypatch, status):
    session = open_session(connected)
    _, generated, path = connected
    entered, release = Event(), Event()
    refusal = ApiError(409, status, "edit is not allowed",
                       details={"status": status, "reason": "policy"})

    def refuse_after_suffix(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        raise refusal

    monkeypatch.setattr(generated, "commit_timeline", refuse_after_suffix)
    rejected = session.queue_move("parent/clip:a", 5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sending = pool.submit(session.flush, idempotency_key="rejected-with-suffix")
        try:
            assert entered.wait(10)
            suffix = open_session(connected).queue_move("parent/clip:a", 7)
        finally:
            release.set()
        with pytest.raises(CommitRefused):
            sending.result(timeout=10)

    state = json.loads(path.read_bytes())
    assert state["flight"] is None
    assert state["pending"] == []
    assert not state.get("rejected")
    assert len(state["pending_changes"]) == 1
    record = next(iter(state["pending_changes"].values()))
    assert record["state"] == "pending"
    assert record["parts"][0]["commands"] == [rejected]
    assert record["parts"][1]["commands"] == [suffix]
    assert json.loads(record["flight"]["body"])["commands"] == [rejected]
    assert record["failure"]["details"]["status"] == status
    before = path.read_bytes()
    with pytest.raises(TimelineEditError, match="no pending"):
        open_session(connected).flush()
    assert path.read_bytes() == before


def test_rederived_refusal_replaces_current_part_and_keeps_suffix_ordered(connected):
    session = open_session(connected)
    _, generated, path = connected
    session.queue_move("parent/clip:a", 5)
    generated.refusal = ApiError(409, "refused", "edit is not allowed",
                                 details={"status": "refused", "reason": "policy"})
    with pytest.raises(CommitRefused) as refused:
        session.flush(idempotency_key="repeat-refusal")

    suffix = capture_move(initial_bundle(), "parent/clip:b", 8)
    current = capture_move(initial_bundle(), "parent/clip:a", 6)
    state = session._state()
    record = state["pending_changes"][refused.value.pending_id]
    record["parts"].append({"base": copy.deepcopy(state["base"]), "commands": [suffix],
                            "commit_options": {"label": "Follow-up", "steps": [], "merge": "fields"}})
    session.outbox.write(state)
    pending_id = record["id"]

    with pytest.raises(CommitRefused):
        session.submit(
            [current], label="Re-derived edit", steps=[], idempotency_key="repeat-refusal",
            expected_state=session._state(), base=session._state()["base"],
            pending_id=pending_id, pending_path=str(path), pending_part=0,
            pending_how="rerun", evaluation={"kind": "function", "count": 1},
        )

    saved = json.loads(path.read_bytes())["pending_changes"][pending_id]
    assert saved["id"] == pending_id
    assert saved["parts"][0]["commands"] == [current]
    assert saved["parts"][1]["commands"] == [suffix]
    assert [part["commands"] for part in saved["parts"]] == [[current], [suffix]]
    assert json.loads(generated.calls[-1][0])["commands"] == [current]
    assert saved["flight"]["key"] != "repeat-refusal"


def test_final_conflict_saves_flight_and_dependent_suffix_as_separate_parts(connected, monkeypatch):
    session = open_session(connected)
    _, generated, path = connected
    entered, release = Event(), Event()
    conflict = ApiError(409, "conflict", "guards changed", details={
        "status": "conflict", "reason": "guard_changed", "conflicts": [],
    })

    def conflict_after_suffix(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        raise conflict

    monkeypatch.setattr(generated, "commit_timeline", conflict_after_suffix)
    flight_command = session.queue_move("parent/clip:a", 5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sending = pool.submit(session.flush, idempotency_key="conflicted-flight")
        try:
            assert entered.wait(10)
            suffix_command = open_session(connected).queue_move("parent/clip:a", 7)
        finally:
            release.set()
        with pytest.raises(CommitConflict):
            sending.result(timeout=10)

    state = json.loads(path.read_bytes())
    assert state["flight"] is None and state["pending"] == []
    assert not state.get("rejected")
    assert len(state["pending_changes"]) == 1
    record = next(iter(state["pending_changes"].values()))
    assert record["state"] == "pending" and record["part"] == 0
    assert len(record["parts"]) == 2
    assert record["parts"][0]["commands"] == [flight_command]
    assert record["parts"][1]["commands"] == [suffix_command]
    assert record["parts"][0]["base"] == state["base"]


def test_old_rejected_conflict_migrates_to_pending_change_on_open(connected):
    session = open_session(connected)
    command = session.queue_move("parent/clip:a", 5)
    failure = {"code": "conflict", "message": "guards changed", "details": {
        "status": "conflict", "reason": "guard_changed",
    }}
    state = session._state()
    body = {
        "api_version": 1, "project_id": "p", "timeline_id": "t",
        "base_revision_id": state["base"]["revision_id"],
        "base_publication_id": state["base"]["publication_id"],
        "commands": [command], "source": {"via": "python"}, "dry_run": False,
    }
    state["rejected"] = {
        "base": copy.deepcopy(state["base"]), "commands": [command],
        "flight": {"path": session.path, "body": request_bytes(body).decode(), "key": "legacy-key",
                   "count": 1, "fence": "legacy-fence"},
        "failure": failure, "commit_options": None, "evaluation": None,
    }
    state["pending"] = []
    session.outbox.write(state)

    reopened = open_session(connected)
    migrated = reopened._state()
    assert not migrated.get("rejected")
    assert len(migrated["pending_changes"]) == 1
    record = next(iter(migrated["pending_changes"].values()))
    assert record["failure"] == failure
    assert record["parts"][0]["commands"] == [command]
    first_snapshot = copy.deepcopy(migrated["pending_changes"])
    assert open_session(connected)._state()["pending_changes"] == first_snapshot


def test_legacy_conflict_migration_preserves_unknown_flight_and_queued_suffix(connected):
    session = open_session(connected)
    _, generated, path = connected
    live_command = session.queue_move("parent/clip:a", 3)
    queued_command = capture_move(initial_bundle(), "parent/clip:b", 7)
    rejected_command = capture_move(initial_bundle(), "parent/clip:a", 9)
    state = session._state()

    def saved_body(command):
        return {
            "api_version": 1, "project_id": "p", "timeline_id": "t",
            "base_revision_id": state["base"]["revision_id"],
            "base_publication_id": state["base"]["publication_id"],
            "commands": [command], "source": {"via": "python"}, "dry_run": False,
        }

    live_body = saved_body(live_command)
    live_flight = {
        "path": session.path, "body": request_bytes(live_body).decode(),
        "key": "live-unknown-key", "count": 1, "fence": "live-fence",
        "explicit_key": True,
    }
    failure = {"code": "conflict", "message": "guards changed", "details": {
        "status": "conflict", "reason": "guard_changed",
    }}
    state["pending"] = [live_command, queued_command]
    state["flight"] = live_flight
    state["rejected"] = {
        "base": copy.deepcopy(state["base"]), "commands": [rejected_command],
        "flight": {"path": session.path, "body": request_bytes(saved_body(rejected_command)).decode(),
                   "key": "legacy-rejected-key", "count": 1, "fence": "legacy-fence"},
        "failure": failure, "commit_options": None, "evaluation": None,
    }
    session.outbox.write(state)
    expected_body = live_flight["body"].encode()
    expected_queue = copy.deepcopy(state["pending"])

    reopened = open_session(connected)
    migrated = reopened._state()
    assert migrated["flight"] == live_flight
    assert migrated["flight"]["body"].encode() == expected_body
    assert migrated["pending"] == expected_queue
    assert not migrated.get("rejected")
    assert len(migrated["pending_changes"]) == 1
    record = next(iter(migrated["pending_changes"].values()))
    assert record["failure"] == failure
    assert record["parts"][0]["commands"] == [rejected_command]

    reopened.flush()

    assert generated.calls == [(expected_body, "live-unknown-key")]
    after_retry = json.loads(path.read_bytes())
    assert after_retry["pending"] == [queued_command]
    assert after_retry["flight"] is None
    assert after_retry["pending_changes"] == migrated["pending_changes"]


def test_failed_refusal_persistence_keeps_exact_flight_for_restart(connected, monkeypatch):
    from astrid.sdk.timeline import outbox

    session = open_session(connected)
    _, generated, path = connected
    command = session.queue_move("parent/clip:a", 5)
    refusal = ApiError(409, "refused", "edit is not allowed",
                       details={"status": "refused", "reason": "policy"})
    generated.refusal = refusal
    send = generated.commit_timeline
    durable_at_send = []

    def capture_durable_flight(*args, **kwargs):
        durable_at_send.append(path.read_bytes())
        return send(*args, **kwargs)

    refusal_seen = False

    def capture_refusal(*args, **kwargs):
        nonlocal refusal_seen
        try:
            return capture_durable_flight(*args, **kwargs)
        finally:
            refusal_seen = True

    monkeypatch.setattr(generated, "commit_timeline", capture_refusal)
    fsync = outbox.os.fsync

    def fail_refusal_write(fd):
        if refusal_seen:
            raise OSError("fsync failed while recording refusal")
        fsync(fd)

    monkeypatch.setattr(outbox.os, "fsync", fail_refusal_write)
    with pytest.raises(OSError, match="recording refusal"):
        session.flush(idempotency_key="rejected-edit")
    assert len(durable_at_send) == 1
    assert path.read_bytes() == durable_at_send[0]
    flight = json.loads(durable_at_send[0])["flight"]
    assert json.loads(flight["body"])["commands"] == [command]

    monkeypatch.setattr(outbox.os, "fsync", fsync)
    monkeypatch.setattr(generated, "commit_timeline", send)
    generated.refusal = None
    open_session(connected).flush()
    assert generated.calls[0] == generated.calls[1]
    assert json.loads(path.read_bytes())["pending"] == []


def test_failed_conflict_persistence_keeps_exact_flight_and_pending_for_restart(connected, monkeypatch):
    from astrid.sdk.timeline import outbox

    session = open_session(connected)
    _, generated, path = connected
    entered, release = Event(), Event()
    conflict = ApiError(409, "conflict", "guards changed", details={
        "status": "conflict", "reason": "guard_changed", "conflicts": [],
    })
    send = generated.commit_timeline
    durable_at_conflict = []
    attempts = []
    conflict_seen = False

    def conflict_after_suffix(*args, **kwargs):
        nonlocal conflict_seen
        attempts.append((request_bytes(args[2]), kwargs["idempotency_key"]))
        entered.set()
        assert release.wait(10)
        durable_at_conflict.append(path.read_bytes())
        conflict_seen = True
        raise conflict

    monkeypatch.setattr(generated, "commit_timeline", conflict_after_suffix)
    fsync = outbox.os.fsync

    def fail_conflict_write(fd):
        if conflict_seen:
            raise OSError("fsync failed while recording conflict")
        fsync(fd)

    flight_command = session.queue_move("parent/clip:a", 5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sending = pool.submit(session.flush, idempotency_key="conflicted-flight")
        try:
            assert entered.wait(10)
            suffix_command = open_session(connected).queue_move("parent/clip:a", 7)
            monkeypatch.setattr(outbox.os, "fsync", fail_conflict_write)
        finally:
            release.set()
        with pytest.raises(OSError, match="recording conflict"):
            sending.result(timeout=10)

    assert len(durable_at_conflict) == 1
    assert path.read_bytes() == durable_at_conflict[0]
    state = json.loads(durable_at_conflict[0])
    assert state["flight"] is not None
    assert json.loads(state["flight"]["body"])["commands"] == [flight_command]
    assert state["pending"] == [flight_command, suffix_command]
    assert not state.get("pending_changes")
    assert not state.get("rejected")

    monkeypatch.setattr(outbox.os, "fsync", fsync)
    monkeypatch.setattr(generated, "commit_timeline", send)
    reopened = open_session(connected)
    assert reopened._state() == state
    reopened.flush()
    assert attempts[0] == generated.calls[0]
    state = json.loads(path.read_bytes())
    assert state["flight"] is None
    assert state["pending"] == [suffix_command]
    assert not state.get("pending_changes")
    assert not state.get("rejected")

    open_session(connected).flush()
    assert len(generated.accepted) == 2
    assert json.loads(path.read_bytes())["pending"] == []


def test_preview_remains_unsent_and_does_not_reserve_its_key(connected):
    session = open_session(connected)
    _, generated, path = connected
    preview = session.move("parent/clip:a", 5, dry_run=True, idempotency_key="preview")
    assert preview["receipt"] is None and "show_command" not in preview["data"]
    assert not generated.accepted
    assert json.loads(path.read_bytes())["pending"]
    accepted = session.flush(idempotency_key="preview")
    assert accepted["receipt"] is not None
    publication = accepted["receipt"]["receipt_id"]
    session.undo([publication])
    assert generated.calls[0][1] == generated.calls[1][1] == "preview"
    assert len({key for _, key in generated.calls}) == 2
    inverse = json.loads(generated.calls[-1][0])["commands"][0]
    assert inverse == {"schema_version": 1, "op": "timeline.inverse", "version": "1", "select": None,
                       "args": {"publications": [publication]}}
    assert generated.bundles["r2"]["parent"] == initial_bundle()["parent"]


@pytest.mark.parametrize("steps", [
    {},
    {"pub1": []},
    {"pub1": "step-1"},
    {"pub1": [{}]},
    {"pub1": [1]},
    {"pub1": ["step-1", "step-1"]},
    {"pub1": [""]},
    {"other": ["step-1"]},
])
def test_undo_rejects_malformed_step_selector_before_staging(connected, steps):
    session = open_session(connected)
    _, generated, path = connected
    before = path.read_bytes()

    with pytest.raises(TimelineEditError, match="undo steps"):
        session.undo(["pub1"], steps=steps)

    assert path.read_bytes() == before and generated.calls == []


def test_identity_change_cannot_send_existing_outbox(connected):
    session = open_session(connected)
    client, generated, path = connected
    session.queue_move("parent/clip:a", 5)
    original = path.read_bytes()
    client._remote._transport._last_handshake["realm_id"] = "other-realm"
    with pytest.raises(TimelineEditError, match="identity changed"):
        session.flush()
    with pytest.raises(TimelineEditError, match="different realm"):
        open_session(connected)
    assert path.read_bytes() == original
    assert generated.calls == []


def test_new_clean_session_captures_current_published_state(connected):
    session = open_session(connected)
    _, generated, _ = connected
    session.move("parent/clip:a", 5)
    generated.bundles["peer"] = execute(generated.bundles["r1"], [
        capture_move(generated.bundles["r1"], "parent/clip:b", 3),
    ]).bundle
    generated.head = {"revision_id": "peer", "publication_id": "peer-publication"}
    generated.publications["peer"] = "peer-publication"
    command = open_session(connected).queue_move("parent/clip:b", 7)
    assert command == capture_move(generated.bundles["peer"], "parent/clip:b", 7)


def test_nonidentity_map_does_not_acknowledge_pending_work(connected, monkeypatch):
    session = open_session(connected)
    _, generated, path = connected
    send = generated.commit_timeline

    def remapping(*args, **kwargs):
        response = send(*args, **kwargs)
        response["data"]["id_map"] = {"a": "different"}
        return response

    monkeypatch.setattr(generated, "commit_timeline", remapping)
    with pytest.raises(TimelineEditError) as unknown:
        session.move("parent/clip:a", 5)
    assert unknown.value.outcome == "unknown"
    assert "nonidentity" in unknown.value.cause["message"]
    state = json.loads(path.read_bytes())
    assert state["flight"] and state["pending"] and state["base"]["revision_id"] == "r0"


def test_blocked_send_allows_queue_and_acknowledges_only_frozen_prefix(connected, monkeypatch):
    session = open_session(connected)
    _, generated, path = connected
    entered, release = Event(), Event()
    send = generated.commit_timeline

    def blocked(*args, **kwargs):
        response = send(*args, **kwargs)
        entered.set()
        assert release.wait(10)
        return response

    monkeypatch.setattr(generated, "commit_timeline", blocked)
    session.queue_move("parent/clip:a", 5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sending = pool.submit(session.flush, idempotency_key="original")
        try:
            assert entered.wait(10)
            frozen = json.loads(path.read_bytes())["flight"]
            suffix = pool.submit(open_session(connected).queue_move, "parent/clip:a", 7).result(timeout=10)
            during = json.loads(path.read_bytes())
            assert during["flight"] == frozen and during["pending"][1:] == [suffix]
            assert suffix["reads"] and suffix["selections"] and suffix["bindings"]
        finally:
            release.set()
        sending.result(timeout=10)
    state = json.loads(path.read_bytes())
    assert state["base"] == {"revision_id": "r1", "publication_id": "pub1"}
    assert state["flight"] is None and state["pending"] == [suffix]
    open_session(connected).flush()
    assert json.loads(generated.calls[-1][0])["commands"] == [suffix]
    assert len(generated.accepted) == 2


@pytest.mark.parametrize("late", ["ack", "timeout", "conflict", "refused"])
def test_duplicate_flush_and_late_completion_cannot_overwrite_new_flight(connected, monkeypatch, late):
    session = open_session(connected)
    _, generated, path = connected
    entered_a, entered_b, release_a, release_b = Event(), Event(), Event(), Event()
    send = generated.commit_timeline

    def blocked(*args, **kwargs):
        response = send(*args, **kwargs)
        if kwargs["idempotency_key"] == "a" and not entered_a.is_set():
            entered_a.set()
            assert release_a.wait(10)
            if late == "timeout":
                raise ConnectionError("late timeout")
            if late == "conflict":
                raise ApiError(409, "conflict", "late conflict", details={"status": "conflict"})
            if late == "refused":
                raise ApiError(409, "refused", "late refusal", details={"status": "refused"})
        elif kwargs["idempotency_key"] == "b":
            entered_b.set()
            assert release_b.wait(10)
        return response

    monkeypatch.setattr(generated, "commit_timeline", blocked)
    session.queue_move("parent/clip:a", 5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(session.flush, idempotency_key="a")
        try:
            assert entered_a.wait(10)
            open_session(connected).flush(idempotency_key="a")
            assert generated.calls[0] == generated.calls[1]
            session.queue_move("parent/clip:a", 7)
            second = pool.submit(session.flush, idempotency_key="b")
            assert entered_b.wait(10)
            newer = path.read_bytes()
            release_a.set()
            if late == "ack":
                first.result(timeout=10)
            else:
                with pytest.raises(CommitConflict if late == "conflict" else
                                   CommitRefused if late == "refused" else WorkspaceClientError):
                    first.result(timeout=10)
            assert path.read_bytes() == newer
            assert json.loads(newer)["flight"]["key"] == "b"
        finally:
            release_a.set()
            release_b.set()
        second.result(timeout=10)
    assert len(generated.accepted) == 2
    assert json.loads(path.read_bytes())["pending"] == []


@pytest.mark.parametrize(("field", "value"), [
    (("data", "status"), "preview"),
    (("receipt",), None),
    (("receipt", "receipt_id"), ""),
    (("receipt", "receipt_id"), "pub0"),
    (("receipt", "command_kind"), "timeline.restore"),
    (("receipt", "project_id"), "other"),
    (("receipt", "idempotency_key"), "other"),
    (("receipt", "request_hash"), "wrong"),
    (("receipt", "result"), {}),
    (("receipt", "project_seq"), [True, 1]),
    (("data", "base_revision_id"), "other"),
    (("data", "base_publication_id"), "other"),
    (("data", "publisher"), "other"),
    (("data", "accepted_revision_id"), "other"),
    (("data", "previous_publication_id"), "other"),
    (("data", "rebased_onto"), {"revision_id": "peer", "publication_id": "peer"}),
    (("data", "canonical_document", "project_id"), "other"),
    (("data", "canonical_document", "timeline_id"), "other"),
    (("data", "canonical_document", "schema_version"), True),
    (("data", "canonical_document", "parent_revision", "revision_id"), "other"),
    (("data", "canonical_document", "parent_revision", "payload"), []),
    (("data", "canonical_document", "shot_revisions"), None),
    (("data", "canonical_document", "dependency_manifest"), {}),
    (("data", "commands"), []),
    (("data", "commands"), [{}]),
    (("data", "id_map"), None),
    (("data", "id_map"), {"": ""}),
    (("data", "provider_version"), 1),
    (("data", "compiler_version"), ""),
    (("data", "document_digest"), "bad"),
])
def test_invalid_ack_retains_exact_flight_and_suffix(connected, monkeypatch, field, value):
    session = open_session(connected)
    _, generated, path = connected
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        session.move("parent/clip:a", 5, idempotency_key="original")
    session.queue_move("parent/clip:a", 7)
    before = path.read_bytes()
    send = generated.commit_timeline

    def invalid(*args, **kwargs):
        result = send(*args, **kwargs)
        target = result
        for key in field[:-1]:
            target = target[key]
        target[field[-1]] = value
        # Isolate semantic validation from the independent result equality check.
        if field[0] == "data":
            result["receipt"]["result"] = copy.deepcopy(result["data"])
        return result

    with monkeypatch.context() as patch:
        patch.setattr(generated, "commit_timeline", invalid)
        with pytest.raises((TimelineEditError, ValueError)):
            open_session(connected).flush()
    assert path.read_bytes() == before
    open_session(connected).flush()
    assert generated.calls[-1] == generated.calls[0]
    assert len(json.loads(path.read_bytes())["pending"]) == 1


@pytest.mark.parametrize(("field", "value"), [
    (("data", "status"), "accepted"),
    (("receipt",), {}),
    (("data", "evaluated_head"), {"revision_id": "r0"}),
    (("data", "accepted_revision_id"), "r1"),
    (("data", "base_publication_id"), "wrong"),
    (("data", "canonical_document", "timeline_id"), "wrong"),
])
def test_invalid_preview_never_releases_original_request(connected, monkeypatch, field, value):
    session = open_session(connected)
    _, generated, path = connected
    send = generated.commit_timeline
    before = []

    def invalid(*args, **kwargs):
        before.append(path.read_bytes())
        result = send(*args, **kwargs)
        target = result
        for key in field[:-1]:
            target = target[key]
        target[field[-1]] = value
        return result

    monkeypatch.setattr(generated, "commit_timeline", invalid)
    with pytest.raises((TimelineEditError, ValueError)):
        session.move("parent/clip:a", 5, dry_run=True, idempotency_key="preview")
    assert path.read_bytes() == before[0]
    assert not generated.accepted


@pytest.mark.parametrize("optional", [{}, {"dry_run": False, "source": {"via": "cli", "detail": "café"},
                                          "label": "", "rationale": "keep this", "group_id": "gesture"}])
def test_restart_preserves_parsed_body_source_and_optional_presence(connected, optional):
    session = open_session(connected)
    client, generated, path = connected
    command = session.queue_move("parent/clip:a", 5)
    with session.outbox.locked():
        state = session._state()
        body = {"api_version": 1, "project_id": "p", "timeline_id": "t", "base_revision_id": "r0",
                "base_publication_id": "pub0", "commands": [command], **optional}
        state["flight"] = {"path": session.path, "key": "original", "count": 1,
                           "body": request_bytes(body).decode()}
        session.outbox.write(state)
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        session.flush(via="app")
    frozen = json.loads(path.read_bytes())["flight"]
    # Explicit reconnect may change discovery/credential, never authenticated scope.
    client._remote._transport.endpoint = "http://new-runtime-door"
    client._remote._transport.token = "rotated"
    open_session(connected).flush(via="python")
    assert generated.calls == [(frozen["body"].encode(), "original")] * 2
    assert json.loads(generated.calls[-1][0]) == body


@pytest.mark.parametrize("change", ["read", "identity"])
def test_dependent_suffix_conflict_preserves_original_guards_and_identity(connected, change):
    session = open_session(connected)
    _, generated, path = connected
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        session.move("parent/clip:a", 5)
    suffix = session.queue_move("parent/clip:a", 7)
    original = copy.deepcopy(suffix)
    session.flush()
    peer = copy.deepcopy(generated.bundles["r1"])
    peer["parent"]["clips"][0]["hold" if change == "read" else "id"] = 2 if change == "read" else "replacement"
    generated.bundles["peer"] = peer
    generated.head = {"revision_id": "peer", "publication_id": "peer-publication"}
    generated.publications["peer"] = "peer-publication"
    with pytest.raises(CommitConflict):
        open_session(connected).flush(idempotency_key="b")
    state = json.loads(path.read_bytes())
    assert state["pending"] == [] and state["flight"] is None
    assert len(state["pending_changes"]) == 1
    record = next(iter(state["pending_changes"].values()))
    assert record["failure"]["details"]["status"] == "conflict"
    assert record["parts"][0]["commands"] == [original]
    assert record["parts"][0]["base"] == {"revision_id": "r1", "publication_id": "pub1"}
    before = path.read_bytes()
    with pytest.raises(TimelineEditError, match="no pending"):
        session.flush(idempotency_key="replacement")
    assert path.read_bytes() == before


@pytest.mark.parametrize("field", ["realm_id", "actor_id"])
def test_scope_change_while_response_is_outstanding_keeps_original_flight(connected, monkeypatch, field):
    session = open_session(connected)
    client, generated, path = connected
    entered, release = Event(), Event()
    send = generated.commit_timeline

    def blocked(*args, **kwargs):
        result = send(*args, **kwargs)
        entered.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(generated, "commit_timeline", blocked)
    session.queue_move("parent/clip:a", 5)
    with ThreadPoolExecutor(max_workers=1) as pool:
        sending = pool.submit(session.flush)
        try:
            assert entered.wait(10)
            before = path.read_bytes()
            handshake = client._remote._transport._last_handshake
            original = handshake[field]
            handshake[field] = "other"
        finally:
            release.set()
        with pytest.raises(TimelineEditError, match="identity changed"):
            sending.result(timeout=10)
    assert path.read_bytes() == before
    handshake[field] = original
    open_session(connected).flush()
    assert generated.calls[0] == generated.calls[1]
    assert len(generated.accepted) == 1


@pytest.mark.parametrize("field", ["realm_id", "actor_id"])
def test_missing_authenticated_scope_never_defaults_identity(connected, field):
    client, generated, path = connected
    del client._remote._transport._last_handshake[field]
    with pytest.raises(TimelineEditError, match="authenticated"):
        open_session(connected)
    assert not path.exists() and not generated.calls


@pytest.mark.parametrize("reading", ["history", "bundle"])
def test_blocked_read_does_not_hold_outbox_lock_or_overwrite_concurrent_work(connected, monkeypatch, reading):
    session = open_session(connected)
    client, generated, path = connected
    entered, release = Event(), Event()
    if reading == "bundle":
        session.queue_move("parent/clip:a", 5)
    read = client.timelines.history if reading == "history" else client.timelines.open_bundle

    def blocked(*args, **kwargs):
        result = read(*args, **kwargs)
        entered.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(client.timelines, "history" if reading == "history" else "open_bundle", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(open_session, connected) if reading == "history" else pool.submit(session.queue_move, "parent/clip:b", 7)
        try:
            assert entered.wait(10)
            if reading == "history":
                # Capture can read through a separate session while history is blocked.
                queued = pool.submit(session.queue_move, "parent/clip:a", 5).result(timeout=10)
                before = path.read_bytes()
            else:
                pool.submit(session.flush).result(timeout=10)
                assert json.loads(path.read_bytes())["base"]["revision_id"] == "r1"
        finally:
            release.set()
        result = first.result(timeout=10)
    state = json.loads(path.read_bytes())
    if reading == "history":
        assert path.read_bytes() == before and state["pending"] == [queued]
        assert result.project_id == "p"
    else:
        assert state["base"]["revision_id"] == "r1" and state["pending"] == [result]
        assert client.reads == ["r0", "r0", "r1"]


@pytest.mark.parametrize("kind", [None, False, 7, "assembly-admission-v2", "commit"])
def test_present_unknown_flight_kind_refuses_reload_without_post_or_mutation(connected, kind):
    session = open_session(connected)
    _, generated, path = connected
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        session.move("parent/clip:a", 5, idempotency_key="original")
    state = json.loads(path.read_bytes())
    state["flight"]["kind"] = kind
    path.write_bytes(request_bytes(state))
    frozen = path.read_bytes()
    calls = copy.deepcopy(generated.calls)

    with pytest.raises(TimelineEditError, match="in-flight request kind"):
        open_session(connected)

    assert path.read_bytes() == frozen
    assert generated.calls == calls


def test_accepted_batch_key_retries_original_bytes_and_refuses_changed_metadata(connected):
    session = open_session(connected)
    _, generated, path = connected
    command = {'schema_version': 1, 'op': 'clip.move', 'version': '1',
               'select': 'parent/clip:a', 'args': {'on': 5}}
    accepted = session.commit_many([command], label='Opening', idempotency_key='batch-key')
    original = generated.calls[-1]
    session.move('parent/clip:b', 7, idempotency_key='later')
    before = path.read_bytes()
    retried = open_session(connected).commit_many(
        [command], label='Opening', idempotency_key='batch-key')
    assert retried['receipt'] == accepted['receipt']
    assert generated.calls[-1] == original
    assert path.read_bytes() == before
    calls = len(generated.calls)
    with pytest.raises(TimelineEditError, match='fresh key'):
        session.commit_many([command], label='Changed', idempotency_key='batch-key')
    assert len(generated.calls) == calls and path.read_bytes() == before
