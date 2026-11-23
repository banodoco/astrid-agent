"""Small CLI adapters for existing timeline verification and visualization options."""
from __future__ import annotations

import argparse

from astrid.core.cli.domain_output import cli_print as print

LABELS_HELP = (
    "With --at, draw each visible layer's address and box on the verification page."
)
VISUALIZE_LABELS_HELP = (
    "With --view motion or --view contact, include the labels overlay without changing the selected view."
)


def add_labels_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--labels", action="store_true", default=False, help=LABELS_HELP)


def add_visualize_labels_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--labels", action="store_true", default=False, help=VISUALIZE_LABELS_HELP)


def with_labels_layer(layers: list[str] | None, enabled: bool) -> list[str] | None:
    """Add the labels layer once, preserving the order of all explicit layers."""
    if not enabled:
        return layers
    selected = list(layers or [])
    result: list[str] = []
    seen_labels = False
    for layer in selected:
        if layer == "labels":
            if seen_labels:
                continue
            seen_labels = True
        result.append(layer)
    if not seen_labels:
        result.append("labels")
    return result


TIMINGS_HELP = "With --at / --verify: where the time went (startup, comparison, end to end) for this verify."


def add_timings_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timings", action="store_true", help=TIMINGS_HELP)


def print_timings(result: dict) -> None:
    """One line of where a verify's time went (the fast lane records it in ``result["timing"]``)."""
    timing = result.get("timing") or {}
    if not timing:
        print("timings  not recorded for this verify")
        return
    print("timings  " + " · ".join(f"{key.removesuffix('_s').replace('_', ' ')} {value:g}s"
                                    for key, value in timing.items()))
