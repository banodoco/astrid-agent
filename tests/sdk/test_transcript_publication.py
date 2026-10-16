"""Restricted-port sequencing doubles; these do not mint a Host invocation.

A owns the actual SDK/Host/Runtime proof. These tests deliberately exercise
only the internal frozen-operation sequence, never substitute a fake trusted
BoundDocumentInvocation into the public writer entry.
"""
import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_transcript_sources

from astrid.sdk.managed_transcript import canonical_transcript_json
from astrid.sdk.timeline.commit import _digest
from astrid.sdk.transcript_publication import (
    ACTION_ID,
    Transcripts,
    _key,
    _publish_frozen,
    _record,
    _verify_mutation,
)
from astrid.sdk.transcript_publication_types import (
    TranscriptParentSelection,
    prepare_transcript_publication,
)


@pytest.fixture
def source():
    return test_transcript_sources.source.__wrapped__()


class Failure(Exception):
    def __init__(self, code):
        self.code = code


def prepared(source, *, request_id="publish-1", version=None, parent=None):
    return prepare_transcript_publication(source[0], project_id="p", expected_document_version=version,
        request_id=request_id, admitted_action_id=ACTION_ID, producer_digest="a" * 64, parent_selection=parent)


class Ports:
    """Synthetic port operations only, explicitly NOT BoundDocumentInvocation."""
    def __init__(self, source, prepared):
        self.calls, self.saved, self.writes = [], {}, 0
        self.source, self.prepared = source, prepared
        self.current = None
        self.mode = "miss"
        self.supported = True
        self.objects_by_id = {source[0]["subject"]["media_object_id"]: b"synthetic-media-only"}
        self.documents = SimpleNamespace(create_document=self.create, update_document=self.update, get_document=self.get_document)
        self.objects = SimpleNamespace(get_object=self.get_object, ingest_project_object=self.ingest)

    def supports_operation_version(self, operation, version):
        self.calls.append(("support", operation, version))
        return self.supported

    def get_document(self, docid):
        self.calls.append(("parent", docid))
        if self.current is None:
            raise Failure("not_found")
        return copy.deepcopy(self.current)

    def create(self, docid, kind, content, *, idempotency_key, replay_only):
        return self.mutate(dict(project_id="p", document_id=docid, kind=kind, content=content), idempotency_key, replay_only, "document.create", None)

    def update(self, docid, *, expected_version, idempotency_key, content, kind, replay_only):
        body = dict(expected_version=expected_version, content=content, kind=kind)
        return self.mutate(dict(project_id="p", document_id=docid, body=body), idempotency_key, replay_only, "document.update", expected_version)

    def mutate(self, body, key, replay_only, operation, expected):
        self.calls.append(("mutation", copy.deepcopy(body), key, replay_only))
        if key in self.saved:
            digest, value = self.saved[key]
            if _digest(body) != digest:
                raise Failure("conflict")
            return copy.deepcopy(value)
        if self.mode not in {"miss", "hit"}:
            raise Failure(self.mode)
        if replay_only:
            raise Failure("no_committed_request")
        if expected is not None and (self.current is None or self.current["version"] != expected):
            raise Failure("conflict")
        data = dict(project_id="p", document_id=body["document_id"], kind="media.transcript/v1",
                    content=copy.deepcopy(body.get("content", body.get("body", {}).get("content"))), version=1 if expected is None else expected + 1)
        receipt = dict(project_id="p", command_kind=operation, idempotency_key=key, request_hash=_digest(body),
            receipt_id="receipt-original", project_seq=1, event_ids=["event-original"], result=copy.deepcopy(data), created_at="synthetic")
        value = dict(data=data, receipt=receipt)
        self.saved[key] = (_digest(body), copy.deepcopy(value))
        self.current = data
        self.writes += 1
        return value

    def get_object(self, oid):
        self.calls.append(("get-object", oid))
        if oid not in self.objects_by_id:
            raise Failure("not_found")
        return dict(data=self.objects_by_id[oid], status=200, headers={})

    def ingest(self, raw, *, media_type, idempotency_key, filename):
        self.calls.append(("ingest", raw, idempotency_key))
        from astrid.sdk.timeline.transcripts import digest_bytes
        oid = digest_bytes(raw)
        self.objects_by_id[oid] = raw
        return dict(data=dict(object_id=oid), receipt={"idempotency_key": idempotency_key})


