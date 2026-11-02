"""Shared, inert validation for generation placement metadata."""
from __future__ import annotations

import re
from typing import Any, Literal, Mapping, TypedDict


class PlacementFields(TypedDict):
    schema: Literal["astrid.generation-placement.v1"]
    supported: tuple[Literal["local", "runpod"], ...]
    default: Literal["local", "runpod"]
    engine: str | None
    requires_endpoint: bool


def parse_placement(data: Mapping[str, Any]) -> PlacementFields:
    if not isinstance(data, Mapping):
        raise ValueError("placement must be an object")
    fields = {"schema", "supported", "default", "engine", "requires_endpoint"}
    if set(data) != fields:
        raise ValueError("placement must contain exactly schema, supported, default, engine, requires_endpoint")
    if not isinstance(data["supported"], list):
        raise ValueError("placement.supported must be an array")
    return {
        "schema": data["schema"],
        "supported": tuple(data["supported"]),
        "default": data["default"],
        "engine": data["engine"],
        "requires_endpoint": data["requires_endpoint"],
    }


def validate_placement(schema: Any, supported: Any, default: Any, engine: Any,
                       requires_endpoint: Any) -> None:
    if not isinstance(schema, str) or schema != "astrid.generation-placement.v1":
        raise ValueError("placement.schema must be astrid.generation-placement.v1")
    if not isinstance(supported, tuple) or not supported:
        raise ValueError("placement.supported must be a non-empty tuple")
    if any(not isinstance(value, str) or value not in ("local", "runpod")
           for value in supported):
        raise ValueError("placement.supported contains an invalid placement")
    if len(set(supported)) != len(supported):
        raise ValueError("placement.supported must not contain duplicates")
    if not isinstance(default, str) or default not in supported:
        raise ValueError("placement.default must be a supported placement")
    if engine is not None and (
        not isinstance(engine, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", engine)
    ):
        raise ValueError("placement.engine must be a valid engine ID or null")
    if type(requires_endpoint) is not bool:
        raise ValueError("placement.requires_endpoint must be a boolean")
    if requires_endpoint and engine is None:
        raise ValueError("placement.requires_endpoint requires an engine ID")


def normalize_placement(data: Mapping[str, Any]) -> dict[str, object]:
    fields = parse_placement(data)
    validate_placement(**fields)
    return {**fields, "supported": list(fields["supported"])}
