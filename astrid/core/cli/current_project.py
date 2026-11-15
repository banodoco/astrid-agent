"""The one resolver for "the project a command means when --project is omitted"."""

from __future__ import annotations

import argparse


def resolve_current_project(parsed: argparse.Namespace) -> str | None:
    """The workspace's current project id, or None when none is selected (or the runtime cannot say)."""
    projects = getattr(getattr(parsed, "client", None), "projects", None)
    current = getattr(projects, "current", None)
    if current is None:
        return None
    try:
        result = current()
    except Exception:  # noqa: BLE001 - unreachable runtime: the caller reports "no current project"
        return None
    if not getattr(result, "ok", False):
        return None
    data = result.data
    row = data.get("project") if isinstance(data, dict) else None
    if not isinstance(row, dict):
        return None
    ref = row.get("project_id") or row.get("id") or row.get("slug")
    return ref.strip() if isinstance(ref, str) and ref.strip() else None
