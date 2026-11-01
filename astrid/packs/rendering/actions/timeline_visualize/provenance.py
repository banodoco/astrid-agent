"""Truthful labels for the immutable snapshot a visualize result consumed."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrid.core.rendering.verified_source import VerifiedTimelineSource


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _is_candidate(metadata: Mapping[str, Any], authority: Mapping[str, Any]) -> bool:
    selection = str(metadata.get("selection") or "").strip().lower()
    if selection in {"explicit_candidate_preview", "candidate_preview", "unpublished_candidate_preview"}:
        return True
    if isinstance(metadata.get("working_copy"), Mapping):
        return True
    if isinstance(authority.get("working_copy"), Mapping):
        return True
    if isinstance(authority.get("authoring_preview"), Mapping):
        return True
    mode = str(authority.get("render_mode") or authority.get("mode") or "").strip().lower()
    return mode in {"authoring_candidate_preview", "candidate", "preview", "unpublished"}


def _candidate_markers(
    metadata: Mapping[str, Any],
    canonical: Mapping[str, Any],
    capture: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    """Return candidate identity hints; these never confer admission."""
    markers: list[Mapping[str, Any]] = []
    for source in (metadata, canonical, capture):
        for key in ("working_copy", "authoring_preview"):
            marker = source.get(key)
            if isinstance(marker, Mapping) and marker not in markers:
                markers.append(marker)
    return markers


def _marker_base_revision(marker: Mapping[str, Any]) -> tuple[str | None, bool]:
    """Read only the claimed base from a candidate marker.

    The other parent fields in a candidate authority identify the unpublished
    candidate (for example ``candidate_parent_revision_id`` / ``parent_revision_id``);
    they are deliberately not accepted as the committed base here.
    """
    base_parent = marker.get("base_parent")
    if not isinstance(base_parent, Mapping):
        return None, False
    values = {
        value
        for key in ("revision_id", "parent_revision_id", "head_revision_id")
        for value in (base_parent.get(key),)
        if isinstance(value, str) and value
    }
    if len(values) > 1:
        return None, True
    return next(iter(values), None), False


def _candidate_base_revision(
    markers: list[Mapping[str, Any]],
) -> tuple[str | None, bool]:
    """Return a shared claimed base, or mark contradictory markers invalid."""
    bases: set[str] = set()
    contradictory = False
    for marker in markers:
        base, marker_contradictory = _marker_base_revision(marker)
        contradictory = contradictory or marker_contradictory
        if base is not None:
            bases.add(base)
    return (next(iter(bases), None) if len(bases) <= 1 else None), contradictory or len(bases) > 1


def snapshot_label(
    snapshot: Mapping[str, Any] | None, *, source: VerifiedTimelineSource | None = None,
) -> dict[str, Any]:
    """Label the exact frozen input, using only host-minted source for authority.

    Metadata without that source is useful as a draft/base hint, never proof of
    an accepted revision. Runtime owns admission; this consumer only checks that
    its snapshot is the one paired with the internal source.
    """
    if source is not None:
        from astrid.core.rendering.verified_source import VerifiedTimelineSource

        if type(source) is not VerifiedTimelineSource:
            raise ValueError("snapshot label requires a host-owned verified source")
        binding = source.read()

        def encoded(value):
            return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

        if encoded(snapshot) != encoded(binding["snapshot"]):
            raise ValueError("snapshot label differs from verified source snapshot")
        ref = binding["ref"]
        if ref["source_kind"] == "revision":
            actual = ref["revision_id"]
            return {
                "status": "committed", "label": f"committed revision {actual}",
                "actual_revision_id": actual, "base_revision_id": None,
            }
        if ref["source_kind"] == "working_copy":
            base = ref["working_copy"]["base_revision_id"]
            metadata = snapshot.get("metadata") if isinstance(snapshot, Mapping) else None
            marker = (metadata.get("authoring_preview") if isinstance(metadata, Mapping) else None)
            if not isinstance(marker, Mapping) and isinstance(snapshot, Mapping):
                marker = snapshot.get("authoring_preview")
            label = marker.get("label") if isinstance(marker, Mapping) else None
            label = label if isinstance(label, str) and label else f"unsaved candidate based on committed revision {base}"
            return {
                "status": "candidate", "label": label,
                "actual_revision_id": None, "base_revision_id": base,
            }
        raise ValueError("snapshot label requires an admitted revision or working copy")
    if not isinstance(snapshot, Mapping):
        return {
            "status": "unavailable", "label": "revision unavailable",
            "actual_revision_id": None, "base_revision_id": None,
        }

    metadata = snapshot.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    canonical = metadata.get("canonical_timeline")
    canonical = canonical if isinstance(canonical, Mapping) else {}
    capture = metadata.get("capture_authority")
    capture = capture if isinstance(capture, Mapping) else {}
    markers = _candidate_markers(metadata, canonical, capture)
    root_marker = snapshot.get("authoring_preview") if isinstance(snapshot, Mapping) else None
    if isinstance(root_marker, Mapping) and root_marker not in markers:
        markers.append(root_marker)
    if _is_candidate(metadata, canonical) or _is_candidate(metadata, capture) or markers:
        base, contradictory = _candidate_base_revision(markers)
        draft = _first_string(*(marker.get("candidate_parent_revision_id") for marker in markers))
        label = _first_string(*(marker.get("label") for marker in markers)) or "UNCERTIFIED unsaved candidate"
        if draft and label == "UNCERTIFIED unsaved candidate":
            label += f" {draft}"
        if contradictory:
            label += " (base revision unavailable: contradictory markers)"
        else:
            label += f" (base {base})" if base else " (base revision unavailable)"
        return {
            "status": "uncertified", "label": label,
            "actual_revision_id": None, "base_revision_id": base if not contradictory else None,
        }
    claimed = _first_string(
        metadata.get("parent_revision_id"),
        *(authority.get(key) for authority in (capture, canonical)
          for key in ("parent_revision_id", "head_revision_id", "revision_id")),
    )
    return {
        "status": "uncertified",
        "label": "UNCERTIFIED preview" + (f" (claimed revision {claimed})" if claimed else " (revision unavailable)"),
        "actual_revision_id": None, "base_revision_id": None,
    }
