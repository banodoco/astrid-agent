"""CLI adapters for saved timeline edits and their conflict recovery."""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping

from astrid.pack_api import print_result
from astrid.core.cli.domain_output import DomainResult
from astrid.packs.timeline.cli_common import need_project


def _open(parsed):
    from astrid.sdk.timeline.commit_session import Timeline

    return Timeline.open(need_project(parsed), parsed.ref, client=parsed.client)


def _error(exc):
    from astrid.sdk.contracts import ErrorObject

    details = dict(getattr(exc, "details", {}) or {})
    failure = getattr(exc, "failure", None)
    if isinstance(failure, Mapping):
        details.update(failure.get("details") or {})
    clash = getattr(exc, "clash", None)
    pending_id = getattr(exc, "pending_id", None)
    if isinstance(clash, Mapping):
        details["clash"] = dict(clash)
    if pending_id:
        details["pending_id"] = pending_id
    return DomainResult.failure(ErrorObject(
        getattr(exc, "code", None) or "timeline_edit_error", str(exc), details,
    ))


def _cmd_list(parsed):
    timeline = _open(parsed)
    records = timeline.pending_list(
        all=parsed.all, actors="all" if parsed.all_actors else "self",
    )
    return print_result(
        DomainResult.success({"pending_changes": records}), as_json=parsed.json,
        human_renderer=lambda _result: _render_pending_list(records, parsed),
    )


def _cmd_show(parsed):
    timeline = _open(parsed)
    record = timeline.pending_show(parsed.ref_id)
    return print_result(
        DomainResult.success(record), as_json=parsed.json,
        human_renderer=lambda _result: _render_pending_show(record, parsed),
    )


def _record_summary(record):
    if record.get("state") == "landed":
        closed = record.get("closed_by") if isinstance(record.get("closed_by"), Mapping) else {}
        publication = closed.get("publication_id")
        return f"Already landed as {publication}" if publication else "Already landed"
    if record.get("state") == "dropped":
        return record.get("dropped_reason") or "Dropped"
    clash = record.get("clash") if isinstance(record.get("clash"), Mapping) else {}
    failure = record.get("failure") if isinstance(record.get("failure"), Mapping) else {}
    details = failure.get("details") if isinstance(failure.get("details"), Mapping) else {}
    summary = clash.get("summary") or record.get("summary") or details.get("message") or details.get("reason")
    if isinstance(summary, str) and summary:
        return summary
    conflicts = details.get("conflicts")
    if isinstance(conflicts, list) and conflicts:
        return f"{len(conflicts)} conflicting field(s); inspect with pending show"
    return "Saved after a timeline conflict; inspect with pending show"


def _render_pending_list(records, parsed):
    if not records:
        return "No pending changes."
    lines = ["ID  STATE  ORIGIN  ATTEMPTS  LABEL  SUMMARY"]
    for record in records:
        lines.append("  ".join(str(value) for value in (
            record.get("id", "?"), record.get("state", "unknown"),
            record.get("origin", "unknown"), record.get("attempts", 1),
            record.get("label") or "Timeline edit", _record_summary(record),
        )))
    lines.append(f"inspect: astrid timelines pending show {parsed.ref} <id> --project {parsed.project}")
    return "\n".join(lines)


def _render_pending_show(record, parsed):
    lines = [
        f"{record.get('id', '?')} · {record.get('state', 'unknown')} · "
        f"{record.get('origin', 'unknown')} · {record.get('label') or 'Timeline edit'}",
        f"attempts: {record.get('attempts', 1)}",
        f"summary: {_record_summary(record)}",
    ]
    clash = record.get("clash") if isinstance(record.get("clash"), Mapping) else {}
    diff = record.get("diff") or clash.get("diff")
    if diff:
        lines.append("diff:")
        lines.append(json.dumps(diff, indent=2, ensure_ascii=False))
    elif record.get("state") == "pending":
        lines.append("diff: no preview diff is stored; retry to inspect the current preview")
    if clash.get("conflicts"):
        lines.append("conflicts:")
        lines.append(json.dumps(clash["conflicts"], indent=2, ensure_ascii=False))
    if record.get("state") == "pending":
        lines.append(f"next: astrid timelines retry {parsed.ref} {record.get('id')} --project {parsed.project}")
    return "\n".join(lines)


