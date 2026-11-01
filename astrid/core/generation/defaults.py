"""User generation defaults and local candidate ordering.

Defaults live in Runtime user app-state.  This module deliberately owns no
local cache: the Runtime version is the concurrency token and the value is
validated before it is ever changed.
"""
from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any


SCHEMA = "astrid.generation-defaults/v1"
APP_STATE_KEY = "generation.defaults"
BUILTINS: dict[str, tuple[str, ...]] = {
    "image/t2i": ("qwen-image-2.1-turbo",),
    "image/edit": ("qwen-image-2.1-turbo-edit",),
    "video/t2v": ("ltx-2.3",),
    "video/i2v": ("ltx-2.3",),
}


class DefaultsError(ValueError):
    """Invalid or unsupported saved generation-default state."""


class DefaultsSchemaError(DefaultsError):
    """Saved state uses a schema this client must preserve unchanged."""


def validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DefaultsError("generation defaults must be an object")
    if value.get("schema") != SCHEMA:
        raise DefaultsSchemaError("generation defaults use an unknown schema; state was preserved")
    slots = value.get("slots")
    if not isinstance(slots, Mapping) or set(value) != {"schema", "slots"}:
        raise DefaultsError("generation defaults must contain only schema and slots")
    for key, entry in slots.items():
        if key not in BUILTINS and key not in {"audio/music", "audio/tts", "audio/sfx"}:
            raise DefaultsError(f"unknown generation-default slot {key!r}")
        if not isinstance(entry, str) or not entry.strip():
            raise DefaultsError(f"generation default for {key} must be a non-empty entry id")
    return {"schema": SCHEMA, "slots": dict(slots)}


def _transport(client: Any) -> Any:
    """Get the existing WorkspaceClient used by the public Runtime wrapper."""
    try:
        return client._remote._transport
    except AttributeError as exc:
        raise DefaultsError("Runtime user app-state is unavailable") from exc


def load(client: Any) -> tuple[dict[str, Any], int]:
    resource = _transport(client).get_user_app_state(APP_STATE_KEY)
    value = resource.content if resource.content is not None else {"schema": SCHEMA, "slots": {}}
    return validate(value), resource.version


def save(client: Any, slot: str, entry: str | None) -> dict[str, Any]:
    current, version = load(client)
    slots = dict(current["slots"])
    if entry is None:
        slots.pop(slot, None)
    else:
        if slot not in BUILTINS and slot not in {"audio/music", "audio/tts", "audio/sfx"}:
            raise DefaultsError(f"unknown generation-default slot {slot!r}")
        if not isinstance(entry, str) or not entry.strip():
            raise DefaultsError("entry id must be a non-empty string")
        slots[slot] = entry
    content = validate({"schema": SCHEMA, "slots": slots})
    _transport(client).update_user_app_state(
        APP_STATE_KEY, content, expected_version=version, idempotency_key=uuid.uuid4().hex,
    )
    return content


def candidates(target: Any, *, kind: str, mode: str, configured: Mapping[str, Any] | None = None,
               entries: list[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Return the local ordered candidates for one resolved target and slot.

    The target is carried in each row so callers cannot accidentally combine
    readiness captured from one ComfyUI with execution against another.
    Readiness is evaluated by the existing generation/listing paths.
    """
    slot = f"{kind}/{mode}"
    slots = configured.get("slots", {}) if configured is not None else {}
    inventory = list(entries or [])
    by_id = {str(row["id"]): row for row in inventory
             if row.get("id") and row.get("kind") == kind
             and row.get("mode") == mode and row.get("backend") == "local"}
    target_record = getattr(target, "generation_target", None)
    endpoint = getattr(getattr(target, "target", None), "url", None)
    target_value = target_record.as_dict() if hasattr(target_record, "as_dict") else target_record
    if endpoint and not target_value:
        target_value = {"engine": "comfyui", "endpoint": endpoint, "owner": "human"}
    result: list[dict[str, Any]] = []
    saved = slots.get(slot)
    if saved:
        row = by_id.get(saved)
        result.append({"entry": saved, "origin": "user", "target": target_value,
                       "candidate": row, "configured": saved})
        return result
    for identifier in BUILTINS.get(slot, ()):
        result.append({"entry": identifier, "origin": "builtin", "target": target_value,
                       "candidate": by_id.get(identifier), "configured": None})
    builtin_ids = {row["entry"] for row in result}
    for identifier in sorted(by_id):
        row = by_id[identifier]
        if (row.get("kind") == kind and row.get("mode") == mode
                and row.get("backend") == "local" and identifier not in builtin_ids):
            result.append({"entry": identifier, "origin": "fallback", "target": target_value,
                           "candidate": row, "configured": None})
    return result
