"""Fail-closed execution guards for reviewed local and provider runs.

These guards are deliberately engine-neutral.  They establish the resource
and time boundaries around an attempt; they do not claim GPU residency or
turn process persistence into warm weights.  In particular, the warm-reuse
expectation is a typed policy field and never depends on a listener port.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from runtime_protocol import platform


class ExecutionGuardError(RuntimeError):
    """Base error raised when an execution allowance cannot be established."""


class EvidenceCapError(ExecutionGuardError):
    """Generated attempt evidence cannot be proven within its declared cap."""

    def __init__(self, message: str, *, diagnostic: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        # Diagnostics are deliberately metadata-only.  They are attached to a
        # terminal failure before an ephemeral attempt root is removed.
        self.diagnostic = dict(diagnostic or {})


class ExecutionDeadlineError(ExecutionGuardError):
    """An attempt crossed its host-owned execution deadline."""


class WarmReuseExpectationError(ExecutionGuardError):
    """The warm-reuse policy is malformed."""


GENERATED_EVIDENCE_CAP_BYTES = 2 * 1024**3
DEFAULT_DEADLINE_SECONDS = 3600.0
# Written by the pack host after each task and read by ``astrid doctor``.
EVIDENCE_STATUS_NAME = "generic-host.evidence.json"


def write_evidence_status(path: str | Path, payload: Mapping[str, Any]) -> None:
    """Atomically publish the host's live evidence charge for doctor."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or any(part.is_symlink() for part in (target.parent, *target.parent.parents)):
        raise OSError("evidence observation path contains an alias")
    # Parallel host slots share this path. Each original writer retains its
    # unique file descriptor; snapshots cannot collide through a PID tempfile.
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(payload), stream, sort_keys=True)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def read_evidence_status(path: str | Path) -> dict[str, Any] | None:
    """Return the last published status, marked with whether its host is alive."""
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    pid = record.get("pid")
    alive = False
    if isinstance(pid, int) and pid > 0:
        try:
            if platform.process_identity(pid) is None:
                raise ProcessLookupError(pid)
            alive = True
        except PermissionError:
            alive = True
        except OSError:
            alive = False
    record["host_alive"] = alive
    return record


