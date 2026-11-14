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
# Keep the built-in machine kind discoverable when no extension registry is supplied.
_PROVIDER_FALLBACK = ("runpod",)

LiveLeases = Callable[[str], list[Mapping[str, Any]]]


def _refuse(message: str, *, field_name: str = "placement", options: Any = (), fix: str | None = None) -> AstridError:
    return AstridError(message, code="placement_invalid", field=field_name, valid_options=options, fix=fix)


def _unknown_keys(raw: Mapping[str, Any], allowed: set[str], what: str) -> None:
    extra = sorted(set(raw) - allowed)
    if extra:
        raise _refuse(f"unknown {what} key(s): {', '.join(map(str, extra))}; allowed: {', '.join(sorted(allowed))}",
                      options=sorted(allowed))


@dataclass(frozen=True, init=False)
class PlacementPolicy:
    idle_timeout_s: float = float(DEFAULT_IDLE_SECONDS)
    gpu_types: tuple[str, ...] = ()
    requested_idle_timeout_s: float | None = field(default=None, init=False, compare=False, repr=False)

    def __init__(self, idle_timeout_s: float | None = None, gpu_types: tuple[str, ...] = ()) -> None:
        object.__setattr__(self, "idle_timeout_s", float(DEFAULT_IDLE_SECONDS) if idle_timeout_s is None else idle_timeout_s)
        object.__setattr__(self, "gpu_types", gpu_types)
        object.__setattr__(self, "requested_idle_timeout_s", idle_timeout_s)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "PlacementPolicy":
        raw = {} if raw is None else raw
        if not isinstance(raw, Mapping):
            raise _refuse("placement policy must be a table", field_name="policy")
        _unknown_keys(raw, {item.name for item in fields(cls) if item.init}, "placement policy")
        idle = raw.get("idle_timeout_s", cls.idle_timeout_s)
        if isinstance(idle, bool) or not isinstance(idle, (int, float)) or not math.isfinite(idle) or idle < 0:
            raise _refuse("idle_timeout_s must be a finite non-negative number", field_name="policy.idle_timeout_s")
        gpus = raw.get("gpu_types", ())
        if isinstance(gpus, str) or not all(isinstance(item, str) and item for item in gpus):
            raise _refuse("gpu_types must be a list of GPU type names", field_name="policy.gpu_types")
        return cls(float(idle) if "idle_timeout_s" in raw else None, tuple(gpus))

    def as_mapping(self) -> dict[str, Any]:
        # Keep omission through resolution and serialization: a remote job
        # inherits its claim's hold unless a request or setting supplied one.
        return {"gpu_types": list(self.gpu_types), **(
            {"idle_timeout_s": self.idle_timeout_s} if self.requested_idle_timeout_s is not None else {})}


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


def is_runpod_placement(value: Any) -> bool:
    """Whether a placement selector or resolved placement names RunPod."""
    if isinstance(value, str):
        return value == "runpod"
    if isinstance(value, (Mapping, Placement)):
        return value.get("kind") == "runpod" if isinstance(value, Mapping) else value.kind == "runpod"
    return False


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
                      client: Any = None, repo: str | Path | None = None) -> Placement:
    """request -> user default -> built-in; the only place a placement is chosen.

    An unrequested placement with no default kind follows a bound ComfyUI
    attachment (env, repo or global config) when one exists; otherwise it is local.
    An explicit request or default is never overridden by discovery.
    """
    from astrid.core.generation import settings

    default = settings.placement_default(client, legacy_path=path)
    if not isinstance(default, Mapping):
        raise _refuse("the [placement] default must be a table", field_name="placement")
    over = request.as_mapping() if isinstance(request, Placement) else dict(request or {})
    merged = _merge((builtin or Placement()).as_mapping(), _merge(default, over))
    if not over and "kind" not in default and merged.get("kind") == "local":
        attached = _discovered_attachment(repo)
        if attached is not None:
            merged = _merge(merged, attached.as_mapping())
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


