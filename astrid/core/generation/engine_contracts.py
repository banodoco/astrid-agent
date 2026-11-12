"""Typed, immutable contracts shared by packaged engine recipes and adapters."""
from __future__ import annotations

import posixpath
import random
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, ContextManager, Literal, Mapping, Protocol, TypeAlias

# Keep the original check text here; the raise-site inventory guards coverage.
_ENGINE_REASONS = frozenset({
    'A target-native --root is required for Windows',
    'An engine owner is still active; retry after it exits.',
    'An engine owner is still running a job; wait for it to finish.',
    'Cannot inspect the local engine queue; leave its supervisor running and restore its control connection before retrying installation',
    "Cannot stop the original supervisor; restore its owner's control connection and retry installation; leave the marker in place",
    'Engine custody directory must be owner-only. /workspace is FUSE-unsafe; install under a local --root.',
    'Engine custody identity does not match this installation.',
    'Engine custody is locked by another owner; retry after it releases the lock.',
    'Engine custody marker is invalid; inspect the local engine state before retrying.',
    'Engine endpoint is not owned by this installation.',
    'Engine owner is unreachable; custody remains unresolved.',
    'Engine owner state could not be verified; custody remains unresolved.',
    'Engine request does not match the bound launch or owner.',
    'Engine supervisor did not confirm command cleanup; inspect the engine state before retrying.',
    'Linux helper requires a Linux recipe',
    'Linux root must be an absolute normalized path',
    'RunPod execution awaits P2-SELF orchestration',
    'RunPod execution awaits W1 (runpod wrapper)',
    'RunPod requires a Linux target',
    'Timed out waiting for the engine owner lock; retry after it releases the lock.',
    'VibeComfy source identity unavailable',
    'VibeComfy source revision changed',
    'Windows execution awaits A7',
    'Windows root must be an absolute drive path',
    'Windows runner execution requires the A7 adapter',
    'Windows status requires the A7 adapter',
    'active supervisor operation did not drain',
    'adapter changed the bound install root',
    'adapter changed the install root',
    'adapter launch differs from verified recipe',
    'adapter launch environment differs from recipe',
    'adapter start request violates recipe binding',
    'cancelled engine command drainage is unconfirmed',
    'comfyui recipe must bind a VibeComfy source at source_bindings',
    'command termination is unproven',
    'committed installed receipt bytes differ',
    'committed installed receipt differs from verified launch',
    'conflicting selected resource digests',
    'context and adapter platform must match Linux',
    'context, adapter and plan target must match',
    'could not inspect source revision',
    'current evidence differs from installed receipt',
    'cyclic pin set reference',
    'deadline must be a finite monotonic time',
    'deadline must be finite',
    'deadline must be greater than zero and at most 604800 seconds',
    'declared entry asset failed size/SHA verification',
    'duplicate environment name',
    'duplicate model destination',
    'duplicate model name',
    'duplicate pin authority',
    'duplicate requirements source path',
    'duplicate source name',
    'duplicate source path',
    'engine bind host must be loopback',
    'engine command exited with a nonzero status',
    'engine command requires a nonempty public argv',
    'engine contribution is unreadable',
    'engine control directory missing',
    'engine control state must be private to its owner',
    'engine custody directory must be an owner-only directory',
    'engine custody requires Linux',
    'engine deadline exceeded',
    'engine does not support the requested platform',
    'engine has no pinned source with the requested name',
    'engine health or required nodes failed',
    'engine helper code/data identity differs',
    'engine id has invalid syntax',
    'engine is installed but stale; run astrid engines install again',
    'engine is not registered in the selected catalog',
    'engine listener ownership changed',
    'engine ownership changed during health',
    'engine port is occupied by an unowned service',
    'engine port or supervisor is not owned by this installation',
    'engine root record needs a plain engine id and an absolute normalized root',
    'engine sanity command failed before any model download',
    'engine session stopping',
    'engine state file is a symlink',
    'engine supervisor operation failed',
    'entry declaration conflicts with managed inventory',
    'environment final verification failed',
    'environment has no node lock',
    'environment has no verified receipt',
    'environment probe inputs differ from persisted lock',
    'exclude_newer must be timezone-aware ISO8601',
    'existing VibeComfy registry cannot be adopted',
    'expected bounded helper operation and JSON request',
    'hash selection must be boolean',
    'hash-bearing requirements lock was not persisted',
    'health path must be a safe same-origin absolute URL path',
    'health timeouts must be positive and poll interval cannot exceed timeout',
    'install lock inode changed',
    'install lock path changed',
    'install lock root differs',
    'install plan differs from the recomputed verified plan',
    'install receipt identity differs',
    'install receipt identity differs from verified recipe',
    'install receipt is a symlink',
    'install receipt placement differs',
    'install receipt placement differs from requested placement',
    'install receipt root differs',
    'install receipt root differs from requested root',
    'install root must be a target-native absolute path',
    'install root must be normalized and below filesystem root',
    'install_engine requires an InstallPlan',
    'installed environment no longer matches its lock',
    'installed launch has no bound GPU visibility',
    'installed receipt changed during managed preparation',
    'installed status requires the A2 production Linux adapter',
    'installed supervisor code/recipe differs from caller',
    'installed wheel RECORD digest is missing',
    'installed wheel RECORD digests are unreadable',
    'interrupted requirements lock identity differs',
    'invalid bounded command',
    'invalid command exit status',
    'invalid engine helper request',
    'invalid engine state file',
    'invalid environment bootstrap pins',
    'invalid environment receipt',
    'invalid index engine/platform',
    'invalid install receipt evidence',
    'invalid install receipt fields',
    'invalid install receipt file',
    'invalid install receipt state',
    'invalid installation engine',
    'invalid installation identity',
    'invalid local GPU device',
    'invalid local GPU visibility',
    'invalid owner frame size',
    'invalid service helper payload',
    'invalid service helper request',
    'invalid service ownership state',
    'invalid start placeholder',
    'invalid supervisor cleanup diagnostic',
    'invalid supervisor marker; explicit recovery required',
    'invalid supervisor operation budget',
    'invalid target JSON',
    'invalid target helper request',
    'invalid target helper response',
    'live engine custody is required',
    'live launch evidence is not verified',
    'load_model_lock requires a DataRef',
    'load_pin_set requires a DataRef',
    'local engine helper identity differs',
    'local engine installation evidence differs from its receipt',
    'local engine requires a completed installation',
    'local engine supervisor is not installed',
    'lock directory changed',
    'malformed recipe placeholder',
    'managed engine is busy',
    'managed launch requires a completed installation',
    'missing primary environment or comfyui source',
    'model digest must be a lowercase SHA-256 digest',
    'model lock is invalid JSON',
    'model lock recipe validation failed',
    'model must have exactly one valid source form',
    'model.bytes must be a positive integer',
    'model.revision must be an immutable lowercase commit',
    'models root must be a target-native absolute path',
    'models root must be normalized',
    'models root must not be inside or contain the engine root',
    'more than one engine row for a platform',
    'mounted model cache requires local execution',
    'native engine custody unresolved',
    'native owned process cleanup unresolved',
    'node lock bytes differ',
    'node lock commit differs from canonical source',
    'node lock differs from pinned source file',
    'node lock verification failed',
    'node_lock references unknown source',
    'owned engine command exceeded deadline',
    'owned engine exited before readiness',
    'owned engine is absent',
    'owned engine server is absent',
    'owned executable could not start',
    'owned output drain unresolved',
    'owned process launch unresolved',
    'owner frame exceeds limit',
    'packaged JSON is unreadable',
    'packaged data digest mismatch',
    'packaged data is unavailable',
    'packaged data path escapes the package',
    'packaged requirements lock mismatch',
    'patch replacement must be packaged data',
    'persisted requirements lock has no matching input identity',
    'pin keys must be non-empty strings',
    'pin revision must be a full lowercase Git commit',
    'pin version must be a non-empty string',
    'pin_set_refs requires a DataRef',
    'pinned source is absent',
    'pinned source origin is unavailable',
    'pins are invalid JSON',
    'pins schema must be astrid.engine-pins.v1 with a source map',
    'placement is not supported by this backend',
    'plan_install requires an EngineRecipe',
    'primary_environment does not name an environment',
    'recipe URL must be credential-free HTTPS',
    'recipe and adapter target must match',
    'recipe differs from the verified packaged recipe',
    'recipe digest must be a lowercase SHA-256 digest',
    'recipe identity differs from index entry',
    'recipe is invalid JSON',
    'recipe must bind at least one source',
    'recipe object has missing or unknown fields',
    'recipe path must be a safe normalized POSIX relative path',
    'recipe placeholder values must be strings',
    'recipe value must be a string with the required content',
    'recipe value must be an array',
    'recipe value must be an object',
    'recipe.engine has invalid syntax',
    'recipe.platform is unsupported',
    'recipes row must start with a list marker',
    'recipes row needs platform, path and sha256',
    'recover the exact prior command before retrying installation',
    'refusing to initialize a nonempty unowned source directory',
    'requirements input is absent',
    'requirements input references unknown source',
    'requirements lock contains an unsupported directive',
    'requirements lock is empty or incomplete',
    'requirements lock lacks exact hash-bearing requirements',
    'requirements lock state changed',
    'requirements mode fields are inconsistent',
    'resource is not declared by the selected pack catalog',
    'reviewed engine helper closure is incomplete',
    'selected engine catalog must belong to this Astrid package',
    'selected pack resource digest differs from declaration',
    'selected resource escapes its owner',
    'selected resource is outside this package',
    'serialize_model_lock requires a ModelLock',
    'serialize_recipe requires an EngineRecipe',
    'server start did not establish ownership',
    'service evidence does not match installation',
    'service helper identity differs',
    'service incarnation or launch binding differs',
    'service request differs from bound recipe or owner',
    'service response exceeds output limit',
    'session retains an unresolved native cleanup obligation',
    'source contains uncommitted content',
    'source directory is not empty',
    'source identity deadline exceeded',
    'source install must disable dependency resolution and build isolation',
    'source install references unknown source',
    'source or Git directory is unsafe',
    'source origin differs from canonical pin',
    'source parents must precede nested checkouts',
    'source patch digest mismatch',
    'source patch unavailable',
    'source references unknown pin',
    'source revision differs from canonical pin',
    'source validation output was truncated',
    'start.argv must not be empty',
    'start.bind_host must be loopback',
    'start.env must map strings to strings',
    'start.port must be in 1..65535',
    'status context does not match requested target',
    'step dependencies have not passed',
    'supervisor did not confirm command cleanup before the response deadline',
    'supervisor incarnation changed during cancellation',
    'supervisor installation identity differs',
    'supervisor launch uncertain; explicit recovery required',
    'supervisor marker changed; explicit recovery required',
    'supervisor peer has a different user',
    'supervisor process evidence unavailable; leave its control state in place',
    'supervisor unavailable; explicit recovery required',
    'target JSON exceeds limit',
    'target JSON is missing',
    'target JSON must be an object',
    'target changed during inspection',
    'target changed during read',
    'target engine helper identity differs',
    'target filesystem deadline exceeded',
    'target helper operation failed',
    'target helper path escapes installation',
    'target helper path traverses a symlink',
    'target is not a singly linked regular file',
    'target parent is missing',
    'target path outside install root',
    'target platform has no supported adapter',
    'target requirements lock differs from packaged lock',
    'target write made no progress',
    'the local engine is being updated; try again when astrid engines install finishes',
    'unbound supervisor response',
    'unbound target helper response',
    'unknown engine helper operation',
    'unknown engine placement',
    'unknown model lock schema',
    'unknown named environment',
    'unknown recipe placeholder',
    'unknown recipe schema',
    'unknown recipes row field',
    'unknown requirements mode',
    'unknown source',
    'unknown source or duplicate source patch',
    'unreadable recipes line',
    'unsafe VibeComfy source identity member',
    'unsafe Windows install root',
    'unsafe engine control directory',
    'unsafe engine owner lock',
    'unsafe install lock file',
    'unsafe install lock inode',
    'unsafe lock directory',
    'unsafe or inaccessible target path',
    'unsafe requirements lock',
    'unsafe target JSON leaf',
    'unsafe target helper temporary file',
    'unsafe target path',
    'unsafe target relative path',
    'unsupported Linux target platform',
    'unsupported engine service operation',
    'unsupported supervisor operation',
    'uv version differs from recipe',
})


