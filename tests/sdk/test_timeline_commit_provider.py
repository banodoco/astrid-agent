from __future__ import annotations

import copy
import json
import sys
import types

import pytest

from astrid.sdk.timeline._trim_commands import capture_clip_trim
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import (
    _digest,
    bundle_from_document,
    create_runtime_commit_service,
)
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.staging_recipes import (
    install_staging_operations,
    staged_move,
)
from tests.sdk.test_timeline_commit_provider_document import MEDIA, document_fixture


class CommitRejected(Exception):
    def __init__(self, failure):
        self.failure = copy.deepcopy(failure)
        super().__init__(failure["message"])


class StaleHead(Exception):
    def __init__(self, current_head):
        self.current_head = copy.deepcopy(current_head)
        super().__init__("timeline head changed")


@pytest.fixture(autouse=True)
def runtime_abi(monkeypatch):
    # Agent's dependency-only environment need not install Runtime. Only its
    # two ABI exceptions/metadata are faked; all command execution is real.
    runtime = types.ModuleType("runtime_protocol")
    runtime.__path__ = []
    if (platform := sys.modules.get("runtime_protocol.platform")) is not None:
        runtime.platform = platform
    metadata = types.ModuleType("runtime_protocol.contract_metadata")
    metadata.SCHEMA_DIGEST = "exact-installed-runtime-digest"
    abi = types.ModuleType("runtime_protocol.timeline_commit")
    abi.CommitRejected, abi.StaleHead = CommitRejected, StaleHead
    for module in (runtime, metadata, abi):
        monkeypatch.setitem(sys.modules, module.__name__, module)


@pytest.fixture
def native_staging_operations(monkeypatch):
    install_staging_operations(monkeypatch)


def detached(value):
    return json.loads(json.dumps(value, allow_nan=False))


def refusal(reason):
    return CommitRejected({"http_status": 409, "code": "conflict", "message": reason,
                           "details": {"status": "refused", "reason": reason,
                                       "base": None, "current": None, "conflicts": []}})


class DetachedRealm:
    """Small scoped in-memory Runtime with immutable reads and acceptance CAS."""

    def __init__(self, document=None):
        document = document or document_fixture()
        revision = document["parent_revision"]["revision_id"]
        self.documents = {revision: detached(document)}
        self.head = {"revision_id": revision, "publication_id": "bootstrap"}
        receipt = {"receipt_id": "bootstrap", "command_kind": "timeline.create",
                   "idempotency_key": "bootstrap-key", "request_hash": _digest(document),
                   "project_id": "project-1", "project_seq": [0], "event_ids": ["event-bootstrap"],
                   "result": {"parent_revision_id": revision}, "created_at": "2026-10-18T00:00:00Z"}
        self.entries = [{"publication_id": "bootstrap", "revision_id": revision,
                         "previous": None, "intent": "bootstrap", "records": None, "receipt": receipt,
                         "provider_version": None, "compiler_version": None, "publisher": "actor-1"}]
        self.live_sources, self.retained_sources = {}, {}
        self.snapshot_objects = {}
        self.accepted, self.prepared, self.reads = [], [], []
        self.before_accept = None
        self.page_size = 100
        self.request_count = 0

    def context(self, calls, *, base=None, dry_run=False):
        base = base or self.head
        body = {"api_version": 1, "project_id": "project-1", "timeline_id": "film",
                "base_revision_id": base["revision_id"],
                "base_publication_id": base["publication_id"], "commands": detached(calls),
                "dry_run": dry_run, "source": {"via": "python"}, "rationale": "test intent"}
        invocation = {"operation": "commitTimeline",
                      "path": "/v1/projects/project-1/timelines/film/commit", "body": body}
        self.request_count += 1
        return {"request": body, "invocation": invocation,
                "idempotency_key": "request-" + str(self.request_count),
                "request_hash": _digest(invocation), "realm_id": "realm-1", "publisher": "actor-1"}

    def call(self, calls, *, base=None, dry_run=False):
        context = self.context(calls, base=base, dry_run=dry_run)
        return create_runtime_commit_service().commit(context, DetachedPort(self, context))

    def move(self, ref="parent/clip:clip-a", on=3.003, *, base=None):
        from astrid.sdk.timeline.capture import capture_move
        base = base or self.head
        bundle = bundle_from_document(self.documents[base["revision_id"]])
        return capture_move(bundle, ref, on)


class DetachedPort:
    def __init__(self, realm, context):
        self.realm, self.context = realm, detached(context)
        self.captures = {}

    def head(self):
        return detached(self.realm.head)

    def revision_closure(self, revision):
        self.realm.reads.append(("revision", revision))
        if revision not in self.realm.documents:
            raise refusal("history_unavailable")
        return detached(self.realm.documents[revision])

    def history_entry(self, publication):
        self.realm.reads.append(("entry", publication))
        for entry in self.realm.entries:
            if entry["publication_id"] == publication:
                return detached(entry)
        raise refusal("history_unavailable")

    def history(self, through, after_publication_id=None, cursor=None, limit=100):
        entries = self.realm.entries
        self.realm.reads.append(("history", detached(through), after_publication_id))
        try:
            start = (next(i + 1 for i, entry in enumerate(entries)
                          if entry["publication_id"] == after_publication_id)
                     if after_publication_id is not None else 0)
            end = next(i + 1 for i, entry in enumerate(entries)
                       if entry["publication_id"] == through["publication_id"])
        except StopIteration:
            raise refusal("history_unavailable") from None
        if start > end:
            raise refusal("history_unavailable")
        offset = int(cursor or start)
        next_offset = min(end, offset + min(limit, self.realm.page_size))
        return {"head": detached(through), "items": detached(entries[offset:next_offset]),
                "next_cursor": str(next_offset) if next_offset < end else None}

    def _source(self, pin, kind):
        self.realm.reads.append((kind, detached(pin)))
        key = _digest(pin)
        capture = self.realm.retained_sources.get(key, self.realm.live_sources.get(key))
        if capture is None:
            raise refusal("dependency_unavailable")
        self.captures[key] = detached(capture)
        return detached(capture)

    def transcript_source(self, pin):
        return self._source(pin, "transcript")

    def config_source(self, pin):
        return self._source(pin, "config")

    def document_object(self, pin, object_id):
        capture = self.captures.get(_digest(pin))
        if (capture is None or capture['document']['kind'] != 'astrid.project-pack'
                or not any(admission.get('snapshot_object', {}).get('object_id') == object_id
                           for admission in capture['document']['content']['admissions'])
                or object_id not in self.realm.snapshot_objects):
            raise refusal('dependency_unavailable')
        return detached(self.realm.snapshot_objects[object_id])

    def shot_closure(self, *args):
        raise AssertionError("M1 move cannot read mutable or unrelated source shots")

    def selected_media(self, *args):
        raise AssertionError("M1 move cannot refresh existing immutable media")

    def narration_take(self, *args):
        raise AssertionError("M1 move cannot invoke narration")

    def _candidate(self, prepared):
        assert set(prepared) == {"expected_head", "canonical_document", "document_digest",
                                 "records", "id_map", "semantic_events", "provider_version",
                                 "compiler_version"}
        assert prepared["document_digest"] == _digest(prepared["canonical_document"])
        records = prepared["records"]["commands"]
        assert [record["ordinal"] for record in records] == list(range(len(records)))
        for record in records:
            assert set(record) == {"ordinal", "command", "writes", "reads", "selections",
                                   "bindings", "created", "dependencies", "derived_writes"}
        assert all(key == value for key, value in prepared["id_map"].items())
        assert prepared["semantic_events"] == []
        document = prepared["canonical_document"]
        pins = document["dependency_manifest"]["documents"]
        assert {_digest(pin) for pin in pins} == self.captures.keys()
        for pin in pins:
            assert any(dep["pin"] == pin and dep["kind"] in {"transcript", "source"}
                       for record in records for dep in record["dependencies"])
        return {**{key: detached(prepared[key]) for key in (
            "canonical_document", "document_digest", "id_map", "provider_version", "compiler_version")},
            "commands": [detached(record["command"]) for record in records]}

    def preview(self, prepared):
        request = self.context["request"]
        assert request["dry_run"]
        data = {"status": "preview", "evaluated_head": self.head(),
                "base_revision_id": request["base_revision_id"],
                "base_publication_id": request["base_publication_id"],
                **self._candidate(prepared)}
        records = prepared["records"]
        for key in ("steps", "merge_mode", "label"):
            if key in records:
                data[key] = detached(records[key])
        base = {"revision_id": request["base_revision_id"],
                "publication_id": request["base_publication_id"]}
        data["would_status"] = "accepted" if self.head() == base else "merged"
        return {"data": data, "receipt": None}

    def accept(self, prepared):
        realm, request = self.realm, self.context["request"]
        realm.prepared.append(detached(prepared))
        callback, realm.before_accept = realm.before_accept, None
        if callback:
            callback()
        if prepared["expected_head"] != realm.head:
            raise StaleHead(realm.head)
        candidate = self._candidate(prepared)
        previous = detached(realm.head)
        revision = candidate["canonical_document"]["parent_revision"]["revision_id"]
        base = {"revision_id": request["base_revision_id"],
                "publication_id": request["base_publication_id"]}
        data = {**candidate, "status": "accepted" if previous == base else "rebased",
                "base_revision_id": base["revision_id"], "base_publication_id": base["publication_id"],
                "previous_revision_id": previous["revision_id"],
                "previous_publication_id": previous["publication_id"], "accepted_revision_id": revision,
                "rebased_onto": None if previous == base else previous, "publisher": "actor-1",
                "source": request["source"], "rationale": request["rationale"]}
        publication = "publication-" + str(len(realm.entries))
        receipt = {"receipt_id": publication, "command_kind": "timeline.commit",
                   "idempotency_key": self.context["idempotency_key"],
                   "request_hash": self.context["request_hash"], "project_id": "project-1",
                   "project_seq": [len(realm.entries)], "event_ids": ["event-" + publication],
                   "result": data, "created_at": "2026-10-19T00:00:00Z"}
        result = {"data": data, "receipt": receipt}
        realm.documents[revision] = detached(candidate["canonical_document"])
        realm.entries.append({"publication_id": publication, "revision_id": revision,
                              "previous": previous, "intent": "recorded",
                              "records": detached(prepared["records"]), "receipt": detached(receipt),
                              "provider_version": prepared["provider_version"],
                              "compiler_version": prepared["compiler_version"], "publisher": "actor-1",
                              "source": request["source"], "rationale": request["rationale"]})
        realm.head = {"revision_id": revision, "publication_id": publication}
        realm.retained_sources.update(detached(self.captures))
        realm.accepted.append(result)
        return result


