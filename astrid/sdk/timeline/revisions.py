"""Composition revisions: the refs ``TL@prev``, ``TL@-N``, ``TL@<prefix>``, ``TL@head`` and their history.

One adapter (``RevisionStore``) hides what the Runtime can do. ``RuntimeRevisions`` uses Runtime's
parent-composition list and restore routes where available, retaining the old parent walk for clients
that predate the list route.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from astrid.sdk.timeline._common import TimelineEditError

_SOURCE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("authoring-parent-revision-", "astrid"),
    ("reigh.editor.timeline.", "app"),
    ("restore-", "restore"),
)
# The prefix a revision id carries, so that a short id can match the part after it.
_ID_PREFIXES: tuple[str, ...] = tuple(prefix for prefix, _ in _SOURCE_PREFIXES)
CANNOT_LIST = (
    "this Runtime cannot list revisions (it serves a composition revision by id and names no parent, "
    "so history cannot walk back from the head)"
)


@dataclass(frozen=True)
class Revision:
    id: str
    source: str  # "astrid" (authoring-parent-revision-*), "app" (reigh.editor.timeline.*), "restore" (restore-*), "other"
    created_at: str | None
    parent: str | None
    message: str | None
    publication_id: str | None = None
    previous_publication_id: str | None = None
    restored_from_revision_id: str | None = None
    provenance: Mapping[str, Any] | None = None
    evidence: Mapping[str, Any] | None = None


def revision_source(revision_id: str) -> str:
    for prefix, source in _SOURCE_PREFIXES:
        if revision_id.startswith(prefix):
            return source
    return "other"


class RevisionStore(Protocol):
    """The adapter. ``list`` returns None when this store cannot list revisions."""

    def head(self, project: str, timeline: str) -> str: ...

    def get(self, project: str, timeline: str, revision_id: str) -> Revision: ...

    def list(self, project: str, timeline: str, *, limit: int = 50) -> list[Revision] | None: ...


def _text(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


class RuntimeRevisions(RevisionStore):
    """Our Runtime: head and get through the timelines client (``open_composition``).

    History follows explicit previous_revision_id / previous_publication_id.
    parent_revision_id names publication linkage and is never interpreted as ancestry.
    """

    def __init__(self, client: Any):
        self._client = client

    def _open(self, project: str, timeline: str, revision_id: str | None) -> Mapping[str, Any]:
        opened = self._client.open_composition(project, timeline, revision_id=revision_id, limit=1)
        if not getattr(opened, "ok", False) or not isinstance(getattr(opened, "data", None), Mapping):
            error = getattr(opened, "error", None)
            detail = getattr(error, "message", None) or str(error or "no answer")
            if revision_id is None:
                raise TimelineEditError(f"could not read the head of {timeline!r} in {project!r}: {detail}")
            raise TimelineEditError(f"no revision {revision_id!r} on {timeline!r} in {project!r}: {detail}")
        return opened.data

    def head(self, project: str, timeline: str) -> str:
        compact_head = getattr(self._client, "head", None)
        if callable(compact_head):
            result = compact_head(project, timeline)
            if not getattr(result, "ok", True):
                raise TimelineEditError(f"could not read the head of {timeline!r} in {project!r}")
            data = getattr(result, "data", result)
            head = _text(data.get("revision_id")) if isinstance(data, Mapping) else None
        else:
            data = self._open(project, timeline, None)
            head = _text((data.get("summary") or {}).get("head_revision_id"))
        if head is None:
            raise TimelineEditError(f"{timeline!r} in {project!r} has no published head yet")
        return head

    def get(self, project: str, timeline: str, revision_id: str) -> Revision:
        data = self._open(project, timeline, revision_id)
        summary = data.get("summary") or {}
        return Revision(
            id=_text(summary.get("revision_id")) or revision_id,
            source=revision_source(revision_id),
            created_at=_text(summary.get("created_at")),
            parent=_text(summary.get("previous_revision_id")),
            publication_id=_text(summary.get("publication_id")),
            previous_publication_id=_text(summary.get("previous_publication_id")),
            restored_from_revision_id=_text(summary.get("restored_from_revision_id")),
            provenance=summary.get("provenance"),
            evidence=summary.get("evidence"),
            message=_text(summary.get("message")),
        )

    def list(self, project: str, timeline: str, *, limit: int = 50) -> list[Revision] | None:
        list_revisions = getattr(self._client, "list_parent_composition_revisions", None)
        if callable(list_revisions):
            return self._list_runtime(project, timeline, limit=limit, list_revisions=list_revisions)
        return self._list_parent_links(project, timeline, limit=limit)

    @staticmethod
    def _list_runtime(
        project: str,
        timeline: str,
        *,
        limit: int,
        list_revisions: Any,
    ) -> list[Revision] | None:
        """Read the Runtime's paged list, keeping its marked head first for positional refs."""
        if limit <= 0:
            return []
        out: list[Revision] = []
        current_head: Revision | None = None
        cursor: str | None = None
        seen_cursors: set[str] = set()
        page_limit = min(200, max(50, limit))

        while len(out) < limit or current_head is None:
            result = list_revisions(project, timeline, cursor=cursor, limit=page_limit)
            if not getattr(result, "ok", True):
                error = getattr(result, "error", None)
                detail = getattr(error, "message", None) or str(error or "no answer")
                raise TimelineEditError(
                    f"could not list revisions of {timeline!r} in {project!r}: {detail}"
                )
            data = getattr(result, "data", result)
            if data is None:
                return None
            if isinstance(data, Mapping):
                rows = data.get("items", [])
                next_cursor = data.get("next_cursor")
            elif isinstance(data, (list, tuple)) and len(data) == 2:
                rows, next_cursor = data
            else:
                raise TimelineEditError("Runtime revision list returned an invalid page")
            if not isinstance(rows, (list, tuple)):
                raise TimelineEditError("Runtime revision list returned invalid items")
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                revision_id = _text(row.get("revision_id"))
                if revision_id is None:
                    raise TimelineEditError("Runtime revision list omitted a revision id")
                revision = Revision(
                    id=revision_id,
                    source=revision_source(revision_id),
                    created_at=_text(row.get("created_at")),
                    parent=_text(row.get("previous_revision_id")),
                    publication_id=_text(row.get("publication_id")),
                    previous_publication_id=_text(row.get("previous_publication_id")),
                    restored_from_revision_id=_text(row.get("restored_from_revision_id")),
                    provenance=row.get("provenance"),
                    evidence=row.get("evidence"),
                    message=_text(row.get("message")),
                )
                out.append(revision)
                if row.get("is_current_head") is True:
                    current_head = revision
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        if current_head is not None:
            # Walk event identities when available. Repeated revisions must not
            # collapse, and wall-clock creation order is not history ancestry.
            if current_head.publication_id is not None:
                by_publication = {r.publication_id: r for r in out if r.publication_id is not None}
                walked: list[Revision] = []
                seen: set[str] = set()
                node = current_head
                while node.publication_id is not None and node.publication_id not in seen:
                    seen.add(node.publication_id)
                    walked.append(node)
                    previous = by_publication.get(node.previous_publication_id)
                    if previous is None:
                        break
                    node = previous
                # A migrated event can have unknown links. Retain every event
                # rather than inventing ancestry or hiding the historical rows.
                out = walked + [r for r in out if r.publication_id not in seen]
            else:
                out = [current_head, *(r for r in out if r is not current_head)]
        return out[:limit]

    def _list_parent_links(self, project: str, timeline: str, *, limit: int = 50) -> list[Revision] | None:
        """Compatibility path for older clients whose summaries expose parent links."""
        head = self.get(project, timeline, self.head(project, timeline))
        if head.parent is None:
            return None  # no revision names its parent, so there is no walk to make
        out: list[Revision] = [head]
        seen = {head.id}
        current = head
        while current.parent is not None and len(out) < limit:
            if current.parent in seen:  # a cycle in stored data must not loop forever
                break
            current = self.get(project, timeline, current.parent)
            seen.add(current.id)
            out.append(current)
        return out

    def restore(
        self,
        project: str,
        timeline: str,
        revision_id: str,
        *,
        expected_head: str | None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Restore one listed revision through Runtime's expected-head/idempotent mutation."""
        restore_revision = getattr(self._client, "restore_parent_composition_revision", None)
        if not callable(restore_revision):
            raise TimelineEditError("this Runtime client cannot restore parent-composition revisions")
        return restore_revision(
            project,
            timeline,
            revision_id,
            expected_head=expected_head,
            idempotency_key=idempotency_key,
        )


def split_ref(text: str) -> tuple[str, str | None]:
    """``TL@prev`` → ``("TL", "prev")``; ``TL`` → ``("TL", None)``. The last ``@`` splits."""
    name, sep, rev = text.rpartition("@")
    if not sep or not name or not rev:
        return text, None
    return name, rev


def _available(revisions: list[Revision] | None) -> str:
    if revisions is None:
        return "the Runtime cannot list them"
    return ", ".join(r.id for r in revisions[:10]) or "none"


def _matches(revision: Revision, text: str) -> bool:
    if revision.id.startswith(text):
        return True
    for prefix in _ID_PREFIXES:
        if revision.id.startswith(prefix) and revision.id[len(prefix):].startswith(text):
            return True
    return False


def resolve_revision(project: str, timeline: str, ref: str | None, *, store: RevisionStore) -> str | None:
    """A ref to a revision id. None means the head (``None``/``head``): the caller reads the working head."""
    if ref is None or ref == "head":
        return None
    if ref == "prev" or (ref.startswith("-") and ref[1:].isdigit() and int(ref[1:]) > 0):
        steps = 1 if ref == "prev" else int(ref[1:])
        revisions = store.list(project, timeline, limit=steps + 1)
        if revisions is None:
            raise TimelineEditError(
                f"{timeline}@{ref}: {CANNOT_LIST}; @{ref} needs a revision that names its parent"
            )
        if revisions and revisions[0].publication_id is not None:
            # Positional refs promise ancestry, not adjacent timestamps. Unknown
            # migration links stay unknown even though history displays all events.
            by_publication = {r.publication_id: r for r in revisions if r.publication_id is not None}
            node = revisions[0]
            seen = {node.publication_id}
            for _ in range(steps):
                previous = by_publication.get(node.previous_publication_id)
                if previous is None or previous.publication_id in seen:
                    raise TimelineEditError(f"{timeline}@{ref}: publication ancestry is unavailable; use an explicit revision id")
                if node.parent != previous.id:
                    raise TimelineEditError(f"{timeline}@{ref}: inconsistent publication ancestry")
                seen.add(previous.publication_id)
                node = previous
            return node.id
        if len(revisions) <= steps:
            raise TimelineEditError(
                f"{timeline}@{ref}: only {len(revisions)} revision(s) back from the head are available "
                f"({_available(revisions)})"
            )
        return revisions[steps].id
    if ref.startswith("-"):
        raise TimelineEditError(f"{timeline}@{ref}: not a revision ref (use prev, -N with N>0, head, or an id prefix)")

    revisions = store.list(project, timeline, limit=200)
    if revisions is None:
        try:
            return store.get(project, timeline, ref).id
        except TimelineEditError as exc:
            raise TimelineEditError(
                f"{timeline}@{ref}: {CANNOT_LIST}, so only a full revision id resolves here ({exc})"
            ) from None
    found = list({r.id: r for r in revisions if _matches(r, ref)}.values())
    if len(found) == 1:
        return found[0].id
    if not found:
        raise TimelineEditError(f"{timeline}@{ref}: no revision matches; available: {_available(revisions)}")
    raise TimelineEditError(
        f"{timeline}@{ref}: ambiguous, matches {', '.join(r.id for r in found[:5])}"
    )
