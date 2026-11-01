"""Canonical timeline event envelope and lifecycle payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# Per-domain payload dataclasses. These were split out of this module into the
# ``payloads`` subpackage; they are imported and re-exported here so that the
# ``PayloadModel`` union, ``_PAYLOAD_TYPES`` dispatch, and all existing import
# paths (projection.py, inverses.py, the schema package __init__, etc.) continue
# to resolve them from ``...schema.types`` unchanged.
from .payloads import (
    ArrangementReplacedPayload,
    AssetRegistryReplacedPayload,
    AudioBoundPayload,
    AudioUnboundPayload,
    ClipAddedPayload,
    ClipAnnotatedPayload,
    ClipMovedPayload,
    ClipRemovedPayload,
    ClipReplacedPayload,
    ClipRetimedPayload,
    ClipRetrackedPayload,
    ClipSwappedPayload,
    ClipTextSetPayload,
    EffectAddedPayload,
    EffectRemovedPayload,
    EffectTunedPayload,
    ErasedPayload,
    PoolAssetAddedPayload,
    PoolAssetRemovedPayload,
    PoolAssetScoredPayload,
    ThemeOverriddenPayload,
    ThemeSetPayload,
    TimelineAssetRegistryReplacedPayload,
    TimelineBranchedFromPayload,
    TimelineConfigReplacedPayload,
    TimelineCreatedPayload,
    TimelineDefaultSetPayload,
    TimelineDeletedPayload,
    TimelineErasedPayload,
    TimelineRecoveredPayload,
    TimelineRenamedPayload,
    TimelineRevertedPayload,
    TimelineTombstonedPayload,
    TrackAddedPayload,
    TrackRemovedPayload,
    TransitionRemovedPayload,
    TransitionSetPayload,
)

# Shared leaf types, validators, and coercion helpers. Re-exported here so that
# ``from astrid.core.timeline.events.schema.types import <name>`` keeps working
# for every historical consumer.
from .payloads._base import (
    ActorType,
    ClipKind as ClipKind,
    ClipPosition as ClipPosition,
    TimelineEventSchemaError,
    TimelineImportSource as TimelineImportSource,
    TrackKind as TrackKind,
    _coerce_clip_position as _coerce_clip_position,
    _require_nonempty_str,
    _require_uuid_str,
    _require_ulid_str,
    _validate_jsonable,
)
from .ulid import generate_event_ulid, is_event_ulid as is_event_ulid

EVENT_SCHEMA_VERSION = 2

TimelineEventKind = Literal[
    "timeline.created",
    "timeline.renamed",
    "timeline.default_set",
    "timeline.tombstoned",
    "timeline.deleted",
    "timeline.config_replaced",
    "timeline.asset_registry_replaced",
    "timeline.recovered",
    "timeline.reverted",
    "timeline.branched_from",
    "timeline.erased",
    "clip.added",
    "clip.removed",
    "clip.moved",
    "clip.retracked",
    "clip.retimed",
    "clip.swapped",
    "clip.replaced",
    "clip.text_set",
    "clip.annotated",
    "transition.set",
    "transition.removed",
    "effect.added",
    "effect.removed",
    "effect.tuned",
    "theme.set",
    "theme.overridden",
    "track.added",
    "track.removed",
    "audio.bound",
    "audio.unbound",
    "pool.asset_added",
    "pool.asset_removed",
    "pool.asset_scored",
    "arrangement.replaced",
    "timeline.asset_registry_replaced",
]


@dataclass(frozen=True)
class TimelineActor:
    type: ActorType
    id: str
    display: str | None = None
    via: list["TimelineActor"] | None = None

    def __post_init__(self) -> None:
        if self.type not in {"agent", "human", "system"}:
            raise TimelineEventSchemaError("actor.type must be one of agent, human, system")
        _require_nonempty_str(self.id, "actor.id")
        if self.display is not None:
            _require_nonempty_str(self.display, "actor.display")
        if self.via is not None:
            if not isinstance(self.via, list):
                raise TimelineEventSchemaError("actor.via must be a list when present")
            for index, actor in enumerate(self.via):
                if not isinstance(actor, TimelineActor):
                    raise TimelineEventSchemaError(
                        f"actor.via[{index}] must be a TimelineActor"
                    )

    def to_json_obj(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type, "id": self.id}
        if self.display is not None:
            payload["display"] = self.display
        if self.via is not None:
            payload["via"] = [actor.to_json_obj() for actor in self.via]
        return payload

    @classmethod
    def from_dict(cls, raw: object) -> "TimelineActor":
        if not isinstance(raw, dict):
            raise TimelineEventSchemaError("actor must be an object")
        via = raw.get("via")
        nested = None if via is None else [cls.from_dict(item) for item in via]
        return cls(
            type=raw.get("type"),  # type: ignore[arg-type]
            id=raw.get("id"),  # type: ignore[arg-type]
            display=raw.get("display"),  # type: ignore[arg-type]
            via=nested,
        )


_ERASED_PAYLOAD_FIELDS = frozenset({"erased", "reason", "erased_at", "erased_by", "policy_ref"})


PayloadModel = (
    TimelineCreatedPayload
    | TimelineRenamedPayload
    | TimelineDefaultSetPayload
    | TimelineTombstonedPayload
    | TimelineDeletedPayload
    | TimelineConfigReplacedPayload
    | TimelineAssetRegistryReplacedPayload
    | TimelineRecoveredPayload
    | TimelineRevertedPayload
    | TimelineBranchedFromPayload
    | TimelineErasedPayload
    | ErasedPayload
    | ClipAddedPayload
    | ClipRemovedPayload
    | ClipMovedPayload
    | ClipRetrackedPayload
    | ClipRetimedPayload
    | ClipSwappedPayload
    | ClipReplacedPayload
    | ClipTextSetPayload
    | ClipAnnotatedPayload
    | TransitionSetPayload
    | TransitionRemovedPayload
    | EffectAddedPayload
    | EffectRemovedPayload
    | EffectTunedPayload
    | ThemeSetPayload
    | ThemeOverriddenPayload
    | TrackAddedPayload
    | TrackRemovedPayload
    | AudioBoundPayload
    | AudioUnboundPayload
    | PoolAssetAddedPayload
    | PoolAssetRemovedPayload
    | PoolAssetScoredPayload
    | ArrangementReplacedPayload
    | AssetRegistryReplacedPayload
)


_PAYLOAD_TYPES: dict[str, type[PayloadModel]] = {
    "timeline.created": TimelineCreatedPayload,
    "timeline.renamed": TimelineRenamedPayload,
    "timeline.default_set": TimelineDefaultSetPayload,
    "timeline.tombstoned": TimelineTombstonedPayload,
    "timeline.deleted": TimelineDeletedPayload,
    "timeline.config_replaced": TimelineConfigReplacedPayload,
    "timeline.asset_registry_replaced": TimelineAssetRegistryReplacedPayload,
    "timeline.recovered": TimelineRecoveredPayload,
    "timeline.reverted": TimelineRevertedPayload,
    "timeline.branched_from": TimelineBranchedFromPayload,
    "timeline.erased": TimelineErasedPayload,
    "clip.added": ClipAddedPayload,
    "clip.removed": ClipRemovedPayload,
    "clip.moved": ClipMovedPayload,
    "clip.retracked": ClipRetrackedPayload,
    "clip.retimed": ClipRetimedPayload,
    "clip.swapped": ClipSwappedPayload,
    "clip.replaced": ClipReplacedPayload,
    "clip.text_set": ClipTextSetPayload,
    "clip.annotated": ClipAnnotatedPayload,
    "transition.set": TransitionSetPayload,
    "transition.removed": TransitionRemovedPayload,
    "effect.added": EffectAddedPayload,
    "effect.removed": EffectRemovedPayload,
    "effect.tuned": EffectTunedPayload,
    "theme.set": ThemeSetPayload,
    "theme.overridden": ThemeOverriddenPayload,
    "track.added": TrackAddedPayload,
    "track.removed": TrackRemovedPayload,
    "audio.bound": AudioBoundPayload,
    "audio.unbound": AudioUnboundPayload,
    "pool.asset_added": PoolAssetAddedPayload,
    "pool.asset_removed": PoolAssetRemovedPayload,
    "pool.asset_scored": PoolAssetScoredPayload,
    "arrangement.replaced": ArrangementReplacedPayload,
    "timeline.asset_registry_replaced": AssetRegistryReplacedPayload,
}


def payload_to_json_obj(payload: PayloadModel | dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, dict):
        _validate_jsonable(payload, "payload")
        return dict(payload)
    return payload.to_json_obj()


def _is_erased_payload_dict(payload: object) -> bool:
    """Return True when *payload* is a dict whose ``erased`` key is truthy."""
    return isinstance(payload, dict) and bool(payload.get("erased"))


def _has_mixed_erased_and_domain_fields(payload: dict[str, Any]) -> bool:
    """Return True when *payload* mixes erased-envelope keys with domain-specific keys."""
    erased_keys = _ERASED_PAYLOAD_FIELDS
    extra_keys = set(payload.keys()) - erased_keys
    return len(extra_keys) > 0


def coerce_payload(kind: str, payload: PayloadModel | dict[str, Any]) -> PayloadModel | dict[str, Any]:
    # ------------------------------------------------------------------
    # Erased payload envelope: accepted for ANY event kind (including
    # timeline.erased itself) BEFORE per-kind coercion.
    # ------------------------------------------------------------------
    if isinstance(payload, ErasedPayload):
        return payload
    if _is_erased_payload_dict(payload):
        assert isinstance(payload, dict)
        if _has_mixed_erased_and_domain_fields(payload):
            raise TimelineEventSchemaError(
                "erased payload must not include domain-specific fields; "
                "only erased, reason, erased_at, erased_by, and optional policy_ref are allowed"
            )
        return ErasedPayload(
            erased=True,
            reason=payload["reason"],
            erased_at=payload["erased_at"],
            erased_by=payload["erased_by"],
            policy_ref=payload.get("policy_ref"),
        )

    # Normal per-kind coercion
    model_type = _PAYLOAD_TYPES.get(kind)
    if model_type is None:
        if not isinstance(payload, dict):
            raise TimelineEventSchemaError("payload must be an object")
        _validate_jsonable(payload, "payload")
        return dict(payload)
    if isinstance(payload, model_type):
        return payload
    if not isinstance(payload, dict):
        raise TimelineEventSchemaError("payload must be an object")
    if model_type is TimelineDeletedPayload:
        if payload:
            raise TimelineEventSchemaError("timeline.deleted payload must be an empty object")
        return TimelineDeletedPayload()
    return model_type(**payload)


@dataclass(frozen=True)
class TimelineEvent:
    event_id: str
    timeline_id: str
    ts: str
    actor: TimelineActor
    prev_hash: str | None
    hash: str | None
    kind: str
    payload: PayloadModel | dict[str, Any]
    expected_version: int | None = None
    schema_version: int = EVENT_SCHEMA_VERSION
    txn_id: str | None = None
    # --- import metadata (cross-backend transfer provenance) ---
    source_backend: str | None = None
    source_timeline_id: str | None = None
    source_event_id: str | None = None
    source_version: int | None = None
    source_hash: str | None = None

    def __post_init__(self) -> None:
        _require_ulid_str(self.event_id, "event_id")
        _require_nonempty_str(self.timeline_id, "timeline_id")
        _require_nonempty_str(self.ts, "ts")
        if not isinstance(self.actor, TimelineActor):
            raise TimelineEventSchemaError("actor must be a TimelineActor")
        if self.prev_hash is not None:
            _require_nonempty_str(self.prev_hash, "prev_hash")
        if self.hash is not None:
            _require_nonempty_str(self.hash, "hash")
        _require_nonempty_str(self.kind, "kind")
        object.__setattr__(self, "payload", coerce_payload(self.kind, self.payload))
        if self.expected_version is not None and (
            not isinstance(self.expected_version, int) or isinstance(self.expected_version, bool)
        ):
            raise TimelineEventSchemaError("expected_version must be an integer when present")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
        ):
            raise TimelineEventSchemaError("schema_version must be an integer")
        if self.schema_version > EVENT_SCHEMA_VERSION:
            raise TimelineEventSchemaError(
                f"schema_version must be <= {EVENT_SCHEMA_VERSION}"
            )
        if self.txn_id is not None:
            _require_ulid_str(self.txn_id, "txn_id")
        # Validate import metadata fields when present
        if self.source_backend is not None:
            _require_nonempty_str(self.source_backend, "source_backend")
        if self.source_timeline_id is not None:
            _require_nonempty_str(self.source_timeline_id, "source_timeline_id")
        if self.source_event_id is not None:
            _require_nonempty_str(self.source_event_id, "source_event_id")
        if self.source_version is not None:
            if not isinstance(self.source_version, int) or isinstance(self.source_version, bool):
                raise TimelineEventSchemaError("source_version must be an integer when present")
        if self.source_hash is not None:
            _require_nonempty_str(self.source_hash, "source_hash")

    def to_json_obj(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "event_id": self.event_id,
            "timeline_id": self.timeline_id,
            "ts": self.ts,
            "actor": self.actor.to_json_obj(),
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "kind": self.kind,
            "payload": payload_to_json_obj(self.payload),
            "expected_version": self.expected_version,
            "schema_version": self.schema_version,
            "txn_id": self.txn_id,
        }
        if self.source_backend is not None:
            result["source_backend"] = self.source_backend
        if self.source_timeline_id is not None:
            result["source_timeline_id"] = self.source_timeline_id
        if self.source_event_id is not None:
            result["source_event_id"] = self.source_event_id
        if self.source_version is not None:
            result["source_version"] = self.source_version
        if self.source_hash is not None:
            result["source_hash"] = self.source_hash
        return result

    @classmethod
    def new(
        cls,
        *,
        timeline_id: str,
        ts: str,
        actor: TimelineActor,
        kind: TimelineEventKind,
        payload: PayloadModel | dict[str, Any],
        prev_hash: str | None = None,
        expected_version: int | None = None,
        txn_id: str | None = None,
        source_backend: str | None = None,
        source_timeline_id: str | None = None,
        source_event_id: str | None = None,
        source_version: int | None = None,
        source_hash: str | None = None,
    ) -> "TimelineEvent":
        return cls(
            event_id=generate_event_ulid(),
            timeline_id=timeline_id,
            ts=ts,
            actor=actor,
            prev_hash=prev_hash,
            hash=None,
            kind=kind,
            payload=payload,
            expected_version=expected_version,
            txn_id=txn_id,
            source_backend=source_backend,
            source_timeline_id=source_timeline_id,
            source_event_id=source_event_id,
            source_version=source_version,
            source_hash=source_hash,
        )

    @classmethod
    def from_dict(cls, raw: object) -> "TimelineEvent":
        if not isinstance(raw, dict):
            raise TimelineEventSchemaError("event must be an object")
        return cls(
            event_id=raw.get("event_id"),  # type: ignore[arg-type]
            timeline_id=raw.get("timeline_id"),  # type: ignore[arg-type]
            ts=raw.get("ts"),  # type: ignore[arg-type]
            actor=TimelineActor.from_dict(raw.get("actor")),
            prev_hash=raw.get("prev_hash"),  # type: ignore[arg-type]
            hash=raw.get("hash"),  # type: ignore[arg-type]
            kind=raw.get("kind"),  # type: ignore[arg-type]
            payload=raw.get("payload"),  # type: ignore[arg-type]
            expected_version=raw.get("expected_version"),  # type: ignore[arg-type]
            schema_version=raw.get("schema_version"),  # type: ignore[arg-type]
            txn_id=raw.get("txn_id"),  # type: ignore[arg-type]
            source_backend=raw.get("source_backend"),  # type: ignore[arg-type]
            source_timeline_id=raw.get("source_timeline_id"),  # type: ignore[arg-type]
            source_event_id=raw.get("source_event_id"),  # type: ignore[arg-type]
            source_version=raw.get("source_version"),  # type: ignore[arg-type]
            source_hash=raw.get("source_hash"),  # type: ignore[arg-type]
        )
