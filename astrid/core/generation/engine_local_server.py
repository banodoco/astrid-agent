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
    Nothing here ever stops the server; it outlives the task. Returns ``"reused"`` or
    ``"started"``. An engine that was never installed is left to ``local_engine_endpoint``.
    """
    from .engine_recipe import load_recipe

    root = root or default_engine_root(engine)
    if not os.path.isfile(posixpath.join(root, RECEIPT_RELATIVE_PATH)):
        return "reused"  # nothing installed: local_engine_endpoint raises EngineNotInstalled
    deadline = time.monotonic() + deadline_seconds
    if runner_factory is None:
        from .engine_execution import create_local_backend
        from .engine_runner import _Runner, plan_install
        context, adapter = create_local_backend(
            engine=engine, platform=platform, placement="local", root=root, deadline=deadline)
        plan = plan_install(load_recipe(engine, platform), root=root, placement="local")
        runner = _Runner(plan, context, adapter, deadline)
    else:
        runner = runner_factory(engine=engine, platform=platform, root=root, deadline=deadline)
    runner.step_id = "start"
    action, _facts = runner.start(readonly=False)
    return "reused" if action == "skipped" else "started"
