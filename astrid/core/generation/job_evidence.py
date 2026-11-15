"""Best-effort, attempt-scoped engine evidence for managed jobs."""
from __future__ import annotations

import json
import math
import os
import time
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrid.core._shared import run_logs
from astrid.core.execution.phase_timing import TIER1_CARDS, PhaseTiming, stamp

from .engine_custody import engine_log_path

_ACTIVE: ContextVar[JobEvidence | None] = ContextVar("job_evidence", default=None)


@contextmanager
def job_evidence_scope(evidence: JobEvidence | None):
    token = _ACTIVE.set(evidence)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def mark_job_collect() -> None:
    """The in-process producer reached local result finalization; never affects it."""
    with suppress(Exception):
        evidence = _ACTIVE.get()
        if evidence is not None:
            evidence.start_collect()


def _process_entry() -> tuple[float | None, str]:
    """Linux pod process birth, including Python startup and imports.

    /proc starttime and CLOCK_BOOTTIME share the boot clock; translate to the
    monotonic clock used for the span. Never label module import as process birth.
    """
    try:
        fields = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
        birth = int(fields[19]) / os.sysconf("SC_CLK_TCK")
        age = time.clock_gettime(time.CLOCK_BOOTTIME) - birth
        return time.monotonic() - age, "Linux /proc/self/stat process starttime"
    except (OSError, ValueError, IndexError, AttributeError):
        return None, "process entry unavailable on this platform"


