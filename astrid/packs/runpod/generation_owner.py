"""Public-client composition of the existing exact RunPod custody owners.

Construction is inert. Discovery reads the canonical R02 claim, never a saved
URL. Preparation consumes the existing exact P1/P2 selection and composes R02,
R03 and the prepared task owner. Missing artifacts fail before paid mutation.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Mapping


class RunPodPlanningTarget:
    """Cold, read-only planning for the bounded core image graph.

    This is not endpoint evidence. Actual node compatibility is rechecked on
    the exact installed claim before prepared-worker activation or a warm hold.
    """
    def __init__(self):
        from types import SimpleNamespace

        from astrid.core.generation.targets import TargetRecord
        self.target = SimpleNamespace(url="")
        self.http_origin = ""
        self.generation_target = TargetRecord(owner="astrid", facts={"planning_only": True})

    def object_info(self):
        raise ValueError("cold RunPod planning requires the core EmptyImage → SaveImage workflow")

    def validate_workflow(self, entry, workflow):
        from astrid.core.generation.entries import infer_kind

        nodes = list(workflow.runtime_nodes().values())
        if (len(nodes) != 2 or sorted(node.class_type for node in nodes) != ["EmptyImage", "SaveImage"]
                or infer_kind(workflow) != "image" or entry.get("needs")):
            raise ValueError("cold RunPod planning requires the core EmptyImage → SaveImage workflow")


class RunPodGenerationOwner:
    """Product owner bound to one authenticated public SDK client."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self._transports: dict[str, Any] = {}
        self._monitors: dict[str, Any] = {}
        self._retained: dict[str, Any] = {}
        self._lock = threading.RLock()

    def _selection_owner(self):
        from .target_selection import RunPodSelectionOwner, read_runpod_preferences

        preferences = read_runpod_preferences()
        return RunPodSelectionOwner(
            preferences, account_ref=preferences.provider_account_ref,
            config_factory=lambda offer: self._provider_config(preferences, offer),
            observe_offer=lambda: self._offer(preferences),
            observed_hourly_usd=lambda pod: self._rate(preferences, pod),
            claim_metadata=lambda config: self._transport_metadata(preferences, config),
        )

    @staticmethod
    def _unpublished(*args: Any, **kwargs: Any):
        from astrid.core.generation.preflight import generation_error

        raise generation_error(
            ValueError("RunPod requires an exact published prepared-worker selection for the canonical claim"),
            code="missing_dependency", details={
                "field": "target",
                "required_artifact": "prepared-worker.json",
                "consumer": "astrid.packs.runpod.prepared_task.load_prepared_worker_selection",
            },
        )

    @staticmethod
    def _credential(preferences):
        from astrid.core.util.credentials_scope import CredentialsScope
        if preferences.credential_ref is None:
            raise ValueError("RunPod preferences must select a credential_ref before paid preparation")
        return CredentialsScope.resolve_local("runpod", env_var=preferences.credential_ref[4:]).value

    def _provider_config(self, preferences, offer):
        import secrets

        from runpod_lifecycle import RunPodConfig
        from runpod_lifecycle.https import read_token_file, write_token_file

        from .target_selection import canonical_claim_path
        from .worker_preparation import target_owner

        path = canonical_claim_path(preferences.provider_account_ref)
        with target_owner(path):
            token_path = self._token_path(preferences)
            if not token_path.exists():
                write_token_file(token_path, secrets.token_urlsafe(32))
            token = read_token_file(token_path)
        return RunPodConfig(
            api_key=self._credential(preferences), gpu_type=offer.gpu_type,
            worker_image=preferences.image, template_id=None, transport="https",
            https_token=token,
            volume_mount_path=preferences.mount_path, storage_name=preferences.volume_name,
            storage_volumes=(preferences.volume_id,), disk_size_gb=0, attach_only=True,
        )

    @staticmethod
    def _token_path(preferences):
        from pathlib import Path

        from .prepared_task import _read_mapping
        from .target_selection import canonical_claim_path

        path = canonical_claim_path(preferences.provider_account_ref)
        if path.exists():
            record = _read_mapping(path, label="RunPod claim")
            selected = record.get("https_token_file")
            if not isinstance(selected, str) or not Path(selected).is_absolute():
                raise ValueError("existing RunPod custody has no original HTTPS token reference")
            return Path(selected)
        return path.parent / "agent.token"

    def _transport_metadata(self, preferences, config):
        from runpod_lifecycle.https import read_token_file

        selected = self._token_path(preferences)
        if config.https_token != read_token_file(selected):
            raise ValueError("original RunPod HTTPS token changed before acquisition")
        if preferences.credential_ref is None:
            raise ValueError("RunPod claim needs its selected provider credential reference")
        return {"transport": "https", "https_token_file": str(selected),
                "api_key_ref": preferences.credential_ref[4:]}

    def _offer(self, preferences):
        from runpod_lifecycle.availability import gpu_availability

        from .target_selection import PaidSelectionError, RunPodOffer
        from .worker_owner import _run_sync

        key = self._credential(preferences)
        if preferences.datacenter_id is None:
            raise PaidSelectionError("RunPod preferences must pin the existing volume's datacenter")
        for gpu in preferences.gpu_types:
            observation = _run_sync(gpu_availability(api_key=key, gpu=gpu,
                datacenter_ids=[preferences.datacenter_id]))
            rows = [row for row in observation.datacenters
                    if row.datacenter_id == preferences.datacenter_id and row.status == "available"
                    and row.network_volume_support is True and row.price_per_hour is not None]
            if observation.status == "available" and len(rows) == 1:
                return RunPodOffer(preferences.provider_account_ref, gpu, rows[0].price_per_hour,
                                   str(observation.gpu_type_id) + ":" + preferences.datacenter_id)
        raise PaidSelectionError("no fresh priced RunPod offer matches the saved GPU/volume preferences")

    def _rate(self, preferences, pod):
        from .target_selection import _number
        from .worker_preparation import read_exact_pod

        status, record = read_exact_pod(self._credential(preferences), pod)
        if status != 200 or record is None:
            raise ValueError("RunPod actual rate requires an exact current pod observation")
        return _number(record.get("costPerHr"), "actual hourly USD rate")

    def _selection(self, owner):
        from .prepared_task import load_prepared_worker_selection
        path = owner.path.parent / "prepared-worker.json"
        if not path.is_file():
            self._unpublished()
        selection = load_prepared_worker_selection(path)
        if selection.claim_handle_path != owner.path:
            raise ValueError("published preparation differs from the canonical RunPod claim")
        return path, selection

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
        idle = (record or {}).get("warm_session", {}).get("state") == "idle"
        if (record is None or record.get("state") != "claimed" or raw is None
                or record.get("target_identity_verified") is not True
                or record.get("reconciliation_required") is not False
                or (managed.get("state") not in {"committed", "ready", "ready_observed_by_preparer"}
                    and not (idle and managed.get("state") == "stopped"))):
            # A cold claim requires verified deployment/schema selection.
            # Readiness must never allocate/install to manufacture that proof.
            return RunPodPlanningTarget()
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

        The prepared monitor supplies its retained inspector and host handle.
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
        from vibecomfy.workflow_bundle import emit_bundle

        from astrid.core.generation import entries
        from astrid.core.generation.interfaces import RunPodPreparationRequest
        from astrid.core.generation.preflight import PreparedRunPodGeneration

        from .generation import RunPodGenerationPreparer
        from .remote_placement import LifecyclePodProvider, Limits, RemotePlacement
        from .target_selection import PaidRunConsent

        owner, claim = self._claim()
        selection_path, selection = self._selection(owner)
        if claim is not None and claim.get("warm_session", {}).get("state") in {"active", "releasing"}:
            raise ValueError("an active or releasing RunPod session cannot authorize another preparation")
        # Inspect exact compatibility and current offers before writing a bundle,
        # renewing paid limits, or taking an active warm-session lease.
        entries.target_schema({"needs": []}, workflow, self.validation_target())
        offer = self._offer(owner.preferences)
        grant = PaidRunConsent(uuid.uuid4().hex, offer.digest(owner.preferences), max_usd,
            deadline, consent, float(selection.deployment_profile.get("preparation_seconds", 900)),
            float(selection.deployment_profile.get("execution_seconds", 900)),
            owner.preferences.idle_timeout_seconds)
        expires = grant.validate(offer, owner.preferences, owner.clock())
        if claim is None or claim.get("state") != "claimed":
            self._unpublished()
        root = selection.journal_dir / ("generation-" + grant.invocation_id)
        root.mkdir(parents=True, mode=0o700)
        bundle = emit_bundle(workflow, root / "workflow.py", {"operation": "authored"})
        (root / "source.json").write_text(json.dumps(bundle.materialize_ui(
            schema_provider=entries._schema_provider(), strict=True), sort_keys=True), encoding="utf-8")
        placement = RemotePlacement(LifecyclePodProvider(owner.path.parent,
            provider_account_ref=owner.preferences.provider_account_ref),
            limits=Limits(deadline_seconds=max(1, int((expires - owner.clock()).total_seconds())),
                          storage_name=owner.preferences.volume_name), leave_running=True)
        preparer = RunPodGenerationPreparer(owner=owner, placement=placement, consent=grant,
            validate=lambda request: entries.target_schema({"needs": []}, workflow, self.validation_target()),
            select_worker=lambda claimed: self._selection(owner)[1])
        preparer.prepare(RunPodPreparationRequest({"engine": "comfyui"}, root / "source.json",
            root / "workflow.py", inputs, consent, max_usd, deadline))
        bound = selection.bind_client(self.client, workflow_path=root / "workflow.py",
                                      workflow_bundle_path=root, workflow_inputs=inputs)
        monitor = _PreparedMonitor(self, selection_path, bound)
        self._monitors[str(selection_path)] = monitor
        return PreparedRunPodGeneration(selection, str(selection_path), monitor.checkout, monitor)

    def validate_h3(self, inputs: Mapping[str, Any]) -> Mapping[str, Any]:
        from pathlib import Path

        from astrid.core.generation.preflight import readiness_envelope
        from astrid.packs.h3_av.cli import _asset_map
        from astrid.packs.h3_av.shared.request import _asset_ids, load_request
        from astrid.packs.h3_av.shared.spec import canonical_spec

        request = load_request(inputs["request"])
        spec = canonical_spec(request)
        assets = _asset_map(Path(inputs["asset_map"]))
        for asset in _asset_ids(request):
            if asset not in assets:
                raise ValueError("H3 asset_map does not bind every requested asset")
            path = Path(assets[asset]).expanduser()
            if path.is_symlink() or not path.is_file():
                raise ValueError("H3 assets must be existing regular files")
        blocked = [op.op for op in spec.ops if not spec.capabilities[op.op]["available"]]
        if blocked:
            raise ValueError("H3 request contains unavailable capabilities: " + ", ".join(sorted(set(blocked))))
        return readiness_envelope(entry={"id": "h3_av.transform", "engine": "comfyui"},
                                  target={"engine": "comfyui", "placement": "runpod"})

    def prepare_h3(self, *, inputs: Mapping[str, Any], consent: tuple[str, ...],
                   max_usd: float, deadline: str):
        self.validate_h3(inputs)
        # H3's child workflow is compiled after parent admission. Its initial
        # installed-selection publication shares the remaining cold P2 gap.
        self._unpublished()

    def release(self, *, wait: bool) -> Any:
        result = self.describe()
        if result["state"] == "unclaimed" or result["released"]:
            return result
        from astrid.core.generation.preflight import generation_error

        from .prepared_task import _read_exact_task, release_prepared_worker

        owner, claim = self._claim()
        selection_path, _selection = self._selection(owner)
        managed = claim.get("managed_worker", {})
        qualification = managed.get("activation_qualification", {})
        task_id = qualification.get("task_id")
        if not isinstance(task_id, str):
            raise generation_error(ValueError("RunPod release has no exact prepared task quiescence"),
                                   code="cleanup_pending")
        task = _read_exact_task(self.client, task_id, capability_id="vibecomfy.run")
        if task.get("run_id") != qualification.get("run_id"):
            raise ValueError("RunPod release task/run differs from the canonical activation")
        if str(task.get("state", "")).casefold() not in _TERMINAL:
            raise generation_error(ValueError("RunPod active task must settle before explicit release"),
                                   code="release_pending", task_id=task_id)
        release_prepared_worker(self.client, selection_path, task_id, resume=True)
        result = dict(self.describe())
        result["idempotency_key"] = "runpod-release:" + claim["operation_id"]
        return result

    def retain(self, task: Mapping[str, Any], monitor: Any) -> None:
        task_id, run_id, capability = task["task_id"], task["run_id"], task["capability_id"]
        with self._lock:
            if task_id in self._retained:
                if self._retained[task_id] is not monitor:
                    raise ValueError("RunPod retained task already belongs to another monitor")
                return
            self._retained[task_id] = monitor
        def follow():
            from .prepared_task import _read_exact_task
            try:
                while True:
                    current = _read_exact_task(self.client, task_id, capability_id=capability)
                    if current.get("run_id") != run_id:
                        raise ValueError("RunPod retained task/run changed")
                    monitor(current)
                    if str(current.get("state", "")).casefold() in _TERMINAL:
                        return
                    time.sleep(1)
            except Exception as exc:  # noqa: BLE001 - keep exact custody and only retain a sanitized error type
                self.last_error_type = type(exc).__name__
        threading.Thread(target=follow, name="runpod-owner-" + task_id, daemon=True).start()

    def _expire(self, request):
        from .prepared_task import _claim_target, _read_exact_task
        from .worker_preparation import replace_target_record, target_owner
        _owner, claim = self._claim()
        if claim is None or any(request.get(key) != claim.get(key) for key in (
            "operation_id", "pod_id", "provider_account_ref",
        )) or request.get("idempotency_key") != "runpod-release:" + claim["operation_id"]:
            raise ValueError("RunPod expiry request differs from canonical custody")
        task_id = request["task_id"]
        current = _read_exact_task(self.client, task_id, capability_id="vibecomfy.run")
        qualification = claim.get("managed_worker", {}).get("activation_qualification", {})
        if (current.get("run_id") != request["run_id"]
                or qualification.get("task_id") != task_id
                or qualification.get("run_id") != request["run_id"]
                or current.get("execution_request", {}).get("target") != _claim_target(claim)):
            raise ValueError("RunPod expiry task/run changed")
        with target_owner(_owner.path):
            if _owner._read() != claim:
                raise ValueError("RunPod custody changed before paid expiry fencing")
            replace_target_record(_owner.path, {
                **claim, "state": "cleanup_pending", "reconciliation_required": True,
            }, operation_id=claim["operation_id"])
        if str(current.get("state", "")).casefold() not in _TERMINAL:
            workspace = self.client._remote._transport
            cancel_key = "runpod-release-cancel-" + hashlib.sha256(
                (request["idempotency_key"] + ":" + task_id).encode("utf-8")).hexdigest()
            workspace.cancel_task(task_id, idempotency_key=cancel_key)
            current = _read_exact_task(self.client, task_id, capability_id="vibecomfy.run")
            if current.get("state") == "cancel_requested":
                # Runtime's existing external-attempt abandonment invalidates
                # its lease/output authority without claiming a physical stop.
                # R04 must still prove drain, exact process stop and absence.
                binding = current["execution_binding"]
                workspace.abandon_task(task_id, {
                    "expected_task_version": current["version"],
                    **{key: binding[key] for key in ("attempt_id", "lease_id", "fence", "runtime_epoch")},
                    "execution_binding": dict(binding), "reason": "RunPod paid lifetime expired",
                }, idempotency_key="runpod-release-fence-" + hashlib.sha256(
                    (request["idempotency_key"] + ":" + task_id).encode("utf-8")).hexdigest())
                current = _read_exact_task(self.client, task_id, capability_id="vibecomfy.run")
            if str(current.get("state", "")).casefold() not in _TERMINAL:
                raise RuntimeError("RunPod paid expiry cancellation is unresolved; retain custody: "
                    + str(current.get("state")) + "/" + str(current.get("waiting_reason")))
        return self.release(wait=True)

    def close(self) -> None:
        """Detach read-only transports; machine custody remains in P1."""
        for transport in self._transports.values():
            transport.close()
        self._transports.clear()


