"""Shared machine-readable availability contract for generation routes."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from astrid.core.contracts.errors import AstridError


@lru_cache(maxsize=1)
def availability_declaration() -> dict[str, Any]:
    """Collect live availability policy from parsed installed pack manifests."""
    from astrid.core.pack import discover_packs

    payload: dict[str, Any] = {
        "schema_version": 1,
        "actions": {},
        "fixture_only_routes": {},
        "models": {},
        "template_models": {},
    }
    for pack in discover_packs():
        policy = pack.extensions.get("astrid", {}).get("generation_availability", {})
        if not isinstance(policy, dict):
            continue
        for section in ("actions", "fixture_only_routes", "models", "template_models"):
            values = policy.get(section, {})
            if isinstance(values, dict):
                payload[section].update(values)
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


def require_template_available(template: str, mode: str = "", backend: str = "local") -> None:
    _require_available(f"{template}/{mode}/{backend}", template_availability(template, mode, backend))


def availability_projection(record: dict[str, Any]) -> dict[str, str]:
    """Use the same reason and fix in discovery, documentation and refusals."""
    if record.get("available") is True:
        return {"availability": "available", "reason": "", "fix": ""}
    error = record.get("error", {})
    fix = (
        "use a qualified generation route; retry this route after it is qualified"
        if error.get("code") == "generation_route_unqualified"
        else "retry after the generation availability declaration marks this route available"
    )
    return {"availability": "unavailable", "reason": str(error.get("message", "")),
            "fix": str(error.get("fix") or fix)}


def entry_availability(entry: dict[str, Any], mode: str, backend: str = "local") -> dict[str, Any]:
    """Only declared actions and bundled entries carry release qualification."""
    if entry.get("action"):
        try:
            return action_availability(entry["action"])
        except KeyError:
            return {"available": True}
    if entry.get("source") == "ready-template":
        decision = template_availability(entry["id"], mode, backend)
        if decision.get("available") is True and entry.get("registry_id"):
            return model_availability(entry["registry_id"], mode, backend)
        return decision
    return {"available": True}


def require_entry_available(entry: dict[str, Any], mode: str, backend: str = "local") -> None:
    _require_available(f"{entry['id']}/{mode}/{backend}", entry_availability(entry, mode, backend))


def require_bundled_workflow_available(path: Any, *, source_path: Any = None) -> None:
    """Gate bundled sources/selectors, never caller-authored graph metadata."""
    from collections.abc import Mapping
    from pathlib import Path

    if path is None:
        return
    path = Path(path)
    if path.suffix.lower() != '.py':
        try:
            descriptor = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError, ValueError):
            # The existing workflow parser owns malformed or missing inputs.
            descriptor = None
        if isinstance(descriptor, Mapping) and isinstance(descriptor.get("template_id"), str) and isinstance(descriptor.get("bindings"), Mapping):
            require_template_available(descriptor["template_id"])
    try:
        import vibecomfy
    except ImportError:
        return  # Existing execution readiness owns optional dependency errors.
    if vibecomfy.__file__:
        root = Path(vibecomfy.__file__).resolve().parent.parent / "ready_templates"
        try:
            relative = Path(source_path or path).resolve().relative_to(root.resolve())
        except ValueError:
            return
        template = relative.parent if relative.name == 'workflow.py' else relative.with_suffix('')
        require_template_available(template.as_posix())


def require_bundled_execution_available(resolved: Any) -> None:
    """Both file execution and typed template execution retain source provenance."""
    workflow = getattr(resolved, 'workflow', resolved)
    require_bundled_workflow_available(getattr(getattr(workflow, 'source', None), 'path', None))


def _require_available(route: str, record: dict[str, Any]) -> None:
    if record.get("available") is True:
        return
    error = record.get("error")
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        raise RuntimeError(f"unavailable route {route!r} has no coded error")
    raise AstridError(
        f"{route}: {error.get('message') or 'route is unavailable'}",
        code=error["code"],
        recovery_command=availability_projection(record)["fix"],
    )