class JobEvidence:
    def __init__(self, out: Path, preparation: Any, *, generation: Any = None):
        self.process_entry, self.process_entry_source = _process_entry()
        self.out = Path(out)
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.preparation_started = time.monotonic()
        self.preparation_ended: float | None = None
        self.timing = PhaseTiming(clock=time.monotonic, generation=generation)
        self.preparation = preparation
        self.path = engine_log_path(str(preparation.root), preparation.recipe.engine)
        self.start: int | None = 0
        self.log_identity: tuple[int, int] | None = None
        self.endpoint = f"http://127.0.0.1:{preparation.recipe.start.port}"
        self.notes: list[str] = []
        self.before: int | None = None
        self.finished = False
        self.begin()

    def waiting(self) -> None:
        stamp(self.timing, "capacity_wait_started_at", first=True)

    def start_execution(self) -> None:
        if self.preparation_ended is None:
            self.preparation_ended = time.monotonic()
        stamp(self.timing, "job_started_at", first=True)

    def start_collect(self) -> None:
        if "job_started_at" in self.timing.stamps:
            stamp(self.timing, "job_ended_at", first=True)
        stamp(self.timing, "collection_started_at", first=True)

    def begin(self) -> None:
        """Reset the boundary after acquiring exclusive engine custody."""
        if "capacity_wait_started_at" in self.timing.stamps:
            stamp(self.timing, "claimed_at", first=True)
        stamp(self.timing, "preparation_started_at")
        self.notes.clear()
        self.log_identity = None
        try:
            info = Path(self.path).stat()
            self.start = info.st_size
            self.log_identity = (info.st_dev, info.st_ino)
        except FileNotFoundError:
            self.start = 0
        except OSError:
            self.start = None
            self.notes.append("engine log start offset unavailable")
        self.before = self._vram("before")

    def _vram(self, when: str) -> int | None:
        with suppress(Exception):
            from .backends.comfy_target import ComfyTarget, ComfyTargetClient
            stats = ComfyTargetClient(ComfyTarget(self.endpoint, "managed")).system_stats(timeout=1.0)
            free = stats["devices"][0]["vram_free"]
            if type(free) is int and free >= 0:
                return free
        self.notes.append(f"VRAM {when} unavailable: system stats request failed or contained no valid free VRAM")
        return None

    def finish(self) -> None:
        if self.finished:
            return
        if self.preparation_ended is None:
            self.preparation_ended = time.monotonic()
        self.start_collect()
        self.finished = True
        after = self._vram("after")
        declared = False
        with suppress(Exception):
            info = Path(self.path).stat()
            end = info.st_size
            if self.start is not None:
                restarted = (end < self.start or self.log_identity is not None
                             and self.log_identity != (info.st_dev, info.st_ino))
                start = 0 if restarted else self.start
                if restarted:
                    self.notes.append("engine log restarted during job")
                run_logs.declare(self.out, run_logs.LogDecl("engine", "engine", self.path, start=start, end=end))
                declared = True
        if not declared:
            self.notes.append("engine log declaration unavailable")
        stamp(self.timing, "collection_ended_at")
        if ("capacity_wait_started_at" in self.timing.stamps
                and "claimed_at" not in self.timing.stamps):
            self.notes.append("queue_wait unavailable: custody admission was interrupted before completion")
        block = self.timing.block()
        verify = self._preparation_seconds("verify_models", "verification_seconds", "verification_attempted")
        phases = {
            "verify_models": verify,
            "engine_start_or_reuse": self._preparation_seconds(
                "engine_start_or_reuse", "engine_start_seconds", "engine_start_attempted"),
            "queue_wait": self._block_seconds(block, "queue_wait", "capacity_wait"),
            "execute": self._block_seconds(block, "execute", "execution"),
            "collect": self._block_seconds(block, "collect", "collection"),
        }
        accounted = [phases[name] for name in ("verify_models", "engine_start_or_reuse", "queue_wait")]
        preparation_other = (None if any(value is None for value in accounted) else max(
            0.0, self.preparation_ended - self.preparation_started - sum(accounted)))
        reused = getattr(self.preparation, "engine_reused", None)
        if type(reused) is not bool:
            reused = False
            self.notes.append("engine_reused=false: no supervisor reuse decision was reached")
        record = {"schema": "astrid.job-run.v1", "tier1_cards": list(TIER1_CARDS),
                  "vram_free_before_bytes": self.before,
                  "vram_free_after_bytes": after, "model_verification_seconds": verify,
                  "started_at": self.started_at, "ended_at": datetime.now(timezone.utc).isoformat(),
                  "phases": phases, "engine_reused": reused,
                  # Preserve v1's fixed phase vocabulary. This disjoint remainder
                  # accounts for the formerly invisible receipt/source/env,
                  # canonical workflow, native alignment and evidence work.
                  "additional_phases": {"preparation_other": preparation_other},
                  "preparation_steps": dict(getattr(self.preparation, "phase_seconds", {})),
                  "phase_sources": {
                      "verify_models": "managed preparation verification_seconds; model_verification_seconds is an alias",
                      "engine_start_or_reuse": "sum of supervisor probe/start and readiness-health calls, including doctor preparation",
                      "queue_wait": "pod monotonic capacity_wait: local engine custody admission",
                      "execute": "pod monotonic execution: dispatch through producer result; includes ComfyUI prompt queue",
                      "collect": "pod monotonic collection: local finalization and evidence capture; excludes record serialization",
                      "preparation_other": "monotonic construction-to-dispatch (or failure), less queue_wait, verify_models and engine_start_or_reuse; preparation_steps are overlapping detail",
                  }, "notes": self.notes}
        with suppress(Exception):
            path = self.out / "job-run.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            whole = (None if self.process_entry is None else
                     max(0.0, time.monotonic() - self.process_entry))
            accounted = list(phases.values())
            phases["whole_job"] = whole
            # Keep unknown durations unknown, and expose overlap as a negative
            # remainder rather than silently hiding an accounting error.
            phases["unaccounted"] = (None if whole is None or any(v is None for v in accounted)
                                     else whole - sum(accounted))
            record["ended_at"] = datetime.now(timezone.utc).isoformat()
            record["phase_sources"].update({
                "whole_job": self.process_entry_source + "; through job-run record write boundary",
                "unaccounted": "whole_job minus sum of the other measured phases",
            })
            path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            run_logs.declare(self.out, run_logs.LogDecl("run", "run", str(path.resolve())))


    def _preparation_seconds(self, phase: str, field: str, attempted: str) -> float | None:
        value = getattr(self.preparation, field, 0.0)
        if value == 0 and not getattr(self.preparation, attempted, False):
            self.notes.append(f"{phase} not reached (or skipped); recorded 0 seconds")
        return self._seconds(phase, value)

    def _block_seconds(self, block: dict[str, Any], phase: str, source: str) -> float | None:
        value = block["phases"][source]
        if value is None:
            self.notes.append(f"{phase} not reached; recorded 0 seconds")
            return 0.0
        return self._seconds(phase, value["seconds"])

    def _seconds(self, phase: str, value: Any) -> float | None:
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            return float(value)
        self.notes.append(f"{phase} unavailable: no finite duration was recorded")
        return None
