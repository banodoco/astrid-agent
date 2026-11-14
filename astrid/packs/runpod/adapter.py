"""Inert, path-loadable A4 adapter. A3 remains the only paid-resource owner.

The broker's priced-stock selection is observational here: create, reconcile
and release always go through the existing write-ahead claim and lifecycle.
Generation runs on the prepared pod as a remote machine
(``astrid.packs.runpod.machine``); no engine endpoint is bridged back here.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from pathlib import Path
from typing import Any

from astrid.packs.runpod.cuda_versions import DEFAULT_ALLOWED_CUDA_VERSIONS
from astrid.packs.runpod.generation import prepare_retained_generation
from astrid.packs.runpod.reuse_policy import (
    RunPodPolicyMismatch as _ReusePolicyMismatch,
)
from astrid.packs.runpod.reuse_policy import check_reuse_policy, current_policy, saved_policy_notice
from astrid.packs.runpod.target_selection import (
    OfferObservationError,
    PaidSelectionError,
    RunPodOffer,
    RunPodSelectionOwner,
    _digest,
    _number,
    ordered_runpod_offers,
)
from astrid.packs.runpod.worker_owner import _run_sync
from astrid.packs.runpod.worker_preparation import (
    RunPodTargetContract,
    inspect_volume_records,
    resolve_existing_volume,
)


def _unavailable_offer_reason(reason: Any) -> str:
    # Lifecycle discovery deliberately returns unknown rather than throwing
    # provider response/error text. Recognize only its fixed sanitized labels.
    if not isinstance(reason, str):
        return "provider_observation_unavailable"
    if reason == "RunPod GraphQL transport or JSON response unavailable":
        return "provider_transport_or_response_unavailable"
    if reason in {"RunPod GraphQL HTTP 401", "RunPod GraphQL HTTP 403"}:
        return "provider_authentication_failed"
    if reason == "RunPod GraphQL HTTP 429":
        return "provider_rate_limited"
    return "provider_observation_unavailable"


async def setup_volumes(api_key: str) -> list[dict[str, Any]]:
    """Read the account's existing network volumes; setup never creates one."""
    from astrid.core.integrations.runpod.storage import list_volumes

    return await list_volumes(api_key)


async def setup_datacenters(api_key: str) -> list[str]:
    """Return provider datacenter IDs, including centers with no current stock."""
    try:
        centers = await _setup_datacenter_rows(api_key)
    except Exception:  # noqa: BLE001 - sanitize provider response failures
        raise RuntimeError("RunPod datacenter catalog is unavailable") from None
    return list(dict.fromkeys(
        dc["id"] for dc in centers
        if isinstance(dc, dict) and isinstance(dc.get("id"), str) and dc["id"].strip()
    ))


async def _setup_datacenter_rows(api_key: str) -> list[dict[str, Any]]:
    # runpod-lifecycle 0.3.1.dev0 has no public raw datacenter query; keep its
    # private query details behind this single Agent-side compatibility helper.
    from runpod_lifecycle.availability import _constraints, _fetch_datacenters

    return await _fetch_datacenters(api_key, _constraints(False, 0, None))


class _SetupGpuTypes(list[str]):
    """Catalog choices with optional, non-blocking stock information."""

    stock_missing: tuple[str, ...] = ()
    availability_fallback: bool = False


def _setup_gpu_catalog(api_key: str) -> list[dict[str, Any]]:
    """Read the provider's GPU catalog through the SDK's existing transport."""
    from runpod.api.graphql import run_graphql_query

    try:
        response = run_graphql_query(
            "query AstridSetupGpuCatalog { gpuTypes { id displayName } }",
            api_key=api_key,
        )
        data = response.get("data") if isinstance(response, dict) else None
        rows = data.get("gpuTypes") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError
        return [row for row in rows if isinstance(row, dict)
                and isinstance(row.get("displayName"), str) and row["displayName"].strip()]
    except Exception:  # noqa: BLE001 - provider errors may contain secrets
        raise RuntimeError("RunPod GPU catalog is unavailable; preferences were not saved") from None


