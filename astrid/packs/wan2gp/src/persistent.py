"""Fixture-only persistent-session protocol for the Wan2GP pack.

This CPU substitute is retained for focused protocol tests only.  The
production ``wan2gp.generate_video`` route is host-mediated and must use the
W2.1 child-owned native ``shared.api`` session; no production executor imports
this module.

This module is a CPU-testable ownership seam, not a claim that the pinned
Wan2GP checkout supports persistence.  A native adapter must explicitly expose
``rebind_output_dir`` and ``observe_output_dir``; this module never guesses a
native attribute or mutates a config field and calls that a rebind.  The
fixture adapter used by the CPU lane implements the same protocol.

The owner uses ``ManagedToolSession`` so every task admission is fenced to one
session incarnation and every replacement is release-verified.  Runtime task
settlement remains outside this pack-level owner.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from astrid.pack_api import CapabilityDescriptor, ManagedToolSession, SessionBinding, StaleAdmissionError, UncertainCancellation

from .compiler import resident_key


class PersistentSessionError(RuntimeError):
    """A persistent owner cannot safely admit or settle a task."""


class PersistentSessionUncertain(PersistentSessionError):
    """The owner cannot prove that the old session is gone or fenced."""


@dataclass(frozen=True)
class PersistentRunResult:
    status: str
    generated_files: tuple[str, ...]
    errors: tuple[str, ...]
    resident_key: str
    generation: int
    reused: bool


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _has_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) or (
        isinstance(value, (list, tuple)) and bool(value)
    )


def _require_residency_attestation(settings: Mapping[str, Any]) -> None:
    """Reject a reuse admission whose model bytes are identified only by name."""
    if "model" in settings and not _has_digest(
        settings.get("model_artifact_digest") or settings.get("model_artifact_digests")
    ):
        raise PersistentSessionError(
            "persistent residency requires a content digest for the model artifact"
        )
    if any(key in settings for key in ("vae", "text_encoder")) and not _has_digest(
        settings.get("vae_digest") or settings.get("text_encoder_digest")
    ):
        raise PersistentSessionError(
            "persistent residency requires content digests for auxiliary artifacts"
        )
    if any(key in settings for key in ("loras", "activated_loras", "additional_loras")) and not _has_digest(
        settings.get("lora_artifact_digests") or settings.get("lora_digests")
    ):
        raise PersistentSessionError(
            "persistent residency requires content digests for LoRA artifacts"
        )


class _SessionAdapter:
    """Bridge an explicit engine session protocol to ManagedToolSession."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.active_job: Any | None = None
        self._last_cancel_result: Mapping[str, Any] | None = None

    def observe(self, *, binding: SessionBinding) -> dict[str, Any]:
        del binding
        observer = getattr(self.session, "observe_output_dir", None)
        if not callable(observer):
            return {"ok": False, "reason": "output observation is not supported"}
        observed = observer()
        return {"ok": bool(observed), "observed": observed}

    def fence(self, *, reason: str) -> dict[str, Any]:
        del reason
        fence = getattr(self.session, "fence", None)
        if callable(fence):
            result = fence()
            return result if isinstance(result, Mapping) else {"ok": bool(result)}
        cancel = getattr(self.active_job, "cancel", None)
        if callable(cancel):
            result = cancel()
            return result if isinstance(result, Mapping) else {"ok": True}
        return {"ok": True, "fenced": True}

    def cancel(self, *, reason: str) -> dict[str, Any]:
        del reason
        if self._last_cancel_result is not None:
            return dict(self._last_cancel_result)
        cancel = getattr(self.active_job, "cancel", None)
        if not callable(cancel):
            return {"ok": False, "reason": "job cancellation is not supported"}
        result = cancel()
        normalized = (
            dict(result)
            if isinstance(result, Mapping)
            else {"ok": result is True, "done": result is True}
        )
        self._last_cancel_result = normalized
        return normalized

    def release(self, *, reason: str) -> dict[str, Any]:
        del reason
        close = getattr(self.session, "close", None)
        observer = getattr(self.session, "observe_closed", None)
        if not callable(close) or not callable(observer):
            return {"ok": False, "reason": "closed-state observation is not supported"}
        close()
        return {"ok": bool(observer())}


