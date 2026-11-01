"""Product shots family CLI — nested beneath timelines.

This module is the product parser for the runtime-owned nested ``shots``
mount. It is **never** a top-level command: ``shots`` is reachable only as
``astrid timelines shots <verb>`` through the timelines family parser
(``astrid/packs/timeline/cli.py`` embeds this parser beneath its ``shots``
subcommand), and the product gateway dispatch never registers a top-level
``shots`` family (sense check SC29).

Every verb is **argument parsing plus exactly one SDK call** on the composed
:class:`~astrid.sdk.client.AstridClient` (stamped onto every subparser by
:func:`astrid.core.cli.registration.register_product_commands`), and every
handler renders through the shared product output layer
(:mod:`astrid.core.cli.domain_output`) so the exact five-key JSON envelope,
concise human output, and stable exit codes stay aligned with the frozen SDK
contract.

Verbs (one SDK call each):

- ``text list/show/set`` — read immutable shot text and edit it with expected-head protection;

- ``list`` — ``client.shots.list(project)`` (sort_key, then id order);
- ``show`` — ``client.shots.show(project, shot_id)`` (ordered items, media ids,
  positions, and best-effort media names/paths);
- ``create`` — ``client.shots.create`` (project, name, optional
  ``--metadata`` JSON, and ``--idempotency-key``; a fresh key is generated
  and returned when absent);
- ``add`` — ``client.shots.add_item`` (project, shot id, exact same-project
  ``--media`` id, optional ``--position``/``--source-frame``/``--metadata``);
- ``remove`` — ``client.shots.remove_item`` (project, shot id, item id;
  the kernel media row and its bytes are preserved);
- ``reorder`` — ``client.shots.reorder`` (project, shot id, one whole-shot
  ``--items`` permutation; omissions/duplicates/extras are rejected by the
  service before any write).

This module contains **no SQL**, **no repository logic**, and **no domain
rules**: it parses argv, makes one SDK call, and renders the returned
envelope. It also installs **no canonical-entrypoint guard**: product parser
modules stay importable through the normal dispatch path (canonical
entrypoint behavior retained — ``guard_canonical_entrypoint`` remains
reserved for pack ``run.py`` modules).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from astrid.core.cli.domain_output import print_result
from astrid.core.cli.registration import CommandSpec, register_product_commands

__all__ = ["COMMANDS", "build_parser"]

_FAMILY = "shots"


def _parse_json_object(value: str) -> dict[str, Any]:
    """Parse a ``--metadata`` JSON object argument (usage error otherwise)."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid JSON object: {exc.msg}"
        ) from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def _parse_item_ids(value: str) -> list[str]:
    """Split one ``--items`` value on commas into non-empty item ids."""
    items = [token.strip() for token in value.split(",") if token.strip()]
    if not items:
        raise argparse.ArgumentTypeError("must name at least one item id")
    return items


def _add_json_flag(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Print the exact SDK envelope (ok/data/error/receipt/idempotency_key); default output.",
    )


def _add_idempotency_key(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--idempotency-key",
        dest="idempotency_key",
        default=None,
        help="Caller idempotency key (a fresh key is generated when absent).",
    )


def _add_project_arg(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--project",
        required=True,
        default=None,
        help="Owning project id or immutable slug.",
    )


# -- handlers (one SDK call each, no domain rules) -------------------------


def _cmd_list(parsed: argparse.Namespace) -> int:
    result = parsed.client.shots.list(parsed.project)
    return print_result(result, as_json=parsed.json)


def _cmd_text_list(parsed: argparse.Namespace) -> int:
    return print_result(parsed.client.shots.list_text_bindings(
        parsed.project, shot_id=parsed.shot, kind=parsed.kind, slot=parsed.slot,
        include_text=True), as_json=parsed.json)


def _cmd_text_show(parsed: argparse.Namespace) -> int:
    return print_result(parsed.client.shots.show_text_binding(
        parsed.project, parsed.binding, include_text=True), as_json=parsed.json)


def _cmd_text_set(parsed: argparse.Namespace) -> int:
    text = parsed.text
    if parsed.text_file is not None:
        try:
            text = Path(parsed.text_file).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            from astrid.sdk.contracts import DomainResult, ErrorObject
            return print_result(DomainResult.failure(ErrorObject(
                "validation_error", f"Cannot read UTF-8 text file: {exc}", {})), as_json=parsed.json)
    return print_result(parsed.client.shots.set_text_binding(
        parsed.project, shot_id=parsed.shot, kind=parsed.kind, slot=parsed.slot,
        text=text, expected_head=parsed.expected_head,
        idempotency_key=parsed.idempotency_key), as_json=parsed.json)