class EvidenceBudget:
    """Bound the generated evidence that attempts owned by this host hold on disk.

    The charge is live bytes, not a lifetime total. Each attempt root is one
    key, charged at its last observed size. A charge is released when the root
    no longer exists (ephemeral roots are deleted at cleanup and released
    explicitly there), so a long-lived host is never exhausted by cumulative
    success. A retained root (``keep_attempt``) keeps its charge while it exists.

    Nothing latches. An overrun fails the attempt that caused it, and every
    later admission is measured again against what is actually on disk.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._live: dict[str, int] = {}

    def _prune_locked(self) -> None:
        for key in [key for key in self._live if not Path(key).exists()]:
            del self._live[key]

    def live_observation(self) -> dict[str, int]:
        """Return one internally consistent measured charge snapshot."""
        with self._lock:
            self._prune_locked()
            return {"charged_bytes": sum(self._live.values()), "live_attempts": len(self._live)}

    @property
    def charged_bytes(self) -> int:
        with self._lock:
            self._prune_locked()
            return sum(self._live.values())

    @property
    def live_attempts(self) -> int:
        with self._lock:
            self._prune_locked()
            return len(self._live)

    def release(self, key: str) -> None:
        with self._lock:
            self._live.pop(key, None)

    def assert_available(self, cap_bytes: int) -> None:
        """Refuse admission only while roots still on disk exceed the cap."""
        with self._lock:
            self._prune_locked()
            total = sum(self._live.values())
            if total > cap_bytes:
                raise EvidenceCapError(
                    f"generated evidence held on disk by this host is {total} bytes, "
                    f"over the {cap_bytes}-byte cap; remove retained attempt roots "
                    "(keep_attempt) or wait for cleanup. No host restart is needed.",
                    diagnostic={
                        "category": "live_budget_exceeded",
                        "live_observed_bytes": total,
                        "cap_bytes": int(cap_bytes),
                        "live_attempts": len(self._live),
                    },
                )

    def account(self, key: str, observed_bytes: int, cap_bytes: int) -> dict[str, int]:
        observed = int(observed_bytes)
        cap = int(cap_bytes)
        with self._lock:
            self._prune_locked()
            previous = self._live.get(key, 0)
            others = sum(size for other, size in self._live.items() if other != key)
            # Record the observation first: these bytes are on disk whether or
            # not this attempt is allowed to keep them. Cleanup releases them.
            self._live[key] = observed
            if observed > cap:
                raise EvidenceCapError(
                    f"this task generated {observed} bytes of evidence, which exceeds cap "
                    f"{cap} bytes. Render a shorter range or use a review render (640x360) "
                    "frames, then re-run. Other tasks are unaffected.",
                    diagnostic={
                        "category": "attempt_cap_exceeded",
                        "attempt_observed_bytes": observed,
                        "cap_bytes": cap,
                    },
                )
            if others + observed > cap:
                raise EvidenceCapError(
                    f"generated evidence on disk would reach {others + observed} bytes, "
                    f"over the {cap}-byte cap: {others} bytes are held by "
                    f"{len(self._live) - 1} other attempt root(s). Remove retained "
                    "attempt roots (keep_attempt) and re-run. No host restart is needed.",
                    diagnostic={
                        "category": "live_budget_exceeded",
                        "attempt_observed_bytes": observed,
                        "other_live_bytes": others,
                        "cap_bytes": cap,
                    },
                )
            return {
                "attempt_delta_bytes": observed - previous,
                "run_observed_bytes": others + observed,
            }


@dataclass(frozen=True, slots=True)
class ExecutionGuardPolicy:
    """Bound one attempt with explicit evidence and deadline limits."""

    evidence_cap_bytes: int = GENERATED_EVIDENCE_CAP_BYTES
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS
    authority_window_seconds: int = 300
    warm_reuse_expected: bool = False
    evidence_budget: EvidenceBudget = field(
        default_factory=EvidenceBudget,
        compare=False,
        repr=False,
    )
    # Input digests keyed by file identity and change stamps. The live guard
    # samples the attempt many times per second; an unchanged input must not
    # be re-read and re-hashed on every sample.
    _digest_cache: dict[tuple[str, int, int, int, int], str] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.evidence_cap_bytes <= 0:
            raise ValueError("evidence_cap_bytes must be positive")
        if self.deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive")
        if self.authority_window_seconds <= 0:
            raise ValueError("authority_window_seconds must be positive")
        if type(self.warm_reuse_expected) is not bool:
            raise WarmReuseExpectationError("warm_reuse_expected must be a boolean")

    def immutable_input_baseline(self, root: str | Path) -> dict[str, tuple[int, str]]:
        """Capture materialized input bytes before the child is launched."""
        directory = Path(root)
        if not directory.exists():
            return {}
        baseline: dict[str, tuple[int, str]] = {}
        for path in directory.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                size = os.fstat(stream.fileno()).st_size
            baseline[str(path.resolve())] = (size, digest)
        return baseline

    def _cached_digest(self, path: Path, stat: os.stat_result) -> str:
        """Hash an input once per (inode, size, mtime, ctime); reuse it after."""
        key = (str(path), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns), int(stat.st_ctime_ns))
        digest = self._digest_cache.get(key)
        if digest is None:
            if len(self._digest_cache) >= 4096:
                self._digest_cache.clear()
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            self._digest_cache[key] = digest
        return digest

    def evidence_bytes(
        self,
        root: str | Path,
        *,
        immutable_inputs: Mapping[str, tuple[int, str]] | None = None,
    ) -> int:
        """Count generated bytes without following symlink escapes.

        Bytes present in the immutable input baseline are excluded; growth of
        an input file after the baseline is still charged as generated data.
        """
        return int(
            self.evidence_measurement(
                root,
                immutable_inputs=immutable_inputs,
            )["observed_bytes"]
        )

    def evidence_measurement(
        self,
        root: str | Path,
        *,
        immutable_inputs: Mapping[str, tuple[int, str]] | None = None,
        path_sample_limit: int = 64,
        excluded_roots: tuple[Path, ...] = (),
    ) -> dict[str, Any]:
        """Return bounded, metadata-only evidence accounting for one sample.

        The exact content of generated files is never read into the receipt.
        A small largest-file sample plus top-level path classes is enough to
        distinguish a real cap breach from a scan failure after the root is
        deleted.
        """
        directory = Path(root)
        if not directory.exists():
            raise EvidenceCapError(
                f"evidence root does not exist: {directory}",
                diagnostic={"category": "root_missing"},
            )
        if path_sample_limit < 0:
            raise ValueError("path_sample_limit must not be negative")
        path_sample_limit = min(int(path_sample_limit), 64)
        baseline = immutable_inputs or {}
        total = 0
        generated_files = 0
        immutable_files = 0
        immutable_bytes = 0
        vanished_files = 0
        classes: dict[str, dict[str, int]] = {}
        # Keep only the largest paths while scanning; a render may create
        # millions of short-lived frame/evidence files.
        entries: list[tuple[int, str, str]] = []
        # rglob() never descends through a symlinked directory, so every file
        # below the root resolves to the resolved root plus its relative path;
        # one resolve() per sample instead of one realpath per file.
        resolved_root = directory.resolve()
        excluded = tuple(path.resolve() for path in excluded_roots)
        try:
            for path in directory.rglob("*"):
                try:
                    if excluded and any(
                        (resolved_root / path.relative_to(directory)).is_relative_to(root)
                        for root in excluded
                    ):
                        continue
                    if path.is_symlink() or not path.is_file():
                        continue
                    stat = path.stat()
                    size = int(stat.st_size)
                    relative = path.relative_to(directory).as_posix()
                    path_class = relative.split("/", 1)[0] if relative else "."
                    identity = baseline.get(str(resolved_root / relative)) if baseline else None
                    unchanged = False
                    if identity is not None:
                        expected_size, expected_digest = identity
                        unchanged = (
                            size == expected_size
                            and self._cached_digest(path, stat) == expected_digest
                        )
                    if unchanged:
                        immutable_files += 1
                        immutable_bytes += size
                        continue
                    generated_files += 1
                    total += size
                    bucket = classes.setdefault(path_class, {"files": 0, "bytes": 0})
                    bucket["files"] += 1
                    bucket["bytes"] += size
                    if path_sample_limit:
                        candidate = (
                            size,
                            relative,
                            "modified_input" if identity is not None else "generated",
                        )
                        if len(entries) < path_sample_limit:
                            heapq.heappush(entries, candidate)
                        elif candidate > entries[0]:
                            heapq.heapreplace(entries, candidate)
                except FileNotFoundError:
                    # Renderers may delete completed frame files while the
                    # evidence guard is taking its point-in-time sample.
                    vanished_files += 1
                    continue
        except FileNotFoundError:
            # The render service may remove a private staging directory while
            # rglob() is advancing between directory entries. Treat that
            # transient outer-walk race the same as an entry disappearing.
            vanished_files += 1
        except OSError as exc:
            raise EvidenceCapError(
                f"cannot measure generated evidence: {directory}",
                diagnostic={"category": "scan_error", "error_type": type(exc).__name__},
            ) from exc
        entries.sort(key=lambda item: (-item[0], item[1], item[2]))
        baseline_digest = hashlib.sha256(
            "\n".join(
                f"{path}\0{size}\0{digest}"
                for path, (size, digest) in sorted(baseline.items())
            ).encode("utf-8")
        ).hexdigest()
        return {
            "observed_bytes": total,
            "generated_file_count": generated_files,
            "immutable_file_count": immutable_files,
            "immutable_input_bytes": immutable_bytes,
            "immutable_input_manifest_digest": baseline_digest,
            "vanished_file_count": vanished_files,
            "path_classes": {
                key: classes[key] for key in sorted(classes)
            },
            "largest_paths": [
                {"path": path, "bytes": size, "classification": classification}
                for size, path, classification in entries[:path_sample_limit]
            ],
            "largest_paths_truncated": generated_files > path_sample_limit,
        }

    def assert_evidence_cap(
        self,
        root: str | Path,
        *,
        immutable_inputs: Mapping[str, tuple[int, str]] | None = None,
        cap_bytes: int | None = None,
        excluded_roots: tuple[Path, ...] = (),
    ) -> dict[str, Any]:
        """Reject one attempt's generated evidence beyond its cap.

        Render media/workspaces may be excluded from diagnostic evidence:
        their capacity is protected by the live free-volume guard instead.
        """
        cap = self.evidence_cap_bytes if cap_bytes is None else int(cap_bytes)
        measurement = self.evidence_measurement(
            root, immutable_inputs=immutable_inputs, excluded_roots=excluded_roots,
        )
        observed_bytes = int(measurement["observed_bytes"])
        try:
            accounting = self.evidence_budget.account(
                str(Path(root).resolve()),
                observed_bytes,
                cap,
            )
        except EvidenceCapError as exc:
            exc.diagnostic.update(measurement)
            raise
        return {
            "root": str(Path(root)),
            **measurement,
            "cap_bytes": cap,
            **accounting,
        }

    def assert_budget_available(self) -> None:
        self.evidence_budget.assert_available(self.evidence_cap_bytes)

    def deadline_from_now(self) -> float:
        return time.monotonic() + self.deadline_seconds

    @staticmethod
    def deadline_expired(deadline: float) -> bool:
        return time.monotonic() >= deadline

    def assert_deadline(self, deadline: float) -> None:
        if self.deadline_expired(deadline):
            raise ExecutionDeadlineError(
                f"execution exceeded {self.deadline_seconds:g}s deadline"
            )

    def warm_expectation(self, *, listener_port: int | None = None) -> dict[str, Any]:
        """Return warm policy evidence without requiring a listener endpoint."""
        if listener_port is not None and not 1 <= int(listener_port) <= 65535:
            raise WarmReuseExpectationError("listener_port must be in the TCP port range")
        return {
            "warm_reuse_expected": self.warm_reuse_expected,
            "listener_port": listener_port,
            "port_independent": True,
        }
