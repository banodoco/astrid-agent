"""Local engine endpoint producer.

Turns an installed engine's ``engine_status`` into a loopback ``EngineEndpoint``
(placement ``local``, auth ``none``, owner ``astrid-local``) carrying a
``LocalAttestation``. It does no installation: a missing or unhealthy engine
fails fast with the next action to run.
"""
from __future__ import annotations

import hashlib
import posixpath
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .engine_contracts import EngineEndpoint, EngineError, EndpointAuth, HealthEvidence, LifecycleOwner, LocalAttestation

DEFAULT_PLATFORM = "linux-x86_64-cu130"
RECEIPT_RELATIVE_PATH = ".astrid-engine/install.json"


class EngineNotInstalled(EngineError):
    code = "engine_not_installed"

    def __init__(self, engine: str, reason: str):
        self.next_action = f"astrid engines install {engine}"
        super().__init__(f"engine {engine} is not ready locally ({reason}); run: {self.next_action}")


@dataclass(frozen=True)
class LocalEngine:
    endpoint: EngineEndpoint
    comfy_output_dir: str


def default_engine_root(engine: str) -> str:
    return str(Path.home() / ".astrid-data" / "engines" / engine)


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
    health = receipt.health
    if (receipt.status != "installed" or health is None or health.status != "healthy"
            or health.required_nodes_present is not True or not health.object_info_sha256):
        raise EngineNotInstalled(engine, f"install status {receipt.status}, engine not healthy")

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
