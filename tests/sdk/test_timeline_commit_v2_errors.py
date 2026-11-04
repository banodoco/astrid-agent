from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import present
from astrid.sdk.timeline._commit_errors import failure
from astrid.sdk.timeline._commit_preview import complete_preview
from tests.editing_conformance.test_doc_patch_v2 import change, patch, submit
from tests.sdk.test_timeline_commit_provider import DetachedRealm, detached


def test_failure_has_stable_structured_details_and_invalid_transport_shape():
    base = {"revision_id": "rev-0", "publication_id": "pub-0"}
    result = failure("invalid_path", "bad path", base, base, path=["clips", "0"])

    assert result["http_status"] == 422
    assert result["code"] == "validation_error"
    assert result["details"] == {
        "status": "invalid",
        "reason": "invalid_path",
        "code": "invalid_path",
        "path": ["clips", "0"],
        "hint": "Use a valid document path from the current timeline",
        "suggested_command": "timeline edit",
        "base": base,
        "current": base,
        "conflicts": [],
    }


def test_intervening_head_is_conflict_with_actual_current_head():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    realm.call([realm.move()])
    from runtime_protocol.timeline_commit import CommitRejected

    with pytest.raises(CommitRejected) as raised:
        realm.call([realm.move(on=4.004)], base=base)

    details = raised.value.failure["details"]
    assert raised.value.failure["http_status"] == 409
    assert raised.value.failure["code"] == "conflict"
    assert details["status"] == "conflict"
    assert details["reason"] in {"intervening_write", "dependency_changed", "value_conflict"}
    assert details["current"] == realm.head
    assert isinstance(details["path"], list)
    assert isinstance(details["hint"], str) and details["hint"]
    assert isinstance(details["suggested_command"], str) and details["suggested_command"]


def test_wrong_previous_value_at_original_base_is_invalid_without_writes():
    realm = DetachedRealm()
    base = detached(realm.head)
    call = patch(change(["name"], present("wrong"), "B"))

    from runtime_protocol.timeline_commit import CommitRejected

    with pytest.raises(CommitRejected) as raised:
        submit(realm, call, reads=[])

    failure_body = raised.value.failure
    details = failure_body["details"]
    assert failure_body["http_status"] == 422
    assert failure_body["code"] == "validation_error"
    assert details["status"] == "invalid"
    assert details["reason"] == "previous_value_mismatch"
    assert details["path"] == ["name"]
    assert details["base"] == base
    assert details["current"] == base
    assert details["hint"]
    assert details["suggested_command"]
    assert realm.head == base
    assert realm.accepted == []
    assert realm.prepared == []


def test_changed_head_replay_remains_a_conflict():
    realm = DetachedRealm()
    base = detached(realm.head)
    submit(realm, patch(change(["name"], present("Renamed Film"), "B")), reads=[])

    from runtime_protocol.timeline_commit import CommitRejected

    with pytest.raises(CommitRejected) as raised:
        submit(realm, patch(change(["name"], present("Renamed Film"), "C")),
               reads=[], base=base)

    failure_body = raised.value.failure
    assert failure_body["http_status"] == 409
    assert failure_body["code"] == "conflict"
    assert failure_body["details"]["status"] == "conflict"
    assert failure_body["details"]["reason"] == "value_conflict"
    assert failure_body["details"]["current"] == realm.head
    assert len(realm.accepted) == 1


def test_malformed_command_at_base_is_invalid():
    realm = DetachedRealm()
    malformed = {
        "schema_version": 1,
        "op": "clip.move",
        "version": "1",
        "select": "parent/clip:clip-a",
        "args": {},
    }

    from runtime_protocol.timeline_commit import CommitRejected

    with pytest.raises(CommitRejected) as raised:
        realm.call([malformed])

    failure_body = raised.value.failure
    assert failure_body["http_status"] == 422
    assert failure_body["code"] == "validation_error"
    assert failure_body["details"]["status"] == "invalid"
    assert failure_body["details"]["reason"] == "invalid_edit_command"
    assert failure_body["details"]["path"] == ["commands"]
    assert failure_body["details"]["current"] is None


def test_preview_projects_landing_head_without_durable_writes():
    realm = DetachedRealm()
    before = {
        "head": detached(realm.head),
        "documents": detached(realm.documents),
        "entries": detached(realm.entries),
        "accepted": detached(realm.accepted),
        "prepared": detached(realm.prepared),
    }

    result = realm.call([realm.move()], dry_run=True)

    after = {
        "head": detached(realm.head),
        "documents": detached(realm.documents),
        "entries": detached(realm.entries),
        "accepted": detached(realm.accepted),
        "prepared": detached(realm.prepared),
    }
    assert after == before
    assert result["receipt"] is None
    assert result["data"]["status"] == "preview"
    assert result["data"]["would_status"] == "accepted"
    assert result["data"]["evaluated_head"] == before["head"]


def test_preview_exposes_authoritative_steps_and_merge_mode_from_prepared_records():
    request = {
        "base_revision_id": "rev-0",
        "base_publication_id": "pub-0",
    }
    prepared = {
        "records": {
            "label": "One note",
            "steps": [{"id": "note", "patch": []}],
            "merge_mode": "atomic",
        }
    }
    result = complete_preview(
        {"data": {"status": "preview"}, "receipt": None},
        prepared,
        request,
        {"revision_id": "rev-1", "publication_id": "pub-1"},
    )

    assert result["receipt"] is None
    assert result["data"]["evaluated_head"] == {
        "revision_id": "rev-1", "publication_id": "pub-1"
    }
    assert result["data"]["would_status"] == "merged"
    assert result["data"]["steps"] == prepared["records"]["steps"]
    assert result["data"]["merge_mode"] == "atomic"
