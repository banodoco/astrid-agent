"""Product references family CLI — nested beneath media.

This module is the product parser for the runtime-owned nested
``references`` mount. It is **never** a top-level command:
``references`` is reachable only as ``astrid media references <verb>``
through the media family parser (``astrid/core/cli/domain_media.py`` embeds
this parser beneath its ``references`` subcommand), and the product gateway
dispatch never registers a top-level ``references`` family (sense check
SC30).

Every verb is **argument parsing plus exactly one SDK call** on the composed
:class:`~astrid.sdk.client.AstridClient` (stamped onto every subparser by
:func:`astrid.core.cli.registration.register_product_commands`), and every
handler renders through the shared product output layer
(:mod:`astrid.core.cli.domain_output`) so the exact five-key JSON envelope,
concise human output, and stable exit codes stay aligned with the frozen SDK
contract.

Verbs (exactly these nine, one SDK call each):

- ``create`` — ``client.references.create`` (project, frozen ``--kind``,
  ``--name``, exact same-project ``--media`` id, optional
  ``--description``/``--metadata``, and ``--idempotency-key``);
- ``update <ref>`` — ``client.references.update`` (name/description/metadata
  delta; kind and project stay immutable);
- ``archive <ref>`` — ``client.references.archive`` (reversible soft archive;
  every byte and association is preserved);
- ``recover <ref>`` — ``client.references.recover`` (id or unambiguous
  project-local name; safe to repeat);
- ``associate <ref>`` — ``client.references.associate`` (exact ``--media``
  id, frozen ``--role``, optional ``--context-task``/``--ordinal``/
  ``--metadata``);
- ``link`` — ``client.references.link`` (``--from``/``--to`` reference ids,
  frozen ``--kind``; ``related_to`` is symmetric);
- ``set-primary <ref>`` — ``client.references.set_primary``
  (``--media-reference`` association id; atomic primary-canonical
  replacement);
- ``list`` — ``client.references.list`` (active by default,
  ``--include-archived`` is the explicit inclusive read);
- ``show <ref>`` — ``client.references.show`` (always includes archived).

This module contains **no SQL**, **no repository logic**, and **no domain
rules**: it parses argv, makes one SDK call, and renders the returned
envelope. It also installs **no canonical-entrypoint guard**: product parser
modules stay importable through the normal dispatch path (the guard remains
reserved for pack ``run.py`` modules).
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from astrid.core.cli.domain_output import print_result
from astrid.core.cli.registration import CommandSpec, register_product_commands

# Parser vocabularies are presentation-only.  Keep them here instead of
# importing the retired kernel-writer repository merely to obtain constants:
# importing a product parser must not load SQLite or any local authority.
REFERENCE_KINDS: tuple[str, ...] = (
    "character",
    "place",
    "object",
    "clothing",
    "other",
)
MEDIA_REFERENCE_ROLES: tuple[str, ...] = (
    "canonical",
    "used_as_input",
    "depicts",
    "inspired_by",
)
REFERENCE_LINK_KINDS: tuple[str, ...] = (
    "belongs_to",
    "wears",
    "located_in",
    "associated_with",
    "related_to",
)

__all__ = ["COMMANDS", "build_parser"]

_FAMILY = "references"


def _parse_json_object(value: str) -> dict[str, Any]:
    """Parse a ``--metadata`` JSON object argument (usage error otherwise)."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON object: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def _add_json_flag(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Print the exact SDK envelope (ok/data/error/receipt/idempotency_key).",
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


def _cmd_create(parsed: argparse.Namespace) -> int:
    result = parsed.client.references.create(
        project=parsed.project,
        kind=parsed.kind,
        name=parsed.name,
        media_id=parsed.media,
        description=parsed.description,
        metadata=parsed.metadata,
        idempotency_key=parsed.idempotency_key,
    )
    return print_result(result, as_json=parsed.json)