async def setup_gpu_types(api_key: str, *, datacenter_id: str | None,
                          require_network_volume: bool) -> list[str]:
    """Return GPUs offered in the selected datacenter, with stock as guidance."""
    rows = _setup_gpu_catalog(api_key)
    names_by_id = {row["id"]: row["displayName"].strip() for row in rows
                   if isinstance(row.get("id"), str) and row["id"].strip()}
    try:
        centers = await _setup_datacenter_rows(api_key)
    except Exception:  # noqa: BLE001 - provider errors may contain secrets
        raise RuntimeError("RunPod datacenter catalog is unavailable; preferences were not saved") from None

    selected = [dc for dc in centers if datacenter_id is None or dc.get("id") == datacenter_id]
    if datacenter_id is not None and not selected:
        return _SetupGpuTypes()
    entries_by_center = [dc.get("gpuAvailability") for dc in selected]
    if any(not isinstance(entries, list) for entries in entries_by_center):
        raise RuntimeError("RunPod datacenter GPU offerings are unavailable; preferences were not saved")
    availability_rows = [row for entries in entries_by_center for row in entries]
    if any(not isinstance(row, dict) or not isinstance(row.get("gpuTypeId"), str)
           for row in availability_rows):
        raise RuntimeError("RunPod datacenter GPU offerings are unavailable; preferences were not saved")
    fallback = not availability_rows
    offered_ids = set(names_by_id) if fallback else {row["gpuTypeId"] for row in availability_rows}
    names = list(dict.fromkeys(name for gpu_id, name in names_by_id.items() if gpu_id in offered_ids))
    result = _SetupGpuTypes(names)
    result.availability_fallback = fallback
    if datacenter_id is not None and not fallback:
        dc_rows = [row for row in availability_rows if row.get("gpuTypeId") in names_by_id]
        stocked = {names_by_id[row["gpuTypeId"]] for row in dc_rows
                   if row.get("available") is True or (
                       row.get("available") is not False
                       and str(row.get("stockStatus", "")).lower() in {"high", "low", "available"}
                   )}
        result.stock_missing = tuple(name for name in names if name not in stocked)
    return result


async def observe_priced_offer(preferences, config) -> RunPodOffer:
    """Return the best datacenter offer for the user's first available GPU."""
    return (await _observe_priced_offers(preferences, config, first_gpu_only=True))[0]


async def observe_priced_offers(preferences, config) -> tuple[RunPodOffer, ...]:
    """Observe every allowed GPU/datacenter option for one capacity cycle."""
    return await _observe_priced_offers(preferences, config, first_gpu_only=False)


