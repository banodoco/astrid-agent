"""Astrid: a harness toolkit for agents and humans to make art."""

from __future__ import annotations

import time as _entry_time

# Both console scripts and python -m astrid import this package first.
_PROCESS_ENTRY_MONOTONIC = _entry_time.monotonic()

import importlib  # noqa: E402 - entry clock precedes heavy imports

from .version import __version__  # noqa: E402

_SDK_EXPORTS = (
    # Curated public SDK surface: lazy
    # discovery, typed invoke, generate, and protocol rendering entrypoint, and the
    # public DTO/exception taxonomy. The raw runner seams
    # (``run_executor``/``run_orchestrator``) and caller registry-injection
    # helpers are internal to ``astrid.sdk`` and are never exported here.
    # Ordering of this tuple is not a compatibility promise; the frozen
    # curated name set in tests/_sdk_contract.py stays authoritative.
    "discover",
    "get_capability",
    "invoke",
    "generate",
    "renderer_main",
    "support",
    "RenderContext",
    "Capability",
    "DiscoveryResult",
    "EventStreamRecord",
    "InvocationResult",
    "AstridSDKError",
    "CapabilityNotFoundError",
    "CapabilityAmbiguousError",
    "CapabilityValidationError",
    "CapabilityMissingInputError",
    "CapabilityPreconditionError",
    "CapabilityRuntimeError",
    "CapabilityLeaseError",
    "CapabilityEventLogError",
    "UnsupportedCapabilityError",
    "CapabilityInvocationError",
    "CapabilityHandle",
    "Port",
    "Output",
    "AliasRecord",
    "Provenance",
    "SafetyDeclaration",
    "ExecError",
)

__all__ = _SDK_EXPORTS


def __getattr__(name: str):
    if name == "audit":
        return importlib.import_module(".core.audit", __name__)
    if name == "AstridClient":
        # Lazy client lifecycle: importing ``astrid`` must never import the
        # SDK package or open a database. ``AstridClient`` is deliberately
        # not part of ``__all__`` (the frozen curated SDK-name tuple in
        # tests/_sdk_contract.py stays authoritative); it is reachable as
        # ``astrid.AstridClient`` / ``from astrid import AstridClient`` and
        # from ``astrid.sdk``.
        return importlib.import_module(".sdk.client", __name__).AstridClient
    if name not in _SDK_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(".sdk", __name__)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_SDK_EXPORTS) | {"audit", "AstridClient"})