def _cmd_update(parsed: argparse.Namespace) -> int:
    result = parsed.client.references.update(
        parsed.project,
        parsed.ref,
        name=parsed.name,
        description=parsed.description,
        metadata=parsed.metadata,
        idempotency_key=parsed.idempotency_key,
    )
    return print_result(result, as_json=parsed.json)


def _cmd_archive(parsed: argparse.Namespace) -> int:
    result = parsed.client.references.archive(
        parsed.project,
        parsed.ref,
        idempotency_key=parsed.idempotency_key,
    )
    return print_result(result, as_json=parsed.json)


def _cmd_recover(parsed: argparse.Namespace) -> int:
    result = parsed.client.references.recover(
        parsed.project,
        parsed.ref,
        idempotency_key=parsed.idempotency_key,
    )
    return print_result(result, as_json=parsed.json)


def _cmd_associate(parsed: argparse.Namespace) -> int:
    if parsed.ordinal is not None:
        from astrid.sdk.contracts import DomainResult, ErrorObject

        return print_result(DomainResult.failure(ErrorObject(
            "validation_error",
            "the Runtime assigns association ordinals; drop --ordinal",
            {"field": "ordinal"},
        )), as_json=parsed.json)
    # Optional lineage flags are forwarded only when given (they used to be
    # parsed and silently dropped).
    extra = {
        key: value
        for key, value in (("metadata", parsed.metadata), ("context_task", parsed.context_task))
        if value is not None
    }
    result = parsed.client.references.associate(
        parsed.project,
        parsed.ref,
        media_id=parsed.media,
        role=parsed.role,
        idempotency_key=parsed.idempotency_key,
        **extra,
    )
    return print_result(result, as_json=parsed.json)


def _cmd_link(parsed: argparse.Namespace) -> int:
    result = parsed.client.references.link(
        parsed.project,
        from_reference_id=parsed.from_reference,
        to_reference_id=parsed.to_reference,
        kind=parsed.kind,
        metadata=parsed.metadata,
        idempotency_key=parsed.idempotency_key,
    )
    return print_result(result, as_json=parsed.json)


def _cmd_set_primary(parsed: argparse.Namespace) -> int:
    result = parsed.client.references.set_primary(
        parsed.project,
        parsed.ref,
        association_id=parsed.media_reference,
        idempotency_key=parsed.idempotency_key,
    )
    return print_result(result, as_json=parsed.json)


def _cmd_list(parsed: argparse.Namespace) -> int:
    result = parsed.client.references.list(
        parsed.project,
        include_archived=parsed.include_archived,
    )
    return print_result(result, as_json=parsed.json)


def _cmd_show(parsed: argparse.Namespace) -> int:
    result = parsed.client.references.show(parsed.project, parsed.ref)
    return print_result(result, as_json=parsed.json)


# -- parser ----------------------------------------------------------------


