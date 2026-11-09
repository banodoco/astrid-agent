"""The one placement value a generation carries: where it runs, and how long that stays warm.

``Placement`` is frozen and validated. It is resolved once, request -> user
default -> built-in, by :func:`resolve_placement`, and then travels down as task
data; nothing below re-infers it. The user default is the ``placement`` key of the
user's generation settings document in the Runtime (``settings``); ``path`` names an old
VibeComfy TOML whose ``[placement]`` is imported into that document once. A machine kind is ``local``, ``attached`` or the key of a
``generation.machine`` contribution, validated against the registry rather than a
closed enum. A lease is not a new object: ``machine`` names one live
``RunPodLeaseOwner`` lease, which the warm hold keeps alive.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from astrid.core.contracts.errors import AstridError
from astrid.core.execution.warm_hold import DEFAULT_IDLE_SECONDS

BUILTIN_KINDS = ("local", "attached")
MACHINE_POINT = "generation.machine"
# Until the generation.machine point (Plan 32 T5) ships, the one machine
# provider that exists is the RunPod pack; T5 replaces this with its contribution.
_PROVIDER_FALLBACK = ("runpod",)

LiveLeases = Callable[[str], list[Mapping[str, Any]]]


def _refuse(message: str, *, field_name: str = "placement", options: Any = (), fix: str | None = None) -> AstridError:
    return AstridError(message, code="placement_invalid", field=field_name, valid_options=options, fix=fix)


def _unknown_keys(raw: Mapping[str, Any], allowed: set[str], what: str) -> None:
    extra = sorted(set(raw) - allowed)
    if extra:
        raise _refuse(f"unknown {what} key(s): {', '.join(map(str, extra))}; allowed: {', '.join(sorted(allowed))}",
                      options=sorted(allowed))


@dataclass(frozen=True)
class PlacementPolicy:
    idle_timeout_s: float = float(DEFAULT_IDLE_SECONDS)
    gpu_types: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "PlacementPolicy":
        raw = {} if raw is None else raw
        if not isinstance(raw, Mapping):
            raise _refuse("placement policy must be a table", field_name="policy")
        _unknown_keys(raw, {item.name for item in fields(cls)}, "placement policy")
        idle = raw.get("idle_timeout_s", cls.idle_timeout_s)
        if isinstance(idle, bool) or not isinstance(idle, (int, float)) or not math.isfinite(idle) or idle < 0:
            raise _refuse("idle_timeout_s must be a finite non-negative number", field_name="policy.idle_timeout_s")
        gpus = raw.get("gpu_types", ())
        if isinstance(gpus, str) or not all(isinstance(item, str) and item for item in gpus):
            raise _refuse("gpu_types must be a list of GPU type names", field_name="policy.gpu_types")
        return cls(float(idle), tuple(gpus))

    def as_mapping(self) -> dict[str, Any]:
        return {"idle_timeout_s": self.idle_timeout_s, "gpu_types": list(self.gpu_types)}


@dataclass(frozen=True)
class Placement:
    kind: str = "local"
    device: int | None = None
    endpoint: str | None = None
    engine: str | None = None
    machine: str | None = None
    policy: PlacementPolicy = field(default_factory=PlacementPolicy)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Placement":
        if not isinstance(raw, Mapping):
            raise _refuse("placement must be a table")
        _unknown_keys(raw, {item.name for item in fields(cls)}, "placement")
        values = {key: value for key, value in raw.items() if key != "policy"}
        kind = values.get("kind", "local")
        if not isinstance(kind, str) or not kind:
            raise _refuse("placement kind must be a nonempty string", field_name="kind")
        device = values.get("device")
        if device is not None and (isinstance(device, bool) or not isinstance(device, int) or device < 0):
            raise _refuse("device must be a non-negative local GPU index", field_name="device")
        for name in ("endpoint", "engine", "machine"):
            value = values.get(name)
            if value is not None and (not isinstance(value, str) or not value):
                raise _refuse(f"{name} must be a nonempty string", field_name=name)
        return cls(policy=PlacementPolicy.from_mapping(raw.get("policy")), **values)

    def as_mapping(self) -> dict[str, Any]:
        out = {key: getattr(self, key) for key in ("kind", "device", "endpoint", "engine", "machine")
               if getattr(self, key) is not None}
        return {**out, "policy": self.policy.as_mapping()}


def _merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    merged = {**base, **{key: value for key, value in over.items() if key != "policy"}}
    if "policy" in base or "policy" in over:
        merged["policy"] = {**(base.get("policy") or {}), **(over.get("policy") or {})}
    if over.get("kind") not in (None, base.get("kind")):
        # A different kind is a different place: the default's address does not carry over.
        for key in ("device", "endpoint", "engine", "machine"):
            if key not in over:
                merged.pop(key, None)
    return merged


def resolve_placement(request: Mapping[str, Any] | Placement | None = None, *,
                      path: str | Path | None = None, builtin: Placement | None = None,
                      client: Any = None) -> Placement:
    """request -> user default -> built-in; the only place a placement is chosen."""
    from astrid.core.generation import settings

    default = settings.placement_default(client, legacy_path=path)
    if not isinstance(default, Mapping):
        raise _refuse("the [placement] default must be a table", field_name="placement")
    over = request.as_mapping() if isinstance(request, Placement) else dict(request or {})
    merged = _merge((builtin or Placement()).as_mapping(), _merge(default, over))
    return Placement.from_mapping(merged)


def request_from_selector(selector: Any) -> Mapping[str, Any] | Placement | None:
    """The CLI/SDK spelling of a placement request: ``auto``, ``managed``, a URL or a machine kind."""
    if selector is None or selector == "auto":
        return None
    if isinstance(selector, (Mapping, Placement)):
        return selector
    if not isinstance(selector, str):
        raise _refuse("placement must be auto, managed, a ComfyUI URL, a machine kind or a table")
    if selector in ("managed", "local"):
        return {"kind": "local"}
    if "://" in selector:
        return {"kind": "attached", "endpoint": selector}
    return {"kind": selector}


def comfy_selector(placement: Placement, selector: Any = None) -> str:
    """What ``select_generation_target`` is asked for, from the resolved placement."""
    if placement.kind == "attached":
        return str(placement.endpoint)
    return "managed" if selector == "managed" else "auto"


def registered_machine_kinds() -> tuple[str, ...]:
    """Keys of the registered ``generation.machine`` contributions."""
    try:
        from astrid.core.extensions.registry import ExtensionRegistry

        names = [item.id.rsplit(".", 1)[-1] for item in ExtensionRegistry().load().contributions(MACHINE_POINT)]
    except Exception:  # an unreadable registry must not hide the built-in providers
        names = []
    return tuple(sorted({*names, *_PROVIDER_FALLBACK}))


def _runpod_leases(kind: str) -> list[Mapping[str, Any]]:
    return []


def validate_placement(placement: Placement, *, machine_kinds: tuple[str, ...] | None = None,
                       live_leases: LiveLeases | None = None) -> Placement:
    """Refuse a placement that names nothing real; return it unchanged otherwise."""
    kinds = tuple(registered_machine_kinds() if machine_kinds is None else machine_kinds)
    known = (*BUILTIN_KINDS, *kinds)
    if placement.kind not in known:
        raise _refuse(f"unknown placement kind {placement.kind!r}; known kinds: {', '.join(known)}",
                      field_name="kind", options=known, fix=f"use one of: {', '.join(known)}")
    if placement.kind == "attached":
        if not placement.endpoint:
            raise _refuse("an attached placement needs an endpoint", field_name="endpoint")
        return placement
    if placement.kind == "local":
        if placement.endpoint or placement.machine:
            raise _refuse("a local placement takes a device, not an endpoint or machine", field_name="endpoint")
        return placement
    if placement.device is not None or placement.endpoint:
        raise _refuse(f"a {placement.kind} placement takes a machine and policy, not a device or endpoint")
    if placement.machine:
        leases = (live_leases or _runpod_leases)(placement.kind)
        live = [str(item.get("machine")) for item in leases]
        if placement.machine not in live:
            raise _refuse(f"no live {placement.kind} lease for machine {placement.machine!r}; "
                          f"live leases: {', '.join(live) or 'none'}", field_name="machine", options=live,
                          fix="omit machine to acquire a new one, or name a live lease")
    return placement


__all__ = ["BUILTIN_KINDS", "Placement", "PlacementPolicy", "comfy_selector", "registered_machine_kinds", "request_from_selector", "resolve_placement",
           "validate_placement"]


def resolve_workflow_placement(placement: str | None = None, execution_request: Any = None) -> str:
    """Resolve and validate the placement of a workflow run; read-only, no backend or provider lookup.

    A workflow runs on the comfyui engine, locally by default or on RunPod. A ``runpod``
    execution-request target is accepted only with placement ``runpod`` (the legacy prepared
    route); otherwise it is refused. Profile/machine selectors cannot establish the exact
    RunPod proof target.
    """
    from astrid.core.generation.engine_contracts import PlacementDescriptor, UnsupportedPlacement
    from astrid.sdk.execution_request import normalize_execution_request

    request = normalize_execution_request(execution_request)
    target_kind = request["target"]["kind"] if request else "default"
    if target_kind not in {"default", "runpod"}:
        raise UnsupportedPlacement("workflow requires a default local or exact RunPod target")
    resolved = PlacementDescriptor("astrid.generation-placement.v1", ("local", "runpod"), "local",
                                   "comfyui", True).resolve(placement)
    if (resolved == "runpod") != (target_kind == "runpod"):
        raise UnsupportedPlacement(
            "a runpod target is accepted only with placement runpod (legacy prepared route)"
        )
    return resolved
