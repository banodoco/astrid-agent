"""One phase-timing block for a machine job (``astrid.generation.phase-timing.v1``).

The controller stamps UTC epoch seconds as each phase starts and ends; the
derived block is the same on success and failure and lands in
``terminal["machine"]["timing"]``. Every phase is ``None`` when it did not run.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import subprocess
import threading
import time
from contextlib import suppress
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

SCHEMA = "astrid.generation.phase-timing.v1"
#: phase -> (start stamp, end stamp)
PHASES = {
    "capacity_wait": ("capacity_wait_started_at", "claimed_at"),
    "preparation": ("preparation_started_at", "preparation_ended_at"),
    "execution": ("job_started_at", "job_ended_at"),
    "collection": ("collection_started_at", "collection_ended_at"),
    "release": ("release_started_at", "released_at"),
}


class PhaseTiming:
    """Thread-safe stamp collector; ``block()`` is the JSON-safe result."""

    def __init__(self, clock: Callable[[], float] = time.time, *, generation: Any = None) -> None:
        self._clock = clock
        self.generation = generation
        self._lock = threading.Lock()
        self.stamps: dict[str, float] = {}
        #: Controller-observed first time each pod phase (align, run, ...) was seen by a poll.
        self.pod_phases: list[dict[str, Any]] = []
        self.paid_started_at: float | None = None
        self.idle_before_s: float | None = None

    def stamp(self, name: str, at: float | None = None, *, first: bool = False) -> None:
        with self._lock:
            if first and name in self.stamps:
                return
            self.stamps[name] = float(self._clock() if at is None else at)
        stamp(self.generation, name, first=first)

    def pod_phase(self, phase: Any) -> None:
        if not isinstance(phase, str) or not phase:
            return
        with self._lock:
            if not self.pod_phases or self.pod_phases[-1]["phase"] != phase:
                self.pod_phases.append({"phase": phase, "at": float(self._clock())})

    def block(self) -> dict[str, Any]:
        with self._lock:
            stamps = dict(self.stamps)
            pod_phases = [dict(row) for row in self.pod_phases]
            paid, idle = self.paid_started_at, self.idle_before_s
        phases: dict[str, Any] = {}
        for name, (start, end) in PHASES.items():
            began, ended = stamps.get(start), stamps.get(end)
            phases[name] = (None if began is None else
                            {"start": began, "end": ended,
                             "seconds": None if ended is None else round(max(0.0, ended - began), 3)})
        released = stamps.get("released_at")
        phases["paid_clock"] = (None if paid is None else
                                {"start": paid, "end": released,
                                 "seconds": None if released is None else round(max(0.0, released - paid), 3)})
        phases["idle"] = None if idle is None else {"before_s": round(float(idle), 3)}
        return {"schema": SCHEMA, "stamps": stamps, "phases": phases, "pod_phases": pod_phases}


def stamp(timing: Any, name: str, **kw: Any) -> None:
    """Stamp when a timing collector exists; never let a stamp fail a job."""
    if timing is None:
        return
    try:
        timing.stamp(name, **kw)
    except Exception:  # noqa: BLE001 - timing must not fail a job
        pass


def attach(machine: dict[str, Any] | None, timing: Any) -> None:
    """Refresh ``machine["timing"]`` from the collector."""
    if machine is None or timing is None:
        return
    try:
        machine["timing"] = timing.block()
    except Exception:  # noqa: BLE001 - timing reporting must not fail a job
        pass


__all__ = ["PhaseTiming", "SCHEMA", "attach", "stamp"]


TIMING_SCHEMA = "astrid.generation.timing.v2"
HOST_PHASES = ("admission", "capacity_wait", "preparation", "execution", "collection", "release")
JOB_PHASES = ("verify_models", "engine_start_or_reuse", "queue_wait", "execute", "collect")


@lru_cache(maxsize=1)
def clock_id() -> str | None:
    """Identify a shared monotonic clock, conservatively; never use epoch subtraction.

    Linux boot IDs distinguish reboots and machines. macOS's boot identity is
    used only as an identity token, never as an interval endpoint. The value is
    published in results and receipts that people share, so it never contains
    the hostname (often a person's name): on macOS it is a truncated hash of the
    boot time, equal for every process on one boot of one machine.
    """
    with suppress(Exception):
        if platform.system() == "Linux":
            return "linux:" + Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if platform.system() == "Darwin":
            boot = subprocess.check_output(["sysctl", "-n", "kern.boottime"], timeout=1, text=True).strip()
            return "darwin:" + hashlib.sha256(boot.encode("utf-8")).hexdigest()[:16]
    return None


def _seconds(value: Any) -> float | None:
    return float(value) if type(value) in (float, int) and math.isfinite(value) else None


def _now(clock: Callable[[], float]) -> float | None:
    with suppress(Exception):
        return _seconds(clock())
    return None


def _remainder(total: float | None, accounted: float) -> dict[str, Any]:
    raw = None if total is None else total - accounted
    return {"seconds": None if raw is None else max(0.0, raw), "raw_seconds": raw}


class GenerationTiming:
    """Exclusive host intervals. All endpoints belong to this process's clock.

    Missing stamps remain visible as null durations and in unaccounted time.
    The v1 collector may forward its phase boundaries here without forwarding
    its epoch values. Re-entering a phase appends another disjoint interval.
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self.started = _now(self._clock)
        self.ended: float | None = None
        self.active: tuple[str, float] | None = None
        self.intervals: list[dict[str, Any]] = []
        self.seen: set[str] = set()
        self.job_phases: dict[str, Any] | None = None
        with suppress(Exception):
            self.transition("admission", at=self.started)

    def transition(self, phase: str | None, *, at: float | None = None) -> None:
        with self._lock:
            if self.ended is not None:
                return
            now = _now(self._clock) if at is None else at
            if now is None:
                return
            if self.active is not None:
                name, start = self.active
                self.intervals.append({"phase": name, "start": start, "end": now,
                                       "seconds": max(0.0, now - start)})
            self.active = None if phase is None else (phase, now)

    def stamp(self, name: str, *, first: bool = False, **_kw: Any) -> None:
        with self._lock:
            if first and name in self.seen:
                return
            self.seen.add(name)
            starts = {start: phase for phase, (start, _) in PHASES.items()}
            ends = {end: phase for phase, (_, end) in PHASES.items()}
            if name in starts:
                self.transition(starts[name])
            elif name in ends and self.active is not None and self.active[0] == ends[name]:
                self.transition(None)

    def read_job(self, output_root: Path) -> None:
        """Read only the fetched job's named evidence, including failed jobs.

        run_logs.read_index records the name of a declared log; its inventory is the source
        of that filename. Never search arbitrary output or credential files.
        """
        with suppress(Exception):
            from astrid.core._shared import run_logs
            paths = [output_root / "job-run.json", output_root / "logs" / "job-run.json"]
            paths.extend(output_root / "logs" / row["file"] for row in run_logs.read_index(output_root)
                         if isinstance(row, dict) and row.get("name") == "run"
                         and isinstance(row.get("file"), str) and Path(row["file"]).name == row["file"]
                         and row["file"].endswith(".json"))
            for path in paths:
                if not path.is_file() or path.is_symlink() or path.stat().st_size > 128 * 1024:
                    continue
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict) and value.get("schema") == "astrid.job-run.v1":
                    self.job_phases = value
                    return

    def block(self, *, finish: bool = True) -> dict[str, Any]:
        with self._lock:
            now = self.ended if self.ended is not None else _now(self._clock)
            if finish and self.ended is None:
                with suppress(Exception):
                    self.transition(None, at=now)
                self.ended = now
            intervals = [dict(row) for row in self.intervals]
            if not finish and self.active is not None and now is not None:
                name, start = self.active
                intervals.append({"phase": name, "start": start, "end": now, "seconds": max(0.0, now - start)})
            total = None if now is None or self.started is None else max(0.0, now - self.started)
            phases = {}
            for name in HOST_PHASES:
                rows = [row for row in intervals if row["phase"] == name]
                phases[name] = {"seconds": sum(row["seconds"] for row in rows) if rows else None}
            if self.job_phases is not None:
                execution = phases["execution"]
                source = self.job_phases.get("phases")
                source = source if isinstance(source, dict) else {}
                execution["sub_phases"] = {name: {"seconds": _seconds(source.get(name))} for name in JOB_PHASES}
                subtotal = sum(row["seconds"] or 0.0 for row in execution["sub_phases"].values())
                execution["sub_phases_seconds"] = subtotal
                execution["engine_reused"] = self.job_phases.get("engine_reused")
                execution["unaccounted"] = _remainder(execution["seconds"], subtotal)
            return {"total": {"seconds": total}, "phases": phases,
                    "unaccounted": _remainder(total, sum(row["seconds"] or 0.0 for row in phases.values())),
                    "clock_id": clock_id(), "started_monotonic": self.started, "ended_monotonic": now,
                    "intervals": intervals}


