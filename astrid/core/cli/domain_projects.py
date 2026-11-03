"""Product projects family CLI.

This module is the product parser for the ``projects`` family: every verb is
**argument parsing plus exactly one SDK call** on the composed
:class:`~astrid.sdk.client.AstridClient` (stamped onto every subparser by
:func:`astrid.core.cli.registration.register_product_commands`), and every
handler renders through the shared product output layer
(:mod:`astrid.core.cli.domain_output`) so the exact five-key JSON envelope,
concise human output, and stable exit codes stay aligned with the frozen SDK
contract.

Verbs (exactly these six):

- ``create`` — one ``client.projects.create`` call; accepts
  ``--idempotency-key`` (a fresh key is generated and returned by the SDK
  when absent) and ``--settings <json-object>``;
- ``list`` — one ``client.projects.list`` call (read, no key);
- ``show <ref>`` — one ``client.projects.show`` call resolving *ref* by id
  or slug (read, no key);
- ``update <ref>`` — one ``client.projects.update`` call with the same
  idempotency-key contract as create;
- ``select <ref>`` — one ``client.projects.select`` call persisting the
  resolved project as the workspace/user routing preference;
- ``current`` — one ``client.projects.current`` call returning the selected
  project and the scope that supplied the selection.

This module contains **no SQL**, **no repository logic**, and **no
domain rules**: it parses argv, makes one SDK call, and renders the
returned envelope.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from astrid.core.cli.domain_output import print_result
from astrid.core.cli.registration import CommandSpec, register_product_commands

__all__ = ["COMMANDS", "build_parser"]

_FAMILY = "projects"


def _parse_json_object(value: str) -> dict[str, Any]:
    """Parse a ``--settings`` JSON object argument (usage error otherwise)."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid JSON object: {exc.msg}"
        ) from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def _add_json_flag(subparser: argparse.ArgumentParser, *, default: bool = True) -> None:
    subparser.add_argument(
        "--json",
        action="store_true",
        default=default,
        help=("Print the exact SDK envelope (ok/data/error/receipt/idempotency_key)."
              if not default else "Print the exact SDK envelope (ok/data/error/receipt/idempotency_key); default output."),
    )


def _add_idempotency_key(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--idempotency-key",
        dest="idempotency_key",
        default=None,
        help="Caller idempotency key (a fresh key is generated when absent).",
    )


# -- handlers (one SDK call each, no domain rules) -------------------------


def _cmd_create(parsed: argparse.Namespace) -> int:
    result = parsed.client.projects.create(
        slug=parsed.slug,
        name=parsed.name,
        metadata=parsed.settings,
        idempotency_key=parsed.idempotency_key,
    )
    if result.ok and getattr(parsed, "select", False):
        selected = parsed.client.projects.select(parsed.slug)
        if not selected.ok:
            return print_result(selected, as_json=parsed.json)
        result = selected
    if result.ok and not parsed.json:
        project = result.data.get("project", result.data) if isinstance(result.data, dict) else {}
        print(f"Created project {project.get('name') or parsed.name} ({project.get('slug') or parsed.slug})")
        print("selected: yes" if parsed.select else "next: astrid projects select " + parsed.slug)
        return 0
    return print_result(result, as_json=parsed.json)


def _cmd_list(parsed: argparse.Namespace) -> int:
    result = parsed.client.projects.list()
    if result.ok and not parsed.json:
        data = result.data
        rows = data[0] if isinstance(data, (list, tuple)) and len(data) == 2 and isinstance(data[0], list) else data
        rows = [dict(row) for row in rows or [] if isinstance(row, dict)]
        current = parsed.client.projects.current()
        selected = current.data.get("project") if current.ok and isinstance(current.data, dict) else {}
        selected_refs = {
            selected.get(key) for key in ("project_id", "id", "slug")
        } if isinstance(selected, dict) else set()
        if not rows:
            print("No projects yet. Create one with: astrid projects create SLUG --name NAME --select")
            return 0
        print("  PROJECT  SLUG  TIMELINES  LAST EDITED")
        from astrid.sdk.timeline._common import TimelineEditError
        from astrid.sdk.timeline.revisions import RuntimeRevisions
        revisions = RuntimeRevisions(parsed.client.timelines)
        for row in rows:
            ref = row.get("project_id") or row.get("id") or row.get("slug")
            mark = "*" if ref in selected_refs or row.get("slug") in selected_refs else " "
            timelines = parsed.client.timelines.list(str(ref)) if ref else None
            timeline_rows = []
            if timelines is not None and timelines.ok:
                from astrid.sdk.pagination import page_pair
                page = page_pair(timelines.data)
                timeline_rows = list(page[0]) if page else []
                cursor = page[1] if page else None
                while cursor is not None:
                    following = parsed.client.timelines.list(str(ref), cursor=cursor)
                    page = page_pair(following.data) if following.ok else None
                    if page is None:
                        break
                    timeline_rows.extend(page[0])
                    cursor = page[1]
            edited = []
            for timeline in timeline_rows:
                timeline_ref = timeline.get("timeline_id") or timeline.get("slug") or timeline.get("id")
                if not timeline_ref:
                    continue
                try:
                    history = revisions.list(str(ref), str(timeline_ref), limit=1)
                except TimelineEditError:
                    history = None
                if history and history[0].created_at:
                    edited.append(history[0].created_at)
            print(f"{mark} {row.get('name', '')}  {row.get('slug', '')}  {len(timeline_rows)}  {max(edited, default='-')}")
        return 0
    return print_result(result, as_json=parsed.json)


