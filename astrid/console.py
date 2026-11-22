"""Installed console dispatch, with a lightweight engine route."""
from __future__ import annotations

import os
import sys
import time
from contextvars import ContextVar


_CLI_STARTED_AT: ContextVar[float | None] = ContextVar("astrid_cli_started_at", default=None)


def cli_started_at() -> float | None:
    """Start of this console invocation, when dispatching through OMP."""
    return _CLI_STARTED_AT.get()


def process_started_at(fallback: float) -> float:
    """Return this process's monotonic start time, or the command-entry fallback."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/self/stat", encoding="ascii") as stream:
                stat = stream.read()
            command_end = stat.rsplit(")", 1)[1]
            fields = command_end.split()
            # The remainder starts at field 3 (state); starttime is field 22.
            start_ticks = int(fields[19])
            ticks_per_second = os.sysconf("SC_CLK_TCK")
            if ticks_per_second <= 0:
                return fallback
            boot_age = start_ticks / ticks_per_second
            mono_before = time.monotonic()
            boot_now = time.clock_gettime(time.CLOCK_BOOTTIME)
            mono_after = time.monotonic()
            monotonic_now = (mono_before + mono_after) / 2
            if boot_age < 0 or boot_age > boot_now:
                return fallback
            started_at = monotonic_now - (boot_now - boot_age)
        elif sys.platform == "darwin" or os.name == "nt":
            try:
                import psutil
            except ImportError:
                return fallback
            try:
                real_before = time.time()
                mono_before = time.monotonic()
                created_at = psutil.Process().create_time()
                mono_after = time.monotonic()
                real_after = time.time()
            except (psutil.Error, OSError, OverflowError, TypeError, ValueError):
                return fallback
            real_now = (real_before + real_after) / 2
            monotonic_now = (mono_before + mono_after) / 2
            started_at = monotonic_now - (real_now - created_at)
        else:
            return fallback
        return started_at if started_at <= fallback else fallback
    except (
        AttributeError,
        IndexError,
        OSError,
        OverflowError,
        TypeError,
        ValueError,
        ZeroDivisionError,
    ):
        return fallback


def main(argv: list[str] | None = None) -> int:
    rest = list(sys.argv[1:] if argv is None else argv)
    started_at = time.monotonic()
    if argv is None and rest and rest[0] == "timelines":
        started_at = process_started_at(started_at)
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
