"""Installed console dispatch, with a lightweight engine route."""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    rest = list(sys.argv[1:] if argv is None else argv)
    if rest and rest[0] == "generate":
        from .core.gateway import main as gateway_main

        return gateway_main(rest)
    if rest and rest[0] == "engines":
        from .core.engine_cli import main as engines_main

        return engines_main(rest[1:])
    if rest and rest[0] == "action":
        from .core.action_cli import main as action_main

        return action_main(rest[1:])
    from .omp_agent import main as omp_main

    return omp_main(rest)