def run(ports, request):
    record, raw = _record(request)
    return _publish_frozen(ports, request, record, raw)


def test_replay_first_exact_retry_after_current_advanced_no_new_revision(source):
    frozen = prepared(source)
    ports = Ports(source, frozen)
    original, _ = run(ports, frozen)
    assert ports.calls[0] == ("support", "createDocument", 2)
    assert ports.calls[1][-1] is True
    ports.current = dict(ports.current, version=99, content={"advanced": True})
    ports.calls.clear()
    recovered, snapshot = run(ports, frozen)
    assert recovered == original
    assert recovered["data"]["version"] == 1 and recovered["receipt"] == original["receipt"]
    assert recovered["data"]["content"]["source"]["revision_id"] == json.loads(frozen.canonical_record_json)["source"]["revision_id"]
    assert snapshot["data"] == frozen.canonical_record_json.encode()
    assert ports.writes == 1
    assert not any(c[0] in {"parent", "ingest"} for c in ports.calls)
    assert [c[-1] for c in ports.calls if c[0] == "mutation"] == [True]


@pytest.mark.parametrize("code", ["conflict", "idempotency_window_expired", "forbidden", "unavailable"])
def test_non_miss_errors_never_fall_through_or_write(source, code):
    frozen = prepared(source)
    ports = Ports(source, frozen)
    ports.mode = code
    with pytest.raises(Failure) as error:
        run(ports, frozen)
    assert error.value.code == code
    assert ports.writes == 0 and not any(c[0] in {"parent", "ingest", "get-object"} for c in ports.calls)


def test_changed_body_same_key_conflicts_before_preflight(source):
    frozen = prepared(source)
    ports = Ports(source, frozen)
    run(ports, frozen)
    changed = copy.deepcopy(source)
    changed[0]["words"][0]["text"] = "Different"
    other = prepared(changed)
    assert _key(other, "document") == _key(frozen, "document")
    ports.calls.clear()
    with pytest.raises(Failure) as error:
        run(ports, other)
    assert error.value.code == "conflict" and ports.writes == 1
    assert not any(c[0] in {"parent", "ingest", "get-object"} for c in ports.calls)


def test_missing_exact_v2_support_sends_zero_document_requests(source):
    frozen = prepared(source)
    ports = Ports(source, frozen)
    ports.supported = False
    with pytest.raises(ValueError, match="version 2"):
        run(ports, frozen)
    assert ports.calls == [("support", "createDocument", 2)]


def corrected(source):
    old, raw, d = source
    parent = TranscriptParentSelection(1, old["source"]["lineage_id"], old["source"]["revision_id"],
        d["document"]["content_digest"], d["snapshot"]["object_id"])
    value = copy.deepcopy(source)
    value[0]["source"]["parent_revision_id"] = old["source"]["revision_id"]
    value[0]["words"][0]["text"] = "Corrected"
    return value, prepared(value, version=1, parent=parent)


def test_stale_parent_miss_performs_no_ingest_or_document_write(source):
    value, frozen = corrected(source)
    ports = Ports(value, frozen)
    ports.current = dict(project_id="p", document_id=frozen.document_id, kind="media.transcript/v1", version=2, content=source[0])
    with pytest.raises(ValueError, match="stale"):
        run(ports, frozen)
    assert ports.writes == 0 and not any(c[0] == "ingest" for c in ports.calls)
    assert ports.calls[0] == ("support", "updateDocument", 2)


def test_correction_snapshot_identity_and_exact_update_body(source):
    value, frozen = corrected(source)
    ports = Ports(value, frozen)
    ports.current = dict(project_id="p", document_id=frozen.document_id, kind="media.transcript/v1", version=1, content=source[0])
    ports.objects_by_id[source[2]["snapshot"]["object_id"]] = source[1]
    result, _ = run(ports, frozen)
    assert result["data"]["version"] == 2
    mutations = [c for c in ports.calls if c[0] == "mutation"]
    assert mutations[0][1:3] == mutations[1][1:3]
    assert [c[-1] for c in mutations] == [True, False]
    assert canonical_transcript_json(result["data"]["content"]) == frozen.canonical_record_json