def inverse(publications):
    return {"schema_version": 1, "op": "timeline.inverse", "version": "1",
            "select": None, "args": {"publications": publications}}


def trim_document():
    document = document_fixture(shared=False)
    payload = document["parent_revision"]["payload"]
    for rows in (payload["clips"], payload["config"]["clips"]):
        rows[0].update(at=1, **{"from": 2, "to": 6, "speed": 2, "hold": 17})
    payload["registry"] = copy.deepcopy(payload["registry"])
    payload["registry"]["assets"]["picture"].update(type="image", duration=10)
    document["parent_revision"]["content_digest"] = _digest(payload)
    return document


def test_provider_accepts_trim_after_move_and_undoes_only_trim():
    realm = DetachedRealm(trim_document())
    moved = realm.call([realm.move()])
    command = capture_clip_trim(
        bundle_from_document(realm.documents[realm.head["revision_id"]]),
        "parent/clip:clip-a", in_s=1, out_s=5)
    accepted = realm.call([command])
    row = bundle_from_document(realm.documents[realm.head["revision_id"]])["parent"]["clips"][0]
    assert row["at"] == 3.003
    assert row["hold"] == 4
    assert row["app"]["astrid"]["fixed_start"] is True
    assert "for" not in row["app"]["astrid"]
    undone = realm.call([inverse([accepted["receipt"]["receipt_id"]])])
    assert undone["data"]["canonical_document"] == moved["data"]["canonical_document"]


def test_legacy_rebase_records_landing_step_metadata():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    command = realm.move()
    realm.call([track_create("unrelated-track")])
    landing = copy.deepcopy(realm.head)

    accepted = realm.call([command], base=base)

    assert accepted["data"]["rebased_onto"] == landing
    records = realm.entries[-1]["records"]
    assert records["steps"] and records["steps"][0]["dependencies"]
    bindings = records["commands"][0]["bindings"]
    assert bindings
    assert records["steps"][0]["patch"]


def test_replay_accepts_legacy_request_head_and_builds_steps():
    realm = DetachedRealm()
    command = realm.move()
    context = realm.context([command])
    base = copy.deepcopy(realm.head)
    request = context["request"]
    input_bundle = bundle_from_document(realm.documents[base["revision_id"]])
    original = execute(input_bundle, [command],
                       commit_request=request, evaluated_head=base)
    landing = {"revision_id": "landing-revision", "publication_id": "landing-publication"}

    replayed = replay(original, input_bundle, commit_request=request, evaluated_head=landing)

    assert replayed.steps and replayed.steps[0]["patch"]
    assert replayed.steps[0]["dependencies"]


def track_create(identity="reserved-track", owner="parent"):
    return {"schema_version": 1, "op": "track.create", "version": "1",
            "select": owner, "args": {"row": {"id": identity, "kind": "visual",
                                                "opaque": {"clear": None}}}}


def test_retired_identity_refuses_new_create_but_guarded_redo_restores_exact_state():
    realm = DetachedRealm()
    original = detached(realm.documents["parent-1"])
    created = realm.call([track_create()])
    deleted = realm.call([inverse([realm.head["publication_id"]])])
    document = deleted["data"]["canonical_document"]
    assert document["parent_revision"]["payload"] == original["parent_revision"]["payload"]
    for key in ("shot_revisions", "internal_timeline_revisions", "dependency_manifest"):
        assert document[key] == original[key]
    head, entries = detached(realm.head), detached(realm.entries)
    with pytest.raises(CommitRejected) as error:
        realm.call([track_create()])
    assert error.value.failure["details"]["reason"] == "identity_collision"
    assert error.value.failure["details"]["status"] == "conflict"
    assert realm.head == head and realm.entries == entries
    assert len(realm.accepted) == len(realm.prepared) == 2
    restored = realm.call([inverse([head["publication_id"]])])
    assert restored["data"]["canonical_document"] == created["data"]["canonical_document"]


def test_factory_matches_fixed_installed_runtime_contract():
    provider = create_runtime_commit_service()
    assert provider.provider_id == "astrid-v1"
    assert type(provider.interface_version) is int and provider.interface_version == 1
    assert provider.contract_digest == "exact-installed-runtime-digest"
    assert not hasattr(provider, "requires_scene_evaluation")


def test_move_inverse_returns_exact_runtime_result_and_complete_original_state():
    realm = DetachedRealm()
    original = copy.deepcopy(realm.documents["parent-1"])
    result = realm.call([realm.move()])
    assert result is realm.accepted[-1]
    assert result["receipt"]["result"] == result["data"]
    assert result["data"]["commands"] == [realm.entries[-1]["records"]["commands"][0]["command"]]
    assert result["data"]["canonical_document"]["shot_revisions"] == original["shot_revisions"]
    undo = realm.call([inverse([realm.head["publication_id"]])])
    assert undo is realm.accepted[-1]
    restored = undo["data"]["canonical_document"]
    assert restored["parent_revision"]["payload"] == original["parent_revision"]["payload"]
    assert restored["shot_revisions"] == original["shot_revisions"]
    assert restored["internal_timeline_revisions"] == original["internal_timeline_revisions"]
    assert realm.entries[-1]["records"]["undo_of"] == [result["receipt"]["receipt_id"]]
    redo = realm.call([inverse([realm.head["publication_id"]])])
    assert redo["data"]["canonical_document"]["parent_revision"]["payload"] == (
        result["data"]["canonical_document"]["parent_revision"]["payload"])


def test_stale_disjoint_rebases_and_same_clip_conflicts():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    stale = realm.move("parent/clip:clip-b", 8.008)
    conflicting = realm.move(on=4.004)
    first = realm.call([realm.move()])
    result = realm.call([stale], base=base)
    assert result["data"]["status"] == "rebased"
    assert result["data"]["rebased_onto"]["publication_id"] == first["receipt"]["receipt_id"]
    positions = {row["id"]: row["at"] for row in result["data"]["canonical_document"][
        "parent_revision"]["payload"]["config"]["clips"]}
    assert positions == {"clip-a": 3.003, "clip-b": 8.008}
    count = len(realm.entries)
    with pytest.raises(CommitRejected) as error:
        realm.call([conflicting], base=base)
    assert error.value.failure["details"]["status"] == "conflict"
    assert len(realm.entries) == count


def test_stale_head_retries_original_plan_and_created_ids_once(monkeypatch):
    from astrid.sdk.timeline import commands
    from astrid.sdk.timeline.capture import capture_track_create

    realm = DetachedRealm()
    original_execute, executions = commands.execute, []

    def execute(bundle, calls, **kwargs):
        if sys._getframe(1).f_globals["__name__"] == "astrid.sdk.timeline.commit":
            executions.append(detached(calls))
        return original_execute(bundle, calls, **kwargs)

    monkeypatch.setattr(commands, "execute", execute)
    command = capture_track_create(bundle_from_document(realm.documents["parent-1"]), "parent",
                                   {"id": "created-track", "kind": "visual"})
    realm.before_accept = lambda: realm.call([realm.move()])
    result = realm.call([command])
    # Provider records this request once; the other call is the intervening
    # request. Frontend capture and core replay use the real same engine.
    assert len(executions) == 2
    assert result["data"]["status"] == "rebased"
    attempts = [row for row in realm.prepared
                if row["records"]["commands"][0]["command"]["op"] == "track.create"]
    assert len(attempts) == 2
    assert attempts[0]["records"] == attempts[1]["records"]
    assert attempts[0]["id_map"] == attempts[1]["id_map"] == {}


def test_dry_run_uses_runtime_preview_and_writes_nothing():
    realm = DetachedRealm()
    result = realm.call([realm.move()], dry_run=True)
    assert result["receipt"] is None and result["data"]["status"] == "preview"
    assert realm.head["publication_id"] == "bootstrap"
    assert realm.accepted == realm.prepared == []


def test_original_capture_claims_are_verified_before_refresh():
    realm = DetachedRealm()
    command = realm.move()
    realm.call([realm.move(on=4.004)])
    with pytest.raises(CommitRejected) as error:
        realm.call([command])
    assert error.value.failure["details"]["reason"] == "dependency_changed"
    assert error.value.failure["details"]["status"] == "conflict"
    assert len(realm.accepted) == 1