def attached_comfy_url(selector: Any) -> str:
    """The loopback ComfyUI a selector places work on, for reading a saved workflow against its schema.

    ``''`` when the selector resolves to no attached endpoint (managed, a machine kind, a
    non-loopback address or a refused placement): the run reports those itself.
    """
    from astrid.core.generation import targets
    try:
        placement = resolve_placement(request_from_selector(selector))
        if placement.kind != "attached" or not placement.endpoint:
            return ""
        return targets.normalize_target({"endpoint": placement.endpoint}).endpoint
    except (AstridError, targets.TargetConfigError, ValueError):
        return ""


def comfy_selector(placement: Placement) -> str:
    """What ``select_generation_target`` is asked for, from the resolved placement.

    Pure: an attached placement is its endpoint and a local one is the managed
    engine. Nothing here reads config, so an explicit local placement cannot reach
    an attached ComfyUI.
    """
    if placement.kind == "attached":
        return str(placement.endpoint)
    if placement.kind == "local":
        return "managed"
    raise _refuse(f"a {placement.kind} placement has no ComfyUI target", field_name="kind")


def _discovered_attachment(repo: str | Path | None = None) -> Placement | None:
    """The one read of ComfyUI config that picks a placement; None means local.

    A bound human endpoint (env, repo or global config) is attached as-is: the
    attached probe reports it unreachable rather than switching. An unbound
    discovery address is attached only when it answers, so an unconfigured user
    with a running ComfyUI keeps the attached route, and an absent one stays local.
    """
    from astrid.core.contracts.errors import AstridError
    from astrid.core.generation import targets
    from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient

    try:
        selection = targets.resolve_target("auto", repo=repo)
    except targets.TargetConfigError as exc:
        raise AstridError(
            str(exc), code="COMFY_TARGET_INVALID", field="target",
            fix="Repair the saved ComfyUI config or explicitly select a loopback URL or managed target",
        ) from exc
    record = selection.record
    if record is None or record.owner == "astrid":
        return None
    if record.endpoint is None:
        if selection.bound:
            raise AstridError(
                "Selected human ComfyUI target has no endpoint; launch it with explicit consent first",
                code="COMFY_TARGET_UNAVAILABLE", field="target",
                fix="Launch the selected ComfyUI and explicitly select its loopback URL",
            )
        return None
    if not selection.bound:
        try:
            facts = ComfyTargetClient(ComfyTarget(record.endpoint, "attached")).request_json(
                "/system_stats", timeout=1.0)
        except (OSError, ValueError, TimeoutError):
            return None
        if not isinstance(facts, dict) or not isinstance(facts.get("system"), dict):
            return None
    return Placement(kind="attached", endpoint=record.endpoint)


def registered_machine_kinds() -> tuple[str, ...]:
    """Keys of the registered ``generation.machine`` contributions."""
    try:
        from astrid.core.extensions.registry import ExtensionRegistry

        names = [item.id.rsplit(".", 1)[-1] for item in ExtensionRegistry().load().contributions(MACHINE_POINT)]
    except Exception:  # noqa: BLE001 - reported through the raised error or fallback; diagnostic at debug, never a user-facing traceback
        import logging
        logging.getLogger(__name__).debug(
            "machine contribution registry could not be read; using built-in provider kinds", exc_info=True,
        )
        names = []
    return tuple(sorted({*names, *_PROVIDER_FALLBACK}))


def _runpod_leases(kind: str) -> list[Mapping[str, Any]]:
    """Live leases of a machine kind, read from its lease journal (no provider call)."""
    if kind != "runpod":
        return []
    from astrid.packs.runpod.machine import journal_leases

    return [{"machine": row["id"], **row} for row in journal_leases()]


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
        live = [str(item.get("machine")) for item in leases if item.get("live", True)]
        if placement.machine not in live:
            ended = next((item.get("ended") for item in leases
                          if str(item.get("machine")) == placement.machine and item.get("ended")), None)
            raise _refuse(f"no live {placement.kind} lease for machine {placement.machine!r}"
                          f"{f' (ended: {ended})' if ended else ''}; "
                          f"live leases: {', '.join(live) or 'none'}", field_name="machine", options=live,
                          fix="omit machine to acquire a new one, or name a live lease")
    return placement


__all__ = ["BUILTIN_KINDS", "Placement", "PlacementPolicy", "comfy_selector", "is_runpod_placement", "registered_machine_kinds", "request_from_selector", "resolve_placement",
           "validate_placement"]
