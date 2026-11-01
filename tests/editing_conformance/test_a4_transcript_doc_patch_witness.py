"""A4 witness for the mounted transcript-shaped generic ``doc.patch`` path.

The App transcript capture module is not a registered Agent operation.  These
cases therefore exercise only the current generic patch provider, with the
App's nested ``parent.config.bundle`` shape and the provider's existing exact
document-input boundary.  They deliberately do not claim transcript
association, reconciliation, caption ownership, or referential integrity for
opaque JSON metadata.
"""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import ABSENT, present
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedRealm,
    detached,
    source_fixture,
)
from tests.sdk.test_timeline_commit_provider import (
    runtime_abi as runtime_abi,
)
from tests.sdk.test_timeline_commit_provider_document import document_fixture

TRANSCRIPT_SCHEMA_REF = "astrid.transcript_segment/v1"
REFERENCES_SCHEMA = "media.transcript.references/v1"
ASSET_ID = "picture"


def change(path, before, value=None, *, op="set", ref="parent", **extra):
    result = {"op": op, "ref": ref, "path": path, "before": copy.deepcopy(before), **extra}
    if op in ("set", "list.insert"):
        result["value"] = copy.deepcopy(value)
    return result


def patch(*changes, reads=None):
    result = {
        "schema_version": 1,
        "op": "doc.patch",
        "version": "1",
        "select": None,
        "args": {"changes": list(changes)},
    }
    if reads is not None:
        result["reads"] = copy.deepcopy(reads)
    return result


def step(identity, ordinal, call, *, reads=()):
    return {
        "id": identity,
        "label": identity,
        "intent": "",
        "tags": [],
        "command_ordinals": [ordinal],
        "patch": copy.deepcopy(call["args"]["changes"]),
        **({"reads": copy.deepcopy(list(reads))} if reads else {}),
    }


def app_transcript_item(pin, *, item_id="transcript-source:whisper:segment:line-1"):
    return {
        "id": item_id,
        "shape": "interval",
        "domain": "source_seconds",
        "extent": {"start": 4, "end": 5},
        "schemaRef": TRANSCRIPT_SCHEMA_REF,
        "sourceArtifactRef": {"assetId": ASSET_ID},
        "provenance": {"adapterId": "legacy.capture", "adapterVersion": "1"},
        "payload": {
            "sourceId": "whisper",
            "segmentId": item_id,
            "text": "Original",
            "words": [{"id": "word-1", "start": 4, "end": 5, "text": "original"}],
            "documentId": pin["document_id"],
            "documentVersion": pin["version"],
            "opaque": {"keep": [None, False, 0]},
        },
        "opaqueEnvelope": {"keep": True},
    }


def app_bundle(pin):
    old_item = app_transcript_item(pin)
    return {
        "schema_version": 1,
        "transcriptReferences": {
            "schema": REFERENCES_SCHEMA,
            "associations": [{"assetId": ASSET_ID, "documentId": "transcript:old"}],
        },
        "itemsBySchemaRef": {
            "other/v1": [
                {
                    "id": "unrelated",
                    "shape": "point",
                    "domain": "ordinal",
                    "extent": {"start": 0},
                    "schemaRef": "other/v1",
                    "payload": {"keep": True},
                    "sourceArtifactRef": {"assetId": "other"},
                    "provenance": {"adapterId": "other", "adapterVersion": "1"},
                }
            ],
            TRANSCRIPT_SCHEMA_REF: [old_item],
        },
        "opaqueBundle": {"keep": [None, False, 0], "future": {"null": None}},
    }


def transcript_document():
    """Use the existing exact source fixture and private DetachedPort store."""
    return source_fixture("transcript")


def transcript_document_fixture():
    source = transcript_document()
    document = document_fixture(shared=False)
    payload = document["parent_revision"]["payload"]
    payload["config"]["bundle"] = app_bundle(source["pin"])
    document["dependency_manifest"]["documents"] = [copy.deepcopy(source["pin"])]
    document["parent_revision"]["content_digest"] = _digest(payload)
    return document, source


def submit(realm, *calls, base=None, **controls):
    context = realm.context(calls, base=base)
    context["request"].update(controls)
    context["invocation"]["body"] = copy.deepcopy(context["request"])
    context["request_hash"] = _digest(context["invocation"])
    from astrid.sdk.timeline.commit import create_runtime_commit_service
    from tests.sdk.test_timeline_commit_provider import DetachedPort

    return create_runtime_commit_service().commit(context, DetachedPort(realm, context)), context


