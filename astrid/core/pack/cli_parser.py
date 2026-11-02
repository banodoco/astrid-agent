"""Parser construction for the pack CLI.

Extracted from ``astrid/core/pack/cli.py`` during the pack CLI split.
``build_parser`` remains the public entry point and binds each command to the
module that owns its implementation.
"""

from __future__ import annotations

import argparse

from astrid.core.cli_choices import RecoverableArgumentParser, add_choice_arg

_TAXONOMY_FIELDS = (
    "origin",
    "install_tier",
    "pack_type",
    "domain",
    "stability",
    "support",
)


def _add_taxonomy_filter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--domain", help="Filter by taxonomy.domain.")
    parser.add_argument("--origin", help="Filter by taxonomy.origin.")
    parser.add_argument(
        "--install-tier", dest="install_tier", help="Filter by taxonomy.install_tier."
    )
    parser.add_argument(
        "--pack-type", dest="pack_type", help="Filter by taxonomy.pack_type."
    )
    parser.add_argument("--stability", help="Filter by taxonomy.stability.")
    parser.add_argument("--support", help="Filter by taxonomy.support.")


def build_parser() -> argparse.ArgumentParser:
    """Build the ``packs`` subcommand parser.

    Handlers are imported from their owning modules so the parser has one
    canonical implementation path and does not depend on the public facade.
    """
    from .cli_basic import (  # noqa: PLC0415
        _handle_list,
        _handle_new,
        _handle_status,
        _handle_validate,
    )
    from .cli_inspect import _handle_inspect  # noqa: PLC0415
    from .cli_search import _handle_agent_index, _handle_search  # noqa: PLC0415

    parser = RecoverableArgumentParser(
        prog="python3 -m astrid.core.pack.cli",
        description="Manage and validate Astrid packs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate", aliases=["check"], help="Statically validate a pack directory."
    )
    validate_parser.add_argument(
        "path", nargs="?", default=".", help="Path to pack root (default: .)"
    )
    validate_parser.add_argument(
        "--warnings", action="store_true", help="Also print non-fatal warnings."
    )
    validate_parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable validation diagnostics."
    )
    validate_parser.set_defaults(handler=_handle_validate)

    new_parser = subparsers.add_parser(
        "new", help="Create a v3 role-based pack starter."
    )
    new_parser.add_argument("pack_id", help="Pack identifier (e.g., my_project).")
    new_parser.add_argument(
        "--starter",
        default="standalone",
        help="Authoring journey: standalone, wrapper, or nested.",
    )
    new_parser.add_argument(
        "--role",
        action="append",
        choices=("action", "ui", "rendering", "shared"),
        dest="roles",
        help="Role to emit; repeat for a mixed starter (default: action).",
    )
    new_parser.add_argument(
        "--destination",
        dest="destination",
        help="Pack root to create (nested defaults to integrations/astrid).",
    )
    new_parser.add_argument(
        "--dependency",
        default="external-package",
        help="External package name for the wrapper journey.",
    )
    new_parser.add_argument(
        "--external-module",
        default="external_package",
        dest="external_module",
        help="Importable module used by the wrapper action.",
    )
    new_parser.set_defaults(handler=_handle_new)

    list_parser = subparsers.add_parser(
        "list", aliases=["ls"], help="List discovered packs."
    )
    list_parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON."
    )
    list_parser.add_argument(
        "--pack-root", action="append", dest="pack_roots",
        help="Additional pack collection root (repeatable; also honors ASTRID_PACKS_PATH).",
    )
    list_parser.add_argument("--category", help="Filter by metadata.category.")
    _add_taxonomy_filter_args(list_parser)
    add_choice_arg(
        list_parser,
        "--status",
        values=("active", "deprecated", "stub", "experimental"),
        help="Filter by effective status.",
    )
    add_choice_arg(
        list_parser,
        "--visibility",
        values=("visible", "hidden"),
        help="Filter by visibility.",
    )
    list_parser.add_argument(
        "--show-hidden", action="store_true", help="Include hidden packs."
    )
    list_parser.set_defaults(handler=_handle_list)

    inspect_parser = subparsers.add_parser(
        "inspect", help="Show details for a discovered pack."
    )
    inspect_parser.add_argument("pack_id", help="Pack identifier to inspect.")
    inspect_parser.add_argument(
        "--agent",
        action="store_true",
        help="Emit agent-focused subset (purpose, entrypoints, constraints, context, secrets).",
    )
    inspect_parser.add_argument(
        "--json",
        action="store_true",
        dest="json",
        help="Output as JSON.",
    )
    inspect_parser.add_argument(
        "--pack-root", action="append", dest="pack_roots",
        help="Additional pack collection root (also honors ASTRID_PACKS_PATH).",
    )
    inspect_parser.set_defaults(handler=_handle_inspect)

    status_parser = subparsers.add_parser(
        "status", help="Validate and summarize discovered packs."
    )
    status_parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON."
    )
    status_parser.add_argument(
        "--pack-root", action="append", dest="pack_roots",
        help="Additional pack collection root (repeatable; also honors ASTRID_PACKS_PATH).",
    )
    status_parser.add_argument("--category", help="Filter by metadata.category.")
    _add_taxonomy_filter_args(status_parser)
    add_choice_arg(
        status_parser,
        "--status",
        values=("active", "deprecated", "stub", "experimental"),
        help="Filter by effective status.",
    )
    add_choice_arg(
        status_parser,
        "--visibility",
        values=("visible", "hidden"),
        help="Filter by visibility.",
    )
    status_parser.add_argument(
        "--show-hidden", action="store_true", help="Include hidden packs."
    )
    status_parser.set_defaults(handler=_handle_status)

    # ── agent-index ──
    agent_index_parser = subparsers.add_parser(
        "agent-index",
        help="Emit a machine-readable pack index for agents.",
    )
    agent_index_parser.add_argument(
        "--pack-id",
        help="Limit output to a single pack (returns the pack dict or null).",
    )
    agent_index_parser.add_argument(
        "--json",
        dest="json",
        action="store_true",
        help="Output as JSON (default).",
    )
    agent_index_parser.add_argument(
        "--text",
        dest="text_output",
        action="store_true",
        help="Output as a human-readable text table.",
    )
    agent_index_parser.set_defaults(handler=_handle_agent_index)

    # ── search ──
    search_parser = subparsers.add_parser(
        "search",
        help="Search packs by keyword/capability/purpose (ranked).",
    )
    search_parser.add_argument(
        "query",
        nargs="+",
        help="One or more search terms (matched against id, name, "
        "description, keywords, capabilities, and purpose).",
    )
    search_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum results to show (default: 20; <=0 for all).",
    )
    search_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    search_parser.set_defaults(handler=_handle_search)

    return parser
