"""New controls for exact queued-request hashes on the reload recovery retry."""

from tests.test_fix17_r13_reload_inflight import reload_receipt_row
from tests.test_fix17_required_update_harness import CANDIDATE
from scripts import check_journey_evidence as gate


JOB_ID = "direct-linux-r13-reload-inflight"


def _retry_row():
    row = reload_receipt_row()
    timeline = row["timelines"][0]
    draft = timeline["author"]["draft"]
    recovery = timeline["reload_inflight"]["restore"]["recovery"]
    timeline["reload_inflight"]["restore"]["pending"] = True
    timeline["reload_inflight"]["restore"]["method"] = "try-again"
    recovery.update({
        "method": "try-again", "clicked": True, "completed": True,
        "draft_provenance": True, "queue_removed": True,
        "accepted_revision_id": "accepted-revision-r14",
        "fresh_label_readback": timeline["config"]["draftLabel"],
        "accepted_receipt": {"receipt_id": "receipt-r14",
            "key_sha256": draft["keySha256"], "accepted_revision_id": "accepted-revision-r14",
            "status": "accepted", "response_status": 200},
        "queue_removal": {"empty_after_acceptance": True, "receipt_id": "receipt-r14",
            "accepted_revision_id": "accepted-revision-r14", "key_sha256": draft["keySha256"]},
        "fresh_navigation": {"url": "https://astrid.haus/tools/video-editor?project=p0&timeline=t0",
            "index_sha256": timeline["reload_inflight"]["https"]["candidate_index_sha256"],
            "selected": True, "main_frame": True},
        "retry_attempts": [{
            "body_sha256": draft["requestSha256"],
            "key_sha256": draft["keySha256"],
            "disposition": "allowed",
        }],
    })
    return row


def test_try_again_retry_hashes_must_match_the_held_draft():
    row = _retry_row()
    assert gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE) == []

    recovery = row["timelines"][0]["reload_inflight"]["restore"]["recovery"]
    recovery["retry_attempts"][0]["body_sha256"] = "0" * 64
    assert any("same original" in error
               for error in gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE))


def test_try_again_requires_hash_receipt_for_accepted_attempt():
    row = _retry_row()
    recovery = row["timelines"][0]["reload_inflight"]["restore"]["recovery"]
    recovery["retry_attempts"] = []
    assert any("same original" in error
               for error in gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE))

    row = _retry_row()
    recovery = row["timelines"][0]["reload_inflight"]["restore"]["recovery"]
    recovery["retry_attempts"].append(dict(recovery["retry_attempts"][0]))
    assert any("same original" in error
               for error in gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE))


def test_try_again_acceptance_and_queue_removal_must_link_to_original_idempotency_receipt():
    row = _retry_row()
    assert gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE) == []
    recovery = row["timelines"][0]["reload_inflight"]["restore"]["recovery"]
    recovery["accepted_receipt"]["key_sha256"] = "0" * 64
    assert any("same original" in error
               for error in gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE))

    row = _retry_row()
    recovery = row["timelines"][0]["reload_inflight"]["restore"]["recovery"]
    recovery["queue_removal"]["key_sha256"] = "0" * 64
    assert any("same original" in error
               for error in gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE))

    for object_name in ("accepted_receipt", "queue_removal"):
        for field in ("idempotency_key", "request", "body"):
            row = _retry_row()
            recovery = row["timelines"][0]["reload_inflight"]["restore"]["recovery"]
            recovery[object_name][field] = "raw-secret"
            assert any("same original" in error
                       for error in gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE))


def test_try_again_fresh_navigation_requires_selected_project_and_pinned_index():
    row = _retry_row()
    recovery = row["timelines"][0]["reload_inflight"]["restore"]["recovery"]
    recovery["fresh_navigation"]["url"] = "https://astrid.haus/tools/video-editor?project=other&timeline=t0"
    assert any("same original" in error
               for error in gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE))


def _no_outbox_restore_row():
    row = reload_receipt_row()
    timeline = row["timelines"][0]
    restore = timeline["reload_inflight"]["restore"]
    restore["pending"] = False
    restore["method"] = "restore"
    restore["recovery"] = {"method": "restore", "clicked": True, "completed": True,
        "draft_provenance": True}
    return row


def test_restore_without_outbox_requires_verified_scoped_slot_proof():
    row = _no_outbox_restore_row()
    assert any("same original" in error
               for error in gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE))

    restore = row["timelines"][0]["reload_inflight"]["restore"]
    owner = row["timelines"][0]["author"]["draft"]["ownerId"]
    restore["recovery"]["recovery_slot_proof"] = {
        "projectId": "p0", "timelineId": "t0", "ownerId": owner,
        "outboxOwnerId": owner, "markerMatch": True, "payloadSha256": "a" * 64,
    }
    assert gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE) == []


def test_restore_slot_proof_does_not_accept_unknown_or_mismatched_project_identity():
    row = _no_outbox_restore_row()
    restore = row["timelines"][0]["reload_inflight"]["restore"]
    owner = row["timelines"][0]["author"]["draft"]["ownerId"]
    proof = {"projectId": "p0", "timelineId": "t0", "ownerId": owner,
        "outboxOwnerId": owner, "markerMatch": True, "payloadSha256": "a" * 64}
    restore["recovery"]["recovery_slot_proof"] = proof
    assert gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE) == []
    for field, value in (("projectId", None), ("projectId", "other"),
                         ("timelineId", "other"), ("outboxOwnerId", "other"),
                         ("markerMatch", False), ("payloadSha256", "unknown")):
        row = _no_outbox_restore_row()
        restore = row["timelines"][0]["reload_inflight"]["restore"]
        invalid = dict(proof)
        invalid[field] = value
        restore["recovery"]["recovery_slot_proof"] = invalid
        assert any("same original" in error
                   for error in gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE))


def test_try_again_cannot_use_recovery_slot_without_pending_outbox_request():
    row = _retry_row()
    restore = row["timelines"][0]["reload_inflight"]["restore"]
    restore["pending"] = False
    restore["recovery"]["recovery_slot_proof"] = {
        "projectId": "p0", "timelineId": "t0",
        "ownerId": row["timelines"][0]["author"]["draft"]["ownerId"],
        "outboxOwnerId": row["timelines"][0]["author"]["draft"]["ownerId"],
        "markerMatch": True, "payloadSha256": "a" * 64,
    }
    assert any("same original" in error
               for error in gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE))
