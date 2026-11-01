"""A4 raw parent-media guard witnesses on the registered Agent provider.

The old App after-anchor capture cloned and normalized a source row before
emitting ``clip.add-media``.  The current registered operation accepts only a
complete literal parent row.  These tests keep that old-to-current distinction
explicit while exercising the real provider preview/accept boundary.
"""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline._command_state import ABSENT
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, bundle_from_document, create_runtime_commit_service
from tests.editing_conformance.test_recipe_m2_parent_media import args, parent_inputs
from tests.editing_conformance.test_recipe_occurrence_media import MediaRealm
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedPort,
    refusal,
    runtime_abi,  # noqa: F401 -- autouse detached ABI fixture
)


def raw_command(row, media, *, select="parent"):
    return {
        "schema_version": 1,
        "op": "clip.add-media",
        "version": "1",
        "select": select,
        "args": {"row": copy.deepcopy(row), "media": copy.deepcopy(media)},
    }


def raw_parent_case(kind="video", *, reviewed=False):
    """Use the registered parent_inputs fixture, optionally with reviewed App rows."""
    document, _, media, verified = parent_inputs(kind)
    payload = document["parent_revision"]["payload"]
    payload["registry"]["assets"]["picture"]["type"] = "video" if kind == "video" else "image"
    if not reviewed:
        return document, media, verified
    rows = [
        {"id": "before", "clipType": kind, "asset": "picture", "track": "overlay", "at": 0, "hold": 1},
        {"id": "anchor", "clipType": kind, "asset": "picture", "track": "overlay", "at": 1.003,
         "hold": 4, **({"from": 2, "to": 6, "speed": 2} if kind == "video" else {})},
        {"id": "later", "clipType": kind, "asset": "picture", "track": "overlay", "at": 6, "hold": 1,
         "at_ms": 6000},
        {"id": "other", "clipType": kind, "asset": "picture", "track": "other", "at": 2, "hold": 1},
    ]
    payload["clips"] = copy.deepcopy(rows)
    payload["config"]["clips"] = copy.deepcopy(rows)
    document["parent_revision"]["content_digest"] = _digest(payload)
    return document, media, verified


def current_birth_row(media, *, kind="video", hold=4):
    row = args(media, kind, at=3.003, hold=hold)["row"]
    row.update(
        effectId=None,
        effect_id=None,
        opaque={"keep": [None, False, {}]},
        unknown={"future": None, "flag": False},
    )
    return row


def submit(realm, *calls, base=None, dry_run=False, **controls):
    """Submit through the same provider/port used by the parent media tests."""
    context = realm.context(calls, base=base, dry_run=dry_run)
    context["request"].update(controls)
    context["invocation"]["body"] = copy.deepcopy(context["request"])
    context["request_hash"] = _digest(context["invocation"])

    class MediaPort(DetachedPort):
        def selected_media(self, pin):
            matches = [value for value in self.realm.verified if value["selection"] == pin]
            if not matches:
                raise refusal("dependency_unavailable")
            return copy.deepcopy(matches[0])

    return create_runtime_commit_service().commit(context, MediaPort(realm, context)), context


def controls():
    return {
        "label": "Parent raw media birth",
        "merge": "fields",
        "reads": [],
        "steps": [{
            "id": "parent-birth",
            "label": "Parent raw media birth",
            "intent": "",
            "tags": ["media"],
            "command_ordinals": [0],
            "patch": [],
        }],
        "source": {"via": "python", "surface": "registered-parent-media"},
        "rationale": "A4 raw parent-media guard witness",
        "group_id": "a4-parent-raw-witness",
    }


def assert_structured_refusal(error):
    failure = error.value.failure
    details = failure["details"]
    assert details["status"] in {"invalid", "conflict", "refused"}
    assert details["reason"]
    assert "path" in details
    assert details["hint"]
    assert details["suggested_command"]
    return details


def assert_no_write(realm, before):
    assert realm.head == before["head"]
    assert realm.entries == before["entries"]
    assert realm.documents == before["documents"]
    assert realm.accepted == before["accepted"]


