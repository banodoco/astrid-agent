"""Shared machine-readable availability contract for generation routes."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

from astrid.core.contracts.errors import AstridError


@lru_cache(maxsize=1)
def availability_declaration() -> dict[str, Any]:
    """Return the canonical generation availability declaration as JSON data."""
    resource = files("astrid.core.generation").joinpath("availability.json")
    payload = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("invalid generation availability declaration")
    return payload


def action_availability(action_id: str) -> dict[str, Any]:
    """Return one action's declared availability record."""
    actions = availability_declaration().get("actions")
    record = actions.get(action_id) if isinstance(actions, dict) else None
    if not isinstance(record, dict):
        raise KeyError(f"unknown generation availability action {action_id!r}")
    return record


def require_action_available(action_id: str) -> None:
    """Raise the declaration's coded error when an action is unavailable."""
    record = action_availability(action_id)
    if record.get("available") is True:
        return
    error = record.get("error")
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        raise RuntimeError(f"unavailable action {action_id!r} has no coded error")
    raise AstridError(
        str(error.get("message") or f"{action_id} is unavailable"),
        code=error["code"],
        recovery_command="retry after the generation availability declaration marks this route available",
    )
