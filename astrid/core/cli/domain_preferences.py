"""User and project preference shortcuts over runtime Markdown documents."""
from __future__ import annotations

import argparse
from typing import Any

from astrid.core.cli.argtypes import input_file, output_file
from astrid.core.cli.domain_documents import _render
from astrid.core.cli.domain_output import print_result
from astrid.core.cli.registration import CommandSpec, register_product_commands


def _render_preferences(result, as_json):
    if as_json or not result.ok:
        return print_result(result, as_json=as_json)
    data = result.data
    if "precedence" in data:
        print(data["precedence"])
        for scope in ("user", "project"):
            row = data[scope]
            print(f"\n{scope.capitalize()} preferences" + (f" ({row['project_id']})" if row and scope == "project" else ""))
            print(row["content"] or "(empty)" if row else "(no project selected)")
    else:
        print(f"{data['scope'].capitalize()} preferences")
        print(data["content"] or "(empty)")
    return 0


def _view(args):
    result = args.client.preferences.get(args.scope, project=args.project) if getattr(args, "scope", None) else args.client.preferences.view(project=args.project)
    return _render_preferences(result, args.json)


def _common(parser):
    parser.add_argument("--project", help="Project ID or slug; defaults to the runtime selection.")
    parser.add_argument("--json", action="store_true", help="Print the five-key SDK envelope.")


def _configure_user(parser):
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_view, scope="user", project=None)


def _configure_project(parser):
    _common(parser)
    parser.set_defaults(handler=_view, scope="project")


def _configure_context(parser):
    _common(parser)
    parser.set_defaults(handler=_view, scope=None)


def _checkout(args):
    return _render(args.client.preferences.checkout(scope=args.scope, file=args.file, project=args.project), as_json=args.json)


def _checkin(args):
    return _render(args.client.preferences.checkin(args.file), as_json=args.json)


def _edit(args):
    return _render(args.client.preferences.edit(scope=args.scope, project=args.project), as_json=args.json)


def _configure_edit(parser):
    _common(parser)
    parser.add_argument("--scope", choices=("user", "project"), required=True)
    parser.set_defaults(handler=_edit)


def _configure_checkout(parser):
    _configure_edit(parser)
    parser.add_argument("--file", type=output_file, required=True, help="New Markdown working-copy path.")
    parser.set_defaults(handler=_checkout)


def _configure_checkin(parser):
    parser.add_argument("file", type=input_file)
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_checkin)


COMMANDS = (
    CommandSpec("user", help="Read your lasting user preferences.", configure=_configure_user),
    CommandSpec("project", help="Read current or specified project overrides.", configure=_configure_project),
    CommandSpec("context", help="Read labeled user/project preferences and precedence.", configure=_configure_context),
    CommandSpec("checkout", help="Fetch Markdown preferences for editing.", configure=_configure_checkout),
    CommandSpec("checkin", help="Save an edited checkout; conflicts preserve your file.", configure=_configure_checkin),
    CommandSpec("edit", help="Checkout, open $EDITOR, then safely check in changes.", configure=_configure_edit),
)


def build_parser(client: Any):
    parser = argparse.ArgumentParser(prog="astrid preferences", description="Markdown defaults: current request > project > user > defaults. Temporary requests stay in conversation. Persist only when asked.")
    _common(parser)
    parser.set_defaults(handler=_view, client=client, scope=None)
    register_product_commands(parser.add_subparsers(dest="command"), COMMANDS, family="preferences", client=client)
    return parser
