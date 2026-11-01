"""Composition revisions: the refs ``TL@prev``, ``TL@-N``, ``TL@<prefix>``, ``TL@head`` and their history.

One adapter (``RevisionStore``) hides what the Runtime can do. Our Runtime serves a composition
revision by id (GET) and the head (open_composition); it has no list route. ``RuntimeRevisions.list``
walks ``parent`` links from the head when a revision names its parent, and returns ``None`` (cannot
list) when none does. Nothing here writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from astrid.sdk.timeline._common import TimelineEditError

_SOURCE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("authoring-parent-revision-", "astrid"),
    ("astrid.editor.timeline.", "app"),
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


def revision_source(revision_id: str) -> str:
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


class RuntimeRevisions(RevisionStore):
    """Our Runtime: head and get through the timelines client (``open_composition``).

    The projection the client returns does not expose a revision's parent today, so ``parent`` is read
    from the summary's ``parent_revision_id`` only if a Runtime ever adds it. Until then ``list`` is None.
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
            parent=_text(summary.get("parent_revision_id")),
            message=_text(summary.get("message")),
        )

    def list(self, project: str, timeline: str, *, limit: int = 50) -> list[Revision] | None:
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
    found = [r for r in revisions if _matches(r, ref)]
    if len(found) == 1:
        return found[0].id
    if not found:
        raise TimelineEditError(f"{timeline}@{ref}: no revision matches; available: {_available(revisions)}")
    raise TimelineEditError(
        f"{timeline}@{ref}: ambiguous, matches {', '.join(r.id for r in found[:5])}"
    )
