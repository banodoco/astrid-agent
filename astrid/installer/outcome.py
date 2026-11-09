"""The run receipt: what one install, update, repair or recovery run actually achieved.

``settle`` runs exactly once at the end of every such run, whichever way it
ends. It re-resolves the installation, reads the live Runtime's release at the
active data root and compares it with what the run was asked for. The result
is the only source of the terminal headline, of the exit code and of
``last-run.json`` (beside the installer's ``install.log``, which diagnostics
already read; never inside a program or data root). Exit 0 means the live release is the target.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

# Exit-code contract (ruled by M-INT on PR #319). The target follows the request:
# --update targets the manifest release; a plain install or repair (including
# Pinokio finishing an interrupted update) targets "the selected version is live
# and healthy". 0 = target reached (updated, already-current, repaired; a
# recovery that ends healthy is repaired with reason recovered-previous-version
# as detail); 3 = not updated, including a declined run and a recovery-only
# --update that leaves the release unchanged;
# 2 = setup consent required (kept: the bootstrap and journeys rerun with --yes);
# 1 = failed or rolled back. Readers: App public/install and install.ps1 pass
# the code through; the Pinokio launcher and the journey harness treat every
# non-zero code as "did not finish"; update_job's worker reads its job record,
# never this code. tests/test_update_from_field_states.py pins the mapping.
FORMAT = "astrid-last-run/v1"
RECORD = "last-run.json"
SUCCESS = ("updated", "already-current", "repaired")
EXIT_CODES = {"updated": 0, "already-current": 0, "repaired": 0,
              "not-updated": 3, "failed": 1, "rolled-back": 1}

PUBLIC_INSTALL_LINE = "curl -fsSL https://astrid.haus/install | sh"
PUBLIC_INSTALL_LINE_WINDOWS = "& ([scriptblock]::Create((irm https://astrid.haus/install.ps1)))"


def _repair_next() -> str:
    line = PUBLIC_INSTALL_LINE_WINDOWS if sys.platform == "win32" else PUBLIC_INSTALL_LINE
    return f"Next: run the repair command to stop it: {line}"


# A success whose target was reached, but where something the run also did did
# not finish. The exit code stays 0; the headline, its Next line and last-run.json
# (warnings) carry the same copy in the terminal and the App. Each phrase follows
# "but" in the first line, joined with "and" when a success has several.
WARNINGS: dict[str, dict[str, Any]] = {
    "skills-not-refreshed": {
        "phrase": "its skills couldn't be refreshed",
        "next": "Next: to refresh Astrid's skills, rerun the install line with --skills on (replace this flag in the Way back line).",
    },
    "other-copy-running": {
        "phrase": "another copy of Astrid is still running",
        "next": _repair_next,
    },
}


def _next_line(name: str) -> str:
    value = WARNINGS[name]["next"]
    return value() if callable(value) else value

# One line of copy for each reason. The terminal, diagnostics and App render these.
REASONS = {
    "self-update-too-old": "releases before 10 can't update from the App, so this one update needs the command.",
    "disk-space": "there isn't enough free disk space for the update.",
    "busy": "Astrid or another installer is busy; try again when it finishes.",
    "needs-repair": "something on this computer is out of step with the installed release; run the install line to repair it.",
    "other-runtime-running": "two copies of Astrid are running for the same workspace.",
    "windows-upgrade-unsupported": "upgrading from an earlier Windows version isn't supported yet.",
    "autostart-off": "start at sign-in is off, so this update has to run from a terminal.",
    "unsupported-service-manager": "in-app updates need user systemd 254 or newer on this Linux computer.",
    "acknowledgement-unknown": "the update was started, but Astrid can't confirm yet that it was accepted.",
    "legacy-update-record": "this update was started by an older Astrid, so this one can't continue it.",
    "pinokio-managed": "this Astrid is managed by Pinokio; use Pinokio's Update button.",
    "source-checkout": "this Astrid runs from source checkouts; update those instead.",
    "consent-required": "setup needs your consent; run the exact rerun command shown above.",
    "declined": "you declined the setup, so nothing was changed.",
    "recovered-previous-version": "an earlier update was interrupted and Astrid restored the version before it; run the same command again to update.",
    "runtime-not-running": "the new release was installed, but the running Astrid did not restart onto it.",
    "refused": "the installer refused to continue; the message above says why.",
    "error": "the run stopped with an error; the message above says why.",
    "installer-bug": "the installer finished without installing the new release. This is a bug in Astrid, not something you did.",
}


@dataclass(frozen=True)
class Request:
    """What the run was asked to achieve: ``target`` is the manifest release, when known."""
    mode: str
    target: int | None = None
    reason: str | None = None
    refused: bool = False
    target_source: str | None = None


@dataclass(frozen=True)
class Outcome:
    kind: str
    still: int | None
    start: int | None = None
    target: int | None = None
    reason: str | None = None
    mode: str = "install"
    log: str | None = None  # absolute path of install.log, named in the Details line
    warnings: tuple[str, ...] = ()
    # Where the target came from: "manifest" (the manifest named it), "activated"
    # (the manifest named none; the release this run activated) or "default".
    target_source: str | None = None
    # The installer log, shown last on a warned success; never inside a program root.
    details: str | None = None

    @property
    def exit_code(self) -> int:
        if self.kind == "not-updated" and self.reason == "consent-required":
            return 2  # The bootstrap and journeys read 2 as "rerun with consent".
        return EXIT_CODES[self.kind]

    def headline(self) -> list[str]:
        lines = self._lines()
        if self.kind in SUCCESS and self.warnings:
            lines += [_next_line(name) for name in self.warnings]
            if self.details:
                lines.append(f"Details: {self.details}")
        if self.exit_code != 0:
            lines += next_steps(self.log, self.reason)
        return lines

    def _lines(self) -> list[str]:
        def release(value):
            return "an unknown release" if value is None else f"Release {value}"
        why = REASONS.get(self.reason or "installer-bug", REASONS["installer-bug"])
        # Only a success carries warnings; their phrases follow "but" in the first line.
        notes = (", but " + " and ".join(WARNINGS[name]["phrase"] for name in self.warnings)
                 if self.kind in SUCCESS and self.warnings else "")
        if self.kind == "updated":
            if self.start is None:
                return [f"Astrid installed: {release(self.target)}{notes}."]
            return [f"Astrid updated: {release(self.start)} → {release(self.target)}{notes}."]
        if self.kind == "already-current":
            return [f"Astrid is already up to date ({release(self.still)}){notes}. Nothing was changed."]
        if self.kind == "repaired":
            lines = [f"Astrid is installed and running ({release(self.still)}){notes}."]
            if self.reason == "recovered-previous-version":
                lines.append("An earlier update was interrupted; Astrid restored the version before it. "
                             "Run the command with --update to update.")
            return lines
        if self.kind == "not-updated":
            if self.still is None and self.start is None:
                first = "Astrid was NOT installed."
            else:
                first = f"Astrid did NOT update. It is still {release(self.still)}"
                first += f"; {release(self.target)} is available." if self.target is not None else "."
            # Only a run that installed nothing may say nothing was changed: a release that was
            # installed but is not running is a change.
            safe = ("Your projects are safe." if self.reason == "runtime-not-running"
                    else "Your projects are safe — nothing was changed.")
            return [first, f"Why: {why}", safe]
        if self.kind == "rolled-back":
            return ["Astrid did NOT update: the new version didn't start.",
                    f"Astrid went back to {release(self.still)}. Your projects are unchanged.",
                    "Run the same command again. If it fails twice, do the next step."]
        if self.still is None and self.start is None:
            # A fresh install that failed has nothing to update; it was not installed.
            return [f"Astrid was NOT installed: {why}", "Nothing was installed."]
        still = "Nothing was installed." if self.still is None else f"Astrid is still {release(self.still)}."
        return [f"Astrid did NOT update: {why}", still]

    def record(self) -> dict:
        return {"format": FORMAT, **asdict(self), "exit_code": self.exit_code,
                "reason_line": REASONS.get(self.reason) if self.reason else None,
                "headline": self.headline()[0], "finished_at": int(time.time())}


# The support line: only for a bug in Astrid or an unexplained error.
NEXT_STEP = "Next: open astrid.haus > Settings > Updates > Download diagnostics, and send it to support."

# One next action for each reason; every reason has its own, and only
# installer-bug and error send the person to support.
NEXT_STEPS = {
    "self-update-too-old": "Next: run the install command once; after that Update now works.",
    "disk-space": "Next: free up at least the amount named above, then run the same command again.",
    "busy": "Next: quit the other Astrid window (or let the other installer finish), "
            "then run the same command again.",
    "needs-repair": "Next: run the install command without --update to repair this installation, "
                    "then update again.",
    "other-runtime-running": "Next: stop the other Astrid, then run the same command again.",
    "windows-upgrade-unsupported": "Next: this Windows version can't be upgraded this way. "
                                   "Open astrid.haus > Settings > Updates > Download diagnostics "
                                   "to see what is installed.",
    "autostart-off": "Next: run the update command from a terminal. It updates Astrid and starts it, "
                     "and your choice stays off.",
    "unsupported-service-manager": "Next: run the update command from a terminal instead.",
    "acknowledgement-unknown": "Next: wait a minute, then run the same update command again. It checks the "
                               "earlier attempt before starting a new one.",
    "legacy-update-record": "Next: run the update command from a terminal to start a fresh update.",
    "pinokio-managed": "Next: use the Update button in Pinokio.",
    "source-checkout": "Next: update the source checkout, then run the same command again.",
    "consent-required": "Next: run the install command again and accept the setup choices, "
                        "or answer the prompts.",
    "declined": "Next: nothing to do. To install later, run the install command again "
                "and accept the setup choices.",
    "recovered-previous-version": "Next: Run the same command again to finish updating.",
    "runtime-not-running": "Next: run the same command again so Astrid restarts on the new release.",
    "refused": "Next: fix what the message above names, then run the same command again.",
    "error": NEXT_STEP,
    "installer-bug": NEXT_STEP,
}


def next_step(reason: str | None) -> str:
    """The next action for ``reason``; an unknown or missing reason is an installer bug."""
    return NEXT_STEPS.get(reason or "installer-bug", NEXT_STEPS["installer-bug"])


CONSOLE_DETAILS = "Details: shown above in this window (the installer log was not used)."


def next_steps(log: str | Path | None = None, reason: str | None = None) -> list[str]:
    """The two closing lines of every non-zero outcome; the one place they are written.

    The Next line is ``reason``'s own action (``next_step``).

    ``log`` is the absolute install.log this run wrote. When the run could not
    use it safely (before selection, or a log inside a selected root), the
    diagnostics went to this window instead, and the line says so.
    """
    return [next_step(reason), f"Details: {Path(log).absolute()}" if log else CONSOLE_DETAILS]


# Refusals that name their own reason on the exception; the enum (REASONS) is their only source of copy.
NAMED_REFUSAL_REASONS = frozenset({"autostart-off", "needs-repair"})
# Refusals recognised by their message: boundary_handoff refuses a Windows upgrade before any stop.
MESSAGE_REFUSAL_REASONS = frozenset({"windows-upgrade-unsupported"})


def _refusal_reason(failure: BaseException | None) -> str | None:
    """The reason of a failure that refused before any change (not-updated, exit 3), or None."""
    if failure is None:
        return None
    named = getattr(failure, "reason", None)
    if named in NAMED_REFUSAL_REASONS:
        return named
    reason = reason_of(failure)
    return reason if reason in MESSAGE_REFUSAL_REASONS else None


def reason_of(failure: BaseException | None) -> str:
    """Map a run's failure to the reason enum; unknown failures are errors, never success."""
    from .core import BackupSpaceError, RuntimeOperationError, is_lifecycle_refusal

    if failure is None:
        return "installer-bug"
    named = getattr(failure, "reason", None)
    if named in NAMED_REFUSAL_REASONS:
        return named
    message = str(failure)
    if isinstance(failure, BackupSpaceError):
        return "disk-space"
    if isinstance(failure, BlockingIOError) or "busy" in message.lower() or "Finish or cancel" in message:
        return "busy"
    if isinstance(failure, RuntimeOperationError) and is_lifecycle_refusal(failure):
        return "busy"
    if "earlier Windows version" in message:
        return "windows-upgrade-unsupported"
    if "Pinokio" in message:
        return "pinokio-managed"
    if "source checkouts" in message:
        return "source-checkout"
    if "repair" in message.lower():
        return "needs-repair"
    if isinstance(failure, ValueError):
        return "refused"
    return "error"


