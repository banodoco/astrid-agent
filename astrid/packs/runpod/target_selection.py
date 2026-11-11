"""Ordinary Comfy selection under the existing exact-pod custody journal.

Offers and explicit consent are separate from saved machine preferences. This
module never reads a provider credential: the host supplies its authenticated
account/config and an observed offer. Acquisition uses the existing lifecycle
and preparation functions, with their existing owner lock held throughout.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import random
import re
import sys
import time
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from astrid.core.execution.warm_hold import DEFAULT_IDLE_SECONDS
from astrid.core.execution.warm_hold import DEFAULT_IDLE_SECONDS
from astrid.sdk.storage_root import resolve_runtime_data_root

from .worker_preparation import (
    AcquisitionUnresolvedError, CapacityExhaustedError, RunPodTargetContract, TargetObservationError,
    acquire_target, create_target_record_exclusive, observe_exact_target, read_exact_pod,
    inspect_volume_records, reconcile_target, replace_target_record, resolve_existing_volume, target_owner,
    _validate_completed_release, write_target_record,
)
from . import worker_preparation


class PaidSelectionError(TargetObservationError):
    """Selection lacks current, exact paid authority or sufficient limits.

    Post-acquisition gates pass a fixed ``gate`` name (volume, GPU, datacenter,
    rate, lifetime, machine) that the outer handler reports.
    """


class RunPodSetupRequiredError(PaidSelectionError):
    """Saved RunPod setup is absent or incomplete; never trigger setup implicitly."""

    def __init__(self, missing: tuple[str, ...]):
        self.missing = missing
        super().__init__(
            "RunPod preferences are incomplete (missing " + ", ".join(missing)
            + "); configure them with `astrid machines setup`"
        )


# Plan 34 live validation (banodoco/astrid-dev#260, 10 Nov 2026): this
# immutable base image was validated for the retained RunPod worker path.
DEFAULT_RUNPOD_WORKER_IMAGE = (
    "runpod/base@sha256:820c605eaaa276de20a0d77993fb0846d54e180efff5de67d46341ed399b5a7f"
)


_OFFER_FAILURES = {
    "stock_unavailable": "no requested GPU has confirmed stock and price in the selected datacenter",
    "provider_transport_or_response_unavailable": "provider transport or response is unavailable; retry offer observation",
    "provider_authentication_failed": "provider authentication was rejected; check the configured credential",
    "provider_rate_limited": "provider rate limit was reached; retry offer observation later",
    "provider_observation_unavailable": "provider availability could not be confirmed; retry offer observation",
    "price_unconfirmed": "the selected GPU price could not be confirmed; retry offer observation",
    "observation_failed": "current RunPod offer could not be confirmed; retry offer observation",
}


class OfferObservationError(PaidSelectionError):
    """Fixed offer diagnostics, independent of provider response/error text."""

    def __init__(self, reason: str, *, retry_after: float | None = None):
        self.reason = reason if isinstance(reason, str) and reason in _OFFER_FAILURES else "observation_failed"
        self.retry_after = (float(retry_after) if type(retry_after) in (int, float)
                            and math.isfinite(retry_after) and retry_after >= 0 else None)
        super().__init__(f"RunPod offer unavailable (reason={self.reason}): {_OFFER_FAILURES[self.reason]}", gate="offer")


class CapacityWaitCancelled(asyncio.CancelledError):
    """The caller cancelled before acquisition could safely complete."""


class CapacityWaitTimeout(PaidSelectionError):
    """The disclosed capacity wait or absolute paid deadline passed."""


# The readiness timeout the acquisition path already used (was the literal in
# min(900, ...) for acquire/reconcile); the GPU/DC confirmation waits this long
# (bounded by the paid lifetime) for GraphQL to populate pod.machine.
_READINESS_TIMEOUT_SECONDS = 900


def _rate_decimal(value: Any, name: str) -> Decimal:
    """Exact Decimal from an int/float/str/Decimal through its shortest decimal text; never float math."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise PaidSelectionError(f"{name} must be a finite positive number", gate="rate")
    try:
        result = Decimal(str(value).strip())
    except InvalidOperation:
        raise PaidSelectionError(f"{name} must be a finite positive number", gate="rate") from None
    if not result.is_finite() or result <= 0:
        raise PaidSelectionError(f"{name} must be a finite positive number", gate="rate")
    return result


