"""Required update prompt title compatibility boundary."""

import pytest

from scripts import check_journey_evidence as gate


@pytest.mark.parametrize("title", [
    "Update Astrid",
    "Release 14 is ready",
    "Update Astrid Required Show how",
])
def test_required_notice_accepts_canonical_and_legacy_receipt_titles(title):
    notice = {
        "title": title,
        "reason": "This update is required: Required.",
        "action": "Update now",
    }

    assert gate._required_notice_matches(notice, "Required.")


@pytest.mark.parametrize("changes", [
    {"title": "Update Astrid Optional"},
    {"reason": ""},
    {"action": "Later"},
])
def test_required_notice_rejects_wrong_title_or_missing_reason_and_action(changes):
    notice = {
        "title": "Update Astrid",
        "reason": "This update is required: Required.",
        "action": "Update now",
        **changes,
    }

    assert not gate._required_notice_matches(notice, "Required.")
