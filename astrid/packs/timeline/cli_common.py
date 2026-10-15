"""Small helpers shared by the working-copy verbs (lint, lines, words, discard, duplicate, status).

Kept out of ``cli.py`` so each verb module stays short. Everything here takes the parsed argparse namespace
and reads the timeline name from ``parsed.ref`` (the product parser's positional).
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from typing import Any


class VerbError(Exception):
    """A user-facing failure: the message goes to stderr and the verb exits with ``code``."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def guard(default_code: int):
    """Turn a VerbError, TimelineEditError or SheetError into one stderr line and an exit code."""

    def wrap(fn):
        def run(parsed: argparse.Namespace) -> int:
            from astrid.sdk.timeline._common import TimelineEditError
            from astrid.sdk.timeline_sheet import SheetError

            try:
                return fn(parsed)
            except VerbError as exc:
                parsed._edit_refusal = {"type": type(exc).__name__, "message": str(exc)}
                print(str(exc), file=sys.stderr)
                return exc.code
            except (TimelineEditError, SheetError) as exc:
                parsed._edit_refusal = {"type": type(exc).__name__, "message": str(exc)}
                if getattr(parsed, "command", None) is not None:
                    import json
                    payload = exc.to_json() if hasattr(exc, "to_json") else {"code": "invalid_edit", "message": str(exc)}
                    print(json.dumps({"error": payload}), file=sys.stderr)
                else:
                    print(str(exc), file=sys.stderr)
                return default_code

        run.__name__ = fn.__name__
        run.__doc__ = fn.__doc__
        return run

    return wrap


def need_project(parsed: argparse.Namespace) -> str:
    if not getattr(parsed, "project", None):
        raise VerbError("--project P is required (the timeline is named inside its project)", 2)
    return parsed.project


def draft_name(parsed: argparse.Namespace) -> str:
    """The working copy a command uses: --draft NAME, else the current one (last checked out), else main."""
    if getattr(parsed, "draft", None):
        return parsed.draft
    try:
        from astrid.sdk.timeline_checkout import current_draft, resolve_ids

        project_id, timeline_id, _head = resolve_ids(need_project(parsed), parsed.ref, client=parsed.client)
        return current_draft(project_id, timeline_id)
    except Exception:  # noqa: BLE001 - the runtime is unreachable: the default name
        return "main"


def working_copy(parsed: argparse.Namespace, *, create: bool) -> tuple[Any, bool]:
    """(checkout, existed). ``create=True`` makes the working copy from the head when there is none."""
    from astrid.sdk.timeline_checkout import Checkout, find_draft

    project = need_project(parsed)
    name = draft_name(parsed)
    existed = find_draft(project, parsed.ref, name, client=parsed.client) is not None
    if not existed and not create:
        return None, False
    return Checkout.draft(project, parsed.ref, name, client=parsed.client, fresh=bool(getattr(parsed, "fresh", False))), existed


def short_rev(revision: Any) -> str:
    text = str(revision or "")
    return text.removeprefix("authoring-parent-revision-")[:8] or text


def clips_changed(n: int) -> str:
    """The one count every verb prints for a working copy: clips added, removed, moved or edited."""
    return f"{n} clip{'s' if n != 1 else ''} changed"


def cuts_summary(tl: Any) -> str:
    return f"{len(tl.cuts)} cuts · {len(tl.clips())} clips · {len(tl.words())} words"


def cap(lines: list[str], limit: int = 30) -> list[str]:
    """A long re-flow prints its first lines and a count; the rest is one `timelines diff` away."""
    if len(lines) <= limit:
        return lines
    return lines[:limit] + [f"… and {len(lines) - limit} more (timelines diff shows them all)"]


def check_lines(report: Any, cuts: list[str] | None = None) -> list[str]:
    """After an edit: valid or not, and NEW lint on the cuts it touched (the full report is `timelines check --all`)."""
    brief = getattr(report, "brief", None)
    if callable(brief):
        return brief(cuts=cuts) if cuts is not None else brief()
    lines = str(report).splitlines()
    return [lines[0]] + [ln for ln in lines[1:] if ln.startswith(("!", "  ", "lint"))]


def working_copy_view(parsed: argparse.Namespace) -> dict[str, Any] | None:
    """The unpublished working copy of this timeline (its document and edits), or None."""
    from astrid.sdk.timeline_checkout import Checkout, find_draft

    try:
        path = find_draft(parsed.project, parsed.ref, getattr(parsed, "draft", None), client=getattr(parsed, "client", None))
    except Exception as exc:  # the runtime is not reachable: say so, show the published head
        print(f"working copy: not checked ({exc}); showing the published head", file=sys.stderr)
        return None
    if path is None:
        return None
    checkout = Checkout.load(path)
    return {
        "bundle": checkout.document(),
        "path": str(path),
        "base_revision": checkout.base_revision,
        "changes": list(checkout.edits().get("changes") or []),
    }


def working_banner(working: Mapping[str, Any]) -> str:
    """The one-line banner show, lint and diff print when they read the working copy."""
    return (f"WORKING COPY · {clips_changed(len(working['changes']))} vs published {working['base_revision']} · "
            "--published for the live version")


