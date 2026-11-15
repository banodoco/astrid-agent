"""Narrow a project-scoped run/task list: one capability, the newest N (``runs list``, ``tasks list``)."""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Mapping

from astrid.core.cli.domain_output import EXIT_USAGE, print_result
from astrid.sdk.contracts import DomainResult, ErrorObject

_TERMINAL = frozenset({"completed", "succeeded", "success", "failed", "cancelled", "canceled", "expired"})
_SUCCESS = frozenset({"completed", "succeeded", "success"})
_ID_WIDTH = 12
_FULL_ID_LENGTH = 32
_MIN_PREFIX = 8
_HEX = re.compile(r"[0-9a-fA-F]+")


def _capability(row: Mapping[str, Any]) -> str:
    spec = row.get("spec") if isinstance(row.get("spec"), Mapping) else {}
    inner = spec.get("spec") if isinstance(spec.get("spec"), Mapping) else {}
    return str(row.get("capability") or row.get("capability_id") or inner.get("capability_id") or "")


def list_rows(result: Any) -> list[Any]:
    """The row list of a (possibly cursor-paged) list result."""
    data = getattr(result, "data", None)
    rows = data[0] if isinstance(data, list) and len(data) == 2 and isinstance(data[0], list) else data
    return list(rows) if isinstance(rows, list) else []


def narrow(result: Any, *, capability: str | None, limit: int | None, as_json: bool) -> tuple[Any, int]:
    """The same result with only matching rows, newest first, at most ``limit``; and how many matched in all."""
    if not getattr(result, "ok", False):
        return result, 0
    data = result.data
    rows, cursor = (data[0], data[1]) if isinstance(data, list) and len(data) == 2 and isinstance(data[0], list) else (data, None)
    if not isinstance(rows, list):
        return result, 0
    if capability:
        rows = [r for r in rows if isinstance(r, Mapping) and (_capability(r) == capability or _capability(r).startswith(capability))]
    rows = sorted(rows, key=lambda r: str(r.get("created_at") or "") if isinstance(r, Mapping) else "", reverse=True)
    total = len(rows)
    if limit is None:
        limit = 0 if as_json else 50
    if limit and limit > 0:
        rows = rows[:limit]
    try:
        from dataclasses import replace

        narrowed = replace(result, data=[rows, cursor] if cursor is not None else rows)
    except TypeError:
        return result, total
    return narrowed, total


def machine_label(task: Mapping[str, Any]) -> str:
    """``<placement>:<machine id>`` from the attempt's machine record (Plan 32 T5c), else ``-``."""
    result = task.get("result") if isinstance(task, Mapping) else None
    machine = (result or {}).get("machine") if isinstance(result, Mapping) else None
    if not isinstance(machine, Mapping) and isinstance(task, Mapping) and isinstance(task.get("machine"), Mapping):
        machine = task["machine"]
    if not isinstance(machine, Mapping):
        return "-"
    placement, ident, device = machine.get("placement"), machine.get("id"), machine.get("device")
    label = f"{placement}:{ident}" if ident else str(placement or "-")
    return f"{label} ({device})" if device not in (None, "", "auto") else label


def primary_output_name(task: Mapping[str, Any]) -> str | None:
    """The file name of a task's primary output (else its first output), when it settled one."""
    result = task.get("result") if isinstance(task, Mapping) else None
    outputs = result.get("outputs") if isinstance(result, Mapping) else None
    if not isinstance(outputs, list):
        return None
    outputs = [o for o in outputs if isinstance(o, Mapping) and o.get("role") != "log"]
    chosen = next((o for o in outputs if o.get("is_primary")), outputs[0] if outputs else None)
    if chosen is None:
        return None
    name = chosen.get("filename") or chosen.get("name")
    return str(name) if name else None


