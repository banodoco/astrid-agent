from __future__ import annotations

import pytest

from astrid.sdk.timeline._commit_wording import present_failure
from astrid.sdk.timeline._common import TimelineSelectionError


@pytest.mark.parametrize("status, reason", [
    ("invalid", "unsupported_operation"),
    ("conflict", "selection_changed"),
    ("refused", "stale_head"),
])
def test_structured_commit_failures_preserve_cause_and_machine_fields(status, reason):
    failure = {
        "code": "conflict",
        "message": "provider message",
        "details": {"status": status, "reason": reason, "base": {"revision_id": "r1"},
                    "current": {"revision_id": "r2"}, "conflicts": [{"ref": "clip:a"}],
                    "path": ["clips", "a"], "hint": "raw hint", "suggested_command": "raw command"},
    }
    presented = present_failure(failure)
    assert presented["headline"] and presented["detail"] and presented["fix"]
    assert presented["outcome"] == "not_applied"
    assert presented["cause"] == failure
    assert presented["cause"]["details"]["reason"] == reason
    if reason == "stale_head":
        assert presented["headline"] == "The edit was made from an older timeline; nothing was committed"
        assert presented["fix"] == "Reopen with Timeline.open(...) and make the edit again."
        assert presented["detail"] == "provider message"


def test_unknown_failure_wording_never_claims_failure_or_nonapplication():
    presented = present_failure({"code": "transport", "message": "socket closed", "details": {"status": "unknown"}},
                                unknown=True)
    assert presented["outcome"] == "unknown"
    assert presented["headline"] and presented["detail"] and presented["fix"]
    assert all(word not in presented["headline"].lower()
               for word in ("fail", "error", "lost", "not committed"))


def test_ambiguous_selection_has_bounded_structured_candidates():
    rows = [{"id": f"clip-{index}", "track": "V1", "start": index, "end": index + 1}
            for index in range(14)]
    error = TimelineSelectionError("intro", rows, total=14)
    assert error.outcome == "not_applied"
    assert error.headline == '"intro" matches 14 clips; nothing was committed'
    assert len(error.candidates) == 12 and error.details["more"] == 2