def checkout_summary(parsed: argparse.Namespace, tl: Any, *, existed: bool) -> list[str]:
    """What ``timelines checkout`` says: which working copy, from which published revision, and the next steps."""
    from astrid.sdk.timeline_checkout import current_draft, resolve_ids

    name = getattr(parsed, "draft", None)
    if not name:
        try:
            project_id, timeline_id, _head = resolve_ids(parsed.project, parsed.ref, client=parsed.client)
            name = current_draft(project_id, timeline_id)
        except Exception:  # noqa: BLE001 - the name is cosmetic here
            name = "main"
    named = len(tl._cut_groups())
    summary = (f"{tl.cut_count()} · {len(tl.clips())} clips · {len(tl.words())} words" if named else cuts_summary(tl))
    where = f"{parsed.ref} --project {parsed.project}"
    fresh = bool(getattr(parsed, "fresh", False))
    return [
        f'working copy "{name}" of {parsed.ref} · from published {short_rev(tl.base_revision)} · {summary}'
        + ("" if existed and not fresh else " · a new working copy, made from the published head"),
        "show, visualize, lint and diff now read this working copy (--published for the live version)",
        "next:",
        f"  read     timelines show {where} --as sheet --range START..END",
        f"  edit     change a line of that sheet, then  timelines apply {where} FILE",
        f"           or  timelines edit {where} --clip ADDRESS --until MOMENT",
        f'           or  Python: tl = Checkout.draft("{parsed.project}", "{parsed.ref}"); tl.clip("ADDRESS").until("MOMENT"); tl.save()',
        f"  look     timelines visualize {where} --at MOMENT   (--compare published: before/after)",
        f'  publish  timelines status {where}   ·   timelines publish {where} -m "what changed"',
    ]


def render_line(parsed: argparse.Namespace, project_id: str, timeline_id: str, head: str) -> str:
    """``latest render RUN (date) renders the head`` / ``... N revision(s) behind the head``."""
    from astrid.sdk.timeline_filmstrip import latest_render

    found = latest_render(parsed.client, project_id=project_id, timeline_id=timeline_id)
    if not found:
        return "latest render: none found (use timelines render with this timeline and project)"
    when = str(found.get("created_at") or "")[:16].replace("T", " ")
    what = "a working copy (draft)" if found.get("candidate") else None
    if what is None and found.get("revision") == head:
        what = "the published head"
    elif what is None:
        behind = None
        try:
            history = parsed.client.timelines.history(parsed.project, parsed.ref, limit=50)
            rows = history.data if getattr(history, "ok", False) else []
            rows = rows[0] if rows and isinstance(rows[0], list) else rows
            ids = [str(r.get("revision_id") or r.get("head_revision_id") or "") for r in rows or [] if isinstance(r, Mapping)]
            if found.get("revision") in ids:
                behind = ids.index(found["revision"])
        except Exception:  # noqa: BLE001
            behind = None
        what = (f"revision {short_rev(found.get('revision'))}, "
                + (f"{behind} revision(s) behind the head" if behind else "older than the head"))
    return f"latest render: {found['run_id']} ({when}) renders {what}"


def status_text(parsed: argparse.Namespace, project_id: str, timeline_id: str, head: str, name: str, path: Any) -> int:
    """``timelines status --text``: the working copy's changes in plain words, its check, and the next step."""
    from astrid.sdk.timeline_checkout import Checkout

    where = f"{parsed.ref} --project {parsed.project}"
    want_render = bool(getattr(parsed, "render", False))
    if path is None:
        print(f"no working copy · next: timelines checkout {where}")
        if want_render:
            try:
                print(render_line(parsed, project_id, timeline_id, head))
            except Exception:  # noqa: BLE001 - status never fails on the render lookup
                pass
        return 0
    tl = Checkout.load(path)
    changes = tl.changes()
    clips = len(tl.edits().get("changes") or [])
    show_all = bool(getattr(parsed, "all", False))
    print(f'WORKING COPY "{name}" · {clips_changed(clips)} vs published {short_rev(tl.base_revision)}'
          + (f" (said in {len(changes)} lines: clips that only moved with the film are one line per shift)"
             if clips != len(changes) else ""))
    shown = changes if show_all or len(changes) <= 25 else changes[:25]
    for line in shown:
        print(f"  {line}")
    if len(shown) < len(changes):
        print(f"  … {len(changes) - len(shown)} more (timelines status {where} --text --all)")
    report = tl.check()
    for line in (report.brief(full=True) if show_all else check_lines(report)):
        print(line)
    if head != tl.base_revision:
        print(f"the published head moved to {head}; publish will merge (three-way) or report conflicts")
    print(render_line(parsed, project_id, timeline_id, head) if want_render
          else "latest render: --render to look it up (it scans the run history, ~10 s)")
    print(f'next: timelines publish {where} -m "…"' if changes
          else f"next: timelines show {where} --as sheet   ·   timelines edit {where} --clip c22.rocket --on viral")
    return 0 if report.valid else 1