def test_parent_raw_provider_accepts_literal_row_with_c0_receipt_and_json_replay():
    document, media, verified = raw_parent_case()
    original_document = copy.deepcopy(document)
    row = current_birth_row(media)
    command = raw_command(row, media)
    roundtripped = json.loads(json.dumps(command))
    bundle = bundle_from_document(document)
    bundle_before = copy.deepcopy(bundle)
    plan = execute(bundle, [roundtripped], verified_media=verified)
    assert bundle == bundle_before
    assert plan.bundle["parent"]["clips"][-1] == row
    assert plan.records[0]["writes"]
    realm = MediaRealm(document, verified)
    base = copy.deepcopy(realm.head)

    preview, preview_context = submit(realm, roundtripped, base=base, dry_run=True, **controls())
    assert preview["receipt"] is None
    assert preview["data"]["status"] == "preview"
    assert preview["data"]["would_status"] == "accepted"
    assert realm.head == base
    assert realm.documents["parent-1"] == original_document
    assert preview_context["request"]["commands"] == [command]

    accepted, accepted_context = submit(realm, roundtripped, base=base, **controls())
    assert accepted["data"]["status"] == "accepted"
    assert accepted["receipt"]["command_kind"] == "timeline.commit"
    assert accepted["receipt"]["result"] == accepted["data"]
    assert accepted["data"]["source"] == controls()["source"]
    assert accepted["data"]["rationale"] == controls()["rationale"]
    assert accepted_context["request"]["commands"] == [command]

    result_document = accepted["data"]["canonical_document"]
    rows = result_document["parent_revision"]["payload"]["clips"]
    assert rows[-1] == row
    assert result_document["parent_revision"]["payload"]["config"]["clips"][-1] == row
    assert row["effectId"] is None and row["effect_id"] is None
    assert row["opaque"] == {"keep": [None, False, {}]}
    assert row["unknown"] == {"future": None, "flag": False}
    assert "at_ms" not in row and "transition" not in row
    assert realm.documents["parent-1"] == original_document

    records = realm.entries[-1]["records"]
    assert records["label"] == controls()["label"]
    assert records["merge_mode"] == "fields"
    assert records["steps"][0]["id"] == "parent-birth"
    record = records["commands"][0]
    assert record["command"]["args"]["row"] == row
    assert {item["ref"] for item in record["created"]} == {
        "parent/asset:" + media["asset"], "parent/clip:" + row["id"]
    }
    assert {binding["key"] for binding in record["bindings"]} >= {"media.owner", "media.track"}
    assert any(read["ref"] == "parent" and read["path"] == ["config", "output", "fps"]
               for read in record["reads"])
    assert any(write["ref"] == "parent/asset:" + media["asset"] and write["before"] == ABSENT
               for write in record["writes"])
    assert any(write["ref"] == "parent/clip:" + row["id"] and write["before"] == ABSENT
               and write["after"]["value"] == row for write in record["writes"])


@pytest.mark.parametrize("kind", ["video", "image"])
@pytest.mark.parametrize("hold", ["missing", "null"])
def test_parent_raw_provider_refuses_missing_or_null_hold_without_normalizing(kind, hold):
    document, media, verified = raw_parent_case(kind)
    source = document["parent_revision"]["payload"]["clips"][1]
    row = copy.deepcopy(source)
    row.update(id="born", asset=media["asset"], at=3.003, clipType=kind)
    if kind == "image":
        for key in ("from", "to", "speed"):
            row.pop(key, None)
    if hold == "missing":
        row.pop("hold")
    else:
        row["hold"] = None
    command = raw_command(row, media, select="parent/clip:ov-a")
    realm = MediaRealm(document, verified)
    before = {key: copy.deepcopy(value) for key, value in (
        ("head", realm.head), ("entries", realm.entries), ("documents", realm.documents),
        ("accepted", realm.accepted))}

    with pytest.raises(CommitRejected) as error:
        submit(realm, command, reads=[])
    details = assert_structured_refusal(error)
    assert details["reason"] == "invalid_edit_command"
    assert_no_write(realm, before)
    assert command["args"]["row"] == row


@pytest.mark.parametrize("hold", ["missing", "null"])
def test_parent_raw_provider_keeps_ordinary_birth_strict_for_missing_or_null_hold(hold):
    document, media, verified = raw_parent_case()
    row = current_birth_row(media)
    if hold == "missing":
        row.pop("hold")
    else:
        row["hold"] = None
    command = raw_command(row, media)
    realm = MediaRealm(document, verified)
    before = {key: copy.deepcopy(value) for key, value in (
        ("head", realm.head), ("entries", realm.entries), ("documents", realm.documents),
        ("accepted", realm.accepted))}

    with pytest.raises(CommitRejected) as error:
        submit(realm, command, reads=[])
    details = assert_structured_refusal(error)
    assert details["reason"] == "invalid_edit_command"
    assert_no_write(realm, before)


