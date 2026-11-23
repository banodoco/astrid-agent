"""Whole-film sweep CLI adapters backed by the V3 timeline checkout."""
from __future__ import annotations

import argparse
import copy
from typing import Any

from astrid.core.cli.domain_output import cli_print as print

CHECK_SWEEP_HELP = (
    "Compare the working copy with the published film at bounded samples across every cut; "
    "unexpected picture changes fail the check."
)
EDIT_SWEEP_HELP = (
    "After saving, compare every cut when an edit re-flows the film, touches more than three cuts, "
    "or --sweep is given."
)
NO_SWEEP_HELP = "Skip the automatic whole-film sweep after an edit."
AUTO_TOUCHED_CUTS = 3


def add_check_sweep_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sweep", action="store_true", help=CHECK_SWEEP_HELP)
    parser.add_argument("--no-sweep", dest="no_sweep", action="store_true", help=NO_SWEEP_HELP)


def add_edit_sweep_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sweep", action="store_true", help=EDIT_SWEEP_HELP)
    parser.add_argument("--no-sweep", dest="no_sweep", action="store_true", help=NO_SWEEP_HELP)


def print_sweep(parsed: Any, checkout: Any) -> int:
    """Run the candidate's V3 sweep. Return 0 expected, 1 unexpected, or 2 not captured."""
    try:
        result = checkout.verify(sweep=True, client=parsed.client)
    except Exception as exc:  # noqa: BLE001 - preserve capture failure as a distinct CLI outcome
        detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        print(f"sweep   not captured: {detail}")
        return 2
    for line in result.get("lines", []):
        print(line)
    return 1 if result.get("unexpected") else 0


def auto_sweep_after_edit(parsed: Any, checkout: Any, *, reflow: bool, touched_cuts: int) -> int:
    """Run after the working copy is saved; ``--no-sweep`` always suppresses the capture."""
    if getattr(parsed, "no_sweep", False):
        return 0
    why = (
        "this edit re-flows the film" if reflow else
        f"this edit touches {touched_cuts} cuts" if touched_cuts > AUTO_TOUCHED_CUTS else
        "you asked for it" if getattr(parsed, "sweep", False) else None
    )
    if why is None:
        return 0
    print(f"sweep   {why}; comparing bounded samples across the film")
    return print_sweep(parsed, checkout)


def touched_cuts(before: dict[str, Any], checkout: Any) -> set[str]:
    """Return cuts containing clips whose authored content changed, before or after the edit."""
    from astrid.sdk import timeline_intent as intent
    from astrid.sdk.timeline_checkout import Checkout
    from astrid.sdk.timeline_cuts import diff_bundles

    changed = {str(row.get("clip_id")) for row in diff_bundles(before, checkout.document())["changes"]}
    found: set[str] = set()
    for document in (Checkout(copy.deepcopy(before)), checkout):
        for clip in document.clips():
            cut_id = intent.cut_of(clip.data)
            if clip.id in changed and cut_id:
                found.add(str(cut_id))
    return found
