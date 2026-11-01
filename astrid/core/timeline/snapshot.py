"""Pure, event-sourced timeline snapshots.

A snapshot is projected and verified from one in-memory runtime event read.
The runtime materialization is the only timeline authority; filesystem
timeline paths, local event-log backends, and repair sidecars are not read.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from astrid.core.timeline.events.schema import (
    TimelineEvent,
    with_event_hash,
)
from astrid.core.timeline.projection import project_to_assembly
from astrid.core.timeline.resolution import classify_asset
from astrid.core.timeline.snapshot_digest import (
    SNS_SCHEMA_VERSION,
    canonical_json_bytes,
    sha256_bytes,
    sns_digest,
)
from astrid.core.timeline.validate import (
    validate_structural,
)

_REGISTRY_EVENT_KIND = "timeline.asset_registry_replaced"
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


class ConcurrentAppendError(RuntimeError):
    """Raised when a stable event-log generation cannot be acquired."""


class SnapshotIntegrityError(RuntimeError):
    """Raised when authoritative snapshot input fails integrity validation."""


@dataclass(frozen=True)
class TimelineSnapshot:
    """One deterministic, read-only view of a timeline event generation.

    ``project_slug`` and ``diagnostics`` are explicit additions to the core
    fields: the SNS v1 envelope requires the former, while stale-cache and
    skipped-media states require somewhere deterministic to be recorded.
    They do not otherwise broaden the authority of compatibility sidecars.
    """

    timeline_id: str
    timeline_ulid: str
    slug: str | None
    project_slug: str
    head_version: int
    last_event_id: str | None
    last_hash: str | None
    assembly: dict[str, Any]
    registry: dict[str, Any]
    display: dict[str, Any] | None
    events: list[dict[str, Any]]
    media_hashes: dict[str, str]
    assembly_sha256: str
    registry_sha256: str
    transcript_sha256: str | None
    diagnostics: tuple[str, ...] = ()

    def sns(self) -> str:
        """Return the canonical source-normalized-snapshot identity."""

        fields: dict[str, Any] = {
            "schema_version": SNS_SCHEMA_VERSION,
            "project_slug": self.project_slug,
            "timeline_uuid": self.timeline_id,
            "timeline_ulid": self.timeline_ulid,
            "head_version": self.head_version,
            "head_last_event_id": self.last_event_id,
            "head_last_hash": self.last_hash,
            "assembly_sha256": self.assembly_sha256,
            "registry_sha256": self.registry_sha256,
            "media_hashes": self.media_hashes,
        }
        if self.transcript_sha256 is not None:
            fields["transcript_sha256"] = self.transcript_sha256
        return sns_digest(fields)


def snapshot_from_runtime(
    *,
    timeline_id: str,
    timeline_ulid: str,
    slug: str,
    project_slug: str,
    events: Sequence[dict[str, Any]],
    project_ref: str | None = None,
    runtime_client: Any | None = None,
    media_snapshot: Any | None = None,
) -> TimelineSnapshot:
    """Build a read-only snapshot from a runtime materialization.

    The generated-client timeline row is the authority.  ``events`` is an
    attempt-local, in-memory normalization supplied by the visualization
    adapter; this path never opens a project directory or repairs a sidecar.
    """
    if not isinstance(timeline_id, str) or not timeline_id:
        raise SnapshotIntegrityError("runtime timeline_id must be a non-empty string")
    if not isinstance(timeline_ulid, str) or _ULID_RE.fullmatch(timeline_ulid) is None:
        raise SnapshotIntegrityError("runtime timeline_ulid must be an uppercase canonical ULID")
    raw_events, parsed_events = _parse_events(list(events))
    chain_errors = _chain_diagnostics(parsed_events, timeline_id=timeline_id)
    if chain_errors:
        raise SnapshotIntegrityError("; ".join(chain_errors))
    return _build_snapshot(
        raw_events,
        parsed_events,
        timeline_id=timeline_id,
        timeline_ulid=timeline_ulid,
        display={"schema_version": 1, "slug": slug, "name": slug, "is_default": False},
        slug=slug,
        project_slug=project_slug,
        project_ref=project_ref or project_slug,
        diagnostics=(),
        runtime_client=runtime_client,
        media_snapshot=media_snapshot,
    )


def _dedupe_diagnostics(items: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _parse_events(
    events: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[TimelineEvent]]:
    if not isinstance(events, list):
        raise SnapshotIntegrityError("events must be a list")
    copied = deepcopy(events)
    parsed: list[TimelineEvent] = []
    for index, raw in enumerate(copied):
        try:
            parsed.append(TimelineEvent.from_dict(raw))
        except (TypeError, ValueError) as exc:
            raise SnapshotIntegrityError(
                f"event {index + 1} is schema-invalid: {exc}"
            ) from exc
    return copied, parsed


def _chain_diagnostics(
    events: Sequence[TimelineEvent],
    *,
    timeline_id: str,
) -> list[str]:
    """Verify the captured runtime event objects as one in-memory hash chain."""

    diagnostics: list[str] = []
    previous_hash: str | None = None
    for index, event in enumerate(events, start=1):
        if event.timeline_id != timeline_id:
            diagnostics.append(
                "EVENT_TIMELINE_MISMATCH: "
                f"event {index} {event.event_id} has timeline_id {event.timeline_id}, "
                f"expected {timeline_id}"
            )
        try:
            unhashed = TimelineEvent.from_dict(
                {**event.to_json_obj(), "hash": None}
            )
            expected = with_event_hash(unhashed, prev_hash=previous_hash)
        except (TypeError, ValueError) as exc:
            diagnostics.append(
                f"EVENT_HASH_UNVERIFIABLE: event {index} {event.event_id}: {exc}"
            )
            previous_hash = event.hash
            continue
        if event.prev_hash != previous_hash:
            diagnostics.append(
                "EVENT_PREV_HASH_MISMATCH: "
                f"event {index} {event.event_id} expected {previous_hash!r}, "
                f"found {event.prev_hash!r}"
            )
        if event.hash != expected.hash:
            diagnostics.append(
                "EVENT_HASH_MISMATCH: "
                f"event {index} {event.event_id} expected {expected.hash!r}, "
                f"found {event.hash!r}"
            )
        previous_hash = event.hash
    return diagnostics


def _registry_from_events(
    events: Sequence[TimelineEvent],
) -> tuple[dict[str, Any], list[str]]:
    """Return the last full registry event, mirroring the bridge reverse scan.

    The bridge helper is intentionally not called: its surrounding recovery
    path persists ``registry.json``.  The event carries a complete registry,
    so a reverse scan over the already captured events is sufficient.
    """

    skipped_replacements = 0
    for event in reversed(events):
        if event.kind != _REGISTRY_EVENT_KIND:
            continue
        registry = getattr(event.payload, "registry", None)
        if not isinstance(registry, dict):
            # Erasure repair keeps the original event kind but replaces its
            # payload. Match the event-stream registry projection by
            # continuing to the newest still-usable full replacement.
            skipped_replacements += 1
            continue
        copied = deepcopy(registry)
        try:
            _validate_registry_envelope(copied)
        except (TypeError, ValueError) as exc:
            raise SnapshotIntegrityError(
                f"event {event.event_id} registry is invalid: {exc}"
            ) from exc
        diagnostics = []
        if skipped_replacements:
            diagnostics.append(
                "REGISTRY_REPLACEMENT_SKIPPED: "
                f"ignored {skipped_replacements} erased or unusable newer replacement(s)"
            )
        return copied, diagnostics
    return {"assets": {}}, [
        "REGISTRY_EVENT_MISSING: no timeline.asset_registry_replaced event; "
        "using an empty registry"
    ]


def _validate_registry_envelope(registry: Any) -> None:
    """Validate the event-owned envelope without narrowing asset metadata.

    The generic render registry validator intentionally rejects bridge
    provenance fields such as ``sourceId`` and ``sourceVersion`` that are
    present in canonical event history.  Snapshot authority must preserve
    those fields losslessly for R5, so only the stable full-registry shape and
    canonical JSON compatibility are checked here.
    """

    if not isinstance(registry, dict):
        raise ValueError("registry must be an object")
    assets = registry.get("assets")
    if not isinstance(assets, dict):
        raise ValueError("registry.assets must be an object")
    for asset_key, entry in assets.items():
        if not isinstance(asset_key, str) or not asset_key:
            raise ValueError("registry asset keys must be non-empty strings")
        if not isinstance(entry, dict):
            raise ValueError(f"registry asset {asset_key!r} must be an object")
    canonical_json_bytes(registry)


def _canonical_digest(value: Any) -> str:
    try:
        return sha256_bytes(canonical_json_bytes(value))
    except (TypeError, ValueError) as exc:
        raise SnapshotIntegrityError(f"value is not canonical JSON: {exc}") from exc


def _project_canonical_composition(
    assembly: Mapping[str, Any], registry: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply the editor's prepared graph before any downstream snapshot read."""
    app = assembly.get("app") if isinstance(assembly, Mapping) else None
    graph = None
    if isinstance(app, Mapping):
        candidate = app.get("shot_composition_graph") or app.get("canonical_shot_composition")
        if isinstance(candidate, Mapping) and isinstance(candidate.get("shot_revisions"), list):
            graph = candidate
    if graph is None:
        return deepcopy(dict(assembly)), deepcopy(dict(registry))
    from .shot_composition_projection import project_shot_composition

    try:
        projected = project_shot_composition(graph, base_config=assembly, base_registry=registry)
    except ValueError as exc:
        raise SnapshotIntegrityError(f"canonical shot-composition projection failed: {exc}") from exc
    return deepcopy(dict(projected.config)), deepcopy(dict(projected.registry))


