"""Reusable runtime documents for people and pack authors."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from astrid.core.cli.argtypes import input_file, output_file
from astrid.core.cli.domain_output import print_result
from astrid.core.cli.registration import CommandSpec, register_product_commands
from astrid.sdk.contracts import DomainResult, ErrorObject


def _common(parser):
    parser.add_argument("--project", help="Project ID or slug; defaults to the runtime selection.")
    parser.add_argument("--json", action="store_true", help="Print the five-key SDK envelope.")


def _render(result, *, as_json=False):
    if as_json or not result.ok:
        status = print_result(result, as_json=as_json)
        if not as_json and result.error and result.error.details.get("file"):
            print(f"Working file: {result.error.details['file']}", file=sys.stderr)
        return status
    data = result.data
    if isinstance(data, dict) and "content" in data:
        content = data["content"]
        print(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2))
    elif isinstance(data, list):
        for row in data:
            print(f"{row.get('document_id') or row.get('id')}  {row['kind']}  v{row['version']}")
        if not data:
            print("No documents found.")
    elif isinstance(data, dict) and "file" in data:
        print(f"{data.get('message') or ('No changes.' if data.get('changed') is False else 'Saved.' if data.get('changed') else 'Checked out.')} {data['file']}")
    else:
        return print_result(result)
    return 0


def _list(args):
    return _render(args.client.documents.list(args.project, kind=args.kind), as_json=args.json)


def _create(args):
    try:
        text = args.file.read_text(encoding="utf-8")
        content = text if args.format == "markdown" and args.kind != "media.transcript" else json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Invalid JSON constant {value}")))
        result = args.client.documents.create(kind=args.kind, content=content, project=args.project, document_id=args.document_id, idempotency_key=args.idempotency_key)
    except (OSError, ValueError) as exc:
        result = DomainResult.failure(ErrorObject("validation_error", str(exc), {"file": str(args.file)}))
    if result.ok and not args.json:
        print(f"{result.data['document_id']}  {result.data['kind']}  v{result.data['version']}")
        return 0
    return _render(result, as_json=args.json)


def _show(args):
    return _render(args.client.documents.show(args.document, project=args.project), as_json=args.json)


def _checkout(args):
    return _render(args.client.documents.checkout(args.document, file=args.file, project=args.project), as_json=args.json)


def _checkin(args):
    return _render(args.client.documents.checkin(args.file), as_json=args.json)


def _configure_list(parser):
    _common(parser)
    parser.add_argument("--kind", help="Exact namespaced document kind, e.g. editorial.brief.")
    parser.set_defaults(handler=_list)


def _configure_create(parser):
    _common(parser)
    parser.add_argument("--kind", required=True, help="Namespaced kind, e.g. editorial.brief.")
    parser.add_argument("--file", required=True, type=input_file)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--document-id", help="Stable ID for a pack's one-per-project document.")
    parser.add_argument("--idempotency-key")
    parser.set_defaults(handler=_create)


def _configure_show(parser):
    _common(parser)
    parser.add_argument("document")
    parser.set_defaults(handler=_show)


def _configure_checkout(parser):
    _configure_show(parser)
    parser.add_argument("--file", required=True, type=output_file, help="New working-copy path; existing paths are refused.")
    parser.set_defaults(handler=_checkout)


def _configure_checkin(parser):
    parser.add_argument("file", type=input_file)
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_checkin)


COMMANDS = (
    CommandSpec("list", help="List all project documents, optionally filtered by kind.", configure=_configure_list),
    CommandSpec("create", help="Create a Markdown or arbitrary JSON project document.", configure=_configure_create),
    CommandSpec("show", help="Read one project document.", configure=_configure_show),
    CommandSpec("checkout", help="Get a working copy and identity/version sidecar.", configure=_configure_checkout),
    CommandSpec("checkin", help="Save a checked-out file using its pinned identity and version.", configure=_configure_checkin),
)


def build_parser(client: Any):
    parser = argparse.ArgumentParser(prog="astrid documents", description="Reusable runtime documents; checkout/checkin preserves edited files on conflicts.")
    register_product_commands(parser.add_subparsers(dest="command", required=True), COMMANDS, family="documents", client=client)
    return parser
