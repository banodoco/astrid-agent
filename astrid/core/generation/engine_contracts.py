"""Typed, immutable contracts shared by packaged engine recipes and adapters."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Mapping, TypeAlias

EngineId: TypeAlias = str
PlatformKey: TypeAlias = Literal["linux-x86_64-cu124", "linux-x86_64-cu128", "linux-x86_64-cu130", "windows-x86_64-cu128", "windows-x86_64-cu130"]
Placement: TypeAlias = Literal["local", "runpod"]
Digest: TypeAlias = str
Deadline: TypeAlias = float
TargetPath: TypeAlias = str


class EngineError(RuntimeError):
    """Stable, secret-free public error shape shared by engine adapters."""

    code: ClassVar[str] = "engine_error"

    def __init__(self, message: str, *, step_id: str | None = None,
                 retryable: bool = False, recovery_handle: str | None = None):
        super().__init__(message)
        self.code = type(self).code
        self.message = message
        self.step_id = step_id
        self.retryable = retryable
        self.recovery_handle = recovery_handle


class RecipeError(EngineError):
    """Base class for local recipe and model-lock failures."""


class UnknownEngine(RecipeError):
    code = "unknown_engine"


class UnsupportedPlatform(RecipeError):
    code = "unsupported_platform"


class RecipeValidationError(RecipeError):
    code = "invalid_recipe"


class ModelLockValidationError(RecipeValidationError):
    code = "invalid_model_lock"


class IntegrityError(RecipeError):
    code = "integrity_failed"


class UnsafePathError(RecipeValidationError):
    code = "unsafe_path"


class DownloadError(RecipeError):
    code = "download_failed"


class DeadlineExceeded(DownloadError):
    code = "deadline_exceeded"


class CommandFailed(EngineError):
    code = "command_failed"


class TransportError(EngineError):
    code = "transport_failed"


class CustodyUnresolved(EngineError):
    code = "custody_unresolved"


class EndpointOwnershipError(EngineError):
    code = "endpoint_ownership"


class EndpointHealthError(EngineError):
    code = "endpoint_unhealthy"


class UnsupportedPlacement(EngineError):
    code = "unsupported_placement"


class AttestationError(EngineError):
    code = "attestation_invalid"


@dataclass(frozen=True)
class DataRef:
    path: str
    sha256: Digest


@dataclass(frozen=True)
class SecretRef:
    kind: Literal["file"]
    path: str


@dataclass(frozen=True)
class SourcePin:
    repository: str
    revision: str


@dataclass(frozen=True)
class SourceSpec:
    name: str
    pin: SourcePin
    relative_path: str


@dataclass(frozen=True)
class WheelSpec:
    name: str
    version: str
    url: str
    sha256: Digest


@dataclass(frozen=True)
class SourcePath:
    source: str
    path: str


@dataclass(frozen=True)
class SourceFileRef:
    source: str
    path: str
    sha256: Digest


@dataclass(frozen=True)
class RequirementsSpec:
    mode: Literal["packaged", "resolve-once"]
    packaged_lock: DataRef | None
    input_paths: tuple[SourcePath, ...]
    constraints: tuple[str, ...]
    exclude_newer: str | None


@dataclass(frozen=True)
class SourceInstall:
    source: str
    argv: tuple[str, ...]
    check_modules: tuple[str, ...]


@dataclass(frozen=True)
class EnvironmentSpec:
    name: str
    relative_path: str
    python_version: str
    uv_version: str
    requirements: RequirementsSpec
    final_wheels: tuple[WheelSpec, ...]
    node_lock: SourceFileRef | None
    source_installs: tuple[SourceInstall, ...]


@dataclass(frozen=True)
class StartSpec:
    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str]
    bind_host: Literal["127.0.0.1"]
    port: int


@dataclass(frozen=True)
class HealthSpec:
    path: str
    object_info_path: str
    required_nodes: tuple[str, ...]
    timeout_seconds: float
    poll_interval_seconds: float


@dataclass(frozen=True)
class EngineRecipe:
    schema: Literal["astrid.engine-recipe.v1"]
    engine: EngineId
    platform: PlatformKey
    pins: DataRef
    sources: tuple[SourceSpec, ...]
    environments: tuple[EnvironmentSpec, ...]
    primary_environment: str
    models_lock: DataRef
    start: StartSpec
    health: HealthSpec


@dataclass(frozen=True)
class RecipeIndexEntry:
    engine: EngineId
    platform: PlatformKey
    recipe: DataRef


@dataclass(frozen=True)
class RecipeIndex:
    schema: Literal["astrid.engine-index.v1"]
    entries: tuple[RecipeIndexEntry, ...]


@dataclass(frozen=True)
class ModelSpec:
    name: str
    repository: str | None
    revision: str | None
    path: str | None
    url: str | None
    destination: str
    bytes: int
    sha256: Digest


@dataclass(frozen=True)
class ModelLock:
    schema: Literal["astrid.models-lock.v1"]
    models: tuple[ModelSpec, ...]


@dataclass(frozen=True)
class DownloadResult:
    model: str
    path: TargetPath
    action: Literal["downloaded", "resumed", "skipped"]
    bytes: int
    sha256: Digest
    verified: Literal[True]
    resumed_from_bytes: int


StepKind: TypeAlias = Literal["source", "environment", "model", "start", "health"]


@dataclass(frozen=True)
class InstallStep:
    id: str
    kind: StepKind
    depends_on: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class InstallPlan:
    schema: Literal["astrid.engine-plan.v1"]
    engine: EngineId
    platform: PlatformKey
    placement: Placement
    root: TargetPath
    recipe_sha256: Digest
    models_lock_sha256: Digest
    steps: tuple[InstallStep, ...]


@dataclass(frozen=True)
class StepReceipt:
    id: str
    kind: StepKind
    action: Literal["executed", "skipped"]
    validation: Literal["passed", "failed"]
    facts: Mapping[str, str | int | bool | None]


@dataclass(frozen=True)
class HealthEvidence:
    status: Literal["healthy", "unhealthy", "unknown"]
    checked_at: str
    object_info_sha256: Digest | None
    required_nodes_present: bool


@dataclass(frozen=True)
class InstallReceipt:
    schema: Literal["astrid.engine-install.v1"]
    receipt_id: str
    engine: EngineId
    platform: PlatformKey
    placement: Placement
    root: TargetPath
    status: Literal["installing", "installed", "failed"]
    qualified: Literal[False]
    recipe_sha256: Digest
    models_lock_sha256: Digest
    requirements_locks: Mapping[str, Digest]
    source_commits: Mapping[str, str]
    models: tuple[DownloadResult, ...]
    steps: tuple[StepReceipt, ...]
    health: HealthEvidence | None
    started_at: str
    completed_at: str | None
    error_code: str | None


@dataclass(frozen=True)
class PlacementDescriptor:
    """Inert placement metadata; validation never looks up recipes or providers."""

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
