"""Composition revision refs and the immutable Runtime history read adapter.

The commit-history route records accepted publication edges separately from the
immutable content revision.  In particular, two accepted writes may reuse one
content revision, so publication identities must survive this adapter's read
model.  Older Runtime clients still get the parent-link fallback below.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from astrid.sdk.timeline._common import TimelineEditError

_SOURCE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("authoring-parent-revision-", "astrid"),
    ("astrid.editor.timeline.", "app"),
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
    source: str  # "astrid" (authoring-parent-revision-*), "app" (astrid.editor.timeline.*), "restore" (restore-*), "other"
    created_at: str | None
    parent: str | None
    message: str | None
    publication_id: str | None = None
    previous_publication_id: str | None = None


RevisionList = list[Revision] | None


def revision_source(revision_id: str, source: Any = None) -> str:
    """Classify a revision, preferring the accepted source label when present."""
    if isinstance(source, Mapping) and _text(source.get("via")):
        return source["via"]
    for prefix, source in _SOURCE_PREFIXES:
        if revision_id.startswith(prefix):
            return source
    return "other"


def short_revision(revision_id: Any) -> str:
    """The 8-character handle people read and type: the part after the revision's known prefix, so
    ``astrid.editor.timeline.5fb2249e…`` and ``authoring-parent-revision-5fb2249e…`` both read ``5fb2249e``."""
    text = str(revision_id or "")
    for prefix in _ID_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):][:8] or text
    return text[:8] or text


class RevisionStore(Protocol):
    """The adapter. ``list`` returns None when this store cannot list revisions."""

    def head(self, project: str, timeline: str) -> str: ...

    def get(self, project: str, timeline: str, revision_id: str) -> Revision: ...

    def list(self, project: str, timeline: str, *, limit: int = 50) -> list[Revision] | None: ...


def _text(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _supports_history_page(reader: Any, project: str, timeline: str) -> bool:
    """Recognize the public scoped immutable history signature.

    Older clients may expose a legacy ``history(project, ref)`` helper under
    the same name. It is not the immutable route and must retain the parent
    link fallback until the public method grows its cursor/limit contract.
    """
    try:
        inspect.signature(reader).bind(project, timeline, cursor=None, limit=50)
    except (TypeError, ValueError):
        return False
    return True


class RuntimeRevisions(RevisionStore):
    """Read immutable revisions and accepted history through the timelines client."""

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
        commits = self._commit_reader(project, timeline)
        head: str | None = None
        if commits is not None:
            page = self._result_data(commits(project, timeline, cursor=None, limit=1),
                                     timeline=timeline, operation="read")
            head = self._head_pair(page, timeline=timeline)[0]
        else:
            data = self._open(project, timeline, None)
            summary = data.get("summary") or {}
            head = _text(summary.get("head_revision_id") or summary.get("revision_id"))
        if head is None:
            raise TimelineEditError(f"{timeline!r} in {project!r} has no published head yet")
        return head

    def get(self, project: str, timeline: str, revision_id: str) -> Revision:
        data = self._open(project, timeline, revision_id)
        summary = data.get("summary") or {}
        actual_id = _text(summary.get("revision_id"))
        if actual_id != revision_id:
            raise TimelineEditError(
                f"Runtime did not prove requested revision {revision_id!r} on {timeline!r}"
            )
        return Revision(
            id=actual_id,
            source=revision_source(actual_id),
            created_at=_text(summary.get("created_at")) or _text(summary.get("accepted_at")),
            parent=_text(summary.get("previous_revision_id")) or _text(summary.get("parent_revision_id")),
            message=_text(summary.get("message")) or _text(summary.get("label")),
            publication_id=_text(summary.get("publication_id")),
            previous_publication_id=_text(summary.get("previous_publication_id")),
        )

    def list(self, project: str, timeline: str, *, limit: int = 50) -> list[Revision] | None:
        commits = self._commit_reader(project, timeline)
        if commits is not None:
            return self._list_commits(project, timeline, limit=limit, reader=commits)
        head = self.get(project, timeline, self.head(project, timeline))
        if head.parent is None:
            return None  # no revision names its parent, so there is no walk to make
        out: list[Revision] = [head]
        seen = {head.id}
        current = head
        while current.parent is not None and len(out) < limit:
            if current.parent in seen:  # a cycle in stored data must not loop forever
                raise TimelineEditError(f"{timeline!r} in {project!r} has cyclic revision ancestry")
            current = self.get(project, timeline, current.parent)
            seen.add(current.id)
            out.append(current)
        return out

    def _commit_reader(self, project: str, timeline: str) -> Any | None:
        """Find the public immutable history method, if this client has it.

        ``RemoteTimelines`` scopes its public ``history`` call from a project
        and timeline reference.  ``WorkspaceClient`` exposes the same frozen
        route directly for callers that already hold canonical IDs.  Once
        either method exists, errors from that route must propagate instead of
        silently downgrading to the legacy parent-link walk.
        """
        direct = getattr(self._client, "list_timeline_commits", None)
        if callable(direct):
            return direct
        scoped = getattr(self._client, "history", None)
        if callable(scoped) and _supports_history_page(scoped, project, timeline):
            return scoped
        return None

    @staticmethod
    def _result_data(result: Any, *, timeline: str, operation: str) -> Mapping[str, Any] | None:
        """Unwrap DomainResult or generated-client mappings without guessing errors."""
        if isinstance(result, Mapping):
            return result
        if not getattr(result, "ok", False):
            error = getattr(result, "error", None)
            detail = getattr(error, "message", None) or str(error or "no answer")
            raise TimelineEditError(f"could not {operation} revisions of {timeline!r}: {detail}")
        data = getattr(result, "data", None)
        if isinstance(data, Mapping):
            return data
        if data is None:
            return None
        raise TimelineEditError(f"Runtime {operation} returned an invalid revision page")

    @staticmethod
    def _head_pair(page: Mapping[str, Any] | None, *, timeline: str) -> tuple[str, str]:
        head = page.get("head") if isinstance(page, Mapping) else None
        if not isinstance(head, Mapping):
            raise TimelineEditError(f"Runtime revision history omitted its head for {timeline!r}")
        revision_id = _text(head.get("revision_id"))
        publication_id = _text(head.get("publication_id"))
        if revision_id is None or publication_id is None:
            raise TimelineEditError(f"Runtime revision history returned an incomplete head for {timeline!r}")
        return revision_id, publication_id

    @staticmethod
    def _history_item(row: Mapping[str, Any], *, timeline: str) -> tuple[str, str, str | None, str | None]:
        required = ("publication_id", "revision_id", "previous", "receipt", "intent",
                    "records", "provider_version", "compiler_version", "publisher")
        missing = [key for key in required if key not in row]
        if missing:
            raise TimelineEditError(
                f"Runtime revision history item omitted required field(s): {', '.join(missing)}"
            )
        publication_id = _text(row.get("publication_id"))
        revision_id = _text(row.get("revision_id"))
        if publication_id is None or revision_id is None:
            raise TimelineEditError(f"Runtime revision history returned an invalid item for {timeline!r}")
        previous = row.get("previous")
        if previous is None:
            previous_publication = previous_revision = None
        elif isinstance(previous, Mapping):
            previous_publication = _text(previous.get("publication_id"))
            previous_revision = _text(previous.get("revision_id"))
            if previous_publication is None or previous_revision is None:
                raise TimelineEditError(f"Runtime revision history returned an incomplete previous head for {timeline!r}")
        else:
            raise TimelineEditError(f"Runtime revision history returned an invalid previous head for {timeline!r}")
        if not isinstance(row.get("receipt"), Mapping):
            raise TimelineEditError(f"Runtime revision history returned an invalid receipt for {timeline!r}")
        if row.get("intent") not in {"recorded", "legacy_unknown", "bootstrap"}:
            raise TimelineEditError(f"Runtime revision history returned an invalid intent for {timeline!r}")
        if row.get("records") is not None and not isinstance(row.get("records"), Mapping):
            raise TimelineEditError(f"Runtime revision history returned invalid records for {timeline!r}")
        for key in ("provider_version", "compiler_version", "publisher"):
            value = row.get(key)
            if value is not None and not isinstance(value, str):
                raise TimelineEditError(f"Runtime revision history returned an invalid {key} for {timeline!r}")
        source = row.get("source")
        if source is not None and (
            not isinstance(source, Mapping)
            or source.get("via") not in {"cli", "python", "app", "script", "sheet"}
            or ("detail" in source and not isinstance(source.get("detail"), str))
        ):
            raise TimelineEditError(f"Runtime revision history returned an invalid source for {timeline!r}")
        return publication_id, revision_id, previous_publication, previous_revision

    @staticmethod
    def _history_revision(row: Mapping[str, Any]) -> Revision:
        revision_id = _text(row.get("revision_id"))
        if revision_id is None:
            raise TimelineEditError("Runtime revision history omitted a revision id")
        previous = row.get("previous")
        if previous is None:
            previous = {}
        receipt = row.get("receipt")
        assert isinstance(previous, Mapping)
        assert isinstance(receipt, Mapping)
        return Revision(
            id=revision_id,
            source=revision_source(revision_id, row.get("source")),
            created_at=_text(receipt.get("created_at")),
            parent=_text(previous.get("revision_id")),
            message=_text(row.get("label")),
            publication_id=_text(row.get("publication_id")),
            previous_publication_id=_text(previous.get("publication_id")),
        )

    @classmethod
    def _list_commits(cls, project: str, timeline: str, *, limit: int, reader: Any) -> RevisionList:
        """Read all pages to reconstruct newest-first publication ancestry.

        The frozen history route is bounded by a fixed head and returns items
        in acceptance order. Fetching its cursor chain before reversing avoids
        treating a reused content revision as a duplicate or as a new parent.
        """
        if limit <= 0:
            return []
        rows: list[Mapping[str, Any]] = []
        head_pair: tuple[str, str] | None = None
        cursor: str | None = None
        seen_cursors: set[str | None] = {None}
        while True:
            page = cls._result_data(reader(project, timeline, cursor=cursor, limit=min(200, max(50, limit))),
                                     timeline=timeline, operation="list")
            if page is None:
                raise TimelineEditError(f"Runtime revision history returned no page for {timeline!r}")
            current_head = cls._head_pair(page, timeline=timeline)
            if head_pair is None:
                head_pair = current_head
            elif current_head != head_pair:
                raise TimelineEditError(f"Runtime revision history changed its head while paging {timeline!r}")
            raw_items = page.get("items")
            if not isinstance(raw_items, list):
                raise TimelineEditError("Runtime revision history returned invalid items")
            for row in raw_items:
                if not isinstance(row, Mapping):
                    raise TimelineEditError(f"Runtime revision history returned an invalid item for {timeline!r}")
                cls._history_item(row, timeline=timeline)
                rows.append(row)
            next_cursor = page.get("next_cursor")
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise TimelineEditError(f"Runtime revision history returned an invalid cursor for {timeline!r}")
            if next_cursor is None:
                break
            if not next_cursor or next_cursor in seen_cursors:
                raise TimelineEditError(f"Runtime revision history cursor loop for {timeline!r}")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        assert head_pair is not None
        head_revision_id, head_publication_id = head_pair
        by_publication: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            publication_id = row["publication_id"]
            if publication_id in by_publication:
                raise TimelineEditError(f"Runtime revision history repeated publication {publication_id!r}")
            by_publication[publication_id] = row
        current = by_publication.get(head_publication_id)
        if current is None or current["revision_id"] != head_revision_id:
            raise TimelineEditError(f"Runtime revision history head is not present in its items for {timeline!r}")
        ordered_rows: list[Mapping[str, Any]] = []
        seen_publications: set[str] = set()
        while current is not None:
            publication_id = current["publication_id"]
            if publication_id in seen_publications:
                raise TimelineEditError(f"Runtime revision history contains a publication cycle for {timeline!r}")
            seen_publications.add(publication_id)
            ordered_rows.append(current)
            previous = current["previous"]
            if previous is None:
                current = None
                continue
            current = by_publication.get(previous["publication_id"])
            if current is None or current["revision_id"] != previous["revision_id"]:
                raise TimelineEditError(f"Runtime revision history has a missing or conflicting ancestor for {timeline!r}")
        if len(seen_publications) != len(by_publication):
            raise TimelineEditError(f"Runtime revision history contains a disconnected or incomplete chain for {timeline!r}")
        revisions = [cls._history_revision(row) for row in ordered_rows]
        return revisions[:limit]


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


def resolve_revision_record(project: str, timeline: str, ref: str | None, *, store: RevisionStore) -> Revision | None:
    """Resolve a history ref while retaining its accepted publication edge."""
    if ref is None or ref == "head":
        return None
    if ref == "prev" or (ref.startswith("-") and ref[1:].isdigit() and int(ref[1:]) > 0):
        steps = 1 if ref == "prev" else int(ref[1:])
        revisions = store.list(project, timeline, limit=steps + 1)
        if revisions is None:
            raise TimelineEditError(
                f"{timeline}@{ref}: {CANNOT_LIST}; @{ref} needs a revision that names its parent"
            )
        if len(revisions) <= steps:
            raise TimelineEditError(
                f"{timeline}@{ref}: only {len(revisions)} revision(s) back from the head are available "
                f"({_available(revisions)})"
            )
        if revisions and revisions[0].publication_id is not None:
            by_publication = {r.publication_id: r for r in revisions if r.publication_id is not None}
            node = revisions[0]
            seen = {node.publication_id}
            for _ in range(steps):
                previous = by_publication.get(node.previous_publication_id or "")
                if previous is None or previous.publication_id in seen:
                    raise TimelineEditError(
                        f"{timeline}@{ref}: publication ancestry is unavailable; use an explicit revision id"
                    )
                seen.add(previous.publication_id or "")
                node = previous
            return node
        return revisions[steps]
    if ref.startswith("-"):
        raise TimelineEditError(f"{timeline}@{ref}: not a revision ref (use prev, -N with N>0, head, or an id prefix)")

    revisions = store.list(project, timeline, limit=200)
    if revisions is None:
        try:
            return store.get(project, timeline, ref)
        except TimelineEditError as exc:
            raise TimelineEditError(
                f"{timeline}@{ref}: {CANNOT_LIST}, so only a full revision id resolves here ({exc})"
            ) from None
    # One immutable revision may occur at several accepted publications. An
    # explicit revision ref names the content, so retain the newest matching
    # event rather than reporting a false ambiguity; history still exposes
    # every publication edge to undo callers.
    found_by_id: dict[str, Revision] = {}
    for revision in revisions:
        if _matches(revision, ref):
            found_by_id.setdefault(revision.id, revision)
    found = list(found_by_id.values())
    if len(found) == 1:
        return found[0]
    if not found:
        raise TimelineEditError(f"{timeline}@{ref}: no revision matches; available: {_available(revisions)}")
    raise TimelineEditError(
        f"{timeline}@{ref}: ambiguous, matches {', '.join(r.id for r in found[:5])}"
    )


def resolve_revision(project: str, timeline: str, ref: str | None, *, store: RevisionStore) -> str | None:
    """Resolve a ref to its immutable revision id; ``None`` means the head."""
    revision = resolve_revision_record(project, timeline, ref, store=store)
    return revision.id if revision is not None else None