@pytest.mark.parametrize("field", ["track", "asset"])
def test_parent_raw_provider_refuses_explicit_null_track_or_asset_without_write(field):
    document, media, verified = raw_parent_case()
    row = current_birth_row(media)
    row[field] = None
    command = raw_command(row, media)
    realm = MediaRealm(document, verified)
    before = {key: copy.deepcopy(value) for key, value in (
        ("head", realm.head), ("entries", realm.entries), ("documents", realm.documents),
        ("accepted", realm.accepted))}

    with pytest.raises(CommitRejected) as error:
        submit(realm, command, reads=[])
    details = assert_structured_refusal(error)
    assert details["reason"] == "invalid_edit_command"
    assert_no_write(realm, before)


def test_parent_raw_provider_rejects_duplicate_source_clip_identity_before_write():
    document, media, verified = raw_parent_case(reviewed=True)
    payload = document["parent_revision"]["payload"]
    duplicate = copy.deepcopy(payload["clips"][1])
    payload["clips"].append(duplicate)
    payload["config"]["clips"].append(copy.deepcopy(duplicate))
    document["parent_revision"]["content_digest"] = _digest(payload)
    command = raw_command(current_birth_row(media), media, select="parent/clip:anchor")
    realm = MediaRealm(document, verified)
    before = {key: copy.deepcopy(value) for key, value in (
        ("head", realm.head), ("entries", realm.entries), ("documents", realm.documents),
        ("accepted", realm.accepted))}

    with pytest.raises(CommitRejected) as error:
        submit(realm, command, reads=[])
    details = assert_structured_refusal(error)
    assert details["reason"] == "identity_conflict"
    assert_no_write(realm, before)


def test_parent_raw_provider_rejects_source_at_ms_and_preserves_input():
    document, media, verified = raw_parent_case(reviewed=True)
    source = document["parent_revision"]["payload"]["clips"][1]
    row = copy.deepcopy(source)
    row.update(id="born", asset=media["asset"], at=3.003, at_ms=1003)
    command = raw_command(row, media, select="parent/clip:anchor")
    realm = MediaRealm(document, verified)
    before = {key: copy.deepcopy(value) for key, value in (
        ("head", realm.head), ("entries", realm.entries), ("documents", realm.documents),
        ("accepted", realm.accepted))}

    with pytest.raises(CommitRejected) as error:
        submit(realm, command, reads=[])
    details = assert_structured_refusal(error)
    assert details["reason"] == "unsupported_timing"
    assert_no_write(realm, before)
    assert command["args"]["row"]["at_ms"] == 1003


def test_parent_raw_provider_refuses_positive_subminimum_and_zero_without_normalizing():
    document, media, verified = raw_parent_case()
    positive = current_birth_row(media, hold=.01)
    positive["from"], positive["to"], positive["speed"] = 0, .01, 1
    positive_command = raw_command(positive, media, select="parent/clip:ov-a")
    realm = MediaRealm(document, verified)
    before = {key: copy.deepcopy(value) for key, value in (
        ("head", realm.head), ("entries", realm.entries), ("documents", realm.documents),
        ("accepted", realm.accepted))}
    with pytest.raises(CommitRejected) as error:
        submit(realm, positive_command, reads=[])
    details = assert_structured_refusal(error)
    assert details["reason"] == "invalid_document"
    assert_no_write(realm, before)

    zero = current_birth_row(media, hold=0)
    zero["from"], zero["to"], zero["speed"] = 0, 0, 1
    zero_command = raw_command(zero, media, select="parent/clip:ov-a")
    before = {key: copy.deepcopy(value) for key, value in (
        ("head", realm.head), ("entries", realm.entries), ("documents", realm.documents),
        ("accepted", realm.accepted))}
    with pytest.raises(CommitRejected) as error:
        submit(realm, zero_command, reads=[])
    details = assert_structured_refusal(error)
    assert details["reason"] == "invalid_edit_command"
    assert_no_write(realm, before)


def test_parent_raw_provider_rejects_incoming_transition_instead_of_dropping_it():
    document, media, verified = raw_parent_case(reviewed=True)
    row = current_birth_row(media)
    row["transition"] = {"type": "crossfade", "duration": .5, "opaque": [False, None]}
    command = raw_command(row, media, select="parent/clip:anchor")
    realm = MediaRealm(document, verified)
    before = {key: copy.deepcopy(value) for key, value in (
        ("head", realm.head), ("entries", realm.entries), ("documents", realm.documents),
        ("accepted", realm.accepted))}

    with pytest.raises(CommitRejected) as error:
        submit(realm, command, reads=[])
    details = assert_structured_refusal(error)
    assert details["reason"] == "unsupported_timing"
    assert_no_write(realm, before)
    assert command["args"]["row"]["transition"] == {
        "type": "crossfade", "duration": .5, "opaque": [False, None]
    }
