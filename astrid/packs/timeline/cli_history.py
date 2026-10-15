"""CLI side of composition revisions: ``timelines history`` and the ``TL@ref`` forms of show and diff."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.revisions import Revision, RuntimeRevisions, resolve_revision, split_ref

REASON = (
    "earlier revisions: this Runtime cannot list them (Runtime ask: composition revisions need "
    "previous_revision_id / previous_publication_id, and a list route)."
)


def _store(parsed: Any) -> RuntimeRevisions:
    return RuntimeRevisions(parsed.client.timelines)


def _short(revision: Revision) -> str:
    for prefix in ("authoring-parent-revision-", "reigh.editor.timeline.", "restore-"):
        if revision.id.startswith(prefix):
            return revision.id[len(prefix):][:8]
    return revision.id[:8]


def _row(revision: Revision) -> str:
    return f"  {_short(revision):<8}  {revision.source:<8}  {revision.created_at or '-':<25}  {revision.message or ''}".rstrip()


def run_history(parsed: Any) -> int:
    """``timelines history TL``: the revisions of TL, newest first. Where the Runtime cannot list them, the
    head revision is printed with a plain line saying the earlier ones are not listable (exit 0)."""
    store = _store(parsed)
    limit = int(getattr(parsed, "limit", None) or 50)
    try:
        revisions = store.list(parsed.project, parsed.ref, limit=limit)
        complete = revisions is not None
        if revisions is None:
            revisions = [store.get(parsed.project, parsed.ref, store.head(parsed.project, parsed.ref))]
    except TimelineEditError as exc:
        print(f"error not_found: {exc}")
        return 2
    reason = None if complete else REASON
    if parsed.json:
        data = {"revisions": [asdict(r) for r in revisions], "complete": complete, "reason": reason}
        print(json.dumps({"ok": True, "data": data, "error": None}, indent=2, sort_keys=True))
        return 0
    if complete:
        lines = [f"Revisions of {parsed.ref} (newest first, {len(revisions)} shown):"]
    else:
        lines = [f"Revisions of {parsed.ref}: the head only."]
    lines.extend(_row(r) for r in revisions)
    if reason:
        lines.append(reason)
    print("\n".join(lines))
    return 0


def show_ref(parsed: Any) -> int | None:
    """``timelines show TL@prev``: set ``parsed.ref`` to TL and ``parsed.revision_id`` to the resolved id.
    Returns an exit code on failure, None when the ref is absent or resolved."""
    name, rev = split_ref(str(parsed.ref))
    if rev is None:
        return None
    try:
        resolved = resolve_revision(parsed.project, name, rev, store=_store(parsed))
    except TimelineEditError as exc:
        print(f"error validation_error: {exc}")
        return 2
    parsed.ref = name
    if resolved is not None:
        parsed.revision_id = resolved
    return None


def diff_ref(parsed: Any) -> int | None:
    """``timelines diff TL@prev``: the base is that revision. Working copy by default (``parsed.ref_revision``);
    with ``--published`` it becomes ``from_revision`` against the head."""
    name, rev = split_ref(str(parsed.ref))
    if rev is None:
        return None
    try:
        resolved = resolve_revision(parsed.project, name, rev, store=_store(parsed))
    except TimelineEditError as exc:
        print(f"error validation_error: {exc}")
        return 2
    parsed.ref = name
    parsed.ref_revision = resolved
    if getattr(parsed, "published", False):
        if getattr(parsed, "from_revision", None):
            print("error validation_error: give the ref (TL@…) or --from, not both")
            return 2
        try:
            parsed.from_revision = resolved or _store(parsed).head(parsed.project, name)
        except TimelineEditError as exc:
            print(f"error not_found: {exc}")
            return 2
    return None
