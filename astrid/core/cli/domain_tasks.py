"""Product tasks family CLI.

This module is the product parser for the ``tasks`` family: ordinary verbs are
**argument parsing plus exactly one SDK call** on the composed
:class:`~astrid.sdk.client.AstridClient` (stamped onto every subparser by
:func:`astrid.core.cli.registration.register_product_commands`), and every
handler renders through the shared product output layer
(:mod:`astrid.core.cli.domain_output`) so the exact five-key JSON envelope,
concise human output, and stable exit codes stay aligned with the frozen SDK
contract.

Verbs (six one-call resource operations plus one durable observer):

- ``create`` — one ``client.tasks.create`` call; accepts ``--project`` (the
  owning project id or immutable slug), ``--capability``, ``--spec`` (a JSON
  object), and the
  optional ``--input-manifest`` (JSON array), generation publication fields,
  plus ``--idempotency-key`` (a fresh key is generated and returned by the SDK
  when absent);
- ``list`` — one ``client.tasks.list`` call (project-scoped read, no key);
- ``show <task_id>`` — one ``client.tasks.show`` call (read, no key);
- ``cancel <task_id>`` — one ``client.tasks.cancel`` call with the same
  idempotency-key contract as create. Operators cooperatively cancel running
  work without exposing the executor-owned attempt fence; an executor may
  provide the complete ``attempt_id``/``lease_id``/``expected_status_version``
  fence through its internal seam, while partial fences remain invalid;
- ``retry <task_id>`` — one ``client.tasks.retry`` call with the same
  idempotency-key contract;
- ``events <task_id>`` — one ``client.tasks.events`` call returning the
  task's ordered ``core.task`` stream events (read, no key).
- ``follow <task_id>`` — repeatedly reads ``client.tasks.show`` until the
  task reaches a terminal state or the explicit timeout expires.

Executor lifecycle verbs (``claim``, ``start``, ``heartbeat``) and
plan/step semantics (``plan``, ``step``, ``next``, ``ack``, ``skip``,
``hook``) are **absent by construction**: the parser registers exactly the
six product verbs above, so there is no product parsing path that can reach
them.

This module contains **no SQL**, **no repository logic**, and **no domain
rules**: it parses argv, makes one SDK call, and renders the returned
envelope.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping
from typing import Any

from astrid.core.cli.argtypes import execution_request, json_array, json_object
from astrid.core.cli.domain_output import exit_code, print_result
from astrid.core.cli.list_narrow import primary_output_name, resolve_cli_id
from astrid.core.cli.registration import CommandSpec, register_product_commands
from astrid.core.cli.task_progress import (
    follow_summary_record,
    follow_task,
    render_observation_json,
    render_observation_line,
    task_handoff,
)
from astrid.sdk.contracts import DomainResult

__all__ = ["COMMANDS", "build_parser"]

_FAMILY = "tasks"


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def _add_json_flag(subparser: argparse.ArgumentParser, *, default: bool = False, help_text: str | None = None) -> None:
    subparser.add_argument(
        "--json",
        action="store_true",
        default=default,
        help=help_text or "Print the exact SDK envelope (ok/data/error/receipt/idempotency_key).",
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
    kwargs = {
        "project_id": parsed.project,
        "capability": parsed.capability,
        "spec": parsed.spec,
        "input_manifest": parsed.input_manifest,
        "idempotency_key": parsed.idempotency_key,
    }
    if parsed.generation_intent is not None:
        kwargs["generation_intent"] = parsed.generation_intent
    if parsed.settlement_effect is not None:
        kwargs["settlement_effect"] = parsed.settlement_effect
    if parsed.execution_request is not None:
        kwargs["execution_request"] = parsed.execution_request
    result = parsed.client.tasks.create(**kwargs)
    if result.ok and isinstance(result.data, dict):
        data = dict(result.data)
        task_id = str(data.get("task_id") or data.get("id") or "")
        run_id = str(data.get("run_id") or "") or None
        if task_id:
            data.setdefault("task_id", task_id)
            data["handoff"] = task_handoff(
                project=parsed.project,
                task_id=task_id,
                run_id=run_id,
            )
            result = DomainResult.success(
                data,
                receipt=result.receipt,
                idempotency_key=result.idempotency_key,
            )
    if parsed.json or not result.ok:
        return print_result(result, as_json=parsed.json)
    data = result.data
    if isinstance(data, dict) and isinstance(data.get("handoff"), dict):
        print(f"task admitted: {data['task_id']}")
        if data.get("run_id"):
            print(f"run: {data['run_id']}")
        print(f"follow: {data['handoff']['follow']}")
        print(f"inspect: {data['handoff']['inspect']}")
        print(f"events: {data['handoff']['events']}")
        if data["handoff"].get("open"):
            print(f"open: {data['handoff']['open']}")
        print(f"recent: {data['handoff']['recent']}")
        return 0
    return print_result(result)


def _cmd_list(parsed: argparse.Namespace) -> int:
    from astrid.core.cli.list_narrow import all_pages, list_rows, narrow, render_list_table

    # The Runtime pages oldest first, so the newest rows are on the last page: read them all
    # (the first page alone showed a project's oldest work as its "newest").
    result = all_pages(parsed.client.tasks.list, parsed.project)
    narrowed, total = narrow(result, capability=parsed.capability, limit=parsed.limit, as_json=parsed.json)
    if parsed.json:
        return print_result(narrowed, as_json=True)
    return print_result(
        narrowed,
        as_json=False,
        human_renderer=lambda shown: render_list_table(
            list_rows(shown),
            total=total,
            output_of=primary_output_name,
            noun="tasks",
        ),
    )


def _task_id(parsed: argparse.Namespace) -> tuple[str | None, int | None]:
    """The full task id for a per-id verb (a unique hex prefix is resolved against the project's list)."""
    return resolve_cli_id(parsed.client.tasks.list, parsed.project, parsed.task_id, noun="task", as_json=parsed.json)


def _cmd_show(parsed: argparse.Namespace) -> int:
    task_id, code = _task_id(parsed)
    if task_id is None:
        return code
    result = parsed.client.tasks.show(task_id)
    return print_result(result, as_json=parsed.json)


def _cmd_cancel(parsed: argparse.Namespace) -> int:
    task_id, code = _task_id(parsed)
    if task_id is None:
        return code
    result = parsed.client.tasks.cancel(
        task_id,
        idempotency_key=parsed.idempotency_key,
    )
    return print_result(result, as_json=parsed.json)


def _cmd_retry(parsed: argparse.Namespace) -> int:
    task_id, code = _task_id(parsed)
    if task_id is None:
        return code
    result = parsed.client.tasks.retry(
        task_id,
        idempotency_key=parsed.idempotency_key,
    )
    return print_result(result, as_json=parsed.json)


def _cmd_events(parsed: argparse.Namespace) -> int:
    task_id, code = _task_id(parsed)
    if task_id is None:
        return code
    result = parsed.client.tasks.events(task_id)
    return print_result(result, as_json=parsed.json)


def _follow_kwargs(parsed: argparse.Namespace) -> dict[str, Any]:
    return {"project": parsed.project, "poll_seconds": parsed.poll_seconds, "timeout_seconds": parsed.timeout_seconds}


def _completed_line(data: Mapping[str, Any]) -> str:
    outputs = data.get("outputs") or []
    label = "-"
    if outputs and isinstance(outputs[0], Mapping):
        resource = outputs[0].get("resource")
        label = outputs[0].get("location") or (resource.get("filename") if isinstance(resource, Mapping) else None) or "-"
    return f"completed task={data.get('task_id') or '-'} run={data.get('run_id') or '-'} output={label}"


def _cmd_follow(parsed: argparse.Namespace) -> int:
    task_id, code = _task_id(parsed)
    if task_id is None:
        return code
    parsed.task_id = task_id
    if parsed.json:
        # JSONL: one observation per line as it happens, then one closing record.
        result = follow_task(
            parsed.client, parsed.task_id, **_follow_kwargs(parsed),
            stream=sys.stdout, render=render_observation_json,
        )
        print(json.dumps(follow_summary_record(result), sort_keys=True, default=str), flush=True)
        return exit_code(result)
    if not sys.stdout.isatty():
        # Piped: one compact line per observation, then one summary line.
        result = follow_task(
            parsed.client, parsed.task_id, **_follow_kwargs(parsed),
            stream=sys.stdout, render=render_observation_line,
        )
        if result.ok and isinstance(result.data, Mapping):
            print(_completed_line(result.data), flush=True)
            return 0
        return print_result(result)
    print(f"following task {parsed.task_id} in project {parsed.project}")
    result = follow_task(parsed.client, parsed.task_id, **_follow_kwargs(parsed), stream=sys.stdout)
    # Human mode has already shown the successful terminal observation.  A
    # failed terminal state still needs one concise error line.
    if result.ok and isinstance(result.data, dict):
        print("completed")
        outputs = result.data.get("outputs")
        if isinstance(outputs, list) and outputs:
            for output in outputs:
                if isinstance(output, dict) and output.get("location"):
                    print(f"output: {output['location']}")
        elif result.data.get("run_id"):
            print(f"output: available through run {result.data['run_id']}")
        handoff = result.data.get("handoff")
        if isinstance(handoff, dict):
            if handoff.get("open"):
                print(f"open: {handoff['open']}")
            print(f"inspect: {handoff['inspect']}")
            print(f"events: {handoff['events']}")
            print(f"recent: {handoff['recent']}")
        return 0
    return print_result(result)


# -- parser ----------------------------------------------------------------


def _configure_create(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument(
        "--capability", required=True, help="Capability id this task invokes."
    )
    subparser.add_argument(
        "--spec",
        type=json_object,
        required=True,
        help="Immutable task spec as a JSON object (e.g. '{\"size\": 1}').",
    )
    subparser.add_argument(
        "--input-manifest",
        dest="input_manifest",
        type=json_array,
        default=None,
        help="Optional input manifest as a JSON array.",
    )
    subparser.add_argument(
        "--generation-intent",
        dest="generation_intent",
        type=json_object,
        default=None,
        help=(
            "Optional generation publication declaration JSON. The runtime "
            "validates it and associates settled outputs with a Generation."
        ),
    )
    subparser.add_argument(
        "--settlement-effect",
        dest="settlement_effect",
        type=json_object,
        default=None,
        help=(
            "Optional generic settlement effect JSON. Pair this with "
            "--generation-intent when using the raw task admission path."
        ),
    )
    subparser.add_argument(
        "--execution-request",
        dest="execution_request",
        type=execution_request,
        default=None,
        help=(
            "Target-aware execution request JSON. The target must be exactly "
            "one of default, profile, machine, or runpod."
        ),
    )
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_create)


def _configure_list(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("--capability", default=None, metavar="ID",
                           help="Only tasks of this capability (e.g. rendering.render; a prefix like pixel. works too).")
    subparser.add_argument("--limit", type=int, default=None,
                           help="The newest N (default 50 for the human view, everything with --json; 0 = everything).")
    _add_json_flag(
        subparser,
        default=False,
        help_text="Print the exact SDK envelope with every row and full spec. Default: a short table.",
    )
    subparser.set_defaults(handler=_cmd_list)


def _configure_show(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("task_id", help="Task id, or a unique hex prefix of at least 8 characters.")
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_show)


def _configure_cancel(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("task_id", help="Task id, or a unique hex prefix of at least 8 characters.")
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_cancel)


def _configure_retry(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("task_id", help="Task id, or a unique hex prefix of at least 8 characters.")
    _add_idempotency_key(subparser)
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_retry)


def _configure_events(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("task_id", help="Task id, or a unique hex prefix of at least 8 characters.")
    _add_json_flag(subparser)
    subparser.set_defaults(handler=_cmd_events)


def _configure_follow(subparser: argparse.ArgumentParser) -> None:
    _add_project_arg(subparser)
    subparser.add_argument("task_id", help="Task id, or a unique hex prefix of at least 8 characters.")
    subparser.add_argument(
        "--poll-seconds",
        type=_positive_float,
        default=2.0,
        help="Runtime task-read interval in seconds (default: 2).",
    )
    subparser.add_argument(
        "--timeout-seconds",
        type=_positive_float,
        default=3600.0,
        help="Stop following after this many seconds (default: 3600).",
    )
    _add_json_flag(
        subparser,
        default=False,
        help_text="One JSON object per line (JSONL): each observation, then a final record. "
        "Default: one progress line per observation (a fuller view on a terminal).",
    )
    subparser.set_defaults(handler=_cmd_follow)


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "create",
        help="Admit one immutable task "
        "(one SDK call; idempotency key and receipt returned).",
        configure=_configure_create,
    ),
    CommandSpec(
        "list",
        help="List every task in a project (created_at then id).",
        configure=_configure_list,
    ),
    CommandSpec(
        "show",
        help="Show one task's full immutable read model by id.",
        configure=_configure_show,
    ),
    CommandSpec(
        "cancel",
        help="Cancel one nonterminal task (running work is cooperatively fenced).",
        configure=_configure_cancel,
    ),
    CommandSpec(
        "retry",
        help="Retry one eligible failed/expired task (one SDK call).",
        configure=_configure_retry,
    ),
    CommandSpec(
        "events",
        help="Show one task's ordered core.task stream events.",
        configure=_configure_events,
    ),
    CommandSpec(
        "follow",
        help="Follow durable task state to completion with quiet progress updates.",
        configure=_configure_follow,
    ),
)


def build_parser(client: Any) -> argparse.ArgumentParser:
    """Build the ``tasks`` product-family parser stamped with *client*.

    Exactly the seven product verbs above are registered; executor lifecycle
    and plan/step verbs are absent by construction.
    """
    parser = argparse.ArgumentParser(
        prog="astrid tasks",
        description=(
            "Task create/list/show/cancel/retry/events/follow (product family); "
            "executor lifecycle verbs are not exposed."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    register_product_commands(
        subparsers, COMMANDS, family=_FAMILY, client=client
    )
    return parser
