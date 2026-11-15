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
from astrid.core.execution.phase_timing import (
    TIER1_CARDS,
    PhaseTiming,
    clock_id,
    current_pod_timing,
    pod_phase,
    pod_step,
    pod_timing_scope,
    stamp,
)

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


@contextmanager
def pod_log_phase(root: Path, phase: str, step_id: str):
    """Append detached log work before the output manifest seals the pod files.

    A boot-clock identity is required before extending the original job span.
    Collection-time engine status runs later and is reported separately.
    """
    if current_pod_timing() is not None:
        with pod_phase(phase, step_id):
            yield
        return
    record = None
    path = root / "job-run.json"
    with suppress(Exception):
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= 128 * 1024:
            value = json.loads(path.read_text(encoding="utf-8"))
            if (value.get("schema") == "astrid.job-run.v1" and value.get("pod_clock_id") == clock_id()
                    and value.get("pod_clock_id") is not None
                    and type(value.get("started_monotonic")) in (int, float)
                    and isinstance(value.get("top_level_phases"), dict)):
                record = value
    if record is None:
        yield
        return
    birth, _ = _process_entry()
    with pod_timing_scope() as timing:
        try:
            with pod_phase(phase, step_id):
                yield
        finally:
            with suppress(Exception):
                rows = timing.named_steps()
                top = record["top_level_phases"]
                if birth is not None and birth >= record.get("ended_monotonic", birth) and rows:
                    top[f"log_process_startup:{len(top)}"] = {
                        "phase": "log_process_startup", "step_id": step_id + ":process-startup",
                        "seconds": max(0.0, rows[0]["start"] - birth)}
                offset = len(record["named_phases"])
                for row in rows:
                    if row["parent"] is None:
                        top[f"{row['phase']}:{len(top)}"] = {
                            key: row[key] for key in ("phase", "step_id", "seconds")}
                    else:
                        row["parent"] += offset
                record["named_phases"].extend(rows)
                end = time.monotonic()
                record["ended_monotonic"] = end
                record["phases"]["whole_job"] = end - record["started_monotonic"]
                record["phases"]["unaccounted"] = (record["phases"]["whole_job"]
                    - sum(row["seconds"] for row in top.values()))
                record["ended_at"] = datetime.now(timezone.utc).isoformat()
                path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")


class JobEvidence:
    def __init__(self, out: Path, preparation: Any, *, generation: Any = None):
        self.process_entry, self.process_entry_source = _process_entry()
        self.out = Path(out)
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.timing = current_pod_timing() or PhaseTiming(clock=time.monotonic, generation=generation)
        self.named = current_pod_timing() is self.timing
        self._wait_step = self._execution_step = self._collection_step = None
        self.record: dict[str, Any] | None = None
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
        if self.named and self._wait_step is None:
            with suppress(Exception):
                self._wait_step = self.timing.start_step("queue_wait", "installation-lock")

    def start_execution(self) -> None:
        stamp(self.timing, "job_started_at", first=True)
        if self.named and self._execution_step is None:
            with suppress(Exception):
                self._execution_step = self.timing.start_step("execute", "execute")

    def start_collect(self) -> None:
        if "job_started_at" in self.timing.stamps:
            stamp(self.timing, "job_ended_at", first=True)
        stamp(self.timing, "collection_started_at", first=True)
        if self.named and self._collection_step is None:
            with suppress(Exception):
                self.timing.end_step(self._execution_step)
                self._collection_step = self.timing.start_step("result_writing", "producer-result-writing")

    def begin(self) -> None:
        """Reset the boundary after acquiring exclusive engine custody."""
        if "capacity_wait_started_at" in self.timing.stamps:
            stamp(self.timing, "claimed_at", first=True)
            with suppress(Exception):
                self.timing.end_step(self._wait_step)
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

    @pod_step("vram_stats", lambda self, when: "vram-" + when)
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
        with suppress(Exception):
            self.timing.end_step(self._collection_step)
        with pod_phase("evidence_collection", "job-evidence"):
            self._finish()
        self.refresh()

    def _finish(self) -> None:
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
                with pod_phase("log_declaration", "engine-log-declaration"):
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
        reused = getattr(self.preparation, "engine_reused", None)
        if type(reused) is not bool:
            reused = False
            self.notes.append("engine_reused=false: no supervisor reuse decision was reached")
        record = {"schema": "astrid.job-run.v1", "tier1_cards": list(TIER1_CARDS),
                  "vram_free_before_bytes": self.before,
                  "vram_free_after_bytes": after, "model_verification_seconds": verify,
                  "started_at": self.started_at, "ended_at": datetime.now(timezone.utc).isoformat(),
                  "phases": phases, "engine_reused": reused,
                  "phase_sources": {
                      "verify_models": "managed preparation verification_seconds; model_verification_seconds is an alias",
                      "engine_start_or_reuse": "sum of supervisor probe/start and readiness-health calls, including doctor preparation",
                      "queue_wait": "pod monotonic capacity_wait: local engine custody admission",
                      "execute": "pod monotonic execution: dispatch through producer result; includes ComfyUI prompt queue",
                      "collect": "pod monotonic collection: local finalization and evidence capture; excludes record serialization",
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
            self.record = record
            with pod_phase("evidence_writing", "job-run-write"):
                path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            with pod_phase("log_declaration", "run-log-declaration"):
                run_logs.declare(self.out, run_logs.LogDecl("run", "run", str(path.resolve())))
    def refresh(self) -> None:
        """Refresh additive timing after cleanup; never repeat probes or declarations.

        The final serialization is outside the recorded boundary (a record cannot
        contain the duration of its own final write). Its earlier write is timed.
        """
        if not self.named or self.record is None:
            return
        with suppress(Exception):
            rows = self.timing.named_steps()
            roots = [row for row in rows if row["parent"] is None]
            top = {f"{row['phase']}:{index}": {key: row[key] for key in ("phase", "step_id", "seconds")}
                   for index, row in enumerate(roots)}
            if self.process_entry is not None and roots:
                top["process_startup"] = {"phase": "process_startup", "step_id": "process-entry",
                                          "seconds": max(0.0, roots[0]["start"] - self.process_entry)}
            whole = None if self.process_entry is None else max(0.0, time.monotonic() - self.process_entry)
            self.record["named_phases"] = rows
            self.record["top_level_phases"] = top
            self.record["pod_clock_id"] = clock_id()
            self.record["started_monotonic"] = self.process_entry
            self.record["ended_monotonic"] = None if whole is None else self.process_entry + whole
            self.record["phases"]["whole_job"] = whole
            self.record["phases"]["unaccounted"] = (None if whole is None else
                whole - sum(row["seconds"] for row in top.values()))
            self.record["phase_sources"]["unaccounted"] = "whole_job minus top_level_phases; nested named_phases are inclusive"
            self.record["ended_at"] = datetime.now(timezone.utc).isoformat()
            (self.out / "job-run.json").write_text(
                json.dumps(self.record, sort_keys=True, indent=2) + "\n", encoding="utf-8")


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