def pod_rate_within_offer(observed: Any, offer_hourly_usd: Any, gpu_count: Any) -> bool:
    """RunPod costPerHr is the pod total: pass iff observed <= offer * gpuCount (exact Decimal)."""
    if type(gpu_count) is not int or gpu_count < 1:
        raise PaidSelectionError("pod GPU count is unknown; the pod total rate cannot be compared", gate="rate")
    return (_rate_decimal(observed, "actual hourly USD rate")
            <= _rate_decimal(offer_hourly_usd, "offer hourly USD rate") * gpu_count)


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
    volume_id: str | None
    volume_name: str | None
    volume_size_gb: int
    mount_path: str
    datacenter_id: str | None = None
    idle_timeout_seconds: int = int(DEFAULT_IDLE_SECONDS)
    credential_ref: str | None = None
    budget_cap_usd: float | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> RunPodPreferences:
        allowed = {"provider_account_ref", "gpu_types", "image", "volume", "datacenter_id",
                   "idle_timeout_seconds", "credential_ref", "budget_cap_usd"}
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
        if volume is None:
            volume = {"id": None, "name": None, "size_gb": 0, "mount_path": "/workspace"}
        if not isinstance(volume, Mapping) or set(volume) != {"id", "name", "size_gb", "mount_path"}:
            raise PaidSelectionError("RunPod needs one exact existing volume and mount")
        size = volume.get("size_gb")
        ephemeral = volume == {"id": None, "name": None, "size_gb": 0, "mount_path": "/workspace"}
        if type(size) is not int or (size <= 0 and not ephemeral):
            raise PaidSelectionError("existing volume size must be a positive integer")
        mount = volume.get("mount_path")
        if (not isinstance(mount, str) or not mount.startswith("/")
                or ".." in PurePosixPath(mount).parts or str(PurePosixPath(mount)) != mount):
            raise PaidSelectionError("volume mount must be a canonical absolute pod path")
        idle = raw.get("idle_timeout_seconds", int(DEFAULT_IDLE_SECONDS))
        if type(idle) is not int or idle < 0:
            raise PaidSelectionError("idle timeout must be a nonnegative integer")
        credential_ref = raw.get("credential_ref")
        if credential_ref is not None and (
            not isinstance(credential_ref, str)
            or not re.fullmatch(r"env:[A-Z][A-Z0-9_]*", credential_ref)
        ):
            raise PaidSelectionError("credential_ref must name an environment credential, never its bytes")
        budget_cap = raw.get("budget_cap_usd")
        if budget_cap is not None:
            budget_cap = _number(budget_cap, "budget cap")
        return cls(
            _label(raw.get("provider_account_ref"), "provider account"), gpus, image,
            None if ephemeral else _label(volume.get("id"), "volume id"),
            None if ephemeral else _label(volume.get("name"), "volume name"),
            size, mount,
            _label(raw["datacenter_id"], "datacenter") if raw.get("datacenter_id") is not None else None,
            idle, credential_ref, budget_cap,
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "provider_account_ref": self.provider_account_ref, "gpu_types": list(self.gpu_types),
            "image": self.image, "idle_timeout_seconds": self.idle_timeout_seconds,
            **({"volume": {"id": self.volume_id, "name": self.volume_name,
                           "size_gb": self.volume_size_gb, "mount_path": self.mount_path}}
               if self.volume_id is not None else {}),
            **({"datacenter_id": self.datacenter_id} if self.datacenter_id is not None else {}),
            **({"credential_ref": self.credential_ref} if self.credential_ref is not None else {}),
            **({"budget_cap_usd": self.budget_cap_usd} if self.budget_cap_usd is not None else {}),
        }


def read_runpod_preferences(*, path: Path | None = None, client: Any = None) -> RunPodPreferences:
    """RunPod placement policy: ``machines.runpod`` in the user's generation settings document.

    ``path`` names an old VibeComfy TOML whose ``[runpod]`` is imported into the document once.
    """
    from astrid.core.generation import settings

    raw = settings.machine_policy("runpod", client, legacy_path=path)
    if not isinstance(raw, Mapping):
        raise RunPodSetupRequiredError(("preferences",))
    missing = tuple(name for name in ("provider_account_ref", "image", "gpu_types", "budget_cap_usd")
                    if not raw.get(name))
    if missing:
        raise RunPodSetupRequiredError(missing)
    try:
        return RunPodPreferences.from_mapping(raw)
    except PaidSelectionError as exc:
        raise RunPodSetupRequiredError(("valid preferences",)) from exc


def save_runpod_preferences(raw: Mapping[str, Any], *, path: Path | None = None, client: Any = None) -> str:
    from astrid.core.generation import settings

    value = RunPodPreferences.from_mapping(raw).as_mapping()
    return settings.set_machine_policy("runpod", value, client, legacy_path=path).label


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
    datacenter_id: str | None = None
    gpu_type_id: str | None = None

    def digest(self, preferences: RunPodPreferences) -> str:
        _number(self.hourly_usd, "observed hourly USD rate")
        _label(self.offer_id, "offer id")
        if (self.provider_account_ref != preferences.provider_account_ref
                or self.gpu_type not in preferences.gpu_types
                or (self.datacenter_id is not None and preferences.datacenter_id is not None
                    and self.datacenter_id != preferences.datacenter_id)):
            raise PaidSelectionError("offer differs from the selected account/GPU preferences")
        if self.datacenter_id is not None:
            _label(self.datacenter_id, "offer datacenter")
        if self.gpu_type_id is not None:
            _label(self.gpu_type_id, "offer GPU id")
        return _digest({"preferences": preferences.as_mapping(), "offer": asdict(self)})


