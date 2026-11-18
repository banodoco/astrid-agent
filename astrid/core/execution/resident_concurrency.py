"""Token-addressed dispatch for explicit-context resident pack actions.

ManagedToolSession still owns admission and lifetime. A single pipe reader
only delivers terminal frames to the invocation that already owns that token.
"""
from __future__ import annotations

import threading
import time
from types import MappingProxyType, SimpleNamespace

from .managed_tool_session import StaleAdmissionError
from .resident_sessions import (
    PROTOCOL, ResidentPackAdapter, ResidentProtocolError, _success_returncode,
    encode_frame, resident_capacity, token_identity,
)


class ConcurrentResidentAdapter(ResidentPackAdapter):
    concurrent = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._jobs_lock = threading.Lock()
        self._jobs = {}
        self._finished = {}
        self._job_adapters = {}
        self._read_error = None
        self._reader_thread = None
        self._release_lock = threading.RLock()

    @property
    def has_jobs(self):
        with self._jobs_lock:
            return bool(self._jobs or self._finished)

    @property
    def at_capacity(self):
        with self._jobs_lock:
            return len(self._jobs) + len(self._finished) >= resident_capacity(self.record.definition.metadata)

    def bind_admission(self, token, adapter):
        with self._jobs_lock:
            self._job_adapters[token.token_id] = adapter

    def has_completed(self, token):
        with self._jobs_lock:
            return self._finished.get(token.token_id) == token_identity(token)

    def has_token(self, token):
        with self._jobs_lock:
            return token.token_id in self._jobs or token.token_id in self._finished

    def start(self):
        super().start()
        self._reader_thread = threading.Thread(target=self._receive, name="resident-results", daemon=True)
        self._reader_thread.start()

    def _receive(self):
        try:
            while not self._fenced.is_set():
                frame = self._read(time.monotonic() + 24 * 60 * 60, self._wire_guard)
                if (set(frame) != {"protocol", "kind", "identity", "job", "result"}
                        or frame["kind"] != "result" or not isinstance(frame["identity"], dict)):
                    raise ResidentProtocolError("resident terminal frame is invalid")
                with self._jobs_lock:
                    pending = self._jobs.get(frame["identity"].get("token_id"))
                    if (pending is None or pending["identity"] != frame["identity"]
                            or pending["job"] != frame["job"] or pending["frame"] is not None):
                        raise ResidentProtocolError("resident terminal frame belongs to another admission")
                    pending["frame"] = frame
                    pending["ready"].set()
        except BaseException as exc:  # noqa: BLE001 - loss of the one reader fences every waiter
            with self._jobs_lock:
                self._read_error = exc
                for pending in self._jobs.values():
                    pending["ready"].set()

    def invoke(self, token, *, request, admission, attempt, cancelled, storage_guard):
        from . import generic_host as core

        if self.host.managed_tool_session.adapter_for(token) is not self:
            raise StaleAdmissionError("resident adapter is not the admission owner")
        identity = token_identity(token)
        job = {key: admission.get(key) for key in ("task_id", "attempt_id", "fence", "runtime_epoch")}
        if (not job["task_id"] or not job["attempt_id"] or type(job["fence"]) is not int
                or job["runtime_epoch"] != self.binding.runtime_epoch
                or token.invocation_id != f"{job['task_id']}:{job['attempt_id']}:{job['fence']}"):
            raise StaleAdmissionError("resident request differs from its admitted attempt")
        frame = {"protocol": PROTOCOL, "kind": "job", "identity": identity, "job": job,
                 "request": dict(request), "attempt": str(attempt),
                 "context": {key: admission[key] for key in ("align", "entry", "execution_identity", "managed_engine_preparation")
                             if key in admission}}
        encode_frame(frame)
        pending = {"identity": identity, "job": job, "frame": None, "ready": threading.Event()}
        try:
            self.assert_current()
            with self._jobs_lock:
                if len(self._jobs) >= resident_capacity(self.record.definition.metadata):
                    raise ResidentProtocolError("resident dispatch capacity exceeded")
                self._jobs[token.token_id] = pending
            deadline, checked_at, cancel_sent = time.monotonic() + 24 * 60 * 60, 0.0, False
            self._write(frame, deadline)
            while True:
                self._wire_guard()
                if self._read_error is not None:
                    raise ResidentProtocolError("resident result channel failed") from self._read_error
                if cancelled is not None and cancelled() and not cancel_sent:
                    self._request_cancel(identity)
                    cancel_sent = True
                    deadline = min(deadline, time.monotonic() + 5.0)
                if time.monotonic() >= checked_at:
                    self.assert_current()
                    storage_guard()
                    checked_at = time.monotonic() + 0.5
                if pending["ready"].wait(timeout=0.05):
                    break
                if time.monotonic() >= deadline:
                    raise ResidentProtocolError("resident job did not drain before its deadline")
            self.assert_current()
            result = pending["frame"]
            if result is None or not isinstance(result["result"], dict):
                raise ResidentProtocolError("resident result has no terminal evidence")
            payload = result["result"]
            with self._jobs_lock:
                self._jobs.pop(token.token_id)
                self._finished[token.token_id] = identity
            if cancel_sent or payload.get("cancelled") is True:
                raise core.HostCancelled("resident action cancelled")
            if payload.get("ok") is not True:
                raise core.HostError(str(payload.get("error") or "resident action failed"))
            code = _success_returncode(payload, self.record.id)
        except (core.HostCancelled, core.HostError):
            if not self.has_completed(token):
                self.fence(reason="resident_dispatch_failed")
                self.release(reason="resident_dispatch_failed")
            raise
        except BaseException:
            self.fence(reason="resident_protocol_failed")
            self.release(reason="resident_protocol_failed")
            raise
        self.host.managed_tool_session.adapter_for(token)
        child_payload = dict(payload["payload"])
        child_payload.update(process_id=self.process.pid, returncode=code,
                             resident_session={"protocol": PROTOCOL, "session_id": self.binding.session_id})
        return SimpleNamespace(ok=True, returncode=code, outputs=payload["outputs"], payload=child_payload,
                               process_id=self.process.pid, stdout=payload["stdout"], stderr=payload["stderr"],
                               resident_evidence=MappingProxyType(identity))

    def finish_admission(self, token):
        with self._jobs_lock:
            if self._finished.pop(token.token_id, None) != token_identity(token):
                raise StaleAdmissionError("resident completion identity differs")
            self._job_adapters.pop(token.token_id, None)

    def cancel_admission(self, *, token, reason):
        if not self.has_completed(token):
            return {"ok": False, "reason": "resident job has not drained"}
        adapter = self._job_adapters.get(token.token_id)
        if adapter is not None and adapter.cancel(reason=reason).get("ok") is not True:
            return {"ok": False, "reason": "job-owned cleanup is unproved"}
        self.finish_admission(token)
        return {"ok": True, "cancelled": True, "reason": reason}

    def release(self, *, reason):
        with self._release_lock:
            result = super().release(reason=reason)
            if self._reader_thread is not None and self._reader_thread is not threading.current_thread():
                self._reader_thread.join(timeout=1.0)
            return result
