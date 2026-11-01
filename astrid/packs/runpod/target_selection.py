"""Ordinary Comfy selection under the existing exact-pod custody journal.

Offers and explicit consent are separate from saved machine preferences. This
module never reads a provider credential: the host supplies its authenticated
account/config and an observed offer. Acquisition uses the existing lifecycle
and preparation functions, with their existing owner lock held throughout.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from astrid.core.generation.targets import read_config, update_config
from astrid.sdk.storage_root import resolve_runtime_data_root

from .worker_preparation import (
    AcquisitionUnresolvedError, RunPodTargetContract, TargetObservationError,
    acquire_target, create_target_record_exclusive, observe_exact_target, read_exact_pod,
    inspect_volume_records, reconcile_target, replace_target_record, resolve_existing_volume, target_owner,
)


class PaidSelectionError(TargetObservationError):
    """Selection lacks current, exact paid authority or sufficient limits."""


def _label(value: Any, name: str) -> str:
    if (not isinstance(value, str) or not value.strip()
            or any(ord(char) < 32 for char in value)
            or value.startswith(("rpa_", "sk-", "Bearer "))):
        raise PaidSelectionError(f"{name} must be a non-secret identity")
    return value.strip()


def _number(value: Any, name: str, *, zero: bool = False) -> float:
    if (type(value) not in (int, float) or not math.isfinite(value)
            or value < 0 or (not zero and value == 0)):
        raise PaidSelectionError(f"{name} must be a finite {'nonnegative' if zero else 'positive'} number")
    return float(value)


def _utc(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() != timedelta(0):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise PaidSelectionError("deadline must be an absolute UTC timestamp") from None
    return result


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


@dataclass(frozen=True)
class RunPodPreferences:
    provider_account_ref: str
    gpu_types: tuple[str, ...]
    image: str
    volume_id: str
    volume_name: str
    volume_size_gb: int
    mount_path: str
    datacenter_id: str | None = None
    idle_timeout_seconds: int = 600
    credential_ref: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> RunPodPreferences:
        allowed = {"provider_account_ref", "gpu_types", "image", "volume", "datacenter_id",
                   "idle_timeout_seconds", "credential_ref"}
        if set(raw) - allowed:
            raise PaidSelectionError("unsupported RunPod preferences; recover legacy journal locators explicitly")
        gpu_types = raw.get("gpu_types")
        if not isinstance(gpu_types, (list, tuple)) or not gpu_types:
            raise PaidSelectionError("RunPod preferences require ordered GPU choices")
        gpus = tuple(_label(gpu, "gpu_type") for gpu in gpu_types)
        if len(set(gpus)) != len(gpus):
            raise PaidSelectionError("RunPod GPU choices must be unique")
        image = _label(raw.get("image"), "image")
        if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
            raise PaidSelectionError("RunPod worker image must be pinned by sha256 digest")
        volume = raw.get("volume")
        if not isinstance(volume, Mapping) or set(volume) != {"id", "name", "size_gb", "mount_path"}:
            raise PaidSelectionError("RunPod needs one exact existing volume and mount")
        size = volume.get("size_gb")
        if type(size) is not int or size <= 0:
            raise PaidSelectionError("existing volume size must be a positive integer")
        mount = volume.get("mount_path")
        if (not isinstance(mount, str) or not mount.startswith("/")
                or ".." in PurePosixPath(mount).parts or str(PurePosixPath(mount)) != mount):
            raise PaidSelectionError("volume mount must be a canonical absolute pod path")
        idle = raw.get("idle_timeout_seconds", 600)
        if type(idle) is not int or idle < 0:
            raise PaidSelectionError("idle timeout must be a nonnegative integer")
        credential_ref = raw.get("credential_ref")
        if credential_ref is not None and (
            not isinstance(credential_ref, str)
            or not re.fullmatch(r"env:[A-Z][A-Z0-9_]*", credential_ref)
        ):
            raise PaidSelectionError("credential_ref must name an environment credential, never its bytes")
        return cls(
            _label(raw.get("provider_account_ref"), "provider account"), gpus, image,
            _label(volume.get("id"), "volume id"), _label(volume.get("name"), "volume name"),
            size, mount,
            _label(raw["datacenter_id"], "datacenter") if raw.get("datacenter_id") is not None else None,
            idle, credential_ref,
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "provider_account_ref": self.provider_account_ref, "gpu_types": list(self.gpu_types),
            "image": self.image, "idle_timeout_seconds": self.idle_timeout_seconds,
            "volume": {"id": self.volume_id, "name": self.volume_name,
                       "size_gb": self.volume_size_gb, "mount_path": self.mount_path},
            **({"datacenter_id": self.datacenter_id} if self.datacenter_id is not None else {}),
            **({"credential_ref": self.credential_ref} if self.credential_ref is not None else {}),
        }


def read_runpod_preferences(*, path: Path | None = None) -> RunPodPreferences:
    raw = read_config(path).get("runpod")
    if not isinstance(raw, Mapping):
        raise PaidSelectionError("save independent RunPod preferences before preparing a target")
    return RunPodPreferences.from_mapping(raw)


def save_runpod_preferences(raw: Mapping[str, Any], *, path: Path | None = None) -> Path:
    value = RunPodPreferences.from_mapping(raw).as_mapping()
    return update_config(lambda document: document.update(runpod=value), path=path)


def canonical_claim_path(account_ref: str, *, data_root: Path | None = None) -> Path:
    account = _label(account_ref, "provider account")
    root = (data_root if data_root is not None else resolve_runtime_data_root()).expanduser().resolve()
    account_id = hashlib.sha256(account.encode()).hexdigest()
    return root / "runpod" / account_id / "claim.json"


@dataclass(frozen=True)
class RunPodOffer:
    """One currently observed GPU offer; rate includes all billable retention."""

    provider_account_ref: str
    gpu_type: str
    hourly_usd: float
    offer_id: str

    def digest(self, preferences: RunPodPreferences) -> str:
        _number(self.hourly_usd, "observed hourly USD rate")
        _label(self.offer_id, "offer id")
        if (self.provider_account_ref != preferences.provider_account_ref
                or self.gpu_type not in preferences.gpu_types):
            raise PaidSelectionError("offer differs from the selected account/GPU preferences")
        return _digest({"preferences": preferences.as_mapping(), "offer": asdict(self)})


@dataclass(frozen=True)
class PaidRunConsent:
    """Fresh per-invocation approval of a disclosed offer and absolute limits."""

    invocation_id: str
    offer_digest: str
    max_usd: float
    deadline: str
    consent: tuple[str, ...]
    preparation_seconds: float
    execution_seconds: float
    idle_seconds: float

    def validate(self, offer: RunPodOffer, preferences: RunPodPreferences, now: datetime) -> datetime:
        if "paid" not in self.consent:
            raise PaidSelectionError("fresh explicit paid consent is required")
        _label(self.invocation_id, "invocation id")
        if self.offer_digest != offer.digest(preferences):
            raise PaidSelectionError("offer or rate changed; disclose it and obtain fresh paid consent")
        cap = _number(self.max_usd, "USD cap")
        seconds = sum(_number(value, name, zero=True) for name, value in (
            ("preparation seconds", self.preparation_seconds),
            ("execution seconds", self.execution_seconds), ("idle seconds", self.idle_seconds),
        ))
        deadline = _utc(self.deadline)
        if seconds <= 0 or deadline <= now or (deadline - now).total_seconds() < seconds:
            raise PaidSelectionError("UTC deadline cannot cover preparation, execution and disclosed idle")
        if seconds * offer.hourly_usd / 3600 > cap:
            raise PaidSelectionError("USD cap cannot cover preparation, execution and disclosed idle")
        return min(deadline, now + timedelta(seconds=cap / offer.hourly_usd * 3600))


class RunPodSelectionOwner:
    def __init__(
        self, preferences: RunPodPreferences, *, account_ref: str, config_factory: Callable[[RunPodOffer], Any],
        observe_offer: Callable[[], RunPodOffer], observed_hourly_usd: Callable[[str], float],
        data_root: Path | None = None, clock: Callable[[], datetime] | None = None,
    ) -> None:
        if account_ref != preferences.provider_account_ref:
            raise PaidSelectionError("authenticated account differs from selected preferences")
        self.preferences, self.config_factory = preferences, config_factory
        self.observe_offer, self.observed_hourly_usd = observe_offer, observed_hourly_usd
        self.path = canonical_claim_path(account_ref, data_root=data_root)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _read(self) -> dict[str, Any] | None:
        # Legacy sibling handles cannot quietly become a second product owner.
        if (any(self.path.parent.glob("claim-*.json"))
                or any(self.path.parent.parent.glob("claim*.json"))):
            raise AcquisitionUnresolvedError("ambiguous legacy RunPod journals require explicit recovery")
        if self.path.is_symlink():
            raise AcquisitionUnresolvedError("RunPod custody journal cannot be a symlink")
        if self.path.parent.resolve() != self.path.parent:
            raise AcquisitionUnresolvedError("RunPod custody directory cannot traverse symlinks")
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise AcquisitionUnresolvedError("RunPod custody journal is unreadable; recover it explicitly") from None
        if not isinstance(record, dict):
            raise AcquisitionUnresolvedError("RunPod custody journal must contain an object")
        return record

    def _owned(self, record: Mapping[str, Any], offer: RunPodOffer) -> None:
        p = self.preferences
        operation = record.get("operation_id")
        expected = {
            "provider_account_ref": p.provider_account_ref, "worker_image": p.image,
            "network_volume_id": p.volume_id, "network_volume_name": p.volume_name,
            "network_volume_size_gb": p.volume_size_gb, "volume_mount_path": p.mount_path,
            "gpu_type": offer.gpu_type, "acquisition_mode": "allocate", "release_authorized": True,
        }
        if (not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f]{32}", operation)
                or record.get("request_name") != f"astrid-product-{operation}"
                or any(record.get(key) != value for key, value in expected.items())):
            raise AcquisitionUnresolvedError("journal does not prove this product's exact account/pod/image/volume custody")

    async def select(self, consent: PaidRunConsent, *, dry_run: bool = False) -> Mapping[str, Any]:
        p, now = self.preferences, self.clock()
        try:
            offer = self.observe_offer()
        except Exception:
            raise PaidSelectionError("current RunPod offer could not be confirmed") from None
        expires = consent.validate(offer, p, now)
        preview = {"offer": asdict(offer), "max_usd": consent.max_usd, "deadline": consent.deadline,
                   "expires_at": expires.isoformat(), "claim_handle_path": str(self.path)}
        if dry_run:
            self._read()
            return preview
        try:
            config = self.config_factory(offer)
        except Exception:
            raise PaidSelectionError("authenticated RunPod configuration is unavailable") from None
        if (tuple(config.gpu_type_candidates) != (offer.gpu_type,) or config.worker_image != p.image
                or config.volume_mount_path != p.mount_path):
            raise PaidSelectionError("provider configuration differs from the consented GPU/image/mount")
        self._read()  # Reject legacy/symlink custody before creating a lock file.
        with target_owner(self.path):
            record = self._read()
            try:
                observed_volume = await resolve_existing_volume(config.api_key, p.volume_id)
                inspect_volume_records(RunPodTargetContract(
                    "unclaimed", offer.gpu_type, p.image, p.volume_id, p.volume_name,
                    p.volume_size_gb, datacenter_id=p.datacenter_id,
                ), volumes=[observed_volume])
            except Exception:
                raise AcquisitionUnresolvedError("existing account volume cannot be confirmed before claim or reuse") from None
            if record is not None:
                self._owned(record, offer)
                if record.get("state") not in {
                    "claimed", "allocation_ready", "allocation_pending", "allocation_unknown", "allocated_unverified",
                }:
                    raise AcquisitionUnresolvedError("pending cleanup or expired custody prevents admission or renewal")
                previous = record.get("paid_limits", {})
                if previous.get("expires_at"):
                    expires = min(expires, _utc(previous["expires_at"]))
                    required = consent.preparation_seconds + consent.execution_seconds + consent.idle_seconds
                    if (expires - now).total_seconds() < required:
                        raise PaidSelectionError("existing claim lifetime cannot be extended by reuse")
                if (record.get("state") == "claimed"
                        and previous.get("invocation_id") == consent.invocation_id):
                    raise PaidSelectionError("an invocation's paid consent cannot authorize another claim or reuse")
            else:
                operation = uuid.uuid4().hex
                record = {
                    "schema_version": "astrid.runpod.allocation-attempt.v1", "state": "allocation_ready",
                    "operation_id": operation, "request_name": f"astrid-product-{operation}",
                    "provider_account_ref": p.provider_account_ref, "acquisition_mode": "allocate",
                    "release_authorized": True, "worker_activation": "not_started", "create_attempts": 0,
                    "gpu_type": offer.gpu_type, "worker_image": p.image,
                    "network_volume_id": p.volume_id, "network_volume_name": p.volume_name,
                    "network_volume_size_gb": p.volume_size_gb, "volume_mount_path": p.mount_path,
                    "network_volume_datacenter_id": p.datacenter_id,
                    "handle_path": str(self.path), "transport": "https",
                    "reconciliation_required": True,
                }
                create_target_record_exclusive(self.path, record)
            volume = {"id": p.volume_id, "name": p.volume_name, "size": p.volume_size_gb,
                      "dataCenterId": p.datacenter_id}
            limits = {"invocation_id": consent.invocation_id, "offer_digest": consent.offer_digest,
                      "hourly_usd": offer.hourly_usd, "max_usd": consent.max_usd,
                      "deadline": consent.deadline, "expires_at": expires.isoformat(),
                      "authorized_at": now.isoformat(), "idle_seconds": consent.idle_seconds}
            if record["state"] == "claimed":
                try:
                    observation = await observe_exact_target(config.api_key, RunPodTargetContract(
                        str(record["pod_id"]), offer.gpu_type, p.image, p.volume_id, p.volume_name,
                        p.volume_size_gb, record["request_name"], p.datacenter_id,
                    ))
                except Exception:
                    raise AcquisitionUnresolvedError("exact journal-owned pod observation is unresolved") from None
                acquired_record = dict(record)
            else:
                # Paid limits survive a lost create response and process restart.
                record = {**record, "paid_limits": limits}
                replace_target_record(self.path, record, operation_id=record["operation_id"])
                # These are the existing decorated implementations. This scope
                # already owns their exact .owner.lock; do not acquire it twice.
                try:
                    if record["state"] == "allocation_ready" and record.get("create_attempts") == 0:
                        acquired = await acquire_target.__wrapped__(
                            config, handle_path=self.path, attempt=record, volume=volume, pod_id=None,
                            max_wait_seconds=max(1, int((expires - now).total_seconds())),
                            capacity_window_seconds=1, retry_interval_seconds=1,
                            ready_timeout_seconds=max(1, min(900, int((expires - self.clock()).total_seconds()))),
                        )
                    else:
                        acquired = await reconcile_target.__wrapped__(
                            config, handle_path=self.path, attempt=record, volume=volume,
                            ready_timeout_seconds=max(1, min(900, int((expires - self.clock()).total_seconds()))),
                        )
                except Exception:
                    raise AcquisitionUnresolvedError("provider acquisition remains unresolved; reconcile the same journal") from None
                acquired_record, observation = dict(acquired.attempt), acquired.observation
            try:
                status, exact = await asyncio.to_thread(read_exact_pod, config.api_key, observation.pod_id)
                if (status != 200 or exact is None or exact.get("volumeMountPath") != p.mount_path
                        or exact.get("networkVolumeId") != p.volume_id):
                    raise PaidSelectionError("provider did not confirm the exact volume mount")
                if _number(self.observed_hourly_usd(observation.pod_id), "actual hourly USD rate") != offer.hourly_usd:
                    raise PaidSelectionError("actual rate changed; fresh consent is required before work")
                if self.clock() >= expires:
                    raise PaidSelectionError("claim preparation consumed its paid lifetime")
            except Exception:
                pending = {**acquired_record, "state": "cleanup_pending", "reconciliation_required": True}
                replace_target_record(self.path, pending, operation_id=record["operation_id"])
                raise PaidSelectionError("unconfirmed price or lifetime; exact claim requires cleanup") from None
            claimed = {
                **acquired_record, "schema_version": "astrid.runpod.claim.v1", "state": "claimed",
                "pod_id": observation.pod_id, "name": observation.pod_name,
                "claimed_at": record.get("claimed_at", self.clock().isoformat()),
                "provider_observation": observation.to_dict(), "target_identity_verified": True,
                "paid_limits": limits, "reconciliation_required": False,
            }
            replace_target_record(self.path, claimed, operation_id=record["operation_id"])
            return claimed