def _configure_create(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument(
        "--kind",
        choices=REFERENCE_KINDS,
        required=True,
        help=f"Frozen reference kind (choices: {', '.join(REFERENCE_KINDS)}).",
    )
    subparser.add_argument("--name", required=True, help="Reference name.")
    subparser.add_argument(
        "--media",
        required=True,
        help="Exact same-project kernel media id (primary canonical).",
    )
    subparser.add_argument(
        "--description",
        default="",
        help="Optional description (default: empty).",
    )
    subparser.add_argument(
        "--metadata",
        type=_parse_json_object,
        default=None,
        help="Metadata as a JSON object.",
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_create)


def _configure_update(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("ref", help="Reference id or exact project-local name.")
    subparser.add_argument("--name", default=None, help="New name.")
    subparser.add_argument(
        "--description",
        default=None,
        help="New description (use '' to clear).",
    )
    subparser.add_argument(
        "--metadata",
        type=_parse_json_object,
        default=None,
        help="Metadata delta as a JSON object.",
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_update)


def _configure_archive(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("ref", help="Reference id or exact project-local name.")
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_archive)


def _configure_recover(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument(
        "ref",
        help=(
            "Reference id or exact project-local name. Ambiguous names fail "
            "with candidate ids from list --include-archived."
        ),
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_recover)


def _configure_associate(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument(
        "ref",
        help=(
            "Reference id or exact project-local name; ambiguous names fail "
            "with candidate ids."
        ),
    )
    subparser.add_argument(
        "--media",
        required=True,
        help="Exact same-project kernel media id to associate.",
    )
    subparser.add_argument(
        "--role",
        choices=MEDIA_REFERENCE_ROLES,
        required=True,
        help=f"Frozen media-reference role (choices: {', '.join(MEDIA_REFERENCE_ROLES)}).",
    )
    subparser.add_argument(
        "--context-task",
        dest="context_task",
        default=None,
        help="Task that used this media (required for role 'used_as_input'); stored in metadata.context_task.",
    )
    subparser.add_argument(
        "--ordinal",
        type=int,
        default=None,
        help="Not supported: the Runtime assigns the next ordinal.",
    )
    subparser.add_argument(
        "--metadata",
        type=_parse_json_object,
        default=None,
        help="Association metadata as a JSON object.",
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_associate)


def _configure_link(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument(
        "--from",
        dest="from_reference",
        required=True,
        help="Source reference id (related_to is symmetric).",
    )
    subparser.add_argument(
        "--to",
        dest="to_reference",
        required=True,
        help="Target reference id.",
    )
    subparser.add_argument(
        "--kind",
        choices=REFERENCE_LINK_KINDS,
        required=True,
        help=f"Frozen reference-link kind (choices: {', '.join(REFERENCE_LINK_KINDS)}).",
    )
    subparser.add_argument(
        "--metadata",
        type=_parse_json_object,
        default=None,
        help="Link metadata as a JSON object.",
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_link)


def _configure_set_primary(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("ref", help="Reference id or exact project-local name.")
    subparser.add_argument(
        "--media-reference",
        required=True,
        help="Exact same-project canonical association id to promote to primary.",
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_set_primary)


def _configure_list(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument(
        "--include-archived",
        dest="include_archived",
        action="store_true",
        help="Include archived references in the list.",
    )
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_list)


def _configure_show(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("ref", help="Reference id or exact project-local name.")
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_show)


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "create",
        help="Create one active reference with its primary canonical media.",
        configure=_configure_create,
    ),
    CommandSpec(
        "update",
        help="Update name/description/metadata (kind and project stay immutable).",
        configure=_configure_update,
    ),
    CommandSpec(
        "archive",
        help="Soft-archive one reference (reversible; associations stay).",
        configure=_configure_archive,
    ),
    CommandSpec(
        "recover",
        help="Restore by id/name; safe to repeat (changed=false when active).",
        configure=_configure_recover,
    ),
    CommandSpec(
        "associate",
        help="Associate one exact media row with an active reference.",
        configure=_configure_associate,
    ),
    CommandSpec(
        "link",
        help="Create one typed reference link (frozen five kinds).",
        configure=_configure_link,
    ),
    CommandSpec(
        "set-primary",
        help="Replace the primary canonical media atomically.",
        configure=_configure_set_primary,
    ),
    CommandSpec(
        "list",
        help="List active references in a project "
        "(--include-archived is the explicit inclusive read).",
        configure=_configure_list,
    ),
    CommandSpec(
        "show",
        help="Show one reference's full read model (includes archived).",
        configure=_configure_show,
    ),
)


def build_parser(client: Any) -> argparse.ArgumentParser:
    """Build the nested ``media references`` product parser stamped with *client*.

    Exactly the nine verbs above are registered: no aliases and no
    top-level exposure — this parser is only reachable beneath the media
    family.
    """
    parser = argparse.ArgumentParser(
        prog="astrid media references",
        description=(
            "Reference create/update/archive/recover/associate/link/"
            "set-primary/list/show "
            "(nested product family)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    register_product_commands(subparsers, COMMANDS, family=_FAMILY, client=client)
    return parser
