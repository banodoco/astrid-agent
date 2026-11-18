"""The one media-id contract: canonical spelling, accepted spellings, and project resolution.

A media id names one managed media object. Its canonical form is
``sha256:<64 lowercase hex>``; that is the only form a command prints.

Accepted on input, within one project:

* the canonical ``sha256:<64 hex>``;
* the bare 64-hex digest (any letter case);
* a unique hex prefix of at least ``MIN_PREFIX`` characters, optionally with the
  ``sha256:`` label, like a git short hash. An ambiguous prefix is refused and
  lists its matches.

Every command that takes a media id resolves it here: ``RemoteMedia.show`` (and
through it ``verify``, ``relate``, crop, handles and transcripts), ``media open``
and the shot-plan writer. Nothing else matches digests by hand.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "CANONICAL_PREFIX",
    "MIN_PREFIX",
    "MediaIdError",
    "canonical_digest",
    "media_id_text",
    "resolve_media_id",
    "resolve_project_media",
]

CANONICAL_PREFIX = "sha256:"
MIN_PREFIX = 8
_FULL = re.compile(r"[0-9a-f]{64}")
_HEX = re.compile(r"[0-9a-f]+")
_LISTED_MATCHES = 10


class MediaIdError(ValueError):
    """A media id that names no media, or more than one. ``code`` is a DomainResult error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_digest(value: Any) -> str | None:
    """``sha256:<64 lowercase hex>`` for a full digest in any accepted spelling, else ``None``."""
    if not isinstance(value, str):
        return None
    digest = value.strip().lower().removeprefix(CANONICAL_PREFIX)
    return CANONICAL_PREFIX + digest if _FULL.fullmatch(digest) else None


def media_id_text(value: Any) -> str:
    """The printed form of a media id: canonical when it is a digest, otherwise the value unchanged."""
    canonical = canonical_digest(value)
    return canonical if canonical is not None else str(value)


def _no_match(project: Any, text: str) -> MediaIdError:
    return MediaIdError("not_found", f"no media in project {project} matches {text}")


def _row_digest(row: Mapping[str, Any]) -> str | None:
    return canonical_digest(row.get("digest")) or canonical_digest(row.get("object_id"))


def resolve_project_media(rows: Iterable[Any], project: Any, ref: Any) -> Mapping[str, Any]:
    """The project media row that *ref* names. Raises :class:`MediaIdError`.

    *rows* is the project's media listing (``digest`` and ``object_id`` per row).
    A runtime id stored verbatim as ``object_id`` (not a digest) still matches exactly.
    """
    text = str(ref if ref is not None else "").strip()
    listed = [row for row in rows if isinstance(row, Mapping)]
    if not text:
        raise MediaIdError("validation_error", "media id is empty")
    for row in listed:
        if text in (str(row.get("digest") or ""), str(row.get("object_id") or "")):
            return row
    full = canonical_digest(text)
    if full is not None:
        for row in listed:
            if full in (canonical_digest(row.get("digest")), canonical_digest(row.get("object_id"))):
                return row
        raise _no_match(project, text)
    hexpart = text.lower().removeprefix(CANONICAL_PREFIX)
    if not _HEX.fullmatch(hexpart):
        raise _no_match(project, text)
    if len(hexpart) < MIN_PREFIX:
        raise MediaIdError(
            "validation_error",
            f"media id {text} is too short: give the full sha256 or at least {MIN_PREFIX} hex characters of it",
        )
    matches: dict[str, Mapping[str, Any]] = {}
    for row in listed:
        digest = _row_digest(row)
        if digest is not None and digest[len(CANONICAL_PREFIX):].startswith(hexpart):
            matches.setdefault(digest, row)
    if not matches:
        raise _no_match(project, text)
    if len(matches) > 1:
        lines = [
            f"{digest}  {row.get('filename') or ''}".rstrip()
            for digest, row in sorted(matches.items())[:_LISTED_MATCHES]
        ]
        more = len(matches) - len(lines)
        raise MediaIdError(
            "validation_error",
            f"media id {text} is ambiguous in project {project}: it matches {len(matches)} media. "
            "Give a longer prefix or the full id:\n  " + "\n  ".join(lines)
            + (f"\n  ... and {more} more" if more > 0 else ""),
        )
    return next(iter(matches.values()))


def resolve_media_id(client: Any, project: str, ref: Any) -> str:
    """The canonical digest of *ref* in *project*. Full digests are normalised with no lookup;
    anything else goes through ``client.media.show``, which resolves it. Raises :class:`MediaIdError`."""
    full = canonical_digest(ref)
    if full is not None:
        return full
    shown = client.media.show(project, str(ref))
    if not getattr(shown, "ok", False):
        error = getattr(shown, "error", None)
        code = str(getattr(error, "code", None) or "not_found")
        raise MediaIdError(code, str(getattr(error, "message", None) or _no_match(project, str(ref)).message))
    digest = _row_digest(shown.data) if isinstance(shown.data, Mapping) else None
    if digest is None:
        raise MediaIdError("protocol_error", f"media {ref} has no sha256 digest in project {project}")
    return digest
