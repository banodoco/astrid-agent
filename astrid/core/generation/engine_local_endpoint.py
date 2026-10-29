"""Local engine endpoint producer.

Turns an installed engine's ``engine_status`` into a loopback ``EngineEndpoint``
(placement ``local``, auth ``none``, owner ``astrid-local``) carrying a
``LocalAttestation``. It does no installation: a missing or unhealthy engine
fails fast with the next action to run.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import posixpath
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .engine_contracts import (
    EngineEndpoint, EngineError, EndpointAuth, EndpointOwnershipError, HealthEvidence, LifecycleOwner,
    LocalAttestation, UnsafePathError,
)

DEFAULT_PLATFORM = "linux-x86_64-cu130"
RECEIPT_RELATIVE_PATH = ".astrid-engine/install.json"
# Set (to the engine id) in the environment of a pack host that serves the local placement.
LOCAL_ENGINE_HOST_ENV = "ASTRID_HOST_LOCAL_ENGINE"
# Explicit override of the engine root for run-path resolution; it wins over the recorded install root.
ENGINE_ROOT_ENV = "ASTRID_ENGINE_ROOT"
# Per-user record of the root the last successful local install wrote to: <engines dir>/<engine>.root.json
ENGINE_ROOT_RECORD_SCHEMA = "astrid.engine-root.v1"
_ENGINE_ID = re.compile(r"[a-z][a-z0-9-]*")
_RECORD_LIMIT = 4096


class EngineNotInstalled(EngineError):
    code = "engine_not_installed"

    def __init__(self, engine: str, reason: str):
        self.next_action = f"astrid engines install {engine}"
        super().__init__(f"engine {engine} is not ready locally ({reason}); run: {self.next_action}")


@dataclass(frozen=True)
class LocalEngine:
    endpoint: EngineEndpoint
    comfy_output_dir: str


def _engines_dir() -> Path:
    return Path.home() / ".astrid-data" / "engines"


def _valid_root(value: Any) -> bool:
    """Absolute, normalized, non-root POSIX path without control characters (the Linux install rule)."""
    return (isinstance(value, str) and posixpath.isabs(value) and value != "/"
            and posixpath.normpath(value) == value and not value.startswith("//")
            and "\\" not in value and ":" not in value and not any(ord(c) < 32 for c in value))


def _record_path(engine: str) -> Path | None:
    if not isinstance(engine, str) or _ENGINE_ID.fullmatch(engine) is None:
        return None
    return _engines_dir() / f"{engine}.root.json"


def record_engine_root(engine: str, root: str) -> Path:
    """Record ``root`` as the install root that run-path resolution uses for ``engine``.

    Called after a successful local install.  The record is only a pointer: every read of it still
    requires a healthy install receipt at that root, so a stale or wrong record refuses rather than
    selecting another install.
    """
    path = _record_path(engine)
    if path is None or not _valid_root(root):
        raise UnsafePathError("engine root record needs a plain engine id and an absolute normalized root")
    text = json.dumps({"schema": ENGINE_ROOT_RECORD_SCHEMA, "engine": engine, "root": root},
                      sort_keys=True, separators=(",", ":")) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{engine}.", suffix=".root.tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return path


def recorded_engine_root(engine: str) -> str | None:
    """The root the last successful local install of ``engine`` recorded, or ``None`` if none was recorded.

    A record that exists but is malformed, unreadable or names another engine is refused, never ignored.
    """
    path = _record_path(engine)
    if path is None:
        return None
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EngineNotInstalled(engine, "engine root record is unreadable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > _RECORD_LIMIT:
        raise EngineNotInstalled(engine, "engine root record is not a regular file")
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise EngineNotInstalled(engine, "engine root record is unreadable") from exc
    if (not isinstance(document, dict) or set(document) != {"schema", "engine", "root"}
            or document["schema"] != ENGINE_ROOT_RECORD_SCHEMA or document["engine"] != engine
            or not _valid_root(document["root"])):
        raise EngineNotInstalled(engine, "engine root record is not valid")
    return document["root"]


def default_engine_root(engine: str) -> str:
    """The engine root used when a caller gives none.

    Order: the ``ASTRID_ENGINE_ROOT`` override, then the root the last successful local install recorded,
    then ``~/.astrid-data/engines/<engine>``.  Callers still verify the install receipt at the chosen root.
    """
    override = os.environ.get(ENGINE_ROOT_ENV, "")
    if override:
        return override
    recorded = recorded_engine_root(engine)
    if recorded is not None:
        return recorded
    return str(_engines_dir() / engine)


def _installed_receipt(
    engine: str, *, platform: str, root: str, deadline_seconds: float,
    context: Any, status: Callable[..., Any] | None,
) -> Any:
    """The install receipt of a healthy installed engine, or ``EngineNotInstalled``.

    One gate for everything the local placement derives from the install: the endpoint and the
    model-root binding both start here, so they cannot disagree about what is installed.
    """
    deadline = time.monotonic() + deadline_seconds
    if status is None:
        from .engine_runner import engine_status as status
    if context is None:
        from .engine_execution import create_local_backend
        context, _adapter = create_local_backend(
            engine=engine, platform=platform, placement="local", root=root, deadline=deadline)
    receipt = status(engine, platform=platform, placement="local", root=root,
                     context=context, deadline=deadline)
    if receipt is None:
        raise EngineNotInstalled(engine, "no install receipt")
    if receipt.status == "failed" and getattr(receipt, "error_code", None) == EndpointOwnershipError.code:
        raise EndpointOwnershipError("engine port or supervisor is not owned by this installation")
    health = receipt.health
    if (receipt.status != "installed" or health is None or health.status != "healthy"
            or health.required_nodes_present is not True or not health.object_info_sha256):
        raise EngineNotInstalled(engine, f"install status {receipt.status}, engine not healthy")
    return receipt


def local_engine_endpoint(
    engine: str = "vibecomfy-h3",
    *,
    platform: str = DEFAULT_PLATFORM,
    root: str | None = None,
    deadline_seconds: float = 120.0,
    context: Any = None,
    status: Callable[..., Any] | None = None,
) -> LocalEngine:
    """Return the loopback endpoint of an installed, healthy local engine.

    ``context`` and ``status`` are injection points (default: the local Linux
    execution context and ``engine_runner.engine_status``).
    """
    from .engine_recipe import load_recipe

    root = root or default_engine_root(engine)
    recipe = load_recipe(engine, platform)
    receipt = _installed_receipt(
        engine, platform=platform, root=root, deadline_seconds=deadline_seconds,
        context=context, status=status)
    health = receipt.health

    receipt_path = posixpath.join(root, RECEIPT_RELATIVE_PATH)
    try:
        receipt_sha = hashlib.sha256(Path(receipt_path).read_bytes()).hexdigest()
    except OSError as exc:
        raise EngineNotInstalled(engine, "install receipt unreadable") from exc
    evidence = HealthEvidence(health.status, health.checked_at, health.object_info_sha256, True)
    attestation = LocalAttestation(
        "local-engine.v1", receipt_path, receipt_sha, evidence, health.object_info_sha256)
    endpoint = EngineEndpoint(
        schema="astrid.engine-endpoint.v1",
        endpoint_id=f"{engine}-local",
        engine=engine,
        platform=platform,
        placement="local",
        url=f"http://127.0.0.1:{recipe.start.port}",
        auth=EndpointAuth("none", None),
        state="ready",
        health=evidence,
        lifecycle_owner=LifecycleOwner("astrid-local", "astrid-local", None, None, None, False),
        attestation=attestation,
    )
    from .engine_endpoint import endpoint_to_dict
    endpoint_to_dict(endpoint)  # closed-shape validation (parse_endpoint round trip)
    comfy = next(s.relative_path for s in recipe.sources if s.name == "comfyui")
    return LocalEngine(endpoint, posixpath.join(root, comfy, "output"))


MODELS_RELATIVE_PATH = "models"
_HEX64 = re.compile(r"[0-9a-f]{64}")


def model_root_binding_from_receipt(receipt: Any, root: str, *, engine: str = "vibecomfy-h3") -> Any:
    """The model-root binding attested by an installed engine's receipt.

    The inventory is the receipt's verified model lock: one entry (path under ``<root>/models``,
    size, sha256) per model file the installer downloaded and checked.  Nothing is discovered on
    disk and nothing comes from a readiness profile; ``validate_model_root_binding`` re-hashes the
    actual files against these digests when the binding is used.
    """
    from .model_root import ModelRootBindingError, canonical_model_inventory_digest, validate_model_root_binding

    models_root = posixpath.join(root, MODELS_RELATIVE_PATH)
    models = tuple(getattr(receipt, "models", ()) or ())
    if not models:
        raise EngineNotInstalled(engine, "install receipt records no verified models")
    inventory = []
    for model in models:
        name = getattr(model, "model", "?")
        digest = getattr(model, "sha256", None)
        size = getattr(model, "bytes", None)
        path = getattr(model, "path", None)
        if getattr(model, "verified", None) is not True:
            raise EngineNotInstalled(engine, f"model {name} is not verified in the install receipt")
        if not isinstance(digest, str) or _HEX64.fullmatch(digest) is None:
            raise EngineNotInstalled(engine, f"model {name} has no verified sha256 in the install receipt")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise EngineNotInstalled(engine, f"model {name} has no verified size in the install receipt")
        prefix = models_root + "/"
        if not isinstance(path, str) or not path.startswith(prefix):
            raise EngineNotInstalled(engine, f"model {name} is outside the engine model root")
        subdir, _, filename = path[len(prefix):].rpartition("/")
        inventory.append({"name": filename, "sha256": "sha256:" + digest, "size": size, "subdir": subdir})
    inventory.sort(key=lambda entry: (entry["subdir"], entry["name"]))
    document = {
        "schema_version": 1,
        "path": models_root,
        "inventory": inventory,
        "inventory_digest": canonical_model_inventory_digest(inventory),
        "qualification_identity": None,
    }
    try:
        return validate_model_root_binding(document, verify_files=False)
    except ModelRootBindingError as exc:
        raise EngineNotInstalled(engine, f"install receipt model lock is not a valid binding: {exc}") from exc


def local_model_root_binding(
    engine: str = "vibecomfy-h3",
    *,
    platform: str = DEFAULT_PLATFORM,
    root: str | None = None,
    deadline_seconds: float = 120.0,
    context: Any = None,
    status: Callable[..., Any] | None = None,
) -> Any:
    """Derive the model-root binding from the installed engine (the one derivation).

    Used by ``astrid action run`` and by the pack host for the local placement.  A missing,
    failed or unverified install is refused with ``EngineNotInstalled``, whose message names
    ``astrid engines install <engine>``.
    """
    root = root or default_engine_root(engine)
    receipt = _installed_receipt(
        engine, platform=platform, root=root, deadline_seconds=deadline_seconds,
        context=context, status=status)
    return model_root_binding_from_receipt(receipt, root, engine=engine)
