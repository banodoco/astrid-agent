"""Start or reuse the installed local engine's long-lived server (Plan 32 T4g).

Kept out of ``engine_local_endpoint``: that module is in the bootstrap closure, which has a size budget.
"""

from __future__ import annotations

import os
import posixpath
import time
from typing import Any, Callable

from .engine_local_endpoint import DEFAULT_PLATFORM, RECEIPT_RELATIVE_PATH, default_engine_root


def ensure_local_engine_server(
    engine: str = "comfyui",
    *,
    platform: str = DEFAULT_PLATFORM,
    root: str | None = None,
    deadline_seconds: float = 120.0,
    runner_factory: Callable[..., Any] | None = None,
) -> str:
    """Start the installed engine's long-lived server, or reuse the one this install owns.

    The same probe/start ``_Runner.start`` does at install time: an owned server is reused
    untouched (same process), an absent one is started, an unowned one on the port is refused.
    Nothing here ever stops the server; it outlives the task. Returns ``"reused"``, ``"started"`` or
    ``"not_installed"``. An engine that was never installed is left to ``local_engine_endpoint``.
    """
    from .engine_recipe import load_recipe

    root = root or default_engine_root(engine)
    if not os.path.isfile(posixpath.join(root, RECEIPT_RELATIVE_PATH)):
        return "not_installed"  # nothing to start: local_engine_endpoint raises EngineNotInstalled
    deadline = time.monotonic() + deadline_seconds
    if runner_factory is None:
        from .engine_contracts import IntegrityError
        from .engine_execution import create_local_backend
        from .engine_runner import (
            _parse_receipt,
            _Runner,
            plan_from_receipt,
            plan_install,
            refresh_installed_launch,
        )
        context, adapter = create_local_backend(
            engine=engine, platform=platform, placement="local", root=root, deadline=deadline)
        with context.acquire_install_lock(root, deadline=deadline):
            raw = context.read_json(posixpath.join(root, RECEIPT_RELATIVE_PATH), deadline=deadline)
            recipe = load_recipe(engine, platform)
            locator = plan_install(recipe, root=root, placement="local")
            receipt = _parse_receipt(raw, locator)
            plan = plan_from_receipt(recipe, receipt, root=root, placement="local")
            if receipt.status != "installed":
                raise IntegrityError("local engine requires a completed installation")
            runner = _Runner(plan, context, adapter, deadline)
            runner.step_id = "helper-identity"
            if runner.helper("identity") != {"helper_sha256": runner.helper_sha256}:
                raise IntegrityError("local engine helper identity differs")
            for step in plan.steps:
                if step.kind in {"source", "environment"}:
                    runner.execute(step, readonly=True)
            if runner.locks != receipt.requirements_locks or runner.commits != receipt.source_commits:
                raise IntegrityError("local engine installation evidence differs from its receipt")
            runner.step_id = "start"
            action, facts = runner.start(readonly=False)
            runner.step_id = "health"
            health_action, health_facts = runner.check_health()
            refresh_installed_launch(runner, raw, start_action=action, start_facts=facts,
                                     health_action=health_action, health_facts=health_facts,
                                     check=lambda: None)
    else:
        runner = runner_factory(engine=engine, platform=platform, root=root, deadline=deadline)
        runner.step_id = "start"
        action, _facts = runner.start(readonly=False)
    return "reused" if action == "skipped" else "started"
