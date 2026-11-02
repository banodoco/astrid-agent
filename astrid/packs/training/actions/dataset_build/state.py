"""Schema-valid review state lifecycle helpers for ``training.dataset_build``."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable, Mapping

import jsonschema
from referencing import Registry, Resource

from astrid.pack_api import read_json, write_json_atomic

from .config import MISSING_SCHEMA_VERSION_SOURCE
from ...shared.items import utc_now_iso

SCHEMAS_ROOT = Path(__file__).resolve().parent / "schemas"
RUN_STATE_SCHEMA = "run-state.schema.json"


class ReviewStateError(ValueError):
    """Raised when review_state.json is invalid."""


def make_initial_state(
    *,
    run_id: str,
    writer_id: str,
    config_hash: str | None = None,
    buckets: Mapping[str, int | Mapping[str, Any]] | None = None,
    schema_version_source: str | None = None,
    status: str = "initializing",
    now: str | None = None,
) -> dict[str, Any]:
    timestamp = now or utc_now_iso()
    state: dict[str, Any] = {
        "run_id": run_id,
        "writer_id": writer_id,
        "state_version": 0,
        "created_at": timestamp,
        "updated_at": timestamp,
        "status": status,
        "processed_source_ids": [],
        "review_decisions": {},
        "filter_stats": {},
        "top_up_rounds": 0,
        "submitted": False,
    }
    if config_hash is not None:
        state["config_hash"] = config_hash
    if schema_version_source is not None:
        if schema_version_source != MISSING_SCHEMA_VERSION_SOURCE:
            raise ReviewStateError(f"unsupported schema_version_source: {schema_version_source}")
        state["schema_version_source"] = schema_version_source
    if buckets is not None:
        state["buckets"] = _initial_buckets(buckets)
    validate_review_state(state)
    return state


def read_review_state(path: str | Path) -> dict[str, Any]:
    state = read_json(path)
    validate_review_state(state)
    return state


def write_review_state(path: str | Path, state: Mapping[str, Any], *, now: str | None = None) -> dict[str, Any]:
    next_state = copy.deepcopy(dict(state))
    next_state["state_version"] = int(next_state.get("state_version", 0)) + 1
    next_state["updated_at"] = now or utc_now_iso()
    validate_review_state(next_state)
    write_json_atomic(path, next_state)
    return next_state


def mutate_review_state(
    path: str | Path,
    mutator: Callable[[dict[str, Any]], None],
    *,
    now: str | None = None,
) -> dict[str, Any]:
    state = read_review_state(path)
    mutator(state)
    return write_review_state(path, state, now=now)


def set_status(
    path: str | Path,
    status: str,
    *,
    error: Mapping[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    def mutate(state: dict[str, Any]) -> None:
        state["status"] = status
        if error is not None:
            state["error"] = dict(error)
        elif "error" in state:
            state.pop("error")
        if status == "finalized" and "completed_at" not in state:
            state["completed_at"] = now or utc_now_iso()

    return mutate_review_state(path, mutate, now=now)


def validate_review_state(state: Mapping[str, Any]) -> None:
    validator = _validator(RUN_STATE_SCHEMA)
    errors = sorted(validator.iter_errors(state), key=lambda error: list(error.path))
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.path) or "<root>"
        raise ReviewStateError(f"review_state.json invalid at {path}: {error.message}")


def _initial_buckets(buckets: Mapping[str, int | Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    initial: dict[str, dict[str, Any]] = {}
    for bucket, value in buckets.items():
        if isinstance(value, Mapping):
            target_count = int(value.get("target_count", 0))
            item_ids = list(value.get("item_ids", []))
        else:
            target_count = int(value)
            item_ids = []
        initial[bucket] = {
            "target_count": target_count,
            "accepted": 0,
            "rejected": 0,
            "pending": 0,
            "item_ids": item_ids,
        }
    return initial


def _schema_registry() -> Registry:
    registry = Registry()
    for path in SCHEMAS_ROOT.glob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(path.name, Resource.from_contents(schema))
        if "$id" in schema:
            registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    return registry


def _validator(schema_name: str) -> jsonschema.Draft7Validator:
    schema = json.loads((SCHEMAS_ROOT / schema_name).read_text(encoding="utf-8"))
    return jsonschema.Draft7Validator(schema, registry=_schema_registry())