def _configure_text(subparser: argparse.ArgumentParser) -> None:
    children = subparser.add_subparsers(dest="text_command", required=True)
    kinds = ("prompt", "voiceover_script", "transcript")
    for verb, handler in (("list", _cmd_text_list), ("show", _cmd_text_show), ("set", _cmd_text_set)):
        command = children.add_parser(verb)
        _add_project_arg(command)
        _add_json_flag(command)
        command.set_defaults(handler=handler)
        if verb == "show":
            command.add_argument("binding", help="Binding id; reads its exact immutable text.")
            continue
        command.add_argument("--kind", choices=kinds, required=verb == "set")
        command.add_argument("--slot", help="Optional lowercase prompt slot; only allowed with kind prompt.")
        if verb == "list":
            command.add_argument("--shot", help="Limit to one registered shot.")
        else:
            command.add_argument("shot", help="Registered shot id.")
            content = command.add_mutually_exclusive_group(required=True)
            content.add_argument("--text", help="Exact narration, prompt, or transcript text.")
            content.add_argument("--text-file", type=Path, help="UTF-8 text file (including its exact whitespace).")
            command.add_argument("--expected-head", type=int, required=True, help="0 for creation; current binding head for edits.")
            _add_idempotency_key(command)


def _cmd_show(parsed: argparse.Namespace) -> int:
    result = parsed.client.shots.show(parsed.project, parsed.shot)
    return print_result(result, as_json=parsed.json)


def _cmd_create(parsed: argparse.Namespace) -> int:
    result = parsed.client.shots.create(
        project=parsed.project,
        name=parsed.name,
        metadata=parsed.metadata,
        idempotency_key=parsed.idempotency_key,
    )
    return print_result(result, as_json=parsed.json)


def _cmd_add(parsed: argparse.Namespace) -> int:
    result = parsed.client.shots.add_item(
        parsed.project,
        parsed.shot,
        media_id=parsed.media,
        position=parsed.position,
        source_frame=parsed.source_frame,
        metadata=parsed.metadata,
        idempotency_key=parsed.idempotency_key,
    )
    return print_result(result, as_json=parsed.json)


def _cmd_remove(parsed: argparse.Namespace) -> int:
    result = parsed.client.shots.remove_item(
        parsed.project,
        parsed.shot,
        parsed.item,
        idempotency_key=parsed.idempotency_key,
    )
    return print_result(result, as_json=parsed.json)


def _cmd_reorder(parsed: argparse.Namespace) -> int:
    item_ids = [
        item for value in parsed.items for item in _parse_item_ids(value)
    ]
    result = parsed.client.shots.reorder(
        parsed.project,
        parsed.shot,
        item_ids,
        idempotency_key=parsed.idempotency_key,
    )
    return print_result(result, as_json=parsed.json)


# -- parser ----------------------------------------------------------------


def _configure_list(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_list)


def _configure_show(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("shot", help="Shot id.")
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_show)


def _configure_create(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("--name", required=True, help="Shot name.")
    subparser.add_argument(
        "--metadata",
        type=_parse_json_object,
        default=None,
        help="Metadata as a JSON object.",
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_create)


def _configure_add(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("shot", help="Shot id.")
    subparser.add_argument(
        "--media",
        required=True,
        help="Exact same-project kernel media id to insert.",
    )
    subparser.add_argument(
        "--position",
        type=int,
        default=None,
        help="0-based insertion position (default: append at the end).",
    )
    subparser.add_argument(
        "--source-frame",
        dest="source_frame",
        type=int,
        default=None,
        help="Optional source frame for the inserted item.",
    )
    subparser.add_argument(
        "--metadata",
        type=_parse_json_object,
        default=None,
        help="Metadata as a JSON object.",
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_add)


def _configure_remove(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("shot", help="Shot id.")
    subparser.add_argument("item", help="Item id to remove (media preserved).")
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_remove)


def _configure_reorder(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("shot", help="Shot id.")
    subparser.add_argument(
        "--items",
        action="append",
        required=True,
        metavar="ITEM_ID[,ITEM_ID...]",
        help="The whole-shot permutation of item ids (repeatable/comma-separated).",
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_reorder)


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("text", help="Read or update canonical shot narration, prompts, and transcripts.", configure=_configure_text),
    CommandSpec(
        "list",
        help="List every shot in a project (sort_key, then id order).",
        configure=_configure_list,
    ),
    CommandSpec(
        "show",
        help="Show one project-level shot with ordered items and media paths.",
        configure=_configure_show,
    ),
    CommandSpec(
        "create",
        help="Create one empty shot (one SDK call, idempotency key returned).",
        configure=_configure_create,
    ),
    CommandSpec(
        "add",
        help="Insert one exact-media item into a shot at a validated position.",
        configure=_configure_add,
    ),
    CommandSpec(
        "remove",
        help="Remove one item from a shot (its kernel media and bytes stay).",
        configure=_configure_remove,
    ),
    CommandSpec(
        "reorder",
        help="Reorder a whole shot to one exact permutation of its item ids.",
        configure=_configure_reorder,
    ),
)


def build_parser(client: Any) -> argparse.ArgumentParser:
    """Build the nested ``timelines shots`` product parser stamped with *client*.

    The eight verbs above are registered: no aliases and no top-level
    exposure — this parser is only reachable beneath the timelines family.
    """
    parser = argparse.ArgumentParser(
        prog="astrid timelines shots",
        description=(
            "Project-level reusable shots: text/list/create/show/add/remove/reorder "
            "(nested product family)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    register_product_commands(
        subparsers, COMMANDS, family=_FAMILY, client=client
    )
    return parser
