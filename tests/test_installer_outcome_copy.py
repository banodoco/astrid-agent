"""The run receipt's terminal copy says only what is true (owner decision 17, rule 4)."""

import re
from pathlib import Path

from astrid import installer
from astrid.installer.outcome import NEXT_STEPS, REASONS, Outcome

# Reasons without receipt copy yet. Empty once every reason the installer can emit has copy.
PENDING_COPY: frozenset[str] = frozenset()

# The owner-approved copy for the two reasons that had none (PR #331).
APPROVED_COPY = {
    "legacy-update-record": (
        "this update was started by an older Astrid, so this one can't continue it.",
        "Next: run the update command from a terminal to start a fresh update.",
    ),
    "acknowledgement-unknown": (
        "the update was started, but Astrid can't confirm yet that it was accepted.",
        "Next: wait a minute, then run the same update command again. It checks the earlier attempt "
        "before starting a new one.",
    ),
}

_NAME = r'"([a-z][a-z0-9-]*)"'
_REASON_SITES = (
    re.compile(r"\breason\s*=\s*\(?\s*" + _NAME),                    # assignments, class attributes, keywords
    re.compile(r"\.reason,\s*\w+\.refused\s*=\s*" + _NAME),         # run.reason, run.refused = "...", True
    re.compile(r"\breason\s*=\s*[\w.]+\s+or\s+" + _NAME),            # reason = request.reason or "..."
    re.compile(r"\"reason\"\s*:\s*" + _NAME),                         # receipt-shaped dictionaries
    re.compile(r"LocalUpdateRefusal\(\"[a-z0-9-]+\",\s*" + _NAME),     # a refusal's code
)
_REASON_OF_RETURN = re.compile(r"\breturn\s+" + _NAME)  # outcome.reason_of's mapping


def _emitted_reasons() -> set[str]:
    """Every reason literal the installer package assigns, names or returns in a reason position."""
    found: set[str] = set()
    for path in Path(installer.__file__).parent.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for site in _REASON_SITES:
            found.update(site.findall(text))
        if path.name == "outcome.py":
            found.update(_REASON_OF_RETURN.findall(text))
    return found


def test_the_scan_sees_the_reasons_the_installer_emits():
    assert {"busy", "needs-repair", "autostart-off", "other-runtime-running", "installer-bug"} <= _emitted_reasons()


def test_every_reason_the_installer_can_emit_has_receipt_copy_and_a_next_step():
    emitted = _emitted_reasons() - PENDING_COPY
    assert not emitted - set(REASONS), sorted(emitted - set(REASONS))
    assert not emitted - set(NEXT_STEPS), sorted(emitted - set(NEXT_STEPS))


def test_the_enum_and_its_next_steps_agree_and_no_pending_reason_already_has_copy():
    assert set(REASONS) == set(NEXT_STEPS)
    assert not (PENDING_COPY & set(REASONS))


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


def test_the_approved_copy_for_the_two_pending_reasons_is_exact():
    for reason, (why, next_line) in APPROVED_COPY.items():
        assert REASONS[reason] == why, reason
        assert NEXT_STEPS[reason] == next_line, reason
