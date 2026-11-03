"""Public APIs for packs that depend on editorial."""

from typing import Any

from . import cut_timeline


def notes_overlap_ratio(prev: Any, curr: Any) -> float:
    from astrid.packs.editorial.actions.editor_review.run import notes_overlap_ratio as implementation

    return implementation(prev, curr)


def plan_next_action(review: dict[str, Any]) -> str:
    from astrid.packs.editorial.actions.editor_review.run import plan_next_action as implementation

    return implementation(review)


__all__ = ["cut_timeline", "notes_overlap_ratio", "plan_next_action"]
