"""Inert, path-loadable A4 adapter. A3 remains the only paid-resource owner.

The broker's priced-stock selection is observational here: create, reconcile
and release always go through the existing write-ahead claim and lifecycle.
A5 consumes the prepared lease using the existing attached generation chain.
"""
from __future__ import annotations

from typing import Any

from astrid.packs.runpod.generation import prepare_retained_generation
from astrid.packs.runpod.target_selection import (
    PaidSelectionError, RunPodOffer, RunPodSelectionOwner, _digest, _number,
)
from astrid.packs.runpod.worker_owner import _run_sync
from astrid.packs.runpod.worker_preparation import (
    RunPodTargetContract, inspect_volume_records, resolve_existing_volume,
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

    def prepare(self, request, consent, *, requested_idle_seconds=None):
        return prepare_retained_generation(
            request, consent=consent, lease_owner=self.lease_owner, placement=self.placement,
            installation=self.installation, validate=self.validate,
            requested_idle_seconds=requested_idle_seconds,
        )


def build_adapter(**context: Any) -> RunPodAdapter:
    """The retained host supplies its one A3 owner and canonical transport."""
    return RunPodAdapter(**context)