def _live(state) -> int | None:
    if state is None:
        return None
    if state.kind == "pinokio":
        return state.selected_release  # Pinokio starts the Runtime; there is no live owner to ask.
    return state.live_release


def receipt_warnings(outcomes: dict | None) -> tuple[str, ...]:
    """The warnings a settled run's own steps raised; they never change the exit code."""
    skills = (outcomes or {}).get("skills")
    if isinstance(skills, dict) and not skills.get("ok"):
        return ("skills-not-refreshed",)
    return ()


def _safe_refusal(failure: BaseException) -> bool:
    from .core import is_lifecycle_refusal

    return is_lifecycle_refusal(failure)


def settle(request: Request, before, resolver, *, failure: BaseException | None = None,
           rolled_back: bool = False, warnings: tuple[str, ...] = (),
           details: str | None = None) -> Outcome:
    """Observe the world after a run and classify it against the request."""
    try:
        after = resolver()
    except Exception:  # noqa: BLE001 - an unreadable installation is an observation
        after = None
    live = _live(after)
    # A plain install or repair that recovered an interrupted update is a repair. The pointer is
    # not its start when no live owner was observed; --update keeps its own receipt rule.
    recovered = request.reason == "recovered-previous-version" and request.mode != "update"
    start = _live(before)
    if start is None and before is not None and before.kind not in {"none"} and not recovered:
        start = before.selected_release
    target = request.target
    source = request.target_source
    others = [r for r in (getattr(after, "live_runtimes", ()) or ()) if r.alive and not r.selected]
    refusal = _refusal_reason(failure)
    if refusal is not None:
        # Refused before any stop, journal or copy (a Windows upgrade, autostart-off, or a malformed
        # saved choice found at admission): nothing was changed, so a refusal that keeps its own reason.
        request, failure = replace(request, refused=True, reason=refusal), None
    if failure is not None and request.mode == "update" and _safe_refusal(failure):
        # Runtime refused the stop safely (nothing destroyed) while another live copy runs: a refusal that
        # keeps its own reason (busy), never relabelled as other-runtime-running.
        request, failure = replace(request, refused=True, reason=reason_of(failure)), None
    if failure is not None:
        kind = "rolled-back" if rolled_back else "failed"
        return Outcome(kind, live if live is not None else start, start, target, reason_of(failure), request.mode,
                       target_source=source)
    if request.refused:
        # Refused before any work: nothing was changed, whatever else is running.
        reason: str | None = request.reason or "other-runtime-running"
        return Outcome("not-updated", live if live is not None else start, start, target, reason, request.mode,
                       target_source=source)
    # A second live Runtime is a warning on a success, and a refusal otherwise.
    success_warnings = tuple(warnings) + (("other-copy-running",) if others else ())
    if request.mode == "update":
        # --update: the target is the manifest release, or the activated release when the manifest names none.
        if live is not None and target is not None and live == target:
            kind = "updated" if start != target else "already-current"
            return Outcome(kind, live, start, target, None, request.mode, warnings=success_warnings,
                           target_source=source, details=details)
    elif live is not None and after is not None and live == after.selected_release:
        # A plain install or repair (including Pinokio finishing an interrupted
        # update): the target is "the selected version is live and healthy".
        # A recovery that ends healthy is repaired; its reason stays as detail.
        kind = "repaired" if recovered or start == live else "updated"
        return Outcome(kind, live, start, live, request.reason, request.mode, warnings=success_warnings,
                       target_source=source, details=details)
    if others:
        reason = request.reason or "other-runtime-running"
        return Outcome("not-updated", live if live is not None else start, start, target, reason, request.mode,
                       target_source=source)
    reason = request.reason
    if reason is None:
        # The selected version advanced but the live Runtime did not follow it.
        selected = after.selected_release if after is not None else None
        reason = ("runtime-not-running" if selected is not None and selected != start
                  and selected == (target if request.mode == "update" else selected) else "installer-bug")
    return Outcome("not-updated", live if live is not None else start, start, target, reason, request.mode,
                   target_source=source)


def clear_record(path: Path) -> None:
    """Remove a previous run's receipt at the start of a run, so it can never outlive it.

    Only Astrid's own regular, single-link record is removed.
    """
    try:
        info = os.lstat(path)
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and read_record(path) is not None:
            os.unlink(path)
    except (OSError, ValueError):
        pass


def write_record(path: Path, outcome: Outcome, *protected: Path, run: dict | None = None) -> Path | None:
    """Write the receipt; never into a protected program or data root."""
    from .core import absolute, write_json

    try:
        path = absolute(path)
        if any(path.is_relative_to(absolute(root)) for root in protected):
            return None
        write_json(path, {**outcome.record(), **(run or {})})
        return path
    except (OSError, ValueError, TypeError):
        return None


def read_record(path: Path) -> dict | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("format") == FORMAT else None