def transition(timing: Any, phase: str | None) -> None:
    with suppress(Exception):
        if timing is not None:
            timing.transition(phase)


def attach_host(target: dict[str, Any], timing: Any, *, output_root: Path | None = None) -> None:
    with suppress(Exception):
        if timing is not None:
            if output_root is not None:
                timing.read_job(output_root)
            target["timing"] = {"schema": TIMING_SCHEMA, "host": timing.block()}


_ACTIVE_CLI: ContextVar[Any] = ContextVar("generation_cli_timing", default=None)


class CliTiming:
    """Client-only intervals; host duration is embedded, never epoch-subtracted."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self.clock = clock or time.monotonic
        self.started = _now(self.clock)
        self.stamps: dict[str, float] = {}
        self.host: dict[str, Any] | None = None

    def stamp(self, name: str, **_kw: Any) -> None:
        now = _now(self.clock)
        if now is not None:
            self.stamps.setdefault(name, now)

    def block(self) -> dict[str, Any]:
        now = _now(self.clock)
        def elapsed(start, end):
            return None if start is None or end is None else max(0.0, end - start)
        runtime = self.stamps.get("runtime")
        submit_start, submitted = self.stamps.get("submit"), self.stamps.get("submitted")
        fetched = self.stamps.get("result")
        host = self.host or {}
        host_total = (host.get("total") or {}).get("seconds")
        same = clock_id() is not None and host.get("clock_id") == clock_id()
        if same:
            claim = host.get("started_monotonic")
            wait = elapsed(submitted, claim)
            fetch = elapsed(host.get("ended_monotonic"), now)
        else:
            # Remove host work and post-result delivery from the client's wait.
            # The remainder also includes polling latency/transport; no remote
            # epoch or monotonic endpoints are compared.
            span = elapsed(submitted, fetched)
            wait = None if span is None or host_total is None else max(0.0, span - host_total)
            fetch = elapsed(fetched, now)
        submit_end = min(submitted, claim) if same and submitted is not None and claim is not None else submitted
        phases = {"cli_startup": {"seconds": elapsed(self.started, runtime)},
                  "submit": {"seconds": elapsed(submit_start, submit_end)},
                  "wait_for_claim": {"seconds": wait}, "host": {"seconds": host_total},
                  "result_fetch_and_print": {"seconds": fetch}}
        total = elapsed(self.started, now)
        return {"total": {"seconds": total}, "same_clock": same, "phases": phases,
                "unaccounted": _remainder(total, sum(row["seconds"] or 0.0 for row in phases.values()))}


def cli_stamp(name: str) -> None:
    stamp(_ACTIVE_CLI.get(), name)


def cli_host(value: Any) -> None:
    with suppress(Exception):
        active = _ACTIVE_CLI.get()
        timing = value.get("timing") if isinstance(value, dict) else None
        if active is not None and isinstance(timing, dict) and isinstance(timing.get("host"), dict):
            active.host = timing["host"]


def attach_cli(payload: Any) -> None:
    with suppress(Exception):
        cli_host(payload)
        active = _ACTIVE_CLI.get()
        if active is not None and isinstance(payload, dict):
            timing = dict(payload.get("timing") or {"schema": TIMING_SCHEMA})
            payload["timing"] = timing
            if active.host is not None:
                timing.setdefault("host", active.host)
            timing["cli"] = active.block()


def _summary(timing: Any) -> str:
    """One compact line, including both levels of unaccounted time."""
    if not isinstance(timing, dict):
        return ""
    def number(value):
        return "?" if value is None else f"{value:.1f}"
    def render(block):
        pieces = []
        for name, phase in block.get("phases", {}).items():
            if phase.get("seconds") is None:
                continue
            text = f"{name} {number(phase['seconds'])}"
            if "sub_phases" in phase:
                nested = [f"{key} {number(row['seconds'])}" for key, row in phase["sub_phases"].items()]
                nested.extend([f"sum {number(phase['sub_phases_seconds'])}",
                               f"unaccounted {number(phase['unaccounted']['seconds'])}",
                               f"engine_reused={phase.get('engine_reused')}"])
                text += " (" + ", ".join(nested) + ")"
            pieces.append(text)
        pieces.append("unaccounted " + number(block.get("unaccounted", {}).get("seconds")))
        return number(block.get("total", {}).get("seconds")) + "s = " + " + ".join(pieces)
    parts = [render(timing["host"])] if "host" in timing else []
    if "cli" in timing:
        parts.append("cli " + render(timing["cli"]))
    return "timing: " + "; ".join(parts) if parts else ""


def summary(timing: Any) -> str:
    with suppress(Exception):
        return _summary(timing)
    return ""