_TERMINAL = {"succeeded", "completed", "failed", "cancelled", "canceled"}


class _PreparedMonitor:
    """Retain existing task activation, warm custody and exact endpoint checkout."""

    def __init__(self, owner, selection_path, bound):
        self.owner, self.selection_path, self.bound = owner, selection_path, bound
        self.runtime = bound._runtime
        self.warm = None
        self._inputs = None
        self.runtime.on_prepared = self._warm

    def _warm(self):
        from .worker_owner import RunPodWarmSessionOwner
        runtime = self.runtime
        if self.warm is None:
            record = runtime._preparer._read_claim()
            self.warm = RunPodWarmSessionOwner(runtime._preparer, release_task=self.owner._expire,
                requested_idle_seconds=record["paid_limits"]["idle_seconds"])
            runtime.warm_owner = self.warm
            if "warm_session" in record:
                self.warm.recover(runtime._reference)
            else:
                self.warm.begin(runtime._reference)

    def __call__(self, task):
        if self._inputs is None and str(task.get("state", "")).casefold() not in _TERMINAL:
            from .prepared_task import _task_inputs

            inputs = _task_inputs(task)
            frozen = {}
            for port, name in (("python", "workflow.py"), ("companion", "workflow.vibe.json"), ("source", "source.json")):
                binding = inputs.get(port, {})
                oid = binding.get("object_id", binding.get("digest")) if isinstance(binding, Mapping) else None
                if not isinstance(oid, str):
                    raise ValueError("RunPod admission omitted a canonical managed workflow member")
                data = self.owner.client.media.read_bytes(oid)
                if type(data) is not bytes or "sha256:" + hashlib.sha256(data).hexdigest() != oid:
                    raise ValueError("RunPod admitted workflow bytes differ from their managed digest")
                frozen[name] = data
            for name, data in frozen.items():
                destination = self.runtime.workflow_bundle_path / name
                if destination.is_symlink():
                    raise ValueError("RunPod workflow member is aliased")
                destination.write_bytes(data)
            self._inputs = tuple(hashlib.sha256(frozen[name]).hexdigest() for name in sorted(frozen))
        self.bound.monitor_task(task)
        if str(task.get("state", "")).casefold() not in _TERMINAL:
            self._warm()
        elif str(task.get("state", "")).casefold() in {"failed", "cancelled", "canceled"}:
            self.owner.release(wait=True)

    @contextmanager
    def checkout(self, **kwargs):
        from runpod_lifecycle.https import PORT

        from astrid.core.generation.backends.vibecomfy import CheckoutServerAdapter
        from astrid.core.generation.engine_contracts import SecretRef

        from .prepared_task import _read_mapping
        from .worker_owner import _run_sync

        runtime = self.runtime
        if runtime._parked is None or runtime._qualification is None:
            raise RuntimeError("RunPod endpoint checkout requires exact committed activation")
        claim = _read_mapping(runtime.selection.claim_handle_path, label="RunPod claim")
        def read_file(path, *, deadline):
            return _run_sync(runtime._preparer.preparation_transport.read_endpoint_file(path, deadline=deadline))
        with self.owner.checkout_prepared_endpoint(runtime._launcher.inspector, runtime._parked.handle,
                engine="vibecomfy-h3", platform="linux-x86_64-cu130", endpoint_id="vibecomfy-h3",
                agent_port=PORT, token_ref=SecretRef("file", claim["https_token_file"]),
                read_file=read_file, deadline=time.monotonic() + 30) as transport:
            yield CheckoutServerAdapter.from_host_session(endpoint_transport=transport, **kwargs)