def _resolve_media_hashes(
    registry: dict[str, Any],
    *,
    project_ref: str | None,
    runtime_client: Any | None = None,
    media_snapshot: Any | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Record digests admitted by the neutral runtime's scoped object read.

    Deterministic diagnostics make every unadmitted entry explicit. No URL,
    project path, CAS locator, or filesystem fingerprint is read here.
    """

    if project_ref is None:
        return {}, []
    assets = registry.get("assets", {})
    if not isinstance(assets, dict):
        raise SnapshotIntegrityError("registry.assets must be an object")

    hashes: dict[str, str] = {}
    diagnostics: list[str] = []
    for asset_key in sorted(assets):
        entry = assets[asset_key]
        if not isinstance(entry, dict):
            diagnostics.append(
                f"MEDIA_INVALID_ENTRY: asset {asset_key!r} is not an object"
            )
            continue
        integrity = classify_asset(
            asset_key,
            entry,
            project_ref=project_ref,
            runtime_client=runtime_client,
            media_snapshot=media_snapshot,
        )
        if integrity.state == "verified_original" and integrity.observed_sha256:
            hashes[asset_key] = integrity.observed_sha256
        else:
            diagnostics.append(
                f"MEDIA_{integrity.state.upper()}: asset {asset_key!r} "
                "is not runtime-managed"
            )
    return hashes, diagnostics


def _build_snapshot(
    raw_events: list[dict[str, Any]],
    parsed_events: list[TimelineEvent],
    *,
    timeline_id: str,
    timeline_ulid: str,
    display: dict[str, Any] | None,
    slug: str | None,
    project_slug: str,
    project_ref: str,
    diagnostics: Sequence[str],
    runtime_client: Any | None = None,
    media_snapshot: Any | None = None,
) -> TimelineSnapshot:
    try:
        assembly = project_to_assembly(parsed_events)
    except Exception as exc:
        raise SnapshotIntegrityError(f"event projection failed: {exc}") from exc
    registry, registry_diagnostics = _registry_from_events(parsed_events)
    assembly, registry = _project_canonical_composition(assembly, registry)
    structural_errors = validate_structural(assembly)
    if structural_errors:
        raise SnapshotIntegrityError(
            "projected assembly is invalid: " + "; ".join(structural_errors)
        )

    media_hashes, media_diagnostics = _resolve_media_hashes(
        registry,
        project_ref=project_ref,
        runtime_client=runtime_client,
        media_snapshot=media_snapshot,
    )
    last = parsed_events[-1] if parsed_events else None
    snapshot = TimelineSnapshot(
        timeline_id=timeline_id,
        timeline_ulid=timeline_ulid,
        slug=slug,
        project_slug=project_slug,
        head_version=len(parsed_events),
        last_event_id=last.event_id if last is not None else None,
        last_hash=last.hash if last is not None else None,
        assembly=assembly,
        registry=registry,
        display=display,
        events=deepcopy(raw_events),
        media_hashes=media_hashes,
        assembly_sha256=_canonical_digest(assembly),
        registry_sha256=_canonical_digest(registry),
        # Transcript authority is deliberately deferred to R19.  Never guess
        # a transcript from neighboring filenames or generic registry assets.
        transcript_sha256=None,
        diagnostics=_dedupe_diagnostics(
            [*diagnostics, *registry_diagnostics, *media_diagnostics]
        ),
    )
    return snapshot


def verify_frozen(
    snapshot: TimelineSnapshot,
    *,
    expect_version: int | None = None,
) -> list[str]:
    """Re-verify only the data frozen into *snapshot* and return diagnostics."""

    diagnostics = list(snapshot.diagnostics)
    try:
        _raw_events, parsed_events = _parse_events(snapshot.events)
    except SnapshotIntegrityError as exc:
        diagnostics.append(f"EVENT_SCHEMA_INVALID: {exc}")
        return list(_dedupe_diagnostics(diagnostics))

    diagnostics.extend(
        _chain_diagnostics(parsed_events, timeline_id=snapshot.timeline_id)
    )
    if expect_version is not None:
        if (
            isinstance(expect_version, bool)
            or not isinstance(expect_version, int)
            or expect_version < 0
        ):
            diagnostics.append(
                "EXPECTED_VERSION_INVALID: expect_version must be a non-negative integer"
            )
        elif snapshot.head_version != expect_version:
            diagnostics.append(
                "EXPECTED_VERSION_MISMATCH: "
                f"expected {expect_version}, found {snapshot.head_version}"
            )

    if snapshot.head_version != len(parsed_events):
        diagnostics.append(
            "HEAD_VERSION_MISMATCH: "
            f"snapshot says {snapshot.head_version}, events contain {len(parsed_events)}"
        )
    expected_event_id = parsed_events[-1].event_id if parsed_events else None
    expected_hash = parsed_events[-1].hash if parsed_events else None
    if snapshot.last_event_id != expected_event_id:
        diagnostics.append(
            "HEAD_EVENT_ID_MISMATCH: snapshot tail does not match frozen events"
        )
    if snapshot.last_hash != expected_hash:
        diagnostics.append(
            "HEAD_HASH_MISMATCH: snapshot tail hash does not match frozen events"
        )

    try:
        replayed = project_to_assembly(parsed_events)
        replayed, _replayed_registry = _project_canonical_composition(replayed, snapshot.registry)
        structural_errors = validate_structural(replayed)
        for error in structural_errors:
            diagnostics.append(f"ASSEMBLY_INVALID: {error}")
        if replayed != snapshot.assembly:
            diagnostics.append(
                "ASSEMBLY_REPLAY_MISMATCH: frozen assembly differs from event replay"
            )
    except Exception as exc:
        diagnostics.append(f"ASSEMBLY_REPLAY_FAILED: {exc}")

    try:
        observed_assembly_hash = _canonical_digest(snapshot.assembly)
        if observed_assembly_hash != snapshot.assembly_sha256:
            diagnostics.append(
                "ASSEMBLY_DIGEST_MISMATCH: frozen assembly digest is incorrect"
            )
    except SnapshotIntegrityError as exc:
        diagnostics.append(f"ASSEMBLY_DIGEST_INVALID: {exc}")

    try:
        replayed_registry, registry_warnings = _registry_from_events(parsed_events)
        diagnostics.extend(registry_warnings)
        if replayed_registry != snapshot.registry:
            diagnostics.append(
                "REGISTRY_REPLAY_MISMATCH: frozen registry differs from the last registry event"
            )
    except SnapshotIntegrityError as exc:
        diagnostics.append(f"REGISTRY_REPLAY_FAILED: {exc}")

    try:
        _validate_registry_envelope(snapshot.registry)
        observed_registry_hash = _canonical_digest(snapshot.registry)
        if observed_registry_hash != snapshot.registry_sha256:
            diagnostics.append(
                "REGISTRY_DIGEST_MISMATCH: frozen registry digest is incorrect"
            )
    except (SnapshotIntegrityError, TypeError, ValueError) as exc:
        diagnostics.append(f"REGISTRY_INVALID: {exc}")

    try:
        snapshot.sns()
    except (TypeError, ValueError) as exc:
        diagnostics.append(f"SNS_INVALID: {exc}")
    return list(_dedupe_diagnostics(diagnostics))


__all__ = [
    "ConcurrentAppendError",
    "SnapshotIntegrityError",
    "TimelineSnapshot",
    "verify_frozen",
]