async def _observe_priced_offers(preferences, config, *, first_gpu_only) -> tuple[RunPodOffer, ...]:
    """Broker provider semantics, without its allocation/retry/credential path."""
    from runpod_lifecycle import gpu_availability

    p = preferences
    datacenter = p.datacenter_id
    if p.volume_id is not None:
        volume = await asyncio.wait_for(resolve_existing_volume(config.api_key, p.volume_id), timeout=30)
        inspect_volume_records(RunPodTargetContract(
            "unclaimed", p.gpu_types[0], p.image, p.volume_id, p.volume_name,
            p.volume_size_gb, datacenter_id=datacenter,
        ), volumes=[volume])
        datacenter = volume.get("dataCenterId")
        if not isinstance(datacenter, str) or not datacenter:
            raise PaidSelectionError("exact volume datacenter could not be confirmed")
    unavailable = []
    retry_after = None
    offers = []
    for gpu in p.gpu_types:
        observed = await asyncio.wait_for(gpu_availability(
            api_key=config.api_key, gpu=gpu,
            datacenter_ids=[datacenter] if datacenter is not None else None,
            require_network_volume=p.volume_id is not None,
            container_disk_gb=config.container_disk_gb,
            allowed_cuda_versions=config.allowed_cuda_versions,
            min_cuda_version=config.min_cuda_version,
        ), timeout=30)
        observed_retry = getattr(observed, "retry_after", None)
        if type(observed_retry) in (int, float) and observed_retry >= 0:
            retry_after = max(retry_after or 0, observed_retry)
        if getattr(observed, "status", None) == "unknown":
            unavailable.append(_unavailable_offer_reason(getattr(observed, "reason", None)))
        if not observed.gpu_type_id:
            continue
        for dc in observed.datacenters:
            dc_retry = getattr(dc, "retry_after", None)
            if type(dc_retry) in (int, float) and dc_retry >= 0:
                retry_after = max(retry_after or 0, dc_retry)
            if dc.status == "unknown":
                unavailable.append(_unavailable_offer_reason(getattr(dc, "reason", None)))
            elif dc.status == "available" and dc.price_per_hour is None:
                unavailable.append("price_unconfirmed")
            if (dc.status != "available" or dc.price_per_hour is None or not dc.datacenter_id
                    or (datacenter is not None and dc.datacenter_id != datacenter)):
                continue
            try:
                rate = _number(dc.price_per_hour, "observed hourly USD rate")
            except PaidSelectionError:
                raise OfferObservationError("price_unconfirmed") from None
            identity = {"gpu": observed.gpu_type_id, "datacenter": dc.datacenter_id,
                        "hourly_usd": rate, "preferences": p.as_mapping(),
                        "container_disk_gb": config.container_disk_gb,
                        "allowed_cuda_versions": config.allowed_cuda_versions,
                        "min_cuda_version": config.min_cuda_version}
            offers.append(RunPodOffer(p.provider_account_ref, gpu, rate, _digest(identity),
                                      dc.datacenter_id, observed.gpu_type_id))
        if first_gpu_only and offers:
            return ordered_runpod_offers(p, offers)
    if offers:
        return ordered_runpod_offers(p, offers)
    # Unknown provider evidence never proves that every selected GPU is sold out.
    # A later confirmed offer still wins, preserving the existing choice order.
    # Lifecycle's aggregate unknown can hide a more useful DC-level reason.
    # Keep the first specific reason in observation order when failures differ.
    reason = next((item for item in unavailable if item != "provider_observation_unavailable"),
                  unavailable[0] if unavailable else "stock_unavailable")
    raise OfferObservationError(reason, retry_after=retry_after)


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
        observe_offers=lambda: _run_sync(observe_priced_offers(preferences, config)),
        observed_hourly_usd=actual_rate, data_root=data_root, compatibility=compatibility,
    )
    owner.https_token_file = owner.path.parent / "https-token"
    return owner


_STOCK_WAIT_MESSAGE = (
    "No machine is in stock right now for the requested GPU; this is normal, and "
    "`astrid machines claim --max-hourly-usd <ceiling>` waits for one by default.")


def _availability_message(reason):
    """Human sentence for an availability reason; status/reason tokens stay machine-readable."""
    if reason == "stock_unavailable":
        return _STOCK_WAIT_MESSAGE
    from .target_selection import _OFFER_FAILURES
    return _OFFER_FAILURES.get(reason, _OFFER_FAILURES["observation_failed"])


