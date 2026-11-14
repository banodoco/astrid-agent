"""Best-effort, attempt-scoped engine evidence for managed jobs."""
from __future__ import annotations

import json
import math
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrid.core._shared import run_logs
from astrid.core.execution.phase_timing import PhaseTiming

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


class JobEvidence:
    def __init__(self, out: Path, preparation: Any):
        self.out = Path(out)
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.timing = PhaseTiming()
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
        self.timing.stamp("capacity_wait_started_at", first=True)

    def start_execution(self) -> None:
        self.timing.stamp("job_started_at", first=True)

    def start_collect(self) -> None:
        if "job_started_at" in self.timing.stamps:
            self.timing.stamp("job_ended_at", first=True)
        self.timing.stamp("collection_started_at", first=True)

    def begin(self) -> None:
        """Reset the boundary after acquiring exclusive engine custody."""
        if "capacity_wait_started_at" in self.timing.stamps:
            self.timing.stamp("claimed_at", first=True)
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
        self.timing.stamp("collection_ended_at")
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
        reused = getattr(self.preparation, "engine_reused", None)
        if type(reused) is not bool:
            reused = False
            self.notes.append("engine_reused=false: no supervisor reuse decision was reached")
        record = {"schema": "astrid.job-run.v1", "vram_free_before_bytes": self.before,
                  "vram_free_after_bytes": after, "model_verification_seconds": verify,
                  "started_at": self.started_at, "ended_at": datetime.now(timezone.utc).isoformat(),
                  "phases": phases, "engine_reused": reused,
                  "phase_sources": {
                      "verify_models": "managed preparation verification_seconds; model_verification_seconds is an alias",
                      "engine_start_or_reuse": "sum of supervisor probe/start and readiness-health calls, including doctor preparation",
                      "queue_wait": "phase-timing.v1 capacity_wait: local engine custody admission",
                      "execute": "phase-timing.v1 execution: dispatch through producer result; includes ComfyUI prompt queue",
                      "collect": "phase-timing.v1 collection: local finalization and evidence capture; excludes record serialization",
                  }, "notes": self.notes}
        with suppress(Exception):
            path = self.out / "job-run.json"
            path.parent.mkdir(parents=True, exist_ok=True)
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
