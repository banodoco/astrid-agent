"""Controls for trigger evidence and actual restored document readback."""
import pytest
from scripts import check_journey_evidence as gate
from tests.test_fix17_r13_reload_inflight import CANDIDATE, JOB_ID, reload_receipt_row


def valid_row(trigger="click"):
    row = reload_receipt_row()
    receipt = row["timelines"][0]["reload_inflight"]
    receipt["reload"].update(trigger=trigger, clicked=trigger == "click", click_observed=trigger == "click", watcher_navigation={"frame_id": "frame1", "reason": "reload", "observed_at": "after-publication-before-commit", "url": "https://astrid.haus/tools/video-editor?project=p0&timeline=t0"} if trigger == "watcher" else None)
    if trigger == "watcher": receipt.pop("notice")
    receipt["restore"].update(method="automatic", readback_label=row["timelines"][0]["config"]["draftLabel"])
    return row


@pytest.mark.parametrize("trigger", ["click", "watcher"])
def test_proven_trigger_and_readback_pass(trigger):
    assert gate._fix17_reload_inflight_errors(JOB_ID, valid_row(trigger), CANDIDATE) == []


@pytest.mark.parametrize("mutation", ["no_click", "no_watcher", "offered_only", "wrong_marker", "no_method"])
def test_unproven_trigger_or_draft_fails(mutation):
    row = valid_row("watcher" if mutation == "no_watcher" else "click")
    receipt = row["timelines"][0]["reload_inflight"]
    if mutation == "no_click": receipt["reload"]["click_observed"] = False
    if mutation == "no_watcher": receipt["reload"]["watcher_navigation"] = False
    if mutation == "offered_only":
        receipt["restore"].update(readback_label=None, persisted_label=None, offered=True, pending=True,
                                  draft_label=row["timelines"][0]["config"]["draftLabel"],
                                  draft_id=row["timelines"][0]["author"]["draft"]["intentId"])
    if mutation == "wrong_marker": receipt["restore"]["readback_label"] = "different"
    if mutation == "no_method": receipt["restore"].pop("method")
    assert gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE)

@pytest.mark.parametrize("mutation", ["wrong_frame", "wrong_reason", "wrong_url", "missing_audit", "unproven_recovery", "missing_restore_click"])
def test_watcher_and_recovery_evidence_is_bound_to_the_selected_document(mutation):
    row = valid_row("watcher")
    receipt = row["timelines"][0]["reload_inflight"]
    watch = receipt["reload"]["watcher_navigation"]
    if mutation == "wrong_frame": watch["frame_id"] = ""
    if mutation == "wrong_reason": watch["reason"] = "anchorClick"
    if mutation == "wrong_url": watch["url"] = "https://astrid.haus/tools/video-editor?project=other&timeline=t0"
    if mutation == "missing_audit": receipt["r14_audit"]["requests"] = receipt["r14_audit"]["requests"][:2]
    if mutation == "unproven_recovery": receipt["restore"]["recovery"]["draft_provenance"] = False
    if mutation == "missing_restore_click":
        receipt["restore"]["method"] = "restore"
        receipt["restore"]["recovery"]["method"] = "restore"
    assert gate._fix17_reload_inflight_errors(JOB_ID, row, CANDIDATE)