class RunPodAdapter:
    """Composition facade, with no timer, publication or teardown authority."""

    def __init__(self, *, lease_owner, placement, installation, validate):
        self.lease_owner, self.placement = lease_owner, placement
        self.installation, self.validate = installation, validate

    def quote(self):
        """Return the current, secret-free offer the caller may accept."""
        from dataclasses import asdict

        try:
            offer = self.lease_owner.selection.observe_offer()
        except OfferObservationError as exc:
            if exc.reason != "stock_unavailable":
                raise
            return {"availability": {"status": "unavailable", "reason": exc.reason,
                                     "message": _availability_message(exc.reason)},
                    "preferences": self.lease_owner.selection.preferences.as_mapping()}
        return {"offer": asdict(offer),
                "offer_digest": offer.digest(self.lease_owner.selection.preferences)}

    def operate(self, operation, *, include_quote=False, machine_id=None, consent=None, until_release=False,
                requested_idle_seconds=None, cancelled=None):
        if operation == "setup":
            if include_quote:
                raise ValueError("setup does not observe offers")
            return self.lease_owner.setup()
        if operation == "ls":
            if include_quote:
                raise ValueError("ls does not observe offers")
            return {"leases": self.lease_owner.ls()}
        if operation == "claim":
            if include_quote:
                raise ValueError("claim observes capacity through the shared acquisition loop")
            record = self.lease_owner.claim(consent, requested_idle_seconds=requested_idle_seconds,
                                            cancelled=cancelled)
        elif operation == "keep":
            if include_quote:
                raise ValueError("keep uses the rented machine's recorded offer")
            record = self.lease_owner.keep(machine_id, consent, until_release)
        elif operation == "release":
            record = self.lease_owner.release(reason="explicit", **(
                {"expected_machine_id": machine_id} if machine_id is not None else {}))
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
            result["lease"]["idle_timeout_s"] = lease.get("idle_seconds")
            if operation in {"keep", "claim"}:
                result["paid_limits"] = {key: record["paid_limits"].get(key) for key in
                                         ("hourly_usd", "max_usd", "expires_at", "offer_digest")}
            if operation == "status":
                notice = saved_policy_notice(record)
                if notice is not None:
                    result["policy_notice"] = notice
        if include_quote:
            if operation != "status":
                raise ValueError("offer observation requires status")
            result.update(self.quote())
        return result

    def prepare(self, request, consent, *, requested_idle_seconds=None, on_acquired=None):
        return prepare_retained_generation(
            request, consent=consent, lease_owner=self.lease_owner, placement=self.placement,
            installation=self.installation, validate=self.validate,
            requested_idle_seconds=requested_idle_seconds,
            on_acquired=on_acquired, observe_mounts=getattr(self, "observe_mounts", None),
        )


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


def prevalidate_task(policy, inputs, *, scratch):
    """Refuse malformed policy and workflow before constructing the owner."""
    from astrid.core.generation.interfaces import RunPodPreparationRequest
    from astrid.packs.vibecomfy.shared.bundle_inputs import staged_workflow_path

    required = {"kind", "engine"}
    if (not isinstance(policy, dict) or not required <= set(policy) <= required | {"machine", "consent", "idle_timeout_s"}
            or policy["kind"] != "runpod"):
        raise PaidSelectionError("target policy requires a machine placement")
    if policy["engine"] not in {"comfyui"}:
        raise PaidSelectionError("unsupported retained installation route")
    if "machine" in policy and (not isinstance(policy["machine"], str) or not policy["machine"]):
        raise PaidSelectionError("a pinned machine must name a live lease's machine id")
    if "idle_timeout_s" in policy:
        _number(policy["idle_timeout_s"], "placement idle timeout", zero=True)
    consent = policy.get("consent", [])
    if not isinstance(consent, list) or any(value != "user_env" for value in consent):
        raise PaidSelectionError("invalid job consent scope")
    if inputs.get("comfy_url") or inputs.get("comfy_output_dir"):
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
                                           workflow, values, tuple(consent), None, None)
        _validate_retained_workflow(request)