def test_undo_detects_aba_and_unchanged_write_publications():
    for unchanged in (False, True):
        realm = DetachedRealm()
        first = realm.call([realm.move()])
        target = first["receipt"]["receipt_id"]
        if not unchanged:
            realm.call([realm.move(on=4.004)])
        before = detached(realm.head)
        realm.call([realm.move(on=3.003)])
        if unchanged:
            assert realm.head["revision_id"] == before["revision_id"]
            assert realm.head["publication_id"] != before["publication_id"]
        with pytest.raises(CommitRejected) as error:
            realm.call([inverse([target])])
        assert error.value.failure["details"]["reason"] == "intervening_write"


def test_timeline_scope_inverse_binding_skips_entity_dispatch_but_keeps_guards(monkeypatch):
    from astrid.sdk.timeline._command_state import CommandError, State, present
    from astrid.sdk.timeline.inverse import _validate_inputs, check_intervening

    bundle = bundle_from_document(document_fixture())
    pin = {key: bundle["base_parent"][key] for key in ("revision_id", "content_digest")}
    scope = {"ref": "timeline", "path": [], "state": present(pin)}
    binding = {"key": "commit.read", "ref": "timeline", "path": [],
               "physical_ref": "parent", **pin}
    record = {"command": {"op": "timeline.init"}, "writes": [], "reads": [scope],
              "selections": [], "bindings": [binding], "created": []}

    mismatched = copy.deepcopy(record)
    mismatched["bindings"][0]["revision_id"] = "different-revision"
    with pytest.raises(CommandError, match="unsupported entity reference 'timeline'"):
        _validate_inputs(State(bundle), mismatched, [])

    original_entity = State.entity

    def entity(state, ref, *, optional=False):
        if ref == "timeline":
            raise AssertionError("timeline scope must not dispatch through State.entity")
        return original_entity(state, ref, optional=optional)

    monkeypatch.setattr(State, "entity", entity)
    _validate_inputs(State(bundle), record, [])

    later = {"writes": [{"ref": "parent", "path": [], "before": present({}),
                          "after": present({"changed": True})}]}
    with pytest.raises(CommandError, match="intervening publication wrote a guarded address"):
        check_intervening([record], [later])


def test_dependent_unsent_original_guards_allow_equal_state_aba_at_fresh_base():
    realm = DetachedRealm()
    realm.call([realm.move()])
    original = realm.move(on=5.005)
    realm.call([realm.move(on=4.004)])
    realm.call([realm.move(on=3.003)])
    result = realm.call([original])
    assert result["data"]["status"] == "accepted"
    assert result["data"]["commands"][0]["expect"] == original["expect"]
    assert result["data"]["commands"][0]["reads"] == original["reads"]


def test_paged_history_and_multiple_target_publications_are_one_inverse():
    realm = DetachedRealm()
    realm.page_size = 1
    initial = detached(realm.documents["parent-1"]["parent_revision"]["payload"])
    first = realm.call([realm.move()])
    second = realm.call([realm.move("parent/clip:clip-b", 8.008)])
    result = realm.call([inverse([first["receipt"]["receipt_id"], second["receipt"]["receipt_id"]])])
    assert result["data"]["canonical_document"]["parent_revision"]["payload"] == initial


def test_reversed_inverse_target_order_is_refused_without_partial_acceptance():
    realm = DetachedRealm()
    first = realm.call([realm.move()])
    second = realm.call([realm.move("parent/clip:clip-b", 8.008)])
    head = detached(realm.head)
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse([second["receipt"]["receipt_id"], first["receipt"]["receipt_id"]])])
    assert error.value.failure["details"]["reason"] == "invalid_inverse_order"
    assert realm.head == head and len(realm.accepted) == 2


def test_client_cannot_supply_inverse_writes():
    realm = DetachedRealm()
    first = realm.call([realm.move()])
    head = detached(realm.head)
    command = inverse([first["receipt"]["receipt_id"]])
    command["args"]["writes"] = [{"ref": "parent/clip:clip-b", "path": ["at"],
                                  "after": {"state": "present", "value": 999}}]
    with pytest.raises(CommitRejected) as error:
        realm.call([command])
    assert error.value.failure["details"]["reason"] == "invalid_edit_command"
    assert realm.head == head and len(realm.accepted) == 1


@pytest.mark.parametrize("damage", ["missing_base_entry", "unknown_writes", "missing_target",
                                   "broken_ancestry", "foreign_scope"])
def test_missing_or_inconsistent_history_and_scope_fail_closed(damage):
    realm = DetachedRealm()
    base = detached(realm.head)
    stale = realm.move("parent/clip:clip-b", 8.008)
    first = realm.call([realm.move()])
    if damage == "missing_base_entry":
        realm.entries.pop(0)
    elif damage == "unknown_writes":
        realm.entries[-1].update(intent="legacy_unknown", records=None)
    elif damage == "missing_target":
        stale = inverse(["unknown-publication"])
    elif damage == "broken_ancestry":
        realm.entries[-1]["previous"]["publication_id"] = "other-history"
    elif damage == "foreign_scope":
        realm.documents[base["revision_id"]]["project_id"] = "foreign-project"
    with pytest.raises(CommitRejected):
        realm.call([stale], base=base)
    assert realm.accepted == [first]


def test_runtime_refusal_and_exception_propagate_for_ledger_reconciliation(monkeypatch):
    realm = DetachedRealm()
    context = realm.context([realm.move()])
    port = DetachedPort(realm, context)
    provider = create_runtime_commit_service()
    for error in (refusal("authority_revoked"), OSError("ambiguous post-commit hook")):
        def fail(prepared):
            raise error
        monkeypatch.setattr(port, "accept", fail)
        with pytest.raises(type(error)) as caught:
            provider.commit(context, port)
        assert caught.value is error


def test_restore_is_recorded_sole_and_strictly_pair_guarded():
    realm = DetachedRealm()
    restore = {"schema_version": 1, "op": "timeline.restore", "version": "1", "select": None,
               "args": {key: realm.documents["parent-1"]["parent_revision"][key]
                        for key in ("revision_id", "content_digest")}}
    original = detached(realm.documents["parent-1"])
    realm.call([realm.move()])
    restored = realm.call([restore])
    assert restored["data"]["canonical_document"] == original
    assert realm.entries[-1]["records"]["restored_from"] == restore["args"]
    assert realm.entries[-1]["records"]["commands"][0]["writes"]
    assert restored["data"]["status"] == "accepted"
    with pytest.raises(CommitRejected) as error:
        realm.call([restore, realm.move()])
    assert error.value.failure["details"]["reason"] == "invalid_commands"
    with pytest.raises(CommitRejected) as error:
        realm.call([restore], base={**realm.head, "publication_id": "older-publication"})
    assert error.value.failure["details"]["reason"] == "stale_head"


def source_fixture(kind):
    from astrid.sdk.media_transcript import transcript_document_id

    if kind == "transcript":
        content = {"schema": "media.transcript/v1", "media_id": MEDIA, "source_id": "whisper",
                   "lines": [{"id": "line-1", "words": [
                       {"id": "word-1", "start": 4, "end": 5, "text": "original"}]}]}
        document_id = transcript_document_id("project-1", MEDIA, "whisper")
        document_kind = "media.transcript"
    else:
        content = {"schema": "timeline.config/v1", "settings": {"original": True}}
        document_id, document_kind = "timeline:config", "timeline.config"
    pin = {"document_id": document_id, "version": 1, "content_digest": _digest(content)}
    return {"pin": pin, "document": {
        "document_id": document_id, "project_id": "project-1", "kind": document_kind,
        "version": 1, "content": content, "created_at": "2026-10-18T00:00:00Z",
        "updated_at": "2026-10-18T00:00:00Z"}}


@pytest.mark.parametrize("kind", ["transcript", "config"])
def test_retained_input_closure_survives_document_overwrite_and_new_provider(kind):
    source = source_fixture(kind)
    document = document_fixture()
    document["dependency_manifest"]["documents"] = [source["pin"]]
    shot = document["shot_revisions"][0]
    shot["payload"]["text_bindings"][0]["source_document"] = detached(source["pin"])
    shot["payload"]["text_bindings"][0]["source_window"] = {"start": 4, "end": 9}
    if kind == "transcript":
        shot["payload"]["text_bindings"][0]["word_ids"] = ["word-1"]
    shot["content_digest"] = _digest(shot["payload"])
    document["dependency_manifest"]["shots"][0]["content_digest"] = shot["content_digest"]
    realm = DetachedRealm(document)
    key = _digest(source["pin"])
    realm.live_sources[key] = detached(source)
    first = realm.call([realm.move()])
    target = first["receipt"]["receipt_id"]
    assert realm.retained_sources[key] == source
    # Producer overwrites live Documents. Fresh ports/providers must still
    # consume accepted v1 bytes, including the original word/source identity.
    updated = detached(source)
    updated["document"]["version"] = updated["pin"]["version"] = 2
    updated["document"]["content"]["replacement"] = "v2"
    updated["pin"]["content_digest"] = _digest(updated["document"]["content"])
    realm.live_sources = {_digest(updated["pin"]): updated}
    second = realm.call([realm.move("parent/clip:clip-b", 8.008)])
    undo = realm.call([inverse([target])])
    assert undo["data"]["canonical_document"]["shot_revisions"] == document["shot_revisions"]
    assert second["data"]["canonical_document"]["dependency_manifest"]["documents"] == [source["pin"]]
    assert undo["data"]["canonical_document"]["dependency_manifest"]["documents"] == [source["pin"]]
    assert realm.retained_sources[key] == source
    assert sum(call[0] == kind for call in realm.reads) == 3


