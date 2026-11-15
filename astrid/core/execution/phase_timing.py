"""One phase-timing block for a machine job (``astrid.generation.phase-timing.v1``).

The controller stamps UTC epoch seconds as each phase starts and ends; the
derived block is the same on success and failure and lands in
``terminal["machine"]["timing"]``. Every phase is ``None`` when it did not run.
"""

from __future__ import annotations

import json
import math
import threading
import time
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

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self.stamps: dict[str, float] = {}
        #: Controller-observed first time each pod phase (align, run, ...) was seen by a poll.
        self.pod_phases: list[dict[str, Any]] = []
        self.pod_job: dict[str, Any] | None = None
        self.paid_started_at: float | None = None
        self.idle_before_s: float | None = None

    def stamp(self, name: str, at: float | None = None, *, first: bool = False) -> None:
        with self._lock:
            if first and name in self.stamps:
                return
            self.stamps[name] = float(self._clock() if at is None else at)

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
            pod_job = self.pod_job
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
        return {"schema": SCHEMA, "stamps": stamps, "phases": phases, "pod_phases": pod_phases,
                **({"pod_job": pod_job} if pod_job is not None else {})}


def ingest_job_run(timing: Any, files: dict[str, Any]) -> None:
    """Nest verified pod evidence in v1 without any extra transport operation."""
    if not isinstance(timing, PhaseTiming):
        return
    try:
        path = files.get("job-run.json") or files.get("logs/run.json")
        if path is None:
            return
        with Path(path).open("rb") as stream:
            record = json.loads(stream.read(65537))
        if record.get("schema") != "astrid.job-run.v1":
            return
        phases = record.get("phases")
        if not isinstance(phases, dict):
            return
        names = {"verify_models", "engine_start_or_reuse", "queue_wait", "execute", "collect",
                 "whole_job", "unaccounted"}
        clean = {name: value for name, value in phases.items() if name in names and
                 (value is None or type(value) in (int, float) and math.isfinite(value))}
        with timing._lock:
            timing.pod_job = {"schema": record["schema"], "phases": clean}
    except Exception:  # noqa: BLE001 - optional evidence must never fail delivery
        pass


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


__all__ = ["PhaseTiming", "SCHEMA", "attach", "ingest_job_run", "stamp"]