def realm_with_transcript():
    document, source = transcript_document_fixture()
    realm = DetachedRealm(document)
    realm.live_sources[_digest(source["pin"])] = detached(source)
    return realm, source


def bundle_at(realm):
    return bundle_from_document(realm.documents[realm.head["revision_id"]])


def test_nested_transcript_planes_preserve_app_shape_and_exact_source_pin():
    realm, source = realm_with_transcript()
    before = bundle_at(realm)
    before_bundle = before["parent"]["config"]["bundle"]
    next_bundle = copy.deepcopy(before_bundle)
    next_bundle["transcriptReferences"]["associations"] = [
        {"assetId": ASSET_ID, "documentId": source["pin"]["document_id"]}
    ]
    next_bundle["itemsBySchemaRef"][TRANSCRIPT_SCHEMA_REF][0]["payload"]["text"] = "Corrected"
    next_bundle["opaqueBundle"]["future"]["null"] = None
    call = patch(
        change(
            ["config", "bundle", "transcriptReferences", "associations"],
            present(before_bundle["transcriptReferences"]["associations"]),
            next_bundle["transcriptReferences"]["associations"],
        ),
        change(
            ["config", "bundle", "itemsBySchemaRef"],
            present(before_bundle["itemsBySchemaRef"]),
            next_bundle["itemsBySchemaRef"],
        ),
        change(
            ["config", "bundle", "opaqueBundle"],
            present(before_bundle["opaqueBundle"]),
            next_bundle["opaqueBundle"],
        ),
    )

    result, context = submit(realm, call, reads=[])
    current = result["data"]["canonical_document"]["parent_revision"]["payload"]
    nested = current["config"]["bundle"]
    assert nested["transcriptReferences"]["associations"] == [
        {"assetId": ASSET_ID, "documentId": source["pin"]["document_id"]}
    ]
    assert nested["itemsBySchemaRef"][TRANSCRIPT_SCHEMA_REF][0]["payload"]["text"] == "Corrected"
    assert nested["itemsBySchemaRef"]["other/v1"][0]["payload"]["keep"] is True
    assert nested["opaqueBundle"] == before_bundle["opaqueBundle"]
    assert result["data"]["canonical_document"]["dependency_manifest"]["documents"] == [source["pin"]]
    assert realm.retained_sources[_digest(source["pin"])] == source
    assert context["request"]["commands"] == [call]
    assert result["data"]["status"] == "accepted"
    assert result["receipt"]["result"]["canonical_document"] == result["data"]["canonical_document"]


def test_explicit_null_and_absence_are_distinct_in_nested_bundle():
    realm, _ = realm_with_transcript()
    before = bundle_at(realm)["parent"]["config"]["bundle"]
    call = patch(
        change(["config", "bundle", "opaqueBundle", "newNull"], ABSENT, None),
        change(["config", "bundle", "opaqueBundle", "future", "null"], present(None), op="remove"),
    )
    result, _ = submit(realm, call, reads=[])
    nested = result["data"]["canonical_document"]["parent_revision"]["payload"]["config"]["bundle"]
    assert nested["opaqueBundle"]["newNull"] is None
    assert "null" not in nested["opaqueBundle"]["future"]
    assert nested["opaqueBundle"]["keep"] == before["opaqueBundle"]["keep"]
    writes = realm.entries[-1]["records"]["commands"][0]["writes"]
    assert any(write["before"] == ABSENT and write["after"] == present(None) for write in writes)
    assert any(write["before"] == present(None) and write["after"] == ABSENT for write in writes)


