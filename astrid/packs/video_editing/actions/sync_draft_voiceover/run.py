"""Caller-authorized V3 action for complete draft narration synchronization."""
from __future__ import annotations

import json
from typing import Any

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint("video_editing.sync_draft_voiceover")

from astrid.packs.video_editing.shared.draft_voiceover import sync_draft_voiceover


def _decode(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def run(
    *,
    project: str,
    timeline_ref: str,
    timing_policy: str,
    settings: Any = None,
    occurrence_ids: Any = None,
    adopt_clip_ids: Any = None,
    padding_seconds: float = 0.15,
    plan_only: bool = False,
) -> dict[str, Any]:
    """Run the complete workflow in the caller-authorized Runtime context."""
    if not project:
        raise ValueError("project is required")
    if not timeline_ref:
        raise ValueError("timeline_ref is required")
    settings_value = _decode(settings, {})
    occurrence_value = _decode(occurrence_ids, None)
    adopt_value = _decode(adopt_clip_ids, [])
    if settings_value is None:
        settings_value = {}
    if adopt_value is None:
        adopt_value = []

    # The caller-bound client retains canonical authoring and publication
    # authority. The action itself adds no worker-owned publish step.
    from astrid.sdk import AstridClient

    with AstridClient.open_from_launcher() as client:
        result = sync_draft_voiceover(
            client,
            project,
            timeline_ref,
            settings=settings_value,
            occurrence_ids=occurrence_value,
            adopt_clip_ids=adopt_value,
            timing_policy=timing_policy,
            padding_seconds=float(padding_seconds),
            plan_only=_as_bool(plan_only),
        )
    if not isinstance(result, dict):
        raise TypeError("draft voiceover synchronization must return a mapping")
    return {"report": result, "receipt": result.get("receipt", {})}