class PersistentWan2GPSession:
    """Own one serial, output-rebindable session across CPU test tasks.

    ``session_factory`` receives the resident key and must return an object
    implementing the explicit substitute/native protocol:

    * ``rebind_output_dir(path)``
    * ``observe_output_dir()`` returning the currently bound path
    * ``submit_task(settings)`` returning an object with ``result(timeout=)``
    * ``close()`` and ``observe_closed()``

    A missing operation is a structured unsupported seam, never an inferred
    implementation.  The actual Wan2GP driver intentionally does not use this
    class until its native checkout proves these operations.
    """

    CAPABILITY = CapabilityDescriptor(
        capability_id="wan2gp-persistent-cpu-substitute",
        residency_support="observable_releasable",
        cancellation_strength="fenced",
        # The substitute exercises the protocol; native Wan2GP warm reuse is
        # not accepted until the P5 GPU gate proves the actual seam.
        warm_reuse_expected=False,
    )

    def __init__(
        self,
        session_factory: Callable[[str], Any],
        *,
        runner_kind: str = "wan2gp-cpu-substitute",
        engine_identity: str = "wan2gp-cpu-substitute",
        warmth_profile: str = "cpu-substitute",
    ) -> None:
        self._factory = session_factory
        self._runner_kind = runner_kind
        self._engine_identity = engine_identity
        self._warmth_profile = warmth_profile
        self._managed = ManagedToolSession(manager_id="wan2gp-persistent-owner")
        self._lock = threading.RLock()
        self._admission_guard = threading.Lock()
        self._session: Any | None = None
        self._adapter: _SessionAdapter | None = None
        self._binding: SessionBinding | None = None
        self._resident: str | None = None
        self._uncertain = False
        self._closed = False
        self._in_flight = False

    @property
    def resident_key(self) -> str | None:
        return self._resident

    @property
    def generation(self) -> int:
        return self._managed.generation

    @property
    def warm(self) -> bool:
        return self._session is not None and not self._uncertain and not self._closed

    def _require_protocol(self, session: Any) -> None:
        required = (
            "rebind_output_dir",
            "observe_output_dir",
            "submit_task",
            "close",
            "observe_closed",
        )
        missing = [name for name in required if not callable(getattr(session, name, None))]
        if missing:
            raise PersistentSessionError(
                "persistent session protocol unsupported; missing explicit operations: "
                + ", ".join(missing)
            )

    def _open(self, key: str) -> bool:
        session = self._factory(key)
        try:
            self._require_protocol(session)
        except PersistentSessionError:
            close = getattr(session, "close", None)
            observer = getattr(session, "observe_closed", None)
            if callable(close):
                try:
                    close()
                    if callable(observer) and not observer():
                        raise PersistentSessionUncertain(
                            "unsupported persistent session could not prove cleanup"
                        )
                except PersistentSessionUncertain:
                    raise
                except Exception as exc:
                    raise PersistentSessionUncertain(
                        "unsupported persistent session cleanup failed"
                    ) from exc
            raise
        adapter = _SessionAdapter(session)
        binding = SessionBinding(
            session_id=uuid.uuid4().hex,
            runtime_instance_id=uuid.uuid4().hex,
            process_birth_id=uuid.uuid4().hex,
            endpoint="inproc://wan2gp-persistent-cpu-substitute",
            source_digest=self._engine_identity,
            config_digest=key,
            execution_identity=key,
        )
        self._managed.open(capability=self.CAPABILITY, binding=binding, adapter=adapter)
        self._session = session
        self._adapter = adapter
        self._binding = binding
        self._resident = key
        return False

    def _retire(self, reason: str) -> None:
        try:
            self._managed.release(reason=reason)
        except Exception as exc:
            self._uncertain = True
            raise PersistentSessionUncertain(
                f"persistent session release was not verified: {reason}"
            ) from exc
        self._session = None
        self._adapter = None
        self._binding = None
        self._resident = None

    def _ensure_open(self, key: str) -> bool:
        if self._uncertain:
            raise PersistentSessionUncertain("persistent session is fenced pending cleanup proof")
        if self._closed:
            raise PersistentSessionError("persistent session owner is closed")
        if self._session is None:
            return self._open(key)
        if self._resident == key:
            if self._binding is None:
                raise PersistentSessionUncertain("resident session has no binding evidence")
            try:
                self._managed.observe(self._binding)
            except Exception as exc:
                try:
                    self._retire("observation_failed")
                except PersistentSessionUncertain as cleanup_exc:
                    raise cleanup_exc from exc
                raise PersistentSessionUncertain(
                    "resident session observation failed; session was retired"
                ) from exc
            return True
        self._retire("resident_key_changed")
        return self._open(key)

    def _cancel_admission(self, token: Any) -> None:
        """Require a positive native cancellation-complete observation."""
        assert self._adapter is not None
        evidence = self._adapter.cancel(reason="cancelled")
        if evidence.get("ok") is not True or evidence.get("done") is not True:
            try:
                self._managed.cancel(token, outcome="uncertain")
            except UncertainCancellation:
                pass
            self._retire("cancellation_uncertain")
            raise PersistentSessionUncertain(
                "native cancellation did not prove job completion/absence"
            )
        self._managed.cancel(token, outcome="confirmed")
        self._retire("cancelled")

    def _wait_for_job(
        self,
        job: Any,
        *,
        timeout: float | None,
        cancel: Callable[[], bool] | None,
        token: Any,
    ) -> Any | None:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            wait_for = 0.05
            if deadline is not None:
                wait_for = min(wait_for, max(0.0, deadline - time.monotonic()))
            try:
                result = job.result(timeout=wait_for)
            except TimeoutError:
                if cancel is not None and cancel():
                    self._cancel_admission(token)
                    return None
                if deadline is not None and time.monotonic() >= deadline:
                    raise
                continue
            if cancel is not None and cancel():
                self._cancel_admission(token)
                return None
            return result

    def run(
        self,
        settings: dict[str, Any],
        *,
        attempt_root: str | Path,
        timeout: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> PersistentRunResult:
        if not self._admission_guard.acquire(blocking=False):
            raise PersistentSessionError("persistent session is serial and already admitted")
        try:
            return self._run_locked(
                settings,
                attempt_root=attempt_root,
                timeout=timeout,
                cancel=cancel,
            )
        finally:
            self._admission_guard.release()

    def _run_locked(
        self,
        settings: dict[str, Any],
        *,
        attempt_root: str | Path,
        timeout: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> PersistentRunResult:
        if not isinstance(settings, dict):
            raise PersistentSessionError("persistent session settings must be an object")
        _require_residency_attestation(settings)
        key = resident_key(
            settings,
            runner_kind=self._runner_kind,
            engine_identity=self._engine_identity,
            warmth_profile=self._warmth_profile,
        )
        spool = Path(attempt_root).expanduser().resolve()
        spool.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if self._in_flight:
                raise PersistentSessionError("persistent session is serial and already admitted")
            if cancel is not None and cancel():
                return PersistentRunResult("cancelled", (), ("cancelled before admission",), key, self.generation, False)
            reused = self._ensure_open(key)
            assert self._session is not None and self._adapter is not None and self._binding is not None
            try:
                rebound = self._session.rebind_output_dir(spool)
                observed = self._session.observe_output_dir()
                if rebound is not True or Path(observed).expanduser().resolve() != spool:
                    raise PersistentSessionError("output spool rebind was not independently verified")
                token = self._managed.admit(
                    capability_id=self.CAPABILITY.capability_id,
                    invocation_id=uuid.uuid4().hex,
                )
                self._in_flight = True
                job = self._session.submit_task(dict(settings))
                self._adapter.active_job = job
                self._adapter._last_cancel_result = None
                if cancel is not None and cancel():
                    self._cancel_admission(token)
                    return PersistentRunResult("cancelled", (), ("cancelled",), key, self.generation, reused)
                result = self._wait_for_job(
                    job,
                    timeout=timeout,
                    cancel=cancel,
                    token=token,
                )
                if result is None:
                    return PersistentRunResult("cancelled", (), ("cancelled",), key, self.generation, reused)
                if getattr(result, "success", None) is not True:
                    errors = tuple(str(error) for error in (getattr(result, "errors", ()) or ()))
                    self._retire("task_failed")
                    return PersistentRunResult(
                        "failed", (), errors or ("persistent engine reported failure",), key, self.generation, reused
                    )
                generated = tuple(str(path) for path in (getattr(result, "generated_files", ()) or ()))
                if not generated:
                    self._retire("empty_success")
                    return PersistentRunResult(
                        "failed", (), ("persistent engine reported success without outputs",), key, self.generation, reused
                    )
                violations = [path for path in generated if not _within(spool, Path(path))]
                if violations:
                    self._retire("output_containment_violation")
                    return PersistentRunResult(
                        "failed", (), (f"output containment violated: {violations}",), key, self.generation, reused
                    )
                self._managed.settle(
                    token,
                    result_evidence={
                        "generation": token.generation,
                        "binding_identity": token.binding_identity,
                    },
                )
                return PersistentRunResult("succeeded", generated, (), key, token.generation, reused)
            except (PersistentSessionUncertain, StaleAdmissionError):
                raise
            except Exception as exc:
                if self._managed.occupied:
                    try:
                        self._retire("persistent_task_failed")
                    except PersistentSessionUncertain as cleanup_exc:
                        raise cleanup_exc from exc
                return PersistentRunResult("failed", (), (str(exc),), key, self.generation, reused)
            finally:
                self._in_flight = False
                if self._adapter is not None:
                    self._adapter.active_job = None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._session is not None:
                self._retire("owner_close")
            self._managed.close(reason="owner_close")
            self._closed = True