def test_exact_source_and_prior_pin_bytes_are_provider_inputs_not_nested_metadata_authority():
    realm, source = realm_with_transcript()
    prior = copy.deepcopy(source)
    prior["document"]["version"] = prior["pin"]["version"] = 2
    prior["document"]["content"]["lines"][0]["words"][0]["text"] = "prior"
    prior["pin"]["content_digest"] = _digest(prior["document"]["content"])
    document = realm.documents[realm.head["revision_id"]]
    document["dependency_manifest"]["documents"] = [source["pin"], prior["pin"]]
    document["parent_revision"]["content_digest"] = _digest(document["parent_revision"]["payload"])
    realm.live_sources[_digest(prior["pin"])] = detached(prior)
    call = patch(
        change(
            ["config", "bundle", "transcriptReferences", "associations"],
            present(app_bundle(source["pin"])["transcriptReferences"]["associations"]),
            [{"assetId": ASSET_ID, "documentId": prior["pin"]["document_id"]}],
        )
    )
    result, _ = submit(realm, call, reads=[])
    assert result["data"]["canonical_document"]["dependency_manifest"]["documents"] == [
        source["pin"], prior["pin"]
    ]
    records = realm.entries[-1]["records"]
    assert {dependency["pin"]["version"] for dependency in records["commands"][0]["dependencies"]} == {1, 2}
    assert realm.retained_sources[_digest(source["pin"])] == source
    assert realm.retained_sources[_digest(prior["pin"])] == prior


def test_declared_nested_read_and_full_c0_metadata_survive_accepted_preview_and_receipt():
    realm, _ = realm_with_transcript()
    base = copy.deepcopy(realm.head)
    before = bundle_at(realm)["parent"]["config"]["bundle"]
    read = {"kind": "field", "ref": "parent", "path": ["config", "bundle"], "state": present(before)}
    call = patch(change(["config", "bundle", "opaqueBundle", "note"], ABSENT, "from App"))
    controls = {
        "label": "Import transcript-shaped bundle note",
        "steps": [step("transcript-bundle", 0, call, reads=[read])],
        "merge": "fields",
        "reads": [read],
        "source": {"via": "app", "surface": "localSaveSession"},
        "rationale": "A2 transcript-shaped generic patch",
        "group_id": "a4-transcript-witness",
    }
    preview, preview_context = submit(realm, call, base=base, dry_run=True, **controls)
    assert preview["receipt"] is None
    assert preview["data"]["status"] == "preview"
    assert preview["data"]["would_status"] == "accepted"
    assert preview["data"]["steps"][0]["id"] == controls["steps"][0]["id"]
    assert preview["data"]["steps"][0]["patch"] == controls["steps"][0]["patch"]
    assert preview["data"]["steps"][0]["reads"] == controls["steps"][0]["reads"]
    assert read in preview["data"]["steps"][0]["dependencies"]
    assert preview["data"]["merge_mode"] == "fields"
    assert realm.head == base
    assert preview_context["request"] == {
        "api_version": 1,
        "project_id": "project-1",
        "timeline_id": "film",
        "base_revision_id": base["revision_id"],
        "base_publication_id": base["publication_id"],
        "commands": [call],
        "dry_run": True,
        "source": controls["source"],
        "rationale": controls["rationale"],
        "label": controls["label"],
        "steps": controls["steps"],
        "merge": "fields",
        "reads": [read],
        "group_id": controls["group_id"],
    }
    accepted, accepted_context = submit(realm, call, base=base, **controls)
    assert accepted["data"]["status"] == "accepted"
    assert accepted["receipt"]
    records = realm.entries[-1]["records"]
    assert records["schema_version"] == 1
    assert records["undo_of"] == []
    assert records["restored_from"] is None
    assert records["undo_steps"] is None
    assert records["label"] == controls["label"]
    assert records["merge_mode"] == "fields"
    assert records["steps"][0]["id"] == "transcript-bundle"
    assert records["steps"][0]["patch"]
    command_record = records["commands"][0]
    assert command_record["writes"]
    assert {
        "ref": read["ref"],
        "path": read["path"],
        "state": read["state"],
    } in command_record["reads"]
    assert command_record["dependencies"]
    assert command_record["bindings"]
    assert command_record["derived_writes"] == []
    assert all(
        binding["boundary"]["evaluated_head"] == base
        for binding in command_record["bindings"]
        if "boundary" in binding
    )
    assert any(read_event[0] == "entry" for read_event in realm.reads)
    assert accepted["receipt"]["idempotency_key"] == "request-2"
    assert accepted["receipt"]["request_hash"] == accepted_context["request_hash"]
    assert accepted["receipt"]["command_kind"] == "timeline.commit"
    assert accepted["receipt"]["project_id"] == "project-1"
    assert accepted["receipt"]["result"] == accepted["data"]
    assert accepted["data"]["source"] == controls["source"]
    assert accepted["data"]["rationale"] == controls["rationale"]
    assert accepted["data"]["accepted_revision_id"] == realm.head["revision_id"]
    assert accepted["data"]["previous_revision_id"] == base["revision_id"]
    assert accepted["data"]["rebased_onto"] is None


