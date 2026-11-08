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
import time
from dataclasses import asdict, dataclass
from pathlib import Path

FORMAT = "astrid-last-run/v1"
RECORD = "last-run.json"
SUCCESS = ("updated", "already-current", "repaired")
EXIT_CODES = {"updated": 0, "already-current": 0, "repaired": 0,
              "not-updated": 3, "failed": 1, "rolled-back": 1}

# One line of copy for each reason. The terminal, diagnostics and App render these.
REASONS = {
    "self-update-too-old": "releases before 10 can't update from the App, so this one update needs the command.",
    "disk-space": "there isn't enough free disk space for the update.",
    "busy": "Astrid or another installer is busy; try again when it finishes.",
    "needs-repair": "something on this computer is out of step with the installed release; run the install line to repair it.",
    "other-runtime-running": "two copies of Astrid are running for the same workspace.",
    "windows-upgrade-unsupported": "upgrading from an earlier Windows version isn't supported yet.",
    "pinokio-managed": "this Astrid is managed by Pinokio; use Pinokio's Update button.",
    "source-checkout": "this Astrid runs from source checkouts; update those instead.",
    "consent-required": "setup needs your consent; run the exact rerun command shown above.",
    "declined": "you declined the setup, so nothing was changed.",
    "recovered-previous-version": "an earlier update was interrupted and Astrid restored the version before it; run the same command again to update.",
    "runtime-not-running": "Astrid's Runtime did not report its release after the run.",
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


@dataclass(frozen=True)
class Outcome:
    kind: str
    still: int | None
    start: int | None = None
    target: int | None = None
    reason: str | None = None
    mode: str = "install"

    @property
    def exit_code(self) -> int:
        if self.kind == "not-updated" and self.reason == "consent-required":
            return 2  # The bootstrap and journeys read 2 as "rerun with consent".
        return EXIT_CODES[self.kind]

    def headline(self) -> list[str]:
        def release(value):
            return "an unknown release" if value is None else f"Release {value}"
        why = REASONS.get(self.reason or "installer-bug", REASONS["installer-bug"])
        if self.kind == "updated":
            if self.start is None:
                return [f"Astrid installed: {release(self.target)}."]
            return [f"Astrid updated: {release(self.start)} → {release(self.target)}."]
        if self.kind == "already-current":
            return [f"Astrid is already up to date ({release(self.still)}). Nothing was changed."]
        if self.kind == "repaired":
            return [f"Astrid is installed and running ({release(self.still)})."]
        if self.kind == "not-updated":
            if self.still is None and self.start is None:
                first = "Astrid was NOT installed."
            else:
                first = f"Astrid did NOT update. It is still {release(self.still)}"
                first += f"; {release(self.target)} is available." if self.target is not None else "."
            return [first, f"Why: {why}", "Your projects are safe — nothing was changed."]
        if self.kind == "rolled-back":
            return ["Astrid did NOT update: the new version didn't start.",
                    f"Astrid went back to {release(self.still)}. Your projects are unchanged.",
                    "Next: run the same command again. If it fails twice, send the diagnostics to support."]
        still = "Nothing was installed." if self.still is None else f"Astrid is still {release(self.still)}."
        return [f"Astrid did NOT update: {why}", still]

    def record(self) -> dict:
        return {"format": FORMAT, **asdict(self), "exit_code": self.exit_code,
                "reason_line": REASONS.get(self.reason) if self.reason else None,
                "headline": self.headline()[0], "finished_at": int(time.time())}


def reason_of(failure: BaseException | None) -> str:
    """Map a run's failure to the reason enum; unknown failures are errors, never success."""
    from .core import BackupSpaceError, RuntimeOperationError, is_lifecycle_refusal

    if failure is None:
        return "installer-bug"
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


def settle(request: Request, before, resolver, *, failure: BaseException | None = None,
           rolled_back: bool = False) -> Outcome:
    """Observe the world after a run and classify it against the request."""
    try:
        after = resolver()
    except Exception:  # noqa: BLE001 - an unreadable installation is an observation
        after = None
    live = _live(after)
    start = _live(before)
    if start is None and before is not None and before.kind not in {"none"}:
        start = before.selected_release
    target = request.target
    if failure is not None:
        kind = "rolled-back" if rolled_back else "failed"
        return Outcome(kind, live if live is not None else start, start, target, reason_of(failure), request.mode)
    if live is not None and target is not None and live == target:
        if start != target:
            return Outcome("updated", live, start, target, None, request.mode)
        kind = "already-current" if request.mode == "update" else "repaired"
        return Outcome(kind, live, start, target, None, request.mode)
    reason = request.reason
    if reason is None:
        reason = "runtime-not-running" if live is None and target is not None and after is not None \
            and after.selected_release == target else "installer-bug"
    return Outcome("not-updated", live if live is not None else start, start, target, reason, request.mode)


def write_record(path: Path, outcome: Outcome, *protected: Path) -> Path | None:
    """Write the receipt; never into a protected program or data root."""
    from .core import absolute, write_json

    try:
        path = absolute(path)
        if any(path.is_relative_to(absolute(root)) for root in protected):
            return None
        write_json(path, outcome.record())
        return path
    except (OSError, ValueError, TypeError):
        return None


def read_record(path: Path) -> dict | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("format") == FORMAT else None
