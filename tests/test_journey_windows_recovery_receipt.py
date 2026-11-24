"""Regression controls for the Windows recovery-only installer receipt."""
import copy

from scripts import check_journey_evidence as gate


def recovery_receipt():
    # The observed Round 5 recovery-only path returned before target selection.
    return {
        "command": "install --update", "exit_code": 3, "receipt_invocation_tied": True,
        "invocation": {"started_at": 100, "finished_at": 120, "previous_operation_id": "0" * 32},
        "installer_receipt": {
            "format": "astrid-last-run/v1", "mode": "update", "operation_id": "1" * 32,
            "kind": "not-updated", "exit_code": 3, "still": 13, "target": None,
            "started_at": 101, "finished_at": 119, "reason": "recovered-previous-version",
        },
    }


def test_observed_recovery_only_receipt_accepts_null_target_with_reason():
    recovery = recovery_receipt()
    assert recovery["exit_code"] == 3
    assert recovery["installer_receipt"]["still"] == 13
    assert recovery["installer_receipt"]["target"] is None
    assert recovery["installer_receipt"]["reason"] == "recovered-previous-version"
    assert gate._windows_update_invocation_matches(recovery, "not-updated", 3, 13)


def test_recovery_null_target_requires_explicit_recovered_previous_reason():
    recovery = recovery_receipt()
    recovery["installer_receipt"]["reason"] = "other"
    assert not gate._windows_update_invocation_matches(recovery, "not-updated", 3, 13)


def test_recovery_null_target_cannot_hide_wrong_exit_still_or_invocation():
    recovery = recovery_receipt()
    for mutate in (
        lambda row: row.update(exit_code=0),
        lambda row: row["installer_receipt"].update(still=14),
        lambda row: row.update(receipt_invocation_tied=False),
        lambda row: row["invocation"].update(started_at=9999999999),
    ):
        altered = copy.deepcopy(recovery)
        mutate(altered)
        assert not gate._windows_update_invocation_matches(altered, "not-updated", 3, 13)


def test_successful_update_still_requires_target_release_14():
    update = recovery_receipt()
    update.update(exit_code=0)
    update["installer_receipt"].update(
        kind="updated", exit_code=0, still=14, target=14, reason=None,
    )
    assert gate._windows_update_invocation_matches(update, "updated", 0, 14)
    update["installer_receipt"]["target"] = None
    assert not gate._windows_update_invocation_matches(update, "updated", 0, 14)
