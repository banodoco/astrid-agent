"""One phase-timing block for a machine job (``astrid.generation.phase-timing.v1``).

The controller stamps UTC epoch seconds as each phase starts and ends; the
derived block is the same on success and failure and lands in
``terminal["machine"]["timing"]``. Every phase is ``None`` when it did not run.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from functools import lru_cache, wraps
from pathlib import Path
from typing import Any, Callable

SCHEMA = "astrid.generation.phase-timing.v1"
TIER1_CARDS = ("P3", "P4", "W-C", "W-D", "W-E", "W-F", "W-H", "Q2")
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
        self.pod_job: dict[str, Any] | None = None
        self.paid_started_at: float | None = None
        self.idle_before_s: float | None = None
        self._named: list[dict[str, Any]] = []
        self._step_stack: list[int] = []

    def start_step(self, phase: str, step_id: str) -> int:
        with self._lock:
            index = len(self._named)
            now = float(self._clock())
            self._named.append({"phase": phase, "step_id": step_id, "start": now,
                                "end": None, "parent": self._step_stack[-1] if self._step_stack else None})
            self._step_stack.append(index)
        stamp(self, f"pod_step_{index}_started_at", at=now)
        return index

    def end_step(self, index: int | None) -> None:
        with self._lock:
            if index is not None and index in self._step_stack:
                now = float(self._clock())
                # An interrupted lock wait may still be active when its owning
                # preparation exits. Close descendants at that same boundary.
                position = self._step_stack.index(index)
                closed = self._step_stack[position:]
                for active in closed:
                    self._named[active]["end"] = now
                del self._step_stack[position:]
            else:
                return
        for active in closed:
            stamp(self, f"pod_step_{active}_ended_at", at=now)

    def named_steps(self) -> list[dict[str, Any]]:
        """Inclusive wall spans plus exclusive seconds; children never enter the sum twice."""
        with self._lock:
            now = float(self._clock())
            rows = [dict(row) for row in self._named]
        for row in rows:
            row["seconds"] = max(0.0, (now if row["end"] is None else row["end"]) - row["start"])
        for index, row in enumerate(rows):
            row["exclusive_seconds"] = row["seconds"] - sum(
                child["seconds"] for child in rows if child["parent"] == index)
        return rows

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
            for key in ("named_phases", "top_level_phases"):
                if key in record:
                    timing.pod_job[key] = clean_pod_steps(record[key])
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


_POD_TIMING: ContextVar[PhaseTiming | None] = ContextVar("pod_phase_timing", default=None)


def current_pod_timing() -> PhaseTiming | None:
    return _POD_TIMING.get()


@contextmanager
def pod_timing_scope(timing: PhaseTiming | None = None):
    timing = timing if timing is not None else PhaseTiming(clock=time.monotonic)
    token = _POD_TIMING.set(timing)
    try:
        yield timing
    finally:
        _POD_TIMING.reset(token)


@contextmanager
def pod_phase(phase: str, step_id: str):
    """Optional pod-only wall timing; a clock failure must preserve the job result."""
    timing, index = current_pod_timing(), None
    with suppress(Exception):
        if timing is not None:
            index = timing.start_step(phase, step_id)
    try:
        yield
    finally:
        with suppress(Exception):
            if timing is not None:
                timing.end_step(index)


def pod_step(phase, step_id):
    """Name a trusted step without changing its arguments, return value or exception."""
    def decorate(operation):
        @wraps(operation)
        def measured(*args, **kwargs):
            if current_pod_timing() is None:
                return operation(*args, **kwargs)
            name, identifier = operation.__name__, operation.__name__
            with suppress(Exception):
                name = phase(*args, **kwargs) if callable(phase) else phase
                identifier = step_id(*args, **kwargs) if callable(step_id) else step_id
            if name is None:
                return operation(*args, **kwargs)
            with pod_phase(name, identifier):
                return operation(*args, **kwargs)
        return measured
    return decorate


POD_TIMING_MARK = "ASTRID_POD_TIMING_JSON="


def read_pod_reports(stderr: str) -> list[dict[str, Any]]:
    reports = []
    if not isinstance(stderr, str):
        return reports
    for line in stderr.splitlines():
        if line.startswith(POD_TIMING_MARK) and len(line) <= 65536:
            with suppress(ValueError, TypeError, AttributeError):
                rows = clean_pod_steps(json.loads(line[len(POD_TIMING_MARK):]).get("named_phases"))
                if isinstance(rows, list) and rows:
                    reports.append({"named_phases": rows})
    return reports


def attach_pod_recheck(timing: Any, reports: Any) -> None:
    with suppress(Exception):
        if isinstance(timing, PhaseTiming) and reports:
            if timing.pod_job is not None:
                timing.pod_job["collection_recheck"] = reports
            if isinstance(timing.generation, GenerationTiming):
                timing.generation.pod_collection_recheck = reports


def clean_pod_steps(value):
    """Only fixed timing fields cross the optional evidence boundary."""
    mapping = isinstance(value, dict)
    source = value.items() if mapping else enumerate(value) if isinstance(value, list) else ()
    result = {}
    for key, row in source:
        if len(result) >= 512 or not isinstance(row, dict):
            continue
        if any(not isinstance(row.get(k), str) or not row[k] or len(row[k]) > 256
               or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:/-" for c in row[k])
               for k in ("phase", "step_id")):
            continue
        seconds = _seconds(row.get("seconds"))
        if seconds is None or seconds < 0:
            continue
        clean = {"phase": row["phase"], "step_id": row["step_id"], "seconds": seconds}
        for field in ("exclusive_seconds", "start", "end"):
            number = _seconds(row.get(field))
            if number is not None:
                clean[field] = number
        if row.get("parent") is None or type(row.get("parent")) is int:
            clean["parent"] = row.get("parent")
        result[str(key)] = clean
    return result if mapping else list(result.values())


def pod_timing_report(phase: str, step_id: str):
    """Emit pod-local status timing on stderr only for the requesting controller.

    This process runs after the generation evidence is sealed. Its span must
    remain outside that earlier whole_job; no cross-process endpoints are subtracted.
    """
    def decorate(operation):
        @wraps(operation)
        def reported(*args, **kwargs):
            if os.environ.get("ASTRID_POD_PHASE_TIMING") != "1":
                return operation(*args, **kwargs)
            with pod_timing_scope() as timing:
                try:
                    with pod_phase(phase, step_id):
                        return operation(*args, **kwargs)
                finally:
                    with suppress(Exception):
                        print(POD_TIMING_MARK + json.dumps({"named_phases": timing.named_steps()}), file=sys.stderr)
        return reported
    return decorate


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
        self.pod_collection_recheck: list[dict[str, Any]] | None = None
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
                top = clean_pod_steps(self.job_phases.get("top_level_phases"))
                if isinstance(top, dict) and top:
                    execution["sub_phases"] = top
                subtotal = sum(row["seconds"] or 0.0 for row in execution["sub_phases"].values())
                execution["sub_phases_seconds"] = subtotal
                # Pod whole-job overlaps its measured subphases; keep it as a
                # nested span, never add it to the exclusive phase sum.
                execution["pod_job"] = {name: {"seconds": _seconds(source.get(name))}
                                        for name in ("whole_job", "unaccounted")}
                if "named_phases" in self.job_phases:
                    execution["pod_job"]["named_phases"] = clean_pod_steps(self.job_phases["named_phases"])
                execution["engine_reused"] = self.job_phases.get("engine_reused")
                execution["unaccounted"] = _remainder(execution["seconds"], subtotal)
            if self.pod_collection_recheck:
                phases["collection"]["pod_recheck"] = self.pod_collection_recheck
            return {"total": {"seconds": total}, "phases": phases, "tier1_cards": list(TIER1_CARDS),
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

    def __init__(self, clock: Callable[[], float] | None = None, *, started: float | None = None) -> None:
        self.clock = clock or time.monotonic
        now = _now(self.clock)
        entry = _seconds(started)
        # Embedded callers may inject another clock or inherit a stale gateway
        # environment value. Never subtract a future stamp from this clock.
        self.started = entry if entry is not None and now is not None and entry <= now else now
        self.stamps: dict[str, float] = {}
        self.host: dict[str, Any] | None = None
        self.delivery_phases: dict[str, float] = {}
        self.phase_stack: list[str] = []
        self.phase_at: float | None = None

    def phase_boundary(self, now: float | None) -> None:
        if self.phase_stack and now is not None and self.phase_at is not None:
            name = self.phase_stack[-1]
            self.delivery_phases[name] = self.delivery_phases.get(name, 0.0) + max(0.0, now - self.phase_at)
        self.phase_at = now

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
        named = sum(self.delivery_phases.values())
        if fetch is not None:
            phases["result_fetch_and_print"]["seconds"] = max(0.0, fetch - named)
        phases.update({name: {"seconds": self.delivery_phases.get(name)} for name in
                       ("delivery", "materialisation", "project_open_url", "output_hashing")})
        total = elapsed(self.started, now)
        return {"total": {"seconds": total}, "same_clock": same, "phases": phases,
                "unaccounted": _remainder(total, sum(row["seconds"] or 0.0 for row in phases.values()))}


@contextmanager
def cli_phase(name: str):
    """Exclusive nested caller work; timings never change delivery behavior."""
    active = _ACTIVE_CLI.get()
    if active is None:
        yield
        return
    active.phase_boundary(_now(active.clock))
    active.phase_stack.append(name)
    try:
        yield
    finally:
        active.phase_boundary(_now(active.clock))
        active.phase_stack.pop()


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
