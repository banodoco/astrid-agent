"""The receipt's warnings: a success keeps its exit code and says what did not finish."""

import json
from types import SimpleNamespace

from astrid.installer.outcome import Outcome, Request, receipt_warnings, settle

SKILLS_NEXT = ("Next: to refresh Astrid's skills, rerun the install line with --skills on "
               "(replace this flag in the Way back line).")


def test_updated_with_skill_warning_names_both_releases_and_next_line():
    outcome = Outcome("updated", 12, 11, 12, warnings=("skills-not-refreshed",))
    assert outcome.headline() == [
        "Astrid updated: Release 11 → Release 12, but its skills couldn't be refreshed.",
        SKILLS_NEXT,
    ]
    assert outcome.exit_code == 0


def test_installed_already_current_and_repaired_carry_the_same_phrase():
    installed = Outcome("updated", 12, None, 12, warnings=("skills-not-refreshed",))
    current = Outcome("already-current", 12, 12, 12, warnings=("skills-not-refreshed",))
    repaired = Outcome("repaired", 12, 12, 12, warnings=("skills-not-refreshed",))
    assert installed.headline()[0] == "Astrid installed: Release 12, but its skills couldn't be refreshed."
    assert current.headline()[0] == ("Astrid is already up to date (Release 12), but its skills "
                                     "couldn't be refreshed. Nothing was changed.")
    assert repaired.headline()[0] == "Astrid is installed and running (Release 12), but its skills couldn't be refreshed."
    for outcome in (installed, current, repaired):
        assert outcome.headline()[-1] == SKILLS_NEXT
        assert outcome.exit_code == 0


def test_warning_never_applies_to_a_failed_or_not_updated_receipt():
    failed = Outcome("failed", 11, 11, 12, reason="error", warnings=("skills-not-refreshed",))
    refused = Outcome("not-updated", 11, 11, 12, reason="busy", warnings=("skills-not-refreshed",))
    assert all("skills" not in line for line in failed.headline() + refused.headline())
    assert failed.exit_code == 1 and refused.exit_code == 3


def test_record_carries_the_same_headline_and_warning_for_the_app():
    outcome = Outcome("updated", 12, 11, 12, warnings=("skills-not-refreshed",))
    record = json.loads(json.dumps(outcome.record()))
    assert record["headline"] == outcome.headline()[0]
    assert record["warnings"] == ["skills-not-refreshed"]
    assert record["exit_code"] == 0


def test_clean_success_has_no_warning_or_next_line():
    outcome = Outcome("updated", 12, 11, 12)
    assert outcome.headline() == ["Astrid updated: Release 11 → Release 12."]
    assert json.loads(json.dumps(outcome.record()))["warnings"] == []


def test_receipt_warnings_reads_only_the_skill_sync_result():
    assert receipt_warnings({"skills": {"ok": False, "destinations": []}}) == ("skills-not-refreshed",)
    assert receipt_warnings({"skills": {"ok": True, "destinations": []}}) == ()
    assert receipt_warnings({"skills": None}) == ()
    assert receipt_warnings({}) == ()
    assert receipt_warnings(None) == ()


def _state(kind, release):
    return SimpleNamespace(kind=kind, live_release=release, selected_release=release, live_runtimes=())


def test_settle_passes_warnings_through_only_on_success():
    before = _state("runtime", 11)
    after = _state("runtime", 12)
    success = settle(Request("update", 12), before, lambda: after, warnings=("skills-not-refreshed",))
    assert success.kind == "updated" and success.exit_code == 0
    assert success.warnings == ("skills-not-refreshed",)

    failed = settle(Request("update", 12), before, lambda: after, failure=RuntimeError("boom"),
                    warnings=("skills-not-refreshed",))
    assert failed.kind == "failed" and failed.warnings == ()


def test_update_target_must_be_the_release_that_was_activated():
    # The receipt compares the live release with the target. A target of 0 (a
    # missing manifest release) against a live 12 is a wrong receipt, not an update.
    before = _state("runtime", 11)
    after = _state("runtime", 12)
    assert settle(Request("update", 0), before, lambda: after).kind == "not-updated"
    assert settle(Request("update", 12), before, lambda: after).kind == "updated"