def engine_reason(value: object) -> str | None:
    """Accept only a declared fixed check, including after a transport round trip."""
    return value if isinstance(value, str) and value in _ENGINE_REASONS else None


EngineId: TypeAlias = str
PlatformKey: TypeAlias = Literal["linux-x86_64-cu124", "linux-x86_64-cu128", "linux-x86_64-cu130", "windows-x86_64-cu128", "windows-x86_64-cu130"]
Placement: TypeAlias = str
Gate: TypeAlias = Literal["identity", "state", "image", "storage", "hardware", "region",
                          "rate", "lifetime", "inventory", "capacity", "install"]
Digest: TypeAlias = str
Deadline: TypeAlias = float
TargetPath: TypeAlias = str


class EngineError(RuntimeError):
    """Stable, secret-free public error shape shared by engine adapters."""

    code: ClassVar[str] = "engine_error"

    def __init__(self, message: str, *, step_id: str | None = None,
                 retryable: bool = False, recovery_handle: str | None = None,
                 reason: str | None = None):
        super().__init__(message)
        self.code = type(self).code
        self.message = message
        self.reason = engine_reason(message if reason is None else reason)
        self.step_id = step_id
        self.retryable = retryable
        self.recovery_handle = recovery_handle
        # Attached only by custody-aware callers; CLI validates before public rendering.
        self.custody_details: dict[str, str]