def test_bad_snapshot_receipt_data_and_body_are_refused(source):
    frozen = prepared(source)
    ports = Ports(source, frozen)
    mutation, _ = run(ports, frozen)
    for mutate in [lambda m: m["receipt"].update(project_id="other"),
                   lambda m: m["receipt"].update(request_hash="f" * 64),
                   lambda m: m["data"].update(version=9)]:
        wrong = copy.deepcopy(mutation)
        mutate(wrong)
        with pytest.raises(ValueError):
            _verify_mutation(frozen, wrong)
    ports.objects_by_id[frozen.snapshot_object_id] = b"changed"
    with pytest.raises(ValueError, match="snapshot bytes"):
        run(ports, frozen)
    with pytest.raises(ValueError, match="bytes/identity"):
        _record(replace(frozen, canonical_record_json=frozen.canonical_record_json + " "))


def test_public_facade_rejects_callback_and_unbound_constructor():
    with pytest.raises(TypeError):
        Transcripts(project_id="p", producer_digest="a" * 64,
            documents=SimpleNamespace(get_document=lambda *_args: None),
            invoke_document_action=lambda *_args, **_kwargs: None)


def test_replay_recovers_missing_snapshot_with_same_frozen_ingest_key(source):
    frozen = prepared(source)
    ports = Ports(source, frozen)
    original, _ = run(ports, frozen)
    initial_key = next(c[2] for c in ports.calls if c[0] == "ingest")
    del ports.objects_by_id[frozen.snapshot_object_id]
    ports.calls.clear()
    replay, snapshot = run(ports, frozen)
    assert replay == original and snapshot["data"] == frozen.canonical_record_json.encode()
    assert [c[2] for c in ports.calls if c[0] == "ingest"] == [initial_key]
    assert ports.writes == 1


def test_output_projection_retains_json_data_receipt_and_snapshot(source):
    from astrid.sdk.transcript_publication import publication_output
    from astrid.sdk.transcript_publication_types import _capture_resolved_transcript_publication
    frozen = prepared(source)
    ports = Ports(source, frozen)
    value, snapshot = run(ports, frozen)
    # DTO fixture factory only: no Host invocation or Runtime authority claim.
    capture = _capture_resolved_transcript_publication(frozen, value,
        snapshot_object_id=frozen.snapshot_object_id,
        snapshot_response=snapshot)
    output = publication_output(capture)
    assert output["data"] == value["data"] and output["receipt"] == value["receipt"]
    assert output["snapshot"]["object_id"] == frozen.snapshot_object_id
    assert json.loads(json.dumps(output)) == output
    with pytest.raises(TypeError):
        publication_output(output)


def test_same_logical_request_cannot_reuse_parent_revision_as_new_update(source):
    original = prepared(source)
    ports = Ports(source, original)
    published, _ = run(ports, original)
    old = published["data"]["content"]
    parent = TranscriptParentSelection(1, old["source"]["lineage_id"], old["source"]["revision_id"],
        original.content_digest, original.snapshot_object_id)
    next_record = copy.deepcopy(old)
    next_record["source"]["parent_revision_id"] = old["source"]["revision_id"]
    next_record["words"][0]["text"] = "Changed"
    update = prepare_transcript_publication(next_record, project_id="p", expected_document_version=1,
        request_id=original.request_id, admitted_action_id=ACTION_ID, producer_digest="a" * 64, parent_selection=parent)
    # A different route must not let the same logical request rewrite a source
    # under its old revision, even if a test ledger namespaces keys by command.
    ports.saved.clear()
    ports.calls.clear()
    with pytest.raises(ValueError, match="correction"):
        run(ports, update)
    assert ports.writes == 1 and not any(c[0] == "ingest" for c in ports.calls)