def build_host_adapter(*, preferences, engine, holds, quiesce, credentials, environ, expected_machine_id=None,
                       fresh_claim=False, job=False):
    """Construct the retained A3/A4 composition on an explicit host request."""
    from runpod_lifecycle.config import RunPodConfig

    from astrid.core.generation import engine_recipe, engine_runner
    from astrid.packs.runpod.remote_placement import (
        ENGINE_BASE,
        RemotePlacement,
        RunPodInstallation,
    )
    from astrid.packs.runpod.target_selection import RunPodLeaseCompatibility, RunPodPreferences
    from astrid.packs.runpod.worker_owner import RunPodLeaseOwner

    if engine not in {"comfyui"}:
        raise PaidSelectionError("unsupported retained installation route")
    preferences = RunPodPreferences.from_mapping(preferences)
    credential_name = (preferences.credential_ref or "env:RUNPOD_API_KEY").removeprefix("env:")
    if not re.fullmatch(r"RUNPOD_[A-Z0-9_]*", credential_name):
        raise PaidSelectionError("provider credential must be a RUNPOD_* environment name")
    if not callable(credentials):
        raise PaidSelectionError("retained host credential resolver is unavailable")
    credential = credentials(credential_name)
    if not isinstance(credential, str) or not credential:
        raise PaidSelectionError("configured provider credential is unavailable to the retained host")
    recipe = engine_recipe.load_recipe(engine, "linux-x86_64-cu130")
    root = f"{ENGINE_BASE}/{engine}"
    plan = engine_runner.plan_install(recipe, root=root, placement="local")
    runtime_lock = json.loads((Path(__file__).resolve().parents[3] / "runtime.lock.json").read_bytes())
    vibe = next(source for source in recipe.sources if source.name == "vibecomfy")
    compatibility = RunPodLeaseCompatibility(
        "sha256:" + plan.recipe_sha256, "sha256:" + plan.models_lock_sha256,
        runtime_lock["source_commit"], vibe.pin.revision,
    )
    config = RunPodConfig(api_key=credential, transport="https",
                          allowed_cuda_versions=DEFAULT_ALLOWED_CUDA_VERSIONS)
    selection = broker_selection(preferences, config, account_ref=preferences.provider_account_ref,
                                 data_root=None, compatibility=compatibility)
    async def drain(record):
        quiesce(record)
        lease = record["lease"]
        return {"schema": "astrid.runpod.lease-drain.v1", "state": "drained", "admission_closed": True,
                **{key: record[key] for key in ("operation_id", "pod_id", "provider_account_ref")},
                **{key: lease[key] for key in ("owner_epoch", "generation", "invocation_id")}}

    owner = RunPodLeaseOwner(selection, holds=holds, quiesce=drain,
                             service_environ=environ)
    try:
        if fresh_claim:
            owner.start_fresh_claim()
        elif job:
            owner.recover_job()
        else:
            owner.recover(**({"expected_machine_id": expected_machine_id} if expected_machine_id is not None else {}))
    except BaseException:
        owner.close()
        raise
    from astrid.packs.runpod.machine import observe_mounts_with

    adapter = RunPodAdapter(lease_owner=owner,
                            placement=RemotePlacement(None),
                            installation=RunPodInstallation(compatibility.as_mapping(), engine=engine, root=root),
                            validate=_validate_retained_workflow)
    adapter.observe_mounts = observe_mounts_with(credential)
    return adapter


def _claimed_preferences(selection=None):
    """Locate the account journal and use the claim's recorded configuration."""
    from astrid.core.generation import settings

    from .target_selection import RunPodSelectionOwner, canonical_claim_path

    if selection is None:
        saved = settings.machine_policy("runpod")
        if not isinstance(saved, dict) or not saved.get("provider_account_ref"):
            raise PaidSelectionError("no machine: run `astrid machines claim`")
        selection = RunPodSelectionOwner.__new__(RunPodSelectionOwner)
        selection.path = canonical_claim_path(saved["provider_account_ref"])
    record = selection._read()
    if record is None:
        raise PaidSelectionError("no machine: run `astrid machines claim`")
    preferences = record.get("claim_preferences")
    if preferences is None:
        machine_id = record.get("pod_id")
        raise PaidSelectionError(
            f"machine {machine_id} has no recorded claim settings: "
            f"run `astrid machines release {machine_id}`, then claim again"
        )
    return preferences