def test_previous_value_mismatch_and_collision_refuse_atomically_with_structured_details():
    realm, _ = realm_with_transcript()
    base = copy.deepcopy(realm.head)
    bad_previous = patch(change(["config", "bundle", "opaqueBundle", "keep"], present(["wrong"]), ["new"]))
    intervening = patch(
        change(
            ["config", "bundle", "opaqueBundle", "keep"],
            present([None, False, 0]),
            ["intervening"],
        )
    )
    submit(realm, intervening, reads=[])
    original = copy.deepcopy(realm.documents[realm.head["revision_id"]])
    with pytest.raises(CommitRejected) as error:
        submit(realm, bad_previous, base=base, reads=[])
    failure = error.value.failure
    assert failure["details"]["reason"] == "previous_value_mismatch"
    assert failure["details"]["code"] == "previous_value_mismatch"
    assert failure["details"]["path"]
    assert failure["details"]["current"] == realm.head
    assert failure["details"]["hint"]
    assert failure["details"]["suggested_command"]
    assert realm.documents[realm.head["revision_id"]] == original
    assert len(realm.accepted) == 1

    collision_realm, _ = realm_with_transcript()
    collision_original = copy.deepcopy(collision_realm.documents[collision_realm.head["revision_id"]])
    existing_track = collision_original["parent_revision"]["payload"]["config"]["tracks"][0]
    collision = patch(
        change(
            ["config", "tracks"],
            present(existing_track),
            existing_track,
            op="list.insert",
            id="picture",
            order_before=["picture"],
            after_id=None,
        )
    )
    with pytest.raises(CommitRejected) as collision_error:
        submit(collision_realm, collision, reads=[])
    assert collision_error.value.failure["details"]["reason"] == "identity_collision"
    assert collision_realm.documents[collision_realm.head["revision_id"]] == collision_original


def test_intervening_unrelated_edit_merges_but_atomic_scope_refuses():
    realm, _ = realm_with_transcript()
    base = copy.deepcopy(realm.head)
    other = patch(change(["config", "bundle", "opaqueBundle", "other"], ABSENT, "kept"))
    submit(realm, other, reads=[])
    target = patch(change(["config", "bundle", "opaqueBundle", "target"], ABSENT, "changed"))
    merged, _ = submit(realm, target, base=base, merge="fields", reads=[])
    assert merged["data"]["status"] == "rebased"
    assert merged["data"]["canonical_document"]["parent_revision"]["payload"]["config"]["bundle"]["opaqueBundle"][
        "other"
    ] == "kept"

    realm, _ = realm_with_transcript()
    base = copy.deepcopy(realm.head)
    submit(realm, other, reads=[])
    with pytest.raises(CommitRejected) as error:
        submit(realm, target, base=base, merge="atomic", reads=[])
    assert error.value.failure["details"]["reason"] == "atomic_scope_changed"
    assert realm.documents[realm.head["revision_id"]]["parent_revision"]["payload"]["config"]["bundle"][
        "opaqueBundle"
    ]["other"] == "kept"


def test_malformed_exact_source_refuses_before_nested_patch_and_no_open_metadata_integrity_is_claimed():
    realm, source = realm_with_transcript()
    original = copy.deepcopy(realm.documents[realm.head["revision_id"]])
    malformed = detached(source)
    malformed["document"]["content"]["lines"] = []
    realm.live_sources[_digest(source["pin"])] = malformed
    call = patch(change(["config", "bundle", "opaqueBundle", "shouldNotWrite"], ABSENT, True))
    with pytest.raises(CommitRejected) as error:
        submit(realm, call, reads=[])
    assert error.value.failure["details"]["reason"] in {"invalid_document", "dependency_changed"}
    assert realm.documents[realm.head["revision_id"]] == original
    assert realm.accepted == []

    # The generic provider intentionally has no relation between this opaque
    # association and the verified source document beyond ordinary JSON bytes.
    # App transcript.associate reconciliation and source-item ownership remain
    # uncovered here; this test does not manufacture that check.
