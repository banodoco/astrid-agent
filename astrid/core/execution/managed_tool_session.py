"""Engine-neutral custody and fencing for one GenericPackHost tool session.

The Runtime remains the durable task/settlement authority.  This module only
owns the in-process lease around an engine session: its observed binding,
capacity slot, lifecycle generation, and one-shot admission tokens.  Engine
adapters are deliberately duck-typed so the same proof surface can cover
VibeComfy, Wan2GP, and future local tools without making this module an engine
router.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


class ManagedToolSessionError(RuntimeError):
    """Base error for a custody or lifecycle invariant violation."""


class SessionCapacityError(ManagedToolSessionError):
    """The single local capacity slot cannot be replaced safely."""


class StaleAdmissionError(ManagedToolSessionError):
    """An invocation token belongs to an older lifecycle generation."""


class UncertainCancellation(ManagedToolSessionError):
    """Cancellation was requested but its native outcome is not known."""


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """Engine-neutral session capabilities advertised by an adapter."""

    capability_id: str
    ownership_mode: str = "manager_owned"
    terminate_authority: str = "generic_pack_host"
    residency_support: str = "observable_releasable"
    admission_capacity: str = "serial"
    cancellation_strength: str = "fenced"
    uncertain_cancellation: str = "reconcile_before_settlement"
    resources_claimed: tuple[str, ...] = ("gpu", "ports", "files")
    warm_reuse_expected: bool = False

    def __post_init__(self) -> None:
        if not self.capability_id.strip():
            raise ValueError("managed session capability_id must be non-empty")
        if self.ownership_mode != "manager_owned":
            raise ValueError("managed session ownership must remain manager_owned")
        if self.terminate_authority != "generic_pack_host":
            raise ValueError("managed session termination must remain host-owned")
        if self.admission_capacity not in {"serial", "concurrent", "batched"}:
            raise ValueError("managed session admission_capacity is invalid")
        if type(self.warm_reuse_expected) is not bool:
            raise ValueError("managed session warm_reuse_expected must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "ownership_mode": self.ownership_mode,
            "terminate_authority": self.terminate_authority,
            "residency_support": self.residency_support,
            "admission_capacity": self.admission_capacity,
            "cancellation_strength": self.cancellation_strength,
            "uncertain_cancellation": self.uncertain_cancellation,
            "resources_claimed": list(self.resources_claimed),
            "warm_reuse_expected": self.warm_reuse_expected,
        }


@dataclass(frozen=True, slots=True)
class SessionBinding:
    """Observed identity of the process/session currently in custody."""

    session_id: str
    runtime_instance_id: str
    process_birth_id: str
    endpoint: str
    source_digest: str
    config_digest: str
    execution_identity: str = ""
    runtime_epoch: int | None = None
    launch_generation: str = ""
    engine_birth_id: str = ""

    def __post_init__(self) -> None:
        for name in (
            "session_id",
            "runtime_instance_id",
            "process_birth_id",
            "endpoint",
            "source_digest",
            "config_digest",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"managed session {name} must be non-empty")
        if self.runtime_epoch is not None and (
            isinstance(self.runtime_epoch, bool)
            or not isinstance(self.runtime_epoch, int)
            or self.runtime_epoch < 1
        ):
            raise ValueError("managed session runtime_epoch must be a positive integer")

    @property
    def identity_key(self) -> tuple[Any, ...]:
        return (
            self.session_id,
            self.runtime_instance_id,
            self.process_birth_id,
            self.endpoint,
            self.source_digest,
            self.config_digest,
            self.execution_identity,
            self.runtime_epoch,
            self.launch_generation,
            self.engine_birth_id,
        )

    def to_dict(self) -> dict[str, str | int]:
        value: dict[str, str | int] = {
            "session_id": self.session_id,
            "runtime_instance_id": self.runtime_instance_id,
            "process_birth_id": self.process_birth_id,
            "endpoint": self.endpoint,
            "source_digest": self.source_digest,
            "config_digest": self.config_digest,
            "execution_identity": self.execution_identity,
        }
        if self.runtime_epoch is not None:
            value["runtime_epoch"] = self.runtime_epoch
        if self.launch_generation:
            value["launch_generation"] = self.launch_generation
        if self.engine_birth_id:
            value["engine_birth_id"] = self.engine_birth_id
        return value


@dataclass(frozen=True, slots=True)
class AdmissionToken:
    """One invocation lease bound to exactly one lifecycle generation."""

    token_id: str
    session_id: str
    capability_id: str
    invocation_id: str
    generation: int
    binding_identity: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class SessionCustodyEnvelope:
    """Host-owned evidence envelope; not a replacement for Runtime records."""

    schema_version: str
    manager: str
    generation: int
    capability: CapabilityDescriptor
    binding: SessionBinding
    state: str
    admission_token: str | None = None
    terminal: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "manager": self.manager,
            "generation": self.generation,
            "capability": self.capability.to_dict(),
            "binding": self.binding.to_dict(),
            "state": self.state,
            "admission_token": self.admission_token,
            "terminal": dict(self.terminal) if self.terminal is not None else None,
        }

    @property
    def digest(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class _ActiveSession:
    capability: CapabilityDescriptor
    binding: SessionBinding
    adapter: Any
    generation: int
    in_flight: dict[str, AdmissionToken]
    fenced: bool = False
    dispatched: set[str] = field(default_factory=set)
    last_admission_token_id: str | None = None


_MISSING_ADAPTER_OPERATION = object()


class ManagedToolSession:
    """Own one engine session slot and fence every stale completion.

    ``open`` may replace a warm session only after the old adapter has been
    fenced and released.  ``admit`` issues a one-shot token.  ``settle`` and
    ``cancel`` require the exact token, generation, and binding identity, so a
    late child result cannot become a valid Runtime settlement.
    """

    SCHEMA_VERSION = "astrid.managed-tool-session.v1"

    def __init__(self, *, manager_id: str = "generic-pack-host") -> None:
        self.manager_id = manager_id
        self._lock = threading.RLock()
        self._generation = 0
        self._active: _ActiveSession | None = None
        self._closed = False

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active is not None and not self._active.fenced

    @property
    def occupied(self) -> bool:
        """Whether a slot is still held, including a fenced/poisoned slot."""
        with self._lock:
            return self._active is not None

    @property
    def current_binding(self) -> SessionBinding | None:
        """Return the currently owned binding for an ordering decision."""
        with self._lock:
            return self._active.binding if self._active is not None else None

    @property
    def current_capability(self) -> CapabilityDescriptor | None:
        with self._lock:
            return self._active.capability if self._active is not None else None

    @property
    def current_adapter(self) -> Any:
        with self._lock:
            return self._active.adapter if self._active is not None else None

    def adapter_for(self, token: AdmissionToken, *, begin: bool = False) -> Any:
        """Validate custody before accepting an attempt's command or observation."""
        with self._lock:
            active = self._require_token_locked(token)
            if active.fenced:
                raise StaleAdmissionError("managed session is fenced")
            if begin:
                if token.token_id in active.dispatched:
                    raise StaleAdmissionError("admission was already dispatched")
                active.dispatched.add(token.token_id)
            return active.adapter

    def fence_admission(self, token: AdmissionToken, *, reason: str) -> None:
        """Fence only this still-current attempt; late failures cannot fence B."""
        with self._lock:
            self._require_token_locked(token)
            self._fence_locked(reason=reason)

    def open(
        self,
        *,
        capability: CapabilityDescriptor,
        binding: SessionBinding,
        adapter: Any,
        start: Callable[[], None] | None = None,
    ) -> SessionCustodyEnvelope:
        """Admit a manager-owned session into the single local capacity slot."""
        with self._lock:
            if self._closed:
                raise ManagedToolSessionError("managed session manager is closed")
            if self._active is not None:
                if self._active.binding.identity_key == binding.identity_key:
                    if self._active.fenced:
                        raise SessionCapacityError("fenced session requires verified release")
                    if self._active.capability != capability:
                        raise ManagedToolSessionError(
                            "session binding cannot change capability in place"
                        )
                    return self._envelope(state="warm")
                self._fence_and_release_locked(reason="capacity_replacement")
            self._generation += 1
            self._active = _ActiveSession(
                capability=capability,
                binding=binding,
                adapter=adapter,
                generation=self._generation,
                in_flight={},
            )
            if start is not None:
                # Establish custody before allocation. Failed startup still owns
                # the slot until release is verified; no second owner may start.
                try:
                    start()
                except BaseException:
                    self._fence_and_release_locked(reason="startup_failed")
                    raise
            return self._envelope(state="cold")

    def observe(self, binding: SessionBinding) -> SessionCustodyEnvelope:
        """Verify an independent observation still matches the owned session."""
        with self._lock:
            active = self._require_active_locked()
            if active.fenced:
                raise StaleAdmissionError("managed session is fenced")
            if active.binding.identity_key != binding.identity_key:
                self._fence_locked(reason="binding_changed")
                raise StaleAdmissionError("managed session binding changed")
            observation = self._call_adapter(active.adapter, "observe", binding=binding)
            if isinstance(observation, Mapping) and observation.get("ok") is False:
                self._fence_locked(reason="observation_failed")
                raise StaleAdmissionError("managed session observation was not verified")
            return self._envelope(state="warm")

    def admit(self, *, capability_id: str, invocation_id: str) -> AdmissionToken:
        with self._lock:
            active = self._require_active_locked()
            if active.fenced:
                raise StaleAdmissionError("managed session is fenced")
            if active.capability.capability_id != capability_id:
                raise ManagedToolSessionError("capability does not match owned session")
            if not invocation_id.strip():
                raise ValueError("managed session invocation_id must be non-empty")
            if active.capability.admission_capacity == "serial" and active.in_flight:
                raise SessionCapacityError("serial managed session already has an admission")
            token = AdmissionToken(
                token_id=uuid.uuid4().hex,
                session_id=active.binding.session_id,
                capability_id=capability_id,
                invocation_id=invocation_id,
                generation=active.generation,
                binding_identity=active.binding.identity_key,
            )
            active.in_flight[token.token_id] = token
            active.last_admission_token_id = token.token_id
            return token

    def _replace_quiescent(
        self, *, expected_adapter: Any, expected_token: AdmissionToken,
        capability: CapabilityDescriptor, binding: SessionBinding, adapter: Any,
        reason: str,
    ) -> SessionCustodyEnvelope:
        """Replace only the exact, most recently admitted, settled session.

        Admission history survives settlement: an intervening completed call
        invalidates an older owner's authorization even with no live tokens.
        The caller supplies its separately verified settlement/cleanup proof;
        this primitive adds no release or native authority.
        """
        with self._lock:
            active = self._active
            if (
                self._closed or active is None or active.adapter is not expected_adapter
                or active.fenced or active.in_flight or active.dispatched
                or active.generation != expected_token.generation
                or self._generation != expected_token.generation
                or active.binding.identity_key != expected_token.binding_identity
                or active.last_admission_token_id != expected_token.token_id
            ):
                raise StaleAdmissionError("quiescent replacement authorization is stale")
            self._fence_and_release_locked(reason=reason)
            return self.open(capability=capability, binding=binding, adapter=adapter)

    def settle(
        self,
        token: AdmissionToken,
        *,
        result_evidence: Mapping[str, Any],
    ) -> SessionCustodyEnvelope:
        """Consume a token only after exact generation/binding validation."""
        with self._lock:
            active = self._require_token_locked(token)
            if active.fenced:
                raise StaleAdmissionError("cannot settle a fenced admission")
            if not isinstance(result_evidence, Mapping) or not result_evidence:
                raise ManagedToolSessionError("settlement requires observed result evidence")
            if result_evidence.get("generation") != token.generation:
                raise StaleAdmissionError("settlement evidence generation is stale")
            for key, expected in (("token_id", token.token_id), ("invocation_id", token.invocation_id)):
                if (key in result_evidence or token.token_id in active.dispatched) and result_evidence.get(key) != expected:
                    raise StaleAdmissionError("settlement evidence invocation is stale")
            raw_binding = result_evidence.get("binding_identity")
            if not isinstance(raw_binding, (list, tuple)) or tuple(raw_binding) != token.binding_identity:
                raise StaleAdmissionError("settlement evidence binding is stale")
            active.in_flight.pop(token.token_id, None)
            active.dispatched.discard(token.token_id)
            return self._envelope(
                state="settled",
                admission_token=token.token_id,
                terminal={"kind": "settled", "invocation_id": token.invocation_id},
            )

    def cancel(self, token: AdmissionToken, *, outcome: str) -> SessionCustodyEnvelope:
        """Fence on uncertain native cancellation; never guess its outcome."""
        with self._lock:
            active = self._require_token_locked(token)
            if outcome not in {"confirmed", "uncertain"}:
                raise ValueError("cancellation outcome must be confirmed or uncertain")
            if outcome == "uncertain":
                active.in_flight.pop(token.token_id, None)
                self._fence_locked(reason="uncertain_cancellation")
                raise UncertainCancellation(
                    "native cancellation is uncertain; session fenced for reconciliation"
                )
            try:
                native = self._call_adapter(active.adapter, "cancel", reason="confirmed")
            except Exception as exc:
                active.in_flight.pop(token.token_id, None)
                self._fence_locked(reason="cancellation_not_verified")
                raise UncertainCancellation(
                    "native cancellation was not verified; session fenced for reconciliation"
                ) from exc
            if not isinstance(native, Mapping) or native.get("ok") is not True:
                active.in_flight.pop(token.token_id, None)
                self._fence_locked(reason="cancellation_not_verified")
                raise UncertainCancellation(
                    "native cancellation was not verified; session fenced for reconciliation"
                )
            active.in_flight.pop(token.token_id, None)
            active.dispatched.discard(token.token_id)
            return self._envelope(
                state="cancelled",
                admission_token=token.token_id,
                terminal={"kind": "cancelled", "invocation_id": token.invocation_id},
            )

    def restart(self, *, binding: SessionBinding, adapter: Any) -> SessionCustodyEnvelope:
        """Replace a process incarnation only through the same fenced path."""
        with self._lock:
            capability = (
                self._active.capability
                if self._active is not None
                else CapabilityDescriptor("restarted")
            )
            if self._active is not None:
                self._fence_and_release_locked(reason="restart")
            return self.open(
                capability=capability,
                binding=binding,
                adapter=adapter,
            )

    def fence(self, *, reason: str) -> None:
        with self._lock:
            self._fence_locked(reason=reason)

    def close(self, *, reason: str = "host_shutdown") -> None:
        with self._lock:
            if self._closed:
                return
            if self._active is not None:
                self._fence_and_release_locked(reason=reason)
            self._closed = True

    def release(self, *, reason: str = "released") -> None:
        """Release the current slot while keeping the manager reusable."""
        with self._lock:
            if self._active is not None:
                self._fence_and_release_locked(reason=reason)

    def _require_active_locked(self) -> _ActiveSession:
        if self._active is None:
            raise ManagedToolSessionError("no managed session is open")
        return self._active

    def _require_token_locked(self, token: AdmissionToken) -> _ActiveSession:
        if self._active is None:
            raise StaleAdmissionError("admission session has been released")
        active = self._require_active_locked()
        if token.generation != active.generation or token.binding_identity != active.binding.identity_key:
            raise StaleAdmissionError("admission token belongs to an older session identity")
        current = active.in_flight.get(token.token_id)
        if current != token:
            raise StaleAdmissionError("admission token is missing or already consumed")
        return active

    def _fence_locked(self, *, reason: str) -> None:
        active = self._active
        if active is None:
            self._generation += 1
            return
        active.fenced = True
        active.in_flight.clear()
        active.dispatched.clear()
        self._generation += 1
        self._call_adapter(active.adapter, "fence", reason=reason)

    def _fence_and_release_locked(self, *, reason: str) -> None:
        active = self._active
        if active is None:
            self._generation += 1
            return
        self._fence_locked(reason=reason)
        try:
            evidence = self._call_adapter(active.adapter, "release", reason=reason)
            if not isinstance(evidence, Mapping) or evidence.get("ok") is not True:
                raise RuntimeError("managed session release was not verified")
        except Exception as exc:
            # Keep the slot poisoned and occupied.  Admitting a replacement
            # after an unverified release would make the old process an
            # orphan and violates the single-resource contract.
            raise SessionCapacityError("managed session release was not verified") from exc
        self._active = None

    @staticmethod
    def _call_adapter(adapter: Any, method: str, **kwargs: Any) -> Any:
        operation = getattr(adapter, method, None)
        if not callable(operation):
            return _MISSING_ADAPTER_OPERATION
        # Resolve adapter signature compatibility before invoking it.  A
        # TypeError raised *inside* the operation must never trigger a second
        # call with different arguments: fence/release are side-effecting.
        try:
            parameters = inspect.signature(operation).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
            return operation(**kwargs)
        for name, value in kwargs.items():
            matching = next(
                (parameter for parameter in parameters if parameter.name == name),
                None,
            )
            if matching is not None:
                if matching.kind is inspect.Parameter.POSITIONAL_ONLY:
                    return operation(value)
                return operation(**{name: value})
        if any(
            parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            for parameter in parameters
        ):
            return operation(kwargs.get("reason", ""))
        return operation()

    def _envelope(
        self,
        *,
        state: str,
        admission_token: str | None = None,
        terminal: Mapping[str, Any] | None = None,
    ) -> SessionCustodyEnvelope:
        active = self._require_active_locked()
        return SessionCustodyEnvelope(
            schema_version=self.SCHEMA_VERSION,
            manager=self.manager_id,
            generation=active.generation,
            capability=active.capability,
            binding=active.binding,
            state=state,
            admission_token=admission_token,
            terminal=terminal,
        )
