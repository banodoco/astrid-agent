"""Shared, test-only managed-tool profile registration for the M2-11a session tests.

Profiles are host-owned and keyed by managed tool. These helpers write one test
host's registry at the instance level, after construction. Nothing a pack can
reach calls them, and nothing here changes the class-level or production table.
"""
from __future__ import annotations

from typing import Any, Callable

from astrid.core.execution.generic_host import ManagedToolProfile


def probe_compile(inputs: Any) -> dict[str, Any]:
    """Test-only host compile for a probe tool: refuses inputs without a prompt."""
    if not isinstance(inputs, dict) or not inputs.get("prompt"):
        raise ValueError("prompt is required")
    return {"prompt": str(inputs["prompt"]), "message": str(inputs.get("message", ""))}


def register_test_profile(host: Any, pack_id: str, invoke: Callable[..., Any],
                          compile_settings: Callable[[Any], Any] = probe_compile) -> None:
    """Register a test-only profile for ``pack_id`` on this host instance only."""
    host._managed_tool_profiles[pack_id] = ManagedToolProfile(
        compile_settings=compile_settings, invoke=invoke)