def ordered_runpod_offers(preferences: RunPodPreferences, offers: Any) -> tuple[RunPodOffer, ...]:
    """Keep the user's GPU order; price orders datacenter offers for one GPU."""
    if not isinstance(offers, (list, tuple)):
        raise PaidSelectionError("RunPod observation must return concrete offers")
    rank = {gpu: index for index, gpu in enumerate(preferences.gpu_types)}
    for offer in offers:
        if not isinstance(offer, RunPodOffer):
            raise PaidSelectionError("RunPod observation must return concrete offers")
        offer.digest(preferences)  # Every option remains inside the selected GPU/DC policy.
    return tuple(sorted(offers, key=lambda offer: (rank[offer.gpu_type], offer.hourly_usd)))


@dataclass(frozen=True)
class PaidRunConsent:
    """Fresh per-invocation approval of a disclosed offer and absolute limits."""

    invocation_id: str
    offer_digest: str | None
    max_usd: float
    deadline: str | None
    consent: tuple[str, ...]
    preparation_seconds: float
    execution_seconds: float
    idle_seconds: float
    policy_digest: str | None = None
    max_hourly_usd: float | None = None
    capacity_timeout_seconds: float | None = None
    wait_for_capacity: bool = True

    def validate_authority(self, preferences: RunPodPreferences, now: datetime) -> datetime | None:
        """Validate paid authority before stock observation or any journal write."""
        if "paid" not in self.consent:
            raise PaidSelectionError("fresh explicit paid consent is required")
        _label(self.invocation_id, "invocation id")
        cap = _number(self.max_usd, "USD cap")
        if preferences.budget_cap_usd is not None and cap > preferences.budget_cap_usd:
            raise PaidSelectionError("invocation cap exceeds the saved RunPod budget ceiling")
        seconds = sum(_number(value, name, zero=True) for name, value in (
            ("preparation seconds", self.preparation_seconds),
            ("execution seconds", self.execution_seconds), ("idle seconds", self.idle_seconds),
        ))
        if type(self.wait_for_capacity) is not bool:
            raise PaidSelectionError("wait_for_capacity must be a boolean")
        if self.capacity_timeout_seconds is not None:
            _number(self.capacity_timeout_seconds, "capacity timeout seconds")
        if self.policy_digest is not None:
            ceiling = _number(self.max_hourly_usd, "hourly USD ceiling")
            if self.offer_digest is not None or self.policy_digest != policy_consent_digest(preferences, ceiling):
                raise PaidSelectionError("RunPod waiting policy changed; review the GPU, storage and hourly ceiling again")
        elif self.max_hourly_usd is not None or not isinstance(self.offer_digest, str):
            raise PaidSelectionError("an exact offer digest or an explicit waiting policy is required")
        deadline = _utc(self.deadline) if self.deadline is not None else None
        if seconds <= 0 or (deadline is not None and (deadline <= now or (deadline - now).total_seconds() < seconds)):
            raise PaidSelectionError("UTC deadline cannot cover preparation, execution and disclosed idle")
        if deadline is None and self.policy_digest is None:
            raise PaidSelectionError("an exact offer requires an absolute UTC deadline")
        return deadline

    def validate(self, offer: RunPodOffer, preferences: RunPodPreferences, now: datetime,
                 *, allow_policy: bool = False) -> datetime:
        deadline = self.validate_authority(preferences, now)
        observed = offer.digest(preferences)
        if self.policy_digest is None:
            if self.offer_digest != observed:
                raise PaidSelectionError("offer or rate changed; disclose it and obtain fresh paid consent")
        elif not allow_policy:
            raise PaidSelectionError("keep requires fresh consent for the exact rented offer")
        elif offer.hourly_usd > self.max_hourly_usd:
            raise PaidSelectionError("observed RunPod hourly rate exceeds the consented hourly ceiling")
        seconds = self.preparation_seconds + self.execution_seconds + self.idle_seconds
        cap = _number(self.max_usd, "USD cap")
        if seconds * offer.hourly_usd / 3600 > cap:
            raise PaidSelectionError("USD cap cannot cover preparation, execution and disclosed idle")
        expiry = now + timedelta(seconds=cap / offer.hourly_usd * 3600)
        return min(deadline, expiry) if deadline is not None else expiry


def policy_consent_digest(preferences: RunPodPreferences, max_hourly_usd: float) -> str:
    """The explicit waiting policy, independent of any current market offer."""
    return _digest({"schema": "astrid.runpod.wait-policy.v1", "preferences": preferences.as_mapping(),
                    "max_hourly_usd": _number(max_hourly_usd, "hourly USD ceiling")})