@pytest.mark.parametrize("damage", ["missing", "version", "digest", "project", "source_identity",
                                   "kind", "pin"])
def test_missing_or_malformed_transcript_source_is_refused(damage):
    source = source_fixture("transcript")
    document = document_fixture()
    document["dependency_manifest"]["documents"] = [source["pin"]]
    realm = DetachedRealm(document)
    key = _digest(source["pin"])
    if damage != "missing":
        realm.live_sources[key] = detached(source)
        row = realm.live_sources[key]
        if damage == "version":
            row["document"]["version"] = 2
        elif damage == "digest":
            row["document"]["content"]["lines"] = []
        elif damage == "project":
            row["document"]["project_id"] = "foreign-project"
        elif damage == "source_identity":
            row["document"]["content"]["source_id"] = "other-source"
            row["pin"]["content_digest"] = _digest(row["document"]["content"])
            document["dependency_manifest"]["documents"] = [detached(row["pin"])]
            realm.documents["parent-1"] = detached(document)
            realm.live_sources = {_digest(row["pin"]): row}
        elif damage == "kind":
            row["document"]["kind"] = "arbitrary.document"
        elif damage == "pin":
            row["pin"]["document_id"] = "foreign-source"
    with pytest.raises(CommitRejected):
        realm.call([realm.move()])
    assert realm.accepted == [] and realm.head["publication_id"] == "bootstrap"


def test_three_head_races_exhaust_budget_without_changing_plan(monkeypatch):
    realm = DetachedRealm()
    context = realm.context([realm.move()])
    port = DetachedPort(realm, context)
    attempts = []

    def stale(prepared):
        attempts.append(detached(prepared))
        raise StaleHead(realm.head)

    monkeypatch.setattr(port, "accept", stale)
    with pytest.raises(CommitRejected) as error:
        create_runtime_commit_service().commit(context, port)
    assert error.value.failure["details"]["reason"] == "head_busy"
    assert len(attempts) == 3
    assert attempts == [attempts[0]] * 3
    assert realm.accepted == []


def test_inverse_rebases_only_over_proven_disjoint_new_writes():
    realm = DetachedRealm()
    original = detached(realm.documents["parent-1"]["parent_revision"]["payload"])
    first = realm.call([realm.move()])
    base = detached(realm.head)
    other = realm.call([realm.move("parent/clip:clip-b", 8.008)])
    undo = realm.call([inverse([first["receipt"]["receipt_id"]])], base=base)
    assert undo["data"]["status"] == "rebased"
    assert undo["data"]["rebased_onto"]["publication_id"] == other["receipt"]["receipt_id"]
    clips = undo["data"]["canonical_document"]["parent_revision"]["payload"]["config"]["clips"]
    assert clips[0] == original["config"]["clips"][0]
    assert clips[1]["at"] == 8.008


def test_shared_occurrence_move_does_not_replace_unedited_sibling():
    document = document_fixture()
    for occurrence in document["parent_revision"]["payload"]["occurrences"]:
        occurrence.update(source_offset=0, speed=1)
    document["parent_revision"]["content_digest"] = _digest(document["parent_revision"]["payload"])
    realm = DetachedRealm(document)
    original = detached(realm.documents["parent-1"])
    first = realm.call([realm.move("occurrence:occ-a/clip:clip-a", 3.003)])
    changed = first["data"]["canonical_document"]
    occurrences = changed["parent_revision"]["payload"]["occurrences"]
    assert occurrences[1] == original["parent_revision"]["payload"]["occurrences"][1]
    assert original["shot_revisions"][0] in changed["shot_revisions"]
    assert original["internal_timeline_revisions"][0] in changed["internal_timeline_revisions"]
    undo = realm.call([inverse([first["receipt"]["receipt_id"]])])
    reopened = bundle_from_document(undo["data"]["canonical_document"])
    shot = reopened["shots"][reopened["placements"][0]["shot_id"]]
    assert shot["internal_timeline"] == original["internal_timeline_revisions"][0]["payload"]


def test_shared_sibling_move_rebases_after_other_occurrence_materializes():
    document = document_fixture()
    for occurrence in document["parent_revision"]["payload"]["occurrences"]:
        occurrence.update(source_offset=0, speed=1)
    document["parent_revision"]["content_digest"] = _digest(document["parent_revision"]["payload"])
    realm = DetachedRealm(document)
    base = detached(realm.head)
    stale = realm.move("occurrence:occ-b/clip:clip-a", 13.013)
    first = realm.call([realm.move("occurrence:occ-a/clip:clip-a", 3.003)])
    result = realm.call([stale], base=base)
    assert result["data"]["status"] == "rebased"
    assert result["data"]["rebased_onto"]["publication_id"] == first["receipt"]["receipt_id"]


