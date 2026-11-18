"""Explicit job contexts for the resident worker's bounded dispatch interval.

The protocol reader is the process supervisor. Jobs never change cwd, signals,
environment or stream descriptors. Ordinary resident actions keep the serial
worker, whose compatibility behavior is unchanged.
"""
from __future__ import annotations

import io
import os
import signal
import sys
import threading
from collections import deque
from contextvars import ContextVar
from types import MappingProxyType

from .resident_sessions import PROTOCOL, ResidentJobContext, ResidentProtocolError, _JOB

_OUTPUT: ContextVar[object | None] = ContextVar("resident_output", default=None)
_CLEANUPS: list = []
_CLEANUP_LOCK = threading.Lock()


def on_session_close(callback):
    """Register a pack-owned transport/cache closer with the worker supervisor."""
    with _CLEANUP_LOCK:
        _CLEANUPS.append(callback)


class JobCancellation:
    def __init__(self):
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.callbacks = set()

    def set(self):
        with self.lock:
            self.event.set()
            callbacks = tuple(self.callbacks)
        for callback in callbacks:
            callback()

    def subscribe(self, callback):
        with self.lock:
            self.callbacks.add(callback)
            cancelled = self.event.is_set()
        if cancelled:
            callback()
        def unsubscribe():
            with self.lock:
                self.callbacks.discard(callback)
        return unsubscribe


class _JobStream(io.TextIOBase):
    def __init__(self, name, fallback):
        self.name, self.fallback = name, fallback

    @property
    def encoding(self):
        return "utf-8"

    def writable(self):
        return True

    def fileno(self):
        return self.fallback.fileno()

    def write(self, value):
        target = _OUTPUT.get()
        if target is None:
            return self.fallback.write(value)
        capture, lock = target
        with lock:
            capture._receive(self.name, value.encode("utf-8", errors="replace"))
        return len(value)

    def flush(self):
        if _OUTPUT.get() is None:
            self.fallback.flush()


def serve_context_jobs(init, *, send, captured, baseline_environment, secret_values):
    from .generic_host import GenericPackHost, _TaskOutputCapture
    from .generic_host_worker import run_payload
    from .network_policy import resident_job_policy
    from .resident_sessions import resident_capacity
    from .resident_worker import _job_frame, _read

    binding, generation = init["binding"], init["generation"]
    definition, admission, policy = init["definition"], init["admission"], init["policy"]
    capacity = resident_capacity(definition["metadata"])
    jobs, lock, completed = {}, threading.Lock(), deque(maxlen=64)
    stdout, stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _JobStream("stdout", stdout), _JobStream("stderr", stderr)

    def execute(parts, settings, cancellation):
        identity, job, request, attempt, output = parts
        environment = dict(baseline_environment)
        environment["ASTRID_PROGRESS_PATH"] = str(attempt / ".astrid-progress.json")
        if settings.get("align"):
            environment["ASTRID_ALIGN"] = settings["align"]
        if settings.get("entry"):
            environment["ASTRID_GENERATION_ENTRY"] = settings["entry"]
        context = ResidentJobContext(
            job["task_id"], job["attempt_id"], job["fence"], job["runtime_epoch"],
            attempt, output, cancellation.event.is_set, tuple(binding),
            admission["capability_digest"], MappingProxyType(settings),
            MappingProxyType(environment), cancellation.subscribe,
        )
        token = _JOB.set(context)
        capture = _TaskOutputCapture(attempt, secret_values, GenericPackHost._scrub_secret_text)
        capture._available.update(stdout=True, stderr=True)
        output_token = _OUTPUT.set((capture, threading.Lock()))
        try:
            with resident_job_policy(policy, attempt / "network-evidence.json", admission=job):
                result = run_payload({"capability_kind": "executor", "capability_id": definition["id"],
                                      "definition": definition, "admission": {**admission, **job, **settings},
                                      "request": request}, captured=captured)
            if cancellation.event.is_set():
                result = {"ok": False, "error": "resident job was cancelled", "cancelled": True}
        except BaseException as exc:  # noqa: BLE001 - a job cannot exit its supervisor
            import traceback
            traceback.print_exc()
            result = {"ok": False, "error": GenericPackHost._scrub_secret_text(
                f"{type(exc).__name__}: {exc}", secret_values)[-3500:],
                "cancelled": cancellation.event.is_set()}
        finally:
            with _OUTPUT.get()[1]:
                capture._finalize("stdout")
                capture._finalize("stderr")
                capture.persist(force=True)
            result["stdout"], result["stderr"] = capture.tails()
            cancellation.set()
            _OUTPUT.reset(output_token)
            _JOB.reset(token)
        with lock:
            jobs.pop(identity["token_id"], None)
            completed.append(identity)
        try:
            send({"protocol": PROTOCOL, "kind": "result", "identity": identity, "job": job, "result": result})
        except BaseException:  # noqa: BLE001 - a partial result is fatal and must not replay
            os._exit(70)

    def stop(_signum, _frame):
        raise SystemExit(0)

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        while True:
            frame = _read(sys.stdin.buffer)
            if frame is None:
                return 0 if not jobs else 70
            if frame.get("kind") == "cancel":
                if set(frame) != {"protocol", "kind", "identity"}:
                    raise ResidentProtocolError("invalid resident cancellation")
                identity = frame["identity"]
                with lock:
                    active = jobs.get(identity.get("token_id")) if isinstance(identity, dict) else None
                    if active is None:
                        if identity in completed:
                            continue  # Completion won the race; never cancel another job.
                        raise ResidentProtocolError("resident cancellation is stale")
                    if active[0] != identity:
                        raise ResidentProtocolError("resident cancellation belongs to another job")
                active[1].set()
                continue
            if set(frame) != {"protocol", "kind", "identity", "job", "request", "attempt", "context"}:
                raise ResidentProtocolError("invalid explicit resident job frame")
            settings = frame.pop("context")
            if (not isinstance(settings, dict) or set(settings) - {"align", "entry", "execution_identity", "managed_engine_preparation"}
                    or settings.get("align", "check") not in {"align", "check", "trust"}
                    or "entry" in settings and not isinstance(settings["entry"], str)
                    or "execution_identity" in settings and not isinstance(settings["execution_identity"], str)
                    or "managed_engine_preparation" in settings and not isinstance(settings["managed_engine_preparation"], dict)):
                raise ResidentProtocolError("invalid resident job settings")
            parts = _job_frame(frame, binding, generation)
            identity = parts[0]
            cancellation = JobCancellation()
            thread = threading.Thread(target=execute, args=(parts, settings, cancellation),
                                      name="astrid-resident-action", daemon=True)
            with lock:
                if len(jobs) >= capacity or identity["token_id"] in jobs or identity in completed:
                    raise ResidentProtocolError("resident capacity or single-dispatch bound exceeded")
                jobs[identity["token_id"]] = (identity, cancellation, thread)
            thread.start()
    finally:
        with lock:
            pending = tuple(jobs.values())
        for _identity, cancellation, thread in pending:
            cancellation.set()
        for _identity, cancellation, thread in pending:
            thread.join(timeout=0.5)
        if not any(thread.is_alive() for _, _, thread in pending):
            with _CLEANUP_LOCK:
                callbacks, _CLEANUPS[:] = tuple(_CLEANUPS), []
            for callback in reversed(callbacks):
                callback()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        sys.stdout, sys.stderr = stdout, stderr
