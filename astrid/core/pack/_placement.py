"""Inert generation placement contract shared by pack and generation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Mapping, TypeAlias

EngineId: TypeAlias = str
Placement: TypeAlias = Literal["local", "runpod"]


class EngineError(RuntimeError):
    """Stable, secret-free public error shape shared by engine adapters."""

    __module__ = "astrid.core.generation.engine_contracts"

    code: ClassVar[str] = "engine_error"

    def __init__(self, message: str, *, step_id: str | None = None,
                 retryable: bool = False, recovery_handle: str | None = None):
        super().__init__(message)
        self.code = type(self).code
        self.message = message
        self.step_id = step_id
        self.retryable = retryable
        self.recovery_handle = recovery_handle



class UnsupportedPlacement(EngineError):
    __module__ = "astrid.core.generation.engine_contracts"

    code = "unsupported_placement"



@dataclass(frozen=True)
class PlacementDescriptor:
    """Inert placement metadata; validation never looks up recipes or providers."""

    __module__ = "astrid.core.generation.engine_contracts"

    schema: Literal["astrid.generation-placement.v1"]
    supported: tuple[Placement, ...]
    default: Placement
    engine: EngineId | None
    requires_endpoint: bool

    def __post_init__(self) -> None:
        import re

        if not isinstance(self.schema, str) or self.schema != "astrid.generation-placement.v1":
            raise ValueError("placement.schema must be astrid.generation-placement.v1")
        if not isinstance(self.supported, tuple) or not self.supported:
            raise ValueError("placement.supported must be a non-empty tuple")
        if any(not isinstance(value, str) or value not in ("local", "runpod")
               for value in self.supported):
            raise ValueError("placement.supported contains an invalid placement")
        if len(set(self.supported)) != len(self.supported):
            raise ValueError("placement.supported must not contain duplicates")
        if not isinstance(self.default, str) or self.default not in self.supported:
            raise ValueError("placement.default must be a supported placement")
        if self.engine is not None and (
            not isinstance(self.engine, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", self.engine)
        ):
            raise ValueError("placement.engine must be a valid engine ID or null")
        if type(self.requires_endpoint) is not bool:
            raise ValueError("placement.requires_endpoint must be a boolean")
        if self.requires_endpoint and self.engine is None:
            raise ValueError("placement.requires_endpoint requires an engine ID")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PlacementDescriptor:
        """Parse the complete wire object without coercing malformed values."""
        if not isinstance(data, Mapping):
            raise ValueError("placement must be an object")
        fields = {"schema", "supported", "default", "engine", "requires_endpoint"}
        if set(data) != fields:
            raise ValueError("placement must contain exactly schema, supported, default, engine, requires_endpoint")
        if not isinstance(data["supported"], list):
            raise ValueError("placement.supported must be an array")
        return cls(
            schema=data["schema"],
            supported=tuple(data["supported"]),
            default=data["default"],
            engine=data["engine"],
            requires_endpoint=data["requires_endpoint"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "supported": list(self.supported),
            "default": self.default,
            "engine": self.engine,
            "requires_endpoint": self.requires_endpoint,
        }

    def resolve(self, placement: Placement | None = None) -> Placement:
        """Resolve an omitted choice or reject unsupported data before execution."""
        if placement is None:
            return self.default
        if not isinstance(placement, str) or placement not in self.supported:
            raise UnsupportedPlacement("placement is not supported by this backend")
        return placement
