"""Harness adapter registry for the skills install layer."""

from __future__ import annotations

from .base import Action, HarnessAdapter, InstallRecord, PlannedStep
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .hermes import HermesAdapter
from .octop import OctopAdapter

ADAPTERS: dict[str, type[HarnessAdapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "hermes": HermesAdapter,
    "octop": OctopAdapter,
}


def adapter_for(name: str) -> HarnessAdapter:
    cls = ADAPTERS[name]
    return cls()


def all_adapters() -> dict[str, HarnessAdapter]:
    return {name: cls() for name, cls in ADAPTERS.items()}


__all__ = [
    "ADAPTERS",
    "Action",
    "ClaudeAdapter",
    "CodexAdapter",
    "HarnessAdapter",
    "HermesAdapter",
    "OctopAdapter",
    "InstallRecord",
    "PlannedStep",
    "adapter_for",
    "all_adapters",
]