def _cleanup_records(machine_id=None):
    """Locate local custody without discovering or parsing saved settings."""
    from astrid.sdk.storage_root import resolve_runtime_data_root

    records = []
    for path in sorted((resolve_runtime_data_root() / "runpod").glob("*/claim.json")):
        selection = RunPodSelectionOwner.__new__(RunPodSelectionOwner)
        selection.path = path
        record = selection._read()
        if (record is not None and record.get("state") != "cleanup_complete"
                and (machine_id is None or machine_id in {record.get("pod_id"), record.get("operation_id")})):
            records.append(record)
    return records


# Every target action must declare how it reaches retained custody. No action
# may reuse an owner without a classification.
RETAINED_OPERATION_POLICY = {
    "setup": "checked", "status": "checked", "claim": "checked", "keep": "checked",
    "ls": "nonspending", "release": "nonspending",
}


class TargetProviderBindings:
    """Host-owned references and drain callback for its single provider owner.

    The host drives generation through the ``generation.machine`` contribution
    (``astrid.packs.runpod.machine``); ``begin``/``done`` mark the one machine
    job a release must drain first.
    """

    def __init__(self, *, pack_roots, holds, credentials, environ):
        self.pack_roots, self.holds = pack_roots, holds
        self.credentials, self.environ = credentials, environ
        self.lock = threading.RLock()
        self.provider = self.signature = self.active = None
        self.constructing = None
        self.operations = 0

    def _check_reuse_policy(self, *, kind, engine, preferences, current_saved=False):
        """The same fence used immediately before a paid create POST."""
        check_reuse_policy(json.loads(self.signature), kind=kind, engine=engine,
                           preferences=preferences, current_saved=current_saved)

    def get(self, *, kind, engine, preferences=None, expected_machine_id=None, fresh_claim=False, job=False):
        signature = None if job else json.dumps(
            [kind, engine, preferences], sort_keys=True, separators=(",", ":"), allow_nan=False)
        while True:
            with self.lock:
                if self.provider is not None:
                    if job:
                        # Saved defaults cannot expand or shrink this job's
                        # recorded budget/expiry; idle renewal stays inside it.
                        preferences = _claimed_preferences(self.provider.lease_owner.selection)
                    try:
                        self._check_reuse_policy(kind=kind, engine=engine, preferences=preferences)
                    except _ReusePolicyMismatch:
                        if job:
                            raise
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
            if job:
                preferences = _claimed_preferences()
                signature = json.dumps([kind, engine, preferences], sort_keys=True, separators=(",", ":"), allow_nan=False)
            provider = build_host_adapter(preferences=preferences, engine=engine,
                                          holds=self.holds, quiesce=self._quiesce,
                                          credentials=self.credentials, environ=self.environ, **(
                                              {"fresh_claim": True} if fresh_claim else {}), **(
                                              {"job": True} if job else {}), **(
                                              {"expected_machine_id": expected_machine_id}
                                              if expected_machine_id is not None else {}))
            with self.lock:
                self.provider, self.signature = provider, signature
                self._check_reuse_policy(kind=kind, engine=engine, preferences=preferences)
                return provider
        finally:
            with self.lock:
                self.constructing = None
                pending.set()

    @staticmethod
    def _key(lease):
        return lease.operation_id, lease.owner_epoch, lease.generation, lease.invocation_id

    def begin(self, lease, cancel):
        """The machine claimed ``lease`` for one job; a release drains it first."""
        with self.lock:
            previous = self.active
            if previous is not None and not previous["done"].is_set():
                raise ValueError("another target invocation is active")
            self.active = {"key": self._key(lease), "lease": lease, "cancel": cancel,
                           "done": threading.Event()}

    def done(self, lease):
        with self.lock:
            active = self.active
            if active is not None and active["key"] == self._key(lease):
                active["done"].set()

    def _quiesce(self, record):
        lease = record["lease"]
        key = (record["operation_id"], lease["owner_epoch"], lease["generation"], lease["invocation_id"])
        with self.lock:
            active = self.active
            if active is not None and active["key"] != key:
                if not active["done"].is_set():
                    raise ValueError("target drain does not name the retained invocation")
                self.active = active = None
        if active is not None:
            # The host's cancel check stops the machine job; the machine then
            # marks it done before releasing. A job already done (its outputs
            # fetched, the machine now releasing it) is not cancelled.
            if not active["done"].is_set():
                active["cancel"].set()
            if not active["done"].wait(timeout=30):
                raise ValueError("target invocation drain remains unresolved")
            with self.lock:
                if self.active is active:
                    self.active = None
        return None

    def observe(self):
        """One liveness pass over the retained owner's live lease (the host calls it on its polls)."""
        with self.lock:
            provider = self.provider
            if provider is None or self.operations or (self.active is not None and not self.active["done"].is_set()):
                return None
        return provider.lease_owner.observe_liveness(observe=getattr(provider, "observe_pod", None))

    def claim_failure_recorder(self, *, task_id, attempt_id, invocation_id):
        """Keep acquisition failures under the real claim task's identity."""
        from types import SimpleNamespace

        from .failure_receipt import record_failure

        job = SimpleNamespace(task_id=task_id, attempt_id=attempt_id, invocation_id=invocation_id)

        def failed(failure):
            identity = getattr(failure, "_astrid_failed_identity", None)
            machine = ({"placement": "runpod", "id": identity[1], "lease": identity[0]}
                       if identity is not None else None)
            record_failure({"failure": failure, "stage": "claim"}, job=job,
                           machine=machine, timing=None)

        return failed

    def operate(self, policy, operation, *, include_quote=False, cancelled=None):
        if operation not in RETAINED_OPERATION_POLICY:
            raise ValueError("unsupported retained target operation")
        checked = RETAINED_OPERATION_POLICY[operation] == "checked"
        if not checked and self.provider is None:
            # A restarted host can list/release journals even when its settings
            # file is corrupt. No provider construction is needed for listing.
            records = _cleanup_records(policy.get("machine") if isinstance(policy, dict) else None)
            if operation == "ls":
                return {"leases": [{
                    **{key: record.get(key) for key in ("operation_id", "pod_id", "provider_account_ref", "state")},
                    "lease": {**{key: record.get("lease", {}).get(key) for key in (
                        "state", "generation", "invocation_id", "paid_expires_at", "idle_expires_at")},
                        "idle_timeout_s": record.get("lease", {}).get("idle_seconds")},
                } for record in records]}
            if records:
                if len(records) != 1:
                    raise ValueError("release requires an exact retained machine id")
                policy = {"kind": "runpod", "engine": "comfyui",
                          "preferences": records[0].get("claim_preferences") or (
                              policy.get("preferences") if isinstance(policy, dict) else None),
                          **({"machine": policy["machine"]} if isinstance(policy, dict) and "machine" in policy else {})}
            elif not isinstance(policy, dict) or not policy.get("preferences"):
                return {"state": "absent"}
        allowed = {"kind", "engine", "preferences"}
        if operation == "status":
            allowed.add("fresh_claim")
        if operation in {"release", "keep"}:
            allowed.add("machine")
        if operation == "keep":
            allowed.update({"paid_consent", "until_release"})
        if operation == "claim":
            allowed.update({"paid_consent", "requested_idle_seconds"})
        if (not isinstance(policy, dict)
                or not {"kind", "engine", "preferences"} <= set(policy)
                <= allowed):
            raise ValueError("target operation requires one explicit provider policy")
        if "machine" in policy and (not isinstance(policy["machine"], str) or not policy["machine"].strip()):
            raise ValueError("exact machine operation requires a nonempty machine id")
        if "fresh_claim" in policy and policy["fresh_claim"] is not True:
            raise ValueError("fresh claim disclosure must be explicit")
        fresh_claim = operation == "claim" or policy.get("fresh_claim", False)
        consent = None
        if operation in {"keep", "claim"}:
            from .target_selection import PaidRunConsent

            if (set(policy) != allowed or include_quote
                    or operation == "keep" and type(policy["until_release"]) is not bool
                    or not isinstance(policy["paid_consent"], dict)
                    or set(policy["paid_consent"]) != set(PaidRunConsent.__dataclass_fields__)
                    or not isinstance(policy["paid_consent"]["consent"], (list, tuple))):
                raise ValueError(f"{operation} requires fresh paid consent and explicit retention")
            consent = PaidRunConsent(**{**policy["paid_consent"],
                                        "consent": tuple(policy["paid_consent"]["consent"])})
        if operation == "status" and not include_quote and policy["preferences"] is None:
            # Default status observes the retained claim. Explicit preference
            # probes and quotes still bind their caller-supplied full policy.
            with self.lock:
                if self.provider is not None:
                    policy = {**policy, "preferences": json.loads(self.signature)[2]}
                else:
                    policy = {**policy, "preferences": current_policy().as_mapping()}
        if checked and policy["kind"] == "runpod":
            from astrid.packs.runpod.target_selection import RunPodPreferences
            # The setup owner signs the representation it persists. Callers
            # may supply equivalent optional-volume forms without owning this
            # serialization policy or mutating their request in place.
            policy = {**policy, "preferences": RunPodPreferences.from_mapping(policy["preferences"]).as_mapping()}
        with self.lock:
            provider = self.provider if (not checked or self.provider is not None
                                         and json.loads(self.signature)[0] == policy["kind"]) else None
            if provider is not None and RETAINED_OPERATION_POLICY[operation] == "checked":
                try:
                    self._check_reuse_policy(kind=policy["kind"], engine=policy["engine"],
                                             preferences=policy["preferences"])
                except _ReusePolicyMismatch:
                    if not (fresh_claim or operation == "setup" or operation == "status" and include_quote):
                        raise
                    # get() may replace an ended owner; a live lease still
                    # refuses before stock observation or paid admission.
                    provider = None
            # Listing and release explicitly retain access to existing custody
            # after saved preferences change. Neither grants paid authority.
        if provider is None:
            if operation == "keep":
                raise ValueError("keep requires a running host that owns this machine")
            provider = self.get(kind=policy["kind"], engine=policy["engine"], preferences=policy["preferences"], **(
                {"fresh_claim": True} if fresh_claim else {}), **(
                {"expected_machine_id": policy["machine"]} if "machine" in policy else {}))
        with self.lock:
            if provider is not self.provider:
                raise ValueError("target provider changed before the operation")
            if RETAINED_OPERATION_POLICY[operation] == "checked":
                # Recheck after any ended-owner replacement. Fresh paid claims
                # and keep cannot use an old request to evade a lower saved cap.
                self._check_reuse_policy(kind=policy["kind"], engine=policy["engine"],
                    preferences=policy["preferences"],
                    current_saved=False if operation in {"setup", "status"} else
                    "ceiling" if operation == "claim" else True)
            self.operations += 1
        try:
            return provider.operate(operation, include_quote=include_quote, **(
                {"machine_id": policy["machine"]} if "machine" in policy else {}), **(
                {"consent": consent, "until_release": policy["until_release"]} if operation == "keep" else
                {"consent": consent, "requested_idle_seconds": policy["requested_idle_seconds"],
                 "cancelled": cancelled} if operation == "claim" else {}))
        finally:
            with self.lock:
                self.operations -= 1

    def close(self):
        with self.lock:
            if self.operations or self.constructing is not None:
                raise ValueError("target operation remains in progress at host shutdown")
            if self.active is not None:
                if not self.active["done"].is_set():
                    raise ValueError("target invocation is still active at host shutdown")
                self.active = None
            provider, self.provider = self.provider, None
        if provider is not None:
            try:
                provider.lease_owner.release(reason="host_shutdown")
            finally:
                provider.lease_owner.close()
