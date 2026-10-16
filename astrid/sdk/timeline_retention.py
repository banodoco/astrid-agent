"""Request validation for Runtime-owned timeline retention (no local deletion)."""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

_TARGET_KEYS = {"timeline_id", "expected_version", "expected_revision_id", "expected_publication_id"}


def _identity(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def validate_targets(targets: Any, *, plan_input: bool = False) -> list[dict[str, Any]]:
    if not isinstance(targets, list) or not targets:
        raise ValueError("purge requires a nonempty explicit list of pinned timeline IDs")
    seen = set()
    for target in targets:
        if not isinstance(target, Mapping) or not _TARGET_KEYS <= target.keys():
            raise ValueError("each purge target requires timeline_id, expected_version, expected_revision_id and expected_publication_id")
        if plan_input and set(target) != _TARGET_KEYS:
            raise ValueError("plan input accepts only exact target IDs and expected state")
        timeline = target["timeline_id"]
        if not _identity(timeline) or timeline in seen:
            raise ValueError("purge timeline IDs must be explicit, nonempty and unique")
        seen.add(timeline)
        if type(target["expected_version"]) is not int or target["expected_version"] < 1:
            raise ValueError("purge expected_version must be a positive integer")
        for field in ("expected_revision_id", "expected_publication_id"):
            if target[field] is not None and not _identity(target[field]):
                raise ValueError(f"purge {field} must be an exact ID or null")
    # Keep every Runtime-returned target field, including provenance. Never
    # rebuild the targets from a listing or discard actual head/lifecycle data.
    return copy.deepcopy([dict(target) for target in targets])


def purge_request(project: str, plan: Any, key: str) -> tuple[list[dict[str, Any]], str]:
    if not _identity(key):
        raise ValueError("purge execution requires an explicit idempotency key; reuse it with the same plan on retry")
    if not isinstance(plan, Mapping) or plan.get("project_id") != project:
        raise ValueError("purge plan must belong to the exact requested project ID")
    digest = plan.get("plan_digest")
    if not _identity(digest):
        raise ValueError("purge execution requires the Runtime plan_digest")
    return validate_targets(plan.get("targets")), digest


def validate_withdrawal(version: Any, withdrawal: Any) -> dict[str, str]:
    fields = {"expected_revision_id", "expected_publication_id"}
    if type(version) is not int or version < 1:
        raise ValueError("publication withdrawal requires an explicit positive expected_version")
    if not isinstance(withdrawal, Mapping) or set(withdrawal) != fields or not all(_identity(withdrawal[f]) for f in fields):
        raise ValueError("publication withdrawal requires exact expected revision and publication IDs")
    return dict(withdrawal)
