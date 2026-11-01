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
    _require_available(action_id, action_availability(action_id))


def model_availability(model: str, mode: str, backend: str) -> dict[str, Any]:
    """Return a catalog qualification decision; undeclared models retain their existing status."""
    models = availability_declaration().get("models", {})
    if not isinstance(models, dict):
        raise RuntimeError("invalid generation model availability declaration")
    if model not in models:
        return {"available": True}
    record = models[model]
    if not isinstance(record, dict):
        raise RuntimeError(f"invalid generation availability for model {model!r}")
    return record


def template_availability(template: str, mode: str, backend: str) -> dict[str, Any]:
    """Resolve declared template families through the same model decision."""
    families = availability_declaration().get("template_models", {})
    if not isinstance(families, dict):
        raise RuntimeError("invalid generation template availability declaration")
    matches = [(prefix, model) for prefix, model in families.items()
               if isinstance(prefix, str) and template.startswith(prefix)]
    if not matches:
        return {"available": True}
    _, model = max(matches, key=lambda item: len(item[0]))
    if not isinstance(model, str) or not model:
        raise RuntimeError(f"invalid generation availability mapping for {template!r}")
    return model_availability(model, mode, backend)


def require_model_available(model: str, mode: str, backend: str) -> None:
    _require_available(f"{model}/{mode}/{backend}", model_availability(model, mode, backend))


def _require_available(route: str, record: dict[str, Any]) -> None:
    if record.get("available") is True:
        return
    error = record.get("error")
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        raise RuntimeError(f"unavailable route {route!r} has no coded error")
    raise AstridError(
        str(error.get("message") or f"{route} is unavailable"),
        code=error["code"],
        recovery_command=(
            "use a qualified generation route; retry this route after it is qualified"
            if error["code"] == "generation_route_unqualified"
            else "retry after the generation availability declaration marks this route available"
        ),
    )
