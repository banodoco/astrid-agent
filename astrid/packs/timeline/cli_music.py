"""The music policy on `timelines edit`: --music keep-end|plays-on (how the music bed follows a re-time).

Pure apart from the checkout it is given: it sets the checkout's policy, which the re-flow ops read.
"""

from __future__ import annotations

import argparse
from typing import Any

MUSIC_CHOICES = ("keep-end", "plays-on")


def add_music_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--music", choices=MUSIC_CHOICES, default=None, metavar="{keep-end,plays-on}",
                        help="how the music bed follows a re-time: keep-end (default) cuts it at a bar with a crossfade "
                             "so its ending stays on the film's end; plays-on keeps it whole")


def apply_music_policy(tl: Any, parsed: argparse.Namespace) -> None:
    """Set the checkout's bed policy from --music, before the timeline-level ops run."""
    value = getattr(parsed, "music", None)
    if value is not None:
        tl.music = value.replace("-", "_")


def configure_adopt(parser):
    """Current source supports detached preflight; D supplies the command join."""
    parser.add_argument('kind', choices=['music-bed'])
    parser.add_argument('--file', required=True, help='Explicit working-copy file.')
    parser.add_argument('--source', action='append', required=True, help='Ordered source ScopedId; repeat for each arrangement.')
    parser.add_argument('--alignment', required=True, help='JSON file of exact owner/alignment evidence.')
    parser.add_argument('--switches', help='JSON file containing ordered switch records.')
    parser.add_argument('--basis', help='Optional JSON file of an exact canonical common basis.')
    parser.add_argument('--dry-run', action='store_true', help='Validate the detached candidate without saving.')
    parser.set_defaults(handler=run_adopt)


def run_adopt(parsed):
    import json
    import sys
    from pathlib import Path

    from astrid.sdk.timeline._common import TimelineEditError
    from astrid.sdk.timeline.bed_adoption import adopt_music_bed
    from astrid.sdk.timeline.checkout import Checkout
    try:
        tl = Checkout.load(parsed.file)
        candidate = adopt_music_bed(tl.document(), parsed.source,
            switches=json.loads(Path(parsed.switches).read_text()) if parsed.switches else [],
            alignment=json.loads(Path(parsed.alignment).read_text()),
            common_basis=json.loads(Path(parsed.basis).read_text()) if parsed.basis else None)
        if not parsed.dry_run:
            raise TimelineEditError('adopt music-bed: this writer cannot edit adopted music beds; '
                                    'use an arrangement-aware writer or --dry-run to validate without saving')
        bed = candidate['parent']['config']['app']['astrid']['bed']
        print(json.dumps(dict(dry_run=True, arrangements=len(bed['arrangements']), owner=bed['owner']), sort_keys=True))
        return 0
    except (OSError, ValueError, TimelineEditError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