def test_move_reload_and_inverse_import_no_renderer():
    import os
    import subprocess

    script = r'''
import sys
class Blocked:
    def find_spec(self, fullname, *args):
        if (fullname.startswith(('astrid.packs.rendering', 'astrid.core.rendering',
                                 'astrid.sdk.authoring_render_preview',
                                 'astrid.core.timeline.compatibility', 'composition'))
                or 'scene' in fullname or fullname.startswith(('node', 'remotion'))):
            raise AssertionError('renderer dependency: ' + fullname)
sys.meta_path.insert(0, Blocked())
import pytest
from tests.helpers.runtime import use_runtime
use_runtime()
import runtime_protocol.platform
from tests.sdk.test_timeline_commit_provider import DetachedRealm, inverse, restore, runtime_abi
runtime_abi.__wrapped__(pytest.MonkeyPatch())
realm = DetachedRealm()
first = realm.call([realm.move()])
realm.call([inverse([first['receipt']['receipt_id']])])
restored = realm.call([restore(realm.documents['parent-1'])])
undone = realm.call([inverse([restored['receipt']['receipt_id']])])
realm.call([inverse([undone['receipt']['receipt_id']])])
assert len(realm.entries) == 6
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr


def test_occupied_track_identity_conflicts_without_acceptance():
    realm = DetachedRealm()
    realm.call([track_create()])
    head = detached(realm.head)
    with pytest.raises(CommitRejected) as error:
        realm.call([track_create()])
    assert error.value.failure["details"]["reason"] == "identity_collision"
    assert realm.head == head and len(realm.accepted) == len(realm.prepared) == 1


def test_structural_preview_does_not_reserve_and_stale_disjoint_rebase_keeps_identity():
    realm = DetachedRealm()
    command, base = track_create(), detached(realm.head)
    before = detached(realm.entries)
    preview = realm.call([command], dry_run=True)
    assert preview["receipt"] is None
    assert realm.entries == before and realm.accepted == realm.prepared == []
    realm.call([realm.move()])
    result = realm.call([command], base=base)
    assert result["data"]["status"] == "rebased"
    record = realm.entries[-1]["records"]["commands"][0]
    assert record["created"] == [{"ref": "parent/track:reserved-track", "kind": "track",
                                   "owner": "parent"}]
    assert record["command"]["args"] == command["args"]
    parent = result["data"]["canonical_document"]["parent_revision"]["payload"]
    assert parent["config"]["tracks"][-1] == command["args"]["row"]
    assert parent["clips"][0]["at"] == 3.003
    assert preview["data"]["canonical_document"]["parent_revision"]["payload"][
        "config"]["tracks"][-1] == command["args"]["row"]
    assert result["data"]["id_map"] == {}


def test_cas_race_create_delete_does_not_release_identity_for_original_request():
    realm = DetachedRealm()
    command = track_create()

    def retire():
        realm.call([track_create()])
        realm.call([inverse([realm.head["publication_id"]])])

    realm.before_accept = retire
    with pytest.raises(CommitRejected) as error:
        realm.call([command])
    assert error.value.failure["details"]["reason"] == "intervening_write"
    assert len(realm.accepted) == 2
    assert realm.prepared[0]["records"]["commands"][0]["created"] == [
        {"ref": "parent/track:reserved-track", "kind": "track", "owner": "parent"}]
    head = detached(realm.head)
    with pytest.raises(CommitRejected) as error:
        realm.call([command])
    assert error.value.failure["details"]["reason"] == "identity_collision"
    assert realm.head == head and len(realm.accepted) == 2


@pytest.mark.parametrize("damage", ["missing_entry", "ancestry", "unknown_intent", "records",
                                   "missing_created", "null_created", "created_item", "created_claim"])
def test_structural_reservation_history_fails_closed(damage):
    realm = DetachedRealm()
    realm.page_size = 1
    realm.call([track_create()])
    realm.call([inverse([realm.head["publication_id"]])])
    entry = realm.entries[1]
    if damage == "missing_entry":
        realm.entries.pop(1)
    elif damage == "ancestry":
        entry["previous"]["publication_id"] = "missing"
    elif damage == "unknown_intent":
        entry["intent"] = "unknown"
    elif damage == "records":
        entry["records"] = None
    elif damage == "missing_created":
        del entry["records"]["commands"][0]["created"]
    elif damage == "null_created":
        entry["records"]["commands"][0]["created"] = None
    elif damage == "created_item":
        entry["records"]["commands"][0]["created"] = [{"ref": "parent/track:reserved-track"}]
    elif damage == "created_claim":
        entry["records"]["commands"][0]["created"] = []
    before, head = detached(realm.entries), detached(realm.head)
    with pytest.raises(CommitRejected) as error:
        realm.call([track_create("different-new-id")])
    assert error.value.failure["details"]["reason"] == "history_unavailable"
    assert realm.entries == before and realm.head == head and len(realm.accepted) == 2


def test_known_legacy_prefix_is_a_boundary_but_unknown_intent_after_recording_refuses():
    realm = DetachedRealm()
    realm.entries[0]["intent"] = "legacy_unknown"
    realm.call([track_create()])
    realm.call([inverse([realm.head["publication_id"]])])
    realm.entries[-1].update(intent="legacy_unknown", records=None)
    head = detached(realm.head)
    with pytest.raises(CommitRejected) as error:
        realm.call([track_create("different-new-id")])
    assert error.value.failure["details"]["reason"] == "history_unavailable"
    assert realm.head == head and len(realm.accepted) == 2


# Pre-upgrade event kinds that the legacy prefix stands for. The SDK never
# reads them: a legacy row is opaque (intent legacy_unknown, no records).
LEGACY_KINDS = ("clip.retimed", "clip.swapped", "clip.text_set", "clip.annotated",
                "effect.tuned", "theme.overridden", "pool.asset_scored",
                "arrangement.replaced", "timeline.config_replaced", "timeline.reverted")


def test_legacy_history_undo_and_restore_use_revisions_or_refuse_visibly():
    import importlib.util
    from typing import get_args

    from astrid.core.timeline.events.schema.types import TimelineEventKind

    assert set(LEGACY_KINDS) <= set(get_args(TimelineEventKind))
    assert importlib.util.find_spec("astrid.core.timeline.inverses") is None
    realm = DetachedRealm()
    original = detached(realm.documents["parent-1"])
    for on, kind in zip([3.003, 4.004, 5.005, 6.006, 7.007, 8.008, 9.009, 10.01, 11.011, 12.012],
                        LEGACY_KINDS):
        realm.call([realm.move(on=on)])
        realm.entries[-1].update(intent="legacy_unknown", records=None)
        realm.entries[-1]["receipt"]["command_kind"] = kind
    legacy = realm.head["publication_id"]
    upgraded = detached(realm.head)
    post = realm.call([realm.move("parent/clip:clip-b", 13.013)])
    recorded = post["receipt"]["receipt_id"]
    head, entries = detached(realm.head), detached(realm.entries)
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse([legacy, recorded])])
    assert error.value.failure["details"]["reason"] == "history_unavailable"
    assert realm.head == head and realm.entries == entries and len(realm.accepted) == 11
    undone = realm.call([inverse([recorded])])
    assert undone["data"]["status"] == "accepted"
    assert undone["data"]["canonical_document"]["parent_revision"]["payload"] == (
        realm.documents[upgraded["revision_id"]]["parent_revision"]["payload"])
    restored = realm.call([restore(original)])
    assert restored["data"]["canonical_document"] == original
    legacy_rows = realm.entries[1:11]
    assert all(entry["records"] is None and entry["intent"] == "legacy_unknown" for entry in legacy_rows)
    assert [entry["receipt"]["command_kind"] for entry in legacy_rows] == list(LEGACY_KINDS)
    assert len(realm.entries) == 14 and len(realm.accepted) == 13


def test_moves_do_not_scan_reservation_history():
    realm = DetachedRealm()
    realm.call([track_create()])
    realm.reads.clear()
    realm.call([realm.move()])
    assert not any(read[0] == "history" for read in realm.reads)


def test_reservations_are_scoped_and_guarded_inverse_does_not_authorize_other_creates():
    realm = DetachedRealm()
    realm.page_size = 1
    realm.call([track_create("other-retired")])
    realm.call([inverse([realm.head["publication_id"]])])
    realm.call([track_create()])
    realm.call([inverse([realm.head["publication_id"]])])
    deleted = realm.head["publication_id"]
    realm.call([track_create(owner="occurrence:occ-a")])
    head = detached(realm.head)
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse([deleted]), track_create("other-retired")])
    assert error.value.failure["details"]["reason"] == "identity_collision"
    assert realm.head == head and len(realm.accepted) == 5


def test_batch_inverse_delete_cannot_reallocate_its_retired_identity():
    realm = DetachedRealm()
    realm.call([track_create()])
    head = detached(realm.head)
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse([head["publication_id"]]), track_create()])
    assert error.value.failure["details"]["reason"] == "identity_collision"
    assert realm.head == head and len(realm.accepted) == len(realm.prepared) == 1


def restore(document):
    return {"schema_version": 1, "op": "timeline.restore", "version": "1", "select": None,
            "args": {key: document["parent_revision"][key]
                     for key in ("revision_id", "content_digest")}}


def test_restore_dry_run_digest_and_accept_race():
    realm = DetachedRealm()
    original = detached(realm.documents["parent-1"])
    command = restore(original)
    realm.call([realm.move()])
    head = detached(realm.head)
    preview = realm.call([command], dry_run=True)
    assert preview["receipt"] is None
    assert preview["data"]["canonical_document"] == original
    assert realm.head == head and len(realm.accepted) == 1
    bad = detached(command)
    bad["args"]["content_digest"] = MEDIA
    with pytest.raises(CommitRejected) as error:
        realm.call([bad])
    assert error.value.failure["details"]["reason"] == "invalid_document"
    assert realm.head == head
    realm.before_accept = lambda: realm.call([realm.move(on=4.004)])
    with pytest.raises(CommitRejected) as error:
        realm.call([command])
    assert error.value.failure["details"]["reason"] == "stale_head"
    assert len(realm.accepted) == 2
    assert len([p for p in realm.prepared if p["records"]["restored_from"]]) == 1


def test_restore_undo_redo_exact_full_closure_and_unknown_fields():
    document = document_fixture()
    for occurrence in document['parent_revision']['payload']['occurrences']:
        occurrence.update(source_offset=0, speed=1)
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    realm = DetachedRealm(document)
    original = detached(realm.documents["parent-1"])
    realm.call([realm.move("occurrence:occ-a/clip:clip-a")])
    changed = realm.call([realm.move()])["data"]["canonical_document"]
    restored = realm.call([restore(original)])
    assert restored["data"]["canonical_document"] == original
    record = realm.entries[-1]["records"]["commands"][0]
    assert record["created"] == []  # Historical authority is not a fresh allocation.
    assert record["writes"] == [{"ref": "timeline", "path": [],
        "before": {"state": "present", "value": restore(changed)["args"]},
        "after": {"state": "present", "value": restore(original)["args"]}}]
    undone = realm.call([inverse([restored["receipt"]["receipt_id"]])])
    assert undone["data"]["canonical_document"] == changed
    redone = realm.call([inverse([undone["receipt"]["receipt_id"]])])
    assert redone["data"]["canonical_document"] == original
    assert realm.entries[-1]["records"]["restored_from"] is None


@pytest.mark.parametrize('v2', [False, True], ids=['legacy-dual-proof', 'v2-effect-proof'])
@pytest.mark.parametrize('selected', [False, True], ids=['whole', 'step'])
def test_restore_inverse_proof_representations_allow_preview_and_redo(v2, selected):
    from tests.editing_conformance.test_doc_patch_v2 import submit

    document = document_fixture()
    for occurrence in document['parent_revision']['payload']['occurrences']:
        occurrence.update(source_offset=0, speed=1)
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    realm = DetachedRealm(document)
    original = detached(realm.documents['parent-1'])
    realm.call([realm.move('occurrence:occ-a/clip:clip-a')])
    changed = realm.call([realm.move()])['data']['canonical_document']
    restored = realm.call([restore(original)])
    command = inverse([restored['receipt']['receipt_id']])
    # The default native request has no v2 controls. Exercise both genuine
    # provider representations without rewriting accepted records or bindings.
    undone = (submit(realm, command, label='Undo restore') if v2 else realm.call([command]))
    assert undone['data']['canonical_document'] == changed
    record = realm.entries[-1]['records']['commands'][0]
    assert any(binding['key'] == 'inverse.stage' for binding in record['bindings'])
    assert any(binding['key'] == 'commit.effect' for binding in record['bindings'])
    assert any(binding['key'] == 'commit.materialization' for binding in record['bindings']) is (not v2)
    command = inverse([undone['receipt']['receipt_id']])
    if selected:
        command['args']['steps'] = {undone['receipt']['receipt_id']: ['edit']}
    before = detached((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
    preview = realm.call([command], dry_run=True)
    assert preview['receipt'] is None
    assert preview['data']['would_status'] == 'accepted'
    assert preview['data']['canonical_document'] == original
    assert before == detached((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
    redone = realm.call([command])
    assert redone['data']['canonical_document'] == original
    assert redone['data']['canonical_document'] == preview['data']['canonical_document']
    assert redone['receipt']['result'] == redone['data']
    assert realm.entries[:-1] == before[1]


@pytest.mark.parametrize("later", ["overlap", "same-value", "aba", "child", "create"])
def test_restore_inverse_refuses_all_intervening_writes(later):
    document = document_fixture()
    for occurrence in document['parent_revision']['payload']['occurrences']:
        occurrence.update(source_offset=0, speed=1)
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    realm = DetachedRealm(document)
    original = detached(realm.documents["parent-1"])
    realm.call([realm.move()])
    restored = realm.call([restore(original)])
    if later == "aba":
        realm.call([realm.move(on=4.004)])
    if later == "create":
        realm.call([track_create()])
    else:
        ref = "occurrence:occ-a/clip:clip-a" if later == "child" else "parent/clip:clip-a"
        realm.call([realm.move(ref, on=1 if later in {"same-value", "aba"} else 3.003)])
    head = detached(realm.head)
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse([restored["receipt"]["receipt_id"]])])
    assert error.value.failure["details"]["reason"] == "intervening_write"
    assert realm.head == head


def test_restore_is_intervening_write_for_ordinary_inverse_and_rebase():
    realm = DetachedRealm()
    first = realm.call([realm.move()])
    stale = detached(realm.head)
    command = realm.move("parent/clip:clip-b", on=6.006)
    realm.call([restore(realm.documents[stale["revision_id"]])])  # Intentional same value.
    for calls, base in [([inverse([first["receipt"]["receipt_id"]])], None), ([command], stale)]:
        with pytest.raises(CommitRejected) as error:
            realm.call(calls, base=base)
        assert error.value.failure["details"]["reason"] == "intervening_write"


def test_restore_historical_ids_keep_ordinary_reservations():
    realm = DetachedRealm()
    created = realm.call([track_create()])["data"]["canonical_document"]
    realm.call([restore(realm.documents["parent-1"])])
    with pytest.raises(CommitRejected) as error:
        realm.call([track_create()])
    assert error.value.failure["details"]["reason"] == "identity_collision"
    restored = realm.call([restore(created)])
    assert restored["data"]["canonical_document"] == created
    assert realm.entries[-1]["records"]["commands"][0]["created"] == []


@pytest.mark.parametrize("kind", ["transcript", "config"])
def test_restore_and_inverse_capture_only_resulting_retained_inputs(kind):
    source = source_fixture(kind)
    original = document_fixture()
    original["dependency_manifest"]["documents"] = [source["pin"]]
    clean = document_fixture()
    clean["parent_revision"]["revision_id"] = "without-inputs"
    realm = DetachedRealm(original)
    realm.documents["without-inputs"] = detached(clean)
    realm.retained_sources[_digest(source["pin"])] = detached(source)
    # Restore must work after the live producer document is gone.
    removed = realm.call([restore(clean)])
    assert removed["data"]["canonical_document"] == clean
    assert not any(row[0] in {"transcript", "config"} for row in realm.reads)
    returned = realm.call([inverse([removed["receipt"]["receipt_id"]])])
    assert returned["data"]["canonical_document"] == original
    redone = realm.call([inverse([returned["receipt"]["receipt_id"]])])
    assert redone["data"]["canonical_document"] == clean
    restored = realm.call([restore(original)])
    assert restored["data"]["canonical_document"] == original
    assert realm.entries[-1]["records"]["commands"][0]["dependencies"][0]["pin"] == source["pin"]


@pytest.mark.parametrize("damage", ["digest", "missing_child", "media", "scope", "revision"])
def test_restore_rejects_tampered_or_foreign_historical_closure(damage):
    realm = DetachedRealm()
    command = restore(realm.documents["parent-1"])
    realm.call([realm.move()])
    source = realm.documents["parent-1"]
    if damage == "digest":
        source["shot_revisions"][0]["payload"]["unknown"] = "tampered"
    elif damage == "missing_child":
        source["internal_timeline_revisions"] = []
    elif damage == "media":
        source["dependency_manifest"]["media"] = []
    elif damage == "scope":
        source["project_id"] = "foreign-project"
    else:
        source["parent_revision"]["revision_id"] = "different-revision"
    head = detached(realm.head)
    with pytest.raises(CommitRejected) as error:
        realm.call([command])
    assert error.value.failure["details"]["reason"] == "invalid_document"
    assert realm.head == head and len(realm.accepted) == 1


def test_restore_same_revision_new_publication_rejects_old_pair():
    realm = DetachedRealm()
    command = restore(realm.documents["parent-1"])
    old = detached(realm.head)
    result = realm.call([command])
    assert realm.head["revision_id"] == old["revision_id"]
    assert realm.head["publication_id"] != old["publication_id"]
    with pytest.raises(CommitRejected) as error:
        realm.call([command], base=old)
    assert error.value.failure["details"]["reason"] == "stale_head"
    assert realm.accepted == [result]


@pytest.mark.parametrize("intent", ["bootstrap", "legacy_unknown"])
def test_restore_preserves_verified_legacy_history_boundary(intent):
    realm = DetachedRealm()
    realm.entries[0]["intent"] = intent
    original = detached(realm.documents["parent-1"])
    realm.call([realm.move()])
    restored = realm.call([restore(original)])
    assert restored["data"]["canonical_document"] == original
    assert realm.entries[0]["intent"] == intent and realm.entries[0]["records"] is None
    undone = realm.call([inverse([restored["receipt"]["receipt_id"]])])
    assert undone["data"]["canonical_document"] != original
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse(["bootstrap"])])
    assert error.value.failure["details"]["reason"] == "history_unavailable"


@pytest.mark.parametrize("damage", ["missing", "digest", "project", "pin"])
def test_restore_refuses_unavailable_or_corrupt_retained_input(damage):
    realm = DetachedRealm()
    realm.call([realm.move()])
    source = source_fixture("config")
    historical = realm.documents["parent-1"]
    historical["dependency_manifest"]["documents"] = [source["pin"]]
    if damage != "missing":
        capture = detached(source)
        if damage == "digest":
            capture["document"]["content"]["settings"] = "corrupt"
        elif damage == "project":
            capture["document"]["project_id"] = "foreign"
        else:
            capture["pin"]["version"] += 1
        realm.retained_sources[_digest(source["pin"])] = capture
    head = detached(realm.head)
    with pytest.raises(CommitRejected) as error:
        realm.call([restore(historical)])
    assert error.value.failure["details"]["reason"] == (
        "dependency_unavailable" if damage == "missing" else "invalid_document")
    assert realm.head == head and len(realm.accepted) == 1


def accept_recorded_fixture(realm, candidate, records, *, undo_of=None):
    """Publish recorder fixtures for structural commands outside the M1 registry."""
    from astrid.sdk.timeline.commit import document_from_bundle

    context = realm.context([record['command'] for record in records])
    document = document_from_bundle(candidate)
    return DetachedPort(realm, context).accept({
        'expected_head': detached(realm.head), 'canonical_document': document,
        'document_digest': _digest(document), 'records': {
            'schema_version': 1, 'commands': records, 'undo_of': undo_of or [], 'restored_from': None},
        'id_map': {}, 'semantic_events': [], 'provider_version': 'fixture', 'compiler_version': 'fixture'})


@pytest.mark.parametrize('removed', [('clip-a',), ('clip-b',), ('clip-c',), ('clip-a', 'clip-b'),
                                    ('clip-a', 'clip-b', 'clip-c')])
def test_scoped_removal_publication_inverse_and_redo_keep_raw_json_order(removed):
    from astrid.sdk.timeline._recording import Recording

    document = document_fixture()
    parent = document['parent_revision']['payload']
    rows = parent['clips']
    rows.append(dict(detached(rows[0]), id='clip-c', at=8))
    rows[0]['unknown'] = {'ordered': [None, False, 0]}
    rows[1]['nullable'] = None
    rows[2].pop('app')
    parent['config']['clips'] = detached(rows)
    document['parent_revision']['content_digest'] = _digest(parent)
    realm = DetachedRealm(document)
    candidate = bundle_from_document(document)
    ctx = Recording(candidate, {'schema_version': 1, 'op': 'clip.remove', 'version': '1',
                                'select': 'parent', 'args': {}}, 0)
    ctx.select('collection.clips', 'parent')
    for identity in removed:
        ctx.clear('parent/clip:' + identity, [])
    ctx.compile_mirrors()
    publication = accept_recorded_fixture(realm, candidate, [ctx.finish(capture=True)])
    target = publication['receipt']['receipt_id']
    accepted_records = detached(realm.entries[-1]['records'])
    removed_document = detached(realm.documents[realm.head['revision_id']])
    undone = realm.call([inverse([target])])
    restored_parent = undone['data']['canonical_document']['parent_revision']['payload']
    assert restored_parent == parent
    assert json.dumps(restored_parent['clips']) == json.dumps(rows)
    assert realm.entries[-2]['records'] == accepted_records
    undo_target = undone['receipt']['receipt_id']
    assert realm.entries[-1]['records']['commands'][0]['selections'][0]['order'] == [
        'parent/clip:' + row['id'] for row in removed_document['parent_revision']['payload']['clips']]
    redone = realm.call([inverse([undo_target])])
    assert redone['data']['canonical_document'] == removed_document
    # Repeated inverse restores order using accepted linkage, never a hidden order field.
    again = realm.call([inverse([redone['receipt']['receipt_id']])])
    assert again['data']['canonical_document'] == undone['data']['canonical_document']


@pytest.mark.parametrize('legacy_stage', ['undo', 'redo'])
def test_existing_m1_structural_inverse_history_is_immutable_and_uses_verified_boundary(legacy_stage):
    from astrid.sdk.timeline._recording import Recording

    realm = DetachedRealm()
    created = realm.call([track_create('a'), track_create('b')])
    created_document = detached(realm.documents[realm.head['revision_id']])
    target = created['receipt']['receipt_id']
    candidate = bundle_from_document(created_document)
    ctx = Recording(candidate, inverse([target]), 0)
    ctx.select('track.create', 'parent')
    for identity in ('b', 'a'):
        ctx.read('parent/track:' + identity)
        ctx.clear('parent/track:' + identity, [])
    old_undo = accept_recorded_fixture(realm, candidate, [ctx.finish(capture=True)], undo_of=[target])
    if legacy_stage == 'redo':
        target = old_undo['receipt']['receipt_id']
        candidate = bundle_from_document(realm.documents[realm.head['revision_id']])
        ctx = Recording(candidate, inverse([target]), 0)
        ctx.select('track.create', 'parent')
        for identity in ('a', 'b'):
            ctx.create('parent', 'track', track_create(identity)['args']['row'])
        accept_recorded_fixture(realm, candidate, [ctx.finish(capture=True)], undo_of=[target])
    immutable_entries = detached(realm.entries)
    result = realm.call([inverse([realm.head['publication_id']])])
    assert realm.entries[:-1] == immutable_entries
    rows = result['data']['canonical_document']['parent_revision']['payload']['config']['tracks']
    assert [row['id'] for row in rows] == (['picture', 'a', 'b'] if legacy_stage == 'undo' else ['picture'])
    if legacy_stage == 'undo':
        assert result['data']['canonical_document'] == created_document


@pytest.mark.parametrize('damage', ['missing-boundary', 'broken-linkage'])
def test_legacy_structural_inverse_without_boundary_or_linkage_refuses(damage):
    from astrid.sdk.timeline._recording import Recording

    realm = DetachedRealm()
    created = realm.call([track_create('a')])
    target = created['receipt']['receipt_id']
    candidate = bundle_from_document(realm.documents[realm.head['revision_id']])
    ctx = Recording(candidate, inverse([target]), 0)
    ctx.select('track.create', 'parent')
    ctx.read('parent/track:a')
    ctx.clear('parent/track:a', [])
    accept_recorded_fixture(realm, candidate, [ctx.finish(capture=True)], undo_of=[target])
    if damage == 'missing-boundary':
        del realm.documents[created['data']['accepted_revision_id']]
    else:
        realm.entries[-1]['records']['undo_of'] = ['other-publication']
    head, count = detached(realm.head), len(realm.accepted)
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse([head['publication_id']])])
    assert error.value.failure['details']['reason'] == 'history_unavailable'
    assert realm.head == head and len(realm.accepted) == count


@pytest.mark.parametrize('aba', [False, True])
def test_collection_publication_conflicts_include_same_value_and_reorder_aba(aba):
    from astrid.sdk.timeline._recording import Recording

    realm = DetachedRealm()
    created = realm.call([track_create('a')])
    target = created['receipt']['receipt_id']
    original_revision = realm.head['revision_id']
    for reverse in ([True, False] if aba else [False]):
        candidate = bundle_from_document(realm.documents[realm.head['revision_id']])
        rows = detached(candidate['parent']['config']['tracks'])
        if reverse:
            rows.reverse()
        else:
            rows.sort(key=lambda row: row['id'] == 'a')
        ctx = Recording(candidate, {'schema_version': 1, 'op': 'fixture.tracks', 'version': '1',
                                    'select': 'parent', 'args': {}}, 0)
        ctx.write('parent', ['config', 'tracks'], rows)
        accept_recorded_fixture(realm, candidate, [ctx.finish(capture=True)])
    assert realm.head['revision_id'] == original_revision
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse([target])])
    assert error.value.failure['details']['reason'] == 'intervening_write'


def test_structural_inverse_preserves_published_property_change_on_untouched_row():
    from astrid.sdk.timeline._recording import Recording

    realm = DetachedRealm()
    candidate = bundle_from_document(realm.documents[realm.head['revision_id']])
    ctx = Recording(candidate, {'schema_version': 1, 'op': 'clip.remove', 'version': '1',
                                'select': 'parent', 'args': {}}, 0)
    ctx.select('collection.clips', 'parent')
    ctx.clear('parent/clip:clip-a', [])
    ctx.compile_mirrors()
    removed = accept_recorded_fixture(realm, candidate, [ctx.finish(capture=True)])
    candidate = bundle_from_document(realm.documents[realm.head['revision_id']])
    ctx = Recording(candidate, {'schema_version': 1, 'op': 'fixture.property', 'version': '1',
                                'select': 'parent/clip:clip-b', 'args': {}}, 0)
    ctx.write('parent/clip:clip-b', ['opaque'], {'newer': [None, False, 0]})
    ctx.compile_mirrors()
    accept_recorded_fixture(realm, candidate, [ctx.finish(capture=True)])
    undone = realm.call([inverse([removed['receipt']['receipt_id']])])
    rows = undone['data']['canonical_document']['parent_revision']['payload']['clips']
    assert [row['id'] for row in rows] == ['clip-a', 'clip-b']
    assert rows[1]['opaque'] == {'newer': [None, False, 0]}
    assert rows[0] == document_fixture()['parent_revision']['payload']['clips'][0]


def test_composed_create_move_accepts_one_inverse_and_replays_original_claims(native_staging_operations):
    realm = DetachedRealm()
    initial = detached(realm.documents['parent-1'])
    result = realm.call([track_create('c'), staged_move('parent/clip:clip-a', 'c'),
                         track_create('d'), staged_move('parent/clip:clip-a', 'd', 5)])
    accepted = detached(realm.entries[-1])
    command = inverse([result['receipt']['receipt_id']])
    preview = realm.call([command], dry_run=True)
    undo = realm.call(preview['data']['commands'])
    restored = undo['data']['canonical_document']
    assert restored['parent_revision']['payload'] == initial['parent_revision']['payload']
    for path in [('clips',), ('config', 'tracks')]:
        before, after = initial['parent_revision']['payload'], restored['parent_revision']['payload']
        for token in path:
            before, after = before[token], after[token]
        assert json.dumps(after) == json.dumps(before)
    assert restored['shot_revisions'] == initial['shot_revisions']
    assert restored['internal_timeline_revisions'] == initial['internal_timeline_revisions']
    assert realm.entries[-2] == accepted
    assert realm.entries[-1]['records']['undo_of'] == [result['receipt']['receipt_id']]
    redo = realm.call([inverse([undo['receipt']['receipt_id']])])
    assert redo['data']['canonical_document'] == result['data']['canonical_document']
    assert len(realm.accepted) == 3
    with pytest.raises(CommitRejected) as error:
        realm.call([track_create('c')])
    assert error.value.failure['details']['reason'] == 'identity_collision'


def test_noncontiguous_inverse_treats_earlier_gap_as_later_target_input(native_staging_operations):
    realm = DetachedRealm()
    realm.page_size = 1
    initial = detached(realm.documents['parent-1']['parent_revision']['payload'])
    first = realm.call([track_create('c')])
    gap = realm.call([realm.move('parent/clip:clip-b', on=7)])
    second = realm.call([staged_move('parent/clip:clip-b', 'c', 8)])
    undone = realm.call([inverse([first['receipt']['receipt_id'], second['receipt']['receipt_id']])])
    expected = detached(gap['data']['canonical_document']['parent_revision']['payload'])
    expected['config']['tracks'] = initial['config']['tracks']
    assert undone['data']['canonical_document']['parent_revision']['payload'] == expected


@pytest.mark.parametrize('aba', [False, True])
def test_noncontiguous_overlap_cannot_be_masked_by_reversing_later_target(native_staging_operations, aba):
    realm = DetachedRealm()
    first = realm.call([staged_move('parent/clip:clip-a', 'picture')])
    realm.call([staged_move('parent/clip:clip-a', 'picture', 5 if aba else 4)])
    if aba:
        realm.call([staged_move('parent/clip:clip-a', 'picture')])
    second = realm.call([staged_move('parent/clip:clip-a', 'picture', 6)])
    head, count = detached(realm.head), len(realm.accepted)
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse([first['receipt']['receipt_id'], second['receipt']['receipt_id']])])
    assert error.value.failure['details']['reason'] == 'intervening_write'
    assert realm.head == head and len(realm.accepted) == count


def test_composed_inverse_later_reference_refuses_at_provider_boundary(native_staging_operations):
    realm = DetachedRealm()
    first = realm.call([track_create('c'), staged_move('parent/clip:clip-a', 'c')])
    realm.call([staged_move('parent/clip:clip-b', 'c', 8)])
    head, count = detached(realm.head), len(realm.accepted)
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse([first['receipt']['receipt_id']])])
    assert error.value.failure['details']['reason'] == 'surviving_dependents'
    assert realm.head == head and len(realm.accepted) == count


def test_multiple_targeted_restores_load_each_authenticated_reverse_stage():
    realm = DetachedRealm()
    original = detached(realm.documents['parent-1'])
    changed = realm.call([realm.move()])['data']['canonical_document']
    first = realm.call([restore(original)])
    second = realm.call([restore(changed)])
    undone = realm.call([inverse([first['receipt']['receipt_id'], second['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == changed
    realm.call([realm.move()])
    head, count = detached(realm.head), len(realm.accepted)
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse([undone['receipt']['receipt_id']])])
    assert error.value.failure['details']['reason'] == 'intervening_write'
    assert realm.head == head and len(realm.accepted) == count


@pytest.mark.parametrize('identity', ['clip-a', 'clip-b', 'clip-c'])
@pytest.mark.parametrize('source_kind', ['transcript', 'config'])
def test_native_clip_delete_reload_inverse_redo_preserves_rows_order_and_retained_pins(identity, source_kind):
    from astrid.sdk.timeline.capture import capture_clip_delete
    from tests.editing_conformance.test_recipe_clip_delete import delete_document

    document = delete_document()
    source = source_fixture(source_kind)
    document['dependency_manifest']['documents'] = [source['pin']]
    realm = DetachedRealm(document)
    key = _digest(source['pin'])
    realm.live_sources[key] = detached(source)
    call = capture_clip_delete(bundle_from_document(document), 'parent/clip:' + identity)
    deleted = realm.call([call])
    removed_document = detached(deleted['data']['canonical_document'])
    assert deleted['data']['commands'] == [call] and deleted['data']['id_map'] == {}
    assert removed_document['shot_revisions'] == document['shot_revisions']
    assert removed_document['internal_timeline_revisions'] == document['internal_timeline_revisions']
    assert removed_document['dependency_manifest'] == document['dependency_manifest']
    assert removed_document['parent_revision']['payload']['registry'] == document['parent_revision']['payload']['registry']
    assert realm.retained_sources[key] == source
    # Every call constructs a fresh provider and reloads detached accepted JSON.
    realm.live_sources.clear()
    undo = realm.call([inverse([deleted['receipt']['receipt_id']])])
    restored = undo['data']['canonical_document']['parent_revision']['payload']
    assert restored == document['parent_revision']['payload']
    assert json.dumps(restored['clips']) == json.dumps(document['parent_revision']['payload']['clips'])
    assert undo['data']['canonical_document']['dependency_manifest'] == document['dependency_manifest']
    redo = realm.call([inverse([undo['receipt']['receipt_id']])])
    assert redo['data']['canonical_document'] == removed_document
    assert realm.retained_sources[key] == source


def test_native_delete_stale_disjoint_opaque_publications_preserve_both():
    from astrid.sdk.timeline._recording import Recording
    from astrid.sdk.timeline.capture import capture_clip_delete

    realm = DetachedRealm()
    original = detached(realm.documents['parent-1'])
    base = detached(realm.head)
    call = capture_clip_delete(bundle_from_document(original), 'parent/clip:clip-a')
    candidate = bundle_from_document(original)
    ctx = Recording(candidate, {'schema_version': 1, 'op': 'fixture.property', 'version': '1',
                                'select': 'parent/clip:clip-b', 'args': {}}, 0)
    ctx.write('parent/clip:clip-b', ['earlier'], {'nullable': None})
    ctx.compile_mirrors()
    accept_recorded_fixture(realm, candidate, [ctx.finish(capture=True)])
    removed = realm.call([call], base=base)
    assert removed['data']['status'] == 'rebased'
    candidate = bundle_from_document(realm.documents[realm.head['revision_id']])
    ctx = Recording(candidate, {'schema_version': 1, 'op': 'fixture.property', 'version': '1',
                                'select': 'parent/clip:clip-b', 'args': {}}, 0)
    ctx.write('parent/clip:clip-b', ['opaque'], {'literal': 'parent/clip:clip-a', 'nullable': None})
    ctx.compile_mirrors()
    accept_recorded_fixture(realm, candidate, [ctx.finish(capture=True)])
    undone = realm.call([inverse([removed['receipt']['receipt_id']])])
    rows = undone['data']['canonical_document']['parent_revision']['payload']['clips']
    assert [row['id'] for row in rows] == ['clip-a', 'clip-b']
    assert rows[0] == original['parent_revision']['payload']['clips'][0]
    assert rows[1]['earlier'] == {'nullable': None}
    assert rows[1]['opaque'] == {'literal': 'parent/clip:clip-a', 'nullable': None}


@pytest.mark.parametrize('change', ['membership', 'order-aba', 'dependency-aba', 'target-source-aba'])
def test_native_delete_provider_refuses_structural_or_dependency_publications_atomically(change):
    from astrid.sdk.timeline._command_state import ABSENT
    from astrid.sdk.timeline._recording import Recording
    from astrid.sdk.timeline.capture import capture_clip_delete

    realm = DetachedRealm()
    base = detached(realm.head)
    original = bundle_from_document(realm.documents['parent-1'])
    command = capture_clip_delete(original, 'parent/clip:clip-a')
    accepted = realm.call([command])
    candidate = bundle_from_document(realm.documents[realm.head['revision_id']])
    ctx = Recording(candidate, {'schema_version': 1, 'op': 'fixture.property', 'version': '1',
                                'select': 'parent/clip:clip-b', 'args': {}}, 0)
    if change == 'membership':
        ctx.create('parent', 'clip', {'id': 'new', 'track': 'picture', 'at': 9, 'hold': 1})
    elif change == 'order-aba':
        ctx.write('parent', ['clips'], detached(candidate['parent']['clips']))
    elif change == 'dependency-aba':
        ctx.write('parent/clip:clip-b', ['app', 'on'], 'c01')
        ctx.assign('parent/clip:clip-b', ['app', 'on'], ABSENT)
    else:
        # The deleted identity is still absent after the intervening publication.
        ctx.write('parent/clip:clip-a', [], detached(original['parent']['clips'][0]))
        ctx.assign('parent/clip:clip-a', [], ABSENT)
    ctx.compile_mirrors()
    accept_recorded_fixture(realm, candidate, [ctx.finish(capture=True)])
    head, count = detached(realm.head), len(realm.accepted)
    for call, on in ((inverse([accepted['receipt']['receipt_id']]), head), (command, base)):
        with pytest.raises(CommitRejected) as error:
            realm.call([call], base=on)
        assert error.value.failure['details']['reason'] in {'intervening_write', 'selection_changed'}
        assert realm.head == head and len(realm.accepted) == count


@pytest.mark.parametrize('previously_created', [False, True])
def test_native_delete_retired_clip_identity_cannot_be_reallocated(native_staging_operations, previously_created):
    from astrid.sdk.timeline.capture import capture_clip_delete

    realm = DetachedRealm()
    identity = 'created' if previously_created else 'clip-a'
    row = {'id': identity, 'track': 'picture', 'at': 3, 'hold': 1}
    create = {'schema_version': 1, 'op': 'fixture.clip-create', 'version': '1',
              'select': 'parent', 'args': {'row': row}}
    if previously_created:
        realm.call([create])
    bundle = bundle_from_document(realm.documents[realm.head['revision_id']])
    deletion = realm.call([capture_clip_delete(bundle, 'parent/clip:' + identity)])
    head, count = detached(realm.head), len(realm.accepted)
    with pytest.raises(CommitRejected) as error:
        realm.call([create])
    assert error.value.failure['details']['reason'] == 'identity_collision'
    assert realm.head == head and len(realm.accepted) == count
    # Historical restoration remains the only admitted reuse.
    restored = realm.call([inverse([deletion['receipt']['receipt_id']])])
    assert any(clip['id'] == identity for clip in restored['data']['canonical_document']['parent_revision']['payload']['clips'])


def test_native_delete_then_reallocate_in_same_batch_is_atomic(native_staging_operations):
    from astrid.sdk.timeline.capture import capture_clip_delete

    realm = DetachedRealm()
    document = detached(realm.documents['parent-1'])
    deletion = capture_clip_delete(bundle_from_document(document), 'parent/clip:clip-a')
    create = {'schema_version': 1, 'op': 'fixture.clip-create', 'version': '1',
              'select': 'parent', 'args': {'row': document['parent_revision']['payload']['clips'][0]}}
    head = detached(realm.head)
    with pytest.raises(CommitRejected) as error:
        realm.call([deletion, create])
    assert error.value.failure['details']['reason'] == 'identity_collision'
    assert realm.head == head and realm.accepted == [] and realm.documents == {'parent-1': document}


def test_native_move_then_delete_succeeds_and_unsupported_batch_keeps_original_document():
    from astrid.sdk.timeline.capture import capture_clip_delete

    realm = DetachedRealm()
    realm.call([realm.move()])
    document = detached(realm.documents[realm.head['revision_id']])
    call = capture_clip_delete(bundle_from_document(document), 'parent/clip:clip-a')
    deletion = realm.call([call])
    undo = realm.call([inverse([deletion['receipt']['receipt_id']])])
    assert undo['data']['canonical_document'] == document
    head, count = detached(realm.head), len(realm.accepted)
    invalid = {'schema_version': 1, 'op': 'clip.delete', 'version': '1',
               'select': 'occurrence:occ-a/clip:clip-a', 'args': {}}
    with pytest.raises(CommitRejected) as error:
        realm.call([realm.move(), invalid])
    assert error.value.failure['details']['reason'] == 'unsupported_ownership'
    assert realm.head == head and len(realm.accepted) == count


@pytest.mark.parametrize("damage", ["missing", "revision", "gap", "scope", "digest"])
def test_inverse_historical_source_requires_authenticated_boundaries(damage):
    realm = DetachedRealm()
    original = detached(realm.head)
    realm.call([realm.move()])
    target = realm.call([realm.move("parent/clip:clip-b", 8.008)])
    previous = realm.entries[-1]["previous"]
    if damage == "missing":
        realm.entries[-1]["previous"] = None
    elif damage == "revision":
        previous["revision_id"] = "wrong-revision"
    elif damage == "gap":
        realm.entries[-1]["previous"] = original
    elif damage == "scope":
        realm.documents[previous["revision_id"]]["project_id"] = "foreign-project"
    else:
        realm.documents[previous["revision_id"]]["shot_revisions"][0]["payload"]["unknown"] = "tampered"
    head, accepted = detached(realm.head), detached(realm.accepted)
    with pytest.raises(CommitRejected):
        realm.call([inverse([target["receipt"]["receipt_id"]])])
    assert realm.head == head and realm.accepted == accepted
