"""Public-client composition of the existing exact RunPod custody owners.

Construction is inert. Discovery reads the canonical R02 claim, never a saved
URL. F07's deployment publication and release dispatcher are prerequisites to
paid preparation; their absence must fail before acquiring paid custody.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Mapping


class RunPodGenerationOwner:
    """Product owner bound to one authenticated public SDK client."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self._transports: dict[str, Any] = {}

    def _selection_owner(self):
        from .target_selection import RunPodSelectionOwner, read_runpod_preferences

        preferences = read_runpod_preferences()
        # Merely constructing R02 and reading its journal needs no credential,
        # provider configuration, or provider call.
        return RunPodSelectionOwner(
            preferences, account_ref=preferences.provider_account_ref,
            config_factory=self._unpublished, observe_offer=self._unpublished,
            observed_hourly_usd=self._unpublished,
        )

    @staticmethod
    def _unpublished(*args: Any, **kwargs: Any):
        from astrid.core.generation.preflight import generation_error

        raise generation_error(
            ValueError("RunPod preparation requires the F07 deployment publication and release dispatcher"),
            code="missing_dependency", details={
                "field": "target",
                "missing_symbols": [
                    "build_prepared_worker_selection",
                    "GenerationReleaseOwner.release_generation",
                ],
            },
        )

    def _claim(self):
        owner = self._selection_owner()
        record = owner._read()
        if record is not None:
            from .target_selection import RunPodOffer

            # Use R02's exact account/image/volume/name ownership check before
            # projecting or using any controller-side custody reference.
            owner._owned(record, RunPodOffer(
                owner.preferences.provider_account_ref, record.get("gpu_type"), 1.0, "journal-read",
            ))
            if record.get("gpu_type") not in owner.preferences.gpu_types:
                raise ValueError("RunPod claim GPU differs from the selected preferences")
        return owner, record

    @staticmethod
    def _released(record: Mapping[str, Any]) -> None:
        from .worker_preparation import _validate_absence_receipt, _validate_release_quiescence

        _validate_release_quiescence(record.get("release_quiescence"), record)
        _validate_absence_receipt(record)
        cleanup = record.get("cleanup", {})
        if (record.get("cleanup_status") != "complete"
                or record.get("reconciliation_required") is not False
                or cleanup.get("pod_id") != record.get("pod_id")
                or cleanup.get("backup_volume_id") != record.get("network_volume_id")
                or cleanup.get("backup_volume_preserved") is not True
                or cleanup.get("status") not in {"terminated", "already_gone"}):
            raise ValueError("RunPod cleanup has no complete exact release receipt")

    def describe(self) -> Mapping[str, Any]:
        owner, record = self._claim()
        result: dict[str, Any] = {
            "engine": "comfyui", "placement": "runpod",
            "preferences": owner.preferences.as_mapping(), "state": "unclaimed",
            "released": False,
        }
        if record is None:
            return result
        result.update({key: record[key] for key in (
            "operation_id", "pod_id", "state", "gpu_type", "worker_image",
            "network_volume_id", "provider_account_ref", "reconciliation_required",
        ) if key in record})
        if record.get("state") == "cleanup_complete":
            self._released(record)
            result["released"] = True
        return result

    def validation_target(self) -> Any:
        from runpod_lifecycle.https import PORT

        from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
        from astrid.core.generation.engine_contracts import RunPodAttestation
        from astrid.core.generation.engine_endpoint import parse_endpoint
        from astrid.core.generation.engine_transport import EngineEndpointTransport
        from astrid.core.generation.preflight import generation_error
        from astrid.core.generation.targets import TargetRecord

        _owner, record = self._claim()
        managed = (record or {}).get("managed_worker")
        raw = managed.get("engine_endpoint") if isinstance(managed, Mapping) else None
        if (record is None or record.get("state") != "claimed" or raw is None
                or record.get("target_identity_verified") is not True
                or record.get("reconciliation_required") is not False
                or managed.get("state") not in {"committed", "ready", "ready_observed_by_preparer"}):
            # A cold claim requires F07's verified deployment/schema selection.
            # Readiness must never allocate/install to manufacture that proof.
            self._unpublished()
        endpoint = parse_endpoint(raw)
        attestation = endpoint.attestation
        if (not isinstance(attestation, RunPodAttestation)
                or attestation.pod_id != record["pod_id"]
                or attestation.provider_account_ref != record["provider_account_ref"]
                or endpoint.lifecycle_owner.owner_id != record["operation_id"]):
            raise generation_error(ValueError("RunPod endpoint differs from the canonical claim"),
                                   code="input_rejected", details={"field": "target"})
        key = record["operation_id"]
        transport = self._transports.get(key)
        if transport is None or transport.endpoint != endpoint:
            if transport is not None:
                transport.close()
            transport = EngineEndpointTransport(endpoint, pod_id=record["pod_id"], agent_port=PORT)
            self._transports[key] = transport
        facts = {"object_info_sha256": endpoint.health.object_info_sha256}
        target = ComfyTargetClient(ComfyTarget(endpoint.url, "pod", facts), endpoint_transport=transport)
        target.generation_target = TargetRecord(owner="astrid", facts=facts,
                                                verified_at=endpoint.health.checked_at)
        return target

    @contextmanager
    def checkout_prepared_endpoint(self, inspector: Any, handle: Any, **kwargs: Any):
        """Check out the original prepared owner; never turn a URL into custody.

        F07's prepared monitor supplies its retained inspector and host handle.
        Ensure keeps the existing provider/Runtime/process/credential checks;
        transport close only detaches the caller from the endpoint.
        """
        from astrid.core.generation.engine_endpoint import ensure_prepared_endpoint
        from astrid.core.generation.engine_transport import EngineEndpointTransport

        endpoint = ensure_prepared_endpoint(inspector, handle, **kwargs)
        with EngineEndpointTransport(endpoint, pod_id=endpoint.attestation.pod_id,
                                     agent_port=kwargs["agent_port"], deadline=kwargs["deadline"]) as transport:
            yield transport

    def prepare(self, *, workflow: Any, inputs: Mapping[str, Any],
                consent: tuple[str, ...], max_usd: float, deadline: str):
        # Refuse before R02.select, bootstrap/install, file writes or warm holds.
        # The missing F07 builder must supply the verified deployment artifacts
        # consumed by R03; inventing a profile would bypass exact activation.
        self._unpublished()

    def validate_h3(self, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        self._unpublished()

    def prepare_h3(self, *, inputs: Mapping[str, Any], consent: tuple[str, ...],
                   max_usd: float, deadline: str):
        self._unpublished()

    def release(self, *, wait: bool) -> Any:
        result = self.describe()
        if result["state"] == "unclaimed" or result["released"]:
            return result
        # Generic teardown is not the F07 dispatcher: it cannot fence the
        # active prepared task or correlate runpod-release:<operation_id>.
        self._unpublished()

    def retain(self, task: Mapping[str, Any], monitor: Any) -> None:
        self._unpublished()

    def close(self) -> None:
        """Detach read-only transports; machine custody remains in P1."""
        for transport in self._transports.values():
            transport.close()
        self._transports.clear()
