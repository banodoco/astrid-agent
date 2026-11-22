"""Installed console dispatch, with a lightweight engine route."""
from __future__ import annotations

import sys
import time
from contextvars import ContextVar


_CLI_STARTED_AT: ContextVar[float | None] = ContextVar("astrid_cli_started_at", default=None)


def cli_started_at() -> float | None:
    """Start of this console invocation, when dispatching through OMP."""
    return _CLI_STARTED_AT.get()


def main(argv: list[str] | None = None) -> int:
    started_at = time.monotonic()
    rest = list(sys.argv[1:] if argv is None else argv)
    if rest and rest[0] == "generate":
        from .core.gateway import main as gateway_main

        return gateway_main(rest)
    if rest and rest[0] == "engines":
        from .core.engine_cli import main as engines_main

        return engines_main(rest[1:])
    if rest and rest[0] == "machines":
        from .core.machines_cli import main as machines_main

        return machines_main(rest[1:])
    if rest and rest[0] == "packs":
        from .core.pack.cli import main as packs_main

        return packs_main(rest[1:])
    if rest and rest[0] == "action":
        from .core.action_cli import main as action_main

        return action_main(rest[1:])
    token = _CLI_STARTED_AT.set(started_at)
    try:
        from .omp_agent import main as omp_main

        return omp_main(rest)
    finally:
        _CLI_STARTED_AT.reset(token)
