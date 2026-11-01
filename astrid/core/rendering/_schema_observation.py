"""Optional inherited observation channel for an admitted render.

The collector owns and authenticates the pipe and its process/run association.
No descriptor or identity is created here; missing evidence is a refusal.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from collections.abc import Mapping, MutableMapping
from pathlib import Path

FD_ENV = "ASTRID_TIMELINE_OBSERVER_FD"
RUN_ENV = "ASTRID_TIMELINE_OBSERVER_RUN_ID"
_RUN = re.compile(r"[0-9a-f]{32}\Z")


def observation_context(run_id: object, raw_fd: object) -> tuple[str, int] | None:
    """Admit only a nonblocking inherited pipe and a kernel UUID-hex run ID."""
    if not isinstance(run_id, str) or not _RUN.fullmatch(run_id):
        return None
    if not isinstance(raw_fd, str) or not re.fullmatch(r"[1-9][0-9]*", raw_fd):
        return None
    try:
        fd = int(raw_fd)
        if fd < 3 or not stat.S_ISFIFO(os.fstat(fd).st_mode) or os.get_blocking(fd):
            return None
        # A read end must not be mistaken for the host's writable endpoint.
        import fcntl
        if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_WRONLY:
            return None
        return run_id, fd
    except (OSError, OverflowError, ValueError, ImportError):
        return None


def bind_host_context(capability: str, run_id: object, env: MutableMapping[str, str],
                      host_env: Mapping[str, str]) -> tuple[int, ...]:
    """Replace untrusted child overlays with the admitted host context."""
    env.pop(FD_ENV, None)
    env.pop(RUN_ENV, None)
    context = observation_context(run_id, host_env.get(FD_ENV)) if capability == "rendering.render" else None
    if context is None:
        return ()
    env[RUN_ENV], fd = context
    env[FD_ENV] = str(fd)
    return (fd,)


def emit_schema_consumed(schema: dict, origin: str) -> None:
    context = observation_context(os.environ.get(RUN_ENV), os.environ.get(FD_ENV))
    if context is None:
        return
    try:
        pid = os.getpid()
        # comm can contain spaces and parentheses; fields after the last ')' begin at field 3.
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        if not stat_line.startswith(f"{pid} ("):
            return
        ticks = int(stat_line[stat_line.rindex(")") + 2:].split()[19])
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        if pid <= 0 or ticks <= 0 or str(uuid.UUID(boot)) != boot:
            return
        if not isinstance(schema, dict) or not isinstance(origin, str) or not origin or not Path(origin).is_absolute():
            return
        document = json.dumps(schema, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        timestamp = time.monotonic_ns()
        if timestamp <= 0:
            return
        event = {
            "event": "timeline_schema_consumed", "run_id": context[0],
            "monotonic_ns": timestamp, "pid": pid,
            "start_ticks": ticks, "boot_id": boot,
            "schema_sha256": hashlib.sha256(document).hexdigest(),
            "schema_reader_origin_sha256": hashlib.sha256(origin.encode("utf-8")).hexdigest(),
        }
        data = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(data) <= os.fpathconf(context[1], "PC_PIPE_BUF"):
            os.write(context[1], data)
    except (OSError, ValueError, TypeError, UnicodeError, IndexError, OverflowError):
        # An unavailable collector cannot break rendering or qualify the run.
        return