class EngineBusy(EngineError):
    """The local engine has work that excludes this operation."""

    code = "engine_busy"

    def __init__(self, message: str = "managed engine is busy"):
        super().__init__(message, retryable=True)


class PlacementRefused(EngineError):
    """A placement gate refused progress with explicit retry and custody policy."""

    code = "placement_refused"

    def __init__(self, message: str, *, gate: Gate, retryable: bool = False,
                 retry_after: float | None = None,
                 custody: Literal["none", "exact", "unknown"] = "unknown"):
        super().__init__(message, retryable=retryable)
        self.gate = gate
        self.retry_after = retry_after
        self.custody = custody


def next_delay(attempt: int, retry_after: float | None) -> float:
    """Exponential observation delay, respecting a provider's minimum delay."""
    base = min(60.0, 5.0 * 2 ** min(4, max(0, attempt - 1)))
    return max(retry_after or 0.0, base) + random.uniform(0.0, 0.5)


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


# Public custody diagnostics use a closed vocabulary.  Transport output and owner
# identifiers never cross this boundary.
_CUSTODY_REASONS = frozenset({
    "identity_mismatch", "owner_alive", "owner_busy", "owner_unknown", "owner_lock_held",
    "marker_invalid", "owner_unreachable", "cleanup_unconfirmed", "owner_lock_timeout", "request_mismatch", "endpoint_unowned", "custody_not_private",
})
_CUSTODY_OWNER_STATES = frozenset({"alive", "dead", "unknown"})
_CUSTODY_LOCK_STATES = frozenset({"held", "available", "unknown"})
_CUSTODY_EVIDENCE = frozenset({"flock", "boot_id", "pid_start_ticks", "different_pod", "none"})
_CUSTODY_MESSAGES = {
    "custody_not_private": "Engine custody directory must be owner-only. /workspace is FUSE-unsafe; install under a local --root.",
    "identity_mismatch": "Engine custody identity does not match this installation.",
    "request_mismatch": "Engine request does not match the bound launch or owner.",
    "endpoint_unowned": "Engine endpoint is not owned by this installation.",
    "owner_alive": "An engine owner is still active; retry after it exits.",
    "owner_busy": "An engine owner is still running a job; wait for it to finish.",
    "owner_unknown": "Engine owner state could not be verified; custody remains unresolved.",
    "owner_lock_held": "Engine custody is locked by another owner; retry after it releases the lock.",
    "marker_invalid": "Engine custody marker is invalid; inspect the local engine state before retrying.",
    "owner_unreachable": "Engine owner is unreachable; custody remains unresolved.",
    "cleanup_unconfirmed": "Engine supervisor did not confirm command cleanup; inspect the engine state before retrying.",
    "owner_lock_timeout": "Timed out waiting for the engine owner lock; retry after it releases the lock.",
}
_CUSTODY_ACTIONS = {
    "custody_not_private": "Choose an owner-only engine root on local disk before retrying.",
    "endpoint_unowned": "Ask the endpoint owner to free the port, then retry; do not adopt or stop its process.",
    "request_mismatch": "Use the installed engine launch and its current owner, then retry.",
    "identity_mismatch": "Restore the old owner control or verify it exited; do not kill it or remove its marker.",
    "owner_alive": "Wait for the active owner to exit, then retry.",
    "owner_busy": "Wait for the current job to finish, then retry.",
    "owner_unknown": "Leave the marker in place; obtain proof the owner has exited before retrying.",
    "owner_lock_held": "Wait for the owner lock to be released, then retry.",
    "marker_invalid": "Inspect or repair the custody marker before retrying.",
    "owner_unreachable": "Leave the marker in place; restore owner connectivity and prove it exited before retrying.",
    "cleanup_unconfirmed": "Inspect engine processes and custody state before retrying.",
    "owner_lock_timeout": "Wait for the owner lock to be released, then retry.",
}


