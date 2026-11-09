"""The run receipt's terminal copy says only what is true (owner decision 17, rule 4)."""

from astrid.installer.outcome import Outcome


def test_a_release_installed_but_not_running_does_not_claim_nothing_changed():
    lines = Outcome("not-updated", 1, 1, 2, "runtime-not-running", "update").headline()
    assert lines[0] == "Astrid did NOT update. It is still Release 1; Release 2 is available."
    assert "nothing was changed" not in " ".join(lines)
    assert "Your projects are safe." in lines


def test_a_run_that_changed_nothing_still_says_so():
    lines = Outcome("not-updated", 1, 1, 2, "busy", "update").headline()
    assert "Your projects are safe — nothing was changed." in lines


def test_a_failed_fresh_install_says_it_was_not_installed_not_that_it_did_not_update():
    lines = Outcome("failed", None, None, 2, "error", "install").headline()
    assert lines[0] == "Astrid was NOT installed: the run stopped with an error; the message above says why."
    assert not any("did NOT update" in line for line in lines)


def test_a_failed_update_of_an_existing_install_still_says_did_not_update():
    lines = Outcome("failed", 1, 1, 2, "error", "update").headline()
    assert lines[0].startswith("Astrid did NOT update: ")
    assert "Astrid is still Release 1." in lines