def _cmd_show(parsed: argparse.Namespace) -> int:
    result = parsed.client.projects.show(parsed.ref)
    return print_result(result, as_json=parsed.json)


def _cmd_update(parsed: argparse.Namespace) -> int:
    result = parsed.client.projects.update(
        parsed.ref,
        name=parsed.name,
        metadata=parsed.settings,
        idempotency_key=parsed.idempotency_key,
    )
    return print_result(result, as_json=parsed.json)


def _cmd_select(parsed: argparse.Namespace) -> int:
    result = parsed.client.projects.select(
        parsed.ref, scope=parsed.scope
    )
    return print_result(result, as_json=parsed.json)


def _cmd_current(parsed: argparse.Namespace) -> int:
    # Runtime project selection is actor-scoped, not a filesystem preference;
    # do not pass the old local-cwd hint into the remote client contract.
    result = parsed.client.projects.current()
    return print_result(result, as_json=parsed.json)


# -- parser ----------------------------------------------------------------


def _configure_create(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("slug", help="Project slug (immutable).")
    subparser.add_argument("--name", required=True, help="Display name.")
    subparser.add_argument("--select", action="store_true", help="Select the new project for subsequent commands.")
    subparser.add_argument(
        "--settings",
        type=_parse_json_object,
        default=None,
        help="Settings as a JSON object (e.g. '{\"owner\": \"team\"}').",
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser, default=False)
    subparser.set_defaults(handler=_cmd_create)


def _configure_list(subparser: argparse.ArgumentParser) -> None:
    _add_json_flag(subparser, default=False)
    subparser.set_defaults(handler=_cmd_list)


def _configure_show(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("ref", help="Project id or slug.")
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_show)


def _configure_update(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("ref", help="Project id or slug.")
    subparser.add_argument("--name", default=None, help="New display name.")
    subparser.add_argument(
        "--settings",
        type=_parse_json_object,
        default=None,
        help="Settings delta as a JSON object.",
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_update)


def _configure_select(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("ref", help="Project id or slug.")
    subparser.add_argument(
        "--scope",
        choices=("workspace", "user"),
        default="workspace",
        help="Preference scope to persist (default: workspace).",
    )
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_select)


def _configure_current(subparser: argparse.ArgumentParser) -> None:
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_current)


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "create",
        help="Create a project (one SDK call, idempotency key returned).",
        configure=_configure_create,
    ),
    CommandSpec(
        "list",
        help="List every project (slug ascending).",
        configure=_configure_list,
    ),
    CommandSpec(
        "show",
        help="Show one project by id or slug.",
        configure=_configure_show,
    ),
    CommandSpec(
        "update",
        help="Update a project's name and/or settings (one SDK call).",
        configure=_configure_update,
    ),
    CommandSpec(
        "select",
        help="Persist a project as the workspace/user routing preference.",
        configure=_configure_select,
    ),
    CommandSpec(
        "current",
        help="Show the selected project and preference scope.",
        configure=_configure_current,
    ),
)


def build_parser(client: Any) -> argparse.ArgumentParser:
    """Build the ``projects`` product-family parser stamped with *client*."""
    parser = argparse.ArgumentParser(
        prog="astrid projects",
        description="Project create/list/show/update/select/current (product family).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    register_product_commands(
        subparsers, COMMANDS, family=_FAMILY, client=client
    )
    return parser