@dataclass(frozen=True)
class RunPodLeaseCompatibility:
    """Pinned requirements for one reusable installed engine snapshot.

    Account/GPU/image/storage are additionally bound by the claim itself.
    A4/A5 must verify the installation against these requirements before use.
    """

    recipe_digest: str
    model_lock_digest: str
    runtime_commit: str
    vibecomfy_commit: str

    def as_mapping(self) -> dict[str, str]:
        value = asdict(self)
        if (any(not isinstance(value[key], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value[key])
                for key in ("recipe_digest", "model_lock_digest"))
                or any(not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{40}", value[key])
                       for key in ("runtime_commit", "vibecomfy_commit"))):
            raise PaidSelectionError("lease compatibility requires exact recipe/model digests and source commits")
        return value


class RunPodSelectionOwner:
    def __init__(
        self, preferences: RunPodPreferences, *, account_ref: str, config_factory: Callable[[RunPodOffer], Any],
        observe_offer: Callable[[], RunPodOffer], observed_hourly_usd: Callable[[str], float],
        data_root: Path | None = None, clock: Callable[[], datetime] | None = None,
        compatibility: RunPodLeaseCompatibility | None = None,
        https_token_file: Path | None = None,
        sleep: Callable[[float], Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        progress: Callable[[Mapping[str, Any]], None] | None = None,
        observe_offers: Callable[[], tuple[RunPodOffer, ...]] | None = None,
    ) -> None:
        if account_ref != preferences.provider_account_ref:
            raise PaidSelectionError("authenticated account differs from selected preferences")
        self.preferences, self.config_factory = preferences, config_factory
        self.observe_offer, self.observed_hourly_usd = observe_offer, observed_hourly_usd
        self.path = canonical_claim_path(account_ref, data_root=data_root)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.compatibility = compatibility.as_mapping() if compatibility is not None else None
        self.https_token_file = https_token_file
        self.sleep, self.monotonic, self.progress = sleep, monotonic, progress
        self.observe_offers = observe_offers
        #: Optional zero-argument callback fired once when this owner's capacity wait begins.
        self.wait_hook: Callable[[], None] | None = None

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
            "network_volume_datacenter_id": offer.datacenter_id or p.datacenter_id,
            "accepted_gpu_type_id": offer.gpu_type_id,
        }
        if (not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f]{32}", operation)
                or record.get("request_name") != f"astrid-product-{operation}"
                or any(record.get(key) != value for key, value in expected.items())):
            raise AcquisitionUnresolvedError("journal does not prove this product's exact account/pod/image/volume custody")
        if record.get("lease_compatibility") != self.compatibility:
            raise AcquisitionUnresolvedError("retained lease has incompatible recipe/model/source requirements")
        if "lease" in record and self.compatibility is None:
            raise AcquisitionUnresolvedError("retained lease requires its original owner")

    def consented_offer(self, consent: PaidRunConsent, now: datetime,
                        *, offer: RunPodOffer | None = None) -> tuple[RunPodOffer, datetime]:
        """Observe the current offer and validate consent against it, before any claim or create.

        A quoted-digest mismatch refuses at zero cost and leaves one secret-free
        refusal record beside (never in) the custody journal.
        """
        try:
            offer = offer if offer is not None else self.observe_offer()
        except OfferObservationError as exc:
            raise OfferObservationError(exc.reason, retry_after=exc.retry_after) from None
        except Exception:
            raise OfferObservationError("observation_failed") from None
        observed = offer.digest(self.preferences)
        if consent.policy_digest is None and consent.offer_digest != observed:
            self._record_offer_refusal(offer, observed, consent.offer_digest, now)
            raise PaidSelectionError(
                "offer or rate changed; disclose it and obtain fresh paid consent "
                f"(quoted {str(consent.offer_digest)[:80]}, observed {observed})")
        return offer, consent.validate(offer, self.preferences, now, allow_policy=True)

    def _record_offer_refusal(self, offer: RunPodOffer, observed: str, quoted: Any, now: datetime) -> None:
        # Diagnostic only: a failure to write it never masks or changes the refusal.
        record = {"schema": "astrid.runpod.offer-refusal.v1", "refused_at": now.isoformat(),
                  "offer": asdict(offer), "run_offer_digest": observed,
                  "quoted_offer_digest": quoted if isinstance(quoted, str) else repr(quoted)[:80]}
        path = self.path.with_name("offer-refusal.json")
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.is_symlink() or path.parent.resolve() != path.parent:
                return
            temporary = path.with_name(f".offer-refusal.{uuid.uuid4().hex}.tmp")
            temporary.write_text(json.dumps(record, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
            temporary.replace(path)
        except (OSError, ValueError):
            pass

    async def select(self, consent: PaidRunConsent, *, dry_run: bool = False,
                     cancelled: Callable[[], bool] | None = None) -> Mapping[str, Any]:
        return await self._select(consent, dry_run=dry_run, cancelled=cancelled)

    async def _select(self, consent: PaidRunConsent, *, dry_run: bool = False,
                      lease: Mapping[str, Any] | None = None, lock_held: bool = False,
                      cancelled: Callable[[], bool] | None = None) -> Mapping[str, Any]:
        """One offer/one journaled POST per iteration; ambiguity never re-enters the loop."""
        consent.validate_authority(self.preferences, self.clock())
        if dry_run:
            return await self._select_once(consent, dry_run=True, lease=lease, lock_held=lock_held)
        self._read()
        started = self.monotonic()

        def check() -> None:
            if cancelled is not None and cancelled():
                raise CapacityWaitCancelled("RunPod capacity wait cancelled")
            if (consent.capacity_timeout_seconds is not None
                    and self.monotonic() - started >= consent.capacity_timeout_seconds):
                raise CapacityWaitTimeout("RunPod capacity timeout passed; no further create will be attempted", gate="capacity")
            if consent.deadline is not None and self.clock() >= _utc(consent.deadline):
                raise CapacityWaitTimeout("RunPod paid deadline passed; no further create will be attempted", gate="lifetime")

        async def interruptible(awaitable: Any, *, provider_timeout: float | None = None) -> Any:
            task = asyncio.ensure_future(awaitable)
            began = self.monotonic()
            try:
                while not task.done():
                    check()
                    if provider_timeout is not None and self.monotonic() - began >= provider_timeout:
                        raise OfferObservationError("provider_transport_or_response_unavailable")
                    await asyncio.wait({task}, timeout=0.05)
                result = await task
                check()
                return result
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        def progress(number: int, offer: RunPodOffer | None, result: str) -> None:
            row = {"attempt": number, "gpu": offer.gpu_type if offer else list(self.preferences.gpu_types),
                   "datacenter": offer.datacenter_id if offer else self.preferences.datacenter_id,
                   "result": result}
            if self.progress is not None:
                self.progress(row)
            else:
                print(f"RunPod capacity attempt {number}: GPU {row['gpu']}, datacenter {row['datacenter'] or 'eligible'}: {result}",
                      file=sys.stderr, flush=True)

        async def observe() -> RunPodOffer:
            try:
                return await asyncio.to_thread(self.observe_offer)
            except OfferObservationError:
                raise
            except Exception:
                raise OfferObservationError("observation_failed") from None

        async def observe_options() -> tuple[RunPodOffer, ...]:
            try:
                offers = await asyncio.to_thread(self.observe_offers)
                ordered = ordered_runpod_offers(self.preferences, offers)
                if not ordered:
                    raise OfferObservationError("stock_unavailable")
                return ordered
            except PaidSelectionError:
                raise
            except Exception:
                raise OfferObservationError("observation_failed") from None

        prefetched_offer = prefetched_failure = None
        initial = self._read()
        if consent.policy_digest is None and (initial is None or initial.get("state") == "cleanup_complete"):
            try:
                prefetched_offer = await interruptible(observe(), provider_timeout=30.0)
            except OfferObservationError as exc:
                prefetched_failure = exc
            else:
                # Preserve the exact-offer refusal before taking claim custody.
                self.consented_offer(consent, self.clock(), offer=prefetched_offer)

        with nullcontext() if lock_held else target_owner(self.path):
            number = 0
            rate_limit_failures = 0
            pending_offers: list[RunPodOffer] = []
            hook = getattr(self, "wait_hook", None)
            if hook is not None:
                try:
                    hook()
                except Exception:
                    pass  # A timing stamp never changes selection.
            while True:
                check()
                number += 1
                offer = None
                record = self._read()
                try:
                    # Retained custody and ambiguous prior POSTs are reconciled using
                    # their recorded offer, never gated on today's market stock.
                    if (record is not None and record.get("state") not in
                            {"cleanup_complete", "capacity_retry_ready", "capacity_exhausted"}):
                        offer = self._recorded_offer(record)
                    elif prefetched_failure is not None:
                        failure, prefetched_failure = prefetched_failure, None
                        raise failure
                    elif prefetched_offer is not None:
                        offer, prefetched_offer = prefetched_offer, None
                    elif consent.policy_digest is not None and self.observe_offers is not None:
                        if not pending_offers:
                            pending_offers.extend(await interruptible(observe_options(), provider_timeout=30.0))
                        offer = pending_offers.pop(0)
                    else:
                        offer = await interruptible(observe(), provider_timeout=30.0)
                    rate_limit_failures = 0
                    offer, _ = self.consented_offer(consent, self.clock(), offer=offer)
                    result = await interruptible(self._select_once(
                        consent, lease=lease, lock_held=True, offer=offer, cancelled=check))
                    progress(number, offer, "claimed")
                    return result
                except OfferObservationError as exc:
                    progress(number, offer, exc.reason)
                    if not consent.wait_for_capacity or exc.reason not in {"stock_unavailable", "provider_rate_limited"}:
                        raise
                    rate_limit_failures = rate_limit_failures + 1 if exc.reason == "provider_rate_limited" else 0
                    base_delay = min(60.0, 5.0 * 2 ** (rate_limit_failures - 1)) if rate_limit_failures else 5.0
                    delay = max(base_delay + random.uniform(0.0, 0.5), exc.retry_after or 0.0)
                except CapacityExhaustedError:
                    current = self._read()
                    if (current is None or current.get("pod_id") is not None
                            or current.get("state") not in {"capacity_retry_ready", "capacity_exhausted"}
                            or current.get("safe_to_retry") is not True
                            or current.get("capacity_rejection") != "ProviderCapacityUnavailable"):
                        raise AcquisitionUnresolvedError("capacity rejection lacks exact no-pod retry proof") from None
                    progress(number, offer, "capacity_rejected")
                    if not consent.wait_for_capacity:
                        raise
                    delay = 5.0 + random.uniform(0.0, 0.5)
                check()
                if consent.capacity_timeout_seconds is not None:
                    delay = min(delay, max(0.0, consent.capacity_timeout_seconds - (self.monotonic() - started)))
                if consent.deadline is not None:
                    delay = min(delay, max(0.0, (_utc(consent.deadline) - self.clock()).total_seconds()))
                await interruptible((self.sleep or asyncio.sleep)(delay))

    def _recorded_offer(self, record: Mapping[str, Any]) -> RunPodOffer:
        concrete = record.get("consented_offer")
        if isinstance(concrete, dict):
            return RunPodOffer(**concrete)
        limits = record.get("paid_limits") or {}

        @dataclass(frozen=True)
        class RecordedOffer(RunPodOffer):
            def digest(self, preferences: RunPodPreferences) -> str:
                # The original offer id was not stored by older journals. Its
                # exact accepted digest remains in the paid authority record.
                RunPodOffer.digest(self, preferences)
                return limits.get("offer_digest")

        return RecordedOffer(record.get("provider_account_ref"), record.get("gpu_type"),
                             limits.get("hourly_usd"), "retained-offer",
                             record.get("network_volume_datacenter_id"), record.get("accepted_gpu_type_id"))

    async def _select_once(self, consent: PaidRunConsent, *, dry_run: bool = False,
                           lease: Mapping[str, Any] | None = None, lock_held: bool = False,
                           offer: RunPodOffer | None = None,
                           cancelled: Callable[[], None] | None = None) -> Mapping[str, Any]:
        p, now = self.preferences, self.clock()
        if not dry_run and lease is None and (self.compatibility is not None or p.volume_id is None):
            raise PaidSelectionError("this target requires admission through the retained lease owner")
        offer, expires = self.consented_offer(consent, now, offer=offer)
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
                or config.volume_mount_path != p.mount_path
                or getattr(config, "network_volume_id", None) != p.volume_id
                or getattr(config, "data_center_id", None) != (offer.datacenter_id or p.datacenter_id)
                or getattr(config, "gpu_type_id", None) != offer.gpu_type_id
                or getattr(config, "storage_name", None) or getattr(config, "storage_volumes", ())):
            raise PaidSelectionError("provider configuration differs from the consented exact placement")
        # Restrict lifecycle's launch matrix to the consented target so each
        # journaled attempt can issue at most one create POST.
        config = copy.copy(config)
        config.ram_tiers_enabled = False
        datacenter = offer.datacenter_id or p.datacenter_id
        self._read()  # Reject legacy/symlink custody before creating a lock file.
        with nullcontext() if lock_held else target_owner(self.path):
            record = self._read()
            completed_operation = None
            if record is not None and record.get("state") == "cleanup_complete" and lease is not None:
                _validate_completed_release(record)
                completed_operation = record["operation_id"]
                record = None
            token_reference = {}
            if self.https_token_file is not None:
                from runpod_lifecycle.https import read_token_file, write_token_file
                token_path = self.https_token_file
                if (not token_path.is_absolute() or token_path.parent != self.path.parent
                        or token_path.parent.resolve() != token_path.parent):
                    raise PaidSelectionError("HTTPS token reference must be in the owned account directory")
                if record is not None and record.get("https_token_file") != str(token_path):
                    raise AcquisitionUnresolvedError("HTTPS custody reference changed")
                if config.transport != "https":
                    raise PaidSelectionError("retained preparation requires HTTPS transport")
                if not token_path.exists():
                    if record is not None:
                        raise AcquisitionUnresolvedError("retained HTTPS token is missing")
                    write_token_file(token_path, config.https_token)
                config = config.merge(https_token=read_token_file(token_path))
                token_reference = {"https_token_file": str(token_path)}
            try:
                observed_volume = (await resolve_existing_volume(config.api_key, p.volume_id)
                                   if p.volume_id is not None else None)
                inspect_volume_records(RunPodTargetContract(
                    "unclaimed", offer.gpu_type, p.image, p.volume_id, p.volume_name,
                    p.volume_size_gb, datacenter_id=datacenter,
                ), volumes=[observed_volume] if observed_volume is not None else [])
            except Exception:
                raise AcquisitionUnresolvedError("existing account volume cannot be confirmed before claim or reuse") from None
            if record is not None:
                safe_capacity = (record.get("state") in {"capacity_retry_ready", "capacity_exhausted"}
                                 and record.get("safe_to_retry") is True and not record.get("pod_id")
                                 and record.get("capacity_rejection") == "ProviderCapacityUnavailable")
                if safe_capacity:
                    self._owned(record, self._recorded_offer(record))
                    if record.get("paid_limits", {}).get("invocation_id") != consent.invocation_id:
                        raise AcquisitionUnresolvedError("stale capacity intent requires owner recovery before another invocation")
                    record = {**record, "gpu_type": offer.gpu_type, "accepted_gpu_type_id": offer.gpu_type_id,
                              "network_volume_datacenter_id": datacenter, "consented_offer": asdict(offer)}
                self._owned(record, offer)
                if "lease" in record and lease is None:
                    raise AcquisitionUnresolvedError("retained lease requires admission through its owner")
                if record.get("state") not in {
                    "claimed", "allocation_ready", "allocation_pending", "allocation_unknown", "allocated_unverified",
                    "capacity_retry_ready", "capacity_exhausted",
                }:
                    raise AcquisitionUnresolvedError("pending cleanup or expired custody prevents admission or renewal")
                previous = record.get("paid_limits", {})
                if previous.get("expires_at") and not safe_capacity:
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
                    "accepted_gpu_type_id": offer.gpu_type_id,
                    "network_volume_id": p.volume_id, "network_volume_name": p.volume_name,
                    "network_volume_size_gb": p.volume_size_gb, "volume_mount_path": p.mount_path,
                    "network_volume_datacenter_id": datacenter,
                    "handle_path": str(self.path), "transport": "https",
                    "reconciliation_required": True,
                    "consented_offer": asdict(offer),
                    "paid_limits": {"invocation_id": consent.invocation_id, "offer_digest": offer.digest(p),
                                    "hourly_usd": offer.hourly_usd, "max_usd": consent.max_usd,
                                    "deadline": consent.deadline, "expires_at": expires.isoformat(),
                                    "authorized_at": now.isoformat(), "idle_seconds": consent.idle_seconds},
                    "storage_mode": "network_volume" if p.volume_id is not None else "ephemeral",
                    **token_reference,
                    **({"lease_compatibility": self.compatibility} if self.compatibility is not None else {}),
                    **({"lease": dict(lease)} if lease is not None else {}),
                }
                if completed_operation is None:
                    create_target_record_exclusive(self.path, record)
                else:
                    # The enclosing account lock excludes another claimant.
                    # Publish the next intent in the same journal, after the
                    # previous exact release receipt has been validated.
                    write_target_record(self.path, record)
            volume = {"id": p.volume_id, "name": p.volume_name, "size": p.volume_size_gb,
                      "dataCenterId": datacenter}
            limits = {"invocation_id": consent.invocation_id, "offer_digest": offer.digest(p),
                      "hourly_usd": offer.hourly_usd, "max_usd": consent.max_usd,
                      "deadline": consent.deadline, "expires_at": expires.isoformat(),
                      "authorized_at": record.get("paid_limits", {}).get("authorized_at", now.isoformat()),
                      "idle_seconds": consent.idle_seconds,
                      **({"policy_digest": consent.policy_digest, "max_hourly_usd": consent.max_hourly_usd}
                         if consent.policy_digest is not None else {})}
            if record["state"] == "claimed":
                try:
                    observation = await observe_exact_target(config.api_key, RunPodTargetContract(
                        str(record["pod_id"]), offer.gpu_type_id or offer.gpu_type, p.image, p.volume_id, p.volume_name,
                        p.volume_size_gb, record["request_name"], datacenter, defer_machine=True,
                    ))
                except Exception:
                    raise AcquisitionUnresolvedError("exact journal-owned pod observation is unresolved") from None
                acquired_record = dict(record)
            else:
                # Paid limits survive a lost create response and process restart.
                safe_capacity = (record["state"] in {"capacity_retry_ready", "capacity_exhausted"}
                                 and record.get("safe_to_retry") is True
                                 and record.get("capacity_rejection") == "ProviderCapacityUnavailable")
                if not safe_capacity and (record["state"] != "allocation_ready" or record.get("create_attempts", 0)):
                    limits = dict(record["paid_limits"])
                    expires = _utc(limits["expires_at"])
                record = {**record, "paid_limits": limits}
                replace_target_record(self.path, record, operation_id=record["operation_id"])
                # These are the existing decorated implementations. This scope
                # already owns their exact .owner.lock; do not acquire it twice.
                from runpod_lifecycle import NotReadyTimeout
                try:
                    if ((record["state"] == "allocation_ready" and record.get("create_attempts") == 0)
                            or safe_capacity):
                        def before_post(pending):
                            if cancelled is not None:
                                cancelled()
                            stamp = self.clock()
                            paid_expiry = consent.validate(offer, p, stamp, allow_policy=True)
                            paid = {**limits, "authorized_at": stamp.isoformat(), "expires_at": paid_expiry.isoformat()}
                            return {**pending, "paid_limits": paid, "consented_offer": asdict(offer),
                                    "last_create_started_at": stamp.isoformat(),
                                    **({"lease": {**pending["lease"], "started_at": stamp.timestamp()}}
                                       if "lease" in pending else {})}

                        acquired = await acquire_target.__wrapped__(
                            config, handle_path=self.path, attempt=record, volume=volume, pod_id=None,
                            max_wait_seconds=max(1, int((expires - now).total_seconds())),
                            capacity_window_seconds=60, retry_interval_seconds=15,
                            max_create_attempts=record.get("create_attempts", 0) + 1,
                            allow_capacity_retry=safe_capacity,
                            single_attempt=True, before_post=before_post,
                            ready_timeout_seconds=max(1, min(_READINESS_TIMEOUT_SECONDS, int((expires - self.clock()).total_seconds()))),
                        )
                    else:
                        acquired = await reconcile_target.__wrapped__(
                            config, handle_path=self.path, attempt=record, volume=volume,
                            ready_timeout_seconds=max(1, min(_READINESS_TIMEOUT_SECONDS, int((expires - self.clock()).total_seconds()))),
                        )
                except CapacityExhaustedError:
                    raise
                except NotReadyTimeout:
                    raise AcquisitionUnresolvedError(
                        "RunPod worker did not become ready within the startup timeout; "
                        "the same journal retains exact pod custody for recovery cleanup"
                    ) from None
                except Exception as exc:
                    gate = worker_preparation.observation_failure_gate(exc)
                    detail = f"; failed gate: {gate}" if gate is not None else ""
                    raise AcquisitionUnresolvedError(
                        "provider acquisition remains unresolved; reconcile the same journal" + detail,
                        gate=gate,
                    ) from None
                acquired_record, observation = dict(acquired.attempt), acquired.observation
                # The successful (or reconciled) attempt owns the durable clock.
                # Readiness and later invocations never restart that paid window.
                limits = dict(acquired_record["paid_limits"])
                expires = _utc(limits["expires_at"])
            try:
                status, exact = await asyncio.to_thread(read_exact_pod, config.api_key, observation.pod_id)
                if (status != 200 or exact is None
                        or (p.volume_id is not None and exact.get("volumeMountPath") != p.mount_path)
                        or exact.get("networkVolumeId") != p.volume_id):
                    raise PaidSelectionError("provider did not confirm the exact volume mount", gate="volume")
                # GPU and datacenter come from GraphQL pod.machine and the provider gpuTypes
                # catalog (REST reports machine {} and no gpuTypeId/dataCenterId on a correct
                # fresh pod). Unreported or unequal fails closed; no REST fallback.
                gpu_display, observed_datacenter = await asyncio.to_thread(
                    worker_preparation.confirm_acquired_machine, config.api_key,
                    RunPodTargetContract(
                        observation.pod_id, offer.gpu_type_id or offer.gpu_type, p.image, p.volume_id,
                        p.volume_name, p.volume_size_gb, record["request_name"], datacenter,
                    ),
                    timeout_seconds=max(1, min(_READINESS_TIMEOUT_SECONDS, int((expires - self.clock()).total_seconds()))),
                )
                observation = replace(observation, gpu_type=gpu_display, datacenter_id=observed_datacenter)
                try:
                    gpu_count = await asyncio.to_thread(
                        worker_preparation.read_acquired_gpu_count, config.api_key, observation.pod_id)
                    observed_rate = self.observed_hourly_usd(observation.pod_id)
                except TargetObservationError as exc:
                    raise PaidSelectionError(str(exc), gate="rate") from None
                except Exception:
                    raise PaidSelectionError("actual hourly USD rate could not be read", gate="rate") from None
                if not pod_rate_within_offer(observed_rate, offer.hourly_usd, gpu_count):
                    raise PaidSelectionError(
                        "actual pod rate exceeds the offer rate times GPU count; fresh consent is required before work",
                        gate="rate")
                if self.clock() >= expires:
                    raise PaidSelectionError("claim preparation consumed its paid lifetime", gate="lifetime")
            except Exception as exc:
                # Every gate names itself; a failure outside any gate is reported as such.
                gate = getattr(exc, "gate", None) or "unclassified"
                pending = {**acquired_record, "state": "cleanup_pending", "reconciliation_required": True,
                           "failed_gate": gate}
                replace_target_record(self.path, pending, operation_id=record["operation_id"])
                # Our own gate messages are fixed, secret-free text; other exceptions are named by type only.
                detail = (str(exc) if isinstance(exc, TargetObservationError)
                          else f"unexpected {type(exc).__name__}")
                raise PaidSelectionError(
                    f"unconfirmed {gate} gate: {detail}; exact claim requires cleanup", gate=gate) from None
            claimed = {
                **acquired_record, "schema_version": "astrid.runpod.claim.v1", "state": "claimed",
                "pod_id": observation.pod_id, "name": observation.pod_name,
                "claimed_at": record.get("claimed_at", self.clock().isoformat()),
                "provider_observation": observation.to_dict(), "target_identity_verified": True,
                "paid_limits": limits, "reconciliation_required": False,
            }
            replace_target_record(self.path, claimed, operation_id=record["operation_id"])
            return claimed
