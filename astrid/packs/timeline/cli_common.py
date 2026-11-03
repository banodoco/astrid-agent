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
                print(str(exc), file=sys.stderr)
                return exc.code
            except (TimelineEditError, SheetError) as exc:
                print(str(exc), file=sys.stderr)
                presentation = getattr(exc, "presentation", None)
                detail = presentation.get("detail") if isinstance(presentation, dict) else None
                if isinstance(detail, str) and detail:
                    print(detail, file=sys.stderr)  # e.g. the candidate listing an ambiguous ref points to
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
    from astrid.sdk.timeline.revisions import short_revision

    return short_revision(revision)


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


def stale_copy_lines(parsed: argparse.Namespace, tl: Any, name: str, head: str) -> list[str]:
    """A reused working copy whose base is behind the head: what it holds, what the head is, and how to start over."""
    from astrid.sdk.timeline.revisions import revision_source

    written = {"app": " (written by the App editor)", "astrid": " (written by Astrid)"}.get(revision_source(head), "")
    try:
        edits = len(tl.edits().get("changes") or [])
    except Exception:  # noqa: BLE001 - the count is a courtesy here
        edits = None
    holds = f"it holds {clips_changed(edits)} vs {short_rev(tl.base_revision)}" if edits is not None else "its edits are not counted"
    where = f"{parsed.ref} --project {parsed.project}"
    return [
        f'STALE: working copy "{name}" was made from {short_rev(tl.base_revision)}, but the head is now {short_rev(head)}{written}.',
        f"  checkout reused the copy and did not pick up the head; {holds}.",
        "  commit merges guarded edits onto the head, or reports a conflict and changes nothing.",
        f"  to start from the head: timelines discard {where}, then timelines checkout {where} --fresh",
    ]


def checkout_summary(parsed: argparse.Namespace, tl: Any, *, existed: bool) -> list[str]:
    """What ``timelines checkout`` says: which working copy, from which published revision, and the next steps."""
    from astrid.packs.timeline.cli import _document_edit_commands
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
    head = getattr(tl, "head_moved_to", None)
    stale = stale_copy_lines(parsed, tl, name, head) if head else []
    return [
        f'working copy "{name}" of {parsed.ref} · from published {short_rev(tl.base_revision)} · {summary}'
        + ("" if existed and not fresh else " · a new working copy, made from the published head"),
        *stale,
        "show, visualize, lint and diff now read this working copy (--published for the live version)",
        "next:",
        f"  read     timelines show {where} --as sheet --range c30..c31     (cut ids, \"words\" or seconds)",
        f"  edit     change a line of that sheet, then  timelines apply {where} FILE",
        f"           or  timelines edit {where} --clip c30.cover --until Astrid",
        f'           or  Python: tl = Checkout.draft("{parsed.project}", "{parsed.ref}"); tl.clip("c30.cover").until("Astrid"); tl.save()',
        f"  look     timelines visualize {where} --preset motion --at Astrid   (--compare published: before/after)",
        f"  commit   {_document_edit_commands(str(getattr(tl, 'path', None) or 'FILE'), parsed.ref, parsed.project)['commit']}",
    ]


def render_line(parsed: argparse.Namespace, project_id: str, timeline_id: str, head: str) -> str:
    """``latest render RUN (date) renders the head`` / ``... N revision(s) behind the head``."""
    from astrid.sdk.timeline_filmstrip import latest_render

    found = latest_render(parsed.client, project_id=project_id, timeline_id=timeline_id)
    if not found:
        return "latest render: none found (timelines render TL --project P)"
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
    from astrid.packs.timeline.cli import _document_edit_commands
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
        print(f"the published head moved to {head}; commit will merge guarded edits or report conflicts")
    print(render_line(parsed, project_id, timeline_id, head) if want_render
          else "latest render: --render to look it up (it scans the run history, ~10 s)")
    print(f"next: {_document_edit_commands(str(path), parsed.ref, parsed.project)['commit']}" if changes
          else f"next: timelines show {where} --as sheet   ·   timelines edit {where} --clip c22.rocket --on viral")
    return 0 if report.valid else 1
