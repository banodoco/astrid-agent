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


# --- Target rule: the manifest's release when it names one, else the activated release.

def test_manifest_names_release_and_activates_it_is_updated_from_manifest():
    out = settle(Request("update", 12, target_source="manifest"), _state("runtime", 11),
                 lambda: _state("runtime", 12))
    assert out.kind == "updated" and out.exit_code == 0
    assert out.target_source == "manifest"
    assert out.record()["target_source"] == "manifest"


def test_manifest_names_none_activates_release_is_updated_with_activated_source():
    out = settle(Request("update", 12, target_source="activated"), _state("runtime", 11),
                 lambda: _state("runtime", 12))
    assert out.kind == "updated" and out.exit_code == 0
    assert out.target == 12 and out.target_source == "activated"
    assert out.headline() == ["Astrid updated: Release 11 → Release 12."]


def test_manifest_names_release_but_another_is_activated_is_not_updated():
    out = settle(Request("update", 13, target_source="manifest"), _state("runtime", 11),
                 lambda: _state("runtime", 12))
    assert out.kind == "not-updated" and out.exit_code == 3
    assert out.target == 13 and out.target_source == "manifest"


# --- Second warning: another copy of Astrid is still running after a success.

def _with_runtimes(state, *runtimes):
    return SimpleNamespace(**{**vars(state), "live_runtimes": runtimes})


OTHER_COPY = SimpleNamespace(alive=True, selected=False)
SELECTED = SimpleNamespace(alive=True, selected=True)
REPAIR_NEXT = "Next: run the repair command to stop it: curl -fsSL https://astrid.haus/install | sh"
DETAILS = "Details: /logs/install.log"


def test_other_copy_warning_is_updated_with_repair_next_line_and_details_last():
    outcome = Outcome("updated", 12, 11, 12, warnings=("other-copy-running",), details="/logs/install.log")
    assert outcome.headline() == [
        "Astrid updated: Release 11 → Release 12, but another copy of Astrid is still running.",
        REPAIR_NEXT,
        DETAILS,
    ]
    assert outcome.exit_code == 0
    assert json.loads(json.dumps(outcome.record()))["warnings"] == ["other-copy-running"]


def test_repair_next_line_uses_the_windows_install_line_on_win32(monkeypatch):
    from astrid.installer import outcome as outcome_module

    monkeypatch.setattr(outcome_module.sys, "platform", "win32")
    lines = Outcome("updated", 12, 11, 12, warnings=("other-copy-running",)).headline()
    assert lines[1] == ("Next: run the repair command to stop it: "
                        "& ([scriptblock]::Create((irm https://astrid.haus/install.ps1)))")


def test_settle_marks_a_successful_update_with_another_live_copy():
    after = _with_runtimes(_state("runtime", 12), SELECTED, OTHER_COPY)
    out = settle(Request("update", 12, target_source="manifest"), _state("runtime", 11), lambda: after)
    assert out.kind == "updated" and out.exit_code == 0
    assert out.warnings == ("other-copy-running",)


def test_another_live_copy_without_a_reached_target_stays_not_updated():
    after = _with_runtimes(_state("runtime", 11), SELECTED, OTHER_COPY)
    out = settle(Request("update", 12, target_source="manifest"), _state("runtime", 11), lambda: after)
    assert out.kind == "not-updated" and out.exit_code == 3
    assert out.reason == "other-runtime-running"
    assert out.warnings == ()


def test_skill_and_other_copy_warnings_stand_together():
    after = _with_runtimes(_state("runtime", 12), SELECTED, OTHER_COPY)
    out = settle(Request("update", 12, target_source="manifest"), _state("runtime", 11),
                 lambda: after, warnings=("skills-not-refreshed",), details="/logs/install.log")
    assert out.warnings == ("skills-not-refreshed", "other-copy-running")
    assert out.headline() == [
        "Astrid updated: Release 11 → Release 12, but its skills couldn't be refreshed and "
        "another copy of Astrid is still running.",
        SKILLS_NEXT,
        REPAIR_NEXT,
        DETAILS,
    ]
    assert out.exit_code == 0


# --- A plain recovery that ends healthy is a repair, never an update, even when it restores an older release.

def test_plain_recovery_restoring_an_older_release_is_repaired_and_keeps_its_reason():
    # An interrupted update stopped the Runtime and moved the pointer to 12; the recovery restored 11.
    stopped = SimpleNamespace(kind="runtime", live_release=None, selected_release=12, live_runtimes=())
    out = settle(Request("install", reason="recovered-previous-version"), stopped, lambda: _state("runtime", 11))
    assert out.kind == "repaired" and out.exit_code == 0
    assert out.reason == "recovered-previous-version"
    assert out.start is None
    assert "An earlier update was interrupted" in " ".join(out.headline())


# --- The Windows upgrade refusal stops before any change: it is a refusal (exit 3), not a failure.

def test_windows_upgrade_refusal_settles_as_not_updated_with_exit_3():
    from astrid.installer.boundary_handoff import WINDOWS_UPGRADE_REFUSAL

    r0 = _state("runtime", 0)
    out = settle(Request("install"), r0, lambda: r0, failure=ValueError(WINDOWS_UPGRADE_REFUSAL))
    assert out.kind == "not-updated" and out.exit_code == 3
    assert out.reason == "windows-upgrade-unsupported"