def _when(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def render_list_table(
    rows: list[Any],
    *,
    total: int,
    output_of: Callable[[Mapping[str, Any]], str | None] | None = None,
    now: datetime | None = None,
    noun: str = "items",
) -> str:
    """A short aligned table (house style: header, one line per row, a footer), not the raw rows.

    Columns: id (short), capability, state, created (UTC), duration (elapsed
    so far while active), the machine that ran it, and the output name once completed.
    """
    clock = now or datetime.now(timezone.utc)
    header = ["ID", "CAPABILITY", "STATE", "CREATED (UTC)", "DURATION", "MACHINE", "OUTPUT"]
    body: list[list[str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        state = str(row.get("status") or row.get("state") or "unknown")
        created = _when(row.get("created_at"))
        updated = _when(row.get("updated_at"))
        if state.lower() in _TERMINAL and created is not None and updated is not None:
            duration = _elapsed((updated - created).total_seconds())
        elif created is not None and state.lower() not in _TERMINAL:
            duration = _elapsed((clock - created).total_seconds()) + " so far"
        else:
            duration = "-"
        output = (output_of(row) if output_of is not None and state.lower() in _SUCCESS else None) or "-"
        ident = str(row.get("id") or row.get("task_id") or "")
        body.append([
            ident[:_ID_WIDTH] or "-",
            str(row.get("capability") or row.get("capability_id") or "-"),
            state,
            created.strftime("%Y-%m-%d %H:%M") if created is not None else "-",
            duration,
            machine_label(row),
            output,
        ])
    widths = [max([len(header[i])] + [len(r[i]) for r in body]) for i in range(len(header))]

    def line(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()

    lines = [line(header)] + [line(r) for r in body]
    if not body:
        lines.append(f"(no {noun})")
    if total > len(body):
        lines.append(
            f"{total - len(body)} more: --limit N (0 = all), --capability ID, or --json for the full envelope"
        )
    return "\n".join(lines)


def all_pages(reader: Any, project: str, *, max_pages: int = 60) -> Any:
    """Every page as one result; incomplete traversal is always a failure."""
    def incomplete(reason: str, pages: int, cursor: Any = None) -> DomainResult:
        return DomainResult.failure(ErrorObject(
            "incomplete_list", f"could not read the complete project list: {reason}",
            {"reason": reason, "pages": pages, "max_pages": max_pages, "cursor": cursor},
        ))

    if max_pages < 1:
        return incomplete("page limit reached", 0)
    first = reader(project, limit=50)
    if not getattr(first, "ok", False):
        return first
    rows: list[Any] = []
    result = first
    seen_cursors: list[Any] = []
    for page_number in range(1, max_pages + 1):
        data = result.data
        page, cursor = (data[0], data[1]) if isinstance(data, list) and len(data) == 2 and isinstance(data[0], list) else (data, None)
        if not isinstance(page, list):
            return incomplete("invalid page", page_number)
        rows.extend(page)
        if not cursor:
            break
        if cursor in seen_cursors:
            return incomplete("repeated cursor", page_number, cursor)
        if page_number == max_pages:
            return incomplete("page limit reached", page_number, cursor)
        seen_cursors.append(cursor)
        result = reader(project, cursor=cursor, limit=50)
        if not getattr(result, "ok", False):
            return result
    from dataclasses import replace

    return replace(first, data=rows)


def resolve_id(reader: Any, project: str, ref: str, *, noun: str) -> tuple[str | None, Any]:
    """The full id that *ref* names, as ``(id, None)``, or ``(None, failure)`` when it names no row.

    A full 32-character id, or a ref that is not a hex prefix of at least 8 characters, is used as
    given without listing. A hex prefix must match exactly one row of the project's complete list;
    several matches return a typed ambiguity failure naming them.
    """
    if not (_MIN_PREFIX <= len(ref) < _FULL_ID_LENGTH and _HEX.fullmatch(ref)):
        return ref, None
    listed = all_pages(reader, project)
    if not getattr(listed, "ok", False):
        return None, listed
    ids = [str(row.get("id") or row.get("task_id") or row.get("run_id") or "")
           for row in list_rows(listed) if isinstance(row, Mapping)]
    text = ref.lower()
    matches = sorted({ident for ident in ids if ident.lower().startswith(text)})
    if not matches:
        return None, DomainResult.failure(ErrorObject("not_found", f"no {noun} id starts with {ref}", {"prefix": ref}))
    if len(matches) > 1:
        return None, DomainResult.failure(ErrorObject(
            "ambiguous_id", f"{ref} matches more than one {noun}: {', '.join(matches)}; use more characters",
            {"prefix": ref, "matches": matches},
        ))
    return matches[0], None


def resolve_cli_id(reader: Any, project: str, ref: str, *, noun: str, as_json: bool) -> tuple[str | None, int | None]:
    """:func:`resolve_id` for a verb handler: ``(id, None)``, or ``(None, exit code)`` after reporting why."""
    resolved, failure = resolve_id(reader, project, ref, noun=noun)
    if failure is not None:
        code = print_result(failure, as_json=as_json)
        return None, EXIT_USAGE if failure.error.code == "ambiguous_id" else code
    return resolved, None
