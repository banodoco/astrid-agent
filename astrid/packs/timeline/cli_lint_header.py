"""The first line of a lint or check run when a pack check module failed to load."""
from __future__ import annotations

from astrid.core.cli.domain_output import cli_print as print


def print_broken_header(kind: str = "check") -> bool:
    """Print the broken-module line when there is one; return whether it printed."""
    from astrid.packs.rendering.actions.timeline_visualize.layers.base import broken_line

    line = broken_line(kind)
    if line is None:
        return False
    print(line)
    return True