def custody_diagnostics(value: Any) -> dict[str, str] | None:
    """Return a sanitized, closed-vocabulary custody diagnostic or None."""
    if not isinstance(value, Mapping):
        return None
    reason = value.get("reason")
    owner_state = value.get("owner_state")
    lock_state = value.get("lock_state")
    evidence = value.get("evidence")
    root = value.get("root")
    marker_path = value.get("marker_path")
    engine = value.get("engine")
    helper_sha256 = value.get("helper_sha256")
    import re
    if (not isinstance(reason, str) or reason not in _CUSTODY_REASONS
            or not isinstance(owner_state, str) or owner_state not in _CUSTODY_OWNER_STATES
            or not isinstance(lock_state, str) or lock_state not in _CUSTODY_LOCK_STATES
            or not isinstance(evidence, str) or evidence not in _CUSTODY_EVIDENCE):
        return None
    if (not isinstance(root, str) or len(root) > 4096 or not root.startswith("/")
            or "\x00" in root or "\n" in root or "\r" in root
            or not isinstance(marker_path, str) or marker_path != root.rstrip("/") + "/.astrid-engine/session/session.json"
            or len(marker_path) > 4096 or "\x00" in marker_path or "\n" in marker_path or "\r" in marker_path
            or not isinstance(engine, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", engine)):
        return None
    if not isinstance(helper_sha256, str) or not re.fullmatch(r"(?:[0-9a-f]{12}|unknown)", helper_sha256):
        return None
    return {"reason": reason, "owner_state": owner_state, "lock_state": lock_state,
            "evidence": evidence, "marker_path": marker_path, "root": root,
            "engine": engine, "helper_sha256": helper_sha256,
            "next_action": _CUSTODY_ACTIONS[reason]}


def custody_message(details: Any) -> str:
    """Render only fixed public text for a validated custody reason."""
    if (isinstance(details, Mapping) and isinstance(details.get("reason"), str)
            and details.get("reason") in _CUSTODY_MESSAGES):
        base = _CUSTODY_MESSAGES[details["reason"]]
        # Add only fields that passed the public diagnostic validator.
        clean = custody_diagnostics(details)
        if clean is None:
            return base
        return (f"{base} root={clean['root']} engine={clean['engine']} "
                f"helper_sha256={clean['helper_sha256']} marker_path={clean['marker_path']} "
                f"owner_state={clean['owner_state']} evidence={clean['evidence']} "
                f"lock_state={clean['lock_state']}. {clean['next_action']}")
    return "Engine custody could not be verified; inspect the engine state before retrying."


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
class SourcePatch:
    source: str
    path: str
    sha256_before: Digest
    sha256_after: Digest
    replacement: str


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
    source_patches: tuple[SourcePatch, ...] = ()


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


@dataclass(frozen=True)
class CommandRequest:
    argv: tuple[str, ...]
    cwd: TargetPath
    env: Mapping[str, str | None]
    deadline: Deadline
    max_output_bytes: int = 262144


@dataclass(frozen=True)
class CommandResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float
    job_id: str | None
    stdout_truncated: bool
    stderr_truncated: bool


@dataclass(frozen=True)
class FileFact:
    exists: bool
    regular: bool
    symlink: bool
    bytes: int | None
    sha256: Digest | None


class ExecutionContext(Protocol):
    placement: Placement
    platform: PlatformKey

    def run(self, request: CommandRequest) -> CommandResult: ...
    def ensure_dir(self, path: TargetPath, *, deadline: Deadline) -> None: ...
    def read_json(self, path: TargetPath, *, deadline: Deadline) -> Mapping: ...
    def write_json_atomic(self, path: TargetPath, value: Mapping,
                          *, deadline: Deadline) -> None: ...
    def file_fact(self, path: TargetPath, *, hash_bytes: bool,
                  deadline: Deadline) -> FileFact: ...
    def acquire_install_lock(self, root: TargetPath, *,
                             deadline: Deadline, kind: Literal["install", "job"] = "job") -> ContextManager[None]: ...


class PlatformAdapter(Protocol):
    key: PlatformKey

    def local_install_lock(self, root: TargetPath, *, deadline: Deadline,
                           cancelled: Callable[[], bool] | None = None,
                           on_contention: Callable[[str], None] | None = None,
                           kind: Literal["install", "job"] = "job") -> ContextManager[None]: ...
    def join(self, root: TargetPath, *relative: str) -> TargetPath: ...
    def validate_root(self, root: TargetPath) -> TargetPath: ...
    def venv_python(self, venv_root: TargetPath) -> TargetPath: ...
    def uv_executable(self, runtime_root: TargetPath) -> TargetPath: ...
    def environment(self, recipe: EngineRecipe, root: TargetPath) -> Mapping[str, str]: ...
    def environment_commands(self, recipe: EngineRecipe, environment: EnvironmentSpec,
                             root: TargetPath) -> tuple[tuple[str, ...], ...]: ...
    def start_request(self, recipe: EngineRecipe, root: TargetPath,
                      *, deadline: Deadline, models_root: TargetPath | None = None) -> CommandRequest: ...


StepKind: TypeAlias = Literal["source", "environment", "start", "health"]


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
    # Where the engine's model files live; None means <root>/models (the default install).
    models_root: TargetPath | None = None


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
class EndpointAuth:
    kind: Literal["none", "bearer-file"]
    token: SecretRef | None


@dataclass(frozen=True)
class LifecycleOwner:
    kind: Literal["attached", "astrid-local", "astrid-runpod"]
    owner_id: str
    handle_path: str | None
    job_id: str | None
    pod_id: str | None
    release_on_finish: bool


@dataclass(frozen=True)
class LocalAttestation:
    kind: Literal["local-engine.v1"]
    install_receipt_path: str
    install_receipt_sha256: Digest
    health: HealthEvidence
    object_info_sha256: Digest


@dataclass(frozen=True)
class RunPodAttestation:
    kind: Literal["runpod-engine.v1"]
    provider_account_ref: str
    pod_id: str
    claim_handle_path: str
    claim_handle_sha256: Digest
    install_receipt_path: str
    install_receipt_sha256: Digest
    health: HealthEvidence
    object_info_sha256: Digest


EndpointState: TypeAlias = Literal["planned", "acquiring", "starting", "ready", "failed",
                                  "stopping", "releasing", "released", "cleanup_pending"]


@dataclass(frozen=True)
class EngineEndpoint:
    schema: Literal["astrid.engine-endpoint.v1"]
    endpoint_id: str
    engine: EngineId
    platform: PlatformKey
    placement: Placement
    url: str
    auth: EndpointAuth
    state: EndpointState
    health: HealthEvidence | None
    lifecycle_owner: LifecycleOwner
    attestation: LocalAttestation | RunPodAttestation | None


@dataclass(frozen=True)
class EndpointOptions:
    platform: PlatformKey
    root: TargetPath
    handle_path: str | None
    attach: bool = False
    release_on_finish: bool = True


@dataclass(frozen=True)
class EndpointProfileEvidence:
    schema: Literal["astrid.runpod.endpoint-evidence.v1"]
    engine: EngineId
    platform: PlatformKey
    claim_operation_id: str
    claim_attestation: RunPodAttestation


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
    # Read-only status diagnostic; absent in receipts written before this field.
    failed_step_id: str | None = None
    # Model directory the install bound; absent in receipts written before this field (then <root>/models).
    models_root: TargetPath | None = None


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
        from astrid.core.generation.placement import registered_machine_kinds

        known = {"local", *registered_machine_kinds()}
        if any(not isinstance(value, str) or value not in known
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


def effective_models_root(root: str, models_root: str | None) -> str:
    """The model directory an install binds: its explicit cache, else ComfyUI/models."""

    return models_root if models_root is not None else posixpath.join(root, "runtime", "ComfyUI", "models")



