"""Inert, path-loadable A4 adapter. A3 remains the only paid-resource owner.

The broker's priced-stock selection is observational here: create, reconcile
and release always go through the existing write-ahead claim and lifecycle.
A5 consumes the prepared lease using the existing attached generation chain.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrid.packs.runpod.generation import prepare_retained_generation
from astrid.packs.runpod.target_selection import (
    PaidSelectionError, RunPodOffer, RunPodSelectionOwner, _digest, _number,
)
from astrid.packs.runpod.worker_owner import _run_sync
from astrid.packs.runpod.worker_preparation import (
    AcquisitionUnresolvedError, RunPodTargetContract, inspect_volume_records, resolve_existing_volume,
)


async def observe_priced_offer(preferences, config) -> RunPodOffer:
    """Broker provider semantics, without its allocation/retry/credential path."""
    from runpod_lifecycle import gpu_availability

    p = preferences
    datacenter = p.datacenter_id
    if p.volume_id is not None:
        volume = await resolve_existing_volume(config.api_key, p.volume_id)
        inspect_volume_records(RunPodTargetContract(
            "unclaimed", p.gpu_types[0], p.image, p.volume_id, p.volume_name,
            p.volume_size_gb, datacenter_id=datacenter,
        ), volumes=[volume])
        datacenter = volume.get("dataCenterId")
        if not isinstance(datacenter, str) or not datacenter:
            raise PaidSelectionError("exact volume datacenter could not be confirmed")
    for gpu in p.gpu_types:
        observed = await gpu_availability(
            api_key=config.api_key, gpu=gpu,
            datacenter_ids=[datacenter] if datacenter is not None else None,
            require_network_volume=p.volume_id is not None,
            container_disk_gb=config.container_disk_gb,
            allowed_cuda_versions=config.allowed_cuda_versions,
            min_cuda_version=config.min_cuda_version,
        )
        if not observed.gpu_type_id:
            continue
        for dc in observed.datacenters:
            if (dc.status != "available" or dc.price_per_hour is None or not dc.datacenter_id
                    or (datacenter is not None and dc.datacenter_id != datacenter)):
                continue
            rate = _number(dc.price_per_hour, "observed hourly USD rate")
            identity = {"gpu": observed.gpu_type_id, "datacenter": dc.datacenter_id,
                        "hourly_usd": rate, "preferences": p.as_mapping(),
                        "container_disk_gb": config.container_disk_gb,
                        "allowed_cuda_versions": config.allowed_cuda_versions,
                        "min_cuda_version": config.min_cuda_version}
            return RunPodOffer(p.provider_account_ref, gpu, rate, _digest(identity),
                               dc.datacenter_id, observed.gpu_type_id)
    raise PaidSelectionError("no requested GPU has confirmed stock and price in the selected datacenter")


def broker_selection(preferences, config, *, account_ref, data_root, compatibility):
    """Build selection from explicit host configuration; imports read no auth."""
    from runpod_lifecycle import api

    if config.transport != "https" or config.engine_startup is not None:
        raise PaidSelectionError("retained preparation requires the existing command-authority HTTPS agent")

    def exact_config(offer):
        return config.merge(
            gpu_type=(offer.gpu_type,), gpu_type_id=offer.gpu_type_id,
            worker_image=preferences.image, network_volume_id=preferences.volume_id,
            volume_mount_path=preferences.mount_path,
            data_center_id=offer.datacenter_id or preferences.datacenter_id,
            storage_name=None, storage_volumes=(), attach_only=preferences.volume_id is not None,
            ram_tiers_enabled=False,
        )

    def actual_rate(pod_id):
        observed = api.get_pod_status(pod_id, config.api_key)
        return _number(observed.get("cost_per_hr") if observed is not None else None, "actual hourly USD rate")

    owner = RunPodSelectionOwner(
        preferences, account_ref=account_ref, config_factory=exact_config,
        observe_offer=lambda: _run_sync(observe_priced_offer(preferences, config)),
        observed_hourly_usd=actual_rate, data_root=data_root, compatibility=compatibility,
    )
    owner.https_token_file = owner.path.parent / "https-token"
    return owner


class RunPodAdapter:
    """Composition facade, with no timer, publication or teardown authority."""

    def __init__(self, *, lease_owner, placement, installation, validate):
        self.lease_owner, self.placement = lease_owner, placement
        self.installation, self.validate = installation, validate

    def quote(self):
        """Return the current, secret-free offer the caller may accept."""
        from dataclasses import asdict

        offer = self.lease_owner.selection.observe_offer()
        return {"offer": asdict(offer),
                "offer_digest": offer.digest(self.lease_owner.selection.preferences)}

    def operate(self, operation, *, include_quote=False):
        if operation == "release":
            record = self.lease_owner.release(reason="explicit")
        elif operation == "status":
            with self.lease_owner.lock:
                record = self.lease_owner._read()
        else:
            raise ValueError("unsupported retained target operation")
        result = {"state": record.get("state") if record else "absent"}
        if record:
            result.update({key: record.get(key) for key in
                           ("operation_id", "pod_id", "provider_account_ref", "estimated_cost_usd", "cleanup")})
            lease = record.get("lease", {})
            result["lease"] = {key: lease.get(key) for key in
                               ("state", "owner_epoch", "generation", "invocation_id", "paid_expires_at", "idle_expires_at")}
        if include_quote:
            if operation != "status":
                raise ValueError("offer observation requires status")
            result.update(self.quote())
        return result

    def bind_task(self, policy, inputs, *, invocation_id, scratch, begin_lease, retain_bridge, retire_bridge):
        """Bind the admitted policy and materialized workflow to one lease."""
        from astrid.core.generation.interfaces import RunPodPreparationRequest
        from astrid.packs.vibecomfy.shared.bundle_inputs import staged_workflow_path
        from astrid.packs.runpod.target_selection import PaidRunConsent

        prevalidate_task(policy, inputs, scratch=scratch)
        if (policy["kind"] != "runpod" or policy["engine"] != self.installation.engine
                or policy["preferences"] != self.lease_owner.selection.preferences.as_mapping()):
            raise PaidSelectionError("target policy differs from the retained owner")
        if not isinstance(policy["consent"], (tuple, list)):
            raise PaidSelectionError("paid consent must be a list")
        members = tuple(inputs.get(name) for name in ("python", "companion", "source"))
        if not all(isinstance(value, str) and value for value in members):
            raise PaidSelectionError("retained generation requires materialized canonical workflow and source")
        values = json.loads(inputs.get("workflow_inputs") or "{}")
        if not isinstance(values, dict):
            raise PaidSelectionError("workflow inputs must be an object")
        consent = PaidRunConsent(
            invocation_id=invocation_id, offer_digest=policy["offer_digest"], max_usd=policy["max_usd"],
            deadline=policy["deadline"], consent=tuple(policy["consent"]),
            preparation_seconds=policy["preparation_seconds"], execution_seconds=policy["execution_seconds"],
            idle_seconds=policy["idle_seconds"],
        )
        with staged_workflow_path(workflow=None, python=members[0], companion=members[1], source=members[2],
                                  scratch=scratch) as (workflow, _):
            companion = workflow.with_suffix(".vibe.json")
            source = workflow.with_name("source.json")
            before = hashlib.sha256(companion.read_bytes()).digest()
            request = RunPodPreparationRequest({"engine": self.installation.engine}, source, workflow,
                                               values, consent.consent, consent.max_usd, consent.deadline)
            prepared, evidence = self.bind_endpoint(
                request, consent, begin_lease=begin_lease, retain_bridge=retain_bridge, retire_bridge=retire_bridge,
                requested_idle_seconds=policy["idle_seconds"])
            if hashlib.sha256(companion.read_bytes()).digest() != before:
                retire_bridge(prepared.lease, bridge_closed=False)
                self.lease_owner.release(reason="workflow_changed", _expected_operation=prepared.lease.operation_id)
                raise PaidSelectionError("canonical workflow companion changed during preparation")
            return prepared, evidence

    def prepare(self, request, consent, *, requested_idle_seconds=None, on_acquired=None):
        return prepare_retained_generation(
            request, consent=consent, lease_owner=self.lease_owner, placement=self.placement,
            installation=self.installation, validate=self.validate,
            requested_idle_seconds=requested_idle_seconds,
            on_acquired=on_acquired,
        )

    def bind_endpoint(self, request, consent, *, begin_lease, retain_bridge, retire_bridge, requested_idle_seconds=None):
        """Prepare one lease and attach its authenticated loopback endpoint.

        ``retain_bridge`` registers the bridge with the host's exact invocation
        drain before it can be used.  The A3 owner remains the only authority
        that settles or releases the paid lease.
        """
        from astrid.core.generation.engine_recipe import load_recipe
        from astrid.core.generation.backends.comfy_target import (
            ComfyTarget, ComfyTargetClient, HTTP_TRANSFER_SECONDS, MAX_JSON_BYTES,
        )
        from runtime_protocol import platform
        from runpod_lifecycle.engine_bridge import EngineBridge
        from runpod_lifecycle.https import HttpsPodTransport, read_token_file

        if not all(callable(value) for value in (begin_lease, retain_bridge, retire_bridge)):
            raise ValueError("endpoint binding requires the host's bridge drain")
        identity = platform.process_identity(os.getpid())
        if (not isinstance(identity, dict) or identity.get("pid") != os.getpid()
                or not isinstance(identity.get("birth_id"), str) or not identity["birth_id"]):
            raise ValueError("endpoint binding requires an observable host process identity")
        recipe = load_recipe(self.installation.engine, self.installation.platform)
        acquired = []
        bridge = None
        transport = None
        try:
            def acquired_lease(lease):
                acquired.append(lease)
                begin_lease(lease)

            prepared = self.prepare(request, consent, requested_idle_seconds=requested_idle_seconds,
                                    on_acquired=acquired_lease)
            # The account lock fences expiry/release while an endpoint is
            # attached. Save the exact association before enabling the proxy.
            with self.lease_owner.lock:
                record = self.lease_owner.preparation_claim(prepared.lease)
                binding = {
                    "schema": "astrid.runpod.endpoint-binding.v1", "state": "binding",
                    "operation_id": prepared.lease.operation_id,
                    "owner_epoch": prepared.lease.owner_epoch,
                    "generation": prepared.lease.generation,
                    "invocation_id": prepared.lease.invocation_id,
                    "pod_id": record["pod_id"],
                    "provider_account_ref": record["provider_account_ref"],
                    "host_pid": identity["pid"], "host_birth_id": identity["birth_id"],
                    "endpoint_id": self.installation.engine,
                    "engine_port": recipe.start.port,
                    "installed_receipt_sha256": prepared.installed["receipt_sha256"],
                    "workflow_sha256": "sha256:" + hashlib.sha256(request.workflow.read_bytes()).hexdigest(),
                    "source_sha256": "sha256:" + hashlib.sha256(request.source.read_bytes()).hexdigest(),
                }
                companion = request.workflow.with_suffix(".vibe.json")
                if companion.is_file() and not companion.is_symlink():
                    binding["companion_sha256"] = "sha256:" + hashlib.sha256(companion.read_bytes()).hexdigest()
                self.lease_owner._save(record, {**record["lease"], "endpoint_binding": binding})
                token = read_token_file(record["https_token_file"])
                transport = HttpsPodTransport(record["pod_id"], token)
                transport.enable_engine_proxy(binding["endpoint_id"], binding["engine_port"])
                bridge = EngineBridge(transport, {"endpoint_id": binding["endpoint_id"],
                                                   "port": binding["engine_port"]})
                transport = None  # bridge owns and closes its dedicated transport
                retain_bridge(prepared.lease, bridge)
                record = self.lease_owner.preparation_claim(prepared.lease)
                binding = {**binding, "state": "bound", "loopback_url": bridge.url}
                self.lease_owner._save(record, {**record["lease"], "endpoint_binding": binding})
                client = ComfyTargetClient(ComfyTarget(bridge.url, "attached"))
                info = client.request_json(recipe.health.object_info_path, timeout=HTTP_TRANSFER_SECONDS,
                                           response_limit=MAX_JSON_BYTES)
                if not isinstance(info, dict) or any(
                    node not in info for node in recipe.health.required_nodes
                ):
                    raise ValueError("bound engine did not satisfy canonical readiness")
                body = json.dumps(info, sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=False, allow_nan=False).encode("utf-8")
                record = self.lease_owner.preparation_claim(prepared.lease)
                binding = {**binding, "state": "ready",
                           "object_info_sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
                           "object_info_encoding": "json-utf8-sorted-v1"}
                self.lease_owner._save(record, {**record["lease"], "endpoint_binding": binding})
                return prepared, dict(binding)
        except BaseException:
            if bridge is not None:
                bridge.close()
            elif transport is not None:
                transport.disconnect()
            # Release, including exact host drain and independent absence
            # observation, stays with the existing lease owner.
            if acquired:
                retire_bridge(acquired[0], bridge_closed=bridge is not None)
                self.lease_owner.release(reason="endpoint_binding_failure",
                                         _expected_operation=acquired[0].operation_id)
            raise


def build_adapter(**context: Any) -> RunPodAdapter:
    """The retained host supplies its one A3 owner and canonical transport."""
    return RunPodAdapter(**context)


def _validate_retained_workflow(request):
    """Use the existing canonical bundle reader before a paid acquisition."""
    from astrid.packs.vibecomfy.production_engine import load_workflow_path

    loaded = load_workflow_path(request.workflow, request.workflow.parent)
    loaded.resolved.require_canonical_authority("retained target execution")
    unknown = set(request.inputs) - set(loaded.resolved.workflow.inputs)
    if unknown:
        raise PaidSelectionError("workflow contains undeclared input bindings")


def _validate_endpoint_binding(record):
    lease = record.get("lease", {})
    binding = lease.get("endpoint_binding") if isinstance(lease, dict) else None
    if binding is None:
        return
    required = {"schema", "state", "operation_id", "owner_epoch", "generation", "invocation_id",
                "pod_id", "provider_account_ref", "host_pid", "host_birth_id", "endpoint_id", "engine_port",
                "installed_receipt_sha256", "workflow_sha256", "source_sha256"}
    optional = {"companion_sha256", "loopback_url", "object_info_sha256", "object_info_encoding"}
    if (not isinstance(binding, dict) or set(binding) - required - optional or not required <= set(binding)
            or binding["schema"] != "astrid.runpod.endpoint-binding.v1"
            or binding["state"] not in {"binding", "bound", "ready"}
            or any(binding[name] != record.get(name) for name in ("operation_id", "pod_id", "provider_account_ref"))
            or any(binding[name] != lease.get(name) for name in ("owner_epoch", "generation", "invocation_id"))
            or type(binding["host_pid"]) is not int or binding["host_pid"] < 1
            or not isinstance(binding["host_birth_id"], str) or not binding["host_birth_id"]
            or binding["endpoint_id"] not in {"comfyui", "vibecomfy-h3"}
            or type(binding["engine_port"]) is not int or not 1 <= binding["engine_port"] <= 65535):
        raise PaidSelectionError("endpoint binding does not match exact lease custody")
    for name in ("installed_receipt_sha256", "workflow_sha256", "source_sha256", "companion_sha256", "object_info_sha256"):
        if name in binding and (not isinstance(binding[name], str)
                                or re.fullmatch(r"sha256:[0-9a-f]{64}", binding[name]) is None):
            raise PaidSelectionError("endpoint binding has invalid digest evidence")
    installed = lease.get("preparation", {}).get("installed", {})
    if binding["installed_receipt_sha256"] != installed.get("receipt_sha256"):
        raise PaidSelectionError("endpoint binding differs from the installed receipt")
    if binding["state"] in {"bound", "ready"}:
        from astrid.core.generation.backends.comfy_target import validate_attached_url
        if validate_attached_url(binding.get("loopback_url")) != binding["loopback_url"]:
            raise PaidSelectionError("endpoint binding has an invalid loopback URL")
    if binding["state"] == "ready" and "object_info_sha256" not in binding:
        raise PaidSelectionError("endpoint binding lacks readiness evidence")
    if "object_info_encoding" in binding and binding["object_info_encoding"] != "json-utf8-sorted-v1":
        raise PaidSelectionError("endpoint binding has invalid readiness encoding")


def prevalidate_task(policy, inputs, *, scratch):
    """Refuse malformed policy and workflow before constructing the owner."""
    from astrid.core.generation.interfaces import RunPodPreparationRequest
    from astrid.packs.runpod.target_selection import RunPodPreferences, _utc
    from astrid.packs.vibecomfy.shared.bundle_inputs import staged_workflow_path

    required = {"kind", "engine", "preferences", "offer_digest", "consent", "max_usd", "deadline",
                "preparation_seconds", "execution_seconds", "idle_seconds"}
    if not isinstance(policy, dict) or set(policy) != required or policy["kind"] != "runpod":
        raise PaidSelectionError("target policy lacks exact offer and paid bounds")
    if policy["engine"] not in {"comfyui", "vibecomfy-h3"}:
        raise PaidSelectionError("unsupported retained installation route")
    if RunPodPreferences.from_mapping(policy["preferences"]).as_mapping() != policy["preferences"]:
        raise PaidSelectionError("target preferences are not canonical")
    if not isinstance(policy["offer_digest"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", policy["offer_digest"]):
        raise PaidSelectionError("target policy requires the disclosed offer digest")
    consent = policy["consent"]
    if (not isinstance(consent, list) or "paid" not in consent
            or any(value not in {"paid", "user_env"} for value in consent)):
        raise PaidSelectionError("fresh paid consent is required")
    _number(policy["max_usd"], "USD cap")
    durations = [_number(policy[name], name, zero=True) for name in
                 ("preparation_seconds", "execution_seconds", "idle_seconds")]
    if sum(durations) <= 0 or (_utc(policy["deadline"]) - datetime.now(timezone.utc)).total_seconds() < sum(durations):
        raise PaidSelectionError("deadline cannot cover the disclosed paid bounds")
    if inputs.get("comfy_url") or inputs.get("comfy_output_dir") or inputs.get("source_video"):
        raise PaidSelectionError("target policy cannot be combined with an endpoint override")
    members = tuple(inputs.get(name) for name in ("python", "companion", "source"))
    if not all(isinstance(value, str) and value for value in members):
        raise PaidSelectionError("retained generation requires materialized canonical workflow and source")
    values = json.loads(inputs.get("workflow_inputs") or "{}")
    if not isinstance(values, dict):
        raise PaidSelectionError("workflow inputs must be an object")
    with staged_workflow_path(workflow=None, python=members[0], companion=members[1], source=members[2],
                              scratch=scratch) as (workflow, _):
        request = RunPodPreparationRequest({"engine": policy["engine"]}, workflow.with_name("source.json"),
                                           workflow, values, tuple(consent), policy["max_usd"], policy["deadline"])
        _validate_retained_workflow(request)


def build_host_adapter(*, preferences, engine, holds, quiesce, credentials, environ):
    """Construct the retained A3/A4 composition on an explicit host request."""
    from astrid.core.generation import engine_recipe, engine_runner
    from runpod_lifecycle.config import RunPodConfig
    from astrid.packs.runpod.remote_placement import RemotePlacement, RunPodInstallation
    from astrid.packs.runpod.target_selection import RunPodLeaseCompatibility, RunPodPreferences
    from astrid.packs.runpod.worker_owner import RunPodLeaseOwner

    if engine not in {"comfyui", "vibecomfy-h3"}:
        raise PaidSelectionError("unsupported retained installation route")
    preferences = RunPodPreferences.from_mapping(preferences)
    credential_name = (preferences.credential_ref or "env:RUNPOD_API_KEY").removeprefix("env:")
    if not callable(credentials):
        raise PaidSelectionError("retained host credential resolver is unavailable")
    credential = credentials(credential_name)
    if not isinstance(credential, str) or not credential:
        raise PaidSelectionError("configured provider credential is unavailable to the retained host")
    source_root = environ.get("ASTRID_RUNPOD_SOURCE_ROOT")
    if not isinstance(source_root, str) or not source_root:
        raise PaidSelectionError("retained installation source root is unavailable")
    recipe = engine_recipe.load_recipe(engine, "linux-x86_64-cu130")
    root = f"/workspace/astrid/engines/{engine}"
    plan = engine_runner.plan_install(recipe, root=root, placement="local")
    runtime_lock = json.loads((Path(__file__).resolve().parents[3] / "runtime.lock.json").read_bytes())
    vibe = next(source for source in recipe.sources if source.name == "vibecomfy")
    compatibility = RunPodLeaseCompatibility(
        "sha256:" + plan.recipe_sha256, "sha256:" + plan.models_lock_sha256,
        runtime_lock["source_commit"], vibe.pin.revision,
    )
    config = RunPodConfig(api_key=credential, transport="https")
    selection = broker_selection(preferences, config, account_ref=preferences.provider_account_ref,
                                 data_root=None, compatibility=compatibility)
    async def drain(record):
        _validate_endpoint_binding(record)
        quiesce(record)
        lease = record["lease"]
        return {"schema": "astrid.runpod.lease-drain.v1", "state": "drained", "admission_closed": True,
                **{key: record[key] for key in ("operation_id", "pod_id", "provider_account_ref")},
                **{key: lease[key] for key in ("owner_epoch", "generation", "invocation_id")}}

    owner = RunPodLeaseOwner(selection, holds=holds, quiesce=drain,
                             service_environ=environ)
    try:
        owner.recover()
    except BaseException:
        owner.close()
        raise
    return RunPodAdapter(lease_owner=owner,
                         placement=RemotePlacement(None, source_root=Path(source_root)),
                         installation=RunPodInstallation(compatibility.as_mapping(), engine=engine, root=root),
                         validate=_validate_retained_workflow)


class TargetProviderBindings:
    """Host-owned references and drain callback for its single provider owner."""

    def __init__(self, *, pack_roots, holds, credentials, environ):
        self.pack_roots, self.holds = pack_roots, holds
        self.credentials, self.environ = credentials, environ
        self.lock = threading.RLock()
        self.provider = self.signature = self.active = None
        self.constructing = None
        self.operations = 0

    def get(self, *, kind, engine, preferences):
        signature = json.dumps([kind, engine, preferences], sort_keys=True, separators=(",", ":"), allow_nan=False)
        while True:
            with self.lock:
                if self.provider is not None:
                    if self.signature != signature:
                        prior = self.provider
                        if self.operations:
                            raise ValueError("target operation remains in progress")
                        with prior.lease_owner.lock:
                            record = prior.lease_owner._read()
                            if record is not None and record.get("state") != "cleanup_complete":
                                raise ValueError("target policy differs from the retained provider owner")
                            if self.active is not None:
                                if not self.active["done"].is_set():
                                    raise ValueError("target invocation remains active")
                                if self.active["bridge"] is not None:
                                    self.active["bridge"].close()
                                self.active = None
                            prior.lease_owner.close()
                        self.provider = self.signature = None
                    else:
                        return self.provider
                if self.constructing is None:
                    self.constructing = threading.Event()
                    pending = self.constructing
                    break
                pending = self.constructing
            if not pending.wait(timeout=30):
                raise ValueError("target provider initialization remains unresolved")
        try:
            if kind != "runpod":
                raise ValueError("target provider does not match the admitted pack")
            provider = build_host_adapter(preferences=preferences, engine=engine,
                                          holds=self.holds, quiesce=self._quiesce,
                                          credentials=self.credentials, environ=self.environ)
            with self.lock:
                self.provider, self.signature = provider, signature
                return provider
        finally:
            with self.lock:
                self.constructing = None
                pending.set()

    @staticmethod
    def _key(lease):
        return lease.operation_id, lease.owner_epoch, lease.generation, lease.invocation_id

    def _quiesce(self, record):
        lease = record["lease"]
        key = (record["operation_id"], lease["owner_epoch"], lease["generation"], lease["invocation_id"])
        with self.lock:
            active = self.active
            if active is not None and active["key"] != key:
                if not active["done"].is_set():
                    raise ValueError("target drain does not name the retained invocation")
                if active["bridge"] is not None:
                    active["bridge"].close()
                self.active = active = None
        if active is not None:
            binding = lease.get("endpoint_binding")
            bridge = active["bridge"]
            if (binding is not None and bridge is not None
                    and binding.get("loopback_url") != bridge.url):
                raise ValueError("target bridge origin differs from the retained listener")
            active["cancel"].set()
            if not active["done"].wait(timeout=30):
                raise ValueError("target invocation drain remains unresolved")
            with self.lock:
                if self.active is not active:
                    raise ValueError("target invocation changed during drain")
                if active["bridge"] is not None:
                    active["bridge"].close()
                    active["bridge"] = None
                self.active = None
        else:
            # After a host crash the listener can only disappear with that
            # exact host process. Unknown process observation stays pending.
            binding = lease.get("endpoint_binding")
            if binding is not None:
                from runtime_protocol import platform

                if (not isinstance(binding, dict) or binding.get("state") not in {"binding", "bound", "ready"}
                        or any(binding.get(name) != record.get(name) for name in
                               ("operation_id", "pod_id", "provider_account_ref"))
                        or any(binding.get(name) != lease.get(name) for name in
                               ("owner_epoch", "generation", "invocation_id"))):
                    raise ValueError("target bridge custody differs from the retained lease")
                pid, birth = binding.get("host_pid"), binding.get("host_birth_id")
                if type(pid) is not int or pid < 1 or not isinstance(birth, str) or not birth:
                    raise ValueError("target bridge owner identity is missing")
                observed = platform.process_identity(pid)
                if observed is not None:
                    matches = platform.process_birth_matches(birth, observed.get("birth_id", ""))
                    if matches is not False:
                        raise ValueError("target bridge owner death is unproven")
        return None

    def bind(self, policy, inputs, *, invocation_id, scratch, cancel):
        if not isinstance(policy, dict):
            raise ValueError("target policy must be an object")
        prevalidate_task(policy, inputs, scratch=scratch)
        provider = self.get(kind=policy.get("kind"), engine=policy.get("engine"),
                            preferences=policy.get("preferences"))
        retained = []

        def begin(lease):
            with self.lock:
                previous = self.active
                if previous is not None:
                    if not previous["done"].is_set():
                        raise ValueError("another target invocation is active")
                    if previous["bridge"] is not None:
                        previous["bridge"].close()
                active = {"key": self._key(lease), "lease": lease, "bridge": None,
                          "cancel": cancel, "done": threading.Event()}
                self.active = active
                retained.append(active)

        def retain(lease, bridge):
            with self.lock:
                active = self.active
                if active is None or active["key"] != self._key(lease) or active["bridge"] is not None:
                    raise ValueError("target bridge does not own the active invocation")
                active["bridge"] = bridge

        def retire(lease, *, bridge_closed):
            with self.lock:
                active = self.active
                if active is not None and active["key"] == self._key(lease):
                    if active["bridge"] is not None and not bridge_closed:
                        active["bridge"].close()
                    active["bridge"] = None
                    active["done"].set()

        with self.lock:
            if provider is not self.provider:
                raise ValueError("target provider changed before binding")
            self.operations += 1
        try:
            prepared, evidence = provider.bind_task(
                policy, inputs, invocation_id=invocation_id, scratch=scratch,
                begin_lease=begin, retain_bridge=retain, retire_bridge=retire)
        finally:
            with self.lock:
                self.operations -= 1
        if len(retained) != 1 or retained[0]["key"] != self._key(prepared.lease):
            retire(prepared.lease, bridge_closed=False)
            provider.lease_owner.release(reason="binding_not_retained",
                                         _expected_operation=prepared.lease.operation_id)
            raise ValueError("target provider did not retain exactly one invocation bridge")
        return provider, retained[0], evidence

    def operate(self, policy, operation, *, include_quote=False):
        if not isinstance(policy, dict) or set(policy) != {"kind", "engine", "preferences"}:
            raise ValueError("target operation requires one explicit provider policy")
        with self.lock:
            provider = self.provider if self.provider is not None and json.loads(self.signature)[0] == policy["kind"] else None
            if provider is not None and json.loads(self.signature)[2] != policy["preferences"]:
                raise ValueError("target operation preferences differ from the retained provider owner")
        if provider is None:
            provider = self.get(kind=policy["kind"], engine=policy["engine"], preferences=policy["preferences"])
        with self.lock:
            if provider is not self.provider:
                raise ValueError("target provider changed before the operation")
            self.operations += 1
        try:
            return provider.operate(operation, include_quote=include_quote)
        finally:
            with self.lock:
                self.operations -= 1

    def finish(self, provider, active, *, success):
        with self.lock:
            if self.active is not active:
                raise ValueError("target finish does not own the retained invocation")
            active["done"].set()
        if success:
            provider.lease_owner.settle(active["lease"])
        else:
            try:
                provider.lease_owner.release(reason="task_failed",
                                             _expected_operation=active["lease"].operation_id)
            except AcquisitionUnresolvedError:
                # A deadline or explicit release may already own the single
                # terminal transition while it waits for this exact drain.
                with provider.lease_owner.lock:
                    record = provider.lease_owner._read()
                    if not (provider.lease_owner._release_inflight and record is not None
                            and record.get("operation_id") == active["lease"].operation_id
                            and record.get("lease", {}).get("state") == "releasing"):
                        raise

    def close(self):
        with self.lock:
            if self.operations or self.constructing is not None:
                raise ValueError("target operation remains in progress at host shutdown")
            if self.active is not None:
                if not self.active["done"].is_set():
                    raise ValueError("target invocation is still active at host shutdown")
                if self.active["bridge"] is not None:
                    self.active["bridge"].close()
                self.active = None
            provider, self.provider = self.provider, None
        if provider is not None:
            provider.lease_owner.close()