def _cmd_drop(parsed):
    timeline = _open(parsed)
    record = timeline.pending_drop(parsed.ref_id, reason=parsed.reason)
    return print_result(DomainResult.success(record), as_json=parsed.json)


def _retry_data(result):
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    retry = data.get("retry") if isinstance(data.get("retry"), Mapping) else {}
    return {
        "status": result.get("status"),
        "pending_id": result.get("pending_id"),
        "token": retry.get("token"),
        "head": data.get("head"),
        "diff": data.get("diff"),
        "conflicts": data.get("conflicts"),
        "receipts": data.get("receipts"),
        "summary": result.get("summary"),
        "next": result.get("next"),
    }


def _cmd_retry(parsed):
    from astrid.sdk.timeline.retry import commit_preview

    timeline = _open(parsed)
    result = timeline.retry(parsed.ref_id)
    if parsed.commit_token is not None:
        if result.get("status") == "landed":
            data = _retry_data(result)
            if parsed.json:
                return print_result(DomainResult.success(data), as_json=True)
            print(data["summary"] or "This pending change already landed.")
            return 0
        token = result.get("data", {}).get("retry", {}).get("token")
        if result.get("status") != "preview" or token != parsed.commit_token:
            raise ValueError(
                f"retry preview token is stale or invalid for {parsed.ref_id}; "
                f"run astrid timelines retry {parsed.ref} {parsed.ref_id} --project {parsed.project} again"
            )
        result = commit_preview(timeline, result)
        from astrid.sdk.timeline.retry import format_cli_result

        result = format_cli_result(result)
    data = _retry_data(result)
    if parsed.json:
        return print_result(DomainResult.success(data), as_json=True)
    lines = [str(data["summary"] or data["status"])]
    if data["diff"] is not None:
        lines.append(f"diff: {data['diff']}")
    if data["token"]:
        lines.append(f"token: {data['token']}")
    if data["next"]:
        if data["status"] == "preview" and data["token"]:
            lines.append(
                f"next: astrid timelines retry {parsed.ref} {parsed.ref_id} --project {parsed.project} "
                f"--commit {data['token']}"
            )
        elif data["status"] == "re_derive":
            lines.append("next: re-derive needs Python; see SKILL.md ‘When to re-derive’")
        else:
            lines.append(f"next: {data['next']}")
    print("\n".join(lines))
    return 0


def _run(parsed, handler):
    try:
        return handler(parsed)
    except Exception as exc:  # errors retain structured conflict fields for --json callers
        if parsed.json:
            return print_result(_error(exc), as_json=True)
        from astrid.sdk.timeline._common import TimelineEditError

        if isinstance(exc, (TimelineEditError, ValueError)):
            print(str(exc), file=sys.stderr)
            return 1
        raise


def _configure_list(parser):
    parser.add_argument("ref", help="Timeline id or slug.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--all", action="store_true", help="Include landed and dropped records.")
    parser.add_argument("--all-actors", action="store_true", help="Include other actors on this machine.")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=lambda p: _run(p, _cmd_list))


def _configure_show(parser):
    parser.add_argument("ref", help="Timeline id or slug.")
    parser.add_argument("ref_id", help="Pending record id or key.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=lambda p: _run(p, _cmd_show))


def _configure_drop(parser):
    parser.add_argument("ref", help="Timeline id or slug.")
    parser.add_argument("ref_id", help="Pending record id or key.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--reason", help="Why this saved edit was dropped.")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=lambda p: _run(p, _cmd_drop))


def _configure_pending(parser):
    nested = parser.add_subparsers(dest="pending_command", required=True)
    nested.add_parser("list", help="List saved timeline edits.").set_defaults(_configure=_configure_list)
    nested.add_parser("show", help="Show a saved edit and its conflict packet.").set_defaults(_configure=_configure_show)
    nested.add_parser("drop", help="Mark a saved edit dropped without deleting it.").set_defaults(_configure=_configure_drop)
    for action in nested.choices.values():
        configure = action.get_default("_configure")
        configure(action)


def _configure_retry(parser):
    parser.add_argument("ref", help="Timeline id or slug.")
    parser.add_argument("ref_id", help="Pending record id or key.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--commit", dest="commit_token", metavar="TOKEN",
                        help="Commit only if the current retry preview still has this token.")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=lambda p: _run(p, _cmd_retry))


def configure_pending(parser):
    _configure_pending(parser)


def configure_retry(parser):
    _configure_retry(parser)
